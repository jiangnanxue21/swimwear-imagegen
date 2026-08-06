"""轮询调度三列 + 测试批次号(任务 15 / 16)。

任务 11 建 `channel_listings` 时留了 `last_polled_at`,当时设想的取数是
「按上次轮询时间排序,挑最久没问过的」。真接上轮询器之后这个设想不成立:

    退避是**每行不同的**   一个刚提交的商品 20 秒后就该再问,一个审核了两小时
                           的一小时问一次。用「上次问过多久了」现算,就得把
                           退避曲线塞进 SQL,而那条曲线是要被穷举测试的判定
    问不到也要推进         平台 5xx 时我们对这个商品的了解没有任何变化,
                           但下一次不该立刻再问。`last_polled_at` 若跟着动,
                           它就不再是「这个状态是什么时候的」了

所以退避落到 `next_poll_at` 列上,`last_polled_at` 退回它本来的语义(展示与
排查),两者刻意分开。索引也跟着改到真实的取数条件上。

## 关于 `test_batch_tag`

4.1 节 H 要求「每一次真实提交在本地留下可检索的标记(测试批次号),能一次性
列出本轮测试创建了哪些外部商品」。此前没有任何一列承担这件事 —— 唯一能用的
代用品是 `created_at` 的时间窗,而人工测试跨天、中断、与别人的测试重叠,
时间窗在最需要它的那一刻(清理时)最不可靠。

Revision ID: 0024
Revises: 0023
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_TABLE = "channel_listings"

_OLD_POLL_INDEX = "ix_channel_listings_status_polled"
_NEW_POLL_INDEX = "ix_channel_listings_status_next_poll"
_BATCH_INDEX = "ix_channel_listings_test_batch"

#: `poll_attempt_count` 必须带 server_default:这张表可能已经有行,
#: 加一个 NOT NULL 列而不给默认值,PostgreSQL 直接拒绝整条迁移(同 0023)
_COLUMNS = (
    ("next_poll_at", sa.DateTime(timezone=True), True, None),
    ("poll_attempt_count", sa.Integer(), False, "0"),
    ("test_batch_tag", sa.String(64), True, None),
)


def upgrade() -> None:
    for name, type_, nullable, default in _COLUMNS:
        op.add_column(
            _TABLE,
            sa.Column(name, type_, nullable=nullable, server_default=default),
        )

    # 已有的行:让它们立刻可轮询,而不是等到有人手工填一次时间。
    # 空值在调度那边也按「现在就可以问」放行(同 `claim_due` 踩过的坑),
    # 两处一致 —— 但显式写上更好排查:`next_poll_at IS NULL` 在库里看起来
    # 像是"漏填了",而它其实是"从来没问过"
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET next_poll_at = created_at WHERE next_poll_at IS NULL"
        )
    )

    op.drop_index(_OLD_POLL_INDEX, table_name=_TABLE)
    op.create_index(_NEW_POLL_INDEX, _TABLE, ["status", "next_poll_at"])
    op.create_index(_BATCH_INDEX, _TABLE, ["test_batch_tag"])


def downgrade() -> None:
    op.drop_index(_BATCH_INDEX, table_name=_TABLE)
    op.drop_index(_NEW_POLL_INDEX, table_name=_TABLE)
    # 降级要把 0022 建的那条索引还回去,否则降回去之后表的形状与 0022
    # 描述的不一致 —— 而 `test_migrations.py` 的升降级往返正是在验这件事
    op.create_index(_OLD_POLL_INDEX, _TABLE, ["status", "last_polled_at"])

    for name, _type, _nullable, _default in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
