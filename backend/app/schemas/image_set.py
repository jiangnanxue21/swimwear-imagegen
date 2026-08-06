"""图片集接口的出入参(M5)。"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import MediaRole
from app.core.field_limits import MAX_IMAGE_SET_ITEMS


class ImageItemIn(BaseModel):
    """图片集的一项。

    长度上限与数据库列一一对应(评审第 15 条)。不写的话,超长值要到
    `flush()` 撞列宽时才失败,抛出来的是 `DataError`,接口层把它变成 500 ——
    而它其实是一个再普通不过的入参问题,应当是 422。
    """

    media_asset_id: UUID
    role: MediaRole
    #: 数据库是 Integer,没有上限;负数没有意义,排序从 0 起
    sort_order: int = Field(default=0, ge=0)
    #: listing_image_items.variant_id String(64)
    variant_id: str | None = Field(default=None, max_length=64)
    is_primary: bool = False
    enabled: bool = True
    #: listing_image_items.derivative_purpose String(32)
    derivative_purpose: str | None = Field(default=None, max_length=32)


class ImageSetItemsIn(BaseModel):
    #: 上限见 `core.field_limits` 的说明。写在入参层是为了让超限变成 422
    #: 而不是一个跑到服务层才发现、已经拿过 advisory lock 的 409
    items: list[ImageItemIn] = Field(min_length=1, max_length=MAX_IMAGE_SET_ITEMS)

    @model_validator(mode="after")
    def _no_duplicates(self) -> ImageSetItemsIn:
        """同一批里排序号不重复、同一素材的同一变体不重复。

        两条都对应库里的唯一索引。在这里查是为了让报错落在提交的那一刻、
        指到具体第几项,而不是变成一个"服务器错误"。
        """
        orders = [i.sort_order for i in self.items]
        duplicated = {o for o in orders if orders.count(o) > 1}
        if duplicated:
            raise ValueError(
                f"排序号重复:{sorted(duplicated)};同一个图片集里每张图的位置必须唯一"
            )

        # variant_id 为 NULL 时数据库那条唯一约束不生效(NULL 互不相等),
        # 所以这里按 '' 归一,口径和 0016 的表达式索引一致
        seen: set[tuple[UUID, str]] = set()
        for index, item in enumerate(self.items):
            key = (item.media_asset_id, item.variant_id or "")
            if key in seen:
                raise ValueError(
                    f"第 {index + 1} 项重复:同一张素材的同一个变体只能出现一次"
                )
            seen.add(key)
        return self


class ImageSetCreate(ImageSetItemsIn):
    spu: str = Field(min_length=1, max_length=64)
    #: listing_image_sets.channel / site 都是 String(32),可空 = 通用
    channel: str | None = Field(default=None, max_length=32)
    site: str | None = Field(default=None, max_length=32)


class ViolationOut(BaseModel):
    code: str
    message: str
    variant_id: str | None = None
    media_asset_id: str | None = None


class ImageSetItemOut(BaseModel):
    """图片集里的一项(出参)。

    详情接口以前只返回版本、状态和校验结果,**不返回 items** ——
    于是前端保存完编排就再也读不回来,FE-212 的验收结果
    「保存后顺序和角色一致」在界面上无法验证。

    这里不带素材的 URL:签名地址要拿 storage,而前端为了做素材选择区
    本来就会拉一遍 `GET /api/media?product_id=`,按 `media_asset_id`
    join 即可。少一条依赖,也少一份可能与素材库不一致的缩略图。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    media_asset_id: UUID
    role: str
    sort_order: int
    variant_id: str | None = None
    is_primary: bool = False
    enabled: bool = True
    derivative_purpose: str | None = None


class ImageSetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    spu: str
    channel: str | None = None
    site: str | None = None
    version: int
    status: str
    derived_from: UUID | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime | None = None
    #: 快审退回(A10)。状态是 REJECTED 时这三项才有值 ——
    #: 界面要显示的是「被退回:颜色不一致」,不是一个孤零零的英文状态
    reject_reason: str | None = None
    reject_note: str | None = None
    rejected_at: datetime | None = None


class VariantCoverageOut(BaseModel):
    """变体覆盖诊断(BLOCK-02)。**不是校验结果,是现状描述。**

    详情页要能回答"这个 SPU 有 3 个颜色,这一版图覆盖了几个" ——
    而今天的答案往往是"一个都没绑定",且校验不会提。
    """

    required: list[str] = []
    #: key → **当前**颜色名(A44)。界面显示这个,不要显示 key ——
    #: key 是变体身份,取值是分配那一刻的颜色名,改过名之后它是历史值
    labels: dict[str, str] = {}
    #: 稳定 key 换来的两种新异常:`renamed` / `label_collisions` / `unassigned`。
    #: 只有 `label_collisions` 是真问题(两个变体在界面上同名,绑图分不出谁)
    drift: dict[str, list[str]] = {}
    tagged: list[str] = []
    has_generic_images: bool = False
    missing: list[str] = []
    #: 用了但不属于该 SPU 的标签 —— 两套变体词表对不上的信号
    unknown_tags: list[str] = []
    #: 多变体 SPU 却完全没做变体绑定
    variant_binding_missing: bool = False


class ImageSetDetailOut(ImageSetOut):
    #: 按 sort_order 升序。前端的编排表直接按这个顺序渲染
    items: list[ImageSetItemOut] = []
    violations: list[ViolationOut] = []
    #: 校验通过**且**已批准才算可发布。两者缺一不可 ——
    #: 已批准但素材后来被隔离的集会在这里显示为不可发布
    publishable: bool = False
    #: 变体覆盖现状。`violations` 里没有它 —— 它今天不阻断,只是可见
    variant_coverage: VariantCoverageOut | None = None
