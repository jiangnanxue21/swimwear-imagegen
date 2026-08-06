"""A45-batch13-3:对 batch13-2 交付包的走读修复。

## 这一批修的六条

    R1  `ColorVariant.skus` 缺 `passive_deletes` —— ORM 删颜色时先把
        `products.color_variant_id` 置 NULL 再删父行,RESTRICT 根本没机会响。
        "删颜色被数据库拦住"这句验收在 `session.delete()` 这条路上是假的,
        而那条路正是所有服务代码会走的路
    R2  `update_product` 改受众只改九份副本,不动 `spus.audience` 权威 ——
        §4.2 定下的唯一权威被标准操作流静默分叉;顺带,SPU 管理的行还能把
        受众清成 NULL(权威列 NOT NULL,同步则 500、不同步则分叉,两头都是错)
    R3  `spu_id` / `color_variant_id` 没打 `info={"identity": True}` ——
        `_is_identity_column()` 自述"问模型、不维护第二张名单",而 batch13
        往表上加了两列**最强**的身份,恰好没进那份唯一名单
    R4  `identity_shadowed` 被 STATUS/§3.23 指定为回填那颗雷的看门狗,
        却没有任何界面渲染它 —— 看不见的看门狗与"恒报等于不报"同罪
    R5  `working_name` 为空时变体目录显示裸 UUID(schema 默认就是空串,
        13-2 的用例恰好每次都填了名字,没盖住这条)
    R6  建档拼 `products.name` 可超列宽:255 + 1 + 16 + 1 + 8 = 281 > 255,
        溢出抛的是 DataError,`except IntegrityError` 接不住,运营拿到裸 500

## 守卫为什么长这个样子

R1/R2/R3/R6 的行为都要真库或 sqlalchemy 才能执行,这台机器两样都没有 ——
和 batch13 同一处境,所以同一对策:**AST 钉形状,真库用例钉行为**
(`tests/test_a45_batch13_3_db.py`,进"已写、未执行"的池子)。

形状断言全部按 batch13 那四条 GREEN 变异的教训写:不数字符串出现次数
(注释里也有那些词),认节点的结构 —— If 的三个条件各是什么 Compare、
keyword 的取值是哪个 Constant、Or 的两臂各指哪个属性。
"""
from __future__ import annotations

import ast
import re

from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT

MODELS_SPU = BACKEND_ROOT / "app" / "models" / "spu.py"
MODELS_PRODUCT = BACKEND_ROOT / "app" / "models" / "product.py"
PRODUCT_SERVICE = BACKEND_ROOT / "app" / "services" / "product_service.py"
SPU_SERVICE = BACKEND_ROOT / "app" / "services" / "spu_service.py"
VARIANTS = BACKEND_ROOT / "app" / "listings" / "variants.py"
IMAGE_SET_TAB = (
    PROJECT_ROOT / "frontend" / "src" / "components" / "workbench" / "ImageSetTab.tsx"
)


# ---------------------------------------------------------------- AST 取数


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _class(path, name: str) -> ast.ClassDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 class {name}")


