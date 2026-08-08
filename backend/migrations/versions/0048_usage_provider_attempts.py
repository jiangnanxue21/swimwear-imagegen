"""费用流水记下"这一行代表几次真实网络请求"(A45-batch18 / P1-2)。

## 缺这一列的时候账是怎么错的

`llm/transport.MultimodalClient.send()` 对超时、429、5xx 自动重试,
`EXTRACTOR_MODEL_MAX_RETRIES=2` 意味着**一次业务识别最多发出 3 个请求**。
而 `attributes/service._record_extraction_usage()` 在最终成功或失败之后
才记一条流水,并且固定写 `billable_units=1`。于是两个方向同时错:

    供应商已受理、客户端超时后重发      可能计费 2～3 次,台账记 1
    Schema 构造 / 图片准备阶段失败      一个请求都没发出去,台账仍记 1

第一种少记已经花掉的钱,第二种记了一笔从来没花过的钱。叠在一起之后,
`/spend` 与供应商账单再也无法逐条核对 —— 而 §10.2 第 5 条准入要的正是
"用量记录与 Provider 后台账单条数一致"。

## 为什么不并进 `billable_units`

`billable_units` 回答"记几个计费单位",这一列回答"对应供应商后台的
几条调用记录"。两者在识别链路上恰好相等(一次往返 = 一个单位),
在生成链路上不等:FASHN 一次 POST 可能出多张图并按额度计价,
`x-fashn-credits-used` 报的是额度不是请求数。

合成一列的话,对账的人拿它去比调用条数,会在一半的链路上得到一个
必然对不上的数 —— 而**对不上的原因是口径不是漏账**,那是最浪费时间的
一种红,也是最容易让人把整张表判成不可信的一种。

## 为什么可空,且不给 server_default

NULL = 这一类调用还没接上次数上报(评分、轮询、取结果今天都是 NULL)。
0 = **确认**一个请求都没发出去。给 `server_default="1"` 等于宣称
"每一条存量流水都恰好发过一次请求",而那正是这次要修的那个假设 ——
存量行里既有重试过的,也有 preflight 就失败的,它们的真实次数
今天已经查不回来了。留 NULL,让"查不回来"如实显示成"不知道"。

## 索引

不加。这一列的读者是对账的人和 `/spend` 页的明细,查询一律先按
`created_at` / `provider` 过滤(那两个索引已经有了),这一列只出现在
投影里,不出现在 WHERE 中。

Revision ID: 0048
Revises: 0047
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "provider_usage_records"
_COLUMN = "provider_attempts"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
