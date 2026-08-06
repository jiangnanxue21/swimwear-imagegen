"""识别硬规则落列:missing_reason 与编造计数(A45-batch14,PRD v3.1 §4.5 / §6.3)。

两列都是"真实抽取器(阶段 3)接上之后才有人写"的:

    attribute_evidence.missing_reason
        §4.5:模型对字段返回 missing_reason 时,**落 evidence 不落 value**。
        NULL = 带值的普通证据。可空且无默认 —— 给它默认值等于替模型宣布
        "所有旧证据都说过看不清",而旧证据什么都没说过。

    product_attribute_extractions.fabricated_field_count
        §6.3:模型对目标清单外的字段给出确定值 → 过滤并计数。
        NOT NULL + server_default 0:存量识别记录没数过,0 对它们是事实
        ("按当时的口径没有拦到任何编造"),不是猜测。

不动任何既有列、不动约束。§4.6 的异步化那批列(status / spu_id /
idempotency_key / input_fingerprint / requested_scope)刻意**不在**这一版:
它们要和 Celery 任务、幂等键唯一约束、取消语义一起落,拆开落一半列
只会造出一批永远 NULL 的"看起来支持异步"的字段。

Revision ID: 0036
Revises: 0035
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "attribute_evidence",
        sa.Column("missing_reason", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "product_attribute_extractions",
        sa.Column(
            "fabricated_field_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("product_attribute_extractions", "fabricated_field_count")
    op.drop_column("attribute_evidence", "missing_reason")
