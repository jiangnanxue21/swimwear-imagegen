"""Provider 调用流水加上费用五列(A23)。

`provider_usage_records` 从建表起就在文档字符串里写着「将来的成本核算」,
而表里一直没有金额。这条迁移补上:

    billable_units      计费单位数
    unit_price_micros   当时的单价快照(NULL = 没配价,不是免费)
    cost_micros         这次调用的金额
    currency            币种
    pricing_version     算这笔钱时用的价目表版本

## 为什么不回填历史行

历史流水没有单价可考 —— 当时既没配价目表,也没记下调用了几个计费单位。
回填只能靠"按今天的价目表 × 猜的用量",那会造出一批**看起来精确、
实际是编的**数字,而它们和厂商账单永远对不上。

所以历史行的 `unit_price_micros` 保持 NULL,`cost_micros` 为 0,
而看板会把「有调用但没有单价」的那部分单独列出来提示配价 ——
一个诚实的空缺比一个编造的数字有用。

`docs/DECISIONS.md` §1.2 写着「回填不占迁移号」,这条不回填,不冲突。

Revision ID: 0023
Revises: 0022
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_TABLE = "provider_usage_records"

#: server_default 是必须的,不是顺手加的:这张表已经有数据,
#: 加一个 NOT NULL 列而不给默认值,PostgreSQL 会直接拒绝这条迁移
_COLUMNS = (
    ("billable_units", sa.Integer(), False, "0"),
    ("unit_price_micros", sa.BigInteger(), True, None),
    ("cost_micros", sa.BigInteger(), False, "0"),
    ("currency", sa.String(8), False, "USD"),
    ("pricing_version", sa.String(32), True, None),
)


def upgrade() -> None:
    for name, type_, nullable, default in _COLUMNS:
        op.add_column(
            _TABLE,
            sa.Column(name, type_, nullable=nullable, server_default=default),
        )
    # 看板的主查询是「本月按 provider 汇总」。没有这条索引,它会随着流水
    # 增长变成全表扫描,而这一页是运营每天都会打开的
    op.create_index(
        "ix_provider_usage_records_created_provider",
        _TABLE,
        ["created_at", "provider"],
    )


def downgrade() -> None:
    op.drop_index("ix_provider_usage_records_created_provider", table_name=_TABLE)
    for name, _type, _nullable, _default in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
