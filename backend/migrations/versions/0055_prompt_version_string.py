"""`evaluation_attempts.prompt_version`:Integer -> String(32)(PRD §11.3 / BE-307)。

四张落 prompt_version 的表里只有它是 int(其余三张 String(32)),跨表聚合
做不了。存量值只做 `str()` 转换,不重算 —— 历史行代表的是"当时那次调用",
不是现在的内容。

## 这份迁移原来指着一张不存在的表(a57 修)

`_TABLE` 原文写的是 `"evaluations"`。**全仓没有这张表**:评分域的四张表是
`rule_sets` / `candidate_evaluations` / `evaluation_problems` / `review_items`,
带 int 版本列的那张叫 `evaluation_attempts`(迁移 0009 建)。这份迁移在任何
真实库上都会以 `relation "evaluations" does not exist` 当场失败。

没人发现的原因写在 `docs/DECISIONS.md` §3.89 的最后一句:「迁移 0055 也只落
文件未在任何库上执行」。**一份从未被执行过的迁移,它的表名没有任何东西在校验**
—— 模型侧不校验(ORM 不知道迁移写了什么),门禁不校验(迁移链只查 head 唯一),
而 autogenerate 不会回头看手写的 alter。

顺带订正原文的第二句:它说「BE-305 之后版本值是 12 位内容哈希,int 列根本
装不下」。**对这一列不成立** —— §3.89 接的三条链路是识别 / 文案 / 出图,
评分链路的版本至今仍是 `prompt_templates.version` 那个自增序号
(`prompt_service.get_active_content` 返回它)。所以这次改型的理由只剩下第一条:
四张表同型才聚合得了,而且评分链路将来接内容哈希时不必再改一次表。

Revision ID: 0055
Revises: 0054
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "evaluation_attempts"
_COLUMN = "prompt_version"


def upgrade() -> None:
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.Integer(),
        type_=sa.String(32),
        existing_nullable=True,
        postgresql_using=f"{_COLUMN}::varchar(32)",
    )


def downgrade() -> None:
    # 有损:内容哈希("a1b2c3...")转不回整数。这条 downgrade 只对"升级后
    # 从未写入哈希值"的库是无损的;写入过的库回退前需要自行清洗该列。
    # nullif + 正则守住纯数字,非数字一律置 NULL 而不是让整条迁移炸掉。
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.String(32),
        type_=sa.Integer(),
        existing_nullable=True,
        postgresql_using=(
            f"nullif(regexp_replace({_COLUMN}, '[^0-9]', '', 'g'), '')::integer"
        ),
    )
