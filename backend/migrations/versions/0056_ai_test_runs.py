"""`ai_test_runs`:AI 测试留档(PRD §11.4 / BE-309)。

在此之前,文案测试的输出一行都没存 —— 页面上那句「请保持页面打开,不要刷新」
就是这件事的自述。评分测试有 `EvaluationAttempt`,但没有任何地方能按提示词
版本把两类测试一起查出来。

迁移号:落码前按 §15 的要求复核过 `ls migrations/versions | tail -1`,
当时 head 是 `0055_prompt_version_string`。**不照抄 PRD 里的数字** ——
v1.0 写的 0032 已被占用,那正是这条要求的来路。

Revision ID: 0056
Revises: 0055
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "ai_test_runs"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        # 刻意无外键:被测对象可能先于留档被清理,而"那次测试跑过"不该跟着消失
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("prompt_key", sa.String(64), nullable=True),
        sa.Column("prompt_version", sa.String(32), nullable=True),
        sa.Column("prompt_template_version", sa.Integer(), nullable=True),
        sa.Column("generator", sa.String(64), nullable=True),
        sa.Column("model_name", sa.String(128), nullable=True),
        sa.Column("payload_in", postgresql.JSONB(), nullable=True),
        sa.Column("payload_out", postgresql.JSONB(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("billable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("actor", sa.String(64), nullable=False, server_default="system"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # 三个索引对应三条真实查询,见模型文档里那一段
    op.create_index("ix_ai_test_runs_created_at", _TABLE, ["created_at"])
    op.create_index("ix_ai_test_runs_prompt", _TABLE, ["prompt_key", "prompt_version"])
    op.create_index("ix_ai_test_runs_kind_created", _TABLE, ["kind", "created_at"])


def downgrade() -> None:
    # 无损:这张表在 0056 之前不存在,回退不会丢掉任何别处也有的数据。
    # 但它**会丢掉留档本身** —— 文案测试的输出在全仓没有第二份副本,
    # 回退等于把那些记录删干净。要留就先导出。
    op.drop_index("ix_ai_test_runs_kind_created", table_name=_TABLE)
    op.drop_index("ix_ai_test_runs_prompt", table_name=_TABLE)
    op.drop_index("ix_ai_test_runs_created_at", table_name=_TABLE)
    op.drop_table(_TABLE)
