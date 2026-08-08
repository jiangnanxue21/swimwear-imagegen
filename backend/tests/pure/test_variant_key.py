"""A44:变体身份稳定、且不跨 SPU 串档。

这一组钉的是 `docs/DECISIONS.md` §3.17 记下的那条遗留,以及本轮在改它
的过程中发现的一个更严重的洞(VARIANT 属性的 owner_id 里没有 SPU)。

**全部是纯判定,没有一条碰库。** 真库那一半(迁移回填、并发建集)
写在 `tests/test_a45_batch13_spu_db.py` 与 `tests/test_a45_batch13_3_db.py`,
带 `requires_db`,本轮跑不了。(这里原来指向 `tests/test_variant_key_db.py`,
树里不存在。)
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.attributes.validation import (
    AttributeOwnerError,
    AttributeValueError,
    owner_for,
    split_variant_owner_id,
)
from app.listings import variant_key as vk
from tests.pure._helpers import BACKEND_ROOT, expect_raises

#: 两个 `color_variants.id`。阶段 1 之后 VARIANT 层的 owner 就是它们。
#: 写成常量而不是每处 `uuid4()`:用例要断言"同一个 id 算出同一个 owner",
#: 随机值会让那条断言变成一句空话
_CV_A = "0f9f1c2e-1111-4a00-8000-000000000001"
_CV_B = "0f9f1c2e-2222-4a00-8000-000000000002"

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
    uid = "11111111-1111-1111-1111-111111111111"
    rows = [
        vk.VariantRow(sku="SW-1-S", uid=uid, key="black", color="曜石黑"),
        vk.VariantRow(sku="SW-1-M", uid=uid, key="black", color="曜石黑"),
    ]
    refs = vk.refs_of(rows)
    assert len(refs) == 1
    assert refs[0].key == uid and refs[0].label == "曜石黑"


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
        # 0046 后 key 只是降级资料,不能再驱动身份或改名诊断。
        vk.VariantRow(sku="SW-1-S", uid="uid-black", key="black", color="曜石黑"),
        # 两个 UUID 变体被改成同名 —— 真问题:界面上分不出该给哪个绑图。
        vk.VariantRow(sku="SW-2-S", uid="uid-red1", key="red1", color="红色"),
        vk.VariantRow(sku="SW-2-M", uid="uid-red2", key="red2", color="红色"),
        # 还没有 UUID —— 仍然不许隐身。
        vk.VariantRow(sku="SW-3-S", key="", color="蓝"),
    ]
    report = vk.drift(rows)
    assert report["renamed"] == []
    assert report["label_collisions"] == ["uid-red1", "uid-red2"]
    assert report["unassigned"] == ["SW-3-S"]


def test_ids_are_ordered_and_deduplicated():
    """进审计与错误消息时顺序要可复现,否则同一份数据两次批准
    给出的违规文案顺序不同,像是数据变了。"""
    rows = [
        vk.VariantRow(sku="SW-1-M", uid="uid-black", key="black", color="黑"),
        vk.VariantRow(sku="SW-1-S", uid="uid-black", key="black", color="黑"),
        vk.VariantRow(sku="SW-1-R", uid="uid-red", key="red", color="红"),
    ]
    assert vk.ids_of(rows) == ["uid-black", "uid-red"]


# --------------------------------------------------------------- 跨 SPU 串档

def test_two_spus_with_the_same_colour_do_not_share_an_attribute_row():
    """本轮发现的那个洞,**以及它今天是怎么被关掉的**。

    `product_attribute_values` 的唯一索引是 (owner_type, owner_id, field_name),
    **没有 SPU**。A43 把 VARIANT 的 owner_id 直接写成颜色名,于是给
    SPU-A 的黑色确认一次,SPU-B 的黑色跟着变,没有任何提示。

    ## A45-batch14-28:守的还是这条不变式,机制换了

    A44 用命名空间前缀关掉它;阶段 1 把 owner_id 切成 `color_variants.id`
    之后,关掉它的是**UUID 本身全局唯一**。所以这一条不退役 —— 它守的
    「同色跨 SPU 不许共用一行」从头到尾没变,退役的只是当时的实现。

    断言因此改成对着 `owner_for()` 的出口问,而不是对着某一个铸造函数问:
    换实现时它跟着换,换掉不变式时它变红。
    """
    a = owner_for("primary_color", spu="SPU-SW-001", variant_id=_CV_A, sku="x")[1]
    b = owner_for("primary_color", spu="SPU-SW-002", variant_id=_CV_B, sku="y")[1]
    assert a != b

    # 反向:同一个颜色变体在两个 SPU 下**不可能出现** —— `color_variants`
    # 的行本身就带 spu_id。真出现了那是数据损坏,不是这一层能救的
    same = owner_for("primary_color", spu="SPU-SW-002", variant_id=_CV_A, sku="z")[1]
    assert same == a, "同一个 color_variants.id 算出了两个 owner —— 身份不再稳定"


def test_the_legacy_namespace_still_parses_unambiguously():
    """**存量行的解析口径。** 铸造退役了,解析没有。

    库里 0046 之前写下的 VARIANT owner_id 还是 `<len>:<spu>/<variant_id>`,
    而 SPU 是自由文本、含斜杠不违法:

        spu="A"   variant="B/C"  ->  "A/B/C"
        spu="A/B" variant="C"    ->  "A/B/C"

    长度前缀让这个歧义在结构上不存在。`orphaned_variant_owners()` 与
    0046 的降级都靠它切,切错的表现是一批属性被认成属于另一个 SPU。

    ## 期望值钉的是**字面量**,不是铸造函数的返回值

    原来这里拿 `variant_owner_id()` 的输出当期望值。那本来就是一个弱断言:
    铸造和解析一起漂的话 round-trip 照样成立,而库里真正躺着的那串字节
    谁都读不了。铸造退役之后这个弱点必须补上 —— 字面量是唯一诚实的期望值。
    """
    assert split_variant_owner_id("1:A/B/C") == ("A", "B/C")
    assert split_variant_owner_id("3:A/B/C") == ("A/B", "C")
    assert "1:A/B/C" != "3:A/B/C"


def test_a_pre_a44_bare_owner_id_is_recognisably_not_namespaced():
    """巡检要能回答"这一行属于哪个 SPU"。裸 id 拆不出来,而返回 None
    正是那个信号 —— 它可能属于任何一个 SPU。"""
    assert split_variant_owner_id("black") is None
    assert split_variant_owner_id("3:AB/x") is None  # 长度前缀对不上


def test_owner_for_puts_the_variant_uuid_on_variant_fields_and_leaves_the_others_alone():
    """VARIANT 层的 owner_id **就是** `color_variants.id`(阶段 1)。

    命名空间随之退役:它的存在理由是变体 id 取值为颜色名,而 UUID 撞不上。
    """
    kind, owner = owner_for(
        "primary_color", spu="SPU-1", variant_id=_CV_A, sku="SW-1-BLK-S"
    )
    assert kind.value == "VARIANT"
    assert owner == _CV_A, "VARIANT 的 owner_id 不是裸 UUID —— 读写会对不齐"
    assert split_variant_owner_id(owner) is None, "还在套命名空间"

    # SPU 层保持裸值:spu 本身就是全局唯一的
    _, spu_owner = owner_for("pattern_type", spu="SPU-1", variant_id=_CV_A, sku="SW-1")
    assert spu_owner == "SPU-1"


def test_owner_for_refuses_a_variant_id_that_is_not_a_uuid():
    """**这一条是顺序装错时唯一会响的东西。**

    `products.color_variant_id` 没回填的行,身份会掉到种子表达式
    (`primary_color or sku`)。回落着写进去的话,值落在一个种子形式的
    owner_id 上,而读取侧按 UUID 找 —— 值还在库里,界面上永远读不到,
    **两侧都不报错**。§3.22 说的"识别侧静默丢颜色字段"就是这个形状。

    所以不许回落,当场抛。而且抛的是 `AttributeOwnerError` 而不是
    `AttributeValueError`:后者会被 `apply_evidence` 吞掉(见那个类的文档)。
    """
    expect_raises(
        AttributeOwnerError,
        owner_for,
        "primary_color",
        spu="SPU-1",
        variant_id="black",
        sku="SW-1",
    )


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
