"""文案生成的幂等键(PRD §4.9 / AC-11,阶段 5 批次 5-3)。

## 这一列补的是哪一格

`listing_copies` 从第一版起就是 **SPU 粒度**的(`spu` + `channel` + `site` +
`locale` 唯一)。而入口是 **SKU 粒度**的:
`POST /products/{product_id}/copy/generate` 收的是一行商品。

一个 S/M/L 三码的 SPU,运营在三行上各点一次「生成文案」,得到三次真实的
LLM 调用、三个版本,而输入(已确认事实、语言、渠道)完全相同。
**三次调用产出的是同一件东西。** 尺码不进文案:标题、卖点、描述里没有
任何一个字来自 `products.size`。

批量那条路径早就是对的(`batch.ACTION_SCOPE[GENERATE_COPY] == "spu"`),
所以这个缺口只长在单件入口上 —— 而单件入口恰恰是运营日常用的那一个。

键的算法在 `app/workflows/copy_idempotency.py`(零依赖纯函数),
这里只落列。

## 为什么可空,而且这一次 NULL 的方向和 0049 相反

本迁移之前建的每一版文案都没有键。判定层(`copy_idempotency.decide`)
把 `None` 判成 **GENERATE**,也就是"重新生成一次"。

0049 那两列的 NULL 判的是"不可证明、不放行"。方向相反是刻意的:
**默认值的方向按代价选,不按对称选。**

    0049 的 NULL 放行了 → 一份缺了整个颜色的草稿被导出,平台那边才发现
    0050 的 NULL 复用了 → 存量文案被当成"这次要的那一版",而它可能是
                          按一批**早就变过**的事实生成的,内容不再成立

两种错都指向同一件事:NULL 是「不知道」,而不知道的时候不许假装知道。
区别只在于"保守"在两处分别指向哪一边 —— 那边是拦下,这边是重算一次。

## 不加唯一索引

一眼看去 `UNIQUE(idempotency_key)` 很自然,而它是错的:
`copy_service.save_copy` 会把规则硬失败的输出也落库(状态 `REJECTED`),
好让运营看见模型到底吐了什么。唯一索引会让那一行**永久占住**这个键,
于是这个 SPU 的文案再也生成不出来 —— 现象是"点了没反应"。

复用的挑选口径因此写在判定层(`REUSABLE_STATUSES`,失败态不占槽位),
串行由 `try_advisory_xact_lock` 保证,与 `batch_service` 的回执表同构。
这与 `attributes/run_state.unique_index_predicate()` 当初拒绝全表唯一
是同一条理由的第二次应用。

只加一条**普通**索引:复用查询按 `(spu, channel, site, locale, idempotency_key)`
找最新一版,而 `ix_listing_copies_lookup` 的列序里没有键。

Revision ID: 0050
Revises: 0049
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "listing_copies"
_COLUMN = "idempotency_key"
_INDEX = "ix_listing_copies_idempotency"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=64), nullable=True))
    op.create_index(_INDEX, _TABLE, ["spu", "channel", "site", "locale", _COLUMN])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, _COLUMN)
