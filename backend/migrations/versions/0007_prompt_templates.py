"""提示词模板表(可编辑 + 版本留档)

Revision ID: 0007
Revises: 0006
Create Date: 视觉评分器提示词可配置

提示词直接决定评分口径,所以不做原地覆盖 —— 每次保存新建一个版本,
只切换 is_active。三个月后要解释"这批图当时为什么判 C",没有历史版本就答不了。

不预置数据:代码里的 DEFAULT_SYSTEM_PROMPT 就是第 0 版。
库里没有 active 记录时评分器用它,第一次在页面上保存才产生 version=1。
这样新部署不需要跑 seed,而且"没改过提示词"和"改过又改回来了"在库里是可区分的。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prompt_templates",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("key", "version", name="uq_prompt_templates_key_version"),
    )
    op.create_index("ix_prompt_templates_key_active", "prompt_templates", ["key", "is_active"])

    # 同一个 key 只允许一条启用中的记录。用部分唯一索引在数据库层面保证,
    # 而不是靠服务层「先停用旧的再启用新的」两步操作 —— 那两步之间崩一次,
    # 就会留下两条 active,而评分器只会取到其中一条,取到哪条看排序
    op.create_index(
        "uq_prompt_templates_one_active",
        "prompt_templates",
        ["key"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("uq_prompt_templates_one_active", table_name="prompt_templates")
    op.drop_index("ix_prompt_templates_key_active", table_name="prompt_templates")
    op.drop_table("prompt_templates")
