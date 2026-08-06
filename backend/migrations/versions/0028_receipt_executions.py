"""回执加"真的开跑过几次"(A-17)。

## 为什么要这一列

"重复触发付费模型调用"原来判的是 `paid_calls > 1`。而评审第 4 条把属性识别
改成了**逐图计费** —— `_exec_extract` 里 `attempted = image_count`,成功与
失败两个返回分支都 `paid_calls=attempted`。于是一件 4 图商品**首次**执行完,
`paid_calls` 就是 4,台账把每一个多图商品都标成 §7.3 的那个 P0。

真正的重复付费淹在里面看不出来。信号被自身正确的计费语义噪声化了。

修法不是把计费改回"一件记一次"(那会让费用台账重新少算),而是承认这是
**两个问题**,各给一列:

    paid_calls    这条作用域一共花了多少次调用(逐图,4 图就是 4)
    executions    这条作用域被真的开跑过几次(`_claim_in_flight` 每认领 +1)

## server_default 为什么是 1 而不是 0

老回执**存在即至少跑过一次** —— 它是执行路径写下的,没有别的来源。默认成 0
会让全部历史行显示"付过钱但没跑过",台账上凭空多出一批需要人工核对、
而其实完全正常的行;默认成 1 则不改变任何一行的语义,也不会凭空造出异常
(异常判据是 `executions > 1`)。

这与 0021 给 `status` 选 `SUCCEEDED` 而不是 `IN_FLIGHT` 是同一条理由。

## 降级会丢什么

降级删掉这一列之后,异常判据自动退回 `paid_calls > 1` —— 也就是退回噪声态。
不丢数据,只丢信噪比。

Revision ID: 0028
Revises: 0027
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from typing import Union

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "batch_action_receipts",
        sa.Column(
            "executions",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("batch_action_receipts", "executions")
