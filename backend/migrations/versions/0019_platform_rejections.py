"""平台侧状态与驳回回流(OPS-REVIEW P1)。

新增:
    listing_drafts.platform_status       平台侧状态(NONE 默认)
    listing_drafts.platform_status_at    状态变更时间
    listing_drafts.platform_status_by    操作人
    platform_rejections                  驳回记录表

Revision ID: 0019
Revises: 0018
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from typing import Union

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    # --- 草稿行补三列 ---
    op.add_column(
        "listing_drafts",
        sa.Column(
            "platform_status",
            sa.String(16),
            nullable=False,
            server_default="NONE",
        ),
    )
    op.add_column(
        "listing_drafts",
        sa.Column("platform_status_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "listing_drafts",
        sa.Column("platform_status_by", sa.String(64), nullable=True),
    )

    # --- 驳回记录表 ---
    op.create_table(
        "platform_rejections",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "draft_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("listing_drafts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("spu", sa.String(64), nullable=False),
        sa.Column("sku", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(48), nullable=False),
        sa.Column("platform_note", sa.Text, nullable=True),
        sa.Column("export_fingerprint", sa.String(64), nullable=True),
        sa.Column("export_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "component_snapshot",
            sa.dialects.postgresql.JSONB,
            nullable=True,
        ),
        sa.Column("located_by", sa.String(24), nullable=False, server_default="unlocated"),
        sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(64), nullable=True),
        sa.Column("resolved_note", sa.Text, nullable=True),
        sa.Column("recorded_by", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_platform_rejections_draft_status",
        "platform_rejections",
        ["draft_id", "status"],
    )
    op.create_index(
        "ix_platform_rejections_spu_status",
        "platform_rejections",
        ["spu", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_platform_rejections_spu_status", table_name="platform_rejections")
    op.drop_index("ix_platform_rejections_draft_status", table_name="platform_rejections")
    op.drop_table("platform_rejections")
    op.drop_column("listing_drafts", "platform_status_by")
    op.drop_column("listing_drafts", "platform_status_at")
    op.drop_column("listing_drafts", "platform_status")
