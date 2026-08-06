"""刊登图片集

Revision ID: 0013
Revises: 0012
Create Date: 阶段 9-13 M5 —— 主图、排序、颜色绑定、版本与回滚

## 两个 COALESCE 索引

`listing_image_sets` 的 `channel` / `site` 与 `listing_image_items` 的
`variant_id` 都可空,而 PostgreSQL 的唯一约束**把 NULL 当成互不相等**。

于是普通的 `UNIQUE(spu, channel, site, version)` 挡不住两条
`channel=NULL, site=NULL, version=1` 的记录;
`UNIQUE(image_set_id, variant_id) WHERE is_primary` 同样挡不住
任意多张 `variant_id=NULL` 的主图。

这类错误的表现是平台那边显示了错的主图,而库里看起来一切正常。
所以两处都用 `COALESCE(..., '')` 的表达式索引兜底。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "listing_image_sets",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("spu", sa.String(64), nullable=False),
        # NULL = 平台无关的默认集 / 该渠道全站点通用
        sa.Column("channel", sa.String(32), nullable=True),
        sa.Column("site", sa.String(32), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column(
            "derived_from",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("listing_image_sets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "spu", "channel", "site", "version", name="uq_listing_image_sets_version"
        ),
    )
    op.create_index(
        "ix_listing_image_sets_spu_status", "listing_image_sets", ["spu", "status"]
    )
    # 上面那条 UniqueConstraint 挡不住 channel/site 同为 NULL 的重复 ——
    # PostgreSQL 把 NULL 当成互不相等。表达式索引才是真正的守卫
    op.create_index(
        "uq_listing_image_sets_version_coalesced",
        "listing_image_sets",
        ["spu", sa.text("COALESCE(channel, '')"), sa.text("COALESCE(site, '')"), "version"],
        unique=True,
    )

    op.create_table(
        "listing_image_items",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "image_set_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("listing_image_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # RESTRICT 而不是 CASCADE:素材被删时不该悄悄把刊登编排也删掉。
        # 真要删素材,得先把它从图片集里拿掉 —— 那是一个应该被看见的动作
        sa.Column(
            "media_asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("media_assets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("variant_id", sa.String(64), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("derivative_purpose", sa.String(32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "image_set_id", "sort_order", name="uq_listing_image_items_order"
        ),
        sa.UniqueConstraint(
            "image_set_id",
            "media_asset_id",
            "variant_id",
            name="uq_listing_image_items_asset_variant",
        ),
    )
    # 每个变体只有一张启用中的主图。COALESCE 同上:不套的话
    # 可以插进任意多张 variant_id 为 NULL 的主图
    op.create_index(
        "uq_image_primary",
        "listing_image_items",
        ["image_set_id", sa.text("COALESCE(variant_id, '')")],
        unique=True,
        postgresql_where=sa.text("is_primary AND enabled"),
    )


def downgrade() -> None:
    op.drop_index("uq_image_primary", table_name="listing_image_items")
    op.drop_table("listing_image_items")
    op.drop_index(
        "uq_listing_image_sets_version_coalesced", table_name="listing_image_sets"
    )
    op.drop_index("ix_listing_image_sets_spu_status", table_name="listing_image_sets")
    op.drop_table("listing_image_sets")
