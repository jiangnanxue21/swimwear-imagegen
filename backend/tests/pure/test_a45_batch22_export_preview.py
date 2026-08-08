"""A45-batch22(阶段 5 批次 5-5):导出预览的颜色→SKU→图片那一段。

## 这一批补的是 AC-19 的后半句

AC-19:「导出预览与最终导出读取**同一份已存映射**,不在预览时重新推断。」

前半句(字段)早就成立:`draft_preview` 读的是落库的 `mapped_payload`,
与导出同源,§5.3 的「导出一致率 100%」就是它。
**后半句(图片)此前一格都没有** —— 预览里回答不了"红色的 M 码用的是哪张
主图"。5-2 把答案写进了 `listing_drafts.color_sku_image_map`,本批把它读出来。

## 守卫分三段,而**第二段是本文件的重点**

    形状   三档状态各自可区分;缺主图的行留在表里而不是消失
    只读   预览**拿不到**重新推断所需的东西 —— 这是反向断言,最容易静默失效
    接线   `draft_preview` 真的把它挂出去了

第二段值得单独说:重新推断的预览**永远是自洽的**。它显示"当前该用哪些图",
而导出用的是草稿生成那一刻存下来的那些。上游变过之后两者不是一回事,
于是运营在预览里看到红色有主图、导出出来那一行是空的,**而两边都不报错**。
"""
from __future__ import annotations

import ast

from app.listings import export_preview as ep
from tests.pure._helpers import BACKEND_ROOT

LAYER = BACKEND_ROOT / "app" / "listings" / "export_preview.py"
SERVICE = BACKEND_ROOT / "app" / "workbench" / "service.py"


def _mapping(**colors):
    return {
        "schema": "1",
        "colors": {
            variant: {"variant_code": variant.upper(), "skus": skus}
            for variant, skus in colors.items()
        },
    }


def _cell(primary="a-1", extras=("a-2",)):
    return {"primary": primary, "extras": list(extras)}


# ---------------------------------------------------------------- 一、形状


def test_the_three_map_states_are_distinguishable():
    """三档状态各自可区分。合并任意两档都会让运营做错动作。

        UNPROVEN      重新生成一次草稿就好
        INCOMPATIBLE  有人换过快照形状 —— 这是工程侧的事,不是运营能修的
        READY         逐行看

    合并前两档的话,两种情况都显示"没有图片信息",而第二种重新生成
    一百次也不会变好。
    """
    assert ep.image_preview(None)["status"] == ep.MAP_UNPROVEN
    assert ep.image_preview({"schema": "99"})["status"] == ep.MAP_INCOMPATIBLE
    assert ep.image_preview(_mapping())["status"] == ep.MAP_READY
    assert len({ep.MAP_UNPROVEN, ep.MAP_INCOMPATIBLE, ep.MAP_READY}) == 3


def test_each_non_ready_state_says_what_to_do():
    """两档非 READY 都要带一句可执行的话(§4.5.1 的同一条口径)。"""
    for mapping in (None, {"schema": "99"}):
        note = ep.image_preview(mapping)["note"]
        assert note and "草稿" in note, f"{mapping} 那一档没说该做什么"
    assert ep.image_preview(_mapping())["note"] == ""


def test_a_sku_without_a_primary_stays_in_the_table():
    """缺主图的行**留在表里**,状态是 MISSING,不是被跳过。

    跳过的话预览里那一行消失,而运营会读成"这个尺码不在这次导出里"——
    它在,只是没有图。READY 门禁会拦下这份草稿,但预览要在被拦之前
    就让人看见是**哪一行**缺。
    """
    preview = ep.image_preview(
        _mapping(red={"S": _cell(), "M": _cell(primary=None, extras=())})
    )
    rows = preview["colors"][0]["rows"]
    assert [r["sku"] for r in rows] == ["M", "S"], "缺图的那一行被跳过了"
    missing = next(r for r in rows if r["sku"] == "M")
    assert missing["status"] == ep.ROW_MISSING
    assert missing["primary"] is None
    assert preview["colors"][0]["missing_count"] == 1
    assert ep.missing_rows(preview) == ["M"]


def test_rows_and_colors_come_out_in_a_stable_order():
    """顺序稳定。JSONB 不保证键序,而预览是给人逐行核对的。

    不排序的话,同一份草稿刷新两次看到的行序不同 —— 运营会以为数据变了。
    """
    first = ep.image_preview(
        _mapping(blue={"M": _cell(), "S": _cell()}, aqua={"L": _cell()})
    )
    second = ep.image_preview(
        _mapping(aqua={"L": _cell()}, blue={"S": _cell(), "M": _cell()})
    )
    assert first == second


def test_the_extras_survive_and_the_primary_is_not_among_them():
    """附图原样带出。**主图不混进附图位**(§6.5 的形状在这一层的体现)。"""
    preview = ep.image_preview(_mapping(red={"S": _cell("p-1", ("e-1", "e-2"))}))
    row = preview["colors"][0]["rows"][0]
    assert row["primary"] == "p-1"
    assert row["extras"] == ["e-1", "e-2"]
    assert row["primary"] not in row["extras"]


