"""A44:变体身份稳定、且不跨 SPU 串档。

这一组钉的是 `docs/DECISIONS.md` §3.17 记下的那条遗留,以及本轮在改它
的过程中发现的一个更严重的洞(VARIANT 属性的 owner_id 里没有 SPU)。

**全部是纯判定,没有一条碰库。** 真库那一半(迁移回填、并发建集)
写在 `tests/test_variant_key_db.py`,带 `requires_db`,本轮跑不了。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.attributes.validation import (
    AttributeValueError,
    owner_for,
    split_variant_owner_id,
    variant_owner_id,
)
from app.listings import variant_key as vk
from tests.pure._helpers import BACKEND_ROOT, expect_raises

# --------------------------------------------------------------- 归一

def test_normalize_folds_the_differences_that_are_only_typing():
    # 全角/半角、大小写、随手打的空格、连字符 —— 四种都是同一个颜色
    assert vk.normalize("Ｒｅｄ") == vk.normalize("red") == vk.normalize("RED")
    assert vk.normalize("深 蓝") == vk.normalize("深蓝")
    assert vk.normalize("navy-blue") == vk.normalize("navy_blue") == vk.normalize("navyblue")
    assert vk.normalize(None) == "" == vk.normalize("   ")


def test_normalize_does_not_guess_synonyms():
    """猜错的后果是两个变体被悄悄并成一个 —— 比多一个变体严重得多。"""
    assert vk.normalize("藏青") != vk.normalize("navy")


# --------------------------------------------------------------- 铸键

def test_a_new_size_of_an_existing_colour_reuses_that_colours_key():
    """同 SPU 同色必须复用。少了这一步,红色 S 和红色 M 会是两个变体,
    于是"红色缺图"这件事永远检查不出来。"""
    siblings = [vk.VariantRow(sku="SW-1-RED-S", key="red", color="red")]
    assert vk.mint_key(color="Red", sku="SW-1-RED-M", siblings=siblings) == "red"


def test_an_empty_colour_never_joins_another_empty_colour():
    """空颜色是"还不知道",不是一种颜色。把两个未知归并成一个变体,
    等于宣称它们同色。"""
    siblings = [vk.VariantRow(sku="SW-1-A", key="SW-1-A", color="")]
    assert vk.mint_key(color="", sku="SW-1-B", siblings=siblings) == "SW-1-B"


def test_the_seed_is_the_pre_a44_expression():
    """迁移日的取值必须与迁移前逐字节相同,否则 0027 跑完的瞬间
    所有存量引用一起指空。"""
    assert vk.seed_for(color="  black ", sku="SW-1") == "black"
    assert vk.seed_for(color="   ", sku="SW-1") == "SW-1"
    assert vk.mint_key(color="black", sku="SW-1", siblings=[]) == "black"


def test_a_colour_that_collides_with_an_unrelated_key_gets_disambiguated():
    """A 行没颜色 -> key 是它的 SKU;之后 B 行的颜色文案恰好叫这个 SKU。
    荒谬但不违法,而两个变体共用一个 key 会让属性互相覆盖。"""
    siblings = [vk.VariantRow(sku="SW-7", key="SW-7", color="")]
    minted = vk.mint_key(color="SW-7", sku="SW-8", siblings=siblings)
    assert minted != "SW-7"
    assert minted.startswith("SW-7" + vk.DISAMBIGUATOR)


def test_a_minted_key_always_fits_the_column():
    long_colour = "赤" * 200
    minted = vk.mint_key(color=long_colour, sku="SW-1", siblings=[])
    assert len(minted) <= vk.MAX_KEY_LENGTH
    # 撞了还要能继续消歧,而后缀不能被截掉 —— 截掉的话第 2 个和第 3 个一样
    taken = [vk.VariantRow(sku="SW-1", key=minted, color=long_colour + "x")]
    second = vk.mint_key(color=long_colour, sku="SW-2", siblings=taken)
    assert second != minted and len(second) <= vk.MAX_KEY_LENGTH


# --------------------------------------------------------------- 名字与身份分开

def test_the_label_follows_the_rename_and_the_key_does_not():
    """这是整轮的那一句话:改名只改名字。"""
    assert vk.label_of(key="black", color="曜石黑") == "曜石黑"
    # 颜色被清空时才回落到 key —— 界面上总要显示点什么
    assert vk.label_of(key="black", color="") == "black"


def test_refs_expose_both_names_for_one_variant():
    rows = [
        vk.VariantRow(sku="SW-1-S", key="black", color="曜石黑"),
        vk.VariantRow(sku="SW-1-M", key="black", color="曜石黑"),
    ]
    refs = vk.refs_of(rows)
    assert len(refs) == 1
    assert refs[0].key == "black" and refs[0].label == "曜石黑"


# --------------------------------------------------------------- 翻译

def test_a_tag_typed_from_the_current_colour_resolves_to_the_key():
    """人照着界面打字,库里存的是 key。不翻译的话越是新绑的图越对不上。"""
    refs = [vk.VariantRef(key="black", label="曜石黑")]
    assert vk.resolve_ref("曜石黑", refs) == "black"
    assert vk.resolve_ref("black", refs) == "black"
    assert vk.resolve_ref("  BLACK  ", refs) == "black"


def test_an_ambiguous_label_is_refused_rather_than_guessed():
    """两个变体被改成同名时挑一个,后果是图挂到错的颜色上,
    而那个错误只有平台会发现。"""
    refs = [vk.VariantRef(key="red1", label="红色"), vk.VariantRef(key="red2", label="红色")]
    assert vk.resolve_ref("红色", refs) is None


def test_an_unknown_tag_resolves_to_none():
    assert vk.resolve_ref("紫色", [vk.VariantRef(key="black", label="黑")]) is None
    assert vk.resolve_ref(None, []) is None
    assert vk.resolve_ref("   ", []) is None


# --------------------------------------------------------------- 漂移诊断

def test_drift_separates_a_harmless_rename_from_a_real_collision():
    rows = [
        # 改过名 —— 正常,只是让人知道 key 不是当前颜色
        vk.VariantRow(sku="SW-1-S", key="black", color="曜石黑"),
        # 两个变体被改成同名 —— 真问题:界面上分不出该给哪个绑图
        vk.VariantRow(sku="SW-2-S", key="red1", color="红色"),
        vk.VariantRow(sku="SW-2-M", key="red2", color="红色"),
        # 还没分到 key —— 仍然会被改名带偏,不许隐身
        vk.VariantRow(sku="SW-3-S", key="", color="蓝"),
    ]
    report = vk.drift(rows)
    assert "black" in report["renamed"]
    assert report["label_collisions"] == ["red1", "red2"]
    assert report["unassigned"] == ["SW-3-S"]


def test_ids_are_ordered_and_deduplicated():
    """进审计与错误消息时顺序要可复现,否则同一份数据两次批准
    给出的违规文案顺序不同,像是数据变了。"""
    rows = [
        vk.VariantRow(sku="SW-1-M", key="black", color="黑"),
        vk.VariantRow(sku="SW-1-S", key="black", color="黑"),
        vk.VariantRow(sku="SW-1-R", key="red", color="红"),
    ]
    assert vk.ids_of(rows) == ["black", "red"]


# --------------------------------------------------------------- 跨 SPU 串档

def test_two_spus_with_the_same_colour_do_not_share_an_attribute_row():
    """本轮发现的那个洞。

    `product_attribute_values` 的唯一索引是 (owner_type, owner_id, field_name),
    **没有 SPU**。A43 把 VARIANT 的 owner_id 直接写成颜色名,于是给
    SPU-A 的黑色确认一次,SPU-B 的黑色跟着变,没有任何提示。
    """
    a = variant_owner_id(spu="SPU-SW-001", variant_id="black")
    b = variant_owner_id(spu="SPU-SW-002", variant_id="black")
    assert a != b


def test_the_namespace_cannot_be_made_ambiguous_by_a_slash_in_the_spu():
    """SPU 是自由文本(schemas 只校验长度),含斜杠不违法。

    直接拼 `spu + "/" + variant` 的话:
        spu="A"   variant="B/C"  ->  "A/B/C"
        spu="A/B" variant="C"    ->  "A/B/C"
    两个不同的变体拼出同一个 owner_id,后果与上一条一模一样。
    """
    one = variant_owner_id(spu="A", variant_id="B/C")
    two = variant_owner_id(spu="A/B", variant_id="C")
    assert one != two
    assert split_variant_owner_id(one) == ("A", "B/C")
    assert split_variant_owner_id(two) == ("A/B", "C")


def test_a_pre_a44_bare_owner_id_is_recognisably_not_namespaced():
    """巡检要能回答"这一行属于哪个 SPU"。裸 id 拆不出来,而返回 None
    正是那个信号 —— 它可能属于任何一个 SPU。"""
    assert split_variant_owner_id("black") is None
    assert split_variant_owner_id("3:AB/x") is None  # 长度前缀对不上


def test_owner_for_namespaces_variant_fields_and_leaves_the_others_alone():
    kind, owner = owner_for(
        "primary_color", spu="SPU-1", variant_id="black", sku="SW-1-BLK-S"
    )
    assert kind.value == "VARIANT"
    assert split_variant_owner_id(owner) == ("SPU-1", "black")

    # SPU 层不套命名空间:spu 本身就是全局唯一的
    _, spu_owner = owner_for("pattern_type", spu="SPU-1", variant_id="black", sku="SW-1")
    assert spu_owner == "SPU-1"


def test_a_variant_field_without_a_spu_is_refused():
    """回落到空字符串会让不同商品的属性挤进同一个 owner,
    而那种数据损坏在读取时看起来完全正常。"""
    err = expect_raises(
        AttributeValueError,
        owner_for,
        "primary_color",
        spu="  ",
        variant_id="black",
        sku="SW-1",
    )
    assert err.field_name == "primary_color"


# --------------------------------------------------------------- 接线钉子

def _assigned_attributes(path: Path, attr: str) -> list[str]:
    """AST 找出 `X.<attr> = ...` 的所有出现,返回所在函数名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign):
                continue
            for target in sub.targets:
                if isinstance(target, ast.Attribute) and target.attr == attr:
                    hits.append(node.name)
    return hits


