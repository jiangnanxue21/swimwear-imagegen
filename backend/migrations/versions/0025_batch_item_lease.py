"""批次条目租约两列 + 回收索引(任务 17)。

## 为什么 `RUNNING` 需要额外两列才够用

`batch_job_items.status` 有 RUNNING,但它回答不了运维真正要问的那个问题:
**现在还有人在跑它吗。**一个正在跑的条目和一个 worker 死掉之后留下的
残骸,在这张表里长得一模一样 —— 都是 `status='RUNNING'`、`started_at`
有值、`finished_at` 为空。

分不出来的后果不是"显示得不好看",是**那几件永远回不来**:
`run_batch()` 只捞 PENDING,`reset_items_for_retry()` 只挑 FAILED,
两条路径都绕开 RUNNING。批次页会照着 `batch.liveness()` 提示
"点重试试试",而重试接口对着一堆 RUNNING 返回 409。

`lease_until` 把"有人在跑"变成一个带到期时刻的事实,回收器据此把过期的
放回 PENDING(判定在 `app/workbench/batch.py` 的「条目租约与回收」一节)。

## 两列都可空,而且 RUNNING 行的 NULL 有明确含义

本迁移之前创建的行不可能有租约。那批行里如果有卡在 RUNNING 的,
`lease_until IS NULL` 必须被当成**已过期**才回收得掉 —— 这正是
`batch.lease_expired()` 把 None 判成过期的原因。反过来写(NULL = 永不过期)
会让这次迁移把存量残骸永久封存,而它们恰恰是本次改动要修的东西。

所以这里**不给 server_default**:填一个"现在 + 30 分钟"进去,等于宣称
那些行正被某个不存在的 worker 持有,回收器要等半小时才敢碰它们。

Revision ID: 0025
Revises: 0024
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_TABLE = "batch_job_items"
_LEASE_INDEX = "ix_batch_job_items_lease"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("lease_owner", sa.String(length=64), nullable=True))
    op.add_column(
        _TABLE, sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True)
    )
    # 回收器的取数条件:status='RUNNING' AND (lease_until IS NULL OR <= now)。
    # 复合索引的列序与 WHERE 一致 —— status 选择性低但它是等值条件,
    # 放在前面才能让范围条件用上索引
    op.create_index(_LEASE_INDEX, _TABLE, ["status", "lease_until"])


def downgrade() -> None:
    op.drop_index(_LEASE_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "lease_until")
    op.drop_column(_TABLE, "lease_owner")
