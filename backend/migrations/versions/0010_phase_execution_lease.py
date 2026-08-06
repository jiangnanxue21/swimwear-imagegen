"""续跑阶段的执行租约

Revision ID: 0010
Revises: 0009
Create Date: 代码评审第二轮第 4 条 —— SCORING / FORMATTING 恢复路径绕过原子认领

`_run()` 在调用 `_claim()` **之前**就处理了 FORMATTING 和 SCORING 两个续跑分支。
那两个分支的前后状态是同一个,所以"认领前后状态不同"的条件 UPDATE 手法用不上:

    Outbox 是至少一次投递,重复消息属于正常现象
        -> 两个 worker 同时拿到同一个任务的消息
        -> 两个都看到 status=SCORING,两个都开始重新评分同一批图
        -> 评分是按次计费的大模型调用,账单直接翻倍

出图那条更隐蔽:商品行锁只能把两次格式化**串行化**,不能阻止第二个 worker
在第一个提交之后再完整地做一遍 —— 结果是同一批成品图被发布两次,
`generation` 代数凭空多一代,网站上挂的还是同一张图,但历史记录对不上账。

租约把"排他"和"持锁"分开:抢到租约的 worker 独占这个阶段,但不持有行锁,
于是取消接口和回收器照常能改这一行。这正是当初拆短事务想要的效果。

## 为什么不新增两个状态

评审里给的另一个选项是 SCORING_QUEUED -> SCORING。那样要动状态机转移表、
STALLABLE_STATUSES、前端的状态枚举和一批测试;而这两列只影响
"谁在跑",不影响"跑到哪一步"——后者才是状态机该回答的问题。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "generation_tasks",
        # worker 的自述标识(主机名 + 进程号 + 一段随机)。
        # 只用于排查"是谁在跑",不参与任何权限判断 —— 它是自报的
        sa.Column("phase_lease_owner", sa.String(64), nullable=True),
    )
    op.add_column(
        "generation_tasks",
        # 租约截止时刻。到期后别的 worker 可以接管,不需要人工干预 ——
        # 持有者进程被 kill 时不会有人来释放它
        sa.Column("phase_lease_until", sa.DateTime(), nullable=True),
    )
    # 抢租约的 WHERE 里就是这两列 + status。给到期时间建索引不划算
    # (这张表的行数按任务算,不按事件算),但状态索引已经存在,够用了。


def downgrade() -> None:
    op.drop_column("generation_tasks", "phase_lease_until")
    op.drop_column("generation_tasks", "phase_lease_owner")
