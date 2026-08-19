"""AI 测试留档(PRD §11.4 / BE-309,迁移 0056)。

## 为什么这张表可以入库,而运行日志不可以

`docs/LOG-CONSOLE.md` §九写着「运行日志不入库」。那条禁令反对的是**把高频
自动写入塞进 OLTP 库**,判据是触发方式与量级,不是"它是不是观测数据":

    运行日志    系统自己触发,每次评分 ≈ 12 条,寿命是分钟到小时
    AI 测试留档 人点按钮触发(还要勾一次费用确认),每天个位数,寿命 180 天

两条判据都不成立时那条禁令不适用。这一段在 PRD §11.5 与
`docs/DECISIONS.md` 里各留了一笔 —— 不写的话,下一个人会拿 §九来否掉这张表。

## 与 `EvaluationAttempt` 的分工

评分测试的正文在 `EvaluationAttempt`,这里只存指针(`payload_out.attempt_id`)
与索引字段;文案测试今天一行都没存,所以这里存全文。取舍与理由在
`app/workflows/ai_test_record.py` 的模块文档,那也是**唯一**构造这些行的地方。

## 索引按查询形状建,不按列的重要性建

FE-314 要的是双向互链:提示词详情页问「这一版跑过哪些测试」
(`prompt_key` + `prompt_version`),历史表问「最近跑了什么」(`created_at` 倒序),
BE-311 的过滤按 `kind`。三条查询各一个索引,没有第四个 —— 一张每天进个位数
行的表,多一个索引的维护成本高于它省下的那次扫描。
"""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AiTestRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "ai_test_runs"
    __table_args__ = (
        Index("ix_ai_test_runs_created_at", "created_at"),
        Index("ix_ai_test_runs_prompt", "prompt_key", "prompt_version"),
        Index("ix_ai_test_runs_kind_created", "kind", "created_at"),
    )

    #: 'evaluation' | 'copy'。留成字符串而不是枚举:它标的是"哪条链路",
    #: 加第三条链路(比如出图测试)时不该动表结构
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 不加外键:被测对象可能在留档之后被清理(候选图会随任务清理走),
    #: 而"那次测试跑过"这件事不该跟着消失。悬空的 id 比消失的档案好 ——
    #: 前者查不到对象,后者连查都无从查起
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )

    #: 用的是哪一处提示词、哪一版正文。**这两列是这张表存在的理由** ——
    #: 没有它们,一条留档只能回答"跑出过什么",答不了"那是哪一版跑的"
    prompt_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 内容哈希,与 `prompts/versioning.content_version` 同源(§11.3)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 人可读序号(`prompt_templates.version`)。与上面那列并存的理由见
    #: `versioning.py`:前者答"这是第几版",后者答"用的到底是哪段文本"
    prompt_template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    generator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: 已脱敏。构造点只有 `workflows/ai_test_record.py`,那里过
    #: `llm.redaction.safe_payload_for_log` —— 新开一个去向最容易漏的
    #: 就是在新去向上把老规矩忘了
    payload_in: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    payload_out: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: 这次测试有没有真的花钱。失败也可能计费,所以它与 `success` 独立
    billable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
