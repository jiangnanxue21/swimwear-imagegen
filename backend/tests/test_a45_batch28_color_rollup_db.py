"""A45-batch28(阶段 6 批次 6-3):聚合工作流 API 的真库用例。

## 为什么非要真库

这一批的正文是**一句关于 HTTP 响应里那个字段的话**:向导一次请求拿到的
颜色子态,和 READY 门禁看到的是不是同一件事。纯层能钉住"取数层调了那个
builder",钉不住"两条路在真实数据上给出同一个答案" —— 而 §3.54 记的三条
缺口,每一条的纯层都是绿的。
"""
from __future__ import annotations

import uuid

from app.core.enums import (
    ImageSetStatus,
    MediaRole,
    MediaSource,
    MediaStatus,
    RoleSource,
    SellableStatus,
    SpuStatus,
)
from app.models.listing_image import ListingImageItem, ListingImageSet
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.workbench import color_rollup
from app.workbench import service as wb
from app.workbench.color_flow import ColorSubState
from tests.conftest import requires_db

pytestmark = requires_db


def _spu(session) -> Spu:
    row = Spu(
        spu_code=f"B28{uuid.uuid4().hex[:5].upper()}",
        internal_name="batch28 测试款",
        audience="WOMEN",
        base_category="swimwear",
        status=SpuStatus.DRAFT.value,
    )
    session.add(row)
    session.flush()
    return row


def _variant(session, spu: Spu, code: str, status=SellableStatus.ACTIVE) -> ColorVariant:
    row = ColorVariant(
        spu_id=spu.id,
        variant_code=code,
        working_name=f"供应商说的{code}",
        sellable_status=status.value,
    )
    session.add(row)
    session.flush()
    return row


def _product(session, spu: Spu, variant: ColorVariant, size="S") -> Product:
    row = Product(
        spu=spu.spu_code,
        spu_id=spu.id,
        color_variant_id=variant.id,
        sku=f"{spu.spu_code}-{variant.variant_code}-{size}",
        name="测试款",
        category="swimwear",
        audience="WOMEN",
        size=size,
    )
    session.add(row)
    session.flush()
    return row


def _asset(session, product: Product, spu: Spu, variant: ColorVariant) -> MediaAsset:
    row = MediaAsset(
        product_id=product.id,
        spu_id=spu.id,
        color_variant_id=variant.id,
        role=MediaRole.PRODUCT_FRONT.value,
        role_source=RoleSource.HUMAN.value,
        source=MediaSource.MANUAL_UPLOAD.value,
        status=MediaStatus.READY.value,
        storage_path=f"orig/{uuid.uuid4().hex}.jpg",
        mime_type="image/jpeg",
        width=800,
        height=1200,
        bytes=1024,
        sha256=uuid.uuid4().hex * 2,
    )
    session.add(row)
    session.flush()
    return row


def _image_set(session, spu: Spu, items) -> ListingImageSet:
    image_set = ListingImageSet(
        spu=spu.spu_code,
        version=1,
        status=ImageSetStatus.APPROVED.value,
        approved_by="batch28",
    )
    session.add(image_set)
    session.flush()
    for variant, asset, order, is_primary in items:
        session.add(
            ListingImageItem(
                image_set_id=image_set.id,
                media_asset_id=asset.id,
                role=asset.role,
                sort_order=order,
                variant_id=None if variant is None else str(variant.id),
                is_primary=is_primary,
                enabled=True,
            )
        )
    session.flush()
    return image_set


def _rollup(session, product: Product):
    canonical = wb._canonical_with_skus(session, product)
    return color_rollup.rollup(session, product, canonical=canonical)


def test_a_colour_without_samples_blocks_and_says_which_one(session):
    """一个颜色没样品 -> 它拦路,而且**点名是哪个颜色**。

    向导的价值全在这句话上:九色 SPU 显示「图片集 30%」时,运营不知道是
    九个颜色各差一点,还是八个齐了、只有一个一张图都没有 ——
    这两种情况要做的事完全不同。
    """
    spu = _spu(session)
    red = _variant(session, spu, "RED")
    blue = _variant(session, spu, "BLU")
    red_sku = _product(session, spu, red)
    _product(session, spu, blue)
    _asset(session, red_sku, spu, red)
    session.flush()

    result = _rollup(session, red_sku)
    assert result.active_count == 2
    assert "BLU" in result.blocking_codes, "没样品的那个颜色没有被点名"
    assert "RED" not in result.blocking_codes

    by_code = {c.variant_code: c for c in result.colors}
    from app.workbench.flow import FlowStep

    assert by_code["BLU"].by_step[FlowStep.MATERIAL].state is ColorSubState.BLOCKED
    assert by_code["BLU"].by_step[FlowStep.MATERIAL].detail, "拦路却不说为什么"


