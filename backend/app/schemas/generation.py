"""生成任务出入参。"""
from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import (
    AttemptStatus,
    CandidateStatus,
    RoutingMode,
    TaskStatus,
)
from app.core.field_limits import (
    MAX_CANDIDATE_COUNT,
    MAX_INPUT_ASSET_IDS,
    MAX_PROVIDER_PARAM_BYTES,
    MAX_PROVIDER_PARAM_KEYS,
    MIN_CANDIDATE_COUNT,
)
from app.providers.base import GenerationMode


class TaskCreate(BaseModel):
    product_id: UUID
    mode: GenerationMode = GenerationMode.VIRTUAL_TRY_ON
    provider: str | None = Field(None, description="MANUAL 路由下指定;留空用默认 Provider")
    routing_mode: RoutingMode = RoutingMode.MANUAL
    input_asset_ids: list[UUID] = Field(
        default_factory=list,
        max_length=MAX_INPUT_ASSET_IDS,
        description="留空则用该商品全部合格素材",
    )
    model_template_id: UUID | None = None
    # 上限从 `field_limits` 读:阶段租约和卡死阈值要按**这个数**估最坏耗时,
    # 两边各写各的就是 A45-batch12-5 / NEW-03(接口放 8 张,阈值按 4 张算)
    candidate_count: int = Field(
        4, ge=MIN_CANDIDATE_COUNT, le=MAX_CANDIDATE_COUNT
    )
    max_rounds: int = Field(3, ge=1, le=10)
    output_width: int | None = Field(None, ge=256, le=4096)
    output_height: int | None = Field(None, ge=256, le=4096)
    base_seed: int | None = Field(None, ge=0, le=2**31 - 1)
    prompt: str | None = Field(None, max_length=2000)
    negative_prompt: str | None = Field(None, max_length=2000)
    prompt_version: str = Field("v1", max_length=32)
    provider_params: dict = Field(
        default_factory=dict,
        description=(
            'Provider 特有参数。Mock Provider 支持 '
            '{"mock_outcome": "success|fail|timeout|empty|safety|rate_limit"};'
            'Mock 评分器支持 '
            '{"mock_evaluator": {"outcome": "auto|a|b|c|d|hard_fail|improving|stuck"}},'
            "用于离线演练 A/B/C/D 分档、硬错误与多轮重生(需求第十七章)。"
        ),
    )
    idempotency_key: str | None = Field(None, max_length=128)

    @model_validator(mode="after")
    def _bound_provider_params(self) -> TaskCreate:
        """`provider_params` 的键数与体积上限(BE-GLOBAL-07)。

        它**会进幂等指纹**。塞进一个大 JSON 的后果不是内存,是幂等失效:
        指纹每次都不同,于是"同一个任务重复提交"这道防线在最需要它的
        场景(重试、批量、网络抖动)下直接失效,而失效的表现是重复计费。
        """
        params = self.provider_params or {}
        if len(params) > MAX_PROVIDER_PARAM_KEYS:
            raise ValueError(
                f"provider_params 最多 {MAX_PROVIDER_PARAM_KEYS} 个键,收到 {len(params)} 个"
            )
        try:
            size = len(json.dumps(params, ensure_ascii=False, default=str))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"provider_params 无法序列化:{type(exc).__name__}") from exc
        if size > MAX_PROVIDER_PARAM_BYTES:
            raise ValueError(
                f"provider_params 序列化后 {size} 字节,超过上限 "
                f"{MAX_PROVIDER_PARAM_BYTES}"
            )
        return self


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    round_number: int
    attempt_number: int
    provider: str
    external_task_id: str | None
    seed: int | None
    status: AttemptStatus
    error_code: str | None
    error_message: str | None
    regeneration_reason: str | None
    candidate_count: int
    duration_ms: int | None
    created_at: datetime


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    round_number: int
    candidate_index: int
    external_id: str | None
    storage_path: str | None
    width: int | None
    height: int | None
    file_size: int | None
    seed: int | None
    status: CandidateStatus
    overall_score: float | None
    grade: str | None
    error_message: str | None
    created_at: datetime
    url: str = ""


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    model_template_id: UUID | None
    mode: GenerationMode
    provider: str
    routing_mode: RoutingMode
    candidate_count: int
    current_round: int
    max_rounds: int
    status: TaskStatus
    error_code: str | None
    error_message: str | None
    external_task_id: str | None
    idempotency_key: str
    base_seed: int | None
    prompt_version: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    #: 该状态下允许的操作,前端据此决定按钮可用性
    can_cancel: bool = False
    can_retry: bool = False


class TaskDetailOut(TaskOut):
    attempts: list[AttemptOut] = Field(default_factory=list)
    candidates: list[CandidateOut] = Field(default_factory=list)


class TaskCreateResult(BaseModel):
    task: TaskOut
    #: True 表示命中幂等,返回的是已存在的任务而非新建
    deduplicated: bool


class ProviderOut(BaseModel):
    name: str
    display_name: str
    configured: bool
    implemented: bool
    capabilities: dict


class ProviderTestResult(BaseModel):
    provider: str
    configured: bool
    reachable: bool | None
    message: str
    base_url: str | None = None
    workflow_file: str | None = None


class ModelTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    pose: str = "UNKNOWN"
    body_type: str = "UNKNOWN"
    background: str = Field("studio", max_length=64)
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None


class ModelTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    storage_path: str
    #: §10.3 模特卡片必须展示受众。它不是可以折叠进"更多信息"的字段 ——
    #: 运营选模特时看不到受众,§10.5 的硬约束就只剩后端一道
    audience: str
    pose: str
    body_type: str
    background: str
    tags: list[str]
    notes: str | None
    enabled: bool
    width: int
    height: int
    created_at: datetime
    # ---- §11 授权字段(管理员端用;普通运营端只看"可用/暂不可用") ----
    source: str = ""
    license_status: str = "UNVERIFIED"
    commercial_scope: str | None = None
    allowed_platforms: list[str] = Field(default_factory=list)
    allowed_regions: list[str] = Field(default_factory=list)
    license_start_at: datetime | None = None
    license_expires_at: datetime | None = None
    allow_ai_dressing: bool = False
    allow_derivative_images: bool = False
    age_verified: bool = False
    prohibited_categories: list[str] = Field(default_factory=list)
    disabled_reason: str | None = None
    url: str = ""
