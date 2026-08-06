"""图片集的两条兜底唯一索引

Revision ID: 0016
Revises: 0015
Create Date: 第三轮评审 —— 第 7 条(并发批准)与第 9 条(NULL 变体重复)

## 为什么这两条必须在数据库层

服务层已经加了 advisory lock,但锁只覆盖**走那条代码路径的进程**。
迁移脚本、手工 SQL、将来另写的批量工具都不会去拿这把锁。

两条索引各自兜住一种脏数据:

    uq_listing_image_sets_one_approved
        同一个 (spu, channel, site) 最多一个 APPROVED。
        缺了它,两个并发审批可以都提交成功,库里留下两个"已批准",
        而 resolve 按版本号取最高的 —— "回滚到旧版"于是有一半概率不生效,
        且没有任何地方看得出为什么。

    uq_listing_image_items_asset_variant_coalesced
        同一张素材在同一个集里只能出现一次。
        0013 建的 UNIQUE(image_set_id, media_asset_id, variant_id) 在
        variant_id 为 NULL 时不生效(PostgreSQL 把 NULL 当成互不相等),
        而"没绑定变体"恰恰是最常见的情况。

两条都用 `COALESCE(..., '')` 把可空列折成可比较的值,和 0013 里
已有的两条表达式索引同一个口径。

## 建索引之前先清历史脏数据

`uq_listing_image_sets_one_approved` 建不上去的唯一原因,是库里**已经**
有同 scope 的多个 APPROVED —— 那正是这条索引要防的东西。直接建会让
`alembic upgrade head` 失败,而失败信息里看不出该动哪一行。

所以先把每个 scope 里版本号最高的那一版留下,其余归档。归档是这个
系统里本来就有的动作(`approve()` 就是这么做的),不丢数据。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 同 scope 多个 APPROVED 的,只留版本号最高的那一版
    op.execute(
        sa.text(
            """
            UPDATE listing_image_sets AS s
               SET status = 'ARCHIVED'
             WHERE s.status = 'APPROVED'
               AND s.version < (
                   SELECT MAX(t.version)
                     FROM listing_image_sets AS t
                    WHERE t.spu = s.spu
                      AND COALESCE(t.channel, '') = COALESCE(s.channel, '')
                      AND COALESCE(t.site, '') = COALESCE(s.site, '')
                      AND t.status = 'APPROVED'
               )
            """
        )
    )
    # 同一集里重复的素材(variant_id 为 NULL 的那批),只留最早插入的一条
    op.execute(
        sa.text(
            """
            DELETE FROM listing_image_items AS i
             WHERE EXISTS (
                   SELECT 1
                     FROM listing_image_items AS j
                    WHERE j.image_set_id = i.image_set_id
                      AND j.media_asset_id = i.media_asset_id
                      AND COALESCE(j.variant_id, '') = COALESCE(i.variant_id, '')
                      AND (j.created_at, j.id) < (i.created_at, i.id)
             )
            """
        )
    )

    op.create_index(
        "uq_listing_image_sets_one_approved",
        "listing_image_sets",
        ["spu", sa.text("COALESCE(channel, '')"), sa.text("COALESCE(site, '')")],
        unique=True,
        postgresql_where=sa.text("status = 'APPROVED'"),
    )
    op.create_index(
        "uq_listing_image_items_asset_variant_coalesced",
        "listing_image_items",
        ["image_set_id", "media_asset_id", sa.text("COALESCE(variant_id, '')")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_listing_image_items_asset_variant_coalesced",
        table_name="listing_image_items",
    )
    op.drop_index(
        "uq_listing_image_sets_one_approved", table_name="listing_image_sets"
    )
