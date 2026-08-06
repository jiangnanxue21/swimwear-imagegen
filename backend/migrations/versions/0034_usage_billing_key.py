"""费用流水加计费身份(A45-batch12-5 / NEW-01)。

## 这一层原来是空的

`provider_usage_records` 上只有三条普通索引,没有任何唯一约束,也没有任何
"这一行记的是哪一笔消费"的概念。于是同一笔外部生成被记两次在库里是**完全
合法**的:

    attempt_id  operation  billable_units  succeeded
    a-1         submit     3               false      <- 第一次:全部下载失败
    a-1         submit     3               true       <- 恢复成功之后又记一遍

Provider 只跑过一次、只收过一次钱。而这两行不会报错、不会告警,只会让月度
花费、外推金额、调用次数、失败率一起偏高 —— 偏高的部分恰好等于**恢复过的
那些任务**,也就是最需要被看清楚的那一批。

## 为什么键是 `<attempt_id>:submit` 这种字符串,不是联合唯一

考虑过 `UNIQUE (attempt_id, operation)`,不行:`attempt_id` 可空(评分流水
`operation=vision_score` 那一批就没有 attempt),而 PostgreSQL 的联合唯一在
含 NULL 时形同虚设 —— 两行 `(NULL, 'vision_score')` 互不冲突,约束等于没加,
而它看起来是加了的。

独立的 `billing_key` 把"有没有幂等身份"变成一个显式选择:

    生成调用   `<attempt_id>:submit`   一条 attempt 只可能有一次 submit
    轮询/取结果 NULL                    每次都是真的又调了一次,不是重复记账
    评分调用   NULL                    每张图每次评分都是一次真实计费

NULL 不参与唯一判定,正好对应"这一类调用没有幂等身份"。

## 存量重复怎么办:报错,不删,也不回填

`upgrade()` 先按 `(attempt_id, operation='submit')` 查一遍重复。有的话
**抛出并列出来**。

不自动合并的理由和迁移 0033 一样:迁移脚本没有资格替人判断"这两行里哪一行
才是真的"。而且这批数据的正确处理方式取决于对账结果 —— 厂商账单上到底收了
几次,只有人能去看。`tools/resolve_billed_unknown.py` 那一类脚本才是干这件事
的地方。

也**不回填**历史行的 `billing_key`:回填等于替过去的账做一个今天才定下来的
判断,而那批行现在长什么样,是排查"当初到底记了几笔"的唯一依据。

Revision ID: 0034
Revises: 0033
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from typing import Union

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_CONSTRAINT = "uq_provider_usage_records_billing_key"

#: 存量重复的定位查询。报错信息里原样给出去,让人能直接粘到 psql 里跑
_FIND_DUPLICATES = """
SELECT attempt_id, count(*) AS n, sum(billable_units) AS units
FROM provider_usage_records
WHERE operation = 'submit' AND attempt_id IS NOT NULL
GROUP BY attempt_id
HAVING count(*) > 1
ORDER BY n DESC
"""


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(_FIND_DUPLICATES)).fetchall()
    if duplicates:
        preview = "\n".join(
            f"    attempt={row[0]} 记了 {row[1]} 条 submit 流水,合计 {row[2]} 个计费单位"
            for row in duplicates[:10]
        )
        more = "" if len(duplicates) <= 10 else f"\n    ……以及另外 {len(duplicates) - 10} 条"
        raise RuntimeError(
            "provider_usage_records 里已经存在同一条 attempt 的多笔 submit 计费,"
            "唯一约束加不上去。\n"
            "这批数据是 A45-batch12-5 / NEW-01(恢复成功后重复记账)留下的:"
            "Provider 只 submit 过一次,内部台账记了两次。\n\n"
            f"重复的 attempt({len(duplicates)} 条):\n{preview}{more}\n\n"
            "**不要直接删。** 哪一行才是真的取决于厂商账单上实际收了几次,"
            "迁移脚本没有资格替人做这个判断。\n"
            "请先人工对账(保留最早那条、把恢复后的那条合并进去),"
            "再跑一次 `alembic upgrade head`。定位用:\n"
            f"{_FIND_DUPLICATES}"
        )
    op.add_column(
        "provider_usage_records",
        sa.Column("billing_key", sa.String(128), nullable=True),
    )
    op.create_unique_constraint(
        _CONSTRAINT, "provider_usage_records", ["billing_key"]
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "provider_usage_records", type_="unique")
    op.drop_column("provider_usage_records", "billing_key")
