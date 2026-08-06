"""生成任务相关表:model_templates / generation_tasks / generation_attempts /
generation_candidates / provider_usage_records

Revision ID: 0002
Revises: 0001
Create Date: 阶段 3

阶段 4 的评分表(candidate_evaluations / evaluation_problems / review_items / rule_sets)
与阶段 6 的 output_assets 将在后续迁移中新增,此处不提前建表。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
UUID = postgresql.UUID(as_uuid=True)


# 迁移是历史快照,列定义一律展开写。依赖可变的辅助函数会让旧迁移的语义
# 随着代码演进悄悄改变,这是迁移里最难排查的一类问题。


def upgrade() -> None:
    op.create_table(
        "model_templates",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("pose", sa.String(length=32), nullable=False),
        sa.Column("body_type", sa.String(length=32), nullable=False),
        sa.Column("background", sa.String(length=64), nullable=False),
        sa.Column("tags", JSONB, server_default="[]", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_model_templates"),
    )
    op.create_index("ix_model_templates_enabled", "model_templates", ["enabled"])

    op.create_table(
        "generation_tasks",
        sa.Column("id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("model_template_id", UUID, nullable=True),
        sa.Column("input_asset_ids", JSONB, server_default="[]", nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("routing_mode", sa.String(length=16), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("current_round", sa.Integer(), nullable=False),
        sa.Column("max_rounds", sa.Integer(), nullable=False),
        sa.Column("output_width", sa.Integer(), nullable=True),
        sa.Column("output_height", sa.Integer(), nullable=True),
        sa.Column("base_seed", sa.Integer(), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("negative_prompt", sa.Text(), nullable=True),
        sa.Column("provider_params", JSONB, server_default="{}", nullable=False),
        sa.Column("external_task_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["product_id"], ["products.id"],
            name="fk_generation_tasks_product_id_products", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_template_id"], ["model_templates.id"],
            name="fk_generation_tasks_model_template_id_model_templates", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generation_tasks"),
        sa.UniqueConstraint("idempotency_key", name="uq_generation_tasks_idempotency_key"),
    )
    op.create_index("ix_generation_tasks_product_id", "generation_tasks", ["product_id"])
    op.create_index("ix_generation_tasks_status", "generation_tasks", ["status"])
    op.create_index("ix_generation_tasks_created_at", "generation_tasks", ["created_at"])

    op.create_table(
        "generation_attempts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_task_id", sa.String(length=128), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column("request_payload", JSONB, server_default="{}", nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("regeneration_reason", sa.String(length=32), nullable=True),
        sa.Column("repair_strategy", JSONB, nullable=True),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["generation_tasks.id"],
            name="fk_generation_attempts_task_id_generation_tasks", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generation_attempts"),
    )
    op.create_index("ix_generation_attempts_task_id", "generation_attempts", ["task_id"])
    op.create_index("ix_generation_attempts_provider", "generation_attempts", ["provider"])

    op.create_table(
        "generation_candidates",
        sa.Column("id", UUID, nullable=False),
        sa.Column("task_id", UUID, nullable=False),
        sa.Column("attempt_id", UUID, nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("candidate_index", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("source_url", sa.String(length=1024), nullable=True),
        sa.Column("storage_path", sa.String(length=512), nullable=True),
        sa.Column("file_hash", sa.String(length=64), nullable=True),
        sa.Column("mime_type", sa.String(length=64), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("overall_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("grade", sa.String(length=2), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("candidate_metadata", JSONB, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["generation_tasks.id"],
            name="fk_generation_candidates_task_id_generation_tasks", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["generation_attempts.id"],
            name="fk_generation_candidates_attempt_id_generation_attempts", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_generation_candidates"),
    )
    op.create_index("ix_generation_candidates_task_id", "generation_candidates", ["task_id"])
    op.create_index("ix_generation_candidates_attempt_id", "generation_candidates", ["attempt_id"])
    op.create_index("ix_generation_candidates_status", "generation_candidates", ["status"])

    op.create_table(
        "provider_usage_records",
        sa.Column("id", UUID, nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("task_id", UUID, nullable=True),
        sa.Column("attempt_id", UUID, nullable=True),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=48), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["generation_tasks.id"],
            name="fk_provider_usage_records_task_id_generation_tasks", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provider_usage_records"),
    )
    op.create_index("ix_provider_usage_records_provider", "provider_usage_records", ["provider"])
    op.create_index("ix_provider_usage_records_created_at", "provider_usage_records", ["created_at"])


def downgrade() -> None:
    op.drop_table("provider_usage_records")
    op.drop_table("generation_candidates")
    op.drop_table("generation_attempts")
    op.drop_table("generation_tasks")
    op.drop_table("model_templates")
