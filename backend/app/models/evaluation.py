"""评分与人工审核模型(需求第八、十、十二章)。

阶段 4 建四张表:

    rule_sets              分档规则,允许运行时调整阈值与权重
    candidate_evaluations  一张候选图一次评分
    evaluation_problems    评分发现的问题明细
    review_items           人工审核队列;审核对象是**任务**,不是候选图

阶段 6 的 output_assets 仍不提前建。
"""
from __future__ import annotations

# datetime 必须运行时 import:SQLAlchemy 2 建映射时会解析 Mapped[...] 注解字符串
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import EvaluationDepth, EvaluationOutcome, ReviewStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RuleSet(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """分档规则集(需求第十二章:允许通过数据库中的 RuleSet 调整)。

    只保留一条 `is_active`,而不是给每个任务绑一个规则集:
    分档标准是全站口径,不同任务用不同标准会让"通过率"这个指标失去意义。
    改规则时新建一个版本并切换 active,旧版本留档,便于解释历史判定。
    """

    __tablename__ = "rule_sets"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_rule_sets_name"),
        Index("ix_rule_sets_is_active", "is_active"),
    )

    name: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: 维度权重,总分由后端按它计算(需求第十章)
    weights: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: A/B/C/D 阈值与开关,结构见 evaluators.rules.GradingThresholds
    thresholds: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: 评分器特有参数,例如 Mock 评分器的演示开关
    evaluator_options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


