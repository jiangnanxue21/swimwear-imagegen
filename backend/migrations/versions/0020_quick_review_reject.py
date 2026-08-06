"""快审退回(A10)。

新增(两张表各四列,形状一致):
    listing_image_sets.reject_reason / reject_note / rejected_by / rejected_at
    listing_copies.reject_reason     / reject_note / rejected_by / rejected_at

不新建表:退回不是一条独立的业务实体,它是"这一版的当前结论"。
建一张 `listing_rejections` 的话,"这一版现在是不是被退回状态"就要靠
子查询取最新一条来推,而判定层每次列表渲染都要问这个问题(50 件商品 50 次)。
历史需求由审计日志承担 —— `audit.record` 每次退回都落一条,带原因和说明,
`AuditLog` 本来就是这个仓库回答"谁在什么时候做了什么"的地方。

`status` 列不动:`ImageSetStatus.REJECTED` 是新加的枚举值,而两张表的
status 都是 `String(16)` 不是数据库枚举(理由见 `core/enums.py` 那两条注释),
加一个取值不触发迁移。

Revision ID: 0020
Revises: 0019
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from typing import Union

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

#: 两张表加的是同一组列。写成数据而不是两段复制粘贴的 add_column ——
#: 复制粘贴的那一版在评审里被改过一次:图片集加了四列、文案漏了一列,
#: 而漏掉的那一列只在文案退回时才用到,测试跑不到那条路径
_TABLES = ("listing_image_sets", "listing_copies")

_COLUMNS = (
    ("reject_reason", sa.String(32)),
    ("reject_note", sa.Text),
    ("rejected_by", sa.String(64)),
    ("rejected_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for table in _TABLES:
        for name, type_ in _COLUMNS:
            # 全部 nullable:存量行没有被退回过,而 "没被退回过" 的正确表达
            # 是 NULL,不是空字符串 —— 空字符串会在 `reject_reason` 有没有值
            # 这个判据上撒谎(见 flow._evaluate_copy)
            op.add_column(table, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for table in reversed(_TABLES):
        for name, _ in reversed(_COLUMNS):
            op.drop_column(table, name)
