"""A45-batch22(阶段 5 批次 5-5):导出预览的真库用例。

## 为什么非要真库

纯层验的是"给一份映射,铺出什么行"。而 AC-19 的后半句是一句**关于两份
数据是不是同一份**的话:「导出预览与最终导出读取同一份已存映射,
不在预览时重新推断。」

那句话只有在**上游真的变过之后**才检验得出来。两边同源时怎么写都对;
分叉的那一刻,重新推断的预览会显示"当前该用哪些图",而导出用的是草稿
生成那一刻存下来的那些 —— 于是运营在预览里看到红色有主图、
导出出来那一行是空的,**而两边都不报错**。

制造那次分叉需要一个真库:改 `listing_image_items`、批准新版图片集,
然后**不重新生成草稿**。纯层没有"草稿"这个东西。
"""
from __future__ import annotations

from app.listings import export_preview as ep
from app.models.listing_image import ListingImageItem, ListingImageSet
from app.workbench import service as wb
from tests.conftest import requires_db
from tests.test_a45_batch20_draft_color_axis_db import (  # noqa: F401  夹具复用
    _approved_copy,
    _approved_image_set,
    _asset,
    _first_product,
    _product,
    _ready_two_colour_spu,
    _spu,
    _variant,
)

pytestmark = requires_db


def _preview_images(session, product) -> dict:
    row = wb._current_draft(session, product.spu)
    return wb.draft_preview(session, product, row)["images"]


# ---------------------------------------------------------------- 一、正常态


def test_the_preview_lists_every_active_sku_with_its_primary(session):
    """每个 ACTIVE 颜色的每一行 SKU 都在预览里,且带着主图。"""
    spu, red, blue, _ = _ready_two_colour_spu(session)
    product = _first_product(session, spu)
    wb.build_draft(session, product, actor="batch22")
    session.flush()

    images = _preview_images(session, product)
    assert images["status"] == ep.MAP_READY
    assert {c["variant_code"] for c in images["colors"]} == {"RED", "BLU"}

    red_rows = next(c for c in images["colors"] if c["variant_code"] == "RED")["rows"]
    assert {r["sku"] for r in red_rows} == {
        f"{spu.spu_code}-RED-S",
        f"{spu.spu_code}-RED-M",
    }, "红色的两个尺码没有都铺进预览"
    assert all(r["status"] == ep.ROW_OK for r in red_rows)
    assert all(r["primary"] for r in red_rows)
    assert ep.missing_rows(images) == []


# ---------------------------------------------------------------- 二、AC-19 的正文


def test_the_preview_keeps_showing_the_stored_images_after_the_set_moves_on(session):
    """**本文件的第一位,也是 AC-19 后半句的字面场景。**

    草稿生成之后有人换掉了图片集里的图。**不重新生成草稿**,再看一次预览:
    它必须仍然显示**草稿存下来的那一张**,不是当前那一张。

    重新推断的实现在这里会显示新图 —— 而导出文件里写的仍然是旧图。
    两个数字都"看起来对",只有平台会看见差异。
    """
    spu, red, blue, _ = _ready_two_colour_spu(session)
    product = _first_product(session, spu)
    wb.build_draft(session, product, actor="batch22")
    session.flush()

    stored = _preview_images(session, product)
    red_before = next(c for c in stored["colors"] if c["variant_code"] == "RED")
    primary_before = red_before["rows"][0]["primary"]
    assert primary_before

    # 上游动了:给红色换一张主图(新素材 + 改图片项指向)。
    image_set = (
        session.query(ListingImageSet)
        .filter(ListingImageSet.spu == spu.spu_code)
        .first()
    )
    replacement = _asset(session, product, spu, red)
    item = (
        session.query(ListingImageItem)
        .filter(
            ListingImageItem.image_set_id == image_set.id,
            ListingImageItem.variant_id == str(red.id),
        )
        .first()
    )
    item.media_asset_id = replacement.id
    session.flush()

    after = _preview_images(session, product)
    red_after = next(c for c in after["colors"] if c["variant_code"] == "RED")
    assert red_after["rows"][0]["primary"] == primary_before, (
        "预览跟着上游变了 —— 它在重新推断,而导出读的是草稿存下来的那一份。"
        "AC-19 的「同一份已存映射」在这里不成立"
    )
    assert str(replacement.id) != primary_before, "夹具没有真的换掉图"


