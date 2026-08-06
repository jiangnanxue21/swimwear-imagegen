"""素材层的纯映射与比较规则(M1)。

**零依赖。** 不 import SQLAlchemy —— 这里的东西全是「旧的什么对应新的什么」
和「两个值算不算一样」,它们是这次迁移里最容易错、也最需要能快速穷举的部分,
必须能在一个不带数据库的测试里跑。

`app/media/service.py` 从这里取值,不自己再写一份。
"""
from __future__ import annotations

from typing import Any

from app.core.enums import AssetType, MediaRole

#: 旧 `AssetType` -> 新 `MediaRole`。
#:
#: 这张表是「role 与 source 正交」这条设计的迁移代价:旧枚举把两件事混在
#: 一起,所以 `MODEL_REFERENCE` 既表达了角色(模特图)又隐含了用途。
#:
#: 两条映射是**有损**的:
#:   GARMENT_CUTOUT  -> FLAT_LAY     透明底商品图未必是平铺图
#:   MODEL_REFERENCE -> MODEL_FRONT  旧数据里分不出模特图的正反面
#:
#: 有损是事实,不该用 None 掩盖它 —— None 会让这条素材对下游不可用
#: (图片集要按角色取图),而一个明确的值至少让人在素材库里知道该不该改。
#: `legacy_kind` 会留痕,素材库页面据此提示复核。
#:
#: 旧枚举**没有** SIZE_CHART。新的 `MediaRole` 有,因为上架需要尺码表,
#: 但旧模型里尺码表只能当 GARMENT_DETAIL 传进来。这条差异靠映射弥补不了,
#: 只能由人工在素材库里改角色。
LEGACY_ASSET_TYPE_TO_ROLE: dict[str, MediaRole] = {
    AssetType.GARMENT_FRONT.value: MediaRole.PRODUCT_FRONT,
    AssetType.GARMENT_BACK.value: MediaRole.PRODUCT_BACK,
    AssetType.GARMENT_DETAIL.value: MediaRole.DETAIL,
    AssetType.GARMENT_CUTOUT.value: MediaRole.FLAT_LAY,
    AssetType.MODEL_REFERENCE.value: MediaRole.MODEL_FRONT,
    AssetType.BACKGROUND_REFERENCE.value: MediaRole.OTHER,
}

#: 影子写来源标记。落在 `MediaAsset.legacy_kind` 上,对账靠它定位。
LEGACY_PRODUCT_ASSET = "product_asset"
LEGACY_CANDIDATE = "generation_candidate"
LEGACY_OUTPUT_ASSET = "output_asset"

#: 参与双读对账的字段。
#:
#: **不含 role / role_source / status。** 前两个的映射本来就是有损的,
#: 后一个是人工改的(隔离与放行)。把它们算进对账会产生一堆无法消除的告警,
#: 然后所有人开始忽略这张表 —— 那时候真正的不一致也没人看了。
#:
#: 剩下的这六个是「同一份事实的两种存法」,任何差异都是真的错。
PARITY_FIELDS: tuple[str, ...] = (
    "storage_path",
    "sha256",
    "mime_type",
    "width",
    "height",
    "bytes",
)


def role_for_legacy_asset_type(asset_type: str) -> MediaRole:
    """旧类型对应的角色。未知类型落 OTHER,不抛错。

    抛错会让一条历史数据卡住整个回填。落 OTHER 且留痕,
    人工在素材库里能看到并改掉。
    """
    return LEGACY_ASSET_TYPE_TO_ROLE.get(asset_type, MediaRole.OTHER)


def normalize_for_parity(value: Any) -> str:
    """比较前统一形状。

    数值列在旧表是 int、新表可能是 Decimal(Numeric 列),直接 `!=` 会产生
    一堆假的不一致 —— 而假告警比没有告警更糟:它会训练人忽略这张表。

    空字符串与 None 同样视作「没有值」。
    """
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        # bool 是 int 的子类,不加这一条 True 会变成 "1"
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value))
    # Decimal 走这里:str(Decimal("1200")) == "1200",但 Decimal("1200.0")
    # 会变成 "1200.0",所以先试着当数值处理
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value)
