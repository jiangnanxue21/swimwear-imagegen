"""文案生成(任务书 FE-221 / BE-202)。

## 两种生成器,同一个契约

    template   规则模板。确定性、零成本、离线可跑,**默认**
    llm        文本大模型。结构化 JSON 输出,需要配 TEXT_MODEL_*

`generate()` 的返回值形状两者完全一致:标题、卖点、描述、关键词,
外加 `claims` —— 模型(或模板)对自己写了什么的结构化标注。
落库和校验都不关心它是谁写的。

## 为什么默认是模板而不是大模型

任务书 §6.1 有一条:「后端依赖未完成时显式标记,允许使用 Mock 演示,
但不得把 Mock 通过视为阶段通过」。模板生成器**不是 Mock**:它读的是
真实的已确认属性,产出的是真实可上架的英文文案,claims 逐条能过校验。
运营用它跑完的闭环是真的闭环。

大模型那条路写在这里、随时可切,但它的价值是文案更好看,不是流程能不能跑通。
把闭环的成立与否押在一个还没配置的外部服务上,是这类项目最常见的拖期原因。

## claims 不是模型的判断

它是**对自己文本的结构化标注**:「我在标题第 12-21 个字符处写了 Navy Blue,
对应 primary_color=NAVY_BLUE」。后端逐条机械验证(`copy_rules.validate_claims`),
标错了校验就过不去。所以它和「不采信模型自报结论」不冲突。
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from app.core.errors import ErrorCode, ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: 生成器名。进 `listing_copies.model_name`,于是「这一版是谁写的」永远可查。
TEMPLATE_GENERATOR = "template"
LLM_GENERATOR = "llm"

#: 模板版本。改了措辞就要动它 —— 落库的 prompt_version 靠它回答
#: 「三个月前那一版为什么和现在不一样」。
TEMPLATE_VERSION = "template-1"

#: LLM 提示词版本。原来 `prompt_version` 里硬写的是字面量 `1`,于是改了
#: `build_prompt` 的措辞之后,落库的版本号一动不动 —— 「这一版为什么和
#: 上一版不一样」就没法回答了,幂等指纹也认不出提示词变过(评审第 3 条)。
PROMPT_VERSION = "llm-1"


def _require_str(data: Mapping[str, Any], key: str, *, where: str) -> str:
    """取一个必须是字符串的字段。**不做 `str()` 强转。**

    强转看起来宽容,实际是把模型的结构错误变成一个合法值:
    `{"title": {"en": "..."}}` 会变成字符串 `"{'en': '...'}"` 进标题列,
    而那一列有长度校验、有必填校验,全都过得去。
    """
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(
            f"{where} 的 {key} 必须是字符串,收到 {type(value).__name__}",
            code=ErrorCode.COPY_COMPLIANCE_FAILED,
        )
    return value


def _require_str_list(data: Mapping[str, Any], key: str, *, where: str) -> list[str]:
    """取一个必须是字符串数组的字段。

    **字符串本身要被拒绝。** `tuple(str(b) for b in "柔软面料")` 是合法的
    Python,结果是四条单字卖点 —— 类型对、长度检查也过,一路写进 Excel
    才会被人看见。这是这次收紧里唯一一个「原来不报错、现在报错」
    却明确是修复的地方。
    """
    value = data.get(key)
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValidationError(
            f"{where} 的 {key} 必须是字符串数组,收到 {type(value).__name__}",
            code=ErrorCode.COPY_COMPLIANCE_FAILED,
        )
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValidationError(
                f"{where} 的 {key}[{index}] 必须是字符串,收到 {type(item).__name__}",
                code=ErrorCode.COPY_COMPLIANCE_FAILED,
            )
        out.append(item)
    return out


#: 可以被单独重新生成的字段(FE-222)。
#:
#: **重生只覆盖被选中的字段。** 一次「只想换个标题」的操作把描述也换掉,
#: 是这类界面最招人烦的行为 —— 运营刚手工改好的那段没了,而且没有撤销。
REGENERABLE_FIELDS: tuple[str, ...] = (
    "title",
    "bullet_points",
    "description",
    "keywords",
)


@dataclass(frozen=True)
class CopyDraft:
    """一次生成的产物。**纯数据**,不带任何数据库身份。"""

    title: str
    bullet_points: tuple[str, ...]
    description: str
    keywords: tuple[str, ...]
    claims: tuple[Mapping[str, Any], ...] = ()
    generator: str = TEMPLATE_GENERATOR
    prompt_version: str = TEMPLATE_VERSION
    #: 生成器自己知道自己没能力做到的事。**不是错误**,是给人看的说明
    notes: tuple[str, ...] = ()
    #: 仅保存可安全展示的调用摘要,不含提示词、响应正文或密钥。
    trace: Mapping[str, Any] = field(default_factory=dict)

    def as_copy_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "bullet_points": list(self.bullet_points),
            "description": self.description,
            "keywords": list(self.keywords),
        }


class CopyGenerator(Protocol):
    name: str
    billable: bool
    usage_provider: str

    def is_configured(self) -> bool: ...

    def generate(
        self,
        *,
        facts: Mapping[str, Any],
        selling_points: Sequence[str],
        forbidden_claims: Sequence[str],
        locale: str,
        only_fields: Sequence[str] | None = None,
        previous: Mapping[str, Any] | None = None,
    ) -> CopyDraft: ...


# ==========================================================
# 词形:把枚举值变成人话
# ==========================================================

#: 枚举值 -> 英文表达。**只覆盖注册表里真实存在的取值**。
#:
#: 没有兜底的「把下划线换成空格再首字母大写」是刻意的:那样
#: `BIKINI_SET` 会变成 `Bikini Set`(碰巧对),而某个将来加的
#: `UPF50_LINED` 会变成 `Upf50 Lined`(错得很难看)。查不到就
#: 不往文案里写这个词,而不是写一个凑合的。
ENUM_WORDS: Mapping[str, Mapping[str, str]] = {
    "garment_type": {
        "ONE_PIECE": "One Piece Swimsuit",
        "BIKINI_TOP": "Bikini Top",
        "BIKINI_BOTTOM": "Bikini Bottom",
        "BIKINI_SET": "Bikini Set",
        "TANKINI": "Tankini",
        "COVER_UP": "Cover Up",
        "SWIM_SHORTS": "Swim Shorts",
        "DRESS": "Dress",
        "TOP": "Top",
    },
    "neckline_type": {
        "V_NECK": "V Neck",
        "SCOOP": "Scoop Neck",
        "SQUARE": "Square Neck",
        "SWEETHEART": "Sweetheart Neck",
        "HIGH_NECK": "High Neck",
        "PLUNGE": "Plunge Neck",
        "BANDEAU": "Bandeau",
    },
    "back_style": {
        "OPEN_BACK": "Open Back",
        "RACER_BACK": "Racer Back",
        "CROSS_BACK": "Cross Back",
        "TIE_BACK": "Tie Back",
        "FULL_BACK": "Full Back",
        "LACE_UP": "Lace Up Back",
    },
    "strap_type": {
        "HALTER": "Halter",
        "SPAGHETTI": "Spaghetti Strap",
        "WIDE": "Wide Strap",
        "STRAPLESS": "Strapless",
        "ONE_SHOULDER": "One Shoulder",
        "CRISS_CROSS": "Criss Cross Strap",
    },
    "pattern_type": {
        "SOLID": "Solid",
        "STRIPE": "Striped",
        "FLORAL": "Floral",
        "ANIMAL": "Animal Print",
        "GEOMETRIC": "Geometric",
        "TIE_DYE": "Tie Dye",
        "COLOR_BLOCK": "Color Block",
        "PRINT_LOGO": "Logo Print",
    },
}

#: 颜色是 TEXT_LIST,取值来自我们自己的规范色名,不是枚举。
#: 这里只做形态转换,不做词表映射 —— 词表映射属于渠道层的 dict。
_COLOR_CLEAN = re.compile(r"[_\-]+")


def humanize(field_name: str, value: Any) -> str | None:
    """枚举值 -> 展示词。查不到返回 None,**不猜**。"""
    if value in (None, ""):
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
        if value is None:
            return None
    text = str(value).strip()
    table = ENUM_WORDS.get(field_name)
    if table is not None:
        return table.get(text.upper())
    if field_name in ("primary_color", "secondary_colors"):
        words = [w for w in _COLOR_CLEAN.sub(" ", text).split() if w]
        return " ".join(w.capitalize() for w in words) or None
    return text or None


# ==========================================================
# 模板生成器
# ==========================================================


@dataclass
class TemplateCopyGenerator:
    """规则模板。**确定性**:同一份 facts 永远得到同一份文案。

    确定性不是省事,是让「改了颜色之后文案哪里变了」这个问题有答案。
    随机化的生成器每重跑一次都全文变化,过期比对就退化成「整篇都不一样」。
    """

    name: str = TEMPLATE_GENERATOR
    billable: bool = False
    usage_provider: str = TEMPLATE_GENERATOR

    def is_configured(self) -> bool:
        return True

    # ---------------------------------------------------------- 各字段

    def _title(self, facts: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        """标题 = 颜色 + 图案 + 领型 + 品类。

        顺序按消费者搜索时的说法排:先说颜色,最后说是什么东西。
        每一段都同时产出一条 claim,于是「标题里写了什么」和
        「它对应哪个已确认属性」是同一次决定的产物,不可能对不上。
        """
        claims: list[dict[str, Any]] = []
        parts: list[str] = []

        for name in ("primary_color", "pattern_type", "neckline_type", "garment_type"):
            word = humanize(name, facts.get(name))
            if not word:
                continue
            if word in parts:
                continue
            parts.append(word)
            claims.append(
                {
                    "field_name": name,
                    "value": _first(facts.get(name)),
                    "text_span": word,
                    "location": "title",
                }
            )

        title = " ".join(parts).strip()
        return title, claims

    def _bullets(self, facts: Mapping[str, Any], selling_points: Sequence[str]) -> list[str]:
        """卖点。先用人工卖点库,不够再从 facts 补。

        `copy_rules` 要求 3-5 条。补不满 3 条时**不编** —— 宁可让校验
        报「卖点条数不足」,也不要产出一句和这件商品无关的漂亮话。
        """
        bullets: list[str] = [p.strip() for p in selling_points if p.strip()]

        derived: list[tuple[str, str]] = [
            ("garment_type", "Classic {} silhouette"),
            ("neckline_type", "{} for a flattering neckline"),
            ("back_style", "{} design"),
            ("strap_type", "{} straps for a secure fit"),
            ("pattern_type", "{} pattern"),
            ("material", "Made from {}"),
        ]
        for name, pattern in derived:
            if len(bullets) >= 5:
                break
            word = humanize(name, facts.get(name))
            if not word:
                continue
            sentence = pattern.format(word)
            if sentence not in bullets:
                bullets.append(sentence)
        return bullets[:5]

    def _description(self, facts: Mapping[str, Any], bullets: Sequence[str]) -> str:
        color = humanize("primary_color", facts.get("primary_color"))
        garment = humanize("garment_type", facts.get("garment_type"))
        opening_bits = [b for b in (color, garment) if b]
        opening = (
            f"This {' '.join(opening_bits).lower()} is designed for everyday "
            "swimming and beach days."
            if opening_bits
            else "Designed for everyday swimming and beach days."
        )
        body = " ".join(f"{b}." for b in bullets if not b.endswith("."))
        closing = "Please refer to the size chart before ordering."
        return " ".join(x for x in (opening, body, closing) if x).strip()

    def _keywords(self, facts: Mapping[str, Any]) -> list[str]:
        words: list[str] = []
        for name in ("garment_type", "primary_color", "pattern_type", "neckline_type"):
            word = humanize(name, facts.get(name))
            if word and word.lower() not in words:
                words.append(word.lower())
        if "swimwear" not in words:
            words.append("swimwear")
        return words[:8]

    # ---------------------------------------------------------- 入口

    def generate(
        self,
        *,
        facts: Mapping[str, Any],
        selling_points: Sequence[str] = (),
        forbidden_claims: Sequence[str] = (),
        locale: str = "en",
        only_fields: Sequence[str] | None = None,
        previous: Mapping[str, Any] | None = None,
    ) -> CopyDraft:
        notes: list[str] = []
        if locale != "en":
            # 本地化不是翻译。硬翻出来的巴葡文案读起来像机器翻译,
            # 而这一点消费者一眼就能看出来
            notes.append(
                f"模板生成器只产出英文;{locale} 需要接入文本模型或人工撰写"
            )

        title, title_claims = self._title(facts)
        bullets = self._bullets(facts, selling_points)
        description = self._description(facts, bullets)
        keywords = self._keywords(facts)

        if not title:
            notes.append("颜色与品类都没有已确认的值,标题无法生成")
        if len(bullets) < 3:
            notes.append(f"只凑出 {len(bullets)} 条卖点,不足 3 条;补充已确认属性或手填卖点")

        draft = CopyDraft(
            title=title,
            bullet_points=tuple(bullets),
            description=description,
            keywords=tuple(keywords),
            claims=tuple(title_claims),
            generator=self.name,
            prompt_version=TEMPLATE_VERSION,
            notes=tuple(notes),
        )
        return merge_fields(previous, draft, only_fields)


# ==========================================================
# 单字段重生
# ==========================================================


def merge_fields(
    previous: Mapping[str, Any] | None,
    fresh: CopyDraft,
    only_fields: Sequence[str] | None,
) -> CopyDraft:
    """把新生成的内容合并进旧文案,**只覆盖被点名的字段**(FE-222)。

    `only_fields` 为空表示整篇重生。

    claims 跟着标题走:标题没被重生时保留旧 claims,否则用新的。
    不这么做的话,「只重生描述」会连带把标题的 claims 换成新标题的,
    而新标题并没有被写进这一版 —— 校验会因为 span 找不到而失败,
    而失败原因和运营刚才做的事毫无关系。
    """
    if not only_fields or not previous:
        return fresh

    unknown = [f for f in only_fields if f not in REGENERABLE_FIELDS]
    if unknown:
        raise ValidationError(
            f"不支持重新生成的字段:{', '.join(unknown)}",
            code=ErrorCode.INPUT_INVALID,
        )

    wanted = set(only_fields)
    title = fresh.title if "title" in wanted else str(previous.get("title") or "")
    bullets = (
        fresh.bullet_points
        if "bullet_points" in wanted
        else tuple(previous.get("bullet_points") or ())
    )
    description = (
        fresh.description
        if "description" in wanted
        else str(previous.get("description") or "")
    )
    keywords = (
        fresh.keywords if "keywords" in wanted else tuple(previous.get("keywords") or ())
    )
    claims = (
        fresh.claims
        if "title" in wanted
        else tuple(previous.get("claims") or fresh.claims)
    )

    return CopyDraft(
        title=title,
        bullet_points=tuple(bullets),
        description=description,
        keywords=tuple(keywords),
        claims=tuple(claims),
        generator=fresh.generator,
        prompt_version=fresh.prompt_version,
        notes=fresh.notes,
        trace=fresh.trace,
    )


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


# ==========================================================
# 文本大模型生成器
# ==========================================================

#: 系统提示词。**规则常量从 `CopyRules` 传进来**,不在这里再写一遍数字 ——
#: 提示词说 120 而校验按 100 判的话,模型会稳定地产出刚好过不了的文案。
LLM_SYSTEM_PROMPT = """你是电商文案撰写助手。严格按 JSON 输出,不要任何解释文字。