def test_a_blank_extra_is_dropped_not_rendered_as_empty():
    """空的附图项丢掉,不铺成一个空格子 —— 那会显示成一张不存在的图。"""
    preview = ep.image_preview(_mapping(red={"S": {"primary": "p", "extras": ["", None, "e"]}}))
    assert preview["colors"][0]["rows"][0]["extras"] == ["e"]


# ---------------------------------------------------------------- 二、只读


def test_the_preview_cannot_reach_anything_it_could_re_infer_from():
    """**本文件的重点。** 预览拿不到重新推断所需的东西。

    这条是反向断言:它钉的不是"预览算对了",是"预览**算不了**"。
    `image_preview()` 的签名里只有落库的那份映射 —— 想重新推断也无从下手。

    退化的样子很具体:有人为了"让预览显示当前最新的图"给它多传一个
    `image_set` 或 `colors`。加上那一刻 AC-19 的后半句就不成立了,
    而**所有正向断言仍然全绿** —— 预览会更"准",只是和导出不再是一回事。
    """
    import inspect

    params = list(inspect.signature(ep.image_preview).parameters)
    assert params == ["mapping"], (
        f"`image_preview()` 的入参变成了 {params} —— "
        "多一个能重新推断的入参,预览就不再和导出读同一份数据"
    )

    tree = ast.parse(LAYER.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    for banned in (
        "app.listings.image_set_rules",
        "app.workbench.upstream_collect",
        "app.listings.image_set_service",
    ):
        assert banned not in imported, (
            f"预览层 import 了 `{banned}` —— 那是重新推断的入口"
        )


def test_the_preview_never_calls_the_builders():
    """源码里不出现构建器与覆盖计算。

    判据是 **AST 的调用名**,不是文本 —— 本模块的文档字符串里写着
    「不许出现 `coverage` / `build_color_sku_image_map`」,按文本扫会被
    那句话本身喂真(同型第八次:§3.26 / 14-13 / 14-22 / batch18 C2 /
    batch19 caller / batch20 B2 / batch21 的 docstring)。
    """
    tree = ast.parse(LAYER.read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for banned in ("coverage", "build_color_sku_image_map", "validate_set", "collect"):
        assert banned not in called, (
            f"预览层调用了 `{banned}` —— 它在重新推断,而导出读的是落库那一份"
        )


def test_the_row_statuses_are_a_closed_set():
    """行状态是封闭取值集。散着写的话改一个拼写不会有任何东西红。"""
    assert set(ep.ROW_STATUSES) == {ep.ROW_OK, ep.ROW_MISSING}
    preview = ep.image_preview(_mapping(red={"S": _cell(), "M": _cell(primary=None)}))
    for row in preview["colors"][0]["rows"]:
        assert row["status"] in ep.ROW_STATUSES


# ---------------------------------------------------------------- 三、接线


def test_draft_preview_actually_exposes_it():
    """`draft_preview` 真的把它挂出去了,而且读的是**落库的那一列**。

    少这一跳的话本模块就是"算好了没人看"——§3.43 点名过的那一类,
    纯层全绿、门禁全绿,而界面上一个字都没多。
    """
    fn = next(
        n
        for n in ast.walk(ast.parse(SERVICE.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "draft_preview"
    )
    body = ast.unparse(fn)
    assert "export_preview.image_preview(row.color_sku_image_map)" in body, (
        "`draft_preview` 没有挂上图片预览,或者读的不是落库的那一列"
    )
    assert "'images'" in body or '"images"' in body


def test_the_colour_label_does_not_use_display_name():
    """颜色标识用 `variant_code`,不用 `display_name`。

    那一列的写入点要等 5-4(`audit_column_writers.LEDGER` 上记着),
    今天恒为默认值 —— 拿它当标识会让**每一个颜色都显示成空**。
    与 `upstream_snapshot._color_label` 同口径。
    """
    src = LAYER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    attrs = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "display_name" not in attrs, "预览读了 `display_name`"
    assert "display_name" not in {
        s for s in literals if s == "display_name"
    }, "预览按字符串键读了 `display_name`"

    preview = ep.image_preview(_mapping(red={"S": _cell()}))
    assert preview["colors"][0]["variant_code"] == "RED"


def test_a_colour_without_a_code_falls_back_to_a_short_id():
    """没有颜色码时退回短 id,不显示成空。"""
    mapping = {
        "schema": "1",
        "colors": {"3f2a1b9c-dead-beef-0000-111122223333": {"skus": {"S": _cell()}}},
    }
    label = ep.image_preview(mapping)["colors"][0]["variant_code"]
    assert label and label != "", "颜色显示成空了"
    assert label.startswith("3f2a1b9c")
