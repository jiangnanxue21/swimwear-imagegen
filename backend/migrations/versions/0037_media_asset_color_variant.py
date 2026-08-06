"""素材归属外键:media_assets.color_variant_id(A45-batch14-15,PRD v3.1 §4.8)。

## 这一列挡着什么

阶段 2 的「素材归属」。它不存在的这段时间里,**四批判定各自验完了却接不上线**:

    scope_fingerprint      双作用域样品指纹(14-9)—— AC-21 的落点
    sample_completeness    §6.2 完整度门禁的颜色作用域(14-10)
    run_state.scope_of     §11 第一行「哪个颜色全军覆没」(14-13)
    evidence_assets_for    §5.1 白名单查询助手的 scope 参数(仍欠)

四批各自留了一条「这一列落库那天我会红」的欠账守卫。本批把前三条收掉。

## 为什么是可空,而且不给默认值

存量素材的归属是**未确认**——那和「通用」不是一回事,也和「属于某个颜色」
不是一回事。给它默认成通用,每一张历史图会凭空成为全部颜色的共享证据;
给它 NOT NULL 则这条迁移在任何有存量数据的库上都升不上去。

可空的代价是下游必须处理 `None`,而那正是「通用素材只进共享作用域、
不进任何颜色子集」那条规则本来就要处理的情况(§5.3 / §6.2 / §11)。

## 为什么 SET NULL 而不是 RESTRICT

删掉一个颜色不该连坐拦住素材:素材是**实物样品的照片**,颜色是业务分组,
后者没了前者还在(那批照片还能作为 SPU 共享证据)。

与 `products.color_variant_id` 的 RESTRICT 是刻意不同的:那一列删掉会让
一个 SKU 失去身份,而这一列删掉只是让一张图退回「未归属」。

## 这一版**不含**的东西

    generation_task_id / generation_candidate_id  溯源列(§4.8)
    evidence_class 存储列 + CHECK 约束
    新去重键 (spu_id, sha256)

它们都属于阶段 2,但各自要一次数据回填或一条约束,而**这一列不需要**——
它是纯增列、可空、无默认,升级不动任何既有行。拆开是为了让这条能先走,
四批欠账能先收;它们随后续批次落,那时可以对着一个已经有归属列的库做。

Revision ID: 0037
Revises: 0036
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "media_assets",
        sa.Column("color_variant_id", PGUUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_media_assets_color_variant",
        "media_assets",
        "color_variants",
        ["color_variant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # 索引不是可选的:按颜色取证据(`evidence_assets_for(spu, scope)`)
    # 是识别、指纹、完整度门禁三条链路的取数入口,而它们都按这一列过滤
    op.create_index(
        "ix_media_assets_color_variant",
        "media_assets",
        ["color_variant_id"],
    )


def downgrade() -> None:
    # 回退顺序与建立相反。**回退会丢掉全部归属**,而归属是人工确认的结果 ——
    # 降级前要先导出这一列,否则再升上来时全库素材退回「未归属」,
    # 每个多颜色 SPU 的颜色事实会一次性全部 stale(§5.3 的指纹算不出来就算过期)
    op.drop_index("ix_media_assets_color_variant", table_name="media_assets")
    op.drop_constraint(
        "fk_media_assets_color_variant", "media_assets", type_="foreignkey"
    )
    op.drop_column("media_assets", "color_variant_id")
