"""受限 source DSL(§9.3)。

**拒绝用例比通过用例重要。** spec 是外部配置文件,会被运营改、会从平台
文档复制粘贴。只要解析器接受任意路径,`product.__class__.__init__.__globals__`
就是一条从 YAML 到任意代码执行的路 —— 而这条路不会有任何测试碰到,
除非专门去写它。
"""
from __future__ import annotations

from app.channels.source_expr import (
    MISSING,
    TRANSFORMS,
    ResolveContext,
    SourceExprError,
    SourceKind,
    parse_source,
    resolve,
    validate_transform,
)
from tests.pure._helpers import expect_raises


def _ctx(**overrides) -> ResolveContext:
    base = dict(
        product={"spu": "SW-1", "sku": "SW-1-S"},
        attrs={"neckline_type": "V_NECK"},
        manual={"price": "19.90"},
        consts={"origin": "CN"},
        copies={"en": {"title": "Navy one-piece"}, "pt-BR": {"title": "Maiô azul"}},
        images={"PRODUCT_FRONT": "media:1"},
        variant=None,
    )
    base.update(overrides)
    return ResolveContext(**base)


# ---------------------------------------------------------------- 通过用例


def test_simple_prefixes_parse():
    cases = {
        "product.spu": (SourceKind.PRODUCT, "spu"),
        "attr.neckline_type": (SourceKind.ATTR, "neckline_type"),
        "variant.size": (SourceKind.VARIANT, "size"),
        "manual.price": (SourceKind.MANUAL, "price"),
        "const.origin": (SourceKind.CONST, "origin"),
    }
    for expr, (kind, key) in cases.items():
        ref = parse_source(expr)
        assert ref.kind is kind and ref.key == key
        assert str(ref) == expr


def test_copy_accepts_both_locale_shapes():
    """两字母与带地区的 locale 都要认。

    巴葡是 `pt-BR` 而不是 `pt` —— 它需要本地表达而不是翻译,
    这两者在文案生成里是两回事,locale 语法必须能表达。
    """
    ref = parse_source("copy.en.title")
    assert ref.kind is SourceKind.COPY and ref.locale == "en" and ref.key == "title"

    ref = parse_source("copy.pt-BR.title")
    assert ref.locale == "pt-BR"
    assert str(ref) == "copy.pt-BR.title"


def test_images_role_parses():
    ref = parse_source("images.PRODUCT_FRONT")
    assert ref.kind is SourceKind.IMAGES and ref.key == "PRODUCT_FRONT"


def test_resolve_reads_from_the_readonly_context():
    ctx = _ctx()
    assert resolve(parse_source("product.spu"), ctx) == "SW-1"
    assert resolve(parse_source("attr.neckline_type"), ctx) == "V_NECK"
    assert resolve(parse_source("manual.price"), ctx) == "19.90"
    assert resolve(parse_source("const.origin"), ctx) == "CN"
    assert resolve(parse_source("copy.pt-BR.title"), ctx) == "Maiô azul"
    assert resolve(parse_source("images.PRODUCT_FRONT"), ctx) == "media:1"


def test_missing_key_is_distinct_from_a_none_value():
    """取不到值返回 MISSING,不是 None。

    None 是一个**合法的字段值**(「这个字段确实没有值」),而「这个来源里
    根本没有这个键」是配置错误。混在一起时,拼错一个字段名的表现
    是安静地产出一个空单元格,要到平台驳回时才被发现。
    """
    ctx = _ctx(product={"spu": None})
    assert resolve(parse_source("product.spu"), ctx) is None
    assert resolve(parse_source("product.nope"), ctx) is MISSING


def test_variant_requires_a_row_context():
    """变体字段只能在 SKU 行上下文里用。

    在 SPU 级 header 上引用它是 spec 写错了,必须当场抛错 ——
    返回 MISSING 会让它退化成「这个字段是空的」。
    """
    ref = parse_source("variant.size")
    assert ref.is_row_only
    expect_raises(SourceExprError, resolve, ref, _ctx(variant=None))
    assert resolve(ref, _ctx(variant={"size": "M"})) == "M"


