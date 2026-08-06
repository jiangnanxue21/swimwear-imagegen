"""总分计算与评分结果解析(需求第十章)。

两条硬规矩:

1. **总分由后端按权重算**,不采信大模型直接给出的总分。
   模型自报的那个数只留档,用来观察它的打分漂移。
2. **不可解析的自由文本一律拒收**。宁可让这一张候选图评分失败、走重生,
   也不能把一段没法追责的散文当成评分结果写进库。

只依赖标准库,可在没有任何三方包的环境里跑测试。
"""
from __future__ import annotations

import json
from typing import Any

from app.core.enums import EvaluationDepth, HardFailCode, ProblemSeverity
from app.core.errors import ErrorCode, ValidationError
from app.evaluators import fact_consistency as fc
from app.evaluators.base import (
    SCORE_DIMENSIONS,
    EvaluationProblemItem,
    EvaluationResult,
)

#: 默认权重。合计为 1.0,商品还原度(身份 + 结构 + 颜色 + 图案)占 53%,
#: 因为这是电商图的生死线:模特好看但衣服不对,这张图毫无价值。
DEFAULT_WEIGHTS: dict[str, float] = {
    "garment_identity": 0.20,
    "structural_fidelity": 0.15,
    "color_fidelity": 0.10,
    "pattern_fidelity": 0.08,
    "anatomy_realism": 0.12,
    "face_realism": 0.06,
    "skin_realism": 0.05,
    "lighting_realism": 0.05,
    "background_realism": 0.04,
    "composition": 0.05,
    "website_usability": 0.10,
}


class EvaluationParseError(ValidationError):
    """评分器返回了无法解析的内容。

    ``vision_meta`` / ``duration_ms`` 带的是这次调用的审计元数据。

    为什么要挂在异常上:HTTP 请求其实**成功了** —— 模型名、响应 ID、token 用量、
    finish reason、耗时全都拿到了,只是响应体解析不了。这些字段恰恰是事后
    找厂商查一次异常调用的全部依据,而它们只在这一刻存在。以前解析异常
    一抛,捕获处只剩一句错误说明,`evaluation_attempts` 里那条失败记录的
    model_name、response_id、tokens 全是空的 —— 一张专门为了留痕而建的表,
    在最需要留痕的场景里什么都没留下。

    元数据由评分器写进 ``_vision_meta`` 命名空间,那个键只有后端自己写,
    不含请求体也不含图片,可以安全落库。
    """

    def __init__(
        self,
        message: str,
        *,
        detail: dict | None = None,
        vision_meta: dict | None = None,
        duration_ms: int | None = None,
    ) -> None:
        super().__init__(
            f"评分结果无法解析:{message}",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
            detail=detail or {},
        )
        self.vision_meta = vision_meta if isinstance(vision_meta, dict) else None
        self.duration_ms = duration_ms


def normalize_weights(weights: dict[str, Any] | None) -> dict[str, float]:
    """清洗权重表。未知维度丢弃,负数与非数字丢弃,全空则退回默认。"""
    if not weights:
        return dict(DEFAULT_WEIGHTS)
    cleaned: dict[str, float] = {}
    for key, value in weights.items():
        if key not in SCORE_DIMENSIONS:
            continue
        try:
            weight = float(value)
        except (TypeError, ValueError):
            continue
        if weight > 0:
            cleaned[key] = weight
    return cleaned or dict(DEFAULT_WEIGHTS)


def compute_overall(scores: dict[str, float], weights: dict[str, float] | None = None) -> float:
    """按权重算总分,只在**实际存在的维度**上归一化。

    快速检查(需求第十三章)只产出部分维度,如果拿缺失维度当 0 分,
    快速检查出来的候选图会被系统性判死。因此这里按出现的维度重新归一化。
    """
    table = normalize_weights(weights)
    present = {k: v for k, v in scores.items() if k in table}
    if not present:
        return 0.0
    total_weight = sum(table[k] for k in present)
    if total_weight <= 0:
        return 0.0
    weighted = sum(float(present[k]) * table[k] for k in present)
    return round(weighted / total_weight, 2)


def _coerce_payload(payload: Any) -> dict[str, Any]:
    """把评分器返回值收敛成 dict。

    允许 JSON 字符串(视觉模型常见的返回形态),但绝不接受散文。
    """
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        text = payload.strip()
        # 容忍 ```json 包裹,这是模型最常见的画蛇添足
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise EvaluationParseError(
                "返回内容不是合法 JSON", detail={"reason": str(exc)}
            ) from exc
        if not isinstance(decoded, dict):
            raise EvaluationParseError("JSON 顶层必须是对象")
        return decoded
    raise EvaluationParseError(f"不支持的返回类型 {type(payload).__name__}")


