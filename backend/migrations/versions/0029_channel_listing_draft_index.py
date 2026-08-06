"""`channel_listings.draft_id` 建索引(A-13 的提交证据链)。

## 为什么现在需要它

A-13 给 `resolve_gate()` 加了第二种等价证据:「驳回之后有一次成功的
PublishAttempt」。取这份留痕的是 `platform_service._publish_attempt_entries()`,
它经 `ChannelListing.draft_id` 关联:

    SELECT publish_attempts.created_at
      FROM publish_attempts
      JOIN channel_listings ON publish_attempts.channel_listing_id = channel_listings.id
     WHERE channel_listings.draft_id = :draft_id
       AND publish_attempts.status = 'SUCCEEDED'

`channel_listings` 上原有三条索引(status+next_poll_at / test_batch_tag /
product_id)和一条唯一约束(channel+shop_id+site+product_id),**没有一条以
`draft_id` 打头**。唯一约束的前导列是 `channel`,`draft_id` 根本不在里面。
所以这个 WHERE 只能顺序扫全表。

## 为什么不能忽略

这条查询不在冷路径上。`resolve_gates()` 每次打开有驳回的商品都会调一次,
而它恰恰是 A-13 之前"永远过不了闸"的那批商品 —— 也就是运营会反复点开的
那批。A-08 刚把发布清单的按行查询收掉,再让驳回页每次全表扫一遍,等于把
省下来的往返换了个地方还回去。

外键列建索引本来也是默认动作:PostgreSQL **不会**为外键自动建索引(这一点
与某些数据库不同),而 `draft_id` 上还挂着 `ondelete="RESTRICT"` —— 删除
`listing_drafts` 行时的约束检查同样要按 `draft_id` 反查一次。

## 为什么是普通索引,不是部分索引

`draft_id` 允许为空,老数据里有相当一部分是空的(那正是 A-13 的已知迁移点:
这类行取不到提交证据链)。写成 `WHERE draft_id IS NOT NULL` 的部分索引能省
一点空间,但代价是查询计划器只在谓词可证包含时才用得上它,而这里的调用点
是参数化的 `draft_id = :param`。参数不为空是运行时事实、不是编译期事实,
省下的空间不值得这个不确定性。

## 降级会丢什么

只丢性能,不丢数据也不丢行为 —— 上面那条查询在没有索引时仍然返回同样的
结果,只是走顺序扫。所以 downgrade 就是单纯 drop 掉。

Revision ID: 0029
Revises: 0028
"""
from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_index(
        "ix_channel_listings_draft",
        "channel_listings",
        ["draft_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_channel_listings_draft", table_name="channel_listings")
