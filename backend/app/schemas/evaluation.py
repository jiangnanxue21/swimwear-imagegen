"""评分与人工审核出入参(需求第十六章)。"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import EvaluationDepth, ReviewReason, ReviewStatus


class ProblemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    severity: str
    dimension: str | None
    message: str
    hard_fail: bool


class EvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID
    round_number: int
    evaluator: str
    model_name: str | None
    depth: EvaluationDepth
    hard_fail: bool
    hard_fail_codes: list[str]
    scores: dict[str, float]
    overall_score: float
    #: 评分器自报的总分。与 overall_score 的差值用来监控大模型打分漂移(需求第十章)
    model_reported_overall: float | None
    grade: str
    recommended_action: str
    grade_reasons: list[str]
    uncertain_items: list[str]
    rank_index: int | None
    similarity_score: float | None
    realism_score: float | None
    duration_ms: int | None
    created_at: datetime
    problems: list[ProblemOut] = Field(default_factory=list)


class EvaluationAttemptOut(BaseModel):
    """评分调用留痕。

    只暴露已经脱敏的排障字段；`meta` 仍留在服务端，避免以后往其中加入的
    内部诊断信息未经审查就自动出现在运营界面。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    candidate_id: UUID | None
    evaluation_id: UUID | None
    round_number: int
    outcome: str
    evaluator: str
    model_name: str | None
    depth: EvaluationDepth
    response_id: str | None
    http_status: int | None
    finish_reason: str | None
    #: 迁移 0055 起是字符串(BE-307)。前端只透传不做算术,见 types.ts 同名字段
    prompt_version: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    duration_ms: int | None
    error_code: str | None
    error_message: str | None
    diagnostic: bool = False
    created_at: datetime


class DiagnosticEvaluationOut(BaseModel):
    """人工评分测试的结构化结果；不写入正式候选评分。"""

    model_config = ConfigDict(from_attributes=True)

    evaluator: str
    model_name: str | None
    depth: EvaluationDepth
    hard_fail: bool
    hard_fail_codes: list[str]
    scores: dict[str, float]
    overall_score: float
    model_reported_overall: float | None
    grade: str
    recommended_action: str
    grade_reasons: list[str]
    uncertain_items: list[str]
    duration_ms: int | None
    problems: list[ProblemOut] = Field(default_factory=list)


class EvaluationDiagnosticOut(BaseModel):
    success: bool
    candidate_id: UUID
    billable: bool
    message: str
    evaluation: DiagnosticEvaluationOut | None = None
    attempt: EvaluationAttemptOut


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    task_id: UUID
    product_id: UUID
    best_candidate_id: UUID | None
    reason: ReviewReason
    status: ReviewStatus
    blocking: bool
    provider: str
    round_count: int
    attempt_count: int
    best_score: float | None
    best_grade: str | None
    hard_fail_codes: list[str]
    summary: str | None
    decided_by: str | None
    decision_note: str | None
    manual_tags: list[str]
    decided_at: datetime | None
    created_at: datetime
    #: 便于队列页直接显示,不必再查一次商品
    product_sku: str = ""
    product_name: str = ""
    #: ---- 受众三件套(PRD v2 §13.2 / §19)----
    #:
    #: 审核页也是**审阅页**:§13.4 第 5 条(模特受众与商品受众一致,
    #: 且呈现为成年人)要在这里逐张执行。它排在八条判断标准的中间而不是
    #: 最后 —— 一旦不成立,后面几条判断都没有意义。
    #:
    #: 与 `workbench._product_out` 同一套字段名与同一套语义:
    #: `audience=None` 是待确认(不是 UNISEX),`audience_label` 由后端算好
    #: (§21.3 文字标签),`review_focus` 从规则包下发(§19,前端不许自己维护)
    audience: str | None = None
    audience_label: str = "待确认"
    review_focus: list[str] = Field(default_factory=list)
    task_status: str = ""


class ReviewCandidateOut(BaseModel):
    """审核页展示的候选图 + 它的评分。"""

    id: UUID
    round_number: int
    candidate_index: int
    url: str
    width: int | None
    height: int | None
    seed: int | None
    status: str
    grade: str | None
    overall_score: float | None
    evaluation: EvaluationOut | None = None


class ReviewDetailOut(ReviewOut):
    """需求第十四章:商品原图、各轮最佳候选图、评分、硬错误、Provider、生成次数。"""

    product_assets: list[dict] = Field(default_factory=list)
    round_best_candidates: list[ReviewCandidateOut] = Field(default_factory=list)
    all_candidates: list[ReviewCandidateOut] = Field(default_factory=list)
    task_mode: str = ""
    max_rounds: int = 0


class ReviewDecision(BaseModel):
    note: str | None = Field(None, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class ReviewApprove(ReviewDecision):
    #: 不传则采用系统挑出的最佳候选
    candidate_id: UUID | None = None


class ReviewRegenerate(ReviewDecision):
    #: 切换 Provider(需求第十四章)。留空则沿用原 Provider。
    provider: str | None = Field(None, max_length=32)
    #: 追加轮次。任务本来就是轮次耗尽才进的队列,不加轮次会立刻弹回来。
    extra_rounds: int = Field(1, ge=1, le=5)


class RuleSetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    version: int
    description: str | None
    is_active: bool
    weights: dict
    thresholds: dict
    evaluator_options: dict
    created_at: datetime


class EvaluatorOut(BaseModel):
    name: str
    display_name: str
    configured: bool
    implemented: bool
    active: bool
