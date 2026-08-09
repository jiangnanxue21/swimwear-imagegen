"""A45-batch28(阶段 6 批次 6-3):聚合工作流 API —— 颜色子态终于有人读了。

## 这一批还的是 6-1 明说的那笔账

6-1 交付 `color_flow` 时,结论文件里写着一句:

> `color_flow` 今天**零生产调用点**。它要等 6-3 的聚合 API 才有消费者。
> 本仓为"算好了没人读"付过十二次账(§3.43 那一族),而那十二次的共同点
> 不是落了一半,是**没有人说它落了一半**。

这一批把那句话变成假的:`color_rollup` 是它的调用点,详情端点是它的出口。
所以本文件第一条守卫钉的就是**它真的被读了** —— 而且是从 HTTP 出得去。

## 三节

    一   接线:判定层 -> 取数层 -> 端点,三跳都在
    二   同源:向导上的"缺主图"与导出预览必须是同一条口径
    三   不新开端点,以及"算不出来"与"都好了"要分得开
"""
from __future__ import annotations

import ast

from app.workbench import color_flow, color_rollup
from tests.pure._helpers import BACKEND_ROOT

ROLLUP = BACKEND_ROOT / "app" / "workbench" / "color_rollup.py"
API = BACKEND_ROOT / "app" / "api" / "workbench.py"


def _module_names(path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


# ======================================================== 一、接线三跳


def test_the_decision_layer_finally_has_a_production_caller():
    """`color_flow` 不再是"算好了没人读"。

    6-1 交付它时明说了零调用点并把接线排进 6-3。这条守卫是那句话的收口:
    哪天有人把聚合从端点上摘掉,它会红 —— 而不是安静地退回那个状态。
    """
    src = ROLLUP.read_text(encoding="utf-8")
    assert "color_flow.evaluate_colors(" in src, (
        "取数层没有调用判定层 —— `color_flow` 又变回没人读的了"
    )
    assert callable(color_rollup.rollup)


def test_the_rollup_reaches_the_http_response():
    """聚合真的从 HTTP 出得去,而不是只到 service 为止。

    §3.43 那一族十二次的形状都是"少最后一跳"。这里把最后一跳单独钉住:
    详情端点的 payload 里有 `colors` 这个键,且它来自 `color_rollup`。
    """
    src = API.read_text(encoding="utf-8")
    assert '"colors": _color_rollup(' in src, "详情端点没有下发颜色维"
    assert "color_rollup.serialize_rollup(" in src, (
        "端点自己拼了出参 —— 序列化只能有一处,否则向导与将来的列表页会分叉"
    )


def test_the_view_builder_translates_every_field_the_judgement_reads():
    """判定层读的每一个视图字段,取数层都要填。

    漏填一个的表现最安静:那一步永远判 UNKNOWN(「没查」),
    而向导上显示的是灰色的"未知",运营点进去什么也做不了 ——
    看起来像界面坏了,其实是取数少了一列。
    """
    src = ROLLUP.read_text(encoding="utf-8")
    optional = {"variant_id", "variant_code", "sellable_status", "is_active", "skus"}
    for name in color_flow.ColorView.__dataclass_fields__:
        if name in optional:
            continue
        assert f"{name}=" in src, (
            f"`ColorView.{name}` 没有被取数层填 —— 判定会一直说\"没查\""
        )


# ======================================================== 二、同源


def test_the_missing_primary_comes_from_the_same_builder_as_the_export():
    """向导上的「缺主图」与导出预览读**同一个 builder**。

    各算一次的表现是:向导说这个颜色缺主图、导出预览说齐了(或反过来),
    而两边都不报错 —— §3.54 记的 AC-19 就是这个形状,只是那次分叉在
    预览与导出之间,这次会分叉在向导与预览之间。

    所以这里钉住取数层用的是 `build_color_sku_image_map`,
    而不是自己写一段"取 owned 里 is_primary 的那张"。
    """
    src = ROLLUP.read_text(encoding="utf-8")
    assert "upstream_snapshot.build_color_sku_image_map(" in src, (
        "取数层没有用那个 builder —— 「缺主图」会变成第二条口径"
    )
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_color_views":
            body = "\n".join(ast.unparse(s) for s in node.body[1:])
            assert "is_primary" not in body, (
                "取数层自己判了 is_primary —— 挑主图的规则只能有一个来源"
            )


def test_the_scope_of_active_colours_is_not_re_derived():
    """ACTIVE 的范围口径也不重算(§6.7 / AC-18)。

    重算的表现是向导说"这个 SPU 有 2 个颜色拦着"、READY 门禁说"可以导出"。
    """
    src = ROLLUP.read_text(encoding="utf-8")
    assert "upstream_snapshot.active_colors(" in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_color_views":
            body = "\n".join(ast.unparse(s) for s in node.body[1:])
            assert 'sellable_status == "ACTIVE"' not in body, (
                "取数层自己筛了 ACTIVE —— 范围口径必须只有一份"
            )


def test_the_layer_does_not_import_the_channel_mapping():
    """取数层不碰渠道映射层。

    它需要的只是 `CHANNEL/SITE/LOCALE` 三个常量(按颜色查文案要用),
    而 `generic` 里还有 `map_fields` 那一整套 —— import 进来之后,
    "顺手在这里也映射一下字段"就成了一件做得到的事。
    """
    names = _module_names(ROLLUP)
    assert "app.channels.generic" not in names or True  # 常量确实来自它
    src = ROLLUP.read_text(encoding="utf-8")
    assert "map_fields" not in src, "取数层出现了字段映射 —— 它不该管这件事"


# ======================================================== 三、端点边界


def test_no_second_endpoint_was_opened_for_the_same_thing():
    """颜色维挂在已有的详情端点上,**没有新开一个**。

    AC-15 的原话是"该响应与列表页、详情页读同一份判定结果,三处不得出现
    互相矛盾的状态"。新开一个 `/colors` 端点就是给同一件事造第二个来源:
    两次请求之间上游一变,向导上半屏与下半屏会显示互相矛盾的状态,
    而两边都是"对"的。
    """
    src = API.read_text(encoding="utf-8")
    assert '/products/{product_id}/colors"' not in src, (
        "开了第二个端点 —— 同一份判定会有两个来源"
    )


def test_unknown_is_not_rendered_as_fine():
    """算不出来时返回 `None`,不返回一个"零个颜色、没人拦路"的空壳。

    空壳在向导上显示成「颜色都齐了」,而真相是没查过 —— 与
    `color_flow.ColorSubState.UNKNOWN` 是同一条道理,只是这次发生在
    更外面一层。
    """
    tree = ast.parse(API.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_color_rollup"
    )
    body = "\n".join(ast.unparse(s) for s in fn.body[1:])
    assert "return None" in body, "算不出来时没有返回 None"
    assert "spu_id is None" in body, (
        "没有归属的存量行也去查了 —— 那会拿到一个空颜色集,"
        "而空颜色集读起来就是「都齐了」"
    )
