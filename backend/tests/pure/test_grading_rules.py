"""A/B/C/D 分档与硬错误优先级(需求第十一、十二、十八章)。

需求第十八章点名的四个关键用例中,有三个落在本文件:
    test_hard_fail_candidate_never_auto_approves
    test_high_score_requires_product_fidelity_thresholds
    (另两个在 test_round_decision.py)
"""
from __future__ import annotations

from app.core.enums import (
    Grade,
    HardFailCode,
    ProblemSeverity,
    RecommendedAction,
    worst_of,
)
from app.evaluators.base import SCORE_DIMENSIONS, EvaluationProblemItem, EvaluationResult
from app.evaluators.rules import (
    DEFAULT_THRESHOLDS,
    GradingThresholds,
    grade_candidate,
    quick_check_result_is_conclusive,
)
from app.evaluators.scoring import compute_overall


def _result(
    value: float = 92.0,
    *,
    overrides: dict[str, float] | None = None,
    hard_fail_codes: list[str] | None = None,
    problems: list[EvaluationProblemItem] | None = None,
) -> EvaluationResult:
    scores = {name: value for name in SCORE_DIMENSIONS}
    scores.update(overrides or {})
    return EvaluationResult(
        scores=scores,
        overall_score=compute_overall(scores),
        hard_fail=bool(hard_fail_codes),
        hard_fail_codes=list(hard_fail_codes or []),
        problems=list(problems or []),
    )


def _problem(code: str, severity: ProblemSeverity) -> EvaluationProblemItem:
    return EvaluationProblemItem(code=code, severity=severity, message=code)


# ---------- Grade 顺序 ----------

def test_grade_rank_is_a_best_to_d_worst():
    assert Grade.A.rank < Grade.B.rank < Grade.C.rank < Grade.D.rank


def test_worst_of_picks_the_lower_grade():
    assert worst_of(Grade.A, Grade.C) is Grade.C
    assert worst_of(Grade.B, Grade.B) is Grade.B
    assert worst_of(Grade.A, Grade.B, Grade.D) is Grade.D


# ---------- A 档 ----------

def test_clean_high_score_is_auto_approved():
    decision = grade_candidate(_result(93.0))
    assert decision.grade is Grade.A
    assert decision.action is RecommendedAction.AUTO_APPROVE
    assert decision.is_auto_approvable


def test_high_score_requires_product_fidelity_thresholds():
    """需求第十八章关键用例。

    A 档除了总分 ≥85,还要过商品身份、结构、人体、可用性四道门槛。
    这里让总分很高、但商品身份一致性掉到 88(<90),必须判不到 A ——
    加权平均会把"衣服不对"这个致命项摊薄,门槛就是用来防这个的。
    """
    result = _result(96.0, overrides={"garment_identity": 88.0})
    assert result.overall_score >= DEFAULT_THRESHOLDS.a_min_overall, "前提:总分仍然够 A"

    decision = grade_candidate(result)
    assert decision.grade is not Grade.A
    assert "garment_identity" in decision.failed_gates
    assert decision.action is RecommendedAction.REGENERATE_TUNED


def test_each_of_the_four_gates_can_block_grade_a():
    for dimension in (
        "garment_identity",
        "structural_fidelity",
        "anatomy_realism",
        "website_usability",
    ):
        decision = grade_candidate(_result(96.0, overrides={dimension: 60.0}))
        assert decision.grade is not Grade.A, f"{dimension} 掉到 60 仍判 A 是错的"


def test_gate_failure_downgrades_to_b_not_to_d():
    """总分够但还原度不够 -> 定向重生,不是直接淘汰。"""
    decision = grade_candidate(_result(96.0, overrides={"structural_fidelity": 85.0}))
    assert decision.grade is Grade.B


def test_missing_gate_dimension_blocks_grade_a():
    """缺维度 = 没有判 A 的依据,保守降档,不是当它满分。"""
    scores = {"garment_identity": 95.0, "color_fidelity": 95.0}
    result = EvaluationResult(scores=scores, overall_score=compute_overall(scores))
    decision = grade_candidate(result)
    assert decision.grade is not Grade.A


# ---------- 硬错误优先级 ----------

def test_hard_fail_candidate_never_auto_approves():
    """需求第十八章关键用例:硬错误出现时,不论总分多高都不能自动通过。"""
    for code in HardFailCode:
        result = _result(99.0, hard_fail_codes=[code.value])
        decision = grade_candidate(result)
        assert decision.grade is Grade.D, f"{code.value} 下仍判 {decision.grade}"
        assert decision.action is RecommendedAction.REJECT
        assert not decision.is_auto_approvable


def test_hard_fail_beats_a_perfect_score():
    result = _result(100.0, hard_fail_codes=[HardFailCode.STRAP_CHANGED.value])
    assert result.overall_score == 100.0
    assert grade_candidate(result).grade is Grade.D


def test_hard_fail_reason_names_the_codes():
    decision = grade_candidate(
        _result(90.0, hard_fail_codes=[HardFailCode.COLOR_VARIANT_WRONG.value])
    )
    assert any("COLOR_VARIANT_WRONG" in reason for reason in decision.reasons)


# ---------- 分数区间 ----------

def test_score_bands_match_the_spec():
    cases = [
        (90.0, Grade.A),
        (84.0, Grade.B),
        (70.0, Grade.B),
        (69.0, Grade.C),
        (55.0, Grade.C),
        (54.0, Grade.D),
        (10.0, Grade.D),
    ]
    for value, expected in cases:
        assert grade_candidate(_result(value)).grade is expected, f"{value} 应判 {expected}"


