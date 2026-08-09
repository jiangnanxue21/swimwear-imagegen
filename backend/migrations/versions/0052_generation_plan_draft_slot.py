"""生成方案把 ACTIVE 与 DRAFT 拆成两个唯一槽(F-17)。

Revision ID: 0052
Revises: 0051

0041 的 `status <> 'ARCHIVED'` 同时覆盖 ACTIVE 与 DRAFT。启用一份方案后,
替代方案的第一步是保存新 DRAFT,却会被旧 ACTIVE 挡成 500。这里保留原索引名
给 ACTIVE,再加一条 DRAFT 索引:每个作用域可以同时有一份生效方案和一份
正在编辑的草稿,但双击不能建出两份草稿。
"""
from __future__ import annotations

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | None = None
depends_on: str | None = None

_ACTIVE_INDEX = "uq_generation_plans_scope"
_DRAFT_INDEX = "uq_generation_plans_draft_scope"


def upgrade() -> None:
    op.execute(f"DROP INDEX {_ACTIVE_INDEX}")
    op.execute(
        f"CREATE UNIQUE INDEX {_ACTIVE_INDEX} "
        "ON generation_plans (spu_id, COALESCE(color_variant_id::text, '')) "
        "WHERE status = 'ACTIVE'"
    )
    op.execute(
        f"CREATE UNIQUE INDEX {_DRAFT_INDEX} "
        "ON generation_plans (spu_id, COALESCE(color_variant_id::text, '')) "
        "WHERE status = 'DRAFT'"
    )


def downgrade() -> None:
    # 0051 的形状无法同时表示 ACTIVE + DRAFT。降级时保留正在生效的方案,
    # 把同层草稿归档而不是删掉;正文与指纹仍在,需要时可人工复制恢复。
    op.execute(
        "UPDATE generation_plans AS draft SET status = 'ARCHIVED' "
        "WHERE draft.status = 'DRAFT' AND EXISTS ("
        "SELECT 1 FROM generation_plans AS active "
        "WHERE active.spu_id = draft.spu_id "
        "AND active.status = 'ACTIVE' "
        "AND COALESCE(active.color_variant_id::text, '') = "
        "COALESCE(draft.color_variant_id::text, '')"
        ")"
    )
    op.execute(f"DROP INDEX {_DRAFT_INDEX}")
    op.execute(f"DROP INDEX {_ACTIVE_INDEX}")
    op.execute(
        f"CREATE UNIQUE INDEX {_ACTIVE_INDEX} "
        "ON generation_plans (spu_id, COALESCE(color_variant_id::text, '')) "
        "WHERE status <> 'ARCHIVED'"
    )
