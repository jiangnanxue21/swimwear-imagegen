"""发布领域三张表(方案 v4.1 第 7.2 节 / 任务 11)。

新建:
    channel_listings    当前事实 —— 商品在某渠道/店铺/站点的状态
    publish_attempts    历史记录 —— 每一次提交尝试的结局,写完不改
    publish_outbox      调度状态 —— 什么时候发、谁领走了、还剩几次

建表顺序不能反:outbox 外键指向 attempts,attempts 外键指向 listings。
downgrade 里反过来,否则 PostgreSQL 会因为外键依赖拒绝 drop ——
`test_migrations.py::test_downgrade_removes_every_table` 会当场发现,
但那要等到有真库的环境才跑得到,所以这里先写对。

## 为什么唯一约束不叫 alembic 自动生成的名字

`db/base.py` 定了命名约定(`uq_%(table_name)s_%(column_0_name)s`),
四列联合唯一时自动名只取第一列 —— `uq_channel_listings_channel`,
读起来像是「channel 单列唯一」。这里显式给名字,和 ORM 的
`__table_args__` 一字不差,否则 autogenerate 下次会认为约束变了、
生成一条毫无内容的迁移。

Revision ID: 0022
Revises: 0021
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "channel_listings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("site", sa.String(16), nullable=False),
        sa.Column("shop_id", sa.String(64), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_spu_id", sa.String(128), nullable=True),
        sa.Column("external_sku_ids", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="DRAFT"),
        sa.Column("last_platform_status", sa.String(64), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_channel_listings"),
        # RESTRICT:平台上还挂着商品时,不允许本地把来源商品/草稿删掉。
        # CASCADE 在这里等于「删一行本地数据,平台上多一个没人认领的商品」
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"],
            name="fk_channel_listings_product_id_products", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"], ["listing_drafts.id"],
            name="fk_channel_listings_draft_id_listing_drafts", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "channel", "shop_id", "site", "product_id",
            name="uq_channel_listings_channel_shop_site_product",
        ),
    )
    op.create_index(
        "ix_channel_listings_status_polled", "channel_listings", ["status", "last_polled_at"]
    )
    op.create_index("ix_channel_listings_product", "channel_listings", ["product_id"])

    op.create_table(
        "publish_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_listing_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column("safe_request_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("safe_response_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_publish_attempts"),
        # CASCADE:listing 没了,它的尝试历史没有独立意义。
        # 和上面的 RESTRICT 不矛盾 —— listing 本身就删不掉
        sa.ForeignKeyConstraint(
            ["channel_listing_id"], ["channel_listings.id"],
            name="fk_publish_attempts_channel_listing_id_channel_listings",
            ondelete="CASCADE",
        ),
        # 「重试不会重复创建」(4.1 节 D)最终落在这一条上。
        # 应用层的先查后写在并发下挡不住,这条挡得住
        sa.UniqueConstraint("idempotency_key", name="uq_publish_attempts_idempotency_key"),
    )
    op.create_index(
        "ix_publish_attempts_listing_started",
        "publish_attempts", ["channel_listing_id", "started_at"],
    )
    op.create_index("ix_publish_attempts_status", "publish_attempts", ["status"])

    op.create_table(
        "publish_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("publish_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_publish_outbox"),
        sa.ForeignKeyConstraint(
            ["publish_attempt_id"], ["publish_attempts.id"],
            name="fk_publish_outbox_publish_attempt_id_publish_attempts",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "publish_attempt_id", name="uq_publish_outbox_publish_attempt_id"
        ),
    )
    op.create_index(
        "ix_publish_outbox_due",
        "publish_outbox", ["status", "next_attempt_at", "lease_until"],
    )


def downgrade() -> None:
    # 顺序与 upgrade 相反:先删被指向者最少的那张。
    # 索引跟着表一起走,显式 drop_index 是多余的 —— 但 drop_table 之后
    # 再 drop_index 会报「index 不存在」,那正是「迁移只写不删」这类
    # 问题的镜像版本,所以这里一条都不写
    op.drop_table("publish_outbox")
    op.drop_table("publish_attempts")
    op.drop_table("channel_listings")