def test_a_non_active_colour_is_listed_but_never_blocks(session):
    """PLANNED 的颜色如实显示,但不拦路(§6.7 / AC-18 的范围口径)。

    算进阻断的话,任何一个刚建档的颜色都会让整个 SPU 变红,
    而运营对此无能为力 —— 那不是修复,是停产。
    """
    spu = _spu(session)
    active = _variant(session, spu, "RED")
    planned = _variant(session, spu, "PLN", status=SellableStatus.PLANNED)
    sku = _product(session, spu, active)
    _product(session, spu, planned)
    _asset(session, sku, spu, active)
    session.flush()

    result = _rollup(session, sku)
    codes = {c.variant_code for c in result.colors}
    assert "PLN" in codes, "非 ACTIVE 的颜色被从结果里删掉了 —— 它要显示,只是不拦路"
    assert "PLN" not in result.blocking_codes
    assert result.active_count == 1


def test_the_missing_primary_agrees_with_the_export_map(session):
    """向导说的「缺主图」与导出预览那份映射**逐 SKU 一致**。

    这是 §3.54 的 AC-19 换个位置的版本:那次分叉在预览与导出之间,
    这次可能分叉在向导与预览之间。两者读同一个 builder,所以这条用例
    比的是**结果**,不是"都调了那个函数"。
    """
    spu = _spu(session)
    colour = _variant(session, spu, "CRL")
    sku = _product(session, spu, colour)
    asset = _asset(session, sku, spu, colour)
    # 有图但**没有任何一张标成主图** —— 合法的中间态,而它正是要报的那种
    _image_set(session, spu, [(colour, asset, 0, False)])
    session.flush()

    from app.listings import image_set_rules
    from app.workbench import upstream_collect, upstream_snapshot

    # **走与生产同一条解析路径。** 不显式传 `image_set_row` 的话
    # `collect` 拿不到那一版,`inputs.image_set` 是 None —— 而这条用例要比的
    # 恰恰是"两边看到同一份图",从一个空图片集比起来毫无意义
    canonical = wb._canonical_with_skus(session, sku)
    inputs = upstream_collect.collect(
        session,
        sku,
        canonical=canonical,
        image_set_row=wb._current_image_set(session, sku.spu),
    )
    assert inputs.image_set is not None, "夹具里的图片集没解析出来,比对无从谈起"
    cov = image_set_rules.coverage(
        inputs.image_set,
        variant_ids=(c.variant_id for c in upstream_snapshot.active_colors(inputs.colors)),
        required_angles=inputs.required_angles,
    )

    mapping = upstream_snapshot.build_color_sku_image_map(colors=inputs.colors, coverage=cov)
    map_missing = {
        s
        for cell in (mapping.get("colors") or {}).values()
        for s, v in (cell.get("skus") or {}).items()
        if not (v or {}).get("primary")
    }

    result = _rollup(session, sku)
    from app.workbench.flow import FlowStep

    step = result.colors[0].by_step[FlowStep.IMAGE_SET]
    assert map_missing, "夹具没造出缺主图的 SKU,这条用例证明不了什么"
    assert step.state is ColorSubState.BLOCKED
    for missing_sku in map_missing:
        assert missing_sku in step.detail, (
            f"映射说 {missing_sku} 缺主图,而向导没点它的名 —— 两条口径分叉了"
        )


def test_the_endpoint_ships_the_colours(session):
    """最后一跳:它真的从 HTTP 出得去。

    §3.43 那一族十二次的形状都是"少最后一跳"。序列化后的形状在这里验一遍,
    前端才敢按它写。
    """
    spu = _spu(session)
    colour = _variant(session, spu, "RED")
    sku = _product(session, spu, colour)
    _asset(session, sku, spu, colour)
    session.flush()

    payload = color_rollup.serialize_rollup(_rollup(session, sku))
    assert payload["active_count"] == 1
    assert isinstance(payload["colors"], list) and payload["colors"]
    first = payload["colors"][0]
    assert set(first) >= {
        "variant_id", "variant_code", "is_active", "current_step", "is_blocking", "steps"
    }
    assert first["steps"] and set(first["steps"][0]) == {"step", "state", "detail"}
    # 所有值都要是 JSON 能装的东西 —— 枚举漏了 `.value` 的话这里会红
    import json

    json.dumps(payload)


def test_a_product_without_an_spu_ref_gets_nothing_not_an_empty_shell(session):
    """没有 SPU 归属的存量行拿到 `None`,不是"零个颜色、没人拦路"。

    空壳在向导上读起来是「颜色都齐了」,而真相是没查过。
    """
    spu = _spu(session)
    colour = _variant(session, spu, "RED")
    sku = _product(session, spu, colour)
    sku.spu_id = None
    session.flush()

    from app.api.workbench import _color_rollup

    assert _color_rollup(session, sku) is None