规则:
1. 只能使用 facts 里给出的属性。**不得新增、修改或推断任何属性值。**
2. 不得出现 forbidden_claims 里的任何宣称。
3. 标题不超过 {title_max} 字符,描述不超过 {description_max} 字符。
4. 卖点 {bullet_min}-{bullet_max} 条,每条不超过 {bullet_item_max} 字符。
5. claims 是你对自己文本的标注:每一条给出 field_name、value、
   text_span(必须在对应 location 的文本中**逐字出现**)、location
   (title / description / bullet.0 / bullet.1 ...)。
6. primary_color 与 garment_type 必须各有至少一条 claim。

输出 JSON:
{{"title": "", "bullet_points": [], "description": "", "keywords": [],
  "claims": [{{"field_name": "", "value": "", "text_span": "", "location": ""}}]}}"""


@dataclass
class LLMCopyGenerator:
    """文本大模型。**未用真实 Key 验证过** —— 与 FASHN 同一状态。

    配置走 `TEXT_MODEL_*`(config.py 里那一组预留配置的第一个真实调用点)。
    没配就 `is_configured()` 返回 False,`get_generator()` 会拒绝选它,
    **不静默退回模板** —— 静默退回的后果是运营以为自己在用大模型,
    而实际拿到的是模板文案,两者质量差别明显却没人知道原因。
    """

    name: str = LLM_GENERATOR
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    api_style: str = "chat_completions"
    timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_base_seconds: float = 0.5
    rules: Any = None
    _client: Any = field(default=None, repr=False)
    _last_trace: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    @property
    def billable(self) -> bool:
        return True

    @property
    def usage_provider(self) -> str:
        from app.llm.endpoint_trust import provider_label_from_base_url

        return provider_label_from_base_url(self.base_url, fallback="text-model")

    @property
    def last_trace(self) -> Mapping[str, Any]:
        return dict(self._last_trace)

    def is_configured(self) -> bool:
        return bool(self.base_url and self.model)

    def build_prompt(
        self,
        *,
        facts: Mapping[str, Any],
        selling_points: Sequence[str],
        forbidden_claims: Sequence[str],
        locale: str,
        only_fields: Sequence[str] | None = None,
        previous: Mapping[str, Any] | None = None,
    ) -> tuple[str, str]:
        """(system, user)。**可离线断言**,不发请求。"""
        from app.listings.copy_rules import CopyRules

        rules = self.rules or CopyRules()
        system = LLM_SYSTEM_PROMPT.format(
            title_max=rules.title_max,
            description_max=rules.description_max,
            bullet_min=rules.bullet_min,
            bullet_max=rules.bullet_max,
            bullet_item_max=rules.bullet_item_max,
        )
        payload: dict[str, Any] = {
            "locale": locale,
            "facts": dict(facts),
            "selling_points": list(selling_points),
            "forbidden_claims": list(forbidden_claims),
        }
        if only_fields:
            payload["regenerate_only"] = list(only_fields)
            payload["previous"] = dict(previous or {})
        return system, json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def parse_response(self, text: str) -> CopyDraft:
        """解析模型输出。**结构不对就整体拒收**,不做修补式解析。

        「把半截 JSON 补全再用」这类兜底在评分层已经被证明是坏主意:
        它把「模型没按约定输出」这个信号吞掉了,而那个信号正是
        提示词需要改进的唯一证据。

        ## 原来的宽松解析漏了什么

        `tuple(str(b) for b in data.get("bullet_points") or [])` 对字符串
        同样成立 —— 模型返回 `"bullet_points": "柔软面料"` 时,这一行把它
        **逐字符拆开**,得到 `("柔","软","面","料")` 四条卖点。这个结果
        类型完全合法、长度检查也过得去,一路写进 Excel 才会被人看见。

        `str(...)` 强转同理:`"title": {"en": "..."}` 会变成
        `"{'en': '...'}"` 这个字符串进标题列。非法 claim 被 `continue`
        静默丢掉,而 claims 是 §8.4 里唯一能被后端逐字验证的东西,
        丢掉一条等于少验一条。

        所以这里改成:**类型不对就抛,不猜模型想说什么。**
        """
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", cleaned).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                "文案模型返回的不是合法 JSON",
                code=ErrorCode.COPY_COMPLIANCE_FAILED,
            ) from exc
        if not isinstance(data, dict):
            raise ValidationError(
                "文案模型返回的顶层不是对象",
                code=ErrorCode.COPY_COMPLIANCE_FAILED,
            )

        title = _require_str(data, "title", where="文案模型响应")
        description = _require_str(data, "description", where="文案模型响应")
        bullet_points = _require_str_list(data, "bullet_points", where="文案模型响应")
        keywords = _require_str_list(data, "keywords", where="文案模型响应")

        raw_claims = data.get("claims")
        if raw_claims is None:
            raw_claims = []
        if not isinstance(raw_claims, list):
            raise ValidationError(
                f"文案模型响应的 claims 必须是数组,收到 {type(raw_claims).__name__}",
                code=ErrorCode.COPY_COMPLIANCE_FAILED,
            )

        claims = []
        for index, raw in enumerate(raw_claims):
            if not isinstance(raw, dict):
                # 原来这里是 continue。claims 是 §8.4 里唯一能被后端逐字
                # 验证的东西 —— 静默丢掉一条,等于少验一条,而少验的那条
                # 恰恰最可能是模型编出来的
                raise ValidationError(
                    f"文案模型响应的 claims[{index}] 不是对象,"
                    f"收到 {type(raw).__name__}",
                    code=ErrorCode.COPY_COMPLIANCE_FAILED,
                )
            where = f"文案模型响应 claims[{index}]"
            field_name = raw.get("field_name", raw.get("field"))
            if not isinstance(field_name, str) or not field_name.strip():
                raise ValidationError(
                    f"{where} 缺少 field_name",
                    code=ErrorCode.COPY_COMPLIANCE_FAILED,
                )
            claims.append(
                {
                    "field_name": field_name,
                    # `value` 刻意不限类型:facts 里本来就有数字和布尔,
                    # 逐字比对由 copy_rules 做,这里限死反而会拦下合法的
                    "value": raw.get("value"),
                    "text_span": _require_str(raw, "text_span", where=where),
                    "location": _require_str(raw, "location", where=where),
                }
            )

        return CopyDraft(
            title=title,
            bullet_points=tuple(bullet_points),
            description=description,
            keywords=tuple(keywords),
            claims=tuple(claims),
            generator=self.name,
            prompt_version=f"{self.model}:{PROMPT_VERSION}",
        )

    def generate(
        self,
        *,
        facts: Mapping[str, Any],
        selling_points: Sequence[str] = (),
        forbidden_claims: Sequence[str] = (),
        locale: str = "en",
        only_fields: Sequence[str] | None = None,
        previous: Mapping[str, Any] | None = None,
    ) -> CopyDraft:
        if not self.is_configured():
            raise ValidationError(
                "文案模型未配置(TEXT_MODEL_BASE_URL / TEXT_MODEL_NAME)。"
                "在设置页配置,或把 COPY_GENERATOR 切回 template",
                code=ErrorCode.CONFIG_INVALID,
                http_status=503,
            )
        system, user = self.build_prompt(
            facts=facts,
            selling_points=selling_points,
            forbidden_claims=forbidden_claims,
            locale=locale,
            only_fields=only_fields,
            previous=previous,
        )
        text = self._call(system, user)
        parsed = replace(self.parse_response(text), trace=dict(self._last_trace))
        return merge_fields(previous, parsed, only_fields)

    def _call(self, system: str, user: str) -> str:
        """发一次同步请求。

        走 `MultimodalClient` 的重试与错误归类 —— 那一层和「有没有图片」
        无关,复用它比再写一套退避靠谱。
        """
        import asyncio

        from app.llm.transport import (
            LLMRequest,
            MultimodalClient,
            extract_chat_text,
            extract_responses_text,
            normalize_endpoint,
        )

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # `api_style` 原来是收下就扔:无论配 responses 还是 chat_completions,
        # 这里都固定发 /chat/completions 并用 extract_chat_text 解。配成
        # responses 的自建端点上,那个请求会 404,而 404 在传输层被归类成
        # 「厂商不可达」—— 排查方向从第一步就是错的。
        #
        # 分支形状与 `evaluators/vision.py` 的 `build_adapter()` 一致,
        # 两处对同一个配置值的理解必须一样。
        if self.api_style == "responses":
            body: dict[str, Any] = {
                "model": self.model,
                "input": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "text": {"format": {"type": "json_object"}},
            }
            suffix = "/responses"
            extract = extract_responses_text
        else:
            body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            }
            suffix = "/chat/completions"
            extract = extract_chat_text

        request = LLMRequest(
            url=normalize_endpoint(self.base_url, suffix),
            headers=headers,
            body=body,
        )
        client = MultimodalClient(self, name="copy-llm", http_client=self._client)
        attempts = 0
        started = time.monotonic()

        def on_attempt(number: int) -> None:
            nonlocal attempts
            attempts = number

        try:
            payload, status = asyncio.run(client.send(request, on_attempt=on_attempt))
        except Exception:
            self._last_trace = {
                "model": self.model,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "provider_attempts": attempts,
            }
            raise

        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        self._last_trace = {
            "model": str(payload.get("model") or self.model),
            "response_id": str(payload.get("id") or "") or None,
            "http_status": status,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "provider_attempts": attempts,
            "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
            "completion_tokens": usage.get(
                "completion_tokens", usage.get("output_tokens")
            ),
            "total_tokens": usage.get("total_tokens"),
        }
        return extract(payload)


# ==========================================================
# 注册表
# ==========================================================

GENERATOR_FACTORIES = {
    TEMPLATE_GENERATOR: lambda: TemplateCopyGenerator(),
    LLM_GENERATOR: lambda: _llm_from_settings(),
}


def _llm_from_settings() -> LLMCopyGenerator:
    """从**运行期**配置造一个 LLM 文案生成器。

    A45-#31 同类:这四个键都在设置页里可改,所以必须走 `provider_setting()`。
    直读 `settings` 的话,运营在设置页换了模型或换了 Key,**要重启后端才生效**
    —— 而那一页承诺的是"改完就生效"。
    """
    from app.core.config import settings
    from app.providers._config import provider_setting

    return LLMCopyGenerator(
        api_key=provider_setting("TEXT_MODEL_API_KEY", settings.TEXT_MODEL_API_KEY),
        base_url=provider_setting("TEXT_MODEL_BASE_URL", settings.TEXT_MODEL_BASE_URL),
        model=provider_setting("TEXT_MODEL_NAME", settings.TEXT_MODEL_NAME),
        api_style=provider_setting("TEXT_MODEL_API_STYLE", settings.TEXT_MODEL_API_STYLE),
    )


def get_generator(name: str | None = None) -> CopyGenerator:
    """按名字取生成器。**未配置的不给,也不静默降级。**

    读 `COPY_GENERATOR` 这个**真字段**(经 `provider_setting`,见下),不再用
    `getattr(settings, "COPY_GENERATOR", TEMPLATE_GENERATOR)`:那个 `getattr`
    的兜底把「字段没声明」和「用户选了 template」变成了同一件事 ——
    而当时字段确实没声明,于是无论设置页里选什么,这里恒定返回模板生成器。
    配好了 Qwen 的密钥和模型名,工作台照样出本地模板文案,没有任何地方会说一句。

    现在字段拼错在启动期就被 `Settings._known_generator` 拦住,
    这里的 `ValidationError` 分支只处理显式传进来的 `name`。
    """
    from app.core.config import settings
    from app.providers._config import provider_setting

    # A45-#31 同类:生成器的选择在设置页里可改,读覆盖层而不是启动期快照
    chosen = (name or provider_setting("COPY_GENERATOR", settings.COPY_GENERATOR)).strip()
    factory = GENERATOR_FACTORIES.get(chosen)
    if factory is None:
        raise ValidationError(
            f"未知的文案生成器 {chosen!r};可选:{', '.join(sorted(GENERATOR_FACTORIES))}",
            code=ErrorCode.CONFIG_INVALID,
        )
    generator = factory()
    if not generator.is_configured():
        raise ValidationError(
            f"文案生成器 {chosen!r} 尚未配置完成,拒绝使用",
            code=ErrorCode.CONFIG_INVALID,
            http_status=503,
        )
    return generator