def test_regenerating_the_draft_is_what_moves_the_preview(session):
    """而重新生成草稿之后,预览**才**跟上。

    这是上一条的反面。只钉"预览不变"的话,最省事的实现是把预览写死 ——
    那样运营重新生成一次草稿,预览还是旧的,而他没有任何办法让它更新。
    """
    spu, red, blue, _ = _ready_two_colour_spu(session)
    product = _first_product(session, spu)
    wb.build_draft(session, product, actor="batch22")
    session.flush()
    before = _preview_images(session, product)
    primary_before = next(
        c for c in before["colors"] if c["variant_code"] == "RED"
    )["rows"][0]["primary"]

    image_set = (
        session.query(ListingImageSet)
        .filter(ListingImageSet.spu == spu.spu_code)
        .first()
    )
    replacement = _asset(session, product, spu, red)
    item = (
        session.query(ListingImageItem)
        .filter(
            ListingImageItem.image_set_id == image_set.id,
            ListingImageItem.variant_id == str(red.id),
        )
        .first()
    )
    item.media_asset_id = replacement.id
    session.flush()

    wb.build_draft(session, product, actor="batch22")
    session.flush()

    after = _preview_images(session, product)
    primary_after = next(
        c for c in after["colors"] if c["variant_code"] == "RED"
    )["rows"][0]["primary"]
    assert primary_after == str(replacement.id), (
        "重新生成草稿之后预览没有跟上 —— 预览被写死了,运营无法让它更新"
    )
    assert primary_after != primary_before


# ---------------------------------------------------------------- 三、两档非 READY


def test_a_draft_from_before_the_column_previews_as_unproven(session):
    """存量草稿(映射为 NULL)预览成 UNPROVEN,并说该做什么。

    不是空表格 —— 空表格会被读成"这个 SPU 没有图",而真相是"没算过"。
    """
    spu, _, _, _ = _ready_two_colour_spu(session)
    product = _first_product(session, spu)
    row = wb.build_draft(session, product, actor="batch22")
    session.flush()

    row.color_sku_image_map = None  # 0049 之前建的那些草稿就长这样
    session.flush()

    images = _preview_images(session, product)
    assert images["status"] == ep.MAP_UNPROVEN
    assert images["colors"] == []
    assert "草稿" in images["note"]


def test_a_stale_shape_version_previews_as_incompatible(session):
    """形状版本对不上时判 INCOMPATIBLE,与 UNPROVEN 分开。

    合并的话运营会一直重新生成草稿,而重新生成一百次也不会变好 ——
    那是有人换过快照形状,属于工程侧的事。
    """
    spu, _, _, _ = _ready_two_colour_spu(session)
    product = _first_product(session, spu)
    row = wb.build_draft(session, product, actor="batch22")
    session.flush()

    stored = dict(row.color_sku_image_map)
    stored["schema"] = "99"
    row.color_sku_image_map = stored
    session.flush()

    images = _preview_images(session, product)
    assert images["status"] == ep.MAP_INCOMPATIBLE
    assert images["status"] != ep.MAP_UNPROVEN


def test_a_single_colour_spu_previews_normally(session):
    """单色 SPU 也有图片预览 —— 颜色维不是"只有多色才有的东西"。"""
    spu = _spu(session)
    only = _variant(session, spu, "BLK", "ACTIVE")
    product = _product(session, spu, only, "S")
    asset = _asset(session, product, spu, only)
    _approved_image_set(session, spu, [(only, asset)])
    _approved_copy(session, spu, only)
    session.flush()

    wb.build_draft(session, product, actor="batch22")
    session.flush()

    images = _preview_images(session, product)
    assert images["status"] == ep.MAP_READY
    assert len(images["colors"]) == 1
    assert images["colors"][0]["rows"][0]["primary"] == str(asset.id)


def test_the_field_preview_still_works_alongside_the_images(session):
    """图片那一段是**加上去的**,不是把原来的字段预览换掉了。

    值得单独一条:`draft_preview` 的返回值有三个消费者(详情页、
    导出预览接口、`scripts/export_parity`),把 header/rows 挤掉的话
    那三处会同时空掉,而本文件的其它断言一条都不会红。
    """
    spu, _, _, _ = _ready_two_colour_spu(session)
    product = _first_product(session, spu)
    row = wb.build_draft(session, product, actor="batch22")
    session.flush()

    preview = wb.draft_preview(session, product, row)
    assert "header" in preview and "rows" in preview
    assert preview["header"], "字段预览的表头空了"
    assert "images" in preview
