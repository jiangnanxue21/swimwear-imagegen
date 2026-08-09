"""变体层映射与字段类型校验(评审第 8 / 9 / 10 条)。

这三条都落在导出链路上,共同点是**故障是安静的**:导出的 Excel 每一格
都有值、格式也对,错误要等平台驳回或消费者收到货才会浮现。所以这里的
断言大多是「这一格不该等于那一格」,而不是「这一格不为空」。

分三节:

    8   属性分层     未确认的属性不得流出,也不得覆盖已确认的值
    9   变体图       每个 SKU 取自己颜色的图,不共用一份 SPU 级的桶
    10  字段类型     price / stock 的类型、区间、小数位在导出前就要拦住
"""
from __future__ import annotations

from decimal import Decimal

from app.attributes.contracts import (
    AttrValue,
    CanonicalProduct,
    CanonicalSku,
    CanonicalVariant,
)
from app.channels import generic
from app.channels.spec_loader import ChannelSpecError, load_spec_dict
from app.core.enums import (
    AttributeSource,
    AttributeStatus,
    ImageSetStatus,
    MediaRole,
)
from app.listings.contracts import (
    CopySnapshot,
    ImageItemSnapshot,
    ImageSetSnapshot,
    ListingDraftData,
)
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401  (装 sys.path)

# ---------------------------------------------------------------- 夹具


def _attr(name: str, value, *, status=AttributeStatus.CONFIRMED) -> AttrValue:
    return AttrValue(
        field_name=name,
        value=value,
        source=AttributeSource.MANUAL,
        status=status,
        version_id=f"v-{name}",
    )


def _image(
    asset: str,
    role: MediaRole,
    order: int,
    path: str,
    *,
    variant_id: str | None = None,
    is_primary: bool = False,
    enabled: bool = True,
) -> ImageItemSnapshot:
    return ImageItemSnapshot(
        media_asset_id=asset,
        role=role,
        sort_order=order,
        storage_path=path,
        width=1200,
        height=1600,
        variant_id=variant_id,
        is_primary=is_primary,
        enabled=enabled,
    )


def _image_set(*items: ImageItemSnapshot) -> ImageSetSnapshot:
    return ImageSetSnapshot(
        image_set_id="is-1",
        spu="SW-001",
        version=1,
        status=ImageSetStatus.APPROVED,
        items=items,
    )


#: 两色两码。评审第 9 条的最小复现需要至少两个变体 —— 单变体的 SPU
#: 无论怎么共用图片桶都看不出问题。
def _two_colour_product(
    *,
    spu_attrs=None,
    variants=(),
    sku_attrs=None,
) -> CanonicalProduct:
    sku_attrs = sku_attrs or {}
    return CanonicalProduct(
        spu="SW-001",
        category_path=("Swimwear",),
        spu_attrs=spu_attrs
        if spu_attrs is not None
        else {
            "primary_color": _attr("primary_color", "NAVY_BLUE"),
            "garment_type": _attr("garment_type", "ONE_PIECE"),
            "material": _attr("material", "82% Nylon 18% Spandex"),
        },
        variants=variants,
        skus=(
            CanonicalSku(
                sku="SW-001-NVY-S",
                variant_id="NVY",
                size="S",
                attrs=sku_attrs.get("SW-001-NVY-S", {}),
            ),
            CanonicalSku(
                sku="SW-001-CRL-S",
                variant_id="CRL",
                size="S",
                attrs=sku_attrs.get("SW-001-CRL-S", {}),
            ),
        ),
    )


_DEFAULT_MANUAL = {
    "brand": "Marine",
    "price": "39.90",
    "currency": "USD",
    "stock": "120",
    "lead_time_days": "5",
}


def _draft(**overrides) -> ListingDraftData:
    data = dict(
        spu="SW-001",
        channel=generic.CHANNEL,
        site=generic.SITE,
        category_id=generic.CATEGORY_ID,
        product=_two_colour_product(),
        image_set=_image_set(
            _image("m1", MediaRole.PRODUCT_FRONT, 0, "media/front.jpg", is_primary=True)
        ),
        copies={
            "en": CopySnapshot(
                copy_id="c1",
                locale="en",
                version=1,
                title="Navy Blue One Piece Swimsuit",
                description="A navy blue one piece swimsuit.",
            )
        },
        manual=dict(_DEFAULT_MANUAL),
        spec_version="1.1.0",
        mapping_version="1",
    )
    data.update(overrides)
    return ListingDraftData(**data)


