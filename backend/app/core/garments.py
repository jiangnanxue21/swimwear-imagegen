"""受众 × 品类的兼容词表(PRD v2 §2.2)。**纯标准库,零依赖。**

## 为什么后端必须自己有一份

前端 `api/types.ts` 的 `GARMENT_TYPES_BY_AUDIENCE` 只负责把越界选项从下拉里
拿掉 —— 而后端在 batch10 之前**按枚举全集收**,于是 `MEN + BIKINI_SET`
这个组合在两条真实路径上都能落库:

    1. 接口直连(下拉的收窄管不到 curl);
    2. 换受众时前端把品类清成 undefined,JSON 序列化把键丢掉,
       后端按「未传不改」处理 —— 界面显示已清空,库里还是旧品类。

与 `BODY_TYPES_BY_AUDIENCE`(§10.4)完全同一个形状的问题,那边的解法也在:
前端一份让选项就近消失,后端一份守住非表单路径,跨语言契约测试
(`tests/pure/test_a45_batch11_fixes.py`)钉住两份不许分叉。

## 取值口径

映射抄自前端那份(batch10 已上线的产品口径),不是从枚举注释推的:
UNISEX **不含** SWIM_SHORTS,尽管枚举注释写着"中性沙滩裤也用它" ——
两处有出入时以已发布的交互口径为准,要改就两份一起改,契约测试会拦住
只改一半的提交。

`OTHER` 对三个受众恒许:它是这一列的中性缺省值(模型列默认),
也是"换受众后品类待重选"的显式清空落点(见 `product_service`)。
"""
from __future__ import annotations

from app.core.audience import coerce
from app.core.enums import Audience, GarmentType

#: 受众 -> 允许的品类。与前端 `GARMENT_TYPES_BY_AUDIENCE` 逐字对应。
GARMENT_TYPES_BY_AUDIENCE: dict[Audience, tuple[str, ...]] = {
    Audience.WOMEN: (
        GarmentType.ONE_PIECE.value,
        GarmentType.BIKINI_TOP.value,
        GarmentType.BIKINI_BOTTOM.value,
        GarmentType.BIKINI_SET.value,
        GarmentType.TANKINI.value,
        GarmentType.COVER_UP.value,
        GarmentType.DRESS.value,
        GarmentType.TOP.value,
        GarmentType.RASH_GUARD.value,
        GarmentType.OTHER.value,
    ),
    Audience.MEN: (
        GarmentType.SWIM_SHORTS.value,
        GarmentType.BOARDSHORTS.value,
        GarmentType.SWIM_BRIEFS.value,
        GarmentType.JAMMERS.value,
        GarmentType.SWIM_TRUNKS_SET.value,
        GarmentType.RASH_GUARD.value,
        GarmentType.OTHER.value,
    ),
    Audience.UNISEX: (
        GarmentType.RASH_GUARD.value,
        GarmentType.COVER_UP.value,
        GarmentType.TOP.value,
        GarmentType.OTHER.value,
    ),
}


def allowed_garment_types(audience: object) -> tuple[str, ...]:
    """该受众允许的品类。受众未确认(None)时返回全集 —— 那时
    "该出哪一组"还没有答案,与 `body_types_allowed` 的兼容缝同一取向。"""
    resolved = coerce(audience)
    if resolved is None:
        return tuple(v.value for v in GarmentType)
    return GARMENT_TYPES_BY_AUDIENCE[resolved]


def garment_allowed(audience: object, garment_type: object) -> bool:
    """这个 (受众, 品类) 组合是否成立。未知品类值判否,不判"没填"。"""
    value = str(garment_type or "").strip().upper()
    if not value:
        return True
    return value in allowed_garment_types(audience)


def garment_block_reason(audience: object, garment_type: object) -> str | None:
    """组合不成立时给运营看的一句话;成立返回 None。措辞按 §20.4 翻译口径。"""
    if garment_allowed(audience, garment_type):
        return None
    from app.core.enums import AUDIENCE_LABELS

    resolved = coerce(audience)
    label = AUDIENCE_LABELS[resolved] if resolved else "该受众"
    return (
        f"品类 {garment_type} 不在{label}词表里;"
        f"请改选兼容品类,或先写回 OTHER 再由运营重选"
    )
