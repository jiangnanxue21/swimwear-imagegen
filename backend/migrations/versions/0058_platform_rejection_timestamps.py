"""`platform_rejections.created_at/updated_at`:补 server default 并收紧为 NOT NULL。

## 真实库里这两列可以是 NULL,而闸门拿它当时间用

`TimestampMixin`(`db/base.py`)对这两列的声明是:

    nullable=False
    server_default=func.now()

而 0019 建 `platform_rejections` 时,这两列写的是:

    nullable=True
    没有 server default

**两个生产创建路径都不显式写时间戳**(`platform_service.py:213` 手工记录、
`:309` 平台轮询),因为 ORM 认为库会自己生成。SQLAlchemy 按当前 ORM 编译
INSERT 时也确实不带这两个值 —— 于是真实库里新建的驳回记录拿到的是
`created_at = NULL`。

`create_all()` 建出来的测试库按 ORM 走(NOT NULL + default,对的),真实库按
迁移走(可空、无默认,错的)。这是 0057 那个坑的镜像:那次宽度只改在 ORM 里,
这次是 nullable/default 只对在 ORM 里。

## 为什么这不是一个展示字段的小毛病

`platform_service.py:406` 把 `rejection.created_at` 转成 `rejected_at` 交给
`platform.resolve_gate`,而那个函数的两个循环都写在 `if since is not None:`
里面 —— `rejected_at` 为空时,**没有任何后续动作会被认定为「发生在驳回之后」**。

于是真实库上会长这样:

    运营改好了 → 重新导出 → 重新提交 → 闸门仍然说「缺新的导出/提交」

驳回记录再也走不完「修复 → 重导 → 关闭」这条路,只能靠人绕过去。
`resolve_gate` 那句「判不出来的时候一律算缺」在设计上是对的(误报的代价是
多勾一个框),但它防的是**审计被清理**这种偶发情况,不是防「每一条新记录
都判不出来」。

## 为什么不去改 0019

0019 可能已经在某些环境上执行过。改历史迁移会让迁移历史不可重放一致 ——
同一个 revision 在两台机器上建出两种表,而 alembic 只认 revision 号。
所以走一条新迁移:回填 + 加默认 + 收紧。

## 回填取哪个时间:只允许**不早于**真实驳回时刻的来源

方向是被 `resolve_gate` 的语义定死的:

    回填得太早   驳回之前的旧导出会被算成「驳回之后」→ 闸门自动放行
                 → 一条还没真修的驳回被关掉。**漏报**
    回填得太晚   什么都算不上「之后」→ 闸门继续要求人工确认。**误报**

`resolve_gate` 自己写着:「误报的代价是运营多勾一个框,漏报的代价是驳回台账
退化成备忘录」。所以四个来源按精度排,而每一个都保证 ≥ 真实创建时刻:

    1  audit_logs 里这条驳回的最早一条留痕   同一个事务里写的,精确
    2  LEAST(草稿的 platform_status_at,      前者与驳回同事务写入,可能被
          这条驳回的 resolved_at)            同草稿的后一条驳回覆盖(只会更晚);
                                             后者是这条驳回被关闭的时刻。
                                             取较早的那个 = 更紧的上界,
                                             同时保住 created_at ≤ resolved_at
    3  now()                                 迁移时刻。最保守的落点:
                                             在此之后真的重导一次才过得了闸

**第 3 条是有语义损失的**,而且没法避免:那些记录的真实创建时刻在库里
不存在第四个副本。落到这一支的行只会让闸门更严,不会让它误放行。

`export_at` 没有进这个列表 —— 它是**被驳回的那次导出**的时间,早于驳回本身。
拿它回填正好踩中上面那条「漏报」。审计报告把它列为候选,但按 `resolve_gate`
的方向推下去它是唯一一个不能用的。

Revision ID: 0058
Revises: 0057
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "platform_rejections"

#: 三级回填。`audit_logs.entity_type` 的取值见 `platform_service.py` 四处
#: `audit.record(...)`;最早的那条就是 CREATE 那次,所以这里用 MIN 而不去
#: 耦合 `AuditAction` 枚举的字面量 —— 枚举改名不该让一条历史迁移变红。
_BACKFILL_CREATED_AT = f"""
UPDATE {_TABLE} AS pr
SET created_at = COALESCE(
    (
        SELECT MIN(a.created_at)
        FROM audit_logs AS a
        WHERE a.entity_type = 'PlatformRejection'
          AND a.entity_id = pr.id
    ),
    LEAST(
        (
            SELECT d.platform_status_at
            FROM listing_drafts AS d
            WHERE d.id = pr.draft_id
        ),
        pr.resolved_at
    ),
    now()
)
WHERE pr.created_at IS NULL
"""

#: `updated_at` 不需要第二套来源:这张表上唯一会改行的动作是关闭驳回
#: (`resolve_rejection`),所以关过的取关闭时刻,没关过的等于创建时刻。
_BACKFILL_UPDATED_AT = f"""
UPDATE {_TABLE}
SET updated_at = COALESCE(resolved_at, created_at)
WHERE updated_at IS NULL
"""


def upgrade() -> None:
    # 顺序不能换:`updated_at` 的回填读的是**已经填好的** `created_at`
    op.execute(sa.text(_BACKFILL_CREATED_AT))
    op.execute(sa.text(_BACKFILL_UPDATED_AT))

    # **两列各写一遍,不用 `for column in (...)`。** 迁移是给人读的历史记录,
    # 而且 `audit_migration_chain.py` 对 `alter_column` 只认解析得出的列名 ——
    # 循环变量在那里解析不出来,整条 alter 对门禁隐形(第一版就这么写的,
    # 于是门禁继续报着已经修好的那 4 条)。四行重复换一条看得见的迁移,值。
    #
    # 每列两步:server default 先加,再收紧 NOT NULL。反过来的话,收紧与加默认
    # 之间有一个窗口:此刻并发插进来的 INSERT 仍然不带时间戳,而列已经不许空。
    op.alter_column(
        _TABLE,
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        existing_nullable=True,
    )
    op.alter_column(
        _TABLE,
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        _TABLE,
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        existing_nullable=True,
    )
    op.alter_column(
        _TABLE,
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        existing_server_default=sa.text("now()"),
    )


def downgrade() -> None:
    # **不清空回填进去的值。** 回退要撤的是约束,不是数据 —— 把时间戳再抹回
    # NULL 等于亲手制造这条迁移要修的那个故障,而且这次是在有数据的库上。
    #
    # 于是回退是有意不对称的:约束回到 0019 的形状,值留着。再 upgrade 一次
    # 时上面那两条回填只会命中真正为空的行,是幂等的。
    op.alter_column(
        _TABLE,
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        _TABLE,
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=None,
        existing_nullable=True,
    )
    op.alter_column(
        _TABLE,
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        existing_server_default=sa.text("now()"),
    )
    op.alter_column(
        _TABLE,
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=None,
        existing_nullable=True,
    )
