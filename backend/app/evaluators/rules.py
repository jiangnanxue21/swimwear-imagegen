"""A/B/C/D 分档规则(需求第十二章)。

纯函数,不碰数据库、不发网络请求。阈值来自 ``GradingThresholds``,
默认值写在这里,运行时可被数据库中的 ``RuleSet`` 覆盖。

分档由三条线共同决定,取**最差**的一条:

1. 硬错误  —— 出现即 D,不论总分多高(需求第十一章);
2. 分数区间 —— A ≥85(且过门槛项)/ B 70-84 / C 55-69 / D <55;
3. 问题严重度上限 —— MINOR 最好 B,MAJOR 最好 C,CRITICAL 只能 D。

第 3 条是需求第十二章里那些"或存在……问题"从句的落地方式。
没有它,一张总分 90 但腰部结构明显崩坏的图会被判 A。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.enums import (
    GRADE_ACTION,
    SEVERITY_GRADE_CAP,
    EvaluationDepth,
    Grade,
    RecommendedAction,
    worst_of,
)
from app.evaluators.base import DIMENSION_LABELS, EvaluationResult
from app.evaluators.scoring import DEFAULT_WEIGHTS, normalize_weights


@dataclass(frozen=True)
class GradingThresholds:
    """分档阈值(需求第十二章默认值)。

    A 档除了总分,还要过四道**商品还原度与可用性**门槛。
    这四项任何一项不达标,即使总分很高也不能自动通过 ——
    电商图的价值在于"衣服是对的",总分是加权平均,会把这个致命项摊薄。
    """

    a_min_overall: float = 85.0
    a_min_garment_identity: float = 90.0
    a_min_structural_fidelity: float = 90.0
    a_min_anatomy_realism: float = 85.0
    a_min_website_usability: float = 85.0

    b_min_overall: float = 70.0
    c_min_overall: float = 55.0

    #: A 档随机抽检比例(0-1)。0 表示不抽检。
    spot_check_ratio: float = 0.10
    #: 轮内预排序开关(需求第十三章),关掉后所有候选图都做完整评分
    prescreen_enabled: bool = True
    #: 预排序后做完整评分的候选数,其余只做快速硬错误检查
    full_evaluation_count: int = 2
    #: C 档是否允许换 Provider(实际能不能换取决于有没有第二个已配置的 Provider)
    allow_provider_switch: bool = True

    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    @property
    def a_gates(self) -> dict[str, float]:
        """A 档的逐维度门槛。"""
        return {
            "garment_identity": self.a_min_garment_identity,
            "structural_fidelity": self.a_min_structural_fidelity,
            "anatomy_realism": self.a_min_anatomy_realism,
            "website_usability": self.a_min_website_usability,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "a_min_overall": self.a_min_overall,
            "a_min_garment_identity": self.a_min_garment_identity,
            "a_min_structural_fidelity": self.a_min_structural_fidelity,
            "a_min_anatomy_realism": self.a_min_anatomy_realism,
            "a_min_website_usability": self.a_min_website_usability,
            "b_min_overall": self.b_min_overall,
            "c_min_overall": self.c_min_overall,
            "spot_check_ratio": self.spot_check_ratio,
            "prescreen_enabled": self.prescreen_enabled,
            "full_evaluation_count": self.full_evaluation_count,
            "allow_provider_switch": self.allow_provider_switch,
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any] | None, *, weights: dict[str, Any] | None = None
    ) -> GradingThresholds:
        """从数据库 RuleSet 构造。未知键忽略,坏值退回默认,绝不因为脏配置炸掉流水线。"""
        defaults = cls()
        if not data:
            return cls(weights=normalize_weights(weights))

        def num(key: str, fallback: float) -> float:
            value = data.get(key, fallback)
            try:
                return float(value)
            except (TypeError, ValueError):
                return fallback

        def flag(key: str, fallback: bool) -> bool:
            value = data.get(key, fallback)
            return bool(value) if isinstance(value, (bool, int)) else fallback

        return cls(
            a_min_overall=num("a_min_overall", defaults.a_min_overall),
            a_min_garment_identity=num("a_min_garment_identity", defaults.a_min_garment_identity),
            a_min_structural_fidelity=num(
                "a_min_structural_fidelity", defaults.a_min_structural_fidelity
            ),
            a_min_anatomy_realism=num("a_min_anatomy_realism", defaults.a_min_anatomy_realism),
            a_min_website_usability=num(
                "a_min_website_usability", defaults.a_min_website_usability
            ),
            b_min_overall=num("b_min_overall", defaults.b_min_overall),
            c_min_overall=num("c_min_overall", defaults.c_min_overall),
            spot_check_ratio=max(0.0, min(1.0, num("spot_check_ratio", defaults.spot_check_ratio))),
            prescreen_enabled=flag("prescreen_enabled", defaults.prescreen_enabled),
            full_evaluation_count=max(
                1, int(num("full_evaluation_count", defaults.full_evaluation_count))
            ),
            allow_provider_switch=flag("allow_provider_switch", defaults.allow_provider_switch),
            weights=normalize_weights(weights),
        )


DEFAULT_THRESHOLDS = GradingThresholds()


@dataclass(frozen=True)
class GradeDecision:
    """分档结论。``reasons`` 是给人看的判据,直接展示在审核页上。"""

    grade: Grade
    action: RecommendedAction
    reasons: tuple[str, ...] = ()
    failed_gates: tuple[str, ...] = ()

    @property
    def is_auto_approvable(self) -> bool:
        return self.grade is Grade.A and self.action is RecommendedAction.AUTO_APPROVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "grade": self.grade.value,
            "action": self.action.value,
            "reasons": list(self.reasons),
            "failed_gates": list(self.failed_gates),
        }


def _score_band(overall: float, thresholds: GradingThresholds) -> Grade:
    if overall >= thresholds.a_min_overall:
        return Grade.A
    if overall >= thresholds.b_min_overall:
        return Grade.B
    if overall >= thresholds.c_min_overall:
        return Grade.C
    return Grade.D


def grade_candidate(
    result: EvaluationResult,
    thresholds: GradingThresholds | None = None,
) -> GradeDecision:
    """给一张候选图分档。确定性:同样的输入永远得到同样的结论。"""
    th = thresholds or DEFAULT_THRESHOLDS
    reasons: list[str] = []

    # ---- 第一条线:硬错误一票否决(需求第十一章)----
    if result.hard_fail:
        codes = "、".join(result.hard_fail_codes) or "未指明代码"
        return GradeDecision(
            grade=Grade.D,
            action=RecommendedAction.REJECT,
            reasons=(f"硬错误:{codes};不论总分多高都不能自动通过",),
        )

    # ---- 第二条线:分数区间 ----
    band = _score_band(result.overall_score, th)
    if band is Grade.A:
        reasons.append(f"总分 {result.overall_score} ≥ {th.a_min_overall}")
    else:
        reasons.append(f"总分 {result.overall_score}")

    # A 档还要过逐维度门槛
    failed_gates: list[str] = []
    if band is Grade.A:
        for dimension, minimum in th.a_gates.items():
            value = result.scores.get(dimension)
            if value is None:
                # 快速检查没产出这个维度 -> 不具备判 A 的信息,保守降档
                failed_gates.append(dimension)
                reasons.append(f"{DIMENSION_LABELS.get(dimension, dimension)}缺少评分")
            elif value < minimum:
                failed_gates.append(dimension)
                reasons.append(
                    f"{DIMENSION_LABELS.get(dimension, dimension)} {value} < {minimum}"
                )
        if failed_gates:
            band = Grade.B  # 总分够但还原度不够 -> 定向重生,不是淘汰

    # ---- 第三条线:问题严重度上限 ----
    cap = Grade.A
    severity = result.worst_severity
    if severity is not None:
        cap = SEVERITY_GRADE_CAP[severity]
        reasons.append(
            f"存在 {severity.value} 级问题({len(result.problems)} 条),最高只能 {cap.value} 档"
        )

    final = worst_of(band, cap)
    return GradeDecision(
        grade=final,
        action=GRADE_ACTION[final],
        reasons=tuple(reasons),
        failed_gates=tuple(failed_gates),
    )


def quick_check_result_is_conclusive(result: EvaluationResult) -> bool:
    """快速检查的结论能否直接采信。

    快速检查只查硬错误(需求第十三章)。查出来了就是确定的淘汰;
    没查出来**不代表这张图好**,只代表它没有明显硬伤,
    因此不能凭快速检查判 A —— 必须补一次完整评分。
    """
    return result.depth is EvaluationDepth.FULL or result.hard_fail
