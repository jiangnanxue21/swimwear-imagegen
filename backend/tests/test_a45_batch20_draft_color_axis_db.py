"""A45-batch20(阶段 5 批次 5-2):草稿颜色维接线的真库用例。

## 为什么这几条非要真库,而且为什么它们是本批最要紧的东西

5-1 把判定层验干净了(29 条纯守卫 + 13 条变异),而**判定验得干不干净和
它有没有被真的调用完全无关**。§3.43 已经把这一类点过名:纯层全绿、门禁
全绿,而功能整条不通。

本批开工时的事实是:**全仓一条测试都没有调用过 `build_draft`。**
`grep -rn build_draft tests/` 只命中 5-1 那份纯守卫里的文本断言。
也就是说接线做完之后,整套 2881 条真库用例照样全绿 —— 它们一条都不走这条路。

所以这个文件补的不是"再多一层保险",是这条链路的**第一份**执行证据:

    一  两列真的被写进 PostgreSQL 的 JSONB,而且读回来形状不变
    二  颜色轴真的会让草稿过期 —— 而 `source_fingerprint` **算不出**这件事
        (它的输入里没有任何一项带颜色),所以少接这一跳的表现是
        "新增了一个 ACTIVE 颜色,草稿说自己没过期",且没有任何东西报错
    三  §6.7 的**反向那一半**:PLANNED 颜色怎么折腾都不许让草稿过期。
        纯层能验它,但纯层验的是一个手搓的 `ColorUpstream`;真库验的是
        `upstream_collect.collect()` 到底有没有把 `sellable_status` 读对
    四  READY 门禁真的会把草稿判成 INVALID,而不是产出一堆没人看的 warning

第四条尤其要真库:`FieldViolation.is_blocking` 与 `DraftStatus` 之间那一跳
写在 `build_draft` 里,而 `_color_ready_violations` 的返回值在纯层看起来
永远是对的 —— 它返回 `level="error"` 的那一刻就"验过了",至于 `build_draft`
有没有把它拼进 `violations` 那个列表,纯层看不见。

## 顺序

    alembic upgrade head
    pytest tests/test_a45_batch20_draft_color_axis_db.py
"""
from __future__ import annotations

import uuid

from app.channels import generic
from app.core.enums import (
    CopyStatus,
    DraftStatus,
    ImageSetStatus,
    MediaRole,
    MediaSource,
    MediaStatus,
    RoleSource,
    SellableStatus,
    SpuStatus,
)
from app.models.listing_copy import ContentPlan, ListingCopy
from app.models.listing_image import ListingImageItem, ListingImageSet
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.workbench import service as wb
from app.workbench import upstream_collect
from tests.conftest import requires_db

pytestmark = requires_db


# ---------------------------------------------------------------- 夹具
#
# 手写而不是复用 `spu_service.create_spu`:那条路径会把受众、SKU 矩阵、
# 素材完整度一并拉进来,而本文件要验的是**颜色维快照**这一跳。
# 建档路径自己的正确性由 batch13 的真库用例守着,在这里再走一遍只会让
# 一条颜色维的失败看起来像是建档坏了。


def _spu(session) -> Spu:
    row = Spu(
        spu_code=f"SP{uuid.uuid4().hex[:6].upper()}",
        internal_name="阶段五颜色轴测试款",
        audience="WOMEN",
        base_category="swimwear",
        status=SpuStatus.DRAFT.value,
    )
    session.add(row)
    session.flush()
    return row


def _variant(session, spu: Spu, code: str, status: str) -> ColorVariant:
    row = ColorVariant(
        spu_id=spu.id,
        variant_code=code,
        working_name=code,
        sellable_status=status,
    )
    session.add(row)
    session.flush()
    return row


def _product(session, spu: Spu, variant: ColorVariant, size: str) -> Product:
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


