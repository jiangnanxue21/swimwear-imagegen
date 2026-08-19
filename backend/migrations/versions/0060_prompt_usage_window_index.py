"""给提示词近期统计增加与查询条件同序的索引。

0059 的 ``(prompt_key, prompt_version, created_at)`` 适合详情页按版本聚合,
但列表页只过滤 ``prompt_key`` 与 ``created_at``。PostgreSQL 16 不能跨过未限定的
中间列 ``prompt_version`` 把尾列当成高效范围条件,会扫描该 key 的全部历史版本。

保留 0059 的索引服务逐版查询,新增 ``(prompt_key, created_at)`` 服务近七天与
未归属窗口。两种查询的列形状不同,用两条窄索引比让一条索引假装覆盖两者诚实。

Revision ID: 0060
Revises: 0059
"""
from __future__ import annotations

from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "evaluation_attempts"
_INDEX = "ix_evaluation_attempts_prompt_created_at"


def upgrade() -> None:
    op.create_index(_INDEX, _TABLE, ["prompt_key", "created_at"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
