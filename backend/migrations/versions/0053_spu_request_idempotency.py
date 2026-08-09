"""建档请求的幂等键(PRD §9.1 / §12.1,2026-08-09 评审修复)。

## 补的是哪一格

PRD §12.1 那张 API 表里,`POST /api/spus` 后面写着「新增(**带
Idempotency-Key**)」,§9.1 又把语义写死了:

    同 key 同指纹   返回同一个 SPU
    同 key 不同指纹 409
    spu_code 唯一约束是最终裁决
    并发均返回同一对象

而在本迁移之前,`POST /api/spus` **一个字都没实现它** —— 全仓读
`Idempotency-Key` 头的地方只有 `api/workbench_batch.py` 一处。

后果不是"少一个特性"。建档是七步向导的第一步,双击提交在这里的表现是
第二次请求撞上 `uq_spus_spu_code`,运营拿到的是 `SPU 编码 X 已存在` ——
一句**在这个语境下是假的**的错误:他没有建两个 SPU,他只是点了两下。
而 AC-17 的后半句正是「双击与重发不建重复对象」。

## 两列,不是一列

    request_key          客户端给的那把键。可空 —— 不带头的调用方是合法的
    request_fingerprint  那一次的入参指纹,算法在
                         `listings/sku_matrix.spu_request_fingerprint`

只存键不存指纹的话,「同键不同入参」这一档判不出来:第二次请求会拿到
第一次建的那个 SPU,而它要的是另一个款。那种错发现得最晚 —— 要等到
有人问"为什么这个编码的商品名字不对"。

与 `batch_jobs` 的 `request_key` / `request_fingerprint` 是同一套形状,
刻意保持一致:两处的 HTTP 幂等语义相同,长得不一样只会让人以为不同。

## 唯一索引带谓词,而且只在键非空时生效

`uq_spus_request_key` 是**部分**唯一索引(`WHERE request_key IS NOT NULL`)。

不加谓词的话,全部不带键的建档行会在 NULL 上互相冲突 —— PostgreSQL 的
唯一索引对 NULL 是放行的,所以实际不会炸,但语义上那条索引会声称
"没有键也是一种键"。写出谓词是为了让读索引定义的人知道:**这条约束
只管带键的那些行。**

它同时是 §9.1 那句「并发均返回同一对象」的裁判:两个并发请求带同一把键
时,一个插入成功、另一个撞索引,撞的那一路回查赢家并复用 ——
与 `create_product` / 识别 run 的做法同构(先查后插只挡得住明显重复)。

## downgrade 能真的删干净

两列 + 一条索引,反序删除。`test_downgrade_removes_every_table` 断言的是
全部 ORM 表都被删掉,本迁移不建表,所以那条守卫在这里表现为
"降级之后 `spus` 上不该还有这两列"。

Revision ID: 0053
Revises: 0052
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "spus"
_INDEX = "uq_spus_request_key"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("request_key", sa.String(length=64), nullable=True))
    op.add_column(
        _TABLE, sa.Column("request_fingerprint", sa.String(length=64), nullable=True)
    )
    # 部分唯一索引:只有带键的行参与唯一性。理由见模块文档。
    op.create_index(
        _INDEX,
        _TABLE,
        ["request_key"],
        unique=True,
        postgresql_where=sa.text("request_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "request_fingerprint")
    op.drop_column(_TABLE, "request_key")
