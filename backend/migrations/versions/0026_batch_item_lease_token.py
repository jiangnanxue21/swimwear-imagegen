"""批次条目租约 fencing token(A43 / BLOCK-02)。

## 0025 补的是"有没有人在跑",这一版补的是"跑完的还是不是同一个人"

0025 给 `batch_job_items` 加了 `lease_owner` / `lease_until`,让"正在跑"
和"worker 死了留下的残骸"在库里分得开。但那两列回答不了落库时真正要问的
那个问题:**我现在还有没有这一件的执行权。**

没有答案的后果在 A42 的真库并发用例里被写成了一条"已知错误行为"的断言:

    1. worker A 领走,租约 30 分钟
    2. 这一件真的跑了 31 分钟
    3. 回收器只看 lease_until,把它放回 PENDING
    4. worker B 领走,跑完,落 SUCCEEDED
    5. A 也跑完了,**无条件**覆写 —— B 的结果消失,paid_calls 二次累加

第 5 步不是理论风险:`_apply_outcome()` 原来是一个纯赋值函数,不带任何
WHERE 条件。本迁移加的这一列,是把那个赋值变成条件更新所需要的唯一凭证。

## 为什么是 UUID 而不是自增版本号

版本号要读一次再加一,而"读-改-写"在两个 worker 之间正是要防的那件事。
UUID 由领取方直接生成,不需要读:`claim_items()` 那一条 UPDATE 里就把
新令牌盖上去了,天然是原子的。

## 为什么可空、且不给 server_default

本迁移之前创建的行没有令牌。给一个默认值等于宣称"这些行归某个 worker
所有",而它们恰恰是没有主人的那批。留 NULL,`lease_still_held()` 会把
它们判成"已失去执行权" —— 一个存量的 RUNNING 残骸落不了库,只能进审计,
然后由回收器正常处理。方向是安全的那个。

## 索引

不加。落库的 WHERE 是 `id = :id AND ...`,主键已经把行定位到唯一一条,
后面三个条件只是过滤。给 `lease_token` 单独建索引不会被用到,却要在每次
领取(每件一次)时多维护一棵树。

Revision ID: 0026
Revises: 0025
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_TABLE = "batch_job_items"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "lease_token")