def _spec():
    return generic.field_spec()


def _map(**by_variant) -> dict:
    """构造一份 `color_sku_image_map`(A45-batch24 起行级图片只从它翻译)。

    `_map(NVY=("m1", ["m2"]))` → 颜色 NVY 下的那一个 SKU 主图 m1、附图 m2。

    这份映射在生产里由 `upstream_snapshot.build_color_sku_image_map` 按
    §6.5 的规则算出来(主图取显式 `is_primary`、通用图只有勾了「可混入」
    才进附图位、缺图就是缺图)。测试里显式写出来而不是重算一遍:
    这个文件验的是**映射之后那一跳**(id 怎么翻译成字段),
    挑图规则本身由 `image_set_rules` 那边的用例与 batch24 的接缝守卫管。
    """
    skus = {"NVY": "SW-001-NVY-S", "CRL": "SW-001-CRL-S"}
    colors = {}
    for variant_id, cell in by_variant.items():
        primary, extras = cell if isinstance(cell, tuple) else (cell, [])
        colors[variant_id] = {
            "variant_code": variant_id,
            "skus": {
                skus[variant_id]: {"primary": primary, "extras": list(extras)}
            },
        }
    return {"schema": "1", "colors": colors}


#: 默认映射:两个颜色各拿默认图片集里那一张主图。
#:
#: 大多数用例验的是属性分层与字段类型,图片只要"有一份合理的映射"即可。
_DEFAULT_MAP = _map(NVY=("m1", []), CRL=("m1", []))


def _rows(draft: ListingDraftData, image_map: dict | None = None):
    return generic.map_fields(
        draft, _spec(), image_map=_DEFAULT_MAP if image_map is None else image_map
    ).rows


def _codes(draft: ListingDraftData, field_key: str) -> set[str]:
    spec = _spec()
    mapped = generic.map_fields(draft, spec, image_map=_DEFAULT_MAP)
    return {
        p.code for p in generic.validate(mapped, spec) if p.field_key == field_key
    }



# ---------------------------------------------------------------- 8 属性分层


def test_an_unconfirmed_sku_attribute_never_reaches_the_row():
    """SKU 层也要过 CONFIRMED 这道滤网。

    表头那一层一直是过的(`confirmed_attrs()`),SKU 层原来是一句
    直接遍历的推导式 —— 于是同一条不变量在同一份文件里一半成立。

    这里把 SPU 层的 `primary_color` 拿掉,让 SKU 层成为唯一的来源:
    有 SPU 值兜着的话,「漏出去」和「被覆盖」两种故障看起来一样,
    分不清是滤网生效了还是只是恰好被盖住了。
    """
    product = _two_colour_product(
        spu_attrs={"garment_type": _attr("garment_type", "ONE_PIECE")},
        sku_attrs={
            "SW-001-NVY-S": {
                "primary_color": _attr(
                    "primary_color", "GUESSED_TEAL", status=AttributeStatus.SUGGESTED
                )
            }
        },
    )
    draft = _draft(product=product)
    assert _rows(draft)[0]["color"] is None
    # 空出来的必填格必须被拦住,而不是安静地留白
    assert "CHANNEL_FIELD_MISSING" in _codes(draft, "color")


def test_an_unconfirmed_sku_attribute_does_not_overwrite_the_confirmed_spu_value():
    """评审第 8 条的正题。

    未过滤的 SKU 属性是**最后**展开的一层,所以它不只是自己漏出去,
    还会盖掉同名的已确认 SPU 值 —— 表头显示运营确认过的颜色,
    同一份文件的 SKU 行显示模型的猜测,而两边都没有任何标记。
    """
    product = _two_colour_product(
        sku_attrs={
            "SW-001-NVY-S": {
                "primary_color": _attr(
                    "primary_color", "GUESSED_TEAL", status=AttributeStatus.SUGGESTED
                )
            }
        }
    )
    rows = _rows(_draft(product=product))
    assert rows[0]["color"] == "NAVY_BLUE"


