"""评审修复:external_task_id 放宽、素材去重键含角色、成品图按商品用途唯一

Revision ID: 0006
Revises: 0005
Create Date: 阶段 8 代码评审

三处都属于「写库那一刻才失败」或「静默写出错误数据」的问题,合并在一版里:

1. ``external_task_id`` VARCHAR(128) -> TEXT。FASHN 的 product-to-model 一次出一张,
   凑 4 张要拼 4 个 UUID(约 147 字符)。请求已经发出去了才写库失败,钱花了 ID 丢了。
2. ``product_assets`` 去重键补上 ``asset_type``。同一张图换个角色再传,以前会直接返回
   旧记录且不按新角色重新校验 —— 先当抠图传失败,再当正面图传,拿回来的还是那条抠图。
3. ``output_assets`` 增加 (product_id, purpose) 的部分唯一索引。以前只按候选图约束,
   商品跑了新任务之后旧成品图仍然 enabled,同一用途会有多条同时生效。

第 3 条在建索引前先把历史数据里同用途的旧记录停用,只保留最新的一条,
否则索引建不上去。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 复合任务 ID 放得下
    op.alter_column(
        "generation_tasks", "external_task_id",
        existing_type=sa.String(length=128), type_=sa.Text(), existing_nullable=True,
    )
    op.alter_column(
        "generation_attempts", "external_task_id",
        existing_type=sa.String(length=128), type_=sa.Text(), existing_nullable=True,
    )

    # 2) 素材去重键补上角色
    op.drop_constraint(
        "uq_product_assets_product_id_file_hash", "product_assets", type_="unique"
    )
    op.create_unique_constraint(
        "uq_product_assets_product_id_file_hash",
        "product_assets",
        ["product_id", "file_hash", "asset_type"],
    )

    # 3) 同商品同用途只允许一条启用中的成品图。
    #    先把历史数据里同用途的旧记录停用,只留最新一条,否则唯一索引建不上去。
    op.execute(
        """
        UPDATE output_assets AS o
           SET enabled = false
         WHERE o.enabled
           AND o.id <> (
                SELECT inner_o.id
                  FROM output_assets AS inner_o
                 WHERE inner_o.product_id = o.product_id
                   AND inner_o.purpose = o.purpose
                   AND inner_o.enabled
                 ORDER BY inner_o.generation DESC, inner_o.created_at DESC
                 LIMIT 1
           )
        """
    )
    op.create_index(
        "uq_output_assets_product_purpose_enabled",
        "output_assets",
        ["product_id", "purpose"],
        unique=True,
        postgresql_where=sa.text("enabled"),
    )


def downgrade() -> None:
    op.drop_index("uq_output_assets_product_purpose_enabled", table_name="output_assets")

    op.drop_constraint(
        "uq_product_assets_product_id_file_hash", "product_assets", type_="unique"
    )
    op.create_unique_constraint(
        "uq_product_assets_product_id_file_hash", "product_assets", ["product_id", "file_hash"]
    )

    op.alter_column(
        "generation_attempts", "external_task_id",
        existing_type=sa.Text(), type_=sa.String(length=128), existing_nullable=True,
    )
    op.alter_column(
        "generation_tasks", "external_task_id",
        existing_type=sa.Text(), type_=sa.String(length=128), existing_nullable=True,
    )