def _func(path, name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 {name}()")


def _keyword(call: ast.Call, name: str) -> ast.keyword | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _attr_chain(node: ast.AST) -> str:
    """`product.spu_id` -> "product.spu_id"。只认 Name/Attribute 两种节点。"""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


# ================================================================ R1 删颜色


def test_the_sku_relationship_leaves_the_children_to_the_database():
    """`passive_deletes` 必须是 **"all"**,`True` 都不够。

    没有它:`session.delete(variant)` 时 SQLAlchemy 先把子行外键置 NULL 再删
    父行 —— 列可空,数据库不拦,"删颜色"成功并把九行 SKU 悄悄孤儿化。
    `True` 只是不去**加载**未加载的子行;已经在 session 里的照样被置 NULL,
    而"刚建完档马上删颜色"恰好就是子行全在 session 里的那条路。
    只有 `"all"` 让 ORM 一个字节都不碰,DELETE 直达 RESTRICT。
    """
    cls = _class(MODELS_SPU, "ColorVariant")
    for stmt in cls.body:
        targets = (
            [stmt.target] if isinstance(stmt, ast.AnnAssign) else getattr(stmt, "targets", [])
        )
        if not any(isinstance(t, ast.Name) and t.id == "skus" for t in targets):
            continue
        call = stmt.value
        assert isinstance(call, ast.Call), "skus 不是 relationship() 调用"
        kw = _keyword(call, "passive_deletes")
        assert kw is not None, (
            "ColorVariant.skus 少了 passive_deletes —— ORM 会在删颜色时把 "
            "products.color_variant_id 置 NULL,RESTRICT 永远响不了"
        )
        assert isinstance(kw.value, ast.Constant) and kw.value.value == "all", (
            f"passive_deletes 必须是 'all'(当前 {ast.dump(kw.value)});"
            "True 挡不住已加载子行被置 NULL"
        )
        return
    raise AssertionError("ColorVariant 里没有 skus 关系")


# ================================================================ R3 身份名单


def test_the_new_identity_columns_carry_the_identity_marker():
    """`_is_identity_column()` 唯一的名单是模型上的 `info={"identity": True}`。

    batch13 加的这两列是 `identity_of()` 的**第一级** —— 改它一次,属性 owner、
    图片标签、导出变体列同时换主。名单漏了它们,`update_product` 的禁改闸
    对最强的身份不设防,只剩 schema 一层(而 schema 挡不住任何直接调服务的路)。
    """
    cls = _class(MODELS_PRODUCT, "Product")
    missing = {"spu_id", "color_variant_id"}
    for stmt in cls.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        if stmt.target.id not in missing or not isinstance(stmt.value, ast.Call):
            continue
        kw = _keyword(stmt.value, "info")
        assert kw is not None and isinstance(kw.value, ast.Dict), (
            f"{stmt.target.id} 的 mapped_column 没有 info 字典"
        )
        marked = any(
            isinstance(k, ast.Constant)
            and k.value == "identity"
            and isinstance(v, ast.Constant)
            and v.value is True
            for k, v in zip(kw.value.keys, kw.value.values, strict=True)
        )
        assert marked, f"{stmt.target.id} 缺 info={{'identity': True}} 标记"
        missing.discard(stmt.target.id)
    assert not missing, f"Product 里找不到这些列的定义:{sorted(missing)}"


# ================================================================ R2 受众权威


def _audience_cascade_body(func: ast.FunctionDef) -> list[ast.stmt]:
    """`if "audience" in applied:` 那个块。同步与传播都必须住在里面。"""
    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Constant)
            and test.left.value == "audience"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.In)
            and isinstance(test.comparators[0], ast.Name)
            and test.comparators[0].id == "applied"
        ):
            return node.body
    raise AssertionError("update_product 里没有 `if \"audience\" in applied:` 块")


def test_clearing_the_audience_on_a_spu_managed_row_is_refused_at_the_gate():
    """那道闸必须**恰好**是三个条件的 And,并且拒绝。

    数出现次数的守卫在 batch13 被 `if ... and False:` 击穿过(M7),
    所以这里认 If.test 的形状:BoolOp(And) 且三臂一臂不多 ——
    多出来的第四臂(比如 False)会让这条守卫红,而不是绿着装样子。
    """
    func = _func(PRODUCT_SERVICE, "update_product")
    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And)):
            continue
        if len(test.values) != 3:
            continue
        shapes = set()
        for value in test.values:
            if not isinstance(value, ast.Compare) or len(value.ops) != 1:
                break
            op, left, right = value.ops[0], value.left, value.comparators[0]
            if (
                isinstance(op, ast.In)
                and isinstance(left, ast.Constant)
                and left.value == "audience"
                and isinstance(right, ast.Name)
                and right.id == "changes"
            ):
                shapes.add("audience_in_changes")
            elif (
                isinstance(op, ast.Is)
                and isinstance(left, ast.Subscript)
                and isinstance(left.value, ast.Name)
                and left.value.id == "changes"
                and isinstance(left.slice, ast.Constant)
                and left.slice.value == "audience"
                and isinstance(right, ast.Constant)
                and right.value is None
            ):
                shapes.add("value_is_none")
            elif (
                isinstance(op, ast.IsNot)
                and _attr_chain(left) == "product.spu_id"
                and isinstance(right, ast.Constant)
                and right.value is None
            ):
                shapes.add("row_is_spu_managed")
        if shapes != {"audience_in_changes", "value_is_none", "row_is_spu_managed"}:
            continue
        raises = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Raise)
            and isinstance(n.exc, ast.Call)
            and getattr(n.exc.func, "id", getattr(n.exc.func, "attr", None))
            == "ValidationError"
        ]
        assert raises, "闸在,但没有拒绝(不抛 ValidationError)"
        return
    raise AssertionError(
        "update_product 缺那道闸:SPU 管理的行(spu_id 非空)把受众清成 NULL "
        "必须在入口被拒 —— 权威列 NOT NULL,同步会 500,不同步会分叉"
    )


