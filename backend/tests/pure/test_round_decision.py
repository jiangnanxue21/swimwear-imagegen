"""轮次决策:自动重生、最大轮次、转人工的时机(需求第十二、十八章)。

需求第十八章点名的另两个关键用例在这里:
    test_low_score_candidate_is_regenerated_before_manual_review
    test_manual_review_only_after_attempts_are_exhausted
"""
from __future__ import annotations

from app.core.enums import (
    DEFAULT_MAX_ROUNDS,
    EvaluationDepth,
    Grade,
    HardFailCode,
    ReviewReason,
    TaskStatus,
)
from app.evaluators.decision import (
    CandidateVerdict,
    RoundOutcome,
    decide_round,
    stable_fraction,
    systematic_problem_codes,
)
from app.evaluators.rules import DEFAULT_THRESHOLDS, GradingThresholds

NO_SPOT_CHECK = GradingThresholds(spot_check_ratio=0.0)


def _verdict(key: str, grade: Grade, score: float, **kwargs) -> CandidateVerdict:
    return CandidateVerdict(key=key, grade=grade, overall_score=score, **kwargs)


def _round(verdicts, round_number=1, max_rounds=DEFAULT_MAX_ROUNDS, thresholds=NO_SPOT_CHECK, **kw):
    return decide_round(
        verdicts,
        round_number=round_number,
        max_rounds=max_rounds,
        thresholds=thresholds,
        task_key="task-fixed-key",
        **kw,
    )


# ---------- A 档自动通过 ----------

def test_best_a_grade_candidate_is_selected():
    decision = _round([
        _verdict("c1", Grade.A, 88.0),
        _verdict("c2", Grade.A, 94.0),
        _verdict("c3", Grade.B, 80.0),
    ])
    assert decision.outcome is RoundOutcome.AUTO_APPROVED
    assert decision.selected_key == "c2", "应选 A 档中总分最高的那张"
    assert set(decision.rejected_keys) == {"c1", "c3"}
    assert decision.next_status is TaskStatus.AUTO_APPROVED


def test_tie_break_is_stable():
    """同分时按 key 排序,保证重放可复现。"""
    verdicts = [_verdict("c2", Grade.A, 90.0), _verdict("c1", Grade.A, 90.0)]
    assert _round(verdicts).selected_key == "c1"
    assert _round(list(reversed(verdicts))).selected_key == "c1"


def test_quick_checked_candidate_cannot_auto_approve():
    """快速检查只查硬错误,查不出问题不等于图好 —— 省成本不能变成放水。"""
    decision = _round([_verdict("c1", Grade.A, 95.0, depth=EvaluationDepth.QUICK)])
    assert decision.outcome is RoundOutcome.REGENERATE


def test_hard_fail_candidate_is_never_selected():
    decision = _round([
        _verdict("c1", Grade.A, 99.0, hard_fail=True,
                 hard_fail_codes=(HardFailCode.STRAP_CHANGED.value,)),
        _verdict("c2", Grade.A, 86.0),
    ])
    assert decision.selected_key == "c2"


# ---------- 低分先重生,不进人工 ----------

def test_low_score_candidate_is_regenerated_before_manual_review():
    """需求第十八章关键用例。

    第 1 轮全是低分,但还有轮次 -> 淘汰候选并重生,**不能**进人工审核。
    """
    decision = _round(
        [_verdict("c1", Grade.C, 60.0), _verdict("c2", Grade.D, 40.0)],
        round_number=1,
        max_rounds=3,
    )
    assert decision.outcome is RoundOutcome.REGENERATE
    assert decision.next_status is TaskStatus.REGENERATING
    assert decision.review_reason is None, "还有轮次时不该产生审核项"
    assert decision.repair_plan is not None, "重生必须带修复策略(需求第九章)"
    assert set(decision.rejected_keys) == {"c1", "c2"}


def test_hard_fail_round_regenerates_instead_of_escalating():
    """需求第十一章:未耗尽轮次时,硬错误候选自动淘汰并触发重生,不立即交给人工。"""
    decision = _round(
        [
            _verdict("c1", Grade.D, 30.0, hard_fail=True,
                     hard_fail_codes=(HardFailCode.ANATOMY_HARD_ERROR.value,)),
            _verdict("c2", Grade.D, 25.0, hard_fail=True,
                     hard_fail_codes=(HardFailCode.FACE_HARD_ERROR.value,)),
        ],
        round_number=2,
        max_rounds=3,
    )
    assert decision.outcome is RoundOutcome.REGENERATE
    assert decision.repair_plan.change_model_template is True


def test_every_intermediate_round_regenerates():
    for round_number in range(1, DEFAULT_MAX_ROUNDS):
        decision = _round(
            [_verdict("c1", Grade.D, 20.0)],
            round_number=round_number,
            max_rounds=DEFAULT_MAX_ROUNDS,
        )
        assert decision.outcome is RoundOutcome.REGENERATE, f"第 {round_number} 轮不该转人工"


# ---------- 轮次耗尽才转人工 ----------

def test_manual_review_only_after_attempts_are_exhausted():
    """需求第十八章关键用例。"""
    verdicts = [_verdict("c1", Grade.D, 30.0), _verdict("c2", Grade.C, 58.0)]

    # 前两轮:重生
    for round_number in (1, 2):
        assert _round(verdicts, round_number=round_number, max_rounds=3).outcome \
            is RoundOutcome.REGENERATE

    # 第三轮(= max_rounds):才转人工
    final = _round(verdicts, round_number=3, max_rounds=3)
    assert final.outcome is RoundOutcome.MANUAL_REVIEW
    assert final.next_status is TaskStatus.MANUAL_REVIEW
    assert final.review_reason is ReviewReason.ROUNDS_EXHAUSTED


