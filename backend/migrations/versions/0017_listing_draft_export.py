"""草稿导出留痕(阶段 2,FE-235 / §5.2 步骤 13)。

「旧导出记录仍可追溯」需要两样东西:草稿上的最近导出信息(页面直接显示),
和审计日志里的每一次导出(完整历史)。这条迁移补前者。

Revision ID: 0017
Revises: 0016
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from typing import Union

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "listing_drafts",
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "listing_drafts", sa.Column("exported_by", sa.String(64), nullable=True)
    )
    op.add_column(
        "listing_drafts",
        sa.Column("export_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("listing_drafts", "export_count")
    op.drop_column("listing_drafts", "exported_by")
    op.drop_column("listing_drafts", "exported_at")
