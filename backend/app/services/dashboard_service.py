"""仪表盘统计(需求第十四章 / 阶段 6)。

一次请求要出十来个数字。做法是**每个指标一条聚合查询**,不是把全表拉进内存再数 ——
商品和候选图的量级会长得很快,拉进内存的写法在几万行时就会开始卡。

刻意没做缓存:阶段 6 不做高并发优化(需求第二十三章),
后台仪表盘的访问频率也撑不起一层缓存的复杂度。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.clock import iso_utc
from app.core.enums import Grade, ReviewStatus, TaskStatus
from app.models.evaluation import CandidateEvaluation, ReviewItem
from app.models.generation import (
    GenerationAttempt,
    GenerationTask,
    ProviderUsageRecord,
)
from app.models.output_asset import OutputAsset
from app.models.product import Product
from app.workflows import state_machine as sm

#: "最近失败任务"取多少条
RECENT_FAILURE_LIMIT = 10


def _count(session: Session, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    return int(session.scalar(stmt) or 0)


def _group_count(session: Session, column, *where) -> dict[str, int]:
    """按列分组计数。`where` 可选 —— 有些指标要和同一块面板上的总数**同口径**,
    而总数是带过滤的(例如成品图只数 enabled 的)。"""
    stmt = select(column, func.count()).group_by(column)
    if where:
        stmt = stmt.where(*where)
    return {str(key): int(value) for key, value in session.execute(stmt).all() if key is not None}


def summary(session: Session) -> dict[str, Any]:
    """需求第十四章点名的全部指标。"""
    task_by_status = _group_count(session, GenerationTask.status)

    # 平均生成轮次:只统计已经跑完的任务。还在跑的任务轮次必然偏低,
    # 混进去会让这个数看起来一直很漂亮。
    #
    # **MANUAL_REVIEW 不在其中**(BLOCK-18)。那个状态的含义是"轮次耗尽或
    # 命中抽检,正在等人",它既可能被批准也可能被退回重生 —— 后者会让
    # `current_round` 继续涨。把它算进"平均要跑几轮"等于用一批还没跑完的
    # 任务去拉低平均值,而这个指标的用途恰恰是判断"要不要调提示词/换模板"。
    # 越是卡在人工审核的任务多,这个数越漂亮,方向正好是反的。
    finished_statuses = [
        TaskStatus.AUTO_APPROVED.value,
        TaskStatus.MANUALLY_APPROVED.value,
        TaskStatus.MANUALLY_REJECTED.value,
        TaskStatus.AUTO_REJECTED.value,
        TaskStatus.COMPLETED.value,
    ]
    avg_rounds = session.scalar(
        select(func.avg(GenerationTask.current_round)).where(
            GenerationTask.status.in_(finished_statuses),
            GenerationTask.current_round > 0,
        )
    )

    provider_calls = _group_count(session, ProviderUsageRecord.provider)
    grade_counts = _group_count(session, CandidateEvaluation.grade)
    review_counts = _group_count(session, ReviewItem.status)

    # 「自动通过」不能把 COMPLETED 整个算进去(BLOCK-18)。
    #
    # COMPLETED 是**出图完成**这一个事实,而通往它的路有两条:
    #
    #   AUTO_APPROVED      -> FORMATTING -> COMPLETED
    #   MANUALLY_APPROVED  -> FORMATTING -> COMPLETED
    #
    # 原来的写法是 `AUTO_APPROVED + COMPLETED`,于是每一件人工批准并出完图的
    # 商品都被记成"自动通过"。这个数字是判断"自动化到底顶不顶用"的唯一依据,
    # 而它的偏差方向是**系统性偏高**:自动化越差、人工介入越多,它反而越好看。
    #
    # 判据用审核台账而不是任务状态:一件任务是不是被人工批过,`review_items`
    # 里有明确记录(阻塞性审核项且结论为 APPROVED),而任务状态在流转到
    # COMPLETED 之后已经把这件事抹掉了。
    manually_approved_completed = int(
        session.scalar(
            select(func.count(func.distinct(ReviewItem.task_id)))
            .select_from(ReviewItem)
            .join(GenerationTask, GenerationTask.id == ReviewItem.task_id)
            .where(
                ReviewItem.status == ReviewStatus.APPROVED.value,
                ReviewItem.blocking.is_(True),
                GenerationTask.status == TaskStatus.COMPLETED.value,
            )
        )
        or 0
    )
    completed_total = task_by_status.get(TaskStatus.COMPLETED.value, 0)
    # 剩下的 COMPLETED 就是自动通过后出完图的。夹一个 max(0, ...):
    # 数据修补或历史数据可能让两个数对不上,而一个负数会直接毁掉整块面板
    auto_completed = max(completed_total - manually_approved_completed, 0)

    recent_failures = [
        {
            "id": str(row.id),
            "product_id": str(row.product_id),
            "provider": row.provider,
            "status": row.status,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "created_at": iso_utc(row.created_at),
        }
        for row in session.scalars(
            select(GenerationTask)
            .where(GenerationTask.status == TaskStatus.FAILED.value)
            .order_by(GenerationTask.created_at.desc())
            .limit(RECENT_FAILURE_LIMIT)
        )
    ]

    attempt_stats = session.execute(
        select(
            func.count(GenerationAttempt.id),
            func.sum(case((GenerationAttempt.status == "FAILED", 1), else_=0)),
        )
    ).one()

    return {
        "products": {
            "total": _count(session, Product),
            "by_status": _group_count(session, Product.status),
        },
        "tasks": {
            "total": _count(session, GenerationTask),
            "by_status": task_by_status,
            # 见上面 `auto_completed` 那段:COMPLETED 里混着人工批准的那部分,
            # 不能整个算进"自动通过"
            "auto_approved": task_by_status.get(TaskStatus.AUTO_APPROVED.value, 0)
            + auto_completed,
            # 人工批准的单独给一个数。原来它不存在,于是"自动化到底顶不顶用"
            # 只能拿一个被污染的 auto_approved 去猜
            "manually_approved": task_by_status.get(
                TaskStatus.MANUALLY_APPROVED.value, 0
            )
            + manually_approved_completed,
            "completed": completed_total,
            "auto_rejected": task_by_status.get(TaskStatus.AUTO_REJECTED.value, 0),
            "manual_review": task_by_status.get(TaskStatus.MANUAL_REVIEW.value, 0),
            "failed": task_by_status.get(TaskStatus.FAILED.value, 0),
            # A9:「运行中任务」卡片要的数。前端本来能自己把 by_status 加起来,
            # 但"哪些状态算运行中"是状态机的知识(`IN_FLIGHT_STATES`):
            # 后端新增一个中间状态时,前端那份清单不会有任何报错,
            # 它只会安静地少数几件。所以这个和号在后端加,清单也从状态机取。
            "in_flight": sum(
                task_by_status.get(status.value, 0) for status in sm.IN_FLIGHT_STATES
            ),
            "average_rounds": round(float(avg_rounds), 2) if avg_rounds is not None else None,
        },
        "grades": {grade.value: grade_counts.get(grade.value, 0) for grade in Grade},
        "reviews": {
            "total": sum(review_counts.values()),
            "pending": review_counts.get(ReviewStatus.PENDING.value, 0),
            "by_status": review_counts,
        },
        "providers": {
            "calls_by_provider": provider_calls,
            "total_calls": sum(provider_calls.values()),
            "attempts": int(attempt_stats[0] or 0),
            "failed_attempts": int(attempt_stats[1] or 0),
        },
        "outputs": {
            "total": _count(session, OutputAsset, OutputAsset.enabled.is_(True)),
            # `total` 过滤了 enabled,这里以前没过滤 —— 两个数口径不同,
            # 于是 by_purpose 的和会大于 total,而面板上它们挨着显示。
            # 被停用的成品图(重出图后的旧代)是"曾经有过",不是"现在有"
            "by_purpose": _group_count(
                session, OutputAsset.purpose, OutputAsset.enabled.is_(True)
            ),
            "products_with_outputs": int(
                session.scalar(
                    select(func.count(func.distinct(OutputAsset.product_id))).where(
                        OutputAsset.enabled.is_(True)
                    )
                )
                or 0
            ),
        },
        "recent_failures": recent_failures,
    }
