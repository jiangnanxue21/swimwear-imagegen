"""A52 走查:试算在第一步真的跑得通,以及 `loc` 只有一种方言。

## 为什么这一批**必须**是行为断言

a51 那一轮给建档试算写了 32 条守卫,**全部是读源码的**。而它漏掉的两条,
恰恰是源码看着完全正确、跑起来才错的那一类:

    第一步的试算从来没通过    `ColorVariantCreate.variant_code` 带
                             `min_length=1`,而第一步的颜色表是一行空编码
                             —— pydantic 先于服务层拒,`code_taken` 一次
                             都没回来过。四类失败里唯一能被提前的那一条,
                             实际上没有被提前
    `loc` 有两种方言          `_api_loc()` 产出 `a[0].b`,而 `main.py` 的
                             `validation_handler` 产出 `body.a.0.b`。
                             前端只认一种,而**最常见**的那几类错都被 schema
                             先接走,走的正是认不出的那一种

两条都不是"某一行代码写错了",而是"两段各自正确的代码接不上"。
读源码的守卫看不见接缝。

## 为什么它们能在 tests/pure 里跑

`sku_matrix`(展开规则)与 `core/errors`(loc 归一)都零三方依赖。
`preview_spu` 那一半要 sqlalchemy,所以这里验的是它调用的两个纯函数
**在空颜色表这个输入下不抛**,加上 `_preview_variants` 的过滤口径 ——
真库那一跳(空颜色表试算真的回 `code_taken`)欠着,如实记在交付说明里。
"""
from __future__ import annotations

import ast

from app.core.errors import normalize_pydantic_loc
from app.listings import sku_matrix
from tests.pure._helpers import BACKEND_ROOT, expect_raises

SPU_SERVICE = BACKEND_ROOT / "app" / "services" / "spu_service.py"
MAIN = BACKEND_ROOT / "app" / "main.py"


def _preview_variants(raw):
    """从 `spu_service` 里把那个纯函数取出来跑。

    直接 import 会拉进 sqlalchemy(模块顶层就有)。用 AST 把函数体单独
    编译出来 —— 它自己只用标准库,所以这样跑起来和真调用逐字相同。
    """
    tree = ast.parse(SPU_SERVICE.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_preview_variants"
    )
    ns: dict = {}
    exec(compile(ast.Module([fn], []), "<preview>", "exec"), {"Any": object}, ns)  # noqa: S102
    return ns["_preview_variants"](raw)


# ------------------------------------------------- 一、第一步的试算跑得通


def test_the_blank_first_row_is_dropped_instead_of_rejected():
    """第一步那行 `emptyColour()` 表达的是"还没填",不是"填了个空的"。

    它原样发上去会被 pydantic 的 `min_length=1` 拒 —— 于是第一步的试算
    从来没跑通过,而它唯一要提前回答的 `code_taken` 一次都没回来。
    """
    blank = [{"variant_code": "", "working_name": "", "supplier_color_code": None}]
    assert _preview_variants(blank) == []
    assert _preview_variants([]) == []
    assert _preview_variants(None) == []


def test_a_row_with_a_name_but_no_code_is_kept_so_the_error_still_surfaces():
    """判据是**整行空白**,不是"编码为空"。

    只看编码的话,一行填了工作名却漏了编码的输入会被静默丢掉 ——
    而那正是运营需要系统当场指出来的那种错。
    """
    rows = [{"variant_code": "", "working_name": "藏青", "supplier_color_code": None}]
    assert _preview_variants(rows) == rows
    only_supplier = [{"variant_code": "", "working_name": "", "supplier_color_code": "C-01"}]
    assert _preview_variants(only_supplier) == only_supplier


def test_an_empty_colour_table_never_reaches_the_expander():
    """颜色表空着时不许调 `normalize_variant_codes` —— 它对空列表是要抛的。

    那句"至少要有一个颜色变体"在**提交**时是对的,在第一步的试算里不是:
    那时候还没轮到填颜色。这条钉住的是"抛"这件事本身确实存在,
    所以服务层那个 `if variants_in` 分支不是多余的。
    """
    expect_raises(sku_matrix.SkuPlanError, sku_matrix.normalize_variant_codes, [])


def test_the_spu_code_still_gets_checked_with_no_colours():
    """颜色表空着,编码本身照样要判 —— 那是第一步唯一答得出的东西。"""
    assert sku_matrix.normalize_spu_code(" sw-001 ") == "SW-001"
    expect_raises(sku_matrix.SkuPlanError, sku_matrix.normalize_spu_code, "SW 001")


