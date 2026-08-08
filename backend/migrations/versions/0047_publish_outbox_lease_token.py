"""发布 Outbox 的租约 fencing token(A45-batch17-2 / P2-1)。

## 这一版补的和 0026 补的是同一件事,只是换了一条链路

0026 给 `batch_job_items` 加了 `lease_token`,理由写在那个文件里:
`lease_owner` / `lease_until` 回答得了「有没有人在跑」,回答不了落库时
真正要问的那个问题 —— **我现在还有没有这一件的执行权。**

发布链路当时没有跟着改,于是同一个缺口在 `publish_outbox` 上原样保留:

    1. worker A 领走,租约 LEASE_SECONDS = 180 秒,发出真实外部调用
    2. 调用挂住,超过 180 秒
    3. `claim_due()` 把「LEASED 且租约过期」当成"领它的 worker 崩了"重新发出
    4. worker B 领走同一行(同一 attempt,uq_publish_outbox_publish_attempt_id 是 1:1),
       发同一把幂等键的同一份报文,成功,落 DONE / SUCCEEDED / LISTED + external_spu_id
    5. A 的调用这时才超时返回,`apply_outcome()` **无条件**覆写:
       attempt 从 SUCCEEDED 改成 UNKNOWN、outbox 从 DONE 改成 DEAD、
       listing 从 LISTED 退回 SUBMIT_RESULT_UNKNOWN

第 5 步覆盖掉的是**较新的事实**。商品在平台上是真的上架了(`external_spu_id`
还留着,第三道防线也保证不会重复创建),但界面会把它显示成"结果未知、需人工",
而人工能做的只有 RECONCILE —— 一次本来不必发生的付费调用,外加一段谁也解释不了的
状态回退。

## 为什么不能用 `lease_until` 当判据(评审给的"退一步最小修法"在本仓行不通)

`_renew_lease()` 在发请求**之前**把 `lease_until` 改成了 `now + LEASE_SECONDS`
(BLOCK-16 关的是另一个窗口),而 `_Call.lease_until` 记的仍是领取时那个值。
真按 `WHERE lease_until = 领取值` 写,**正常路径也会 rowcount = 0** ——
每一次投递结果都落不了库,而那是比被覆写严重得多的一种坏法。

所以判据只能是一个**在续租时不变、在重领时必变**的值。那就是令牌。

## 为什么是 UUID 而不是自增版本号

同 0026:版本号要"读一次再加一",而"读-改-写"在两个 worker 之间正是要防的
那件事。UUID 由领取方直接生成,`_lease()` 那一次赋值就是原子的。

## 为什么可空、且不给 server_default

本迁移之前的存量行没有令牌。给默认值等于宣称"这些行归某个 worker 所有",
而它们恰恰是没有主人的那批。留 NULL:一个存量的 LEASED 残骸落不了库
(条件更新影响 0 行,结果进审计),然后由 `claim_due` 在租约过期后正常重领 ——
方向是安全的那个。

注意不能靠 ORM 表达式 `lease_token == None` 获得上述语义:SQLAlchemy 会把它
编译成 `IS NULL`,反而匹配存量行。服务层因此在续租与落库前都显式拒绝空令牌。

## 索引

不加,理由与 0026 逐字相同:落库的 WHERE 是 `id = :id AND ...`,主键已经
把行定位到唯一一条,后面几个条件只是过滤。

Revision ID: 0047
Revises: 0046
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "publish_outbox"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "lease_token")
