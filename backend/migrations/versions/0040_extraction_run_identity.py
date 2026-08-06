"""识别 run 的身份五列 + §9.2 的部分唯一索引(A45-batch14-20,§4.6 / §9.2)。

## 这五列挡着什么

`run_state.py` 与 `scope_fingerprint.py` 两个判定模块从 batch14-9 / 14-12 起
就写好并穷举验过了,**却一直接不上线** —— 理由每一批都写在同一句话里:
没有列可写。两条欠账守卫逐字记着这件事:

    test_the_idempotency_half_cannot_be_wired_yet_and_here_is_exactly_why
    test_this_module_cannot_be_wired_yet_and_here_is_exactly_why

本批把列落下去,那两条守卫按它们自己的约定被删掉并换成正向守卫。

    status              这次 run 算哪一档。**同时是下面那条索引的谓词列**
    spu_id              归属。幂等键的第一维
    input_fingerprint   这次 run 吃进去的那批素材的指纹(§5.3)
    requested_scope     请求的作用域 token(§4.6)
    idempotency_key     §9.2 的键本体

## 为什么 `status` 不回填

回填要按 `succeeded_count` / `failed_count` / `image_count` 重新推导每一行,
而那个推导是 `run_state.terminal_status_for` —— 一个**判定**。把它写成
SQL 的 CASE 表达式等于给它造第二个判定点,而两个判定点漂移时没有人会发现
(§5.1 白名单那一批为同一个理由拒绝把过滤写成 WHERE 子句)。

那把 `terminal_status_for` import 进来跑一遍呢?更糟:迁移是**冻结在时间里**
的脚本,而判定会演进。今天 import 它,三个月后规则改了,在一台新库上
`alembic upgrade head` 会用**新规则**去写**旧行**,而这件事不会有任何提示。
全仓 40 份迁移没有一份 import 过 `app.*`,这条不由本批开口子。

所以:`server_default` 取 `FAILED`,存量行落在**不放行**那一侧 ——
它既不进事实合并(`AUTHORITATIVE_STATUSES`)也不占幂等键
(`KEY_OCCUPYING_STATUSES`)。方向是有代价的:一次真的成功过的旧 run
会显示成失败。选它是因为反方向的代价更贵 —— 默认 COMPLETED 会让一次
从来没有被判定过的 run 以「算数」的身份参与两件要花钱的事。
§3.1 另说明系统尚未正式使用,这一列之前的行在真实部署里不存在。

## 为什么唯一索引必须是部分的

全表唯一的话,一次 FAILED 之后**同样的输入再也建不出第二个 run** ——
输入没变、模型没变、字段没变,而那正是重试的定义。运营看到的是一个
再也识别不了的商品,唯一的解法是去改点什么来骗过约束。

占键的只有「还会被复用的那几档」。谓词在这里是**字面量**,而
`run_state.unique_index_predicate()` 是它的孪生;两者不许漂移这件事由
`test_the_migration_predicate_is_the_twin_of_the_pure_one` 盯着。
写字面量而不是调函数,理由同上一节(迁移冻结、判定演进);而正因为它冻结,
`KEY_OCCUPYING_STATUSES` 哪天变了,那条守卫会红 —— 那时该做的是**再写一条
迁移重建索引**,不是回来改这一行。

## `spu_id` 用 SET NULL 不用 CASCADE

删掉一个 SPU 不该连坐删掉「当时识别过什么、花了多少钱」的记录 ——
那是账本。断了归属之后这一行的幂等键仍在,但它指向的 SPU 没了,
下一次同型请求会因为 `spu_id` 取不到而根本建不出键(见服务层),
于是不会误命中一条孤儿键。

Revision ID: 0040
Revises: 0039
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_TABLE = "product_attribute_extractions"

#: `run_state.unique_index_predicate()` 的孪生。**改这里之前先读上面第四节**
#: —— 正确的做法是新写一条迁移重建索引,不是编辑这一行。
_KEY_OCCUPYING_PREDICATE = "status IN ('COMPLETED', 'QUEUED', 'RUNNING')"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="FAILED",
        ),
    )
    op.add_column(_TABLE, sa.Column("spu_id", PGUUID(as_uuid=True), nullable=True))
    op.add_column(
        _TABLE, sa.Column("input_fingerprint", sa.String(length=64), nullable=True)
    )
    op.add_column(_TABLE, sa.Column("requested_scope", sa.Text(), nullable=True))
    op.add_column(
        _TABLE, sa.Column("idempotency_key", sa.String(length=64), nullable=True)
    )
    op.create_foreign_key(
        f"fk_{_TABLE}_spu_id",
        _TABLE,
        "spus",
        ["spu_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_attr_extractions_spu", _TABLE, ["spu_id", "created_at"])
    op.create_index(
        "uq_attr_extractions_idempotency_key",
        _TABLE,
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text(_KEY_OCCUPYING_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("uq_attr_extractions_idempotency_key", table_name=_TABLE)
    op.drop_index("ix_attr_extractions_spu", table_name=_TABLE)
    op.drop_constraint(f"fk_{_TABLE}_spu_id", _TABLE, type_="foreignkey")
    for column in (
        "idempotency_key",
        "requested_scope",
        "input_fingerprint",
        "spu_id",
        "status",
    ):
        op.drop_column(_TABLE, column)
