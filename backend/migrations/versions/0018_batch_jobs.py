"""批量任务、批次条目与幂等回执(阶段 3,任务书 §6)。

三张表的分工见 `app/models/batch_job.py` 的模块注释。

这里只说一件迁移层面的事:`batch_action_receipts.idempotency_key` 的唯一约束
是"不重复触发付费模型调用"的**最后一道防线**。服务层会先查回执再执行,
但两个并发请求可以同时查到"没有回执"然后各自去调一次模型。唯一约束让第二个
在 INSERT 时撞掉,调用方捕获冲突后改走复用分支 —— 和 `db/locks.py` 里
"锁负责让正常并发不产生 500,索引负责让异常写入不产生脏数据"同一个思路。

Revision ID: 0018
Revises: 0017
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from typing import Union

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "batch_jobs",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="QUEUED"),
        sa.Column("label", sa.String(120), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False, server_default="system"),
        sa.Column("params", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_batch_jobs_created_at", "batch_jobs", ["created_at"])
    op.create_index(
        "ix_batch_jobs_action_status", "batch_jobs", ["action", "status"]
    )

    op.create_table(
        "batch_job_items",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False, server_default=""),
        sa.Column("spu", sa.String(64), nullable=False, server_default=""),
        sa.Column("scope_key", sa.String(128), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("target_step", sa.String(16), nullable=True),
        sa.Column("target_ref", sa.String(128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paid_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "idempotency_key", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch_jobs.id"],
            name="fk_batch_job_items_batch_id_batch_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_batch_job_items_product_id_products",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "batch_id", "product_id", name="uq_batch_job_items_batch_product"
        ),
    )
    op.create_index(
        "ix_batch_job_items_batch_status", "batch_job_items", ["batch_id", "status"]
    )
    op.create_index("ix_batch_job_items_product", "batch_job_items", ["product_id"])

    op.create_table(
        "batch_action_receipts",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("scope_key", sa.String(128), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("paid_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reuse_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("last_actor", sa.String(64), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_batch_action_receipts_product_id_products",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_batch_action_receipts_key"
        ),
    )
    op.create_index(
        "ix_batch_action_receipts_scope",
        "batch_action_receipts",
        ["action", "scope_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_batch_action_receipts_scope", table_name="batch_action_receipts")
    op.drop_table("batch_action_receipts")
    op.drop_index("ix_batch_job_items_product", table_name="batch_job_items")
    op.drop_index("ix_batch_job_items_batch_status", table_name="batch_job_items")
    op.drop_table("batch_job_items")
    op.drop_index("ix_batch_jobs_action_status", table_name="batch_jobs")
    op.drop_index("ix_batch_jobs_created_at", table_name="batch_jobs")
    op.drop_table("batch_jobs")