def _parse_scores(raw: Any, depth: EvaluationDepth) -> dict[str, float]:
    """解析 scores,并要求维度集合**严格等于**这次深度要求的那一组。

    以前这里只要求「至少有一个认识的维度」,加上 `compute_overall()` 只在
    实际返回的维度上归一化,两者叠起来是一个安全漏洞而不只是宽松:
    一次 FULL 评分只返回 ``{"garment_identity": 100}``,总分就是 100,
    直接 A 档自动通过、自动出图、自动上网站 —— 而模型其实只看了一个维度。

    宽松在这里没有任何收益。维度集合是我们在 Schema 和提示词里**同时**声明的
    硬要求,模型少给一个就是它没照做,那次输出不可信,应该重试或转人工,
    而不是拿一个基于残缺数据的分数继续往下走。

    所以缺失、多余、未知一律解析失败。QUICK 只要 4 个维度,FULL 要全部 11 个,
    由 `dimensions_for(depth)` 说了算。
    """
    if not isinstance(raw, dict) or not raw:
        raise EvaluationParseError("缺少 scores 字段或为空")

    from app.evaluators.vision_schema import dimensions_for

    required = set(dimensions_for(depth))

    scores: dict[str, float] = {}
    unknown: list[str] = []
    for key, value in raw.items():
        if key not in SCORE_DIMENSIONS:
            unknown.append(str(key))
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvaluationParseError(f"维度 {key} 的分值不是数字")
        number = float(value)
        if not 0 <= number <= 100:
            raise EvaluationParseError(f"维度 {key} 的分值 {number} 超出 0-100")
        scores[key] = round(number, 2)

    if unknown:
        raise EvaluationParseError(
            f"返回了未知评分维度:{', '.join(sorted(unknown))}",
            detail={"unknown_dimensions": sorted(unknown), "depth": depth.value},
        )

    missing = sorted(required - set(scores))
    extra = sorted(set(scores) - required)
    if missing:
        raise EvaluationParseError(
            f"{depth.value} 评分缺少维度:{', '.join(missing)}",
            detail={"missing_dimensions": missing, "depth": depth.value},
        )
    if extra:
        # 多给的维度同样拒收:QUICK 只看得到四个维度的信息,却给出了第五个,
        # 说明模型在猜。猜出来的分会被算进总分,而它没有任何依据。
        raise EvaluationParseError(
            f"{depth.value} 评分返回了本次不该出现的维度:{', '.join(extra)}",
            detail={"unexpected_dimensions": extra, "depth": depth.value},
        )
    return scores


def _parse_problems(raw: Any) -> list[EvaluationProblemItem]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise EvaluationParseError("problems 必须是数组")

    problems: list[EvaluationProblemItem] = []
    for entry in raw:
        if isinstance(entry, str):
            # 只给了代码字符串:按硬错误表判定严重度
            code = entry.strip()
            is_hard = code in HardFailCode.__members__
            problems.append(
                EvaluationProblemItem(
                    code=code,
                    severity=ProblemSeverity.CRITICAL if is_hard else ProblemSeverity.MINOR,
                    message=code,
                    hard_fail=is_hard,
                )
            )
            continue
        if not isinstance(entry, dict):
            raise EvaluationParseError("problems 的元素必须是对象或字符串")

        code = str(entry.get("code") or "").strip()
        if not code:
            raise EvaluationParseError("problem 缺少 code")
        is_hard = bool(entry.get("hard_fail", code in HardFailCode.__members__))
        raw_severity = str(entry.get("severity") or "").upper()
        if raw_severity in ProblemSeverity.__members__:
            severity = ProblemSeverity(raw_severity)
        else:
            severity = ProblemSeverity.CRITICAL if is_hard else ProblemSeverity.MINOR
        if is_hard:
            # 硬错误的严重度不容降级,否则分档上限会被绕过
            severity = ProblemSeverity.CRITICAL

        dimension = entry.get("dimension")
        problems.append(
            EvaluationProblemItem(
                code=code,
                severity=severity,
                message=str(entry.get("message") or code)[:500],
                dimension=str(dimension) if dimension else None,
                hard_fail=is_hard,
            )
        )
    return problems


