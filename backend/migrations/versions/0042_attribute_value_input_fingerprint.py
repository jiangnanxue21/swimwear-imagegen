"""事实侧的作用域指纹列(A45-batch14-21,§4.5 / §5.3 / §6.3 完成条件)。

## 这一列挡着什么

`scope_fingerprint.py` 从 batch14-9 起就写好并穷举验过,`fingerprints` /
`views` 在 batch14-20 接上了 —— 但 **`facts_stale` 与 `changed_scopes` 全仓
零调用点**,一直到本批。理由每一批都写在同一句话里:没有列可比。

0040 落的那个 `input_fingerprint` 在 `product_attribute_extractions` 上,
也就是**识别 run 行**。它回答的是"这次 run 吃进去的是哪批素材"。
而 `facts_stale()` 问的是**"这条事实还成不成立"** —— 一次 run 可以产出
多条事实,一条事实也可以跨多次 run 存活,两者的作用域根本不同。
拿 run 行的指纹去比属性值,是那条欠账守卫逐字点名的捷径。

本列是事实侧的落点。§4.5 行 226 把这个决定写死了:

    `input_fingerprint` | **新增列**:`product_attribute_values.input_fingerprint`
    (该值产生/确认时的作用域指纹,见 §5.3);`facts_stale` 由它与当前指纹
    比较派生,**不落状态**

"不落状态"是这一列存在的形状:库里存的是**当初的指纹**,不是 STALE 标记。
存标记就有两个事实源 —— 标记说不过期、指纹说过期,而没人知道该信哪个。

## 为什么不回填

回填要回答"这条存量事实当初是按哪批素材算出来的",而那件事**没有人知道**:
`attribute_evidence` 只记到 run,run 的素材快照(`input_asset_ids`)是 §4.6
还没落的一列。唯一算得出来的是**今天**的指纹,而把今天的指纹写进去等于
宣布"每一条存量事实都仍然成立" —— 那是这一列要防的那件事的反面。

所以留 NULL,由 `facts_stale(stored=None) is True` 判过期
(`scope_fingerprint` 模块文档第四条:算不出来就算过期)。与 0040 的
`status` 落 FAILED 是同一条规矩的第四次应用:**默认值是给"没有人想过这种
情况"准备的,而那种情况下不该有人被放行。**

代价说清楚:这一列上线之后,**存量已确认事实会集体判过期**,工作台属性步
每个必填字段出一条 `ATTR_FACT_STALE`,运营要重新点一次头。§3.1 写着系统
尚未正式使用,所以这批行在真实部署里不存在;真要有存量时,正确的做法是
先做一次数据盘点再决定回填口径,**不是回来把这条改成"默认不过期"**。

## 为什么不加索引

这一列**从不出现在 WHERE 里**。判定是逐行比较(取出来的行本来就要读),
不是按指纹查行。加一条没有查询会用的索引,写入时白付代价。

## 宽度与 0040 那一列一致

`_digest()` 出的是 sha256 十六进制,恒 64 字符。两列不同宽的话,同一个
函数的产物在一张表里存得下、在另一张表里被截断 —— 而截断之后的比较
恒不相等,表现是"这条事实永远过期",没有任何地方会报。

Revision ID: 0042
Revises: 0041
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_TABLE = "product_attribute_values"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("input_fingerprint", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "input_fingerprint")
