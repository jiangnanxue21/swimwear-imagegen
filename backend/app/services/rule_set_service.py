"""规则集加载(需求第十二章)。

数据库里的 `RuleSet` 是**可选覆盖**,不是必需配置:
一个空库必须能跑完整条流水线,否则"没有外部依赖也能跑"这条底线就破了。
因此这里的取值顺序是:活跃 RuleSet -> 代码默认值。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evaluators.rules import DEFAULT_THRESHOLDS, GradingThresholds
from app.evaluators.scoring import DEFAULT_WEIGHTS
from app.models.evaluation import RuleSet

DEFAULT_RULE_SET_NAME = "default"


@dataclass(frozen=True)
class ResolvedRuleSet:
    """解析后的规则集。评分器拿到的是 `payload`,分档拿到的是 `thresholds`。"""

    id: UUID | None
    name: str
    version: int
    thresholds: GradingThresholds
    evaluator_options: dict[str, Any]

    def payload(self, **extra: Any) -> dict[str, Any]:
        """传给 `ImageQualityEvaluator.evaluate` 的 rule_set 参数。"""
        return {
            "name": self.name,
            "version": self.version,
            "weights": dict(self.thresholds.weights),
            "thresholds": self.thresholds.to_dict(),
            **self.evaluator_options,
            **extra,
        }


DEFAULT_RESOLVED = ResolvedRuleSet(
    id=None,
    name=DEFAULT_RULE_SET_NAME,
    version=0,
    thresholds=DEFAULT_THRESHOLDS,
    evaluator_options={},
)


def load_active_rule_set(session: Session) -> ResolvedRuleSet:
    """取当前生效的规则集。没有活跃行时用代码默认值。"""
    row = session.scalar(
        select(RuleSet).where(RuleSet.is_active.is_(True)).order_by(RuleSet.version.desc())
    )
    if row is None:
        return DEFAULT_RESOLVED
    return ResolvedRuleSet(
        id=row.id,
        name=row.name,
        version=row.version,
        thresholds=GradingThresholds.from_dict(row.thresholds, weights=row.weights),
        evaluator_options=dict(row.evaluator_options or {}),
    )


def ensure_default_rule_set(session: Session) -> RuleSet:
    """确保库里有一条默认规则集。幂等,可重复调用。

    存在的意义是让运营在后台**看得见**当前判定标准,
    而不是去翻代码里的常量。
    """
    existing = session.scalar(
        select(RuleSet).where(RuleSet.name == DEFAULT_RULE_SET_NAME, RuleSet.version == 1)
    )
    if existing is not None:
        return existing

    row = RuleSet(
        name=DEFAULT_RULE_SET_NAME,
        version=1,
        description="需求第十二章默认分档规则",
        is_active=True,
        weights=dict(DEFAULT_WEIGHTS),
        thresholds=DEFAULT_THRESHOLDS.to_dict(),
        evaluator_options={},
    )
    session.add(row)
    session.flush()
    return row


def list_rule_sets(session: Session) -> list[RuleSet]:
    return list(
        session.scalars(select(RuleSet).order_by(RuleSet.name, RuleSet.version.desc()))
    )
