"""候选图加业务唯一键(A45-batch12-3 / REG-03、REG-01)。

## 这一层原来是空的

`generation_candidates` 上只有三条普通索引,没有任何唯一约束。于是"同一个
外部结果被存了两遍"在库里是**完全合法**的:

    attempt_id  round_number  candidate_index
    a-1         1             0                <- 第一次落库
    a-1         1             0                <- 续跑之后又存了一遍

而这一行不会报错、不会告警,只会让候选数、排序、评分输入、影子素材对应关系
全部悄悄失真。REG-03 的复现证据就是这个形状(候选总数 2、本次续跑新增 1)。

## 为什么键是 (attempt_id, round_number, candidate_index)

`candidate_index` 是 `fetch_results()` 返回顺序里的位置。同一个
`external_task_id` 反复 fetch 拿到的是**同一批图、同样的顺序**,变的只是
那批短效签名 URL —— 所以这三列合起来正好是"这一轮的第几张图"的业务身份。

`round_number` 冗余(attempt 已经属于某一轮)但留着:它让这条约束在只看
索引定义时就能读懂,也让 `(attempt_id, round_number)` 这个常用过滤走得到索引。

考虑过 `attempt_id + source_url` 和 `attempt_id + file_hash`,都不行:
前者每次 fetch 都在变(签名不同),后者在下载失败的行上是 NULL,
而下载失败恰恰是最需要被这条约束管住的那一类。

## 约束的价值不在"防住",在"炸出来"

应用层已经改成按这三列认领已有行(`_persist_candidates`),正常路径不会撞。
这条约束管的是**下一次有人写新的落库路径时**:没有它,漏掉认领那一步的
后果是一批看不出来的重复候选;有它,后果是一次 IntegrityError。
第二种可以在测试里被发现,第一种不能。

## 存量重复怎么办:报错,不删

`upgrade()` 先查一遍重复。有的话**抛出并列出来**,不自动删。

删不得的理由是外键:`evaluations`、`review_items`、`output_assets` 都指向
`generation_candidates.id`,其中 `evaluations` 是 `ON DELETE CASCADE`。
"顺手删掉重复候选"会连带删掉挂在它上面的评分记录,而那批记录是判断
"哪一条才是有效候选"的唯一依据 —— 迁移脚本没有资格替人做这个判断。

报错信息里带了定位用的 SQL。清理完再跑一次 `alembic upgrade head` 即可。

Revision ID: 0033
Revises: 0032
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from typing import Union

revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_CONSTRAINT = "uq_generation_candidates_attempt_round_index"

#: 存量重复的定位查询。报错信息里原样给出去,让人能直接粘到 psql 里跑
_FIND_DUPLICATES = """
SELECT attempt_id, round_number, candidate_index, count(*) AS n
FROM generation_candidates
GROUP BY attempt_id, round_number, candidate_index
HAVING count(*) > 1
ORDER BY n DESC
"""


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(sa.text(_FIND_DUPLICATES)).fetchall()
    if duplicates:
        preview = "\n".join(
            f"    attempt={row[0]} round={row[1]} index={row[2]} 出现 {row[3]} 次"
            for row in duplicates[:10]
        )
        more = "" if len(duplicates) <= 10 else f"\n    ……以及另外 {len(duplicates) - 10} 组"
        raise RuntimeError(
            "generation_candidates 里已经存在重复候选,唯一约束加不上去。\n"
            "这批数据是 A45-batch12-3 / REG-03(候选落库异常提交半截数据)与 "
            "REG-01(全部下载失败后自动重生)留下的。\n\n"
            f"重复的组({len(duplicates)} 组):\n{preview}{more}\n\n"
            "**不要直接删。** 这些行可能挂着 evaluations(ON DELETE CASCADE)、"
            "review_items、output_assets,删掉候选会连带删掉判断'哪条有效'的依据。\n"
            "请先人工核对,再跑一次 `alembic upgrade head`。定位用:\n"
            f"{_FIND_DUPLICATES}"
        )
    op.create_unique_constraint(
        _CONSTRAINT,
        "generation_candidates",
        ["attempt_id", "round_number", "candidate_index"],
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "generation_candidates", type_="unique")