def _asset(session, product: Product, spu: Spu, variant: ColorVariant | None) -> MediaAsset:
    row = MediaAsset(
        product_id=product.id,
        spu_id=spu.id,
        color_variant_id=None if variant is None else variant.id,
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


def _approved_image_set(session, spu: Spu, per_colour) -> ListingImageSet:
    """一版已批准的图片集:每个颜色一张属于它自己的主图。

    `variant_id` 写的是 `str(color_variants.id)` —— 迁移 0046 之后图片项的
    变体标签就是这个,与 `products.color_variant_id` 同源。写成颜色名的话
    §6.5 会把它判成 `FOREIGN_VARIANT_IMAGE`,而那正是身份分叉的表现。
    """
    image_set = ListingImageSet(
        spu=spu.spu_code,
        version=1,
        status=ImageSetStatus.APPROVED.value,
        approved_by="batch20-db-test",
    )
    session.add(image_set)
    session.flush()
    for order, (variant, asset) in enumerate(per_colour):
        session.add(
            ListingImageItem(
                image_set_id=image_set.id,
                media_asset_id=asset.id,
                role=MediaRole.PRODUCT_FRONT.value,
                sort_order=order,
                variant_id=str(variant.id),
                is_primary=True,
                enabled=True,
            )
        )
    session.flush()
    return image_set


def _approved_copy(session, spu: Spu) -> ListingCopy:
    plan = ContentPlan(spu=spu.spu_code, facts={}, selling_points=[], forbidden_claims=[])
    session.add(plan)
    session.flush()
    row = ListingCopy(
        spu=spu.spu_code,
        channel=generic.CHANNEL,
        site=generic.SITE,
        locale=generic.LOCALE,
        version=1,
        content_plan_id=plan.id,
        status=CopyStatus.APPROVED.value,
        title="Test swimwear",
        bullet_points=["a", "b"],
        description="d",
        keywords=["k"],
    )
    session.add(row)
    session.flush()
    return row


def _ready_two_colour_spu(session, *, second_status=SellableStatus.ACTIVE.value):
    """一个双色 SPU,每色两个尺码,每色一张自有主图,文案与图片集都已批准。"""
    spu = _spu(session)
    red = _variant(session, spu, "RED", SellableStatus.ACTIVE.value)
    blue = _variant(session, spu, "BLU", second_status)

    products = {}
    for variant in (red, blue):
        products[variant.variant_code] = [
            _product(session, spu, variant, size) for size in ("S", "M")
        ]

    per_colour = [
        (variant, _asset(session, products[variant.variant_code][0], spu, variant))
        for variant in (red, blue)
    ]
    _approved_image_set(session, spu, per_colour)
    _approved_copy(session, spu)
    return spu, red, blue, products


def _colour_problems(row) -> list[str]:
    """草稿上**颜色维**的那几条阻断项。

    断言按这个来,不按 `row.status`。理由是本文件的夹具刻意不填渠道必填字段
    (品牌 / 材质 / 价格 / 库存那一串)—— 填齐它们要把属性注册表、确认流与
    价格库一起拉进来,而那些和颜色轴一个字的关系都没有。

    **但代价必须说清楚:`status` 在这些夹具下恒为 INVALID**,所以
    「颜色维把草稿拦下了」这件事不能靠 `status == INVALID` 来证明 ——
    那个断言在颜色维完全没接线的时候也是绿的。按违规码分组才分得开。
    """
    return [
        v["message"]
        for v in row.validation_errors
        if v["code"] == wb.COLOR_READY_VIOLATION_CODE
    ]


def _first_product(session, spu: Spu) -> Product:
    return (
        session.query(Product)
        .filter(Product.spu == spu.spu_code)
        .order_by(Product.sku)
        .first()
    )


# ---------------------------------------------------------------- 一、落库


def test_both_snapshot_columns_survive_a_round_trip_through_jsonb(session):
    """两列真的写进 PostgreSQL,而且 `expire_on_commit=False` 之后读回来形状不变。

    纯层验过「快照能 JSON round-trip」,但那是 `json.dumps` / `json.loads`。
    JSONB **不保证键序**,而快照的比较是逐项的 —— 靠键序的实现在纯层全绿、
    在真库上表现为草稿每刷新一次就说自己过期一次。
    """
    spu, red, blue, _ = _ready_two_colour_spu(session)
    product = _first_product(session, spu)

    row = wb.build_draft(session, product, actor="batch20")
    session.flush()
    session.expire(row)

    assert row.upstream_versions is not None, "`upstream_versions` 没落库"
    assert row.color_sku_image_map is not None, "`color_sku_image_map` 没落库"

    colours = row.upstream_versions["colors"]
    assert set(colours) == {str(red.id), str(blue.id)}, (
        "快照里的颜色集合不是那两个 ACTIVE 颜色 —— 身份口径分叉了"
    )
    assert colours[str(red.id)]["variant_code"] == "RED"

    # AC-19 的「可解释的上游版本引用」:每个颜色记得下自己的映射是从哪一版
    # 图片集铺出来的。**这一格不参与比较**(那个轴归 `diff_components`),
    # 所以只有这里能证明它真的落进去了 —— 换一版图片集是不会有提示的。
    image_set_ref = colours[str(red.id)]["image_set"]
    assert image_set_ref and image_set_ref["version"] == 1, (
        "颜色没记下自己的图片集版本 —— 出事之后查不出这批图是从哪一版铺的"
    )

    mapped = row.color_sku_image_map["colors"][str(red.id)]["skus"]
    assert set(mapped) == {f"{spu.spu_code}-RED-S", f"{spu.spu_code}-RED-M"}, (
        "红色的两个尺码没有都铺进映射 —— §4.9「尺码不换图」要求全集铺开"
    )
    assert all(entry["primary"] for entry in mapped.values()), (
        "有 SKU 行拿不到主图,而 §6.5 说缺图就是缺图"
    )


# ---------------------------------------------------------------- 二、颜色轴过期


def test_a_new_active_colour_makes_the_draft_stale(session):
    """**本文件的第一位。** 新增一个 ACTIVE 颜色 -> 草稿过期。

    `source_fingerprint` 算不出这件事:它的输入是 SPU 属性 + 图片集 + 文案 +
    手填 + 两个版本号,**没有一项带颜色**。少了 `refresh_draft` 里那一跳,
    这个场景下 `is_stale` 是 False,界面说「上游全部有效」,而导出文件里
    新颜色的 SKU 行根本不存在。
    """
    spu, red, blue, _ = _ready_two_colour_spu(session)
    product = _first_product(session, spu)
    row = wb.build_draft(session, product, actor="batch20")
    session.flush()

    fresh_stale, fresh_changes = wb.refresh_draft(session, product, row, dry_run=True)
    assert not fresh_stale, f"刚生成的草稿就被判过期:{[c.message for c in fresh_changes]}"

    green = _variant(session, spu, "GRN", SellableStatus.ACTIVE.value)
    _product(session, spu, green, "S")
    session.flush()

    is_stale, changes = wb.refresh_draft(session, product, row, dry_run=True)
    assert is_stale, "新增 ACTIVE 颜色之后草稿仍说自己没过期 —— 颜色轴没接上"
    colour_set = [c for c in changes if c.component == "color_set"]
    assert colour_set, f"没有 color_set 那一条:{[c.component for c in changes]}"
    assert str(green.id) in colour_set[0].fields
    assert "GRN" in colour_set[0].message, "过期提示没点名是哪个颜色"
    assert colour_set[0].action, "过期提示没说该做什么(§4.5.1)"


def test_a_planned_colour_never_makes_the_draft_stale(session):
    """§6.7 的**反向那一半**:PLANNED 颜色怎么折腾都不许让草稿过期。

    正向漏掉时有人会在导出文件里看见错的行;反向漏掉时,一个还没到样的
    颜色会让整个 SPU 的草稿永远处在过期态 —— 运营重新生成一次它又过期,
    而提示里说的是一个他压根还没开始做的颜色。**第二种没有任何东西会报错**,
    只会让人不再相信过期提示。

    纯层也验这一条,但它验的是手搓的 `ColorUpstream`。这里验的是
    `collect()` 真的把 `sellable_status` 从库里读对了 —— 把那一列读成
    别的东西(比如 `status`)在纯层是看不见的。
    """
    spu, red, blue, _ = _ready_two_colour_spu(
        session, second_status=SellableStatus.PLANNED.value
    )
    product = _first_product(session, spu)
    row = wb.build_draft(session, product, actor="batch20")
    session.flush()

    assert set(row.upstream_versions["colors"]) == {str(red.id)}, (
        "PLANNED 颜色进了 colors 那一格 —— 它的事实一改草稿就会过期"
    )
    assert row.upstream_versions["inactive"] == {str(blue.id): SellableStatus.PLANNED.value}
    assert not row.upstream_versions["inactive"].get("fact_versions"), (
        "inactive 带上了版本引用"
    )

    # 给 PLANNED 颜色补一张样品图。共享指纹会变(素材集合变了),
    # 而那一条归 `shared_sample`;**颜色轴不该因此多出一条 BLU 的提示**。
    _asset(session, blue_product(session, spu, blue), spu, blue)
    session.flush()

    _, changes = wb.refresh_draft(session, product, row, dry_run=True)
    blue_mentions = [c for c in changes if str(blue.id) in c.fields or "BLU" in c.message]
    assert not blue_mentions, (
        f"一个 PLANNED 颜色让草稿报出了变化:{[c.message for c in blue_mentions]}"
    )


def blue_product(session, spu, blue):
    return session.query(Product).filter(Product.color_variant_id == blue.id).first()


def test_deactivating_a_colour_says_deactivated_not_deleted(session):
    """停用与删除对运营是两句不同的话(`inactive` 存在的全部理由)。"""
    spu, red, blue, _ = _ready_two_colour_spu(session)
    product = _first_product(session, spu)
    row = wb.build_draft(session, product, actor="batch20")
    session.flush()

    blue.sellable_status = SellableStatus.DISABLED.value
    session.flush()

    is_stale, changes = wb.refresh_draft(session, product, row, dry_run=True)
    assert is_stale
    (colour_set,) = [c for c in changes if c.component == "color_set"]
    assert colour_set.new_value == SellableStatus.DISABLED.value, (
        "停用被报成了「已删除」—— 两者对运营是两个不同的动作"
    )


def test_a_draft_built_before_the_snapshot_column_cannot_be_proven(session):
    """存量草稿(`upstream_versions IS NULL`)判**不可证明**,不是放行。

    这是 5-1 D2 定下的上线语义,而它只有在真库上才谈得上验证:
    NULL 是迁移 0049 留给存量行的状态,纯层没有"存量行"这个东西。
    """
    spu, _, _, _ = _ready_two_colour_spu(session)
    product = _first_product(session, spu)
    row = wb.build_draft(session, product, actor="batch20")
    session.flush()

    row.upstream_versions = None  # 0049 之前建的那些草稿就长这样
    session.flush()

    is_stale, changes = wb.refresh_draft(session, product, row, dry_run=True)
    assert is_stale, "没有颜色维快照的草稿被静默放行了"
    assert [c.component for c in changes] == ["upstream_snapshot"]
    assert "无法证明" in changes[0].message


# ---------------------------------------------------------------- 三、READY 门禁


def test_a_colour_without_its_own_image_blocks_the_draft(session):
    """AC-18:图片集规则那一侧失败 -> 草稿 INVALID,不是 warning。

    这里拆掉蓝色那张自有图。§6.5:通用图不算覆盖、不得回退用别的颜色的图,
    所以蓝色变成"一张属于自己的图都没有"。
    """
    spu, red, blue, _ = _ready_two_colour_spu(session)
    image_set = session.query(ListingImageSet).filter(
        ListingImageSet.spu == spu.spu_code
    ).first()
    blue_item = session.query(ListingImageItem).filter(
        ListingImageItem.image_set_id == image_set.id,
        ListingImageItem.variant_id == str(blue.id),
    ).first()
    session.delete(blue_item)
    session.flush()

    product = _first_product(session, spu)
    row = wb.build_draft(session, product, actor="batch20")
    session.flush()

    problems = _colour_problems(row)
    assert any("没有任何属于它自己的图片" in m for m in problems), (
        f"缺一个颜色的图却没有颜色维阻断项:{problems}"
    )
    assert row.status == DraftStatus.INVALID.value
    assert all(v["level"] == "error" for v in row.validation_errors
               if v["code"] == wb.COLOR_READY_VIOLATION_CODE), (
        "颜色维问题被降级成了 warning —— §6.7 说任一侧失败都不得 READY"
    )


def test_a_sku_added_after_the_draft_shows_up_as_a_mapping_gap(session):
    """AC-18 的另一侧:SKU 映射不完整 -> 颜色维阻断。

    两侧必须**同时**接上。`validate_set` 查得出"红色一张自有图都没有",
    查不出"红色有 3 个 ACTIVE SKU 而映射里只有 2 个";`map_problems` 反过来。
    这一条与上一条是同一个门禁的两半,少接任一半都只会让本文件少红一条。

    场景是真的:建档之后给红色加了一个尺码,而草稿没有重新生成。
    检查读的是**落库的那一份映射**,不是现算的 —— 现算的话它永远自洽,
    这个门禁就只是在验证自己刚算出来的东西。
    """
    spu, red, blue, _ = _ready_two_colour_spu(session)
    product = _first_product(session, spu)
    row = wb.build_draft(session, product, actor="batch20")
    session.flush()
    assert not _colour_problems(row), (
        f"基准场景就有颜色维问题:{_colour_problems(row)}"
    )

    _product(session, spu, red, "L")
    session.flush()

    # 不重新生成草稿,只重新跑一次门禁 —— 也就是运营点「导出」那一刻。
    inputs = upstream_collect.collect(
        session,
        product,
        canonical=wb._canonical_with_skus(session, product),
        image_set_row=session.query(ListingImageSet)
        .filter(ListingImageSet.spu == spu.spu_code)
        .first(),
    )
    violations = wb._color_ready_violations(row.color_sku_image_map, inputs)
    messages = [v.message for v in violations]
    assert any(f"{spu.spu_code}-RED-L" in m and "不在映射里" in m for m in messages), (
        f"新加的尺码没有被 map_problems 抓到:{messages}"
    )
    assert all(v.is_blocking for v in violations), "颜色维问题被降级成了 warning"

    # 重新生成一次就该干净 —— 提示里说的动作(「重新生成草稿」)必须真的有效。
    rebuilt = wb.build_draft(session, product, actor="batch20")
    session.flush()
    assert not _colour_problems(rebuilt), (
        f"按提示重新生成之后问题还在:{_colour_problems(rebuilt)}"
    )


def test_a_single_colour_spu_is_not_blocked_by_the_colour_axis(session):
    """单色 SPU 不因颜色轴被拦 —— 颜色轴不是"多一道普遍阻断"。

    这一条挡的是把 §6.7 接成"停产"的那种实现。它值得单独一条,因为
    `image_set_service.variant_coverage` 的文档字符串里正好记着上一次
    有人考虑硬阻断时的顾虑("那不是修复,是停产")。
    """
    spu = _spu(session)
    only = _variant(session, spu, "BLK", SellableStatus.ACTIVE.value)
    product = _product(session, spu, only, "S")
    asset = _asset(session, product, spu, only)
    _approved_image_set(session, spu, [(only, asset)])
    _approved_copy(session, spu)
    session.flush()

    row = wb.build_draft(session, product, actor="batch20")
    session.flush()
    assert not _colour_problems(row), (
        f"单色 SPU 被颜色轴拦下了:{_colour_problems(row)}"
    )
    assert set(row.color_sku_image_map["colors"]) == {str(only.id)}


def test_a_product_without_the_ownership_fk_gets_no_colour_axis(session):
    """`spu_id` 为空的存量行拿不到颜色轴,而且**不因此被拦**。

    §4.4 的归属外键是阶段 1 的交付,库里仍可能有没回填的行。
    对它们兜底(比如按 `spu` 字符串码反查 SPU)是 §3.39 明令禁掉的形状:
    那个码可能已经因改名而断开,反查出来的是另一个款的颜色集,
    表现是这份草稿按别人的颜色去铺 SKU,而没有任何地方会报错。

    正确的行为是「这个 SPU 在颜色轴上没有东西要查」——
    和 `upstream_versions IS NULL`(「没算过」)**不是**同一件事,
    所以这里同时钉:两列有值,而颜色格是空的。
    """
    spu = _spu(session)
    only = _variant(session, spu, "BLK", SellableStatus.ACTIVE.value)
    product = _product(session, spu, only, "S")
    asset = _asset(session, product, spu, only)
    _approved_image_set(session, spu, [(only, asset)])
    _approved_copy(session, spu)
    product.spu_id = None  # 未回填的存量行
    session.flush()

    row = wb.build_draft(session, product, actor="batch20")
    session.flush()
    assert row.upstream_versions is not None, (
        "颜色轴查不到东西被当成了「没算过」—— 那两者语义相反"
    )
    assert row.upstream_versions["colors"] == {}
    assert row.color_sku_image_map["colors"] == {}
    assert not _colour_problems(row), (
        f"没有归属外键的存量行被颜色轴拦下了:{_colour_problems(row)}"
    )
