"""总分计算与评分结果解析(需求第十章)。"""
from __future__ import annotations

from app.core.enums import EvaluationDepth, ProblemSeverity
from app.evaluators.base import SCORE_DIMENSIONS
from app.evaluators.scoring import (
    DEFAULT_WEIGHTS,
    EvaluationParseError,
    compute_overall,
    normalize_weights,
    parse_evaluator_payload,
)
from tests.pure._helpers import expect_raises  # noqa: F401  (供其它用例复用)


def _full_scores(value: float = 80.0) -> dict[str, float]:
    return {name: value for name in SCORE_DIMENSIONS}


def _payload(**overrides) -> dict:
    base = {
        "hard_fail": False,
        "hard_fail_codes": [],
        "scores": _full_scores(),
        "uncertain_items": [],
        "problems": [],
        "overall_score": 80,
    }
    base.update(overrides)
    return base


# ---------- 权重 ----------

def test_default_weights_cover_every_dimension():
    assert set(DEFAULT_WEIGHTS) == set(SCORE_DIMENSIONS)


def test_default_weights_sum_to_one():
    assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9


def test_product_fidelity_dominates_the_weighting():
    """商品还原度必须占大头 —— 模特好看但衣服不对的图毫无价值。"""
    fidelity = sum(
        DEFAULT_WEIGHTS[d]
        for d in ("garment_identity", "structural_fidelity", "color_fidelity", "pattern_fidelity")
    )
    assert fidelity > 0.5


def test_normalize_weights_drops_unknown_and_bad_values():
    cleaned = normalize_weights(
        {"garment_identity": 0.5, "not_a_dimension": 0.5, "composition": "abc", "face_realism": -1}
    )
    assert cleaned == {"garment_identity": 0.5}


def test_normalize_weights_falls_back_to_default_when_everything_is_junk():
    assert normalize_weights({"nope": 1}) == DEFAULT_WEIGHTS


# ---------- 总分 ----------

def test_overall_is_weighted_not_arithmetic_mean():
    scores = _full_scores(50.0)
    scores["garment_identity"] = 100.0  # 权重最高的维度
    weighted = compute_overall(scores)
    mean = sum(scores.values()) / len(scores)
    assert weighted > mean


def test_overall_renormalizes_over_present_dimensions_only():
    """快速检查只产出部分维度,缺的维度不能当 0 分,否则会被系统性判死。"""
    partial = {"garment_identity": 90.0, "structural_fidelity": 90.0}
    assert compute_overall(partial) == 90.0


def test_overall_of_empty_scores_is_zero():
    assert compute_overall({}) == 0.0


# ---------- 后端总分优先于模型自报总分 ----------

def test_backend_recomputes_overall_and_ignores_model_claim():
    """需求第十章:总分由后端依据配置权重计算,不要完全相信大模型给出的总分。"""
    result = parse_evaluator_payload(
        _payload(scores=_full_scores(40.0), overall_score=99),
        evaluator="test",
    )
    assert result.overall_score < 50, "后端必须用自己算的分"
    assert result.model_reported_overall == 99.0, "模型自报的分要留档,用于观察漂移"


def test_model_claim_is_kept_even_when_it_matches():
    result = parse_evaluator_payload(_payload(scores=_full_scores(80.0)), evaluator="test")
    assert result.model_reported_overall == 80.0
    assert result.overall_score == 80.0


# ---------- 拒收不可解析内容 ----------

def test_free_text_is_rejected():
    """需求第十章:评分结果必须是结构化数据,不接受不可解析的自由文本。"""
    expect_raises(
        EvaluationParseError,
        parse_evaluator_payload,
        "这张图整体看起来不错,衣服还原度挺高的。",
        evaluator="test",
    )


def test_json_string_is_accepted():
    import json

    result = parse_evaluator_payload(json.dumps(_payload()), evaluator="test")
    assert result.overall_score == 80.0


def test_markdown_fenced_json_is_accepted():
    """模型爱加 ```json 包裹,这属于可以容忍的画蛇添足。"""
    import json

    text = "```json\n" + json.dumps(_payload()) + "\n```"
    assert parse_evaluator_payload(text, evaluator="test").overall_score == 80.0


def test_missing_scores_is_rejected():
    expect_raises(
        EvaluationParseError, parse_evaluator_payload, {"hard_fail": False}, evaluator="test"
    )


def test_non_numeric_score_is_rejected():
    expect_raises(
        EvaluationParseError,
        parse_evaluator_payload,
        _payload(scores={"garment_identity": "很好"}),
        evaluator="test",
    )


def test_out_of_range_score_is_rejected():
    expect_raises(
        EvaluationParseError,
        parse_evaluator_payload,
        _payload(scores={"garment_identity": 145}),
        evaluator="test",
    )


def test_boolean_score_is_rejected():
    """True 在 Python 里是 int 的子类,不能让它混进分数。"""
    expect_raises(
        EvaluationParseError,
        parse_evaluator_payload,
        _payload(scores={"garment_identity": True}),
        evaluator="test",
    )