# ------------------------------------------------- 二、`loc` 只有一种方言


def test_pydantic_loc_becomes_the_same_dialect_the_service_layer_uses():
    """框架层与服务层必须产出同一种形状,否则前端只认得出一半。"""
    assert (
        normalize_pydantic_loc(("body", "color_variants", 0, "variant_code"))
        == "color_variants[0].variant_code"
    )
    # 与 `_api_loc()` 的产出逐字相同 —— 那是前端正则认的那一种
    assert normalize_pydantic_loc(("body", "color_variants", 12, "variant_code")) == (
        "color_variants[12].variant_code"
    )


def test_the_request_location_prefix_is_stripped():
    """`body` / `query` / `path` 说的是"从哪儿来",不是"表单里哪一格"。"""
    for source in ("body", "query", "path", "header", "cookie"):
        assert normalize_pydantic_loc((source, "spu_code")) == "spu_code"
    # 不是前缀的同名字段不许被误剥
    assert normalize_pydantic_loc(("filters", "body")) == "filters.body"


def test_a_degenerate_loc_does_not_crash_the_error_handler():
    """空 `loc` 要回空串。错误处理器自己抛异常是最糟的一种失败。"""
    assert normalize_pydantic_loc(()) == ""
    assert normalize_pydantic_loc(("body",)) == ""


def test_the_handler_really_uses_the_normalizer():
    """接线守卫:`validation_handler` 不许再自己 `".".join(...)`。

    判定写得再对,没有人调它也没用 —— 而这一处的"没有人调"是它上一次
    出问题的方式。
    """
    src = MAIN.read_text(encoding="utf-8")
    assert "normalize_pydantic_loc(" in src, "错误处理器没有走归一化"
    assert '".".join(str(x) for x in e.get("loc"' not in src, (
        "还留着第二种方言的拼法"
    )


# ------------------------------------------------- 三、消息里不许有内部形参名


def test_no_message_repeats_its_own_locator():
    """错误正文里不许出现 `variant_codes[0]` 这种**本模块形参名**。

    位置已经由 `loc` 表达了(而且是表单认得的那一种),句首再重复一遍
    只会让运营读到一个他在界面上找不到的词。前端 `help={problem}` 与
    「第 N 行颜色:{msg}」都是原样渲染,所以那串名字是直达运营的。
    """
    cases = [
        ([""], None),
        (["BL-K"], None),
        (["A" * 40], None),
        (["X", "X"], None),
    ]
    for codes, _ in cases:
        try:
            sku_matrix.normalize_variant_codes(codes)
        except sku_matrix.SkuPlanError as exc:
            assert exc.field not in exc.message, (
                f"消息里带着内部定位符:{exc.message}"
            )
            assert "variant_codes" not in exc.message, (
                f"消息里带着形参名:{exc.message}"
            )
        else:  # pragma: no cover - 上面每一组都该抛
            raise AssertionError(f"{codes} 没有抛")


def test_the_row_number_in_the_message_counts_from_one():
    """行号从 1 数 —— 表单上那一行的序号是 1,不是 0。"""
    try:
        sku_matrix.normalize_variant_codes(["OK", ""])
    except sku_matrix.SkuPlanError as exc:
        assert exc.field == "variant_codes[1]", "定位符仍按 0 起(给机器用)"
        assert "第 2 行" in exc.message, f"消息里的行号不对:{exc.message}"
    else:  # pragma: no cover
        raise AssertionError("没有抛")


# ------------------------------------------------- 四、规则表两侧不许漏


def test_every_code_rule_reaches_the_frontend():
    """`code_rules()` 新增一条规则,`CodeRulesOut` 必须跟上。

    `model_validate` **挡不住这个方向**:多出来的键被 pydantic 静默忽略,
    而那正是 `code_rules_out()` 的文档说要避免的那件事本身。
    表现是前端少提示一句,没有任何东西报错。

    读源码而不是 import schema:`schemas/spu.py` 要 pydantic。
    """
    from app.listings.sku_matrix import code_rules

    schema = (BACKEND_ROOT / "app" / "schemas" / "spu.py").read_text(encoding="utf-8")
    tree = ast.parse(schema)
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "CodeRulesOut"
    )
    declared = {
        n.target.id
        for n in cls.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    }
    # `size_templates` 刻意不在这个出参里 —— 它走 `/spus/size-templates`
    produced = set(code_rules()) - {"size_templates"}
    assert produced == declared, (
        f"两侧对不上:后端多给 {sorted(produced - declared)}、"
        f"出参多声明 {sorted(declared - produced)}"
    )