def test_the_audience_edit_updates_the_spu_authority_in_the_same_block():
    """副本怎么改,权威就怎么改 —— 而且在 `if "audience" in applied:` 里面。

    放在块外的同步会在"这次没改受众"的请求上也去摸 SPU;放在块里、
    但拿错行(不经 `product.spu_id`)的同步会把别的款改了。
    所以两件事一起钉:`session.get(Spu, product.spu_id)` 与
    `spu_row.audience = ...` 都必须出现在这个块里。
    """
    body = _audience_cascade_body(_func(PRODUCT_SERVICE, "update_product"))
    got_lookup = False
    got_assign = False
    for stmt in body:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and _attr_chain(node.func.value) == "session"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "Spu"
                and _attr_chain(node.args[1]) == "product.spu_id"
            ):
                got_lookup = True
            if isinstance(node, ast.Assign) and any(
                _attr_chain(t) == "spu_row.audience" for t in node.targets
            ):
                got_assign = True
    assert got_lookup, (
        "受众级联块里没有 session.get(Spu, product.spu_id) —— 权威没被找到"
    )
    assert got_assign, (
        "受众级联块里没有对 spu_row.audience 的赋值 —— 权威没被改,副本分叉"
    )


def test_the_sibling_propagation_also_matches_by_foreign_key():
    """兄弟行 = 字符串命中 ∪ 外键命中。

    只按 `Product.spu` 字符串查,对带外键的行违反 §4.4;只按外键查,
    混合期里同款的存量/导入行(外键为空)会漏。所以要的是 `or_` 里
    那条 `Product.spu_id == product.spu_id` —— 认这个 Compare 的两端,
    不认"文件里出现过 spu_id"(注释里也出现过)。
    """
    body = _audience_cascade_body(_func(PRODUCT_SERVICE, "update_product"))
    for stmt in body:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Compare)
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)
                and _attr_chain(node.left) == "Product.spu_id"
                and _attr_chain(node.comparators[0]) == "product.spu_id"
            ):
                return
    raise AssertionError(
        "受众传播的兄弟行筛选里没有 Product.spu_id == product.spu_id —— "
        "带外键的行只能靠反规范化字符串命中,§4.4 禁止"
    )


# ================================================================ R5 目录名


def test_the_catalog_label_falls_back_to_the_variant_code():
    """`working_name or variant_code`,顺序不能反。

    `working_name` 在建档表单上可选(schema 默认空串)。没有这级回落,
    只填了编码的颜色在目录里是一串 UUID —— "BLK" 不是好名字,但它是
    运营自己填、印在样品袋上的编码;十六进制谁也认不出。
    反过来(`variant_code or working_name`)则是编码永远压过名字,
    运营填的名字再也显示不出来 —— 所以两臂的**顺序**也在断言里。
    """
    func = _func(VARIANTS, "_rows")
    for node in ast.walk(func):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
            continue
        attrs = [
            v.attr for v in node.values if isinstance(v, ast.Attribute)
        ]
        if attrs == ["working_name", "variant_code"]:
            return
    raise AssertionError(
        "_rows() 里没有 `working_name or variant_code` 这级回落 —— "
        "working_name 为空的颜色在目录里会显示成 UUID"
    )


