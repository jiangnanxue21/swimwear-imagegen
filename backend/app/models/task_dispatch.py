"""派发意图表(事务性 Outbox,评审第 3 条)。

## 为什么需要这张表

原来的顺序是「先提交业务事务,再 `.delay()`」:

    session.commit()          # 任务已经落库
    run_generation_task.delay(...)   # <- 这里挂了/进程被 kill

中间那道缝里,任务已经是 CREATED 或 REGENERATING,界面上看着像"排队中",
队列里却没有任何消息,永远不会有 worker 来跑它。反过来「先派发再提交」更糟:
worker 可能抢在提交之前读到不存在的任务。**两个顺序都有窗口,这不是能靠调顺序解决的问题。**

Outbox 的做法是把"我打算投递这条消息"当成业务数据的一部分,和任务写在
**同一个事务**里。事务提交了,意图就一定在;事务回滚了,意图也一定不在。
之后由 relay 负责把 PENDING 的意图真正投进队列 —— 投递可以失败任意多次,
因为意图还躺在库里,下一轮扫描会再来一次。

## 刻意不追求"恰好一次"

投递成功、标记 DISPATCHED 之前进程挂掉 -> relay 会再投一次 -> 同一个任务收到两条消息。
这是**可接受的**,因为 worker 侧有带状态条件的原子认领(评审第 2 条):
第二个 worker 抢不到,直接退出。至少一次 + 幂等消费,比恰好一次简单得多,
也比"先提交后派发"漏投可靠得多。
"""
from __future__ import annotations

# datetime 必须是运行时 import,理由同 models/generation.py:
# SQLAlchemy 2 建映射时要解析 Mapped[...] 注解字符串。
from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import DispatchReason, DispatchStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class TaskDispatch(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "task_dispatches"
    __table_args__ = (
        # relay 的主查询就是这个组合:status='PENDING' AND available_at <= now()
        Index("ix_task_dispatches_status_available_at", "status", "available_at"),
        Index("ix_task_dispatches_task_id", "task_id"),
    )

    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: 哪条路径产生的这次派发。漏投时能一眼定位是创建、重试还是重生在漏。
    reason: Mapped[str] = mapped_column(
        String(24), nullable=False, default=DispatchReason.CREATE.value
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DispatchStatus.PENDING.value
    )
    #: 已经尝试投递的次数。用来算退避,也用来判定何时放弃。
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 最早可以尝试投递的时刻。Broker 挂掉时靠它退避,避免 relay 空转打满日志。
    available_at: Mapped[datetime | None] = mapped_column(nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: 最后一次投递失败的原因。只存我们自己拼的说明,不塞第三方异常原文。
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
