"""评分与人工审核表:rule_sets / candidate_evaluations / evaluation_problems / review_items

Revision ID: 0003
Revises: 0002
Create Date: 阶段 4

阶段 6 的 output_assets 将在后续迁移中新增,此处不提前建表。

同阶段 3 的做法:列定义一律展开写。迁移是历史快照,
依赖可变的辅助函数会让旧迁移的语义随代码演进悄悄改变。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "rule_sets",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("weights", JSONB, server_default="{}", nullable=False),
        sa.Column("thresholds", JSONB, server_default="{}", nullable=False),
        sa.Column("evaluator_options", JSONB, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_rule_sets"),
        sa.UniqueConstraint("name", "version", name="uq_rule_sets_name"),
    )
    op.create_index("ix_rule_sets_is_active", "rule_sets", ["is_active"])

    op.create_table(
        "candidate_evaluations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("rule_set_id", UUID, nullable=True),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("evaluator", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=64), nullable=True),
        sa.Column("depth", sa.String(length=8), nullable=False),
        sa.Column("hard_fail", sa.Boolean(), nullable=False),
        sa.Column("hard_fail_codes", JSONB, server_default="[]", nullable=False),
        sa.Column("scores", JSONB, server_default="{}", nullable=False),
        sa.Column("overall_score", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("model_reported_overall", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("grade", sa.String(length=2), nullable=False),
        sa.Column("recommended_action", sa.String(length=24), nullable=False),
        sa.Column("grade_reasons", JSONB, server_default="[]", nullable=False),
        sa.Column("uncertain_items", JSONB, server_default="[]", nullable=False),
        sa.Column("rank_index", sa.Integer(), nullable=True),
        sa.Column("similarity_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("realism_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("raw_response", JSONB, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["generation_tasks.id"],
            name="fk_candidate_evaluations_task_id_generation_tasks", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["generation_candidates.id"],
            name="fk_candidate_evaluations_candidate_id_generation_candidates", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_set_id"], ["rule_sets.id"],
            name="fk_candidate_evaluations_rule_set_id_rule_sets", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_candidate_evaluations"),
    )
    op.create_index("ix_candidate_evaluations_task_id", "candidate_evaluations", ["task_id"])
    op.create_index(
        "ix_candidate_evaluations_candidate_id", "candidate_evaluations", ["candidate_id"]
    )
    op.create_index("ix_candidate_evaluations_grade", "candidate_evaluations", ["grade"])

    op.create_table(
        "evaluation_problems",
        sa.Column("id", UUID, nullable=False),
        sa.Column("evaluation_id", UUID, nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("dimension", sa.String(length=32), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("hard_fail", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["evaluation_id"], ["candidate_evaluations.id"],
            name="fk_evaluation_problems_evaluation_id_candidate_evaluations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evaluation_problems"),
    )
    op.create_index(
        "ix_evaluation_problems_evaluation_id", "evaluation_problems", ["evaluation_id"]
    )
    op.create_index("ix_evaluation_problems_code", "evaluation_problems", ["code"])

    op.create_table(
        "review_items",
        sa.Column("id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("best_candidate_id", UUID, nullable=True),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("blocking", sa.Boolean(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("round_count", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("best_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("best_grade", sa.String(length=2), nullable=True),
        sa.Column("hard_fail_codes", JSONB, server_default="[]", nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("manual_tags", JSONB, server_default="[]", nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["generation_tasks.id"],
            name="fk_review_items_task_id_generation_tasks", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"],
            name="fk_review_items_product_id_products", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["best_candidate_id"], ["generation_candidates.id"],
            name="fk_review_items_best_candidate_id_generation_candidates", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_items"),
    )
    op.create_index("ix_review_items_status", "review_items", ["status"])
    op.create_index("ix_review_items_task_id", "review_items", ["task_id"])
    op.create_index("ix_review_items_created_at", "review_items", ["created_at"])
    # 部分唯一索引:一个任务同时只能有一条待办。
    # 放在数据库层而不是应用层 —— 重试、并发派发、手工脚本都绕不过去。
    op.create_index(
        "uq_review_items_open_task",
        "review_items",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_table("review_items")
    op.drop_table("evaluation_problems")
    op.drop_table("candidate_evaluations")
    op.drop_table("rule_sets")