# ================================================================ R6 name 列宽


def test_the_derived_name_is_clamped_to_the_column_width():
    """`Product(name=...)` 的值必须是 `...[: limit_for("name")]`。

    认的是 Subscript+Slice 的形状且上界是 `limit_for("name")` ——
    写死 255 的截断会在有人改列宽时静默过期,而这正是 `field_limits`
    存在的理由(列宽、schema、截断同一份来源)。
    """
    func = _func(SPU_SERVICE, "create_spu")
    for node in ast.walk(func):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Product"
        ):
            continue
        kw = _keyword(node, "name")
        assert kw is not None, "Product(...) 没有 name 参数"
        value = kw.value
        assert isinstance(value, ast.Subscript) and isinstance(value.slice, ast.Slice), (
            "name 没有截断 —— internal_name 超过 ~229 字符时 flush 抛 DataError,"
            "except IntegrityError 接不住,运营拿到裸 500"
        )
        upper = value.slice.upper
        assert (
            isinstance(upper, ast.Call)
            and isinstance(upper.func, ast.Name)
            and upper.func.id == "limit_for"
            and len(upper.args) == 1
            and isinstance(upper.args[0], ast.Constant)
            and upper.args[0].value == "name"
        ), "截断上界必须是 limit_for(\"name\"),不是写死的数字"
        return
    raise AssertionError("create_spu 里没有 Product(...) 调用")


def test_the_name_clamp_is_actually_reachable():
    """上限必须小于能拼出来的最大长度,否则这条截断一次都不会执行。

    batch13 的 M9(行数上限 200 永远触发不了)是同一个形状:
    一条永远不生效的保护比没有更糟,它看起来在保护。
    255(schemas/spu.SpuCreate.internal_name 的上限)+ 空格 + 颜色 + 空格 + 尺码。
    """
    from app.core.field_limits import limit_for
    from app.listings.sku_matrix import MAX_SIZE_CODE, MAX_VARIANT_CODE

    longest = 255 + 1 + MAX_VARIANT_CODE + 1 + MAX_SIZE_CODE
    assert longest > limit_for("name"), (
        f"最长拼接 {longest} ≤ 列宽 {limit_for('name')},截断永远不会执行 —— "
        "要么去掉它,要么这条守卫和它一起过期了"
    )


# ================================================================ R4 看门狗上屏


def test_the_shadow_watchdog_is_rendered_not_just_typed():
    """`identity_shadowed` 必须有人渲染,不只是出现在类型联合里。

    STATUS 与 §3.23 把它指定为回填那颗雷的看门狗(「在那之前由
    drift().identity_shadowed 盯着」)。13-2 把它写进了 `imageSets.ts` 的
    联合类型 —— 类型不渲染任何东西。四件事一起钉:从 drift 里取出来、
    有渲染守卫、真的把 SKU 列出来、并且把整条横幅升级成 error
    (它被文档定为"真问题",和 label_collisions 同级)。
    """
    src = IMAGE_SET_TAB.read_text(encoding="utf-8")
    assert re.search(r"coverage\?\.drift\.identity_shadowed", src), (
        "ImageSetTab 没有从 drift 里取 identity_shadowed"
    )
    # 锚在 JSX 表达式的开括号上:渲染守卫必须**就是**这个表达式本身。
    # 第一版没锚,`{false && identityShadowed.length > 0 && (` 照样匹配 ——
    # 渲染死了、守卫还绿,batch13 M7 的原样重演,被本批次的变异验证当场抓到
    assert re.search(r"\{identityShadowed\.length > 0 && \(", src), (
        "identity_shadowed 没有渲染块 —— 看不见的看门狗和没有是一回事"
    )
    assert "identityShadowed.join(" in src, (
        "渲染块没有把具体 SKU 列出来 —— 只说'有问题'等于让人自己去猜是哪几行"
    )
    assert re.search(r"\|\| identityShadowed\.length > 0 \? 'error'", src), (
        "identity_shadowed 非空时横幅必须是 error —— 它被文档定为'真问题'"
    )
