"""内容计划与渠道文案

Revision ID: 0014
Revises: 0013
Create Date: 阶段 9-13 M6 —— 事实锚 + 按 (渠道, 站点, 语言) 独立生成

## 为什么 channel / site 用哨兵值而不是 NULL

它们进 `uq_listing_copies_version` 的唯一键,而 PostgreSQL 把 NULL 当成
互不相等 —— 用 NULL 的话,两条"平台无关来源稿"能同时存在。

图片集那边(0013)因为 channel/site 有真正的"未指定"语义,用的是
NULL + COALESCE 表达式索引;文案这边"平台无关"是一个**确定的值**,
所以直接用 'GENERIC' / 'ANY'。两种写法解决的是同一个问题,
选哪个取决于 NULL 在那张表里到底是不是一种有意义的状态。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_plans",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("spu", sa.String(64), nullable=False),
        # 只能来自 CONFIRMED 属性。模型不得新增或修改键值
        sa.Column("facts", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("selling_points", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("forbidden_claims", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("facts_version", sa.String(16), nullable=False, server_default="1"),
        # 进草稿指纹:属性一改,依赖它的草稿就该 STALE
        sa.Column("attr_snapshot_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_content_plans_spu", "content_plans", ["spu", "created_at"])

    op.create_table(
        "listing_copies",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("spu", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False, server_default="GENERIC"),
        sa.Column("site", sa.String(32), nullable=False, server_default="ANY"),
        sa.Column("category_id", sa.String(64), nullable=True),
        sa.Column("locale", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "content_plan_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("content_plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("bullet_points", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("keywords", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("extra", postgresql.JSONB(), nullable=False, server_default="{}"),
        # 模型对自己文本的结构化标注。后端逐条验证
        sa.Column("claims", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("violations", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("model_name", sa.String(120), nullable=True),
        sa.Column("prompt_version", sa.String(32), nullable=True),
        sa.Column("repair_rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("spec_version", sa.String(32), nullable=False, server_default="0"),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "spu", "channel", "site", "locale", "version",
            name="uq_listing_copies_version",
        ),
    )
    op.create_index(
        "ix_listing_copies_lookup",
        "listing_copies",
        ["spu", "channel", "site", "locale", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_listing_copies_lookup", table_name="listing_copies")
    op.drop_table("listing_copies")
    op.drop_index("ix_content_plans_spu", table_name="content_plans")
    op.drop_table("content_plans")
