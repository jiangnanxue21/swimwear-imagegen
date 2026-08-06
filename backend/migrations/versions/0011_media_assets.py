"""统一素材层

Revision ID: 0011
Revises: 0010
Create Date: 阶段 9-13 M1 —— 把任何来源的图片收敛成 MediaAsset

## 编号说明

需求方案 §14.1 把这一批排在 0008。那个号被 `task_dispatch_outbox` 占了,
0009 是 `evaluation_attempts`,0010 是上一轮代码评审第 4 条的阶段执行租约。
全部顺延,见 `docs/DECISIONS.md` §1.1(编号表)。

## 这一步做什么

建三张新表 + 给 `generation_candidates` 加一列。**读路径一个字都不改。**

这是三步走的第一步(迁移 A · 影子写):新表建好、写入路径同事务追加写,
历史数据由 `app/scripts/backfill_media_assets.py` 后台回填。
读仍然走旧表,所以这一步**没有任何行为变化** —— 回滚只需停止影子写。

## 为什么 `generation_candidates` 是加列不是建表

需求方案 §4.2 写的是 CREATE TABLE。但这张表 0002 就建好了,而且当前直接
持有 storage_path / file_hash / width / height。要的是让它退化成
「任务 ↔ 素材」的关联,那是一次带数据迁移的重构。

**旧列在这一步一个都不删。** 双读对账需要新旧两边都在;删列是迁移 C
之后的独立清理任务。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------------------------------------------------------- 授权
    # 先建它:media_assets.consent_id 要引用
    op.create_table(
        "media_consents",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_ref", sa.String(255), nullable=True),
        sa.Column(
            "scope", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "allowed_regions", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column(
            "allowed_processors", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        # 默认禁止训练。保守的那一边才是安全的那一边
        sa.Column(
            "training_opt_out", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("document_ref", sa.String(512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # ---------------------------------------------------------- 素材
    op.create_table(
        "media_assets",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "product_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("spu", sa.String(64), nullable=True),
        # source 与 role 正交:两列,不是一列
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("role", sa.String(24), nullable=True),
        sa.Column(
            "role_source", sa.String(8), nullable=False, server_default="UNSET"
        ),
        sa.Column("role_confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("variant_hint", sa.String(64), nullable=True),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("quality_json", postgresql.JSONB(), nullable=True),
        # 合规与数据治理
        sa.Column("processor", sa.String(64), nullable=True),
        sa.Column("processing_region", sa.String(16), nullable=True),
        sa.Column(
            "training_opt_out", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column(
            "consent_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("media_consents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("retention_policy", sa.String(32), nullable=True),
        sa.Column("quarantine_reason", sa.Text(), nullable=True),
        # 迁移期溯源。收口前不要删 —— 它们是唯一能把新旧两边对上的东西
        sa.Column("legacy_kind", sa.String(24), nullable=True),
        sa.Column("legacy_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        # 同商品同图去重。跨商品**不**去重:角色、变体绑定、授权、隔离状态
        # 都是按商品算的,物理层的内容寻址已经保证只存一份字节
        sa.UniqueConstraint("product_id", "sha256", name="uq_media_assets_product_sha256"),
    )
    op.create_index("ix_media_assets_product_status", "media_assets", ["product_id", "status"])
    op.create_index("ix_media_assets_spu_role", "media_assets", ["spu", "role"])
    op.create_index("ix_media_assets_sha256", "media_assets", ["sha256"])
    # 回填与对账按 (legacy_kind, legacy_id) 查,没有索引会全表扫
    op.create_index(
        "ix_media_assets_legacy", "media_assets", ["legacy_kind", "legacy_id"]
    )

    # ---------------------------------------------------------- 派生版本
    op.create_table(
        "media_derivatives",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_media_asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("media_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("transform_version", sa.String(32), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "source_media_asset_id",
            "purpose",
            "transform_version",
            name="uq_media_derivatives_source_purpose_version",
        ),
    )
    op.create_index(
        "ix_media_derivatives_source", "media_derivatives", ["source_media_asset_id"]
    )

    # ---------------------------------------------------------- 对账
    op.create_table(
        "media_migration_mismatches",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("legacy_kind", sa.String(24), nullable=False),
        sa.Column("legacy_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("media_asset_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("legacy_value", sa.Text(), nullable=True),
        sa.Column("media_value", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
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
        "ix_media_mismatch_kind_created",
        "media_migration_mismatches",
        ["legacy_kind", "created_at"],
    )

    # ---------------------------------------------------------- 候选表加列
    # 可空。影子写上线之前的历史候选没有对应素材,回填跑完才会补上;
    # 现在就置非空会让任何一条漏网的历史数据把整张表锁死
    op.add_column(
        "generation_candidates",
        sa.Column("media_asset_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_generation_candidates_media_asset_id_media_assets",
        "generation_candidates",
        "media_assets",
        ["media_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_generation_candidates_media_asset_id_media_assets",
        "generation_candidates",
        type_="foreignkey",
    )
    op.drop_column("generation_candidates", "media_asset_id")
    op.drop_index("ix_media_mismatch_kind_created", table_name="media_migration_mismatches")
    op.drop_table("media_migration_mismatches")
    op.drop_index("ix_media_derivatives_source", table_name="media_derivatives")
    op.drop_table("media_derivatives")
    op.drop_index("ix_media_assets_legacy", table_name="media_assets")
    op.drop_index("ix_media_assets_sha256", table_name="media_assets")
    op.drop_index("ix_media_assets_spu_role", table_name="media_assets")
    op.drop_index("ix_media_assets_product_status", table_name="media_assets")
    op.drop_table("media_assets")
    op.drop_table("media_consents")
