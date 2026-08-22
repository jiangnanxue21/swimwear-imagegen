"""一轮候选图评完之后,任务该往哪走(需求第十二章)。

分档是**候选图级别**的判断,这里做的是**任务级别**的判断。两者必须分开:
需求第十二章明确写了"人工审核的对象是商品任务,不是全部低分候选图"。
一张 D 档候选图只是被淘汰,它自己永远不会出现在审核队列里;
只有当整个任务把轮次和可用 Provider 都用完了,任务才进队列。

纯函数,不碰数据库、不发网络请求。
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.enums import (
    EvaluationDepth,
    Grade,
    ReviewReason,
    TaskStatus,
)
from app.evaluators.repair import RepairAction, RepairPlan, build_repair_plan
from app.evaluators.rules import GradingThresholds


class RoundOutcome(StrEnum):
    AUTO_APPROVED = "AUTO_APPROVED"
    REGENERATE = "REGENERATE"
    MANUAL_REVIEW = "MANUAL_REVIEW"


@dataclass(frozen=True)
class CandidateVerdict:
    """一张候选图评完后的结论。`key` 是候选图标识,DB 里是 candidate.id。"""

    key: str
    grade: Grade
    overall_score: float
    depth: EvaluationDepth = EvaluationDepth.FULL
    hard_fail: bool = False
    hard_fail_codes: tuple[str, ...] = ()
    problem_codes: tuple[str, ...] = ()

    @property
    def can_auto_approve(self) -> bool:
        """能否凭这条结论自动通过。

        快速检查(需求第十三章)只查硬错误,查不出问题**不等于**这张图好。
        因此只有完整评分出来的 A 档才算数 —— 否则省成本的机制会变成放水机制。
        """
        return (
            self.grade is Grade.A
            and self.depth is EvaluationDepth.FULL
            and not self.hard_fail
        )


@dataclass(frozen=True)
class RoundDecision:
    outcome: RoundOutcome
    next_status: TaskStatus
    selected_key: str | None = None
    rejected_keys: tuple[str, ...] = ()
    repair_plan: RepairPlan | None = None
    review_reason: ReviewReason | None = None
    #: A 档抽检命中。任务照常通过,只是额外生成一条不阻塞的审核项。
    spot_check: bool = False
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "next_status": self.next_status.value,
            "selected_key": self.selected_key,
            "rejected_keys": list(self.rejected_keys),
            "repair_plan": self.repair_plan.to_dict() if self.repair_plan else None,
            "review_reason": self.review_reason.value if self.review_reason else None,
            "spot_check": self.spot_check,
            "reasons": list(self.reasons),
        }


def stable_fraction(key: str) -> float:
    """把任意字符串映射到 [0,1) 的确定性小数。

    抽检用它而不是 random:同一个任务反复评估必须得到同样的抽检结论,
    否则重试一次就能把抽检"摇"掉。
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def systematic_problem_codes(
    verdicts: list[CandidateVerdict], *, min_ratio: float = 0.5
) -> list[str]:
    """挑出本轮**系统性**出现的问题代码。

    只看最好那张图的问题会漏掉规律性缺陷:四张图里三张都把肩带画错,
    说明这是提示词或模型的系统偏差,必须专门修,而不是换个 seed 碰运气。
    """
    if not verdicts:
        return []
    counts: dict[str, int] = {}
    for verdict in verdicts:
        for code in {*verdict.problem_codes, *verdict.hard_fail_codes}:
            counts[code] = counts.get(code, 0) + 1
    threshold = max(1, int(len(verdicts) * min_ratio + 0.999))  # 向上取整
    return sorted(code for code, count in counts.items() if count >= threshold)


