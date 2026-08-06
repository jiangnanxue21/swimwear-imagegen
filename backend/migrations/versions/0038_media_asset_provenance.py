"""素材溯源列:generation_task_id / generation_candidate_id(A45-batch14-16,§4.8)。

## 这两列挡着什么

「这张图是本系统生成出来的吗、是哪一次生成的哪一张候选」。在它们之前,
这个问题由 `legacy_kind='generation_candidate'` 回答 —— 那是迁移期的过渡记号。

三个判定早就写好在读这两列(`getattr` 取,列不存在时退回 None):

    evidence_rules.derive_evidence_class   §4.8 规则表的第一条
    provenance_conflict.verdict            AC-22「AI 图伪装拦截」
    provenance_conflict.may_fill_role      去重命中那一路的补角色闸

后两条在这两列落库之前**只有过渡记号那一路真的会命中** —— §11 那一批
自己写下过这句话,并用一条守卫把它钉成了断言。本批把它收掉。

## 为什么不回填

存量的 AI 影子行有 `legacy_kind` / `legacy_id`,理论上能回填。**不做**:

    回填要 join 候选表并信任 legacy_id 指的就是候选行 —— 而那是一个
    迁移期约定,不是外键;`legacy_id` 上没有任何约束保证它指得到。
    回填错的表现是**一张人工上传的样品被标成 AI 生成**,
    从此永久掉出识别输入,而没有任何地方会说为什么。

判定认「任一信号命中即算带溯源」,所以过渡记号那一路照旧生效 ——
存量行不回填也不会漏判。新行两条都写。

## SET NULL 的代价写在模型注释里

清理生成任务不该连坐删掉素材文件。溯源断了之后那张图仍然被
`source=AI_GENERATED` 拦着;真正靠这两列拦的是**被人重新上传成
MANUAL_UPLOAD 的那一份**,而它指着的是候选行,候选行不随任务删除消失。

Revision ID: 0038
Revises: 0037
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_COLUMNS = (
    ("generation_task_id", "generation_tasks"),
    ("generation_candidate_id", "generation_candidates"),
)


def upgrade() -> None:
    for column, target in _COLUMNS:
        op.add_column(
            "media_assets", sa.Column(column, PGUUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            f"fk_media_assets_{column}",
            "media_assets",
            target,
            [column],
            ["id"],
            ondelete="SET NULL",
        )
        # 索引不是可选的:§5.1 的白名单查询要按 `generation_task_id IS NULL`
        # 过滤,而那是识别取数的唯一入口
        op.create_index(f"ix_media_assets_{column}", "media_assets", [column])


def downgrade() -> None:
    for column, _ in reversed(_COLUMNS):
        op.drop_index(f"ix_media_assets_{column}", table_name="media_assets")
        op.drop_constraint(f"fk_media_assets_{column}", "media_assets", type_="foreignkey")
        op.drop_column("media_assets", column)