def test_only_assign_variant_key_writes_the_identity_column():
    """身份列被别处赋值一次,已确认的属性、已绑的图片标签、导出的变体列
    会同时指向一个不存在的变体,而三者都不报错。

    靠注释约束不住这件事(同 `test_attribute_merge` 那条投影列扫描)。
    """
    app_dir = BACKEND_ROOT / "app"
    offenders: list[str] = []
    for path in app_dir.rglob("*.py"):
        for func in _assigned_attributes(path, "variant_key"):
            if path.name == "variants.py" and func == "assign_variant_key":
                continue
            offenders.append(f"{path.relative_to(app_dir)}:{func}")
    assert not offenders, (
        "只有 listings/variants.py 的 assign_variant_key 允许写 variant_key,"
        f"这些地方也写了:{offenders}"
    )


def test_the_create_paths_assign_a_key_before_insert():
    """先 add 再分配的话,新行会在同门兄弟里查到自己,于是"已有同色变体"
    永远成立 —— 复用逻辑从第 2 行起再也不会执行,而测试全绿。"""
    source = (BACKEND_ROOT / "app" / "services" / "product_service.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    for name in ("create_product", "import_products"):
        func = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name
        )
        order: list[str] = []
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if called in ("assign_variant_key", "add"):
                order.append(called)
        assert "assign_variant_key" in order, f"{name} 没有分配变体身份"
        assert order.index("assign_variant_key") < order.index("add"), (
            f"{name} 必须在 session.add() 之前分配 variant_key"
        )


def test_the_image_set_write_path_translates_tags_before_deduplicating():
    """去重的键是 (media_asset_id, variant_id or "")。先去重再翻译的话,
    同一张图打了「红色」和它的 key 两个标签会被放行,翻译完变成同一项,
    而唯一约束在库里 —— 报出来是 500,不是那句能读的 422。"""
    source = (BACKEND_ROOT / "app" / "listings" / "image_set_service.py").read_text(
        encoding="utf-8"
    )
    func = next(
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.FunctionDef) and n.name == "create_set"
    )
    order: list[str] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if called in ("_resolve_variant_refs", "_reject_duplicates"):
                order.append(called)
    assert order.index("_resolve_variant_refs") < order.index("_reject_duplicates")


def test_variant_attr_map_is_called_with_a_spu():
    """没有 spu 就拼不出命名空间,而拼不出的后果是查裸 owner_id ——
    也就是跨 SPU 共用的那一份。"""
    source = (BACKEND_ROOT / "app" / "workbench" / "service.py").read_text(encoding="utf-8")
    call = next(
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "variant_attr_map"
    )
    assert len(call.args) == 3, "variant_attr_map(session, spu, variant_ids)"
