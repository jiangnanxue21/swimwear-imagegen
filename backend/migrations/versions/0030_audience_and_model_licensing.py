"""受众一等维度 + 模特授权字段组(PRD v2 阶段 1)。

## products.audience —— 可空,NULL = "待确认受众"

枚举里刻意没有 UNCONFIRMED 取值(§3.4 规则 3):UNISEX 是一个明确的业务
判断,"没填"必须和它区分开。**存量商品不回填**:它们全部进"待确认受众",
由运营逐件确认(§9.4)—— 按"库里只有泳装所以都是女装"回填,恰恰是
§9.1 明令禁止的"按类目猜一个默认值填进去"。存量商品在确认前仍走
受众前时代的规则包(category_code 无前缀),提交闸口的矩阵见
`app/workbench/audience_rules.py`。

## model_templates.audience —— NOT NULL,存量回填 WOMEN

与商品那一列取向相反,理由有二:

1. 模特本人总有受众,不存在"待确认"这个业务状态;可空只会让 §10.5 的
   硬过滤多一个"没填怎么算"的分支,而那个分支没有正确答案。
2. 存量模板全部来自女装泳衣校准期(本仓库到 a44 为止唯一开放的组合,
   见 CLAUDE.md 开头),回填 WOMEN 是在**记录事实**,不是猜测。
   如果某个存量模板实际是男性模特,管理员改一次字段即可 —— 而错误方向
   是安全的:被标成 WOMEN 的男性模特不会出现在男装商品的候选列表里,
   最坏结果是"少一个候选",不是"错配生成"。

## 授权字段组(§11)—— 回填取向:如实记录"未核实"

license_status=UNVERIFIED、age_verified=false、范围字段全空。
把存量标成 LICENSED / age_verified=true 等于替不存在的授权文件签字。
UNVERIFIED **不阻断**新任务(否则存量全停摆),EXPIRED / REVOKED /
过了 license_expires_at 才硬阻断 —— 判定只在
`model_template_service.assert_usable` 一处。

## 为什么是一次迁移而不是两次

两张表的新列服务同一条规则(§10.5 受众硬约束需要两边都有受众才判得了),
拆成两次会出现"商品有受众、模特还没有"的中间版本,那个版本里硬约束
恒放行 —— 半上线的保护比没上线更糟,因为它看起来在。
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    # ---- products.audience:可空,不回填(见模块文档) ----
    op.add_column("products", sa.Column("audience", sa.String(length=16), nullable=True))
    op.create_index("ix_products_audience", "products", ["audience"])

    # ---- model_templates:受众 + 授权字段组 ----
    op.add_column(
        "model_templates",
        sa.Column(
            "audience", sa.String(length=16), nullable=False, server_default="WOMEN"
        ),
    )
    op.create_index("ix_model_templates_audience", "model_templates", ["audience"])

    op.add_column(
        "model_templates",
        sa.Column("source", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column(
        "model_templates",
        sa.Column(
            "license_status",
            sa.String(length=16),
            nullable=False,
            server_default="UNVERIFIED",
        ),
    )
    op.add_column(
        "model_templates", sa.Column("commercial_scope", sa.Text(), nullable=True)
    )
    op.add_column(
        "model_templates",
        sa.Column(
            "allowed_platforms", JSONB(), nullable=False, server_default="[]"
        ),
    )
    op.add_column(
        "model_templates",
        sa.Column("allowed_regions", JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "model_templates", sa.Column("license_start_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "model_templates", sa.Column("license_expires_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "model_templates",
        sa.Column(
            "allow_ai_dressing", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.add_column(
        "model_templates",
        sa.Column(
            "allow_derivative_images",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "model_templates",
        sa.Column("age_verified", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "model_templates",
        sa.Column(
            "prohibited_categories", JSONB(), nullable=False, server_default="[]"
        ),
    )
    op.add_column(
        "model_templates", sa.Column("disabled_reason", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("model_templates", "disabled_reason")
    op.drop_column("model_templates", "prohibited_categories")
    op.drop_column("model_templates", "age_verified")
    op.drop_column("model_templates", "allow_derivative_images")
    op.drop_column("model_templates", "allow_ai_dressing")
    op.drop_column("model_templates", "license_expires_at")
    op.drop_column("model_templates", "license_start_at")
    op.drop_column("model_templates", "allowed_regions")
    op.drop_column("model_templates", "allowed_platforms")
    op.drop_column("model_templates", "commercial_scope")
    op.drop_column("model_templates", "license_status")
    op.drop_column("model_templates", "source")
    op.drop_index("ix_model_templates_audience", table_name="model_templates")
    op.drop_column("model_templates", "audience")
    op.drop_index("ix_products_audience", table_name="products")
    op.drop_column("products", "audience")
