"""素材库的请求/响应体(M1)。"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import MediaRole


class MediaAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    spu: str | None = None
    source: str
    role: str | None = None
    role_source: str
    role_confidence: float | None = None
    variant_hint: str | None = None
    #: 这张图归属到哪个颜色(§4.8 的归属外键,迁移 0037)。空 = 通用图。
    #:
    #: **和 `variant_hint` 不是一回事,别在前端把两者合并。** 那一列是
    #: 「识别建议位」,模型猜的;这一列是人在上传时指定的归属,而
    #: §6.2 颜色作用域完整度、§5.3 颜色指纹读的都是这一列。
    #: 合并的表现是模型猜错一次,那个颜色的门禁与事实过期判定跟着错,
    #: 而两处都不报错 —— `run_state.scope_of` 拒绝读 hint 是同一条理由,
    #: 那里已经拒过三次。
    color_variant_id: UUID | None = None
    storage_path: str
    mime_type: str
    width: int
    height: int
    bytes: int
    sha256: str
    status: str
    quality_json: dict[str, Any] | None = None
    quarantine_reason: str | None = None
    #: 迁移期溯源。前端据此提示"这条是从旧表带过来的,角色映射可能有损"
    legacy_kind: str | None = None
    created_at: datetime | None = None
    #: 短期签名地址。**不是永久公开地址** —— 素材落在私有前缀下
    url: str = ""


class MediaRoleIn(BaseModel):
    role: MediaRole


class MediaQuarantineIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