def parse_evaluator_payload(
    payload: Any,
    *,
    evaluator: str,
    depth: EvaluationDepth = EvaluationDepth.FULL,
    weights: dict[str, Any] | None = None,
    model_name: str | None = None,
    confirmed_facts: dict[str, Any] | None = None,
    allowed_hard_fail_codes: list[str] | None = None,
) -> EvaluationResult:
    """把评分器原始返回解析成结构化结果。解析不了就抛错。

    `confirmed_facts` 是该商品/该颜色**已确认**的属性值(§6.5)。
    传空等于"这次不做事实一致性检查" —— 每一项都会带着"事实未确认"
    进 `uncertain_items`,而**不会**产生任何硬错误。这一条不能写反,
    理由在 `fact_consistency.py` 模块文档第三条:默认判不一致会让整批图
    在事实确认之前全部被否,而运营看不出是自己少确认了一个字段。
    """
    data = _coerce_payload(payload)
    scores = _parse_scores(data.get("scores"), depth)
    problems = _parse_problems(data.get("problems"))

    # §6.5 事实一致性维度组。**在硬错误合并之前算**,这样它产出的码
    # 走的是和评分器自报的码完全同一条路(去重、白名单、并进 hard_fail)——
    # 另开一条"事实不一致也算 hard_fail"的赋值,就是第二个否决点。
    findings = fc.evaluate(
        data.get(fc.PAYLOAD_KEY),
        confirmed_facts=confirmed_facts,
        allowed_codes=allowed_hard_fail_codes,
    )
    problems = [*problems, *_parse_problems(fc.problem_dicts(findings))]

    # 硬错误代码有两个来源:显式的 hard_fail_codes,以及标了 hard_fail 的 problem。
    # 取并集,并且**只承认需求第十一章列出的代码** —— 否则评分器可以自造代码
    # 让任意问题冒充硬错误,分档规则就没有确定性可言了。
    declared = data.get("hard_fail_codes") or []
    if not isinstance(declared, list):
        raise EvaluationParseError("hard_fail_codes 必须是数组")

    codes: list[str] = []
    unrecognized: list[str] = []
    for code in [*(str(c) for c in declared), *(p.code for p in problems if p.hard_fail)]:
        if code in HardFailCode.__members__:
            if code not in codes:
                codes.append(code)
        elif code not in unrecognized:
            unrecognized.append(code)

    uncertain = data.get("uncertain_items") or []
    if not isinstance(uncertain, list):
        raise EvaluationParseError("uncertain_items 必须是数组")
    uncertain_items = [str(u)[:200] for u in uncertain]
    # 认不出来的硬错误代码是评分器与后端不同步的信号,必须留痕。
    # (未知**维度**不在这里 —— 它已经在 _parse_scores 里被判成解析失败了)
    uncertain_items += [f"未知硬错误代码:{c}" for c in unrecognized]
    uncertain_items += fc.uncertain_notes(findings)

    reported = data.get("overall_score")
    model_reported: float | None = None
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        model_reported = round(float(reported), 2)

    # 评分器声明了 hard_fail 却给不出任何可识别的代码,是一个自相矛盾的结果。
    # 这里选择**相信那个 True**:误判成硬错误只是多生成一轮,漏判则是把废图
    # 直接送上网站。两种错误的代价不对称,就该往保守的一侧倒。
    declared_hard_fail = bool(data.get("hard_fail"))
    if declared_hard_fail and not codes:
        uncertain_items.append("评分器声明 hard_fail 但未给出可识别的硬错误代码")

    return EvaluationResult(
        scores=scores,
        overall_score=compute_overall(scores, weights),
        hard_fail=bool(codes) or declared_hard_fail,
        hard_fail_codes=codes,
        problems=problems,
        uncertain_items=uncertain_items,
        depth=depth,
        evaluator=evaluator,
        # model_name 只接受调用方传进来的值,**绝不从 data 里读**:
        # 那是评分记录里唯一用来回答「三个月前这批图是谁打的分」的字段,
        # 让被审计对象自己填等于没有审计。由 ImageQualityEvaluator.resolved_model_name()
        # 从 HTTP 响应的 model 字段取。
        model_name=(str(model_name)[:120] if model_name else None),
        model_reported_overall=model_reported,
        fact_findings=findings,
        raw=data if isinstance(payload, dict) else {"parsed": data},
    )
