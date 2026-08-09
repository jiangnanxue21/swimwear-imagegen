"""识别 run 异步执行、取消与可恢复投递所需列。

Revision ID: 0054
Revises: 0053
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "product_attribute_extractions"
_INDEX = "ix_attr_extractions_status_created"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("requested_media_ids", postgresql.JSONB(), nullable=True))
    op.add_column(
        _TABLE, sa.Column("requested_variant_ids", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "input_asset_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "image_outcomes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "failed_scopes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "requested_by",
            sa.String(length=128),
            nullable=False,
            server_default="system",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        _TABLE, sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        _TABLE, sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(_INDEX, _TABLE, ["status", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "finished_at")
    op.drop_column(_TABLE, "started_at")
    op.drop_column(_TABLE, "cancel_requested")
    op.drop_column(_TABLE, "requested_by")
    op.drop_column(_TABLE, "failed_scopes")
    op.drop_column(_TABLE, "image_outcomes")
    op.drop_column(_TABLE, "input_asset_ids")
    op.drop_column(_TABLE, "requested_variant_ids")
    op.drop_column(_TABLE, "requested_media_ids")