def decide_round(
    verdicts: list[CandidateVerdict],
    *,
    round_number: int,
    max_rounds: int,
    thresholds: GradingThresholds,
    task_key: str = "",
    provider_exhausted: bool = False,
    repair_table: Mapping[str, RepairAction] | None = None,
) -> RoundDecision:
    """决定本轮结束后任务的去向。"""
    if not verdicts:
        # 一张图都没评出来:当作本轮失败,按轮次决定重生还是转人工
        return _no_winner(
            [],
            round_number=round_number,
            max_rounds=max_rounds,
            thresholds=thresholds,
            provider_exhausted=provider_exhausted,
            repair_table=repair_table,
            extra_reason="本轮没有可评分的候选图",
        )

    # ---- 优先选择达到 A 档且总分最高的候选图(需求第十三章)----
    winners = [v for v in verdicts if v.can_auto_approve]
    if winners:
        # 同分时按 key 排序,保证可复现
        best = sorted(winners, key=lambda v: (-v.overall_score, v.key))[0]
        ratio = thresholds.spot_check_ratio
        spot_check = bool(ratio > 0 and stable_fraction(task_key or best.key) < ratio)
        reasons = [f"候选 {best.key} 达到 A 档,总分 {best.overall_score}"]
        if spot_check:
            reasons.append(f"命中随机抽检(比例 {ratio:.0%}),生成一条不阻塞的复核项")
        return RoundDecision(
            outcome=RoundOutcome.AUTO_APPROVED,
            next_status=TaskStatus.AUTO_APPROVED,
            selected_key=best.key,
            rejected_keys=tuple(sorted(v.key for v in verdicts if v.key != best.key)),
            spot_check=spot_check,
            reasons=tuple(reasons),
        )

    return _no_winner(
        verdicts,
        round_number=round_number,
        max_rounds=max_rounds,
        thresholds=thresholds,
        provider_exhausted=provider_exhausted,
        repair_table=repair_table,
    )


def _no_winner(
    verdicts: list[CandidateVerdict],
    *,
    round_number: int,
    max_rounds: int,
    thresholds: GradingThresholds,
    provider_exhausted: bool,
    repair_table: Mapping[str, RepairAction] | None = None,
    extra_reason: str | None = None,
) -> RoundDecision:
    """本轮没有可自动通过的候选图。"""
    rejected = tuple(sorted(v.key for v in verdicts))
    reasons: list[str] = []
    if extra_reason:
        reasons.append(extra_reason)

    # 轮次还没用完 -> 淘汰当前候选并重生,**不进人工审核**(需求第十一、十二章)
    if round_number < max_rounds:
        best = (
            sorted(verdicts, key=lambda v: (v.grade.rank, -v.overall_score, v.key))[0]
            if verdicts
            else None
        )
        codes = systematic_problem_codes(verdicts)
        if best is not None:
            # 最好那张图的问题一定要修 —— 它是最接近过线的那条线
            for code in [*best.problem_codes, *best.hard_fail_codes]:
                if code not in codes:
                    codes.append(code)
        hard_codes = sorted({c for v in verdicts for c in v.hard_fail_codes})

        plan = build_repair_plan(
            grade=best.grade if best else Grade.D,
            problem_codes=sorted(set(codes)),
            hard_fail_codes=hard_codes,
            allow_provider_switch=thresholds.allow_provider_switch and not provider_exhausted,
            repair_table=repair_table,
        )
        reasons.append(
            f"第 {round_number}/{max_rounds} 轮无 A 档候选,淘汰全部候选并按修复策略重生"
        )
        if hard_codes:
            reasons.append(f"本轮硬错误:{'、'.join(hard_codes)}")
        return RoundDecision(
            outcome=RoundOutcome.REGENERATE,
            next_status=TaskStatus.REGENERATING,
            rejected_keys=rejected,
            repair_plan=plan,
            reasons=tuple(reasons),
        )

    # 轮次用尽 -> 任务(不是候选图)进入人工审核队列
    reasons.append(f"已用完 {max_rounds} 轮仍无 A 档候选,任务转人工审核")
    return RoundDecision(
        outcome=RoundOutcome.MANUAL_REVIEW,
        next_status=TaskStatus.MANUAL_REVIEW,
        rejected_keys=rejected,
        review_reason=(
            ReviewReason.PROVIDER_EXHAUSTED if provider_exhausted else ReviewReason.ROUNDS_EXHAUSTED
        ),
        # 不再自动重生,给一份修复计划只会误导后续代码去执行它
        repair_plan=None,
        reasons=tuple(reasons),
    )
