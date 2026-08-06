"""网站图片输出表:output_assets

Revision ID: 0004
Revises: 0003
Create Date: 阶段 6

同前几版的做法:列定义一律展开写。迁移是历史快照,
依赖可变的辅助函数会让旧迁移的语义随代码演进悄悄改变。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "output_assets",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("candidate_id", UUID, nullable=True),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=False),
        sa.Column("image_format", sa.String(length=8), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("meta", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["generation_tasks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["candidate_id"], ["generation_candidates.id"],
                                ondelete="SET NULL"),
        sa.UniqueConstraint("candidate_id", "purpose", "generation",
                            name="uq_output_assets_candidate_purpose"),
    )
    op.create_index("ix_output_assets_product_id", "output_assets", ["product_id"])
    op.create_index("ix_output_assets_task_id", "output_assets", ["task_id"])
    op.create_index("ix_output_assets_enabled", "output_assets", ["enabled"])
    op.create_index("ix_output_assets_file_hash", "output_assets", ["file_hash"])


def downgrade() -> None:
    op.drop_index("ix_output_assets_file_hash", table_name="output_assets")
    op.drop_index("ix_output_assets_enabled", table_name="output_assets")
    op.drop_index("ix_output_assets_task_id", table_name="output_assets")
    op.drop_index("ix_output_assets_product_id", table_name="output_assets")
    op.drop_table("output_assets")
