"""后台设置覆盖表:app_settings

Revision ID: 0005
Revises: 0004
Create Date: 阶段 8

同前几版:列定义一律展开写,不依赖可变的辅助函数。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", UUID, primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("is_secret", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("value_hint", sa.String(length=32), server_default=sa.text("''"),
                  nullable=False),
        sa.Column("updated_by", sa.String(length=64), server_default=sa.text("'system'"),
                  nullable=False),
        sa.UniqueConstraint("key", name="uq_app_settings_key"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