class CandidateEvaluation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一张候选图的一次评分。

    同一张候选图可能被评两次(先快速检查、后补完整评分),因此不对 candidate_id
    做唯一约束,而是靠 depth + created_at 区分。取"最终结论"时取最新那条。
    """

    __tablename__ = "candidate_evaluations"
    __table_args__ = (
        Index("ix_candidate_evaluations_task_id", "task_id"),
        Index("ix_candidate_evaluations_candidate_id", "candidate_id"),
        Index("ix_candidate_evaluations_grade", "grade"),
    )

    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("generation_candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    rule_set_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("rule_sets.id", ondelete="SET NULL"), nullable=True
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    evaluator: Mapped[str] = mapped_column(String(32), nullable=False, default="mock")
    model_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    depth: Mapped[str] = mapped_column(
        String(8), nullable=False, default=EvaluationDepth.FULL.value
    )

    hard_fail: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hard_fail_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    scores: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: 后端按权重算出的总分,这才是分档依据
    overall_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    #: 评分器自报的总分,只留档用于观察打分漂移(需求第十章)
    model_reported_overall: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)

    grade: Mapped[str] = mapped_column(String(2), nullable=False, default="D")
    recommended_action: Mapped[str] = mapped_column(String(24), nullable=False, default="REJECT")
    grade_reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    uncertain_items: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    #: 轮内预排序结果(需求第十三章),关闭预排序时为空
    rank_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    similarity_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    realism_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)

    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 评分器原始返回,便于事后复盘"模型当时到底说了什么"
    raw_response: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    problems: Mapped[list[EvaluationProblem]] = relationship(
        back_populates="evaluation", cascade="all, delete-orphan"
    )


class EvaluationProblem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """评分发现的单条问题。修复策略按 code 查表(evaluators/repair.py)。"""

    __tablename__ = "evaluation_problems"
    __table_args__ = (
        Index("ix_evaluation_problems_evaluation_id", "evaluation_id"),
        Index("ix_evaluation_problems_code", "code"),
    )

    evaluation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("candidate_evaluations.id", ondelete="CASCADE"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="MINOR")
    dimension: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: 是否属于需求第十一章的硬错误
    hard_fail: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    evaluation: Mapped[CandidateEvaluation] = relationship(back_populates="problems")


class ReviewItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """人工审核队列项。

    需求第十二章:人工审核的对象是**商品任务**,不是全部低分候选图。
    因此这张表按任务建行,不按候选图建行。

    同一个任务不允许有两条待办 —— 靠部分唯一索引在数据库层保证,
    而不是靠应用代码"记得先查一下"。重试、并发、手工调用都绕不过去。
    """

    __tablename__ = "review_items"
    __table_args__ = (
        Index("ix_review_items_status", "status"),
        Index("ix_review_items_task_id", "task_id"),
        Index("ix_review_items_created_at", "created_at"),
        Index(
            "uq_review_items_open_task",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    #: 每轮最佳候选中的最佳一张,审核页默认展示它
    best_candidate_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("generation_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )

    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ReviewStatus.PENDING.value
    )
    #: 抽检项不阻塞任务,任务保持 AUTO_APPROVED 继续走后续流程
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    round_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    best_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    best_grade: Mapped[str | None] = mapped_column(String(2), nullable=True)
    #: 历轮出现过的硬错误代码汇总,审核页一眼能看出卡在哪
    hard_fail_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 人工标签,用于将来沉淀训练/复盘样本
    manual_tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)


class EvaluationAttempt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一次**评分请求**的留痕,成功与失败都记。

    为什么不能只有 `candidate_evaluations`:那张表只在解析成功之后才写行。
    于是所有\"没评成\"的情况 —— 模型返回非法 JSON、被内容安全拒绝、限流、
    鉴权失败、网络超时 —— 在库里**不留任何痕迹**。事后想回答几个很基本的问题:

        上周有多少次评分是因为限流没做成的?
        那批解析失败的响应,响应 ID 是多少,能不能找厂商查?
        换了提示词之后解析失败率有没有下降?

    一个都答不了,只能翻日志,而日志是滚动清理的。

    这张表刻意做得比 `candidate_evaluations` 轻:只存排查需要的元数据和
    **脱敏后**的错误信息,不存图片、不存完整响应体。成功的那次评分详情仍然
    在 `candidate_evaluations` 里,这里只留一条指针。
    """

    __tablename__ = "evaluation_attempts"
    __table_args__ = (
        Index("ix_evaluation_attempts_task_round", "task_id", "round_number"),
        Index("ix_evaluation_attempts_outcome", "outcome"),
        Index("ix_evaluation_attempts_created_at", "created_at"),
        # 详情页按 (key, version) 收逐版统计;这一条保留三列顺序服务它。
        Index(
            "ix_evaluation_attempts_prompt",
            "prompt_key",
            "prompt_version",
            "created_at",
        ),
        # 列表页只给 key 与 created_at。把 version 插在中间时,PostgreSQL 16
        # 只能扫完该 key 的全部版本再过滤时间,历史越长打开列表越慢。
        Index(
            "ix_evaluation_attempts_prompt_created_at",
            "prompt_key",
            "created_at",
        ),
    )

    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="CASCADE"), nullable=False
    )
    #: 整轮级别的失败(例如\"这一轮全部评分都没做成\")没有具体候选图,允许为空
    candidate_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("generation_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: 成功时指向落库的那条评分,便于从失败率反查具体分数
    evaluation_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("candidate_evaluations.id", ondelete="SET NULL"),
        nullable=True,
    )

    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    outcome: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EvaluationOutcome.SUCCEEDED.value
    )
    evaluator: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    #: 实际响应的模型名。由后端从 HTTP 响应里取,不采信模型自报
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    depth: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EvaluationDepth.FULL.value
    )

    #: 厂商侧的响应标识。找厂商查一次异常调用时,这是唯一的凭据
    response_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)

    #: 当时生效的提示词版本。评分口径变化和失败率变化要能对得上。
    #:
    #: **String 而不是 Integer**(迁移 0055 / BE-307):四张落 prompt_version
    #: 的表里原来只有这一张是 int,跨表聚合做不了。今天这一列装的仍是
    #: `prompt_templates.version` 那个自增序号(评分链路还没接内容哈希),
    #: 但类型先归一 —— 接的那天不必再改一次表,也不必再写一次有损 downgrade。
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: 当时用的是**哪一份**提示词(迁移 0059 / BE-302)。
    #:
    #: 单有上面那一列归不了属:版本号是 `max(version) WHERE key=?` **按 key 各自**
    #: 自增的,于是女装 v3 与男装 v3 都存成 `"3"`。按版本号聚合会把两份提示词的
    #: 调用次数加在一起,而这张统计要回答的恰恰是「换了这一份之后失败率有没有下降」。
    #:
    #: **允许为空,且空值不许兜底成女装。** 三种情况会留空:迁移之前的历史行、
    #: 提示词读取失败退回内置默认的降级路径(`_prompt_context` 返回 {})、
    #: 受众推不出来。把它们记成女装等于往统计里掺假归属,而掺进去之后
    #: 没有任何东西能把它们分出来 —— 统计侧因此单独报一个 `unattributed`,
    #: 不把未归属的调用悄悄算进任何一份提示词的分母。
    prompt_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: 归一化的错误码(ErrorCode 的取值),便于按类聚合
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    #: **脱敏后**的说明。只放我们自己生成的文案,不放第三方异常原文 ——
    #: 那里面可能带着 URL 里的凭据
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 其余可安全留档的元信息(api_style、response_format、图片摘要……)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    @property
    def diagnostic(self) -> bool:
        """是否由人工能力测试触发，而不是生成任务的自动评分阶段。"""

        return bool((self.meta or {}).get("diagnostic"))