def test_a_confirmed_variant_attribute_reaches_the_row():
    """`CanonicalVariant.attrs` 在改动前全程没有读取方。

    契约建了颜色这一层,映射层却跳过它 —— 一个已确认的颜色级事实
    无论怎么确认都进不了导出文件。
    """
    product = _two_colour_product(
        variants=(
            CanonicalVariant(
                variant_id="CRL",
                attrs={"primary_color": _attr("primary_color", "CORAL_RED")},
            ),
        )
    )
    rows = _rows(_draft(product=product))
    assert rows[0]["color"] == "NAVY_BLUE"     # 没有变体覆盖,回落到 SPU
    assert rows[1]["color"] == "CORAL_RED"     # 颜色层覆盖了 SPU 层


def test_a_confirmed_sku_attribute_wins_over_the_variant_layer():
    """三层从粗到细:SPU -> 颜色 -> 颜色×尺码,细的覆盖粗的。"""
    product = _two_colour_product(
        variants=(
            CanonicalVariant(
                variant_id="NVY",
                attrs={"primary_color": _attr("primary_color", "VARIANT_LEVEL")},
            ),
        ),
        sku_attrs={
            "SW-001-NVY-S": {"primary_color": _attr("primary_color", "SKU_LEVEL")}
        },
    )
    assert _rows(_draft(product=product))[0]["color"] == "SKU_LEVEL"


def test_an_attribute_cannot_overwrite_the_skus_own_identity_fields():
    """身份字段来自商品行本身,是这一行的定义。

    同名属性覆盖掉真实尺码的话,导出的**每一行**都是错的,
    而每一行看起来都很正常。
    """
    product = _two_colour_product(
        sku_attrs={"SW-001-NVY-S": {"size": _attr("size", "XXL")}}
    )
    rows = _rows(_draft(product=product))
    assert rows[0]["size"] == "S"
    assert rows[0]["sku_code"] == "SW-001-NVY-S"


def test_multi_valued_attributes_are_flattened_the_same_way_at_every_layer():
    """多值属性在 SPU 层折成逗号分隔,SKU 层原来不折。

    同一个属性挂在哪一层,决定了它在 Excel 里是 `"A, B"` 还是
    `"['A', 'B']"` —— 后者会原样进平台。
    """
    product = _two_colour_product(
        sku_attrs={
            "SW-001-NVY-S": {
                "primary_color": _attr("primary_color", ["CORAL", "PEACH"])
            }
        }
    )
    assert _rows(_draft(product=product))[0]["color"] == "CORAL, PEACH"


# ---------------------------------------------------------------- 9 变体图


def test_each_sku_gets_the_image_bound_to_its_own_colour():
    """评审第 9 条的正题:五色 SPU 导出去,五种颜色的变体图不能是同一张。"""
    draft = _draft(
        image_set=_image_set(
            _image("m1", MediaRole.PRODUCT_FRONT, 0, "media/navy.jpg", variant_id="NVY"),
            _image("m2", MediaRole.PRODUCT_FRONT, 1, "media/coral.jpg", variant_id="CRL"),
        )
    )
    rows = _rows(draft, _map(NVY=("m1", []), CRL=("m2", [])))
    assert rows[0]["variant_image"] == "media/navy.jpg"
    assert rows[1]["variant_image"] == "media/coral.jpg"