def test_exhausted_round_carries_no_repair_plan():
    """不再自动重生了,还给一份修复计划只会误导后续代码去执行它。"""
    decision = _round([_verdict("c1", Grade.D, 30.0)], round_number=3, max_rounds=3)
    assert decision.repair_plan is None


def test_provider_exhausted_is_reported_separately():
    decision = _round(
        [_verdict("c1", Grade.D, 30.0)], round_number=3, max_rounds=3, provider_exhausted=True
    )
    assert decision.review_reason is ReviewReason.PROVIDER_EXHAUSTED


def test_a_grade_on_the_last_round_still_auto_approves():
    """轮次用尽不等于失败 —— 最后一轮出了好图照样自动通过。"""
    decision = _round([_verdict("c1", Grade.A, 90.0)], round_number=3, max_rounds=3)
    assert decision.outcome is RoundOutcome.AUTO_APPROVED


def test_single_round_task_goes_straight_to_review_when_bad():
    decision = _round([_verdict("c1", Grade.D, 10.0)], round_number=1, max_rounds=1)
    assert decision.outcome is RoundOutcome.MANUAL_REVIEW


# ---------- 空轮次 ----------

def test_empty_round_regenerates_when_rounds_remain():
    decision = _round([], round_number=1, max_rounds=3)
    assert decision.outcome is RoundOutcome.REGENERATE


def test_empty_round_escalates_when_exhausted():
    decision = _round([], round_number=3, max_rounds=3)
    assert decision.outcome is RoundOutcome.MANUAL_REVIEW


# ---------- 系统性问题识别 ----------

def test_systematic_codes_need_to_appear_in_half_the_candidates():
    verdicts = [
        _verdict("c1", Grade.C, 60.0, problem_codes=("STRAP_CHANGED",)),
        _verdict("c2", Grade.C, 61.0, problem_codes=("STRAP_CHANGED",)),
        _verdict("c3", Grade.C, 62.0, problem_codes=("LIGHTING_FLAT",)),
        _verdict("c4", Grade.C, 63.0, problem_codes=()),
    ]
    codes = systematic_problem_codes(verdicts)
    assert "STRAP_CHANGED" in codes, "四张里两张都错,是系统性偏差"
    assert "LIGHTING_FLAT" not in codes, "只出现一次的不算系统性"


def test_repair_plan_covers_systematic_and_best_candidate_problems():
    """只看最好那张会漏掉规律性缺陷;只看系统性会漏掉最接近过线那张的短板。"""
    decision = _round(
        [
            _verdict("c1", Grade.B, 80.0, problem_codes=("COMPOSITION_OFF",)),
            _verdict("c2", Grade.C, 60.0, problem_codes=("NECKLINE_CHANGED",)),
            _verdict("c3", Grade.C, 61.0, problem_codes=("NECKLINE_CHANGED",)),
        ],
        round_number=1,
        max_rounds=3,
    )
    triggered = set(decision.repair_plan.triggered_by)
    assert "NECKLINE_CHANGED" in triggered, "系统性问题必须进修复计划"
    assert "COMPOSITION_OFF" in triggered, "最好那张的问题也必须修"


def test_systematic_codes_of_empty_input_is_empty():
    assert systematic_problem_codes([]) == []


# ---------- 抽检 ----------

def test_spot_check_is_deterministic():
    """同一个任务反复评估必须得到同样的抽检结论,否则重试一次就能把抽检摇掉。"""
    always = GradingThresholds(spot_check_ratio=1.0)
    verdicts = [_verdict("c1", Grade.A, 90.0)]
    first = _round(verdicts, thresholds=always)
    assert first.spot_check is True
    for _ in range(10):
        assert _round(verdicts, thresholds=always).spot_check is True


def test_spot_check_can_be_disabled():
    decision = _round([_verdict("c1", Grade.A, 90.0)], thresholds=GradingThresholds(
        spot_check_ratio=0.0
    ))
    assert decision.spot_check is False


def test_spot_check_does_not_block_auto_approval():
    always = GradingThresholds(spot_check_ratio=1.0)
    decision = _round([_verdict("c1", Grade.A, 90.0)], thresholds=always)
    assert decision.outcome is RoundOutcome.AUTO_APPROVED
    assert decision.selected_key == "c1"


def test_stable_fraction_is_in_range_and_deterministic():
    for key in ("a", "b", "task-1", "任务", ""):
        value = stable_fraction(key)
        assert 0.0 <= value < 1.0
        assert stable_fraction(key) == value


def test_stable_fraction_spreads_across_the_range():
    """抽检比例要有意义,映射就不能全挤在一头。"""
    values = [stable_fraction(f"task-{i}") for i in range(200)]
    assert min(values) < 0.1
    assert max(values) > 0.9
    assert 0.35 < sum(values) / len(values) < 0.65


# ---------- 决策是纯函数 ----------

def test_decision_is_deterministic():
    verdicts = [_verdict("c1", Grade.C, 60.0), _verdict("c2", Grade.D, 40.0)]
    first = _round(verdicts).to_dict()
    for _ in range(10):
        assert _round(verdicts).to_dict() == first


def test_default_thresholds_spot_check_ratio_is_documented_value():
    assert DEFAULT_THRESHOLDS.spot_check_ratio == 0.10
