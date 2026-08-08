"""A45-batch21(阶段 5 批次 5-3):文案幂等的真库用例。

## 为什么非要真库

纯层验的是键算得对、判定挑得对。而 AC-11 要的是一句**关于行为**的话:
「同一 SPU、同一语言、同一确认事实版本集下,S/M/L 三行 SKU 只建立一个
文案生成幂等单元;并发或重试不产生第二次付费生成。」

那句话里的每一个名词都在库里:三行 SKU 是 `products` 的三行,
"一个单元"是 `listing_copies` 里**没有多出来的那两个版本**,
"付费生成"是生成器**没有被调到的那两次**。

纯层能证明键相等,证明不了「点第二次时真的没有新版本落库」——
而那两件事之间隔着 `_content_plan_or_raise`、advisory lock 与 `save_copy`
三段代码,每一段都能把这笔账重新花出去。

## 计数怎么做

不 mock 生成器,而是数**真的落了几行**:`listing_copies` 的版本数、
`content_plans` 的行数。默认配置下生成器是模板(不花钱),所以
"调了几次"要靠副作用来数 —— 而副作用恰好就是这笔账的形状:
多一个版本 = 多一次调用 = 多一次付费(真接 LLM 时)。
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.attributes import validation
from app.core.enums import AttributeStatus, CopyStatus, SellableStatus, SpuStatus
from app.listings import variants
from app.models.attribute import ProductAttributeValue
from app.models.listing_copy import ContentPlan, ListingCopy
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.workbench import service as wb
from tests.conftest import requires_db

pytestmark = requires_db


def _spu(session) -> Spu:
    row = Spu(
        spu_code=f"CP{uuid.uuid4().hex[:6].upper()}",
        internal_name="文案幂等测试款",
        audience="WOMEN",
        base_category="swimwear",
        status=SpuStatus.DRAFT.value,
    )
    session.add(row)
    session.flush()
    return row


def _product(session, spu: Spu, size: str) -> Product:
    row = Product(
        spu=spu.spu_code,
        spu_id=spu.id,
        sku=f"{spu.spu_code}-BLK-{size}",
        name="测试款",
        category="swimwear",
        audience="WOMEN",
        size=size,
    )
    session.add(row)
    session.flush()
    return row


def _confirmed_fact(session, product: Product, name: str, value: str):
    """一条已确认属性。文案生成要求至少有一条,否则 `_content_plan_or_raise` 拒绝。

    owner 走注册表(`validation.owner_for`),不手写 `(SPU, product.id)` ——
    手写的话这条值会落在 A43 之前那个 legacy 位置上,而 `effective_map`
    对 legacy 位置有一条回落链。用回落链喂本文件的断言等于验了一条
    **正在退役**的路径:幂等键读的是 `_confirmed_version_ids`,
    而那边走的是分层之后的新位置。
    """
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
        source="MANUAL",
        is_current=True,
    )
    session.add(row)
    session.flush()
    return row


#: 够生成一版**合格**文案的最小事实集。
#:
#: 少一条就 REJECTED,而 REJECTED 不可复用 —— 于是整个文件会变成
#: "每次都重新生成",复用一条都验不到。第一版只写了 `material`,
#: 拿到的正是那个结果:三个版本、断言全红,而幂等逻辑其实是对的。
#:
#: 换句话说:**这个夹具的完整性本身是本文件能不能验到东西的前提。**
_FACTS = (
    ("material", "Nylon"),
    ("garment_type", "BIKINI_SET"),
    ("pattern_type", "SOLID"),
    ("neckline_type", "V_NECK"),
    ("primary_color", "Black"),
)


def _three_sizes(session):
    """一个 SPU + 一个 ACTIVE 颜色 + 三个尺码 + 一组已确认事实。

    ## 两个坑,都是第一版踩过的

    **事实只写一次。** `material` 等在注册表里是 SPU 层字段,三行商品的
    `owner_id` 都是同一个 SPU 码,按行写三次会撞 `uq_pav_current`。
    那次撞击本身就是本批次前提在库层面的证明:已确认事实是 SPU 级的,
    所以 S/M/L 三行看到的是同一个事实版本集,于是同一个幂等键。

    **颜色变体必须真的存在。** `primary_color` 是 VARIANT 层字段,
    `validation.owner_for` 要求 owner 是 `color_variants.id`;
    不建颜色行的话它当场抛 `AttributeOwnerError`,而错误信息指向的是
    「还没有回填 products.color_variant_id」—— 那是阶段 1 的欠账,
    和本批次无关,但会把这个文件挡在门外。
    """
    spu = _spu(session)
    colour = ColorVariant(
        spu_id=spu.id,
        variant_code="BLK",
        working_name="BLK",
        sellable_status=SellableStatus.ACTIVE.value,
    )
    session.add(colour)
    session.flush()

    rows = []
    for size in ("S", "M", "L"):
        row = _product(session, spu, size)
        row.color_variant_id = colour.id
        rows.append(row)
    session.flush()

    for name, value in _FACTS:
        _confirmed_fact(session, rows[0], name, value)
    return spu, rows


def _copy_count(session, spu: Spu) -> int:
    return session.scalar(
        select(func.count(ListingCopy.id)).where(ListingCopy.spu == spu.spu_code)
    )


def _plan_count(session, spu: Spu) -> int:
    return session.scalar(
        select(func.count(ContentPlan.id)).where(ContentPlan.spu == spu.spu_code)
    )


# ---------------------------------------------------------------- 一、AC-11 正文


def test_three_sizes_produce_one_copy_unit_not_three(session):
    """**本文件的第一条,也是 AC-11 的字面场景。**

    S/M/L 三行各点一次「生成文案」。在幂等键之前这是三次真实调用、三个版本;
    现在应当是一次调用、一个版本,后两次直接复用。
    """
    spu, rows = _three_sizes(session)

    first = wb.generate_copy(session, rows[0], actor="batch21")
    session.flush()
    assert _copy_count(session, spu) == 1

    second = wb.generate_copy(session, rows[1], actor="batch21")
    third = wb.generate_copy(session, rows[2], actor="batch21")
    session.flush()

    assert _copy_count(session, spu) == 1, (
        "S/M/L 三行各生成了一个版本 —— AC-11 的「只建立一个幂等单元」不成立,"
        "接真实 LLM 时这就是三次付费调用"
    )
    assert second.id == first.id and third.id == first.id, (
        "复用没有返回同一行"
    )


def test_the_reused_path_creates_no_content_plan_garbage(session):
    """复用路径不建 `ContentPlan`,也不写生成审计。

    `_content_plan_or_raise` 每次都 `session.add` 一行并写一条审计。
    放在判定之前的话,这笔账**看起来还是没省下来**:审计里躺着三条
    "生成文案",而运营正是靠它数复用省了多少。
    """
    spu, rows = _three_sizes(session)
    wb.generate_copy(session, rows[0], actor="batch21")
    session.flush()
    after_first = _plan_count(session, spu)

    wb.generate_copy(session, rows[1], actor="batch21")
    wb.generate_copy(session, rows[2], actor="batch21")
    session.flush()

    assert _plan_count(session, spu) == after_first, (
        "复用路径仍然在建内容计划 —— 判定排在 `_content_plan_or_raise` 之后了"
    )


def test_the_key_is_actually_written_to_the_row(session):
    """键真的落库。不落的话下一次必然不命中,等于没有幂等。"""
    spu, rows = _three_sizes(session)
    row = wb.generate_copy(session, rows[0], actor="batch21")
    session.flush()
    session.expire(row)
    assert row.idempotency_key, "`idempotency_key` 是空的"
    assert row.idempotency_key.startswith("c1-")


# ---------------------------------------------------------------- 二、该重算的要重算


def test_confirming_a_new_fact_produces_a_new_version(session):
    """已确认事实变了 -> 键变 -> 重新生成(AC-10 的隔离在这里落地)。

    这是幂等的**反面那一半**。只钉复用的话,最省事的实现是"有就返回",
    而那样文案会永远停在第一次生成的那一版 —— 运营确认了新属性,
    重新生成一次,拿回来的还是旧文案,而没有任何东西报错。
    """
    spu, rows = _three_sizes(session)
    first = wb.generate_copy(session, rows[0], actor="batch21")
    session.flush()

    _confirmed_fact(session, rows[0], "strap_type", "ADJUSTABLE")
    session.flush()

    second = wb.generate_copy(session, rows[0], actor="batch21")
    session.flush()
    assert second.id != first.id, "确认了一条新事实,文案却没有重新生成"
    assert _copy_count(session, spu) == 2
    assert second.idempotency_key != first.idempotency_key


def test_a_rejected_version_does_not_block_the_next_attempt(session):
    """失败态不占槽位 —— 否则这个 SPU 的文案永远修不好。

    现象会是"点生成没反应":每次都返回那一版规则硬失败的稿子。
    """
    spu, rows = _three_sizes(session)
    first = wb.generate_copy(session, rows[0], actor="batch21")
    session.flush()

    first.status = CopyStatus.REJECTED.value
    session.flush()

    second = wb.generate_copy(session, rows[0], actor="batch21")
    session.flush()
    assert second.id != first.id, (
        "上一版是 REJECTED 却被复用了 —— 这个 SPU 的文案再也生成不出来"
    )


def test_force_regenerates_even_when_nothing_changed(session):
    """`force=True` 是运营的明确重做意图,不受复用拦截。"""
    spu, rows = _three_sizes(session)
    first = wb.generate_copy(session, rows[0], actor="batch21")
    session.flush()

    second = wb.generate_copy(session, rows[0], force=True, actor="batch21")
    session.flush()
    assert second.id != first.id, "`force=True` 被复用吃掉了"
    assert _copy_count(session, spu) == 2


def test_single_field_regeneration_still_works(session):
    """单字段重生蕴含 force —— 否则「重新生成标题」会变成"点了没反应"。"""
    spu, rows = _three_sizes(session)
    first = wb.generate_copy(session, rows[0], actor="batch21")
    session.flush()

    second = wb.generate_copy(
        session, rows[0], only_fields=["title"], actor="batch21"
    )
    session.flush()
    assert second.id != first.id, "单字段重生被幂等吃掉了"


# ---------------------------------------------------------------- 三、边界


def test_two_different_spus_never_share_a_unit(session):
    """两个 SPU 各生成各的。键少一个轴的表现是**拿别人的文案复用**。"""
    spu_a, rows_a = _three_sizes(session)
    spu_b, rows_b = _three_sizes(session)

    copy_a = wb.generate_copy(session, rows_a[0], actor="batch21")
    copy_b = wb.generate_copy(session, rows_b[0], actor="batch21")
    session.flush()

    assert copy_a.id != copy_b.id
    assert copy_a.idempotency_key != copy_b.idempotency_key
    assert _copy_count(session, spu_a) == 1
    assert _copy_count(session, spu_b) == 1
