"""刊登草稿与 SKU 维度

Revision ID: 0015
Revises: 0014
Create Date: 阶段 9-13 M6 —— 持久化草稿 + products 增列 size / size_group

## 这是唯一需要维护窗口的一次迁移

`products` 加两列。加列本身是秒级的(可空、无默认值),但**回滚会丢数据**:
downgrade 会 drop 掉已经录入的 size。所以上线前确认没有业务在写新列,
或者接受回滚时丢掉它们。

其余表都是新建,downgrade 干净。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "listing_drafts",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("spu", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("site", sa.String(32), nullable=False),
        sa.Column("category_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="BUILDING"),
        sa.Column("canonical_snapshot", postgresql.JSONB(), nullable=False, server_default="{}"),
        # RESTRICT:图片集被删时不该悄悄把草稿也删掉
        sa.Column(
            "image_set_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("listing_image_sets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("copy_version_ids", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("manual_payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("mapped_payload", postgresql.JSONB(), nullable=True),
        sa.Column("validation_errors", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("validation_warnings", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("mapping_version", sa.String(32), nullable=False, server_default="0"),
        sa.Column("spec_version", sa.String(32), nullable=False, server_default="0"),
        sa.Column("template_checksum", sa.String(64), nullable=True),
        # 上游任一变化 -> 指纹变化 -> STALE -> 不允许导出与提交
        sa.Column("source_fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_listing_drafts_lookup", "listing_drafts", ["spu", "channel", "site", "status"]
    )

    # SPU-SKU 展开需要尺码维度(§9.5)。可空:存量商品没有这两个值,
    # 置非空会让整张表锁死
    op.add_column("products", sa.Column("size", sa.String(32), nullable=True))
    op.add_column("products", sa.Column("size_group", sa.String(32), nullable=True))


def downgrade() -> None:
    # 注意:这会丢掉已经录入的 size / size_group
    op.drop_column("products", "size_group")
    op.drop_column("products", "size")
    op.drop_index("ix_listing_drafts_lookup", table_name="listing_drafts")
    op.drop_table("listing_drafts")
