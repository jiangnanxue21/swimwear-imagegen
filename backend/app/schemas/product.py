"""商品出入参。"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import (
    Audience,
    BackStyle,
    GarmentType,
    NecklineType,
    PatternType,
    ProductComplexity,
    ProductStatus,
    StrapType,
)
from app.core.field_limits import (
    MAX_SECONDARY_COLOR_LENGTH,
    MAX_SECONDARY_COLORS,
    limit_for,
)

#: 辅助颜色的规则只写一份。以前 ProductBase 上有校验、ProductUpdate 上没有,
#: 于是 POST 建商品时限制 10 个、PATCH 改商品时可以塞进任意多个任意长的值 ——
#: 同一个字段有两套规则,而宽松的那套刚好在改动路径上。
#: 长度上限统一从 app/core/field_limits 读,和数据库列宽、批量导入共用一份。
MAX_COLOR_LENGTH = MAX_SECONDARY_COLOR_LENGTH


def normalize_secondary_colors(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if len(value) > MAX_SECONDARY_COLORS:
        raise ValueError(f"辅助颜色最多 {MAX_SECONDARY_COLORS} 个")
    return [c.strip()[:MAX_COLOR_LENGTH] for c in value if c and c.strip()]


class ProductBase(BaseModel):
    spu: str = Field(..., min_length=1, max_length=limit_for("spu"))
    sku: str = Field(..., min_length=1, max_length=limit_for("sku"))
    name: str = Field(..., min_length=1, max_length=limit_for("name"))
    category: str = Field("swimwear", max_length=limit_for("category"))
    #: 受众(PRD v2 §3.4)。**默认 None = 待确认,不是 UNISEX**。
    #: 留空的商品进"待确认受众"队列(§8.2 排在待选模特之前 —— 受众确认
    #: 之后模特候选集才是对的)。不允许按类目猜一个默认值填进去(§9.1),
    #: 那会让 §4.2「AI 不得静默覆盖受众」的保护形同虚设
    audience: Audience | None = None
    primary_color: str = Field("", max_length=limit_for("primary_color"))
    secondary_colors: list[str] = Field(default_factory=list)
    pattern_type: PatternType = PatternType.SOLID
    garment_type: GarmentType = GarmentType.OTHER
    material: str = Field("", max_length=limit_for("material"))
    strap_type: StrapType = StrapType.UNKNOWN
    neckline_type: NecklineType = NecklineType.UNKNOWN
    back_style: BackStyle = BackStyle.UNKNOWN
    complexity: ProductComplexity = ProductComplexity.UNKNOWN
    notes: str | None = None

    @field_validator("secondary_colors")
    @classmethod
    def _limit_colors(cls, v: list[str]) -> list[str]:
        return normalize_secondary_colors(v) or []


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    """PATCH 语义:只更新显式传入的字段。SPU/SKU 不允许改,避免破坏已生成资产的对应关系。"""

    name: str | None = Field(None, min_length=1, max_length=limit_for("name"))
    category: str | None = Field(None, max_length=limit_for("category"))
    #: PATCH 语义:不传 = 不改。改已确认的受众要二次确认并作废已生成图片
    #: (§20.5),那道确认在界面层;这里只保证值本身合法
    audience: Audience | None = None
    primary_color: str | None = Field(None, max_length=limit_for("primary_color"))
    secondary_colors: list[str] | None = None
    pattern_type: PatternType | None = None
    garment_type: GarmentType | None = None
    material: str | None = Field(None, max_length=limit_for("material"))
    strap_type: StrapType | None = None
    neckline_type: NecklineType | None = None
    back_style: BackStyle | None = None
    complexity: ProductComplexity | None = None
    status: ProductStatus | None = None
    notes: str | None = None

    @field_validator("secondary_colors")
    @classmethod
    def _limit_colors(cls, v: list[str] | None) -> list[str] | None:
        return normalize_secondary_colors(v)


class ProductOut(ProductBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: ProductStatus
    created_at: datetime
    updated_at: datetime
    asset_count: int = 0


class ImportRowError(BaseModel):
    row_number: int
    field: str
    message: str


class ImportResultOut(BaseModel):
    created: int
    skipped_existing: int
    failed: int
    errors: list[ImportRowError] = Field(default_factory=list)
    created_ids: list[UUID] = Field(default_factory=list)


class ProductArchiveIn(BaseModel):
    """归档请求体。

    理由**必填**。归档是这套系统里最接近「删除」的动作,而它的痕迹只留在
    审计表里 —— 一条没有理由的归档记录,在三个月后复盘「这件商品当初为什么
    不做了」时等于没有。`MediaQuarantineIn` 用同一档长度,同理。
    """

    reason: str = Field(min_length=1, max_length=500)
