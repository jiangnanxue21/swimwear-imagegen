"""回执加「付费调用发出的时刻」(A45-batch12-2 / EX-02)。

## 为什么要这一列

`ReceiptStatus.IN_FLIGHT` 的语义是「开始执行了,结果未知」。但**未知**里
压着两件处理方式完全相反的事:

    崩在付费调用之前   请求没发出去,钱没花 -> 重跑免费且正确
    崩在付费调用之后   对方可能已受理计费 -> 重跑就是重复计费

分不出来的时候只能折中。0028 之后的折中是 `MAX_BILLED_EXECUTIONS = 2`:
允许**一次**自动付费重跑,理由是取 1 会让每一次 pod 驱逐都要人工介入。
那个理由本身没错,错在前提 —— 它默认了区分不出来。

这一列把区分变成事实:由 `batch_service._mark_provider_call()` 在一条
**独立事务**里、紧挨着付费调用之前写入。独立事务与 `_claim_in_flight`
同理:业务事务会随崩溃一起回滚,而这个标记必须在崩溃之后仍然读得到。

于是 `receipt_route` 对 IN_FLIGHT 的分派变成:

    provider_call_at 为空    EXECUTE            —— 可证明没花钱
    provider_call_at 有值    NEEDS_RECONCILIATION —— 停下,等人对账

**自动重复付费从此为 0。**

## 为什么可空,以及存量行怎么算

0031 之前的行没有这一列。它们按「可能已发出」处理(见 `receipt_route`
的 `call_dispatched` 默认值)—— 方向与 0021 给 `status` 选 `SUCCEEDED`、
0028 给 `executions` 选 1 是同一条:默认值不许凭空造出一个更乐观的结论。

这里比那两处更保守,理由是代价不对称:把一条其实没花钱的回执judged 成
「要对账」,代价是运营核对一次;反过来则是一笔谁也发现不了的重复扣费。
而且 IN_FLIGHT 本来就是瞬时状态,**留在库里的存量 IN_FLIGHT 行恰恰就是
有疑问的那一批**,把它们送去对账不是误伤,是补上一直缺的那一步。

不给 server_default:NULL 就是「不知道」,而 `now()` 之类的默认值会给
每一条存量行编造一个假的调用时刻,那比没有更糟 —— 运营会照着它去查。

## 降级会丢什么

降级删掉这一列之后,`receipt_route` 拿不到 `call_dispatched`,IN_FLIGHT
退回「允许一次自动付费重跑」。不丢业务数据,丢的是那一次重跑的拦截。

Revision ID: 0031
Revises: 0030
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from typing import Union

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "batch_action_receipts",
        sa.Column(
            "provider_call_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("batch_action_receipts", "provider_call_at")
