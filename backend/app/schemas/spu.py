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
    code_rules,
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


class SpuPreviewIn(BaseModel):
    """建档试算的入参。**是 `SpuCreate` 的一个子集,不是它的兄弟。**

    只留下会影响展开结果的三项。受众、内部名称、供应商号一个都不要 ——
    它们不参与 `expand()`,收进来只会让前端以为"试算过了就能建成"。

    `size_template` 在这里**可空**,而在 `SpuCreate` 里必填。理由是表单第二步:
    那一步颜色已经填完、模板还没选,而颜色码合不合法此刻就该有答案。
    空着时试算只跑 `normalize_variant_codes()`(与 `expand()` 内部同一个函数),
    答得出编码字符集、重复、数量,答不出行数上限 —— 后者要两个数才算得出,
    所以出参里的 `sku_count` 那时是 `null`,不是 0。

    上限与 `SpuCreate` 用同一组常量:试算比建档宽松一点点的话,那点差额里
    的输入会在试算时说"没问题"、提交时被拒,而这正是试算要消灭的东西。
    """

    spu_code: str = Field(..., min_length=1, max_length=MAX_SPU_CODE)
    color_variants: list[ColorVariantCreate] = Field(
        default_factory=list, max_length=MAX_VARIANTS_PER_SPU
    )
    size_template: str | None = Field(None, min_length=1, max_length=32)


class PlannedSkuOut(BaseModel):
    """试算出来的一行。**不是数据库行** —— 它还没存在。"""

    sku: str
    variant_code: str
    size: str
    size_group: str


class SpuPreviewOut(BaseModel):
    """试算结果。

    `sku_count` 可空:没给尺码模板时算不出行数,那时它是 `null`。
    **不要退化成 0** —— 界面上 0 和"算不出"长得一样,而 0 会被读成
    "这次建档一行 SKU 都不会产生"。
    """

    spu_code: str
    variant_codes: list[str] = Field(default_factory=list)
    size_template: str | None = None
    sizes: list[str] = Field(default_factory=list)
    skus: list[PlannedSkuOut] = Field(default_factory=list)
    sku_count: int | None = None
    #: 这个编码已经被占了。**是标志位不是错误** —— 试算是运营边打字边跑的,
    #: 把它做成 422 的话,`SW-0` 打到一半就会红一次,而那时他还没打完
    code_taken: bool = False


class CodeRulesOut(BaseModel):
    """编码规则。给建档表单显示提示与做输入归一化用。

    存在的理由与 `SizeTemplateOut` 逐字相同(硬规则 4):前端内置一份的话,
    改一次上限要改两个仓库,而漏改的那一侧不报错 —— 它只会安静地按旧数字
    提示,然后运营照着提示填,被后端拒。

    **它不是校验器。** 前端拿它写提示文案、拿 `normalizes_to_uppercase`
    在失焦时做同一个归一化;判定仍然只有 `listings/sku_matrix` 一处,
    走试算端点问。
    """

    separator: str
    allowed_charset: str
    spu_code_allows_separator: bool
    normalizes_to_uppercase: bool
    max_spu_code: int
    max_variant_code: int
    max_size_code: int
    max_variants_per_spu: int
    max_skus_per_expansion: int


class SpuDisableIn(BaseModel):
    """停用请求体。

    理由**必填**,判据与 `ProductArchiveIn` 逐字相同:停用是这套系统里
    最接近「删除一个款」的动作,而它的痕迹只留在审计表里 —— 一条没有理由的
    停用记录,在三个月后复盘「这个款当初为什么不做了」时等于没有。
    """

    reason: str = Field(min_length=1, max_length=500)


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
    #: 建档备注。**出参里必须有它,`SpuPatch` 才编辑得了它** ——
    #: 读不到就改,那不是编辑是覆盖:表单打开时是空的,保存一次就把
    #: 别人写的备注抹掉了,而没有任何地方会报错
    notes: str | None = None
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


def code_rules_out() -> CodeRulesOut:
    """`sku_matrix.code_rules()` -> 出参。**这里不许出现字面量。**

    `model_validate` 而不是逐个字段抄:抄的话新增一条规则要改两个地方,
    而漏改的那一侧不报错 —— 它只会少报一条规则,然后前端少提示一句。
    多出来的键(`size_templates`)由 pydantic 默认忽略,那一份走
    `/spus/size-templates`,不在这个出参里重复一遍。
    """
    return CodeRulesOut.model_validate(code_rules())
