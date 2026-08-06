"""批次回执生命周期、不可变导出文件、明细留存(评审第 5、6、17 条)。

三件事,都是"状态与事实对不上"的同一类问题:

    batch_action_receipts.status   回执原来只有\"存在/不存在\",而\"存在\"被
                                   硬编码成\"做成了\"。付费调用已发生但后处理
                                   失败时无处可记,普通重试会再付一次费
    batch_jobs.file_*              导出文件原来每次 GET 现场重建并累加导出
                                   次数。文件落盘一次之后,GET 只读字节
    batch_job_items.product_id     原来非空 + CASCADE,商品一删明细整行消失,
                                   而冗余 sku/spu 的注释写着\"删后仍可读\"

Revision ID: 0021
Revises: 0020
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from typing import Union

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

#: 明细表指向商品的外键名。0018 里显式命名过,这里必须逐字一致 ——
#: 名字对不上时 drop_constraint 会在升级中途炸,而那时表已经改了一半。
_ITEM_PRODUCT_FK = "fk_batch_job_items_product_id_products"


def upgrade() -> None:
    # ---------- 第 5 条:回执生命周期 ----------
    #
    # server_default 取 SUCCEEDED 而不是 IN_FLIGHT:老回执**只在成功时写**
    # (旧 `_execute` 里的 `if result.ok`),所以已有的每一行确实都是成功的。
    # 默认成 IN_FLIGHT 会把全部历史回执标成"费用不明",台账上凭空多出
    # 一批需要人工核对的行,而它们其实没有任何问题。
    op.add_column(
        "batch_action_receipts",
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="SUCCEEDED",
        ),
    )
    # 重试路径要按状态捞回执,单列索引足够(回执按 idempotency_key 唯一查,
    # 这个索引服务的是台账页"还有多少条卡在 IN_FLIGHT")
    op.create_index(
        "ix_batch_action_receipts_status", "batch_action_receipts", ["status"]
    )

    # ---------- 第 6 条:不可变导出文件 ----------
    op.add_column("batch_jobs", sa.Column("file_path", sa.String(255), nullable=True))
    op.add_column("batch_jobs", sa.Column("file_name", sa.String(160), nullable=True))
    op.add_column("batch_jobs", sa.Column("file_sha256", sa.String(64), nullable=True))
    op.add_column("batch_jobs", sa.Column("file_size", sa.Integer, nullable=True))
    op.add_column(
        "batch_jobs",
        sa.Column("file_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "batch_jobs", sa.Column("file_generated_by", sa.String(64), nullable=True)
    )

    # ---------- 第 17 条:商品删除后明细留存 ----------
    #
    # 顺序要紧:先放开 NOT NULL,再换外键。反过来的话,SET NULL 的外键
    # 与仍然 NOT NULL 的列同时存在,删商品会直接撞非空约束 ——
    # 一个只在真的删商品时才暴露的坏中间态。
    op.alter_column(
        "batch_job_items",
        "product_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.drop_constraint(_ITEM_PRODUCT_FK, "batch_job_items", type_="foreignkey")
    op.create_foreign_key(
        _ITEM_PRODUCT_FK,
        "batch_job_items",
        "products",
        ["product_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ---------- 第 7 条:驳回中间态 ----------
    _widen_rejection_status()


def downgrade() -> None:
    # ---------- 第 7 条回滚 ----------
    _narrow_rejection_status()

    # ---------- 第 17 条回滚 ----------
    #
    # 降级要恢复 NOT NULL,而此时库里可能已经有 product_id 为空的行
    # (商品删过)。那些行在旧结构下无法表达,只能删掉 —— 但要说清楚:
    # 删的是"商品已不存在的批次明细",不是任何还有商品的数据。
    op.execute("DELETE FROM batch_job_items WHERE product_id IS NULL")
    op.drop_constraint(_ITEM_PRODUCT_FK, "batch_job_items", type_="foreignkey")
    op.create_foreign_key(
        _ITEM_PRODUCT_FK,
        "batch_job_items",
        "products",
        ["product_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column(
        "batch_job_items",
        "product_id",
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    # ---------- 第 6 条回滚 ----------
    for column in (
        "file_generated_by",
        "file_generated_at",
        "file_size",
        "file_sha256",
        "file_name",
        "file_path",
    ):
        op.drop_column("batch_jobs", column)

    # ---------- 第 5 条回滚 ----------
    op.drop_index("ix_batch_action_receipts_status", table_name="batch_action_receipts")
    op.drop_column("batch_action_receipts", "status")


# ==========================================================
# 评审第 7 条:驳回中间态需要更宽的 status 列
# ==========================================================
#
# `FIXED_PENDING_EXPORT` 有 20 个字符,而 0019 建表时这一列是 String(16)。
# 少了这次加宽,新状态在 PostgreSQL 上会被直接拒写 —— 而在 SQLite 夹具上
# 不会,于是它是那种"测试全绿、上真库才炸"的改动。
def _widen_rejection_status() -> None:
    op.alter_column(
        "platform_rejections",
        "status",
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
        existing_server_default="OPEN",
    )


def _narrow_rejection_status() -> None:
    # 降级前先把中间态折回 OPEN:它在旧结构下既装不下、也没有语义。
    # 折回 OPEN 而不是 RESOLVED —— 那些记录确实还没解决。
    op.execute(
        "UPDATE platform_rejections SET status = 'OPEN' "
        "WHERE status = 'FIXED_PENDING_EXPORT'"
    )
    op.alter_column(
        "platform_rejections",
        "status",
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
        existing_server_default="OPEN",
    )
