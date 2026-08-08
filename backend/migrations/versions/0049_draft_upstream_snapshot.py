"""草稿的颜色维上游快照两列(PRD v3.1 §4.10 / 阶段 5 批次 5-1)。

## 这两列补的是哪一格

`listing_drafts` 今天有两样描述上游的东西:`source_fingerprint` 回答
「变没变」,`canonical_snapshot["components"]` 回答「哪个轴变了」。
六个轴里**没有一个带颜色维** —— 于是这些话在草稿这一侧说不出来:

    这个 SPU 新增了一个 ACTIVE 颜色,草稿里没有它的 SKU 行
    红色的样品换了,红色的事实版本变了,草稿引用的还是上一版
    红色的生成方案换了指纹,它那一版图片集已经不该用了
    某个颜色被停用了,它的 SKU 还在导出文件里

§6.7 要求「所有上游版本和指纹仍然有效」由 `upstream_versions_json` 逐项
比较**派生**,不落 STALE 状态列 —— 也就是现有草稿过期机制的推广。
形状与判定在 `app/workbench/upstream_snapshot.py`。

## 为什么两列可空、没有 server_default —— 与同表其它 JSONB 列不同

`listing_drafts` 上的 `canonical_snapshot` / `manual_payload` 等一律
`nullable=False, server_default='{}'`。照抄那个写法在这里是错的。

那几列从第一版起就有写入点,`{}` 的含义是「这份草稿确实没有手填字段」。
这两列不是:本迁移之前建的每一行草稿都**一次都没算过颜色维**,
而 `{}` 的含义是「算过,颜色维是空的」。READY 门禁读到它会在颜色轴上
**静默放行** —— 一份缺了整个颜色的草稿显示「上游全部有效」,不报错。

所以留 NULL:让「没算过」如实显示成「不知道」。判定层把 None 判成
不可证明、不放行。0042(留 NULL 兜底成「过期」)、0045(留 NULL 兜底成
「没算过」)、0048(留 NULL 兜底成「不知道发过几次请求」)是同一条规矩
的前三次应用,共同点是:**默认值不许让任何人被静默放行或静默拦下。**

## 不回填

答案要算,而且算它要读 `color_variants` / `products` / `listing_image_sets` /
`generation_plans` 四张表,再按 §6.5 的混排规则挑图。迁移不许 import
`app.*`(全仓 43 份没有一份破例,0038 / 0040 / 0042 / 0045 各写过为什么),
在这里回填只剩「把那套规则冻成一段 SQL」一条路 —— 而那正是 0045 拒绝过
的第二个判定点。

也不需要回填:存量草稿的正确结论就是「颜色维没算过」,而 NULL 已经
如实表达了它。重新生成一次草稿就有了。

## 不加索引

两列都只在按主键取到草稿之后整体读写,不出现在任何 WHERE 里。
JSONB 上的 GIN 索引在这里是纯开销(写放大),而它挡不住任何真实查询。

Revision ID: 0049
Revises: 0048
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "listing_drafts"
_COLUMNS = ("upstream_versions", "color_sku_image_map")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            _TABLE,
            sa.Column(name, postgresql.JSONB(), nullable=True),
        )


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