def test_a_variant_absent_from_the_map_gets_no_image_at_all():
    """**这条断言换过方向(A45-batch24)。**

    原来它叫「没有专属图的变体回落到 SPU 通用图」,断言 NVY 那一行拿到
    `media/generic.jpg`。那个回落是**导出这一侧自己长出来的**:
    §6.5 / BLOCK-02 明写「不得回退使用其他颜色的图片,缺图就是缺图」,
    而 `image_set_rules.coverage()` 与 `color_sku_image_map` 从来就没有
    把通用图算进主图位 —— 通用图只有勾了「可混入」才进附图位。

    于是同一份草稿有两个答案:预览(读映射)说 NVY 缺主图、READY 门禁
    据此拦下这份草稿,而 Excel 里 NVY 那一行填着通用图。两边都不报错。
    行级图片改为只从映射翻译之后,这个分叉在结构上没有了 ——
    代价是这条用例的期望值要跟着换,而换的方向是**规则原本就写着的**那个。
    """
    draft = _draft(
        image_set=_image_set(
            _image("m0", MediaRole.PRODUCT_FRONT, 0, "media/generic.jpg"),
            _image("m2", MediaRole.PRODUCT_FRONT, 1, "media/coral.jpg", variant_id="CRL"),
        )
    )
    rows = _rows(draft, _map(CRL=("m2", [])))
    assert rows[0]["variant_image"] is None, (
        "NVY 在映射里没有主图,而导出仍然给它填了一张 —— 通用图回落又回来了"
    )
    assert rows[1]["variant_image"] == "media/coral.jpg"


def test_an_extra_never_takes_the_slot_the_primary_already_holds():
    """附图不顶替主图,哪怕它的编辑序更靠前。

    `extras` 里排第一的恰恰常常是"排序最靠前的那张自有图"(见
    `upstream_snapshot._extra_ids`),也就是修复前会被导出选中的那一张。
    主图先填自己的角色位,所以顺序这件事在这里是**承重的**,不是美观。
    """
    draft = _draft(
        image_set=_image_set(
            _image(
                "m-first", MediaRole.PRODUCT_FRONT, 0, "media/other-front.jpg",
                variant_id="CRL",
            ),
            _image(
                "m-primary", MediaRole.PRODUCT_FRONT, 5, "media/coral.jpg",
                variant_id="CRL", is_primary=True,
            ),
        )
    )
    rows = _rows(draft, _map(CRL=("m-primary", ["m-first"])))
    assert rows[1]["variant_image"] == "media/coral.jpg"



def test_a_row_never_borrows_another_variants_image():
    """宁可空着,也不能填错颜色。

    空格会被必填校验拦下来并显示在页面上;填错的图不会 ——
    它要等消费者收到货才会被发现。
    """
    draft = _draft(
        image_set=_image_set(
            _image("m2", MediaRole.PRODUCT_FRONT, 0, "media/coral.jpg", variant_id="CRL"),
        )
    )
    rows = _rows(draft, _map(CRL=("m2", [])))
    assert rows[0]["variant_image"] is None
    assert rows[1]["variant_image"] == "media/coral.jpg"


def test_the_explicit_primary_wins_over_the_editorial_first():

    """**AC-19 的核心场景**:两张合法正面图,显式主图不是排序第一张。

    这是老师们在真库上复现的那一条:预览显示 `is_primary` 的那张,
    而导出按 `sort_order` 取第一张 —— 一份合法且 READY 的图片集,
    两边给出不同的图,都不报错。行级从映射翻译之后,答案只有一个。
    """
    draft = _draft(
        image_set=_image_set(
            _image(
                "m-first", MediaRole.PRODUCT_FRONT, 0, "media/secondary-front.jpg",
                variant_id="CRL",
            ),
            _image(
                "m-primary", MediaRole.PRODUCT_FRONT, 5, "media/marked-primary.jpg",
                variant_id="CRL", is_primary=True,
            ),
        )
    )
    # 映射里的主图是显式标记的那一张(`coverage().primary` 的口径)
    rows = _rows(draft, _map(CRL=("m-primary", ["m-first"])))
    assert rows[1]["variant_image"] == "media/marked-primary.jpg", (
        "导出没有用映射里那张主图 —— 预览与导出又分叉了(AC-19)"
    )



def test_the_spu_header_prefers_the_generic_image_over_a_variant_bound_one():
    """表头是 SPU 级的,不该被排序最靠前的那个颜色抢走。"""
    draft = _draft(
        image_set=_image_set(
            _image("m2", MediaRole.PRODUCT_FRONT, 0, "media/coral.jpg", variant_id="CRL"),
            _image("m0", MediaRole.PRODUCT_FRONT, 1, "media/generic.jpg"),
        )
    )
    header = generic.map_fields(draft, _spec(), image_map=_DEFAULT_MAP).header
    assert header["main_image"] == "media/generic.jpg"


