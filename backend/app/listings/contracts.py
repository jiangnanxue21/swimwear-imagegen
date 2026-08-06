"""刊登层契约(M0):图片集快照、文案快照、内容计划、草稿。

这里定的是 v1 那个自相矛盾的 `ListingDraft` 的替代方案(意见 #4):

    ListingDraftData   纯数据。渠道适配器的输入,纯函数世界,可离线测试
    listing_drafts     持久化表(M6)。页面、审核、导出、发布都指向它

v1 把两者混成一个「无数据库依赖的纯数据类」,同时又给它设计了
`POST /api/listings/drafts` 和长期引用它的发布任务。那样的东西没法
回答一个很实际的问题:草稿建好之后属性被改了,这份草稿还能不能提交?

**纯数据。** 只用标准库 + 本项目的契约模块。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.attributes.contracts import CanonicalProduct
from app.core.enums import ImageSetStatus, MediaRole

LISTING_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class ImageItemSnapshot:
    """图片集里的一项。"""

    media_asset_id: str
    role: MediaRole
    sort_order: int
    storage_path: str
    width: int
    height: int
    variant_id: str | None = None
    is_primary: bool = False
    enabled: bool = True
    #: 指定用哪个派生版本;None = 原图。
    #: 平台要 1:1 主图时填 SQUARE_1_1,而不是另造一条素材
    derivative_purpose: str | None = None


@dataclass(frozen=True)
class ImageSetSnapshot:
    """由 `image_set_id` 解析出的只读快照(§7)。

    草稿里记的是 `image_set_id`(**含 version**),不是图片列表的副本。
    图片集不可原地修改,批准后任何改动都生成 version+1 的新集,
    于是「平台驳回的是哪一版图」永远可查,回滚就是把旧版重新置为 APPROVED。
    """

    image_set_id: str
    spu: str
    version: int
    status: ImageSetStatus
    items: tuple[ImageItemSnapshot, ...] = ()
    channel: str | None = None
    site: str | None = None

    def __post_init__(self) -> None:
        """状态统一折成 `ImageSetStatus`(评审第 11 条)。

        快照的来源有三种:ORM 行(`str`)、序列化后的 JSON(`str`)、
        测试里直接构造(枚举)。前两种进来的是字符串,而
        `self.status is ImageSetStatus.APPROVED` 对字符串恒为 `False` ——
        一个已批准的图片集会被判定成不可发布,且没有任何报错。

        在入口折一次,后面的比较就不必再关心来源。
        """
        if isinstance(self.status, ImageSetStatus):
            return
        try:
            coerced = ImageSetStatus(str(self.status))
        except ValueError as exc:
            raise ValueError(
                f"图片集状态 {self.status!r} 不是合法的 ImageSetStatus"
            ) from exc
        object.__setattr__(self, "status", coerced)

    @property
    def is_publishable(self) -> bool:
        """发布只读 `APPROVED`(硬规矩 5)。

        用 `==` 而不是 `is`:`__post_init__` 已经折过一次,这里再用 `is`
        只是把同一个陷阱留在原地等下一个改动的人。
        """
        return self.status == ImageSetStatus.APPROVED

    def enabled_items(self) -> tuple[ImageItemSnapshot, ...]:
        return tuple(i for i in self.items if i.enabled)

    def primary_for(self, variant_id: str | None) -> ImageItemSnapshot | None:
        """取某个变体的主图。变体没有专属主图时回落到 SPU 通用主图。"""
        for want in (variant_id, None):
            for item in self.enabled_items():
                if item.is_primary and item.variant_id == want:
                    return item
        return None


@dataclass(frozen=True)
class ClaimData:
    """模型对自己文本的结构化标注(§8.4)。

    **这不是模型的判断,是模型对自己写了什么的说明**,每一条都能被后端
    逐字验证:`field/value` 必须在 `ContentPlan.facts` 里且值一致,
    `text_span` 必须在对应 `location` 的文本中逐字出现。

    所以它和「不采信模型自报结论」不冲突 —— 我们采信的不是它的判断,
    是可以被机械校验的一个位置索引。
    """

    field_name: str
    value: str
    text_span: str
    location: str        # title / bullet.0 / description


@dataclass(frozen=True)
class ContentPlanData:
    """平台无关、语言无关的事实锚(§8.1)。

    `facts` **只能来自 CONFIRMED 属性**,由后端组装,模型不得新增或修改键值。
    多语言一致性靠共享同一份 facts 保证,而不是靠一次调用同时出多语言 ——
    后者 JSON 更长更易截断、一个语言失败拖垮全部、各语言字符限制还不同。
    """

    spu: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    selling_points: tuple[str, ...] = ()
    forbidden_claims: tuple[str, ...] = ()
    facts_version: str = "1"
    attr_snapshot_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CopySnapshot:
    """某个 `(channel, site, locale)` 的文案快照。"""

    copy_id: str
    locale: str
    version: int
    title: str | None = None
    bullet_points: tuple[str, ...] = ()
    description: str | None = None
    keywords: tuple[str, ...] = ()
    claims: tuple[ClaimData, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FieldViolation:
    """一条字段级问题。

    `level` 分 error / warning 是刻意的:同义词词典扫描出的颜色词不一致
    只能是 warning(词典永远不全,硬失败会把正常文案全拦下来),
    而 claims 与 facts 不一致必须是硬失败。
    """

    field_key: str
    code: str
    message: str
    level: str = "error"        # error / warning
    row_index: int | None = None

    @property
    def is_blocking(self) -> bool:
        return self.level == "error"


@dataclass(frozen=True)
class ListingDraftData:
    """渠道适配器的输入(§9.1 纯数据层)。"""

    spu: str
    channel: str
    site: str
    category_id: str
    product: CanonicalProduct
    image_set: ImageSetSnapshot
    copies: Mapping[str, CopySnapshot] = field(default_factory=dict)   # locale -> 快照
    manual: Mapping[str, Any] = field(default_factory=dict)
    spec_version: str = "TODO"
    mapping_version: str = "TODO"
    schema_version: str = LISTING_SCHEMA_VERSION

    def locale_mismatches(self) -> tuple[str, ...]:
        """映射键和文案自身 locale 对不上的那些键(评审第 5 条)。

        `copies` 是 `{locale: 快照}`。键和 `CopySnapshot.locale` 不一致时,
        整批文案就被绑到了错的语言上 —— 巴葡的正文会以 `en` 的身份进 Excel。
        这类错误在导出文件里几乎看不出来,要等平台或消费者发现。
        """
        return tuple(
            key for key, copy in self.copies.items() if key != copy.locale
        )

    def source_fingerprint(self) -> str:
        """上游事实的指纹,用于 `STALE` 判定(§9.1)。

        进指纹的东西分两类:

            上游事实   属性版本 id 集合、image_set_id(含 version)、
                       文案版本 id 集合、manual
            输出上下文 spu、channel、site、category_id、
                       spec_version、mapping_version、schema_version

        第二类是评审第 4 条补上的。改渠道、改站点、改类目都会换一套
        必填规则和一套列头,而上一版的指纹对它们完全无感 —— 一份在
        `SHEIN/BR` 下校验通过的草稿,改成 `GENERIC/MAIN` 之后仍然显示
        「校验通过、可以导出」,导出的却是按旧规则算出来的字段。
        `schema_version` 同理:草稿结构自身换版之后,旧草稿必须重算。

        文案那一项按 `(映射键, locale, copy_id, version)` 进哈希,不只是
        `copy_id:version` —— 只哈希文案对象值的话,把同一批文案换个
        语言键重新挂一遍,指纹一动不动(评审第 5 条)。

        `sort_keys=True` 和对集合排序不是洁癖:指纹必须对同一份事实
        给出同一个值,否则重算一次就 STALE 一次,这个机制立刻变成噪音。
        """
        payload = {
            "spu": self.spu,
            "channel": self.channel,
            "site": self.site,
            "category_id": self.category_id,
            "attrs": list(self.product.attribute_version_ids()),
            "image_set_id": self.image_set.image_set_id,
            "image_set_version": self.image_set.version,
            "copies": sorted(
                f"{key}:{copy.locale}:{copy.copy_id}:{copy.version}"
                for key, copy in self.copies.items()
            ),
            "manual": self.manual,
            "spec_version": self.spec_version,
            "mapping_version": self.mapping_version,
            "schema_version": self.schema_version,
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MappedListing:
    """映射结果(§9.1)。

    `rows` 是 SKU 行,`header` 是 SPU 级字段。Excel 与 API 共用同一个结构 ——
    这是「不允许表格里对、接口里错」那条目标的落点。
    """

    rows: tuple[Mapping[str, Any], ...] = ()
    header: Mapping[str, Any] = field(default_factory=dict)
    violations: tuple[FieldViolation, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not any(v.is_blocking for v in self.violations)
