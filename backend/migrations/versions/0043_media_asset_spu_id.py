"""素材的 SPU 归属改成真外键(A45-batch14-26,§4.8)。

## 这一列回答的是一个决定,不是一个需求

`media_assets` 今天挂 SPU 靠 `spu: String(64)` —— 一个反规范化的字符串码。
而 `products` 从 batch13 起挂的是 `spu_id` 外键(0035)。两张表指着同一个
SPU、用两种口径回答「是不是同一个 SPU」。

14-23 把这件事记成欠账时写的是:§4.8 的去重键要 `spu_id`,所以第一步不是
改约束,是先回答「素材挂 SPU 挂字符串码还是真外键」。**本迁移是那个答案。**

选外键的理由不是"外键更规范",是那个字符串码有一个具体的失效方式:

    改一次 `spu_code`,`products.spu_id` 跟着走(外键),
    `media_assets.spu` 原地断开(字符串码)。

断开之后按 SPU 聚合的素材**静默少掉一批** —— 不报错、不告警,表现是
「这个款的图怎么少了几张」,而排查的人会先去看上传记录。§4.4 已经为
`products` 付过这笔学费,这里不再付第二次。

## 回填留在迁移里,而 0045 的回填不留 —— 区别在哪

这一条的回填是:

    UPDATE media_assets SET spu_id = products.spu_id
    FROM products WHERE media_assets.product_id = products.id

**它是一次 join,不是一条规则。**"这条素材属于哪个 SPU"这个问题,答案
已经在库里(素材挂在商品上、商品挂在 SPU 上),迁移只是把它抄过来。
抄错的方式只有一种:join 写错,而那会当场炸。

0045 的 `evidence_class` 不同:它的答案**不在库里**,要由四条业务规则算。
把规则写进迁移等于在 `derive_evidence_class` 之外再立一个判定点,而
两个判定点会漂移。那一条因此走脚本 —— 见 `app/scripts/backfill_evidence_class.py`。

这条分界线值得记住,它不是"迁移能不能回填",是**"答案是查出来的还是算出来的"**。

## 为什么 RESTRICT 而不是 CASCADE / SET NULL

`products.spu_id` 那一列用的是 SET NULL 语义(删 SPU 不该连坐删商品行)。
这里刻意不同:

    SET NULL 之后那批素材会落进兜底去重键(0044 的第二条局部索引),
    于是**同一张图在「删 SPU 前」和「删 SPU 后」的去重结果不一致** ——
    删之前跨颜色重复会命中提示,删之后不会。

而 SPU 本来就不该在还挂着素材时被删掉。RESTRICT 让那个动作当场失败,
失败信息指向"先处理素材",这比一批悄悄改变了去重口径的素材便宜得多。

## 索引

`spu_id` 上单独建索引:0044 的局部唯一索引带 `WHERE spu_id IS NOT NULL`,
它不能用来回答「这个 SPU 有哪些素材」那类查询(前导列相同但谓词不同,
规划器不会用它做全 SPU 扫描)。而按 SPU 取素材是 §5.1 白名单、§6.2 门禁、
14-9 的作用域指纹三处都在做的事。

Revision ID: 0043
Revises: 0042
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_TABLE = "media_assets"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("spu_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )

    # 回填:素材 → 商品 → SPU。纯 join,见模块文档「回填留在迁移里」。
    #
    # 商品自己 `spu_id` 为空时(老建档路径的存量行)这里写进去的仍是 NULL,
    # 那是对的 —— 那批素材确实还不知道属于哪个 SPU,而 0044 的兜底去重键
    # 正是为它们准备的。**不要在这里试图从 `media_assets.spu` 字符串码
    # 反查 SPU 行**:那个码可能已经因为改名而断开,反查出来的 SPU 会是错的,
    # 而错的外键比空的外键难发现得多。
    op.execute(
        sa.text(
            """
            UPDATE media_assets AS m
               SET spu_id = p.spu_id
              FROM products AS p
             WHERE m.product_id = p.id
               AND p.spu_id IS NOT NULL
            """
        )
    )

    op.create_foreign_key(
        "fk_media_assets_spu_id",
        _TABLE,
        "spus",
        ["spu_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_media_assets_spu_id", _TABLE, ["spu_id"])


def downgrade() -> None:
    op.drop_index("ix_media_assets_spu_id", table_name=_TABLE)
    op.drop_constraint("fk_media_assets_spu_id", _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "spu_id")
