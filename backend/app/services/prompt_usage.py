"""提示词调用统计的取数(PRD BE-302 / FE-301 / FE-306)。

判定全部在 `app/workflows/prompt_stats.py`,这里只做两件事:把 SQL 分组计数
取回来,和把「统计从什么时候开始有」答出来。

## 为什么单独一个模块

`prompt_service` 管的是 `prompt_templates` 的读写。这里读的是
`evaluation_attempts` —— 另一个域的表,只为回答「这一份提示词被用得怎么样」。
混进去的话,一个只想改保存逻辑的人得先读懂评分留痕表。

## GROUP BY 而不是取回原始行

这张表每次评分都写一行(一轮 4 张候选 × 最多 3 轮),按用量线性增长。
取回原始行再在 Python 里数,近 7 天可能是几十万行 —— 而它挂在一个每次打开
列表页都要跑的接口上。分组之后回传的是十几行计数,判定层照样穷举得了
(`tally` 认 `count` 键)。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.models.evaluation import EvaluationAttempt
from app.workflows.prompt_stats import RECENT_WINDOW_DAYS, PromptCallStats, tally

#: `meta` 里那两个布尔的取值路径。写入点在
#: `evaluation_service._record_attempt` 的 `meta={...}`,两处必须同名 ——
#: 拼错一个字母的后果是**全部行都被当成非整轮、非诊断**,而统计照样出数,
#: 没有任何东西会红。`test_a70_prompt_stats.py` 对着写入点比对这两个字面量。
_META_ROUND_LEVEL = "round_level"
_META_DIAGNOSTIC = "diagnostic"


def _grouped(session: Session, *, key: str | None = None, since=None, by_version: bool = False):
    """按 (outcome, round_level, diagnostic[, version]) 分组计数。

    `meta` 是 JSONB,两个布尔用 `->>` 取成文本再和 `'true'` 比 —— 键不存在时
    `->>` 返回 NULL,比出来是 false,正好是历史行该有的判定(它们没有
    `round_level`,而它们同时也没有 `prompt_key`,所以根本进不了按 key 的统计)。
    """
    meta = EvaluationAttempt.meta
    round_level = (meta[_META_ROUND_LEVEL].astext == "true").label("round_level")
    diagnostic = (meta[_META_DIAGNOSTIC].astext == "true").label("diagnostic")

    columns = [EvaluationAttempt.outcome, round_level, diagnostic, func.count().label("count")]
    if by_version:
        columns.insert(0, EvaluationAttempt.prompt_version)

    stmt = select(*columns)
    if key is not None:
        stmt = stmt.where(EvaluationAttempt.prompt_key == key)
    else:
        stmt = stmt.where(EvaluationAttempt.prompt_key.is_(None))
    if since is not None:
        stmt = stmt.where(EvaluationAttempt.created_at >= since)

    group_by = [EvaluationAttempt.outcome, round_level, diagnostic]
    if by_version:
        group_by.insert(0, EvaluationAttempt.prompt_version)
    return session.execute(stmt.group_by(*group_by)).all()


def recent_stats(
    session: Session, key: str, *, days: int = RECENT_WINDOW_DAYS
) -> PromptCallStats:
    """一份提示词近 N 天的调用统计(FE-301 那一列)。"""
    since = utc_now() - timedelta(days=days)
    rows = _grouped(session, key=key, since=since)
    return tally(
        [
            {
                "outcome": r.outcome,
                "round_level": r.round_level,
                "diagnostic": r.diagnostic,
                "count": r.count,
            }
            for r in rows
        ]
    )


def version_stats(session: Session, key: str) -> dict[str, PromptCallStats]:
    """这份提示词**每一版**的调用统计(FE-306 那一列),不设时间窗。

    键是 `prompt_version` 的**字符串**形态,与 `evaluation_attempts` 里存的一致 ——
    调用方拿版本号去查时要自己 `str()`。不在这里转成 int:那一列是 String(32),
    历史值里可能有 `llm-1` 这种人写的标签(见迁移 0055 的兼容性说明),
    int() 会抛,而抛在一个统计上等于整页打不开。

    不设时间窗是刻意的:FE-306 问的是「这一版一共跑过多少次」,而一个老版本
    的全部调用可能都在 7 天之前 —— 加了窗会让每一个非当前版本都显示 0 次。
    """
    rows = _grouped(session, key=key, by_version=True)
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        version = r.prompt_version
        if version is None:
            # 版本号为空的行归不到任何一版上。它们仍然在 `recent_stats` 的
            # 总数里(那里不按版本分),这里跳过而不是塞进一个 "" 桶 ——
            # 界面上一个空版本号的行会被读成"有这么一版"
            continue
        buckets.setdefault(str(version), []).append(
            {
                "outcome": r.outcome,
                "round_level": r.round_level,
                "diagnostic": r.diagnostic,
                "count": r.count,
            }
        )
    return {version: tally(rows) for version, rows in buckets.items()}


def unattributed_recent(session: Session, *, days: int = RECENT_WINDOW_DAYS) -> int:
    """近 N 天里**归不了属**的调用次数(`prompt_key IS NULL`)。

    三种来源:0059 之前的历史行、提示词读取失败退回内置默认的降级路径、
    受众推不出来。它们不该被算进任何一份提示词,也不该消失 —— 消失的话
    「近 7 天 3 次」会被读成「这一份只被用了 3 次」,而真相可能是另外 297 次
    落在统计的起点之前。界面拿这个数说「另有 N 次未归属」。
    """
    since = utc_now() - timedelta(days=days)
    rows = _grouped(session, key=None, since=since)
    return tally(
        [
            {
                "outcome": r.outcome,
                "round_level": r.round_level,
                "diagnostic": r.diagnostic,
                "count": r.count,
            }
            for r in rows
        ]
    ).calls


def coverage_since(session: Session):
    """统计**从什么时候开始有**:第一条带 `prompt_key` 的留痕时间。

    没有任何带归属的行时返回 None(0059 刚执行完、还没评过分)。

    界面要拿它说「统计自 X 起」。少了这句,一个 0059 之前跑过 500 次的版本
    在版本表里显示「调用 0 次」,而那是一句假话 —— 它和一个真的没人用过的
    版本长得一模一样,正是 FE-306 这一列要分开的两件事。
    """
    return session.execute(
        select(func.min(EvaluationAttempt.created_at)).where(
            EvaluationAttempt.prompt_key.isnot(None)
        )
    ).scalar_one_or_none()


__all__ = [
    "coverage_since",
    "recent_stats",
    "unattributed_recent",
    "version_stats",
]
