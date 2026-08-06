"""属性层契约与 `CanonicalProduct`(M0)。

`CanonicalProduct` 是识别层与渠道层之间**唯一**的商品契约。
渠道适配器只接受它 + 图片集 + 文案 + manual 输入,**不得直接查数据库**。

这条限制看起来严苛,但它是「映射逻辑必须可穷举测试」的前提:
一旦适配器能查库,写一个新类目的映射测试就要先造一整套表数据,
于是没人会写那个测试,而字段映射恰恰是最容易错、也最容易测的部分。

**纯数据。** 只用标准库 + `core/enums.py` + 本层注册表。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.enums import AttributeSource, AttributeStatus, OwnerType
from app.media.contracts import MediaRef

#: 契约版本。`CanonicalProduct` 会被整份快照进 `listing_drafts`,
#: 读一份三个月前的草稿时靠它决定按哪版解释。
CANONICAL_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class AttributeEvidenceData:
    """一图一字段一条证据(§5.1)。

    **证据不是结论。** 它只记录「模型看着这张图,对这个字段说了什么」。
    多张图的证据怎么合并成一个值,是后端纯函数的事(`extractors/merge.py`),
    模型不参与。v1 让模型在同一次调用里既识别又比对,那不是交叉验证,
    是让它复读我们喂进去的答案。
    """

    id: str
    extraction_id: str
    media_asset_id: str
    field_name: str
    raw_value: Any                          # 模型原始输出,未归一
    model_name: str
    prompt_version: str
    normalized_value: str | None = None     # 归一到 core/enums.py;失败为 None
    model_confidence: float | None = None   # 模型自报,**不直接参与阈值判定**
    evidence_text: str | None = None        # 模型给的理由,仅供人看
    region: Mapping[str, Any] | None = None  # 可选 bbox
    created_at: datetime | None = None


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """`system_confidence` 的因子分解(§6.3)。

    每个因子都留下来,不是为了好看 —— 前端要能回答运营的那个问题:
    「0.71 是因为只有一张图,还是因为图太糊?」这两种情况人要做的事
    完全不同(补一张图 vs 重拍),只给一个总分等于没告诉他。

    `calibrated` 为 None 表示这个 (字段, 模型, 提示词版本) 组合还没校准。
    **未校准一律不允许自动确认**(fail closed)——校准表为空时阈值是一个
    没有意义的数字,而「看起来很高的置信度」会让人以为它有意义。
    """

    calibrated: float | None
    agreement_factor: float
    quality_factor: float
    visibility_prior: float
    source_factor: float

    @property
    def value(self) -> float | None:
        """最终的 `system_confidence`。未校准返回 None。"""
        if self.calibrated is None:
            return None
        return (
            self.calibrated
            * self.agreement_factor
            * self.quality_factor
            * self.visibility_prior
            * self.source_factor
        )


@dataclass(frozen=True)
class AttrValue:
    """一个字段的当前采信值。

    同时带 `model_confidence` 与 `system_confidence` 两个数:前者是模型自报的,
    只用于展示和校准;**只有后者参与阈值判定**。把两者合成一个的代价是
    模型换版之后没有任何东西能告诉你阈值已经失效了。
    """

    field_name: str
    value: Any                              # 单值或多值,与注册表的 value_type 对应
    source: AttributeSource
    status: AttributeStatus
    owner_type: OwnerType = OwnerType.SPU
    owner_id: str | None = None
    normalized_value: str | None = None     # 单值时的可索引投影
    model_confidence: float | None = None
    system_confidence: float | None = None
    breakdown: ConfidenceBreakdown | None = None
    evidence_ids: tuple[str, ...] = ()
    schema_version: str = CANONICAL_SCHEMA_VERSION
    calibration_version: str | None = None
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    #: 属性值版本 id。进 `listing_drafts.source_fingerprint`,
    #: 属性一改指纹就变,草稿自动 STALE
    version_id: str | None = None

    @property
    def is_confirmed(self) -> bool:
        return self.status is AttributeStatus.CONFIRMED

    @property
    def is_manual(self) -> bool:
        """人工值。**永不被自动覆盖**(§6.2 规则 4)。"""
        return self.source is AttributeSource.MANUAL


@dataclass(frozen=True)
class CanonicalVariant:
    """颜色层。一个 SPU 下的一个变体。"""

    variant_id: str
    attrs: Mapping[str, AttrValue] = field(default_factory=dict)
    media: tuple[MediaRef, ...] = ()


@dataclass(frozen=True)
class CanonicalSku:
    """颜色 × 尺码。"""

    sku: str
    variant_id: str
    size: str | None = None
    size_group: str | None = None
    gtin: str | None = None
    attrs: Mapping[str, AttrValue] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalProduct:
    """平台无关的商品事实(§5.5)。

    由 `attributes/service.build_canonical()` 组装。渠道适配器唯一的商品输入。
    """

    spu: str
    category_path: tuple[str, ...] = ()
    spu_attrs: Mapping[str, AttrValue] = field(default_factory=dict)
    variants: tuple[CanonicalVariant, ...] = ()
    skus: tuple[CanonicalSku, ...] = ()
    media: tuple[MediaRef, ...] = ()
    schema_version: str = CANONICAL_SCHEMA_VERSION

    def confirmed_attrs(self) -> Mapping[str, AttrValue]:
        """只取已确认的属性。

        `ContentPlan.facts` 只能来自这里 —— 用 SUGGESTED 的值去生成文案,
        等于把一个还没人看过的模型猜测写进面向消费者的商品描述里。
        """
        return {k: v for k, v in self.spu_attrs.items() if v.is_confirmed}

    def attribute_version_ids(self) -> tuple[str, ...]:
        """所有参与构成本快照的属性版本 id,排序后返回。

        进 `listing_drafts.source_fingerprint`。排序是必须的:
        指纹要对同一份事实给出同一个值,而字典序在 Python 里虽然稳定,
        但依赖插入顺序会让「同样的商品重算一次指纹就变了」。
        """
        ids: list[str] = []
        for bucket in (self.spu_attrs, *(v.attrs for v in self.variants),
                       *(s.attrs for s in self.skus)):
            ids.extend(v.version_id for v in bucket.values() if v.version_id)
        return tuple(sorted(ids))
