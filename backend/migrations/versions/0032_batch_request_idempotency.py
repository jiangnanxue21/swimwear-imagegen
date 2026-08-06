"""批次加请求级幂等键(A45-batch12-2 / EX-06)。

## 这一层原来是空的

系统里已经有一层幂等,在 `batch_action_receipts` 上 —— 它按「动作 + 作用域 +
上游指纹」挡住重复的**付费模型调用**。那一层是对的,但它管的是批次**内部**。

「创建批次」这条 HTTP 命令本身没有任何幂等。于是:

    运营双击        两个 BatchJob,两套条目
    响应回程丢失    客户端重发,同样两个

两批条目的动作幂等键相同,所以通常不会直接多花钱(第二批会命中回执或撞上
并发锁)。但批次中心里会出现两个逻辑相同的任务、两份进度、两套审计,
而运营没有任何依据判断哪一个是有效的 —— 然后他会两个都盯着,或者两个都不信。

## 为什么是两列而不是一列

只存键的话,同一把键配不同入参时没有可判据。而那种情况必须报 409:
客户端用同一把键提交了两件不同的事,返回其中任意一个都会让它以为另一件
也做了 —— 那比多建一个批次更危险,因为它是**静默**的。

    request_key          客户端给的 `Idempotency-Key`
    request_fingerprint  动作 + 商品集合 + include_exported + label 的 sha256

## 唯一约束是这条修复的全部

服务层那句「先查有没有」是 check-then-act:两个并发请求会双双查空、
双双 INSERT。只有数据库能裁决谁赢,Python 那一层的职责是**认输之后回读**
(`create_batch` 里捕获 `IntegrityError` 再查一次),与
`generation_service` 处理生成幂等键冲突是同一个套路。

## 为什么可空

不带 `Idempotency-Key` 的调用方行为一个字不变。强制这一列意味着所有现存
客户端、脚本、测试夹具当场全红,而这条修复的价值不值那个代价。
可空 + 唯一约束在 PostgreSQL 下正好是想要的语义:NULL 互不冲突,
于是不带键的请求照旧各建各的。

Revision ID: 0032
Revises: 0031
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from typing import Union

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "batch_jobs",
        sa.Column("request_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "batch_jobs",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_batch_jobs_request_key", "batch_jobs", ["request_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_batch_jobs_request_key", "batch_jobs", type_="unique")
    op.drop_column("batch_jobs", "request_fingerprint")
    op.drop_column("batch_jobs", "request_key")
