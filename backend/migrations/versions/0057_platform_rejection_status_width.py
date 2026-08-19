"""`platform_rejections.status`:String(16) -> String(32)。

## 这一列在库里装不下代码要写的值

`RejectionStatus.FIXED_PENDING_EXPORT` 是 **20 个字符**,而 0019 建这一列时给的是
`sa.String(16)`。`platform_service.py:539` 那一句
`rejection.status = RejectionStatus.FIXED_PENDING_EXPORT.value` 在真实库上会被
PostgreSQL 直接拒掉 —— 运营点「我已经改好了」,拿到的是一个 500。

**ORM 侧早就改过了。** `models/platform_rejection.py` 那一列的注释原样写着:

    原来的 String(16) 装不下,写入会被 PostgreSQL 直接拒掉。
    32 留了余量:下一个状态名不该再触发一次迁移。

也就是说**有人发现过这件事、改了 ORM、没有写迁移**。而 `create_all()` 建出来的测试库
按 ORM 走(32,够用),真实库按迁移走(16,不够)——「测试库和生产库的结构不一致,
比没有测试更危险:它让人以为测过了」(`models/prompt_template.py` 顶部记着这句话的
上一次)。这一次是同一个坑的镜像:那次约束只写在迁移里,这次宽度只改在 ORM 里。

## 怎么发现的

a65 的 `audit_migration_chain.py` 只比列名集合,a66 把它扩到**列类型**:554 列两侧都
解析得出类型,不一致的只有这一处。**它是这条门禁扩容当天抓到的第一条真缺陷。**

Revision ID: 0057
Revises: 0056
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "platform_rejections"
_COLUMN = "status"


def upgrade() -> None:
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(16),
        type_=sa.String(32),
        existing_nullable=False,
        existing_server_default="OPEN",
    )


def downgrade() -> None:
    # **有损,而且是会丢业务事实的那一种。** 回退之后 `FIXED_PENDING_EXPORT`
    # (20 字符)再也写不进去,而库里已有的那些行会在收缩列宽时被 PostgreSQL
    # 直接拒绝 —— 不是截断,是整条迁移失败。
    #
    # 所以这里**先把超长的值归到 OPEN**:那是语义上最保守的落点(未解决),
    # 而不是 RESOLVED(会让一条还没关的驳回从清单里消失)。要保住那些行的
    # 真实状态,回退前先导出。
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET {_COLUMN} = 'OPEN' WHERE char_length({_COLUMN}) > 16"
        )
    )
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(32),
        type_=sa.String(16),
        existing_nullable=False,
        existing_server_default="OPEN",
    )
