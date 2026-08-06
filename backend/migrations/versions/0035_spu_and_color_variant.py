"""身份规范化:spus / color_variants 两张新表 + products 增列(A45-batch13,PRD v3.1 §4.2~4.4)。

## 这一版**只增不改**

`products` 上的 `spu` / `variant_key` / `audience` 三列一列没动,新外键**可空**。
理由不是留后路,是顺序:老建档路径(`product_service.create_product`、CSV 导入)
还不知道这两张表,把外键立刻设成 NOT NULL 会让它们在 flush 阶段整体失败。

PRD §3.1 允许一次性切换(系统尚未正式使用,没有存量数据),但"允许"的是
**数据**层面;代码层面十来个域按 `products.spu` 取数、约 1800 条纯测试断言现有形状,
那是 §3.1 自己点名的"成本不在数据,在代码与测试"。所以切换分两批,
第二批的前提写在 `docs/STATUS.md` 阶段 1。

## 为什么 `products` 的两个外键是 RESTRICT 而不是 CASCADE

`color_variants -> spus` 用 CASCADE:颜色是款的一部分,款没了颜色没有意义。

`products -> color_variants` 用 RESTRICT:一行 SKU 上挂着素材、生成任务、
候选图、图片集、文案、草稿、发布记录。CASCADE 的意思是"删一个颜色,顺手把
它下面九行 SKU 连同上面那七个域的东西一起删掉",而**发出这个删除动作的人
在界面上看到的只是一个颜色**。RESTRICT 让数据库先拦住,由业务层去回答
"这些东西怎么办"。

## `uq_products_variant_size` 对 NULL 不生效 —— 这是已知且刻意的

PostgreSQL 里 NULL 彼此不相等,所以尺码为空的行可以在同一个颜色下重复任意多次。
不改成部分唯一索引 + COALESCE 的原因是:那样会给"尺码留空"一个**看起来正常**的
语义,而正确的做法是根本不写 NULL 尺码 —— 均码走模板 `ONE_SIZE`,落库是 "OS"。
存量行(batch13 之前建的)`color_variant_id` 为 NULL,压根进不了这条约束。

Revision ID: 0035
Revises: 0034
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "spus",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("spu_code", sa.String(length=32), nullable=False),
        sa.Column("internal_name", sa.String(length=255), nullable=False),
        # 受众 NOT NULL 且**没有 server_default**。给它一个默认值等于给"没填"
        # 一个合法受众,而受众填错的代价是整条链路(模特、槽位、检查项、尺码表、
        # 平台类目)全部走错,且每一步单看都是"正常完成"(§4.2)
        sa.Column("audience", sa.String(length=16), nullable=False),
        sa.Column(
            "base_category",
            sa.String(length=64),
            nullable=False,
            server_default="swimwear",
        ),
        sa.Column("supplier_ref", sa.String(length=128), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="DRAFT"
        ),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint("uq_spus_spu_code", "spus", ["spu_code"])
    op.create_index("ix_spus_status", "spus", ["status"])
    op.create_index("ix_spus_audience", "spus", ["audience"])

    op.create_table(
        "color_variants",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("spu_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("variant_code", sa.String(length=16), nullable=False),
        sa.Column(
            "working_name", sa.String(length=128), nullable=False, server_default=""
        ),
        sa.Column(
            "display_name", sa.String(length=128), nullable=False, server_default=""
        ),
        sa.Column("supplier_color_code", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "sellable_status",
            sa.String(length=16),
            nullable=False,
            server_default="PLANNED",
        ),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["spu_id"],
            ["spus.id"],
            name="fk_color_variants_spu_id_spus",
            ondelete="CASCADE",
        ),
    )
    op.create_unique_constraint(
        "uq_color_variants_spu_code", "color_variants", ["spu_id", "variant_code"]
    )
    op.create_index("ix_color_variants_spu", "color_variants", ["spu_id"])
    op.create_index(
        "ix_color_variants_sellable_status", "color_variants", ["sellable_status"]
    )

    op.add_column(
        "products", sa.Column("spu_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "products",
        sa.Column("color_variant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("products", sa.Column("barcode", sa.String(length=64), nullable=True))
    op.add_column("products", sa.Column("price", sa.Numeric(12, 2), nullable=True))
    op.add_column("products", sa.Column("cost", sa.Numeric(12, 2), nullable=True))
    op.add_column("products", sa.Column("inventory", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_products_spu_id_spus",
        "products",
        "spus",
        ["spu_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_products_color_variant_id_color_variants",
        "products",
        "color_variants",
        ["color_variant_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_products_variant_size", "products", ["color_variant_id", "size"]
    )
    op.create_index("ix_products_spu_id", "products", ["spu_id"])


def downgrade() -> None:
    # 顺序与 upgrade 严格相反。products 上的东西先撤,否则外键指着的表删不掉 ——
    # 这在只跑过 upgrade 的机器上不会暴露,而 P0-2 要求的正是
    # `0001 → head → 回退 → head` 走一遍
    op.drop_index("ix_products_spu_id", table_name="products")
    op.drop_constraint("uq_products_variant_size", "products", type_="unique")
    op.drop_constraint(
        "fk_products_color_variant_id_color_variants", "products", type_="foreignkey"
    )
    op.drop_constraint("fk_products_spu_id_spus", "products", type_="foreignkey")
    op.drop_column("products", "inventory")
    op.drop_column("products", "cost")
    op.drop_column("products", "price")
    op.drop_column("products", "barcode")
    op.drop_column("products", "color_variant_id")
    op.drop_column("products", "spu_id")

    op.drop_index("ix_color_variants_sellable_status", table_name="color_variants")
    op.drop_index("ix_color_variants_spu", table_name="color_variants")
    op.drop_table("color_variants")

    op.drop_index("ix_spus_audience", table_name="spus")
    op.drop_index("ix_spus_status", table_name="spus")
    op.drop_table("spus")