def test_unknown_dimension_is_rejected():
    """未知维度不再被静默丢弃。

    以前它只进 uncertain_items,评分照常产出。但\"模型输出了一个我们不认识的维度\"
    和\"模型少输出了一个维度\"是同一件事的两面:它没按 Schema 走,这次输出不可信。
    丢掉未知维度、拿剩下的算分,等于替一次不合格的输出打圆场。
    """
    exc = expect_raises(
        EvaluationParseError,
        parse_evaluator_payload,
        _payload(scores={**_full_scores(), "vibes": 100}),
        evaluator="test",
    )
    assert "vibes" in str(exc)


def test_scores_with_only_unknown_dimensions_is_rejected():
    expect_raises(
        EvaluationParseError, parse_evaluator_payload, _payload(scores={"vibes": 100}),
        evaluator="test",
    )


# ---------- 硬错误代码 ----------

def test_only_documented_hard_fail_codes_are_honoured():
    """评分器不能自造代码让普通问题冒充硬错误,否则分档规则没有确定性可言。"""
    result = parse_evaluator_payload(
        _payload(hard_fail=True, hard_fail_codes=["STRAP_CHANGED", "I_JUST_DONT_LIKE_IT"]),
        evaluator="test",
    )
    assert result.hard_fail_codes == ["STRAP_CHANGED"]
    assert any("I_JUST_DONT_LIKE_IT" in item for item in result.uncertain_items)


def test_hard_fail_declared_without_codes_is_still_treated_as_hard_fail():
    """自相矛盾的结果往保守一侧倒:误判多生成一轮,漏判则把废图送上网站。"""
    result = parse_evaluator_payload(
        _payload(hard_fail=True, hard_fail_codes=[]), evaluator="test"
    )
    assert result.hard_fail is True
    assert result.uncertain_items


def test_problem_marked_hard_fail_contributes_its_code():
    result = parse_evaluator_payload(
        _payload(problems=[{"code": "PATTERN_DISTORTED", "hard_fail": True, "message": "x"}]),
        evaluator="test",
    )
    assert result.hard_fail is True
    assert "PATTERN_DISTORTED" in result.hard_fail_codes


def test_hard_fail_problem_severity_cannot_be_downgraded():
    """把硬错误标成 MINOR 就能绕过分档上限 —— 这条路必须堵死。"""
    result = parse_evaluator_payload(
        _payload(problems=[{"code": "FACE_HARD_ERROR", "severity": "MINOR", "message": "x"}]),
        evaluator="test",
    )
    assert result.problems[0].severity is ProblemSeverity.CRITICAL


def test_bare_string_problem_is_classified_by_the_hard_fail_table():
    result = parse_evaluator_payload(
        _payload(problems=["GARMENT_WRONG", "LIGHTING_FLAT"]), evaluator="test"
    )
    by_code = {p.code: p for p in result.problems}
    assert by_code["GARMENT_WRONG"].hard_fail is True
    assert by_code["LIGHTING_FLAT"].hard_fail is False


def test_problems_must_be_a_list():
    expect_raises(
        EvaluationParseError, parse_evaluator_payload, _payload(problems={"code": "x"}),
        evaluator="test",
    )


def test_problem_without_code_is_rejected():
    expect_raises(
        EvaluationParseError, parse_evaluator_payload, _payload(problems=[{"message": "坏了"}]),
        evaluator="test",
    )


def test_depth_is_carried_through():
    from app.evaluators.vision_schema import dimensions_for

    quick = {name: 80 for name in dimensions_for(EvaluationDepth.QUICK)}
    result = parse_evaluator_payload(
        _payload(scores=quick), evaluator="test", depth=EvaluationDepth.QUICK
    )
    assert result.depth is EvaluationDepth.QUICK


def test_full_depth_rejects_a_partial_dimension_set():
    """这是本次修复的核心:FULL 只回一个维度不能得满分。

    旧行为里 `_parse_scores` 只要求\"至少有一个认识的维度\",而 `compute_overall`
    只在出现的维度上归一化 —— 两者叠起来,``{"garment_identity": 100}`` 的总分
    就是 100,直接 A 档自动通过、自动上网站,而模型其实只看了一个维度。
    """
    exc = expect_raises(
        EvaluationParseError,
        parse_evaluator_payload,
        _payload(scores={"garment_identity": 100}),
        evaluator="test",
        depth=EvaluationDepth.FULL,
    )
    assert "缺少维度" in str(exc)


def test_quick_depth_rejects_dimensions_it_could_not_have_judged():
    """QUICK 只看得到四个维度的信息,多给的那些是猜的,不能进总分。"""
    exc = expect_raises(
        EvaluationParseError,
        parse_evaluator_payload,
        _payload(scores=_full_scores()),
        evaluator="test",
        depth=EvaluationDepth.QUICK,
    )
    assert "不该出现的维度" in str(exc)


def test_custom_weights_change_the_total():
    payload = _payload(
        scores={**_full_scores(), "garment_identity": 100.0, "face_realism": 0.0}
    )
    heavy_identity = parse_evaluator_payload(
        payload, evaluator="t", weights={"garment_identity": 0.9, "face_realism": 0.1}
    )
    heavy_face = parse_evaluator_payload(
        payload, evaluator="t", weights={"garment_identity": 0.1, "face_realism": 0.9}
    )
    assert heavy_identity.overall_score > heavy_face.overall_score
