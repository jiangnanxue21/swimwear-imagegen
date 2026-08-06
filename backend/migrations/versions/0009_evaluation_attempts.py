"""评分尝试留痕表

Revision ID: 0009
Revises: 0008
Create Date: 代码评审第 23 条 —— 评分失败没有独立的失败评估记录

`candidate_evaluations` 只在解析成功之后才写行,于是所有「没评成」的情况
—— 非法 JSON、内容安全拒绝、限流、鉴权失败、网络超时 —— 在库里不留任何痕迹。
排查时只能翻日志,而日志是滚动清理的,响应 ID 一旦丢了就没法找厂商查。

这张表把每一次评分请求都记下来,成功与失败同样对待。它刻意做得很轻:
只有排查需要的元数据和脱敏后的错误说明,不存图片也不存完整响应体。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evaluation_attempts",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("generation_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 整轮级别的失败没有具体候选图,允许为空
        sa.Column(
            "candidate_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("generation_candidates.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "evaluation_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("candidate_evaluations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("round_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("outcome", sa.String(32), nullable=False, server_default="SUCCEEDED"),
        sa.Column("evaluator", sa.String(32), nullable=False, server_default=""),
        sa.Column("model_name", sa.String(120), nullable=True),
        sa.Column("depth", sa.String(16), nullable=False, server_default="FULL"),
        sa.Column("response_id", sa.String(128), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("finish_reason", sa.String(48), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(48), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "meta", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # (task, round) 是最常用的查询:重新评分时要数\"这一轮已经失败过几次\"
    op.create_index(
        "ix_evaluation_attempts_task_round", "evaluation_attempts", ["task_id", "round_number"]
    )
    # 按结局聚合:\"上周有多少次评分是因为限流没做成的\"
    op.create_index("ix_evaluation_attempts_outcome", "evaluation_attempts", ["outcome"])
    op.create_index("ix_evaluation_attempts_created_at", "evaluation_attempts", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_evaluation_attempts_created_at", table_name="evaluation_attempts")
    op.drop_index("ix_evaluation_attempts_outcome", table_name="evaluation_attempts")
    op.drop_index("ix_evaluation_attempts_task_round", table_name="evaluation_attempts")
    op.drop_table("evaluation_attempts")