def test_band_boundaries_are_inclusive_at_the_bottom():
    """只测分数区间的边界,把四道门槛放开,避免两条规则纠缠在一个用例里。"""
    band_only = GradingThresholds(
        a_min_garment_identity=0,
        a_min_structural_fidelity=0,
        a_min_anatomy_realism=0,
        a_min_website_usability=0,
    )
    assert grade_candidate(_result(85.0), band_only).grade is Grade.A
    assert grade_candidate(_result(84.99), band_only).grade is not Grade.A
    assert grade_candidate(_result(70.0), band_only).grade is Grade.B
    assert grade_candidate(_result(69.99), band_only).grade is Grade.C
    assert grade_candidate(_result(55.0), band_only).grade is Grade.C
    assert grade_candidate(_result(54.99), band_only).grade is Grade.D


# ---------- 问题严重度上限 ----------

def test_minor_problem_caps_at_b():
    """需求第十二章 B 档的"或存在可修复的轻度问题"。"""
    decision = grade_candidate(
        _result(95.0, problems=[_problem("LIGHTING_FLAT", ProblemSeverity.MINOR)])
    )
    assert decision.grade is Grade.B


def test_major_problem_caps_at_c():
    """需求第十二章 C 档的"或存在明显但有机会通过重新生成解决的问题"。"""
    decision = grade_candidate(
        _result(95.0, problems=[_problem("SKIN_PLASTIC", ProblemSeverity.MAJOR)])
    )
    assert decision.grade is Grade.C
    assert decision.action is RecommendedAction.REGENERATE_RESEED


def test_critical_problem_caps_at_d():
    decision = grade_candidate(
        _result(95.0, problems=[_problem("ANYTHING", ProblemSeverity.CRITICAL)])
    )
    assert decision.grade is Grade.D


def test_cap_never_upgrades_a_low_score():
    """上限只能往下压,不能把 D 分的图抬成 B。"""
    decision = grade_candidate(
        _result(30.0, problems=[_problem("LIGHTING_FLAT", ProblemSeverity.MINOR)])
    )
    assert decision.grade is Grade.D


def test_worst_severity_wins_among_many_problems():
    decision = grade_candidate(
        _result(
            95.0,
            problems=[
                _problem("LIGHTING_FLAT", ProblemSeverity.MINOR),
                _problem("SKIN_PLASTIC", ProblemSeverity.MAJOR),
                _problem("COMPOSITION_OFF", ProblemSeverity.MINOR),
            ],
        )
    )
    assert decision.grade is Grade.C


# ---------- 阈值可覆盖 ----------

def test_thresholds_can_be_relaxed_via_rule_set():
    """需求第十二章:允许通过数据库中的 RuleSet 调整。"""
    relaxed = GradingThresholds.from_dict(
        {"a_min_overall": 70, "a_min_garment_identity": 70, "a_min_structural_fidelity": 70,
         "a_min_anatomy_realism": 70, "a_min_website_usability": 70}
    )
    result = _result(75.0)
    assert grade_candidate(result).grade is Grade.B  # 默认阈值下
    assert grade_candidate(result, relaxed).grade is Grade.A  # 放宽后


def test_thresholds_can_be_tightened():
    strict = GradingThresholds.from_dict({"a_min_overall": 97})
    assert grade_candidate(_result(93.0), strict).grade is Grade.B


def test_from_dict_ignores_junk_and_keeps_defaults():
    parsed = GradingThresholds.from_dict(
        {"a_min_overall": "很高", "b_min_overall": None, "unknown_key": 1}
    )
    assert parsed.a_min_overall == DEFAULT_THRESHOLDS.a_min_overall
    assert parsed.b_min_overall == DEFAULT_THRESHOLDS.b_min_overall


def test_from_dict_clamps_spot_check_ratio():
    assert GradingThresholds.from_dict({"spot_check_ratio": 5}).spot_check_ratio == 1.0
    assert GradingThresholds.from_dict({"spot_check_ratio": -1}).spot_check_ratio == 0.0


def test_from_dict_round_trips_through_to_dict():
    original = GradingThresholds(a_min_overall=88.0, spot_check_ratio=0.25, prescreen_enabled=False)
    restored = GradingThresholds.from_dict(original.to_dict())
    assert restored.a_min_overall == 88.0
    assert restored.spot_check_ratio == 0.25
    assert restored.prescreen_enabled is False


# ---------- 分档是纯函数 ----------

def test_grading_is_deterministic():
    result = _result(78.0, problems=[_problem("LIGHTING_FLAT", ProblemSeverity.MINOR)])
    first = grade_candidate(result)
    for _ in range(20):
        assert grade_candidate(result) == first


def test_every_grade_maps_to_an_action():
    seen = set()
    for value in (95.0, 80.0, 60.0, 20.0):
        decision = grade_candidate(_result(value))
        seen.add(decision.grade)
        assert decision.action is not None
    assert seen == {Grade.A, Grade.B, Grade.C, Grade.D}


# ---------- 快速检查的结论边界 ----------

def test_quick_check_without_hard_fail_is_not_conclusive():
    """快速检查没查出硬错误,不代表这张图好 —— 不能凭它判 A。"""
    from app.core.enums import EvaluationDepth

    result = _result(95.0)
    result.depth = EvaluationDepth.QUICK
    assert quick_check_result_is_conclusive(result) is False


def test_quick_check_with_hard_fail_is_conclusive():
    from app.core.enums import EvaluationDepth

    result = _result(95.0, hard_fail_codes=[HardFailCode.IMAGE_CORRUPTED.value])
    result.depth = EvaluationDepth.QUICK
    assert quick_check_result_is_conclusive(result) is True
