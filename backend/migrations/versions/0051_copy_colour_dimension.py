"""文案的颜色维(PRD §4.9 / AC-11 第二句,A45-batch24)。

## 这一列对齐的是键与存储的作用域

0050 落了幂等键。键的输入含「已确认事实版本集」,而 `primary_color` 是
VARIANT 层字段 —— 同一个 SPU 的红、蓝两个颜色各有一条 CONFIRMED 的它,
于是**键天然随颜色变化**。存储却仍是 SPU 一个槽
(`spu`+`channel`+`site`+`locale` 唯一):

    红色 SKU 点生成 → 存 key_R
    蓝色 SKU 点生成 → 既有版本带 key_R ≠ key_B → 判 GENERATE → 付费一次,
                      新版本把红色那一版挤成 ARCHIVED
    回红色再点一次 → 又不等 → 又付费一次

在颜色轴上来回点,**每次都花钱**,而「当前文案」在两个颜色之间摆动。
这不是键算细了:文案内容本身就是颜色相关的(标题模板第一段是颜色,
`copy_rules.REQUIRED_FACTS` 要求 `primary_color` 必须有 claim),
把颜色从键里去掉等于让蓝色行复用一篇写着「黑色」的文案。
键与存储必须对齐,取哪一侧由 AC-11 第二句定死:
「颜色维文案若启用,则幂等粒度为 SPU + 颜色 + 语言,仍不含尺码」。

## 列存身份串,不是外键

取值是 `listings/variants.variant_id_for()` 的产出 —— 与属性 owner_id、
`color_sku_image_map` 的键同一份定义(三级回落只在 `variant_key.identity_of`
一处)。立 UUID 外键会给「变体口径只有一份定义」开第二个口,而且
掉到 variant_key/种子级的存量行根本给不出 UUID。

## 存量行回填哨兵 `""`,不回填真实颜色

`""` 的含义是「0051 之前建的版本,颜色维未知」。按颜色查询时它永远不
命中,存量文案一律判「这个颜色还没有文案」—— fail closed,与 0050 的
NULL 键判 GENERATE 同向:代价只是多生成一次,而反方向(把 SPU 槽的旧
文案指派给某个颜色)等于替当年的生成动作补一个它没有回答过的问题。
不用 NULL 的理由与 `site` 的 'ANY' 相同:这一列进唯一键,PostgreSQL 把
NULL 当成互不相等,版本号唯一性会在 NULL 槽上失效。

## 唯一约束与两条索引原地重建

版本号的分槽粒度变了(每个颜色一条版本序列),唯一约束必须带上新列;
`lookup` / `idempotency` 两条索引的查询都改成了带颜色过滤,列序跟着走。
降级按相反顺序还原,数据列直接丢弃 —— 丢的只是「这一版属于哪个颜色」
这个标注,文案正文与版本历史都还在。

Revision ID: 0051
Revises: 0050
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "listing_copies"
_COLUMN = "color_variant_id"
_UQ = "uq_listing_copies_version"
_IX_LOOKUP = "ix_listing_copies_lookup"
_IX_IDEM = "ix_listing_copies_idempotency"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(length=64), nullable=False, server_default=""),
    )
    op.drop_constraint(_UQ, _TABLE, type_="unique")
    op.create_unique_constraint(
        _UQ, _TABLE, ["spu", "channel", "site", "locale", _COLUMN, "version"]
    )
    op.drop_index(_IX_LOOKUP, table_name=_TABLE)
    op.create_index(
        _IX_LOOKUP, _TABLE, ["spu", "channel", "site", "locale", _COLUMN, "status"]
    )
    op.drop_index(_IX_IDEM, table_name=_TABLE)
    op.create_index(
        _IX_IDEM,
        _TABLE,
        ["spu", "channel", "site", "locale", _COLUMN, "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_index(_IX_IDEM, table_name=_TABLE)
    op.create_index(
        _IX_IDEM, _TABLE, ["spu", "channel", "site", "locale", "idempotency_key"]
    )
    op.drop_index(_IX_LOOKUP, table_name=_TABLE)
    op.create_index(_IX_LOOKUP, _TABLE, ["spu", "channel", "site", "locale", "status"])
    op.drop_constraint(_UQ, _TABLE, type_="unique")
    op.create_unique_constraint(
        _UQ, _TABLE, ["spu", "channel", "site", "locale", "version"]
    )
    op.drop_column(_TABLE, _COLUMN)
