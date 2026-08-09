"""A45-batch24(阶段 5 三处接缝):真库用例。

## 为什么这三条非要真库

它们各自的正文都是**一句关于库里那一行/那一列的话**,而纯层看不见:

    F-1   识别自动确认出来的颜色,`color_variants.display_name` 有没有值
    F-6   在两个颜色之间来回点生成,`listing_copies` 到底多了几行
    AC-19 预览显示的那张主图,和 `mapped_payload` 里那一行用的是不是同一张

三条都有一个共同点:**在纯层它们全都是绿的**。投影函数算得对、幂等键算得对、
预览函数读得对 —— 出错的是它们各自与上游的那一条接缝,而接缝要有真的
`decide_status`、真的唯一约束、真的一次 `build_draft` 才走得到。

## 计数怎么做

不 mock 生成器,数**真的落了几行**(`listing_copies` 的行数、`content_plans`
的行数)。默认配置下生成器是模板(不花钱),所以"调了几次"要靠副作用来数,
而副作用恰好就是这笔账的形状:多一行 = 多一次调用 = 多一次付费(真接 LLM 时)。
这与 batch21 那份是同一条口径。
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.attributes import service as attr_service
from app.attributes import validation
from app.attributes.registry import get_field
from app.channels import generic
from app.core.enums import (
    AttributeSource,
    AttributeStatus,
    CopyStatus,
    ImageSetStatus,
    MediaRole,
    MediaSource,
    MediaStatus,
    OwnerType,
    RoleSource,
    SellableStatus,
    SpuStatus,
)
from app.listings import export_preview as ep
from app.listings import variants
from app.models.attribute import (
    AttributeCalibration,
    AttributeEvidence,
    ProductAttributeExtraction,
    ProductAttributeValue,
)
from app.models.listing_copy import ContentPlan, ListingCopy
from app.models.listing_image import ListingImageItem, ListingImageSet
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.workbench import service as wb
from tests.conftest import requires_db

pytestmark = requires_db


# ================================================================ 夹具


def _spu(session) -> Spu:
    row = Spu(
        spu_code=f"B24{uuid.uuid4().hex[:5].upper()}",
        internal_name="batch24 测试款",
        audience="WOMEN",
        base_category="swimwear",
        status=SpuStatus.DRAFT.value,
    )
    session.add(row)
    session.flush()
    return row


def _variant(session, spu: Spu, code: str) -> ColorVariant:
    row = ColorVariant(
        spu_id=spu.id,
        variant_code=code,
        working_name=f"供应商说的那个{code}",
        sellable_status=SellableStatus.ACTIVE.value,
    )
    session.add(row)
    session.flush()
    return row


def _product(session, spu: Spu, variant: ColorVariant, size: str = "S") -> Product:
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


def _asset(
    session,
    product: Product,
    spu: Spu,
    variant: ColorVariant | None,
    *,
    role: MediaRole = MediaRole.PRODUCT_FRONT,
    sharp: bool = False,
) -> MediaAsset:
    row = MediaAsset(
        quality_json={"sharpness": 1.0} if sharp else None,

        product_id=product.id,
        spu_id=spu.id,
        color_variant_id=None if variant is None else variant.id,
        role=role.value,
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
    """一版已批准图片集。`items` 是 (variant, asset, sort_order, is_primary)。"""
    image_set = ListingImageSet(
        spu=spu.spu_code,
        version=1,
        status=ImageSetStatus.APPROVED.value,
        approved_by="batch24-db-test",
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


def _approved_copy(session, spu: Spu, *colours: ColorVariant) -> None:
    plan = ContentPlan(spu=spu.spu_code, facts={}, selling_points=[], forbidden_claims=[])
    session.add(plan)
    session.flush()
    for colour in colours:
        session.add(
            ListingCopy(
                spu=spu.spu_code,
                channel=generic.CHANNEL,
                site=generic.SITE,
                locale=generic.LOCALE,
                color_variant_id=str(colour.id),
                version=1,
                content_plan_id=plan.id,
                status=CopyStatus.APPROVED.value,
                title="Test swimwear",
                bullet_points=["a", "b"],
                description="d",
                keywords=["k"],
            )
        )
    session.flush()


#: 够生成一版**合格**文案的最小事实集(口径同 batch21)。
#:
#: 少一条就 REJECTED,而 REJECTED 不可复用 —— 于是"复用"一条都验不到,
#: 本文件关于"第二次点击不花钱"的断言会全部失去意义。
_SHARED_FACTS = (
    ("material", "Nylon"),
    ("garment_type", "BIKINI_SET"),
    ("pattern_type", "SOLID"),
    ("neckline_type", "V_NECK"),
)


def _confirmed_fact(session, product: Product, name: str, value: str):
    owner_type, owner_id = validation.owner_for(
        name,
        spu=product.spu,
        variant_id=variants.variant_id_for(product),
        sku=product.sku,
    )
    row = ProductAttributeValue(
        owner_type=owner_type.value,
        owner_id=owner_id,
        field_name=name,
        value_json=value,
        status=AttributeStatus.CONFIRMED.value,
        source=AttributeSource.MANUAL.value,
        is_current=True,
    )
    session.add(row)
    session.flush()
    return row


def _copy_rows(session, spu: Spu) -> list[ListingCopy]:
    return list(
        session.scalars(
            select(ListingCopy).where(ListingCopy.spu == spu.spu_code)
        )
    )


def _plan_count(session, spu: Spu) -> int:
    return session.scalar(
        select(func.count(ContentPlan.id)).where(ContentPlan.spu == spu.spu_code)
    )


# ================================================ 一、F-1 自动确认也要投影


def _calibrated(session, *, field_name: str, model_name: str, prompt_version: str):
    """一条让 `system_confidence` 算得出高值的校准。

    没有它,`decide_status` 在 `system_confidence is None` 那一档就返回
    CANDIDATE —— 自动确认那条路走不到,而**这正是 F-1 长期没有暴露的原因**:
    没跑过校准的部署根本到不了那一档。所以这条夹具本身是本节的前提,
    不是装饰。
    """
    session.add(
        AttributeCalibration(
            field_name=field_name,
            model_name=model_name,
            prompt_version=prompt_version,
            bin_lower=0.9,
            bin_upper=1.0,
            observed_accuracy=0.99,
            sample_count=500,
            calibration_version="v1",
        )
    )
    session.flush()


def _extraction_with_colour_evidence(session, product: Product, assets, value: str):
    """一次识别:每张图各一条同值的颜色证据。

    **三张图、且每张都有清晰度指标**,不是随手写的数量。`system_confidence`
    是几个因子的乘积:单图的一致性因子只有 0.85、没测过质量的图按 0.85 算,
    于是 0.99 的校准值乘下来是 0.72 —— 低于 identity 组的 0.95 阈值,
    判定会停在 SUGGESTED,而本节要验的**自动确认那一档根本走不到**。

    这件事本身值得记:自动确认在真实数据上并不容易触发,而这正是 F-1
    躲了几批的第二层原因 —— 它需要校准数据**加上**一组足够好的证据。
    """
    extraction = ProductAttributeExtraction(
        product_id=product.id,
        spu_id=product.spu_id,
        status="SUCCEEDED",
        extractor="scripted",
        model_name="scripted-model",
        prompt_version="scripted-1",
        idempotency_key=uuid.uuid4().hex,
    )
    session.add(extraction)
    session.flush()
    for asset in assets:
        session.add(
            AttributeEvidence(
                extraction_id=extraction.id,
                media_asset_id=asset.id,
                field_name="primary_color",
                raw_value_json=value,
                normalized_value=value,
                model_confidence=0.97,
                model_name="scripted-model",
                prompt_version="scripted-1",
            )
        )
    session.flush()
    return extraction


def test_an_automatically_confirmed_colour_reaches_the_display_name(session):
    """**本文件的第一条,也是 F-1 的全部内容。**

    识别合并把颜色判成 CONFIRMED(置信度 ≥ 组阈值且库里为空),而 batch23
    的投影只挂在 `confirm()` 上 —— 于是这条路写出来的事实是 CONFIRMED,
    `display_name` 仍是空串。后端 5 处读、前端 7 处显示,没有一处会报错。

    这条用例同时是那句"当前没暴露是因为没跑过校准"的实证:上面那条
    校准夹具一旦拿掉,`decide_status` 会在未校准档返回 CANDIDATE,
    本用例的前置断言(状态是 CONFIRMED)当场失败。
    """
    spu = _spu(session)
    colour = _variant(session, spu, "BLK")
    product = _product(session, spu, colour)
    assets = [_asset(session, product, spu, colour, sharp=True) for _ in range(3)]
    _calibrated(
        session,
        field_name="primary_color",
        model_name="scripted-model",
        prompt_version="scripted-1",
    )
    extraction = _extraction_with_colour_evidence(session, product, assets, "Auto Black")

    assert get_field("primary_color").auto_confirm_threshold is not None, (
        "颜色字段被改成了永不自动确认 —— 那样这条缺口不存在,但本用例也失去意义"
    )
    assert colour.display_name == "", "夹具本身就带了颜色名,证明不了投影发生过"

    written = attr_service.apply_evidence(
        session, product=product, extraction=extraction, actor="system"
    )
    session.flush()

    fact = next(r for r in written if r.field_name == "primary_color")
    assert fact.status == AttributeStatus.CONFIRMED.value, (
        f"识别没有自动确认颜色(状态 {fact.status}) —— 校准夹具没生效,"
        "本用例证明不了 F-1"
    )
    assert fact.owner_type == OwnerType.VARIANT.value

    session.refresh(colour)
    assert colour.display_name == "Auto Black", (
        "自动确认出来的颜色没有投影到 display_name —— F-1 原样还在"
    )


def test_the_manual_path_still_projects_after_the_move(session):
    """人工确认那条路没有因为调用点搬家而丢掉投影。

    batch23 的用例全部走 `confirm()`,而 batch24 把调用点挪进了 `set_value`。
    搬家最省事的错法是"挪过去、忘了原来那条路也经过这里" —— 那样这条会红,
    而上面那条仍然绿。两条一起才说明"两条路都走同一跳"。
    """
    spu = _spu(session)
    colour = _variant(session, spu, "RED")
    product = _product(session, spu, colour)

    attr_service.confirm(
        session, product=product, field_name="primary_color", value="Coral Red",
        actor="operator@example.com",
    )
    session.flush()
    session.refresh(colour)
    assert colour.display_name == "Coral Red"


def test_a_suggested_colour_never_reaches_the_display_name(session):
    """**反向**:没被确认的颜色不许写进正式颜色名。

    投影挂到写入边界上之后,这条是必须的另一半:边界上流过的不只是
    CONFIRMED,还有 SUGGESTED / CANDIDATE / CONFLICT。少了状态门,
    一个模型猜测会直接显示成这个颜色的正式名字,而界面上看起来完全正常。
    """
    spu = _spu(session)
    colour = _variant(session, spu, "BLU")
    product = _product(session, spu, colour)
    owner_type, owner_id = validation.owner_for(
        "primary_color",
        spu=product.spu,
        variant_id=variants.variant_id_for(product),
        sku=product.sku,
    )

    attr_service.set_value(
        session,
        owner_type=owner_type,
        owner_id=owner_id,
        field_name="primary_color",
        value="Guessed Blue",
        source=AttributeSource.EXTRACTED,
        status=AttributeStatus.SUGGESTED,
    )
    session.flush()
    session.refresh(colour)
    assert colour.display_name == "", (
        "一个 SUGGESTED 的猜测被写成了正式颜色名"
    )


# ================================================ 二、F-6 文案的颜色粒度


def _two_colour_spu(session):
    """双色 SPU,每色一个尺码,共享事实齐全,每色各有自己的颜色事实。"""
    spu = _spu(session)
    red = _variant(session, spu, "RED")
    blue = _variant(session, spu, "BLU")
    red_sku = _product(session, spu, red)
    blue_sku = _product(session, spu, blue)

    for name, value in _SHARED_FACTS:
        _confirmed_fact(session, red_sku, name, value)
    _confirmed_fact(session, red_sku, "primary_color", "Coral Red")
    _confirmed_fact(session, blue_sku, "primary_color", "Navy Blue")
    session.flush()
    return spu, red_sku, blue_sku


def test_two_colours_do_not_regenerate_each_other(session):
    """**F-6 的字面场景。** 红 → 蓝 → 红,三次点击只花两次钱。

    0051 之前:键含颜色(经事实版本集)而槽只到 SPU,于是每一次点击都
    判"不命中 → 生成",三次点击三次付费;而且每一版都把上一个颜色的
    当前版挤成 ARCHIVED —— "当前文案"在两个颜色之间摆动。
    """
    spu, red_sku, blue_sku = _two_colour_spu(session)

    wb.generate_copy(session, red_sku, actor="op")
    session.flush()
    after_red = len(_copy_rows(session, spu))

    wb.generate_copy(session, blue_sku, actor="op")
    session.flush()
    after_blue = len(_copy_rows(session, spu))
    assert after_blue == after_red + 1, "蓝色没有生成自己的那一版"

    wb.generate_copy(session, red_sku, actor="op")
    session.flush()
    rows = _copy_rows(session, spu)
    assert len(rows) == 2, (
        f"回到红色又生成了一版(共 {len(rows)} 版) —— "
        "在颜色轴上来回点每次都要付费,F-6 原样还在"
    )

    by_colour = {r.color_variant_id: r for r in rows}
    assert len(by_colour) == 2, "两版文案落在同一个颜色槽里"
    assert all(r.status != CopyStatus.ARCHIVED.value for r in rows), (
        "其中一版被挤成历史 —— 两个颜色的文案在互相覆盖"
    )


def test_the_reused_colour_creates_no_content_plan_garbage(session):
    """复用那条路不建内容计划(口径同 batch21,这里按颜色再验一次)。

    `_content_plan_or_raise` 每次都 `session.add` 一行并写一条审计 ——
    放在判定之前的话,复用路径会安静地攒垃圾行,而"复用了几次"在审计里
    看起来和"生成了几次"一模一样。
    """
    spu, red_sku, _ = _two_colour_spu(session)
    wb.generate_copy(session, red_sku, actor="op")
    session.flush()
    plans = _plan_count(session, spu)

    wb.generate_copy(session, red_sku, actor="op")
    session.flush()
    assert _plan_count(session, spu) == plans, "复用路径建了一份新的内容计划"


def test_each_colour_carries_its_own_colour_word(session):
    """两个颜色的文案内容真的不同 —— 这是"必须按颜色分槽"的理由本身。

    如果标题里没有颜色词,那么"复用另一个颜色的那一版"就没有代价,
    整个 F-6 也就不成立。这条把那个前提钉住:改动生成模板让颜色不再进
    标题的话,它会红,提醒下一个人重新想一遍分槽还需不需要。
    """
    spu, red_sku, blue_sku = _two_colour_spu(session)
    wb.generate_copy(session, red_sku, actor="op")
    wb.generate_copy(session, blue_sku, actor="op")
    session.flush()

    titles = {r.color_variant_id: (r.title or "") for r in _copy_rows(session, spu)}
    assert len(titles) == 2
    red_title = titles[str(red_sku.color_variant_id)]
    blue_title = titles[str(blue_sku.color_variant_id)]
    assert "Coral" in red_title and "Navy" in blue_title, (
        f"颜色词没有进标题:{titles} —— 那样按颜色分槽就失去了理由"
    )
    assert red_title != blue_title


def test_the_version_sequences_of_two_colours_are_independent(session):
    """两个颜色各有一条版本序列。

    共用一条的话,"这个颜色改到第几版了"答不出来,而 `_current_copy`
    取 max(version) 会在两个颜色之间摆动 —— F-6 的现象换个形状回来。
    """
    spu, red_sku, blue_sku = _two_colour_spu(session)
    wb.generate_copy(session, red_sku, actor="op")
    wb.generate_copy(session, blue_sku, actor="op")
    wb.generate_copy(session, red_sku, force=True, actor="op")
    session.flush()

    versions: dict[str, list[int]] = {}
    for row in _copy_rows(session, spu):
        versions.setdefault(row.color_variant_id, []).append(row.version)
    assert sorted(versions[str(red_sku.color_variant_id)]) == [1, 2]
    assert versions[str(blue_sku.color_variant_id)] == [1], (
        "蓝色的版本号被红色的重生成推高了 —— 两条序列没有分开"
    )


def test_a_legacy_copy_in_the_sentinel_slot_is_never_reused(session):
    """0051 之前的存量行(哨兵槽)不会被当成任何颜色的当前文案。

    回落的表现是:红色还没有文案时,界面把那版可能写着别的颜色的旧文案
    显示成红色的当前文案,而运营看不出它不是自己这个颜色的。
    宁可多生成一次 —— 与 0050 对 NULL 键判 GENERATE 同一个方向。
    """
    spu, red_sku, _ = _two_colour_spu(session)
    plan = ContentPlan(spu=spu.spu_code, facts={}, selling_points=[], forbidden_claims=[])
    session.add(plan)
    session.flush()
    session.add(
        ListingCopy(
            spu=spu.spu_code,
            channel=generic.CHANNEL,
            site=generic.SITE,
            locale=generic.LOCALE,
            version=1,
            content_plan_id=plan.id,
            status=CopyStatus.APPROVED.value,
            title="Legacy Title From The SPU Slot",
            bullet_points=["a"],
            description="d",
            keywords=["k"],
        )
    )
    session.flush()

    current = wb._current_copy(session, spu.spu_code, variants.variant_id_for(red_sku))
    assert current is None, "存量哨兵槽的那一版被当成了这个颜色的当前文案"


# ================================================ 三、AC-19 预览与导出同源


def _two_front_images_one_marked_primary(session):
    """一个颜色下两张合法正面图,**显式主图不是排序第一张**。

    这是老师们在真库上复现 AC-19 分叉的那个形状,也是它最常见的来法:
    运营先传了一张,后来换了更好的一张并把它设成主图,而排序没动。
    """
    spu = _spu(session)
    colour = _variant(session, spu, "CRL")
    product = _product(session, spu, colour)
    first = _asset(session, product, spu, colour)
    marked = _asset(session, product, spu, colour)
    _image_set(
        session,
        spu,
        [(colour, first, 0, False), (colour, marked, 5, True)],
    )
    _approved_copy(session, spu, colour)
    session.flush()
    return spu, colour, product, first, marked


def _move_primary_to(session, image_set_id, asset: MediaAsset) -> None:
    """把主图标记挪到另一张上。**先全部清空、flush,再置新的。**

    `uq_image_primary` 是 `(image_set_id, COALESCE(variant_id, ''))` 上的
    部分唯一索引,而 SQLAlchemy 的 flush 顺序不保证"先清后置" ——
    一条 UPDATE 语句里两行同时存在 True 的瞬间就会撞约束。
    这不是被测代码的问题,是夹具必须按库的形状写。
    """
    items = list(
        session.scalars(
            select(ListingImageItem).where(
                ListingImageItem.image_set_id == image_set_id
            )
        )
    )
    for item in items:
        item.is_primary = False
    session.flush()
    for item in items:
        if str(item.media_asset_id) == str(asset.id):
            item.is_primary = True
    session.flush()


def test_the_preview_and_the_export_pick_the_same_image(session):

    """**AC-19 的字面要求,也是这一节的全部内容。**

    修复前:预览读 `color_sku_image_map`(显式主图),最终 `mapped_payload`
    由 `map_fields` 自己按 `sort_order` 挑(排序第一张) —— 一份合法且
    READY 的图片集,预览显示 A 图、Excel 与 API 报文用 B 图,两边都不报错。
    """
    spu, colour, product, first, marked = _two_front_images_one_marked_primary(session)

    row = wb.build_draft(session, product, actor="batch24")
    session.flush()

    preview = wb.draft_preview(session, product, row)
    images = preview["images"]
    assert images["status"] == ep.MAP_READY
    preview_primary = images["colors"][0]["rows"][0]["primary"]
    assert preview_primary == str(marked.id), (
        "预览没有取显式主图 —— 这条用例的前提(两套规则会分叉)不成立了"
    )

    exported = row.mapped_payload["rows"][0]["variant_image"]
    assert exported == marked.storage_path, (
        f"导出用的是 {exported},而预览显示的是 {marked.storage_path} —— "
        "AC-19 的分叉原样还在"
    )
    assert exported != first.storage_path


def test_the_export_keeps_the_stored_choice_after_the_upstream_moves(session):
    """上游变了而草稿没重建时,导出仍用**存下来的那一份**。

    这是"读同一份已存映射"的第二层含义:两边同源还不够,同的必须是
    **已存**的那一份。重新推断的一侧永远自洽 —— 它显示"当前该用哪些图",
    而另一侧用的是草稿生成那一刻存下来的那些。
    """
    spu, colour, product, first, marked = _two_front_images_one_marked_primary(session)
    row = wb.build_draft(session, product, actor="batch24")
    session.flush()
    stored = row.mapped_payload["rows"][0]["variant_image"]

    # 上游动了:把主图标记挪到另一张上(不重建草稿)
    _move_primary_to(session, row.image_set_id, first)

    preview = wb.draft_preview(session, product, row)
    assert preview["images"]["colors"][0]["rows"][0]["primary"] == str(marked.id), (
        "预览跟着当前上游动了 —— 它必须读落库的那一份"
    )
    assert row.mapped_payload["rows"][0]["variant_image"] == stored


def test_rebuilding_the_draft_moves_both_sides_together(session):
    """重建草稿之后,两边**一起**换到新的选择。

    一次计算两个消费者的正面表述:上一条证明两边一起"不动",这一条证明
    两边一起"动"。少了这一条,一个把映射写死成常量的实现也能通过上一条。
    """
    spu, colour, product, first, marked = _two_front_images_one_marked_primary(session)
    wb.build_draft(session, product, actor="batch24")
    session.flush()

    _move_primary_to(
        session, wb._current_draft(session, spu.spu_code).image_set_id, first
    )


    row = wb.build_draft(session, product, actor="batch24")
    session.flush()
    preview = wb.draft_preview(session, product, row)
    assert preview["images"]["colors"][0]["rows"][0]["primary"] == str(first.id)
    assert row.mapped_payload["rows"][0]["variant_image"] == first.storage_path, (
        "重建之后导出侧没跟上 —— 两边又各读各的了"
    )


def test_a_colour_missing_from_the_map_gets_no_borrowed_image(session):
    """映射里没有主图的那一行,导出**空着**,不借别的图。

    §6.5 / BLOCK-02:「不得回退使用其他颜色的图片,缺图就是缺图」。
    修复前导出这一侧会回落到 SPU 通用图,于是预览说缺、READY 门禁据此
    拦下这份草稿,而 Excel 那一行填着一张通用图 —— 两个答案。
    """
    spu = _spu(session)
    colour = _variant(session, spu, "GRN")
    product = _product(session, spu, colour)
    generic_asset = _asset(session, product, spu, None)
    _image_set(session, spu, [(None, generic_asset, 0, True)])
    _approved_copy(session, spu, colour)
    session.flush()

    row = wb.build_draft(session, product, actor="batch24")
    session.flush()

    assert row.mapped_payload["rows"][0]["variant_image"] is None, (
        "这个颜色在映射里没有主图,导出却给它填了一张通用图"
    )
    problems = [
        v["message"]
        for v in row.validation_errors
        if v["code"] == wb.COLOR_READY_VIOLATION_CODE
    ]
    assert problems, "缺图的颜色没有被 READY 门禁拦下"
