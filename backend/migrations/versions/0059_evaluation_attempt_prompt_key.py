"""`evaluation_attempts.prompt_key`:给每次评分留下「用的是哪一份提示词」。

## 为什么只有版本号不够

`prompt_version` 这一列装的是 `prompt_templates.version`,而那个序号是

    max(version) WHERE key = ?   +1

**按 key 各自自增**的(`prompt_service._next_version`)。于是女装的第 3 版与男装的
第 3 版在这张表里都写成 `"3"`,两者在库里**一个字节都不差**。

BE-302 的调用统计要按提示词逐份回答「近 7 天调用了多少次、解析失败率多少」,
而 FE-306 要逐版回答同样的问题。只按 `prompt_version` 聚合的话,两份提示词的
调用会加在一起 —— 得到的那个数看起来完全正常,却恰好是这张统计要回答的问题的
反面:「换了**这一份**之后失败率有没有下降」,答案里掺着另一份的调用。

## 为什么不回填

历史行的归属在库里**不存在第二个副本**。能想到的两条回填路径都不成立:

    按受众反推   evaluation_attempts -> generation_tasks -> products.audience
                 商品的受众**会被改**,改完之后反推出来的是今天的受众,
                 不是当时评分用的那一份
    按版本号猜   两份提示词的版本号空间重叠(上面那段),猜不出来

所以历史行**留 NULL**,统计侧把它们单独计成 `unattributed` 报出去,而不是
悄悄算进任何一份提示词的分母。这与 a63 那条订正是同一条判据:序号转不动时
返回 None,不兜底成 0 —— **一个编出来的归属混进统计之后,没有任何东西能再把
它分出来**,而它长得和真的一模一样。

代价是这张统计有一个**起点**:0059 执行之前的调用不在里面。FE-301 的窗口是
近 7 天,所以那一列上线一周后就自然填满;FE-306 的逐版统计对更早的版本会偏少,
界面上因此如实写「统计自 X 起」,不假装它覆盖了全部历史。

## 索引

`(prompt_key, prompt_version, created_at)` 一条覆盖两种查询:列表页按 key 收
近 7 天(前缀 + 时间窗),详情页按 (key, version) 逐版收。这张表每次评分都写一行
(一轮 4 张候选 × 最多 3 轮),不带索引的话统计会随用量线性变慢,而它挂在
一个每次打开列表页都要跑的接口上。

Revision ID: 0059
Revises: 0058
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "evaluation_attempts"
_INDEX = "ix_evaluation_attempts_prompt"


def upgrade() -> None:
    # 可空且**不回填**(见模块文档)。宽度 64 与 `prompt_templates.key` 同量级,
    # 注册表里最长的是 `generation_prompt_compose`(25)—— 留足余量,
    # 加一个 key 不该触发一次迁移(后端 CLAUDE.md「枚举」那条的同一判据)
    op.add_column(
        _TABLE,
        sa.Column("prompt_key", sa.String(length=64), nullable=True),
    )
    op.create_index(_INDEX, _TABLE, ["prompt_key", "prompt_version", "created_at"])


def downgrade() -> None:
    # 索引先删再删列:反过来 PostgreSQL 会连带把索引删掉,而 alembic 这边
    # 仍然记着一条它以为还在的索引 —— 再 upgrade 时 create_index 撞名
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "prompt_key")