# ---------------------------------------------------------------- 拒绝用例


def test_attribute_traversal_is_rejected():
    """属性穿透是从 YAML 到任意代码执行的第一步。

    dunder 名字要单独盯着:`__class__` / `__globals__` 全都是
    「小写字母加下划线」,一个写成 `[a-z0-9_]+` 的字段名正则会原样放行它们。
    这条用例第一次跑就抓到了这个洞。
    """
    for expr in (
        "product.__class__",
        "product.__class__.__init__",
        "attr.a.b",
        "product.spu.upper",
        "const.__globals__",
        "manual._secret",
        "copy.en.__dict__",
    ):
        expect_raises(SourceExprError, parse_source, expr)


def test_illegal_syntax_is_rejected():
    """表达式只能是「前缀 + 点号字段」,任何计算都必须走 transform 白名单。

    分组保留在数据里而不是拆成多个函数 —— 失败信息里带着分组名,
    定位并不比一个函数一种语法差。
    """
    cases = {
        "函数调用": ("copy.en.title.upper()", "product.spu()", "manual.price.strip()"),
        "下标": ("images[0]", "product.spu[0]", "copy.en.bullet_points[1]"),
        "运算": ("const.x + 1", "manual.price * 2", "product.spu + '-S'"),
        # 前后空白不静默接受:静默 strip 意味着 " attr.x" 和 "attr.x" 是两个
        # 字符串却是同一个引用,而 spec 的去重与覆盖检查是按字符串做的
        "前后空白": (" product.spu", "product.spu ", "\tattr.x"),
    }
    for kind, exprs in cases.items():
        for expr in exprs:
            err = expect_raises(SourceExprError, parse_source, expr)
            assert err is not None, f"{kind}未被拒绝: {expr!r}"


def test_unknown_prefixes_are_rejected():
    for expr in ("session.query", "os.environ", "settings.ADMIN_TOKEN", "spu"):
        expect_raises(SourceExprError, parse_source, expr)


def test_malformed_input_is_rejected():
    for expr in ("", "   ", "product.", ".spu", "product..spu", "copy.en", "images."):
        expect_raises(SourceExprError, parse_source, expr)


def test_non_string_input_is_rejected():
    for expr in (None, 42, ["product.spu"], {"source": "product.spu"}):
        expect_raises(SourceExprError, parse_source, expr)


def test_case_rules_are_enforced():
    """字段名小写、图片角色大写。

    不是风格洁癖:`images.<ROLE>` 直接对应 `MediaRole` 枚举值,
    大小写不一致会让白名单校验在启动期漏掉一个不存在的角色。
    """
    expect_raises(SourceExprError, parse_source, "product.SPU")
    expect_raises(SourceExprError, parse_source, "images.product_front")
    expect_raises(SourceExprError, parse_source, "copy.EN.title")


# ---------------------------------------------------------------- 变换白名单


def test_transform_whitelist_rejects_unknown_names():
    expect_raises(SourceExprError, validate_transform, {"name": "eval"})
    expect_raises(SourceExprError, validate_transform, {"name": "format_string"})
    expect_raises(SourceExprError, validate_transform, {})


def test_transform_requires_its_parameters():
    expect_raises(SourceExprError, validate_transform, {"name": "truncate"})
    validate_transform({"name": "truncate", "max_length": 120})
    validate_transform({"name": "upper"})


def test_map_transform_must_reference_a_dict_file():
    """`map` 只能引用 dict/<name>.yaml,不允许内联字典。

    内联会让「巴西站的颜色词表」散落在十几个字段声明里,改一次要找十几处。
    """
    assert TRANSFORMS["map"] == ("dict",)
    expect_raises(SourceExprError, validate_transform, {"name": "map"})
    validate_transform({"name": "map", "dict": "color_map"})
