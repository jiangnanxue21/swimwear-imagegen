"""素材出参。"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core import audience as audience_rules
from app.core.enums import (
    AssetType,
    Audience,
    ModelBodyType,
    ModelLicenseStatus,
    ModelPose,
)


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    asset_type: AssetType
    file_hash: str
    mime_type: str
    file_size: int
    width: int
    height: int
    storage_path: str
    original_filename: str
    check_passed: bool
    check_message: str | None
    created_at: datetime
    url: str = ""


class AssetUploadResult(BaseModel):
    asset: AssetOut
    deduplicated: bool


class ModelTemplateForm(BaseModel):
    """模特模板的表单字段。

    以前这些值从 Form 直接写进数据库,没有长度、枚举、数量校验 ——
    name 可以是 10 万字(撞 VARCHAR(128) 变成 500),pose 可以是任意字符串
    (界面上显示成一个谁也不认识的标签)。表单不是「可信输入」,
    它和 JSON body 一样来自浏览器。
    """

    name: str = Field(..., min_length=1, max_length=128)
    #: **必填,无默认值**(PRD v2 §10.5)。给它一个默认(比如 WOMEN)会让
    #: 男装模特被顺手传成女装,而那个错误方向恰好是危险的一侧:男装商品的
    #: 候选列表里冒出女性模特。宁可让上传表单报"缺字段"。
    #: 取值不含 UNISEX —— 那是商品的取值,模特本人总有受众(见 Audience 文档)。
    audience: Audience
    pose: ModelPose = ModelPose.UNKNOWN
    body_type: ModelBodyType = ModelBodyType.UNKNOWN
    background: str = Field("studio", max_length=64)
    tags: list[str] = Field(default_factory=list)
    notes: str | None = Field(None, max_length=2000)

    # ---- §11 授权字段组。缺省一律"未核实",不替人签字 ----
    source: str = Field("", max_length=128)
    license_status: ModelLicenseStatus = ModelLicenseStatus.UNVERIFIED
    commercial_scope: str | None = Field(None, max_length=2000)
    allowed_platforms: list[str] = Field(default_factory=list)
    allowed_regions: list[str] = Field(default_factory=list)
    license_start_at: datetime | None = None
    license_expires_at: datetime | None = None
    allow_ai_dressing: bool = False
    allow_derivative_images: bool = False
    #: §11.1 年龄确认。默认 false = "没人核实过",**不是**"未成年"。
    #: 泳衣 + AI 生成人像是所有服饰品类里这一条最敏感的组合,
    #: 默认 true 等于替不存在的核实签字
    age_verified: bool = False
    prohibited_categories: list[str] = Field(default_factory=list)

    @field_validator("audience")
    @classmethod
    def _reject_unisex(cls, v: Audience) -> Audience:
        if v is Audience.UNISEX:
            raise ValueError("UNISEX 是商品的受众取值,不是模特的;请选择 WOMEN 或 MEN")
        return v

    @model_validator(mode="after")
    def _body_type_matches_audience(self) -> ModelTemplateForm:
        """§10.4:体型词表按受众过滤。

        "给男装模特打上 CURVY 不应该是一个可以做到的操作" —— 校验在这里
        和 service 层各一道:这里让界面就近报错,service 那道守住非表单路径。
        """
        allowed = audience_rules.body_types_allowed(self.audience)
        if self.body_type.value not in allowed:
            raise ValueError(
                f"体型 {self.body_type.value} 不在{self.audience.value}词表里,"
                f"可选:{', '.join(allowed)}"
            )
        return self

    @model_validator(mode="after")
    def _license_window_is_ordered(self) -> ModelTemplateForm:
        start, end = self.license_start_at, self.license_expires_at
        if start and end and end <= start:
            raise ValueError("授权到期时间必须晚于开始时间")
        return self

    @field_validator("tags")
    @classmethod
    def _limit_tags(cls, v: list[str]) -> list[str]:
        if len(v) > 20:
            raise ValueError("标签最多 20 个")
        return [t.strip()[:32] for t in v if t and t.strip()]

    @field_validator("allowed_platforms", "allowed_regions", "prohibited_categories")
    @classmethod
    def _limit_scope_lists(cls, v: list[str]) -> list[str]:
        if len(v) > 50:
            raise ValueError("范围列表最多 50 项")
        return [x.strip()[:64] for x in v if x and x.strip()]


class ModelTemplateUpdateForm(BaseModel):
    """模特模板的**可改字段**(PRD v2 §11)。PATCH 语义:不传 = 不改。

    ## 为什么必须有这个 PATCH

    §11 的十二个授权字段后端能收、能存、`model_license.assert_usable` 也
    真的会读其中四个 —— 但在这个 schema 之前,创建表单一个都不发,而且
    **没有任何更新入口**。于是 `license_status` 永远停在 `UNVERIFIED`,
    而 `assert_usable` 对非 `LICENSED` 提前 return:那道授权闸口在产品里
    **永远不会触发任何一次阻断**。一份存下来却从不生效的合规记录,
    比没有更糟 —— 它看起来像已经做过了。

    ## 受众与体型不在这里

    `audience` 不可改:模特的受众是它本人的属性,不是分类偏好。改它等于
    把一个模特换成另一个人,而已生成的历史图片仍然溯源到这一行。
    要换受众就新建一个模板。`body_type` 同理跟着受众走(§10.4),
    所以一并留在创建路径上。
    """

    name: str | None = Field(None, min_length=1, max_length=128)
    pose: ModelPose | None = None
    background: str | None = Field(None, max_length=64)
    tags: list[str] | None = None
    notes: str | None = Field(None, max_length=2000)

    # ---- §11 授权字段组 ----
    source: str | None = Field(None, max_length=128)
    license_status: ModelLicenseStatus | None = None
    commercial_scope: str | None = Field(None, max_length=2000)
    allowed_platforms: list[str] | None = None
    allowed_regions: list[str] | None = None
    license_start_at: datetime | None = None
    license_expires_at: datetime | None = None
    allow_ai_dressing: bool | None = None
    allow_derivative_images: bool | None = None
    #: §11.1 年龄确认。**没有默认值** —— PATCH 里 `None` 是"不改",
    #: 不是"改成未确认"。给它 `False` 默认会让每一次改备注都顺手
    #: 把已确认的年龄抹掉
    age_verified: bool | None = None
    prohibited_categories: list[str] | None = None
    disabled_reason: str | None = Field(None, max_length=500)

    @field_validator("tags")
    @classmethod
    def _limit_tags(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if len(v) > 20:
            raise ValueError("标签最多 20 个")
        return [t.strip()[:32] for t in v if t and t.strip()]

    @field_validator("allowed_platforms", "allowed_regions", "prohibited_categories")
    @classmethod
    def _limit_scope_lists(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if len(v) > 50:
            raise ValueError("范围列表最多 50 项")
        return [x.strip()[:64] for x in v if x and x.strip()]
