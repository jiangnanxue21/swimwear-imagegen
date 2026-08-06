"""派发意图表(事务性 Outbox)

Revision ID: 0008
Revises: 0007
Create Date: 代码评审第 3 条 —— 消息队列派发失败后任务永久滞留

「先提交事务再 .delay()」中间那道缝里进程挂掉,任务就永远停在 CREATED /
REGENERATING,队列里却没有对应的消息。原来靠「打标记 + 定时扫描状态」兜底,
那是启发式的:它只能猜"停在这个状态超过两分钟的大概是漏投了",
队列积压时会误判,而真正漏投的又要等下一次扫描。

这张表把派发意图变成业务事务的一部分,不再需要猜。

不预置数据。历史上滞留的任务仍由 requeue_stranded 的状态扫描分支捞回来 ——
那条分支保留着,专门服务这次升级之前产生的存量任务。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_dispatches",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("generation_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(24), nullable=False, server_default="create"),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # relay 的主查询:status='PENDING' AND available_at <= now()
    op.create_index(
        "ix_task_dispatches_status_available_at",
        "task_dispatches",
        ["status", "available_at"],
    )
    op.create_index("ix_task_dispatches_task_id", "task_dispatches", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_dispatches_task_id", table_name="task_dispatches")
    op.drop_index("ix_task_dispatches_status_available_at", table_name="task_dispatches")
    op.drop_table("task_dispatches")
