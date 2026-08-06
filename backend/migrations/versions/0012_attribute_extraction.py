"""属性识别与证据

Revision ID: 0012
Revises: 0011
Create Date: 阶段 9-13 M3 —— 逐图识别、证据留痕、值版本化、置信度校准

## 编号说明

需求方案 §14.1 把这一批排在 0010,那个号被上一轮评审的阶段执行租约占了。
M0 迁移计划原本把 0012 留给「影子写回填」,但 M1 把回填做成了脚本
(理由:回填量按图算,而 Alembic 迁移跑在一个事务里,几十万行 UPDATE 会长时间持锁),
所以 0012 空出来给属性表。见 `docs/DECISIONS.md` §1.1–1.2。

## 四张表的分工

    product_attribute_extractions  一次识别请求
    attribute_evidence             一图一字段一条证据(模型说了什么)
    product_attribute_values       当前采信的值(我们决定信什么)
    attribute_calibrations         model_confidence -> 真实准确率

**证据和值必须分开。** 合并成一张的话,重新识别一次就会把人工确认过的值
冲掉 —— 而"人工值永不被自动覆盖"是 §6.2 的硬规则。

## 部分唯一索引

`uq_pav_current` 保证同一 (owner, field) 只有一个当前值。它同时写在 ORM 的
`__table_args__` 里 —— 上一轮评审第 20 条:只写在迁移里的话,
`create_all()` 建出来的测试库没有它,并发冲突在测试里永远复现不出来,
而生产环境会 500。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 时间戳列**逐张表写全**,不抽 helper。
#
# 原来的理由是"抽成 `*_timestamps()` 会把这两列藏进一次函数调用,
# `tests/pure/test_migration_consistency.py` 的 AST 比对就看不见它们"。
# **那个理由现在不成立了**:那条 AST 比对已删,换成
# `tests/test_migrations.py::test_upgrade_matches_orm_metadata` ——
# 它比对的是真的迁上去之后库里的列,抽不抽 helper 都看得见。
#
# 但写全这件事本身要留着,理由换成一条更硬的:新守卫带 `requires_db`,
# **没有 PostgreSQL 时整模块跳过**。也就是说本机 `make check-offline`
# 对"模型加了列但迁移没跟上"这件事是零覆盖的,而迁移恰恰是最不能靠
# "本地绿了"下结论的一类改动。展开写的重复,换的是一份不依赖运行环境
# 就能用眼睛读出来的对照。


def upgrade() -> None:
    # ---------------------------------------------------------- 识别请求
    op.create_table(
        "product_attribute_extractions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "product_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("extractor", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(120), nullable=True),
        sa.Column("prompt_version", sa.String(32), nullable=True),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        # 本次问了哪些字段。存下来而不是从证据反推:
        # "模型对某个字段一条证据都没给"和"我们根本没问它"是两件事
        sa.Column("target_fields", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("image_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        # 库里只存脱敏摘要 + 对象存储 key(§12.3)。全文进 JSONB 的话,
        # 一次识别几十 KB、一天几千次,几周就能把库撑爆
        sa.Column("raw_response_summary", postgresql.JSONB(), nullable=True),
        sa.Column("raw_response_ref", sa.String(512), nullable=True),
        # token 用量走独立列:它要被聚合成成本曲线,JSONB 里的数字没法 SUM
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
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
        "ix_attr_extractions_product",
        "product_attribute_extractions",
        ["product_id", "created_at"],
    )

    # ---------------------------------------------------------- 证据
    op.create_table(
        "attribute_evidence",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "extraction_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("product_attribute_extractions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "media_asset_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("media_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("raw_value_json", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_value", sa.String(128), nullable=True),
        sa.Column("model_confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("evidence_text", sa.Text(), nullable=True),
        sa.Column("region_json", postgresql.JSONB(), nullable=True),
        sa.Column("model_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(32), nullable=False, server_default=""),
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
        "ix_attr_evidence_field_media", "attribute_evidence", ["field_name", "media_asset_id"]
    )
    op.create_index("ix_attr_evidence_extraction", "attribute_evidence", ["extraction_id"])

    # ---------------------------------------------------------- 当前值
    op.create_table(
        "product_attribute_values",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_type", sa.String(16), nullable=False),
        # CHANNEL 层的 owner_id 是 channel_listing_id,所以是 TEXT 不是 UUID
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("value_json", postgresql.JSONB(), nullable=False),
        sa.Column("normalized_value", sa.String(128), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("model_confidence", sa.Numeric(4, 3), nullable=True),
        # 未校准时为 NULL。NULL 不是 0 —— 前者是"这个数字没有意义",
        # 后者是"置信度很低",两者的处理完全不同
        sa.Column("system_confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="CANDIDATE"),
        sa.Column("evidence_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("confidence_breakdown", postgresql.JSONB(), nullable=True),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        sa.Column("calibration_version", sa.String(32), nullable=True),
        sa.Column("confirmed_by", sa.String(64), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # 同一 (owner, field) 只能有一个当前值,历史版本全部保留
    op.create_index(
        "uq_pav_current",
        "product_attribute_values",
        ["owner_type", "owner_id", "field_name"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        "ix_pav_field_value",
        "product_attribute_values",
        ["field_name", "normalized_value"],
        postgresql_where=sa.text("is_current"),
    )

    # ---------------------------------------------------------- 校准
    op.create_table(
        "attribute_calibrations",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("model_name", sa.String(120), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("bin_lower", sa.Numeric(4, 3), nullable=False),
        sa.Column("bin_upper", sa.Numeric(4, 3), nullable=False),
        sa.Column("observed_accuracy", sa.Numeric(4, 3), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("calibration_version", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "field_name",
            "model_name",
            "prompt_version",
            "bin_lower",
            "calibration_version",
            name="uq_attr_calibration_bin",
        ),
    )


def downgrade() -> None:
    op.drop_table("attribute_calibrations")
    op.drop_index("ix_pav_field_value", table_name="product_attribute_values")
    op.drop_index("uq_pav_current", table_name="product_attribute_values")
    op.drop_table("product_attribute_values")
    op.drop_index("ix_attr_evidence_extraction", table_name="attribute_evidence")
    op.drop_index("ix_attr_evidence_field_media", table_name="attribute_evidence")
    op.drop_table("attribute_evidence")
    op.drop_index("ix_attr_extractions_product", table_name="product_attribute_extractions")
    op.drop_table("product_attribute_extractions")