def test_the_header_still_falls_back_when_every_image_is_variant_bound():
    """全部图都绑了颜色不是配置错误,主图仍要有值。

    这一条是防回归的:表头如果只认 SPU 通用图,这类图片集会突然
    产出一个空主图,把一份本来能导出的草稿变成不能导出。
    """
    draft = _draft(
        image_set=_image_set(
            _image("m2", MediaRole.PRODUCT_FRONT, 0, "media/coral.jpg", variant_id="CRL"),
            _image("m1", MediaRole.PRODUCT_FRONT, 1, "media/navy.jpg", variant_id="NVY"),
        )
    )
    header = generic.map_fields(draft, _spec(), image_map=_DEFAULT_MAP).header
    assert header["main_image"] == "media/coral.jpg"


def test_disabled_variant_images_are_still_excluded():
    draft = _draft(
        image_set=_image_set(
            _image(
                "m1", MediaRole.PRODUCT_FRONT, 0, "media/navy.jpg",
                variant_id="NVY", enabled=False,
            ),
        )
    )
    assert _rows(draft)[0]["variant_image"] is None


# ---------------------------------------------------------------- 10 字段类型


def test_the_shipped_spec_declares_the_numeric_rules():
    spec = _spec()
    by_key = {f.key: f for f in spec.row_fields}
    assert by_key["price"].value_type == "number"
    assert by_key["price"].minimum == Decimal("0")
    assert by_key["price"].decimal_places == 2
    assert by_key["stock"].value_type == "integer"
    assert by_key["stock"].is_numeric


def test_operator_typed_numeric_strings_are_accepted():
    """**最重要的一条防误伤。**

    价格、库存、备货时效一律走 `manual`,而 `manual` 里装的是运营在
    页面上手填的东西 —— 到校验器手上就是字符串 `"39.90"`。校验器只认
    `int`/`float` 的话,真实数据会被整批判成「不是数字」。
    """
    spec = _spec()
    mapped = generic.map_fields(_draft(), spec, image_map=_DEFAULT_MAP)
    blocking = [p for p in generic.validate(mapped, spec) if p.is_blocking]
    assert blocking == [], [p.message for p in blocking]


def test_a_price_with_too_many_decimal_places_is_rejected():
    manual = {**_DEFAULT_MANUAL, "price": "39.905"}
    assert "CHANNEL_FIELD_TOO_PRECISE" in _codes(_draft(manual=manual), "price")


def test_a_negative_price_is_rejected():
    manual = {**_DEFAULT_MANUAL, "price": "-39.90"}
    assert "CHANNEL_FIELD_OUT_OF_RANGE" in _codes(_draft(manual=manual), "price")


def test_a_non_numeric_price_is_rejected():
    """required 只回答「填了没有」。`"面议"` 是填了的。"""
    manual = {**_DEFAULT_MANUAL, "price": "面议"}
    assert "CHANNEL_FIELD_TYPE_INVALID" in _codes(_draft(manual=manual), "price")


def test_a_fractional_stock_is_rejected():
    manual = {**_DEFAULT_MANUAL, "stock": "12.5"}
    assert "CHANNEL_FIELD_TYPE_INVALID" in _codes(_draft(manual=manual), "stock")


def test_an_integer_valued_stock_written_with_a_decimal_point_is_accepted():
    """`"12.0"` 的**值**是整数,只是写法带了小数点。

    这里刻意放行:校验的是值不是排版,而把一个正确的数字判成非法
    会让运营去改一个本来没错的格子。
    """
    manual = {**_DEFAULT_MANUAL, "stock": "12.0"}
    assert _codes(_draft(manual=manual), "stock") == set()


def test_a_boolean_is_not_a_number():
    """`bool` 是 `int` 的子类。一个「是/否」被当成数量 1 通过校验,
    比直接报错难查得多。"""
    manual = {**_DEFAULT_MANUAL, "stock": True}
    assert "CHANNEL_FIELD_TYPE_INVALID" in _codes(_draft(manual=manual), "stock")


