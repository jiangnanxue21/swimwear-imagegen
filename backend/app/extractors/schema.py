"""识别 Schema(M3)。**由注册表生成,不手写。**

手写一份的代价在 M0 文档里说过:spec 引用了一个识别层根本不会输出的字段,
要到 M7 做真实映射时才会发现,而那时候改的是提示词、Schema、阈值配置和 spec 四处。

这里只做一件事:把 `app/attributes/registry.py` 翻译成模型能理解的
JSON Schema 与提示词片段。零依赖,可离线穷举测试。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.attributes.registry import REGISTRY, enum_definitions, extractable_fields
from app.core.enums import Audience, MissingReason

#: 模型必须输出的顶层键。
#:
#: `unreadable_fields` 不是可选的:没有它,「看不清」只能表现为一个低置信度的
#: 猜测值,而那和「有把握但就是这个值」在数值上是同一个东西。
#:
#: `missing` 是 A45-batch14 / §4.5 加的第三个键:`unreadable_fields` 的带原因版。
#: 两个键并存不是冗余 —— `unreadable_fields` 是 v1 契约,Mock 和已写的用例
#: 都按它产出;`missing` 让真实模型能区分"看不清"和"这个角度判断不了"。
#: 落库层把前者按 INSUFFICIENT_EVIDENCE 归并,只有一条"无值证据"的写入路径。
TOP_LEVEL_KEYS = ("fields", "unreadable_fields", "missing")

#: 识别 Schema 版本。落到 `product_attribute_extractions.schema_version` 上,
#: 校准分箱按 (字段 × 模型 × Prompt) 查,而"当时的输出契约长什么样"靠它回答。
#: v1 没有 `missing` 通道;v2 起有。
EXTRACTION_SCHEMA_VERSION = "2"

#: 模型允许自报的缺失原因。**刻意不含 AWAITING_SUPPLIER_DATA**:
#: 那个取值的意思是"看图永远判断不了",而非视觉字段根本不进识别 Schema ——
#: 模型没有资格对一个我们没问它的字段宣布"等供应商数据"。
#: 它留在 `MissingReason` 枚举里,给导入与人工标记那条路用。
MODEL_MISSING_REASONS: tuple[str, ...] = (
    MissingReason.INSUFFICIENT_EVIDENCE.value,
    MissingReason.NOT_VISUALLY_DETERMINABLE.value,
)


def build_extraction_schema(
    fields: tuple[str, ...] | None = None,
    *,
    audience: Audience | None = None,
) -> dict[str, Any]:
    """生成结构化输出 Schema。

    只包含 `visual=True` **且本受众适用**的字段(PRD v2 §18.3)——
    看图看不出来的(材质、成分、克重)和这件商品上不存在的(男装的领型、
    女装的腰头)**都禁止出现在这里**。这是「不让模型猜」那条规矩的执行点,
    不是提示词里的一句叮嘱。显式传 `fields` 时以 fields 为准(增量识别),
    但仍会拦下不可识别与不适用的字段。
    """
    names = tuple(fields or extractable_fields(audience))
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(f"未注册的字段:{unknown}")
    blocked = [n for n in names if not REGISTRY[n].visual]
    if blocked:
        # 抛错而不是静默过滤:调用方显式点名了一个不可识别的字段,
        # 那是它的 bug,悄悄忽略会让它以为识别过了
        raise ValueError(f"这些字段看图判断不了,不允许进识别 Schema:{blocked}")
    if audience is not None:
        off_audience = [n for n in names if audience not in REGISTRY[n].applies_to]
        if off_audience:
            # 同一条规矩的受众版:这件商品上不存在的字段,不许让模型猜。
            # 泳裤的"领型"会得到一个非常自信的编造值(PRD v2 §18.3)
            raise ValueError(
                f"这些字段在受众 {audience.value} 的商品上不存在,"
                f"不允许进识别 Schema:{off_audience}"
            )

    enums = enum_definitions(audience)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(TOP_LEVEL_KEYS),
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "value", "confidence"],
                    "properties": {
                        "name": {"type": "string", "enum": list(names)},
                        # value 不在这里限枚举:一个字段一套取值,
                        # JSON Schema 表达"name 是 A 时 value 只能取 A 的枚举"
                        # 需要 oneOf 展开,很多端点不支持。取值约束放提示词 +
                        # 后端归一化,归一失败的证据 normalized_value 为 NULL
                        "value": {"type": ["string", "array", "number", "boolean", "null"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "evidence": {"type": "string"},
                    },
                },
            },
            "unreadable_fields": {
                "type": "array",
                "items": {"type": "string", "enum": list(names)},
            },
            "missing": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "reason"],
                    "properties": {
                        "name": {"type": "string", "enum": list(names)},
                        "reason": {
                            "type": "string",
                            "enum": list(MODEL_MISSING_REASONS),
                        },
                    },
                },
            },
        },
        "_enum_hints": enums,   # 提示词用,不进 Schema 本身
    }


def api_ready_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """把 `build_extraction_schema()` 的产物变成能直接塞进请求体的形状。

    做两件事,两件都是"厂商 strict 模式的现实"而不是我们的偏好:

    1. 摘掉 `_enum_hints` —— 那个键是给提示词生成用的,发出去会被
       strict 校验以 `additionalProperties` 之名整个拒掉;
    2. strict json_schema 要求每层 `required` 列出**全部** properties。
       `fields[].evidence` 在我们的契约里是可选的(§6.1:理由仅供人看),
       但 strict 模式下"可选"不存在 —— 补进 required,模型没话说时给空串,
       解析层把空串当 None。**契约本身不变**,变的只是发给厂商的那份投影。
    """
    import copy

    out = {k: copy.deepcopy(v) for k, v in schema.items() if k != "_enum_hints"}
    items = out.get("properties", {}).get("fields", {}).get("items", {})
    props = items.get("properties", {})
    if "evidence" in props:
        required = list(items.get("required", []))
        if "evidence" not in required:
            required.append("evidence")
        items["required"] = required
    return out


def build_extraction_prompt(
    fields: tuple[str, ...] | None = None,
    *,
    audience: Audience | None = None,
) -> str:
    """识别提示词。

    三条约束(§6.1):枚举值只能取 `core/enums.py` 的值、不问模型要
    confidence 以外的任何决定、看不清必须进 `unreadable_fields`。

    **不包含 `known`、不包含商品标题、不包含供应商描述** —— 这些都是锚定源。
    模型看到 `{"primary_color": "NAVY"}` 之后就会把图判成 NAVY,
    所谓交叉验证只是复读我们喂进去的答案。
    """
    names = tuple(fields or extractable_fields(audience))
    enums = enum_definitions(audience)
    lines = [
        "你在看一张服装商品图片。请**只根据这张图片**判断下列属性。",
        "",
        "严格要求:",
        "1. 只输出 JSON,不要任何解释性文字。",
        "2. 没有把握的字段**不要猜一个值**,放进 missing 并说明原因:"
        "看不清、被遮挡、分辨率不够用 INSUFFICIENT_EVIDENCE;"
        "这张图的角度或内容根本判断不了(比如正面图判断背部款式)"
        "用 NOT_VISUALLY_DETERMINABLE。没有其他取值。"
        "unreadable_fields 与 missing 含义相同,填 missing 即可,"
        "unreadable_fields 给空数组。",
        "3. confidence 是你对这个判断的把握程度(0-1)。"
        "不要输出等级、评分、是否合格之类的结论 —— 那些由后端决定。",
        "4. 枚举字段只能取下面列出的值,不要自造。"
        "也不要输出字段清单之外的任何字段 —— 清单外的输出一律作废并被记录。",
        "",
        "需要判断的字段:",
    ]
    for name in names:
        spec = REGISTRY[name]
        allowed = enums.get(name)
        suffix = f",取值:{'、'.join(allowed)}" if allowed else "(自由文本)"
        multi = ",可多值" if spec.multi_value else ""
        lines.append(f"  - {name}{multi}{suffix}")
    return "\n".join(lines)


def normalize_value(field_name: str, raw: Any) -> str | None:
    """把模型输出归一到注册表声明的取值。归一不了返回 None。

    返回 None 不是失败 —— 证据照样落库,只是 `normalized_value` 为空,
    合并时不参与投票。**模型自造的枚举值必须在这里被拦下**,
    否则它会一路流到平台字段映射,变成一个平台不认识的值。
    """
    spec = REGISTRY.get(field_name)
    if spec is None or raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        # 多值字段的归一在合并层做,这里只投影单值
        return None
    text = str(raw).strip()
    if not text:
        return None
    if spec.enum is None:
        return text[:128]
    allowed = {v.value.upper(): v.value for v in spec.enum}
    return allowed.get(text.upper())


def enum_hint_for(field_name: str) -> tuple[str, ...]:
    return enum_definitions().get(field_name, ())


def schema_matches_registry(schema: Mapping[str, Any]) -> bool:
    """Schema 里的字段名是否与注册表一致。给测试与启动期自检用。"""
    props = schema.get("properties", {})
    names = set(props.get("fields", {}).get("items", {})
                .get("properties", {}).get("name", {}).get("enum", []))
    return names == set(extractable_fields())


# ---------------------------------------------------------------- 响应解析


class ExtractionParseError(ValueError):
    """模型返回的文本不符合识别契约。**必须失败,不给默认值。**

    与评分层的 `EvaluationParseError` 是同一条规矩的识别版:解析不了时
    静默给一个空结果,表现是"这张图识别成功但一个字段都没有",
    和"模型真的什么都没看出来"在库里长得一模一样 —— 而前者该重试,
    后者该换图。带 `detail` 是为了让识别记录里留得下"到底哪里不合契约"。
    """

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


#: 解析失败时留进日志的原文上限。够看清 JSON 坏在哪(通常坏在开头或结尾),
#: 又不至于让一条日志把整个采集管道撑爆 —— 模型输出上限由
#: `EXTRACTOR_MODEL_MAX_OUTPUT_TOKENS` 兜着,但那个值是可配的
LOG_EXCERPT_LIMIT = 600

#: 只认 http(s)。模型输出里能带凭据的现实载体只有一种:预签名地址的查询串。
#: `EXTRACTOR_MODEL_SEND_PUBLIC_URLS=true` 时,预签名地址是**我们送进提示词的**,
#: 模型完全可能在 `evidence_text` 里把它抄回来
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>\\]+")


def redact_for_log(text: str, *, limit: int = LOG_EXCERPT_LIMIT) -> str:
    """把模型原文变成可以进日志的样子:抹掉查询串,再截断。

    ## 为什么要有这个函数

    `service.py` 的那条 catch-all 只记 `type(exc).__name__`,理由写在注释里:
    「错误原文不入库(可能带 URL 里的凭据)」。这对 provider / 传输错误是对的 ——
    它们的消息里嵌着请求地址。但它把 `ExtractionParseError` 也一起吞了,
    而那一条是**我们自己的解析器**对**模型输出**报的错:消息是我们自己写的字符串,
    `detail` 是 json 解码器的位置信息,两者都不含请求凭据。

    代价不是"日志少一行":模型开始返回坏 JSON 时(`json_object` / `prompt_only`
    两个降级档位连 Schema 都没有,这是**今天就会发生**的事),运营看到的是
    那张图 failed、日志里一句 `error: ExtractionParseError`,
    **没有任何办法知道模型到底说了什么**。唯一想得到的动作是再点一次识别,
    而每一次都在花钱 —— 与 §4.5 那三个 `missing_reason` 指向三个不同动作
    是同一个病灶,只是换到了日志这条通道上。

    ## 抹的是查询串,不是整个地址

    整个抹掉的话日志里就查不出是哪张图了,而"哪张图"是排查坏 JSON 的起点。
    `partition("?")` 保留 scheme + host + path,只丢查询串 —— 凭据只在那里。

    ## 关于顺序

    实现是先抹后截。**这不是一条安全边界** —— 走查时曾把它当成一条,
    是错的:`_URL_IN_TEXT` 对被截断的半截地址一样匹配得上
    (它只要求 `https?://` 开头、到空白为止),`partition("?")` 照样削掉
    查询串。先截一个窗口再抹同样不泄漏。这个说法是 batch14-3 的一条
    变异(P1)验出来站不住的 —— **一条守卫咬不住的"边界",多半是那个
    "边界"本来就不存在**。

    先抹后截的真实理由只是简单:正则对整串走一遍是线性的,
    模型输出本来就有 token 上限,不值得为它多一个窗口参数。

    真正承重的是下面这两条,各有一条变异钉着:
    `partition` 必须按 `?` 切(按 `&` 切会把第一个参数留下,
    而签名通常就是第一个),截断必须真的发生且要自报截断。
    """
    redacted = _URL_IN_TEXT.sub(_strip_query, text)
    if len(redacted) <= limit:
        return redacted
    return redacted[:limit] + f"…(截断,共 {len(text)} 字符)"


def _strip_query(match: re.Match[str]) -> str:
    head, sep, _ = match.group(0).partition("?")
    return head + ("?<已抹去>" if sep else "")


def _coerce_json_object(text: str) -> dict[str, Any]:
    """散文与半截 JSON 在这里被拒绝。容忍 ```json 围栏(模型最常见的画蛇添足)。"""
    import json

    if not isinstance(text, str) or not text.strip():
        raise ExtractionParseError("模型返回内容为空")
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.lower().startswith("json"):
            body = body[4:]
        body = body.strip()
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ExtractionParseError(
            "返回内容不是合法 JSON", detail={"reason": str(exc)}
        ) from exc
    if not isinstance(decoded, dict):
        raise ExtractionParseError("JSON 顶层必须是对象")
    return decoded


def parse_extraction_payload(text: str):
    """把模型返回的文本解析成 `ImageExtractionResult`。**只解析,不过滤。**

    "只解析"是刻意的边界:字段在不在本次目标里、在不在注册表里,
    是 `run_extraction` 的事(它要数编造、要写审计)。在这里顺手丢掉的话,
    §6.3 的"不可见属性编造率"永远数不出来 —— 被丢掉的东西没有留下痕迹。

    fail-closed 的部分是**形状**:

        fields 不是数组 / 项缺 name / confidence 越界   -> ExtractionParseError
        missing 的 reason 不在模型允许的两个取值里       -> 降级成
            INSUFFICIENT_EVIDENCE(原因是诊断信息,不参与任何判定;
            "没有值"这个决定性的部分原样保留,降级方向不会凭空造出值来)

    confidence 缺失按 None 处理而不是报错:json_object / prompt_only 档位的
    端点没有 Schema 兜着,模型漏一个 confidence 不该毁掉整张图的其余字段 ——
    None 在合并层的含义是"权重 0",fail-closed 语义没有松动。
    """
    from app.extractors.base import FieldMissing, FieldObservation

    decoded = _coerce_json_object(text)

    raw_fields = decoded.get("fields")
    if raw_fields is None:
        raw_fields = []
    if not isinstance(raw_fields, list):
        raise ExtractionParseError("fields 必须是数组")

    observations: list[FieldObservation] = []
    for index, item in enumerate(raw_fields):
        if not isinstance(item, dict):
            raise ExtractionParseError(f"fields[{index}] 不是对象")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ExtractionParseError(f"fields[{index}] 缺少字段名")
        confidence = item.get("confidence")
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ExtractionParseError(
                    f"fields[{index}].confidence 不是数字",
                    detail={"field": name.strip()[:64]},
                )
            if not 0 <= float(confidence) <= 1:
                raise ExtractionParseError(
                    f"fields[{index}].confidence 越界:{confidence}",
                    detail={"field": name.strip()[:64]},
                )
            confidence = float(confidence)
        evidence = item.get("evidence")
        evidence = evidence.strip() if isinstance(evidence, str) else None
        observations.append(
            FieldObservation(
                name=name.strip(),
                value=item.get("value"),
                confidence=confidence,
                evidence=evidence or None,
            )
        )

    unreadable = decoded.get("unreadable_fields")
    if unreadable is None:
        unreadable = []
    if not isinstance(unreadable, list) or any(
        not isinstance(n, str) for n in unreadable
    ):
        raise ExtractionParseError("unreadable_fields 必须是字符串数组")

    raw_missing = decoded.get("missing")
    if raw_missing is None:
        raw_missing = []
    if not isinstance(raw_missing, list):
        raise ExtractionParseError("missing 必须是数组")
    missing: list[FieldMissing] = []
    for index, item in enumerate(raw_missing):
        if not isinstance(item, dict):
            raise ExtractionParseError(f"missing[{index}] 不是对象")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ExtractionParseError(f"missing[{index}] 缺少字段名")
        reason = str(item.get("reason") or "").strip().upper()
        if reason not in MODEL_MISSING_REASONS:
            reason = MissingReason.INSUFFICIENT_EVIDENCE.value
        missing.append(FieldMissing(name=name.strip(), reason=reason))

    from app.extractors.base import ImageExtractionResult

    return ImageExtractionResult(
        fields=tuple(observations),
        unreadable_fields=tuple(n.strip() for n in unreadable if n.strip()),
        missing=tuple(missing),
    )
