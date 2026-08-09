"""建档接口的出入参(PRD v3.1 §12.1 / §6.1 步骤 1)。

## 这里**没有**视觉属性字段

阶段 1 的验收之一是「不填视觉属性即可建档」。做法不是把那 8 个字段设成可选,
而是让它们在这个接口上根本不存在:可选字段会被前端填成空串,而空串和
"还没识别"在下游是两件事 —— 前者会被当成一个确定的事实("主色是空")。

## 规则不写在这里

编码字符集、重复颜色、行数上限全部住在 `listings/sku_matrix`,schema 只管
字段类型和长度。把规则复制进 schema,它就有了两个版本,然后其中一个先过期
(`schemas/product.py` 顶部记着这类事故的原样:同一个字段 POST 有校验、
PATCH 没有)。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import Audience, SellableStatus, SpuStatus
from app.listings.sku_matrix import (
    MAX_SPU_CODE,
    MAX_VARIANT_CODE,
    MAX_VARIANTS_PER_SPU,
    SIZE_TEMPLATES,
)


class ColorVariantCreate(BaseModel):
    """建档时的一个颜色。

    `display_name`(正式颜色名称)**不在这里** —— 它是投影列,唯一写入点是
    属性服务在 VARIANT 层颜色事实(`primary_color`)被确认时写入(§4.3)。
    **不是 `standard_color_name`** —— 那个名字只出现在几句注释里,全仓没有这个字段。
    建档阶段能填的是 `working_name`:供应商口中的那个名字,内部用。
    """

    variant_code: str = Field(..., min_length=1, max_length=MAX_VARIANT_CODE)
    working_name: str = Field("", max_length=128)
    supplier_color_code: str | None = Field(None, max_length=64)


class SpuCreate(BaseModel):
    spu_code: str = Field(..., min_length=1, max_length=MAX_SPU_CODE)
    internal_name: str = Field(..., min_length=1, max_length=255)
    #: 受众**必填**,而且这里没有 `None`。§4.2:SPU 层不存在"待确认受众",
    #: 建档第一步就要选 —— 选不出来说明这个款还不该建档。
    #: `products.audience` 那个可空列是给旧路径留的,不是这里的先例
    audience: Audience
    base_category: str = Field("swimwear", max_length=64)
    supplier_ref: str | None = Field(None, max_length=128)
    notes: str | None = None
    color_variants: list[ColorVariantCreate] = Field(
        ..., min_length=1, max_length=MAX_VARIANTS_PER_SPU
    )
    #: 尺码模板名。取值必须是 `sku_matrix.SIZE_TEMPLATES` 的键 ——
    #: 校验在服务层(`sizes_of()` 会点名可选项),这里只保证长度
    size_template: str = Field(..., min_length=1, max_length=32)


class SpuPatch(BaseModel):
    expected_version: int = Field(..., ge=1)
    internal_name: str | None = Field(None, min_length=1, max_length=255)
    supplier_ref: str | None = Field(None, max_length=128)
    notes: str | None = None


class ColorVariantAdd(ColorVariantCreate):
    size_template: str | None = Field(None, min_length=1, max_length=32)


class ColorVariantPatch(BaseModel):
    expected_version: int = Field(..., ge=1)
    working_name: str | None = Field(None, max_length=128)
    supplier_color_code: str | None = Field(None, max_length=64)
    sellable_status: SellableStatus | None = None


class SkuCreateItem(BaseModel):
    color_variant_id: UUID
    size: str = Field(..., min_length=1, max_length=32)
    sku: str | None = Field(None, min_length=1, max_length=64)
    barcode: str | None = Field(None, max_length=64)
    price: Decimal | None = Field(None, ge=0)
    inventory: int | None = Field(None, ge=0)


class SkuBatchCreate(BaseModel):
    items: list[SkuCreateItem] = Field(..., min_length=1, max_length=200)


class ColorVariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    variant_code: str
    working_name: str
    display_name: str
    supplier_color_code: str | None = None
    sort_order: int
    sellable_status: SellableStatus
    row_version: int


class SpuSkuOut(BaseModel):
    """SPU 下面的一行 SKU。**只回身份与商务字段**,不回那 8 个投影列 ——
    建档视图里它们全是空的,回过去只会让前端以为"识别完了但都是空"。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    sku: str
    size: str | None = None
    size_group: str | None = None
    color_variant_id: UUID | None = None
    barcode: str | None = None
    price: Decimal | None = None
    inventory: int | None = None


class SpuOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    spu_code: str
    internal_name: str
    audience: Audience
    base_category: str
    supplier_ref: str | None = None
    status: SpuStatus
    created_by: str
    created_at: datetime
    updated_at: datetime
    row_version: int
    color_variants: list[ColorVariantOut] = Field(default_factory=list)
    sku_count: int = 0


class SpuDetailOut(SpuOut):
    skus: list[SpuSkuOut] = Field(default_factory=list)


class SizeTemplateOut(BaseModel):
    """尺码模板。给建档表单第二步用 —— 前端不许自己内置一份

    (硬规则 4 的同一条道理:前端展示后端给的东西,不推测)。
    """

    name: str
    label: str
    sizes: list[str]


def size_template_options(labels: dict[str, str]) -> list[SizeTemplateOut]:
    return [
        SizeTemplateOut(name=name, label=labels.get(name, name), sizes=list(sizes))
        for name, sizes in SIZE_TEMPLATES.items()
    ]