def test_infinity_and_nan_do_not_slip_past_the_range_check():
    """`Decimal` 认这两个字面量,而它们和任何边界比较都返回 False ——
    一个 `Infinity` 库存能干净地通过 `minimum`。"""
    for text in ("Infinity", "-Infinity", "NaN"):
        manual = {**_DEFAULT_MANUAL, "stock": text}
        assert "CHANNEL_FIELD_TYPE_INVALID" in _codes(_draft(manual=manual), "stock"), text


def test_every_sku_row_reports_its_own_numeric_problem():
    """两行 SKU 都要各报一次,否则「哪一行错」无法回答。"""
    spec = _spec()
    manual = {**_DEFAULT_MANUAL, "price": "39.905"}
    mapped = generic.map_fields(_draft(manual=manual), spec, image_map=_DEFAULT_MAP)
    rows = [
        p.row_index
        for p in generic.validate(mapped, spec)
        if p.code == "CHANNEL_FIELD_TOO_PRECISE"
    ]
    assert sorted(rows) == [0, 1]


def test_an_empty_optional_numeric_field_is_not_a_type_error():
    """选填且没填 = 没问题。类型校验不该把「空」变成「非法」。"""
    manual = {k: v for k, v in _DEFAULT_MANUAL.items() if k != "lead_time_days"}
    assert _codes(_draft(manual=manual), "lead_time_days") == set()


# ---------------------------------------------------------------- 10 加载期


def _load(field: dict):
    return load_spec_dict(
        {"channel": "X", "sites": ["MAIN"], "row_fields": [dict(field)]},
        site="MAIN",
        category_id="swimwear",
    )


def _rejects(field: dict, fragment: str) -> None:
    try:
        _load(field)
    except ChannelSpecError as exc:
        assert fragment in str(exc), f"报错信息里没有 {fragment!r}:{exc}"
    else:
        raise AssertionError(f"这份字段声明应当在加载期被拒绝:{field}")


def test_loader_rejects_an_unknown_type():
    _rejects({"key": "a", "source": "manual.a", "type": "float"}, "未知的 type")


def test_loader_rejects_numeric_rules_on_a_non_numeric_field():
    """约束写了却不生效,和没写一样危险 —— 而且更容易让人以为已经拦过了。"""
    _rejects(
        {"key": "a", "source": "manual.a", "type": "string", "minimum": 0},
        "只能配在",
    )
    _rejects({"key": "a", "source": "manual.a", "maximum": 9}, "只能配在")


def test_loader_rejects_decimal_places_on_an_integer_field():
    _rejects(
        {"key": "a", "source": "manual.a", "type": "integer", "decimal_places": 2},
        "decimal_places",
    )


def test_loader_rejects_a_negative_decimal_places():
    _rejects(
        {"key": "a", "source": "manual.a", "type": "number", "decimal_places": -1},
        "不能为负数",
    )


def test_loader_rejects_an_impossible_range():
    _rejects(
        {"key": "a", "source": "manual.a", "type": "number",
         "minimum": 10, "maximum": 1},
        "不可能被满足",
    )


def test_loader_rejects_a_non_numeric_bound():
    _rejects(
        {"key": "a", "source": "manual.a", "type": "number", "minimum": "便宜"},
        "不是合法的数字",
    )
    _rejects(
        {"key": "a", "source": "manual.a", "type": "number", "minimum": "Infinity"},
        "有限数",
    )


def test_a_yaml_float_bound_does_not_pick_up_binary_float_drift():
    """`Decimal(0.1)` 是 `0.1000000000000000055…`。

    走 float 的话,一个**恰好等于下界**的价格会被判成越界,而报错信息里
    看到的仍然是 `0.1` —— 没有人能从那条信息里看出问题在哪。
    """
    spec = _load(
        {"key": "a", "source": "manual.a", "type": "number", "minimum": 0.1}
    )
    assert spec.row_fields[0].minimum == Decimal("0.1")
