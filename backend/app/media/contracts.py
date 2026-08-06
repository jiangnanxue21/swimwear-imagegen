"""素材层契约(M0)。

**纯数据。** 只用标准库,不 import SQLAlchemy / httpx / FastAPI。
`tests/pure/test_m0_contracts.py::test_contract_modules_have_no_infrastructure_imports`
用 AST 静态守住这一点(原先在 `test_contract_purity.py`,该文件已并入 m0 契约集;
本行一度还指着旧名字)。

为什么契约要和 ORM 模型分开:识别层、图片集、渠道适配器都以素材为输入,
但它们**不该能查数据库**。渠道适配器一旦拿到 ORM 对象,就会有人顺手写一句
`asset.product.spu`,于是一个本该是纯函数的映射变成了需要数据库才能测的东西,
而映射逻辑恰恰是最需要快速穷举测试的部分。

命名约定:契约类一律带 `Data` / `Ref` / `Snapshot` 后缀,和 ORM 模型区分开。
`MediaAssetData` 是「一条素材的事实」,`media_assets` 表是它的一种存储方式。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.enums import (
    ConsentScope,
    MediaRole,
    MediaSource,
    MediaStatus,
    RoleSource,
)

#: 契约 schema 版本。任何**破坏性**改动都要动它:加可选字段不算,
#: 改字段含义、删字段、改类型算。落库的快照里会带上它,
#: 于是三个月后读到一份旧快照时能知道该按哪个版本解释。
MEDIA_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class MediaConsentData:
    """素材授权(§12.1)。

    四个 scope 标志位相互独立,**缺一不可**:
    「可以用 AI 处理」不等于「可以传到境外」,更不等于「可以商用发布」。
    v1 只记了一句「授权来源」,回答不了其中任何一个问题。
    """

    id: str
    subject_type: str                       # MODEL_TEMPLATE / REAL_MODEL / SUPPLIER_IMAGE / STOCK
    scope: frozenset[str] = frozenset()     # ConsentScope 的值
    allowed_regions: tuple[str, ...] = ()   # CN / US / EU ...
    allowed_processors: tuple[str, ...] = ()
    training_opt_out: bool = True
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    subject_ref: str | None = None
    document_ref: str | None = None

    def covers(self, scope: ConsentScope) -> bool:
        return scope.value in self.scope


@dataclass(frozen=True)
class MediaQuality:
    """确定性图像指标。**不是模型打的分。**

    这几个数由 Pillow/OpenCV 之类的确定性算法算出来,进
    `system_confidence` 的 `quality_factor`。让模型给「这张图清不清楚」
    等于用一个不确定的量去修正另一个不确定的量。
    """

    sharpness: float | None = None
    brightness: float | None = None
    blur_score: float | None = None
    has_watermark: bool | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MediaAssetData:
    """一条素材的完整事实。

    ``source`` 与 ``role`` **正交**,这是整个素材层最重要的一条设计:
    「这张图是 AI 生成的」和「这张图是尺码表」是两个互不相关的问题。
    v1 把它们挤在一个 `AssetType` 里,结果 AI 图只能是成品图、
    供应商推来的正面图和人工上传的正面图要走两条代码路径。
    """

    id: str
    product_id: str
    storage_path: str
    mime_type: str
    width: int
    height: int
    bytes: int
    sha256: str
    source: MediaSource
    status: MediaStatus
    spu: str | None = None
    role: MediaRole | None = None
    role_source: RoleSource = RoleSource.UNSET
    role_confidence: float | None = None
    variant_hint: str | None = None
    quality: MediaQuality | None = None
    # 合规与数据治理
    processor: str | None = None
    processing_region: str | None = None
    training_opt_out: bool = True
    consent_id: str | None = None
    retention_policy: str | None = None
    created_at: datetime | None = None
    schema_version: str = MEDIA_SCHEMA_VERSION

    @property
    def is_usable(self) -> bool:
        """能不能进识别与图片集。

        只有 READY。`QUARANTINED` 刻意**不算**可用 —— 隔离的含义就是
        「先别用,但也别删」,它需要人来放行。
        """
        return self.status is MediaStatus.READY

    @property
    def is_ai_generated(self) -> bool:
        """AI 生成图不能成为任何属性的唯一来源(硬规矩 6)。"""
        return self.source is MediaSource.AI_GENERATED

    @property
    def can_be_primary(self) -> bool:
        """能不能放主图位(§7.3)。

        角色是模型猜的、且把握不足 0.9 时不行。主图放错是消费者第一眼
        就看到的错误,而平铺图和模特图恰恰是角色识别最容易混的一对。
        """
        if not self.is_usable:
            return False
        if self.role_source is RoleSource.MODEL:
            return (self.role_confidence or 0.0) >= 0.9
        return True


@dataclass(frozen=True)
class MediaDerivativeData:
    """同一张图的不同尺寸/底色/带水印版本。

    **派生版本不是新素材。** 属性识别、图片集、去重、授权、审计一律以
    `MediaAssetData` 为单位。平台要 1:1 主图时,取的是「图片集里那一项的
    素材的 SQUARE_1_1 派生」,而不是另一条素材 —— 否则同一张图会
    在素材库里出现五遍,去重、授权和保留策略全部失效。
    """

    id: str
    source_media_asset_id: str
    purpose: str                # SQUARE_1_1 / VERTICAL_3_4 / WHITE_BG / WATERMARKED / THUMB ...
    width: int
    height: int
    bytes: int
    storage_path: str
    sha256: str
    transform_version: str      # 裁切/补白/压缩算法版本,可复现


@dataclass(frozen=True)
class MediaRef:
    """`CanonicalProduct` 里对素材的只读引用。

    比 `MediaAssetData` 窄:渠道适配器需要知道「这是哪张图、什么角色、
    什么来源」,不需要知道保留策略和质量指标。契约越窄,将来越好改。
    """

    media_asset_id: str
    role: MediaRole | None
    source: MediaSource
    storage_path: str
    width: int
    height: int
    variant_hint: str | None = None
