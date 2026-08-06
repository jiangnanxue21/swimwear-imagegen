"""阶段 1 第一批:身份规范化的骨架(A45-batch13,PRD v3.1 §4.2~4.4 / §13 阶段 1)。

## 这一批做了什么

两张新表(`spus` / `color_variants`)、`products` 六个新列、一条迁移(0035),
以及把三者接起来的建档路径:一次请求落一个 SPU、若干颜色、以及它们展开出来的
SKU 行。阶段 1 的第一条验收「可构造三颜色九 SKU 的 SPU」在这里是可执行的。

## 这份守卫分两半,来源不同

    纯逻辑   `listings/sku_matrix` 只依赖标准库,直接 import 进来真的跑
    AST      服务层 / 模型 / schema 都 import sqlalchemy 或 pydantic,
             纯测试环境里 import 不进来 —— 只能读源文本做结构断言

第二半挡得住「有人把这些决定删掉」,**挡不住「决定本身是错的」**。真库那一半
写在 `tests/test_a45_batch13_spu_db.py`,和 12-4 / 12-5 / 12-7 的用例同一个
池子:已写、未执行。不要把这份守卫全绿读成「建档跑通了」。
"""
from __future__ import annotations

import ast

from app.listings import sku_matrix as m
from tests.pure._helpers import BACKEND_ROOT, expect_raises

SPU_MODEL = BACKEND_ROOT / "app" / "models" / "spu.py"
PRODUCT_MODEL = BACKEND_ROOT / "app" / "models" / "product.py"
SPU_SERVICE = BACKEND_ROOT / "app" / "services" / "spu_service.py"
SPU_SCHEMA = BACKEND_ROOT / "app" / "schemas" / "spu.py"
SPU_API = BACKEND_ROOT / "app" / "api" / "spus.py"
API_ROUTER = BACKEND_ROOT / "app" / "api" / "router.py"
MIGRATION = BACKEND_ROOT / "migrations" / "versions" / "0035_spu_and_color_variant.py"
MODELS_INIT = BACKEND_ROOT / "app" / "models" / "__init__.py"


# ---------------------------------------------------------------- AST 取数


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(path, name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 {name}()")


def _class(path, name: str) -> ast.ClassDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 class {name}")


def _calls(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def _keywords_of_call(node: ast.AST, callee: str) -> dict[str, ast.AST]:
    """找 `callee(...)` 这次调用的关键字实参。用于读模型里的 mapped_column(...)。"""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == callee:
                return {kw.arg: kw.value for kw in child.keywords if kw.arg}
    return {}


def _column(path, class_name: str, column: str) -> ast.AnnAssign:
    cls = _class(path, class_name)
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == column:
                return node
    raise AssertionError(f"{class_name} 上没有 {column} 这一列")


def _assigned_names(node: ast.AST) -> set[str]:
    """函数里所有被赋值的属性名(`x.attr = ...`)与关键字实参名。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            for target in child.targets:
                if isinstance(target, ast.Attribute):
                    names.add(target.attr)
        if isinstance(child, ast.Call):
            names.update(kw.arg for kw in child.keywords if kw.arg)
    return names


# ================================================================ 展开规则


def test_three_colors_and_three_sizes_make_nine_skus():
    """§13 阶段 1 的第一条验收,逐字:「可构造三颜色九 SKU 的 SPU」。"""
    rows = m.expand(
        spu_code="SPU-SW-011",
        variant_codes=["BLK", "NVY", "COR"],
        size_template="ALPHA_3",
    )
    assert len(rows) == 9, f"三颜色 × 三尺码应该是 9 行,拿到 {len(rows)}"
    assert len({r.sku for r in rows}) == 9, "九行 SKU 必须互不相同"
    assert rows[0].sku == "SPU-SW-011-BLK-S"


def test_the_sku_is_composed_of_exactly_three_segments():
    """编码是算出来的,不是人填的 —— 九行 SKU 就是九次打字机会。"""
    assert (
        m.sku_code(spu_code="SPU-SW-011", variant_code="BLK", size="S")
        == "SPU-SW-011-BLK-S"
    )


def test_rows_follow_the_template_order_not_the_alphabet():
    """S/M/L 按字典序排是 L/M/S,而尺码表是运营和平台都要看的东西。"""
    rows = m.expand(
        spu_code="SPU-A", variant_codes=["BLK"], size_template="ALPHA_4"
    )
    assert [r.size for r in rows] == ["S", "M", "L", "XL"]


def test_colors_come_before_sizes_in_the_expansion():
    """先颜色后尺码:同一个颜色的几行挨在一起,建完之后按颜色核对最省事。"""
    rows = m.expand(
        spu_code="SPU-A", variant_codes=["BLK", "NVY"], size_template="ALPHA_3"
    )
    assert [r.variant_code for r in rows] == ["BLK"] * 3 + ["NVY"] * 3


def test_a_separator_inside_the_color_code_is_refused():
    """后两段禁用分隔符 —— 唯一性就靠这个(见模块文档的右起切分)。"""
    error = expect_raises(
        m.SkuPlanError,
        m.expand,
        spu_code="SPU-A",
        variant_codes=["BL-K"],
        size_template="ALPHA_3",
    )
    # 断言的是**报错说清了原因**,不是"消息里出现过一个横线"。第一版写的是
    # 后者,而字符集那条兜底分支的消息里正好也有一个横线(它在列举非法字符)
    # —— 于是把这条专用判断整个删掉,守卫照样绿。
    assert "分段分隔符" in error.message, (
        f"报错没说清为什么不能带分隔符,运营只会看到一句「含非法字符」:{error.message}"
    )


def test_the_spu_segment_may_keep_its_hyphens():
    """`SPU-SW-001` 这种编码印在样品袋和供应商单据上,不能因为拼接方便就要人改掉。

    它安全的理由**不是**"宽松一点":颜色与尺码里没有分隔符,所以从右边数,
    最后两段一定是尺码和颜色,剩下的全是 SPU —— 切法唯一。
    """
    assert m.normalize_spu_code("spu-sw-001") == "SPU-SW-001"


def test_two_different_triples_can_never_collide():
    """右起切分的不变式:拼出来的字符串能唯一还原回三元组。

    这条不是在测某个函数,是在测那条不变式还成不成立 —— 它是允许第一段
    带分隔符的**全部**理由,而下一个人很可能只看到"这里松一点"。
    """
    def split_from_the_right(sku: str) -> tuple[str, str, str]:
        head, _, size = sku.rpartition(m.SEPARATOR)
        spu, _, variant = head.rpartition(m.SEPARATOR)
        return spu, variant, size

    for spu, variant, size in [
        ("SPU-SW-001", "BLK", "S"),
        ("SPU", "SW", "001"),
        ("A-B-C", "D", "E"),
    ]:
        composed = m.sku_code(spu_code=spu, variant_code=variant, size=size)
        assert split_from_the_right(composed) == (spu, variant, size)


def test_lowercase_is_normalized_rather_than_rejected():
    """`blk` 和 `BLK` 在人眼里是一个颜色,在 `uq_products_sku` 眼里是两个。"""
    rows = m.expand(
        spu_code="spu-a", variant_codes=["blk"], size_template="alpha_3"
    )
    assert rows[0].sku.startswith("SPU-A-BLK-")


def test_a_duplicate_color_code_is_named_by_position():
    """`UNIQUE(spu_id, variant_code)` 是最终裁判,但它报不出是第几个重复了。"""
    error = expect_raises(
        m.SkuPlanError,
        m.expand,
        spu_code="SPU-A",
        variant_codes=["BLK", "NVY", "BLK"],
        size_template="ALPHA_3",
    )
    assert "[2]" in error.field, f"应该点名第 3 个(下标 2),实际 {error.field!r}"


def test_illegal_characters_are_listed():
    error = expect_raises(
        m.SkuPlanError, m.normalize_code, "BL K", field="c", max_length=16
    )
    assert "不允许的字符" in error.message


def test_an_empty_code_is_refused():
    expect_raises(m.SkuPlanError, m.normalize_code, "   ", field="c", max_length=16)


def test_a_stray_leading_hyphen_in_the_spu_code_is_caught():
    """多打一个横线不会有任何下游报错,只会让九行 SKU 都长一个双横线。"""
    expect_raises(m.SkuPlanError, m.normalize_spu_code, "-SPU-A")


def test_an_unknown_template_lists_the_options():
    """回落到"空列表"的后果是一行 SKU 都没建,而建档那一步看起来成功了。"""
    error = expect_raises(m.SkuPlanError, m.sizes_of, "ALPHA_9")
    assert "ALPHA_3" in error.message, "报错要把可选模板列出来"


def test_one_size_is_a_real_size_not_null():
    """均码必须是一个真实取值。

    `UNIQUE(color_variant_id, size)` 在 PostgreSQL 下对 NULL 不生效,
    于是尺码留空的行可以在同一个颜色下重复任意多次,而唯一约束一声不吭。
    """
    sizes = m.sizes_of("ONE_SIZE")
    assert len(sizes) == 1
    assert sizes[0].strip(), "均码不能是空串 —— 那等于把 NULL 换了个写法"


def test_size_group_carries_the_template_name():
    """`products.size_group` 从 M6 起就在,取值一直没有来源。"""
    rows = m.expand(spu_code="SPU-A", variant_codes=["BLK"], size_template="ONE_SIZE")
    assert rows[0].size_group == "ONE_SIZE"


def test_every_template_fits_the_sku_column_width():
    """三段的上限加起来必须装进 `products.sku` 的 VARCHAR(64)。

    这条守的是有人把某个上限调大之后忘了另一处 —— 那时的表现是 flush 阶段
    一句 StringDataRightTruncation,行号在批量插入里丢掉。
    """
    longest_spu = "A" * m.MAX_SPU_CODE
    longest_variant = "B" * m.MAX_VARIANT_CODE
    for name, sizes in m.SIZE_TEMPLATES.items():
        for size in sizes:
            sku = m.sku_code(
                spu_code=longest_spu, variant_code=longest_variant, size=size
            )
            assert len(sku) <= 64, f"模板 {name} 的 {size} 拼出来有 {len(sku)} 字符"


def test_every_template_size_is_a_legal_code():
    """模板是常量,但它迟早会变成可配置的 —— 那时这条是唯一的闸。"""
    for name, sizes in m.SIZE_TEMPLATES.items():
        for size in sizes:
            assert (
                m.normalize_code(size, field=name, max_length=m.MAX_SIZE_CODE) == size
            ), f"模板 {name} 里的 {size!r} 不是规范形式"


def test_every_template_has_a_label():
    """下拉项由后端给(硬规则 4)。少一个 label,界面上就是一个裸的机器名。"""
    assert set(m.SIZE_TEMPLATE_LABELS) == set(m.SIZE_TEMPLATES)


def test_the_expansion_row_limit_is_enforced():
    """一次建档落几百行不是大生意,是有人把循环写错了。"""
    error = expect_raises(
        m.SkuPlanError,
        m.expand,
        spu_code="SPU-A",
        variant_codes=[f"C{i:02d}" for i in range(m.MAX_VARIANTS_PER_SPU)],
        size_template="ALPHA_6",
    )
    assert str(m.MAX_SKUS_PER_EXPANSION) in error.message


def test_the_row_limit_can_actually_be_reached():
    """行数上限必须小于「颜色上限 × 最长模板」,否则它一次都不会触发。

    第一版是 200,而 24 × 6 = 144 —— 写完的当天它就是死的,而且**测试照样绿**
    (那条用例只断言"抛了错",拿不到"这个错永远抛不出来"这件事)。
    这条守的正是那个形状:一条永远不会生效的守卫比没有守卫更糟,
    因为它看起来在保护。
    """
    longest = max(len(sizes) for sizes in m.SIZE_TEMPLATES.values())
    assert m.MAX_SKUS_PER_EXPANSION < m.MAX_VARIANTS_PER_SPU * longest, (
        f"上限 {m.MAX_SKUS_PER_EXPANSION} 大于能构造出来的最大行数 "
        f"{m.MAX_VARIANTS_PER_SPU} × {longest} —— 它永远不会触发"
    )


def test_at_least_one_color_is_required():
    expect_raises(
        m.SkuPlanError,
        m.expand,
        spu_code="SPU-A",
        variant_codes=[],
        size_template="ALPHA_3",
    )


# ================================================================ 受众


def test_audience_is_not_nullable_at_the_spu_layer():
    """§4.2:SPU 层没有"待确认受众"。

    `products.audience` 那个可空列是给旧路径留的,不是这里的先例 ——
    给"没填"一个合法取值,它会被当成真受众流进模特筛选和规则包派生。
    """
    node = _column(SPU_MODEL, "Spu", "audience")
    kwargs = _keywords_of_call(node, "mapped_column")
    assert isinstance(kwargs.get("nullable"), ast.Constant)
    assert kwargs["nullable"].value is False


def test_the_migration_gives_audience_no_default():
    """NOT NULL 加一个 server_default 等于把"没填"变成一个合法受众。"""
    text = MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "Column":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            if node.args[0].value == "audience":
                keys = {kw.arg for kw in node.keywords}
                assert "server_default" not in keys, "受众列不许有默认值"
                return
    raise AssertionError("迁移里没有 audience 列")


def test_the_archiving_service_refuses_an_unset_audience():
    """服务层要自己再拦一次:schema 挡得住 REST 入口,挡不住脚本和后续内部调用。

    断言的是**那条 if 的形状**,不是"源码里有没有 `audience is None` 这串字"。
    第一版就是后者,而 `if audience is None and False:` 能让它绿着通过 ——
    变异验证当场抓到的正是这个(守卫在验的是文本,不是行为)。
    """
    fn = _func(SPU_SERVICE, "create_spu")
    guarded = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if (
            isinstance(test.left, ast.Name)
            and test.left.id == "audience"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Is)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
            and any(isinstance(c, ast.Raise) for c in ast.walk(node))
        ):
            guarded = True
    assert guarded, "缺一条『受众为空就抛』的判断,或者它被别的条件稀释了"
    assert "coerce" in _calls(fn), "受众的字符串→枚举只走 core/audience.coerce"


def test_the_create_schema_requires_an_audience():
    """`Audience | None` 会让"不传"成为合法请求。"""
    cls = _class(SPU_SCHEMA, "SpuCreate")
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "audience":
            assert isinstance(node.annotation, ast.Name), "受众不许是可选类型"
            assert node.annotation.id == "Audience"
            assert node.value is None, "受众不许有默认值"
            return
    raise AssertionError("SpuCreate 上没有 audience")


def test_the_audience_ruling_still_lives_in_one_place():
    """多了一张表不等于多了一处判定(§23.5)。"""
    text = SPU_SERVICE.read_text(encoding="utf-8")
    assert "category_code_for" not in text, (
        "规则包键的派生只在 core/audience 一处 —— 建档服务不许自己拼前缀"
    )


# ================================================================ 归属与投影


def test_skus_are_looked_up_by_the_foreign_key_not_the_spu_string():
    """§4.4:`products.spu` 降级为反规范化读列,禁止作为查询权威。

    同样一句"这个 SPU 下面有哪些 SKU",按字符串查会把改过名的行漏掉,
    按外键查不会。阶段 1 的验收里那句"不存在依赖字符串 spu 推断归属的接口"
    在新代码上就是这一条。
    """
    src = ast.get_source_segment(
        SPU_SERVICE.read_text(encoding="utf-8"), _func(SPU_SERVICE, "skus_of")
    )
    assert "Product.spu_id" in src
    assert "Product.spu ==" not in src


def test_the_archiving_path_sets_both_foreign_keys_on_every_row():
    """两个外键这一版可空(旧路径还不知道这两张表),所以"新路径一定写"必须有人钉着。

    钉不住的话,建档建出来的 SKU 可以整批 `spu_id` 为空,而数据库不会拦、
    列表页照常显示 —— 等到阶段 1 第二批要把列收成 NOT NULL 时才发现。
    """
    fn = _func(SPU_SERVICE, "create_spu")
    assigned = _assigned_names(fn)
    assert "spu_id" in assigned, "建档没有写 spu_id"
    assert "color_variant" in assigned, "建档没有挂上颜色变体"


def test_the_ids_exist_before_the_sku_rows_are_built():
    """主键是 `default=uuid.uuid4`,而 Python 侧 default **在 flush 时才求值**。

    少了中间那次 flush,`spu.id` 还是 None,于是九行 SKU 的 spu_id 全是空 ——
    本批次那一列可空,没有任何东西会报错。
    """
    fn = _func(SPU_SERVICE, "create_spu")
    flushes = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "flush"
    ]
    assert len(flushes) >= 2, (
        "建档要 flush 两次:先落 SPU 与颜色拿到主键,再落 SKU 行。"
        f"当前只有 {len(flushes)} 次"
    )


def test_the_archiving_service_never_writes_the_projection_columns():
    """那 8 个列的唯一写入点是属性服务(§5.4),`display_name` 同理(§4.3)。

    建档时填一个"正式颜色名称"等于绕过那个写入点,而绕过它的值没有来源、
    没有证据、不会被复核 —— 读起来却和确认过的事实一模一样。
    """
    projections = {
        "primary_color",
        "secondary_colors",
        "pattern_type",
        "garment_type",
        "material",
        "strap_type",
        "neckline_type",
        "back_style",
        "display_name",
    }
    written = _assigned_names(_func(SPU_SERVICE, "create_spu"))
    leaked = sorted(projections & written)
    assert not leaked, f"建档服务写了投影列:{leaked}"


def test_the_create_schema_has_no_visual_attribute_fields():
    """阶段 1 验收:「不填视觉属性即可建档」。

    做法不是把它们设成可选 —— 可选字段会被前端填成空串,而空串和"还没识别"
    在下游是两件事。
    """
    fields: set[str] = set()
    for name in ("SpuCreate", "ColorVariantCreate"):
        for node in _class(SPU_SCHEMA, name).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                fields.add(node.target.id)
    forbidden = {
        "primary_color",
        "secondary_colors",
        "pattern_type",
        "garment_type",
        "material",
        "strap_type",
        "neckline_type",
        "back_style",
        "display_name",
    }
    leaked = sorted(fields & forbidden)
    assert not leaked, f"建档入参里出现了视觉属性/投影字段:{leaked}"


def _identifiers(path) -> set[str]:
    """源文件里**代码**用到的名字。注释与文档字符串不算。

    分开的理由:退役一个字段的过程中,新模块的文档里几乎一定会提到它
    ("身份不再由 X 推导")。把那种说明算成依赖,守卫会逼着人把解释删掉 ——
    而解释正是下一个人最需要的东西(硬规则 5)。
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def test_the_new_code_does_not_read_variant_key():
    """`variant_key` 在阶段 1 退役(§4.4)。新代码不许再长出对它的依赖。

    退役分两步:先让新路径不再产生新的依赖(这一条),再把存量调用点切掉
    (阶段 1 第二批,前提是 products 都带上了 color_variant_id)。
    """
    for path in (SPU_SERVICE, SPU_SCHEMA, SPU_API, SPU_MODEL):
        assert "variant_key" not in _identifiers(path), (
            f"{path.name} 的代码里用了 variant_key —— 它是被退役的那一个"
        )


# ================================================================ 事务与接线


def test_the_archiving_service_does_not_commit_the_session():
    """事务归**调用方**所有,服务层只 flush(§7.8,`publish_service` 顶部详述)。

    这里说的调用方是路由,不是 `api/deps.get_session` —— 那个依赖明确不提交,
    请求级自动 commit 是 §7.8 的第一条禁止项。配套的另一半守卫在
    `test_a45_batch13_2_fixes.py`:路由**必须**提交。两条一起才说清边界在哪,
    只有这一条的话,"服务不提交"会被读成"没有人需要提交"。

    只数 `commit` 这个名字是不够的:保存点自己也有 `commit()`,而它是修复的
    一部分。所以这里认的是**接收者** —— `session.commit()` 不许有,
    `savepoint.commit()` 必须有。
    """
    fn = _func(SPU_SERVICE, "create_spu")
    receivers = {
        node.func.value.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
        and isinstance(node.func.value, ast.Name)
    }
    assert "session" not in receivers, "服务层不许 commit session"
    assert "savepoint" in receivers, "保存点建了不提交,等于整段写入被丢掉"


def test_the_three_writes_are_one_transaction():
    """SPU、颜色、SKU 行必须一起成或一起不成。

    分三次调用留下的残骸(没有颜色的 SPU / 有颜色没 SKU 的 SPU)在界面上
    都显示为"建档中",而运营唯一的动作是再建一次。
    """
    fn = _func(SPU_SERVICE, "create_spu")
    calls = _calls(fn)
    assert "begin_nested" in calls, "缺保存点:约束冲突会让整个事务进 aborted 状态"
    assert "IntegrityError" in {
        h.type.id
        for h in ast.walk(fn)
        if isinstance(h, ast.ExceptHandler) and isinstance(h.type, ast.Name)
    }


def test_the_router_includes_the_archiving_routes():
    """写好了没接线 = 测试全绿而功能整条不通(`verify_delivery` 那条同源)。"""
    text = API_ROUTER.read_text(encoding="utf-8")
    assert "spus" in text and "include_router(spus.router)" in text


def test_size_templates_are_served_by_the_backend():
    """前端内置一份的话,加一个模板要改两个仓库,而漏改的那侧不会报错。"""
    text = SPU_API.read_text(encoding="utf-8")
    assert "size-templates" in text


def _declared_routes() -> list[str]:
    """按源码顺序列出这个路由文件声明的路径。"""
    paths: list[str] = []
    for node in ast.walk(_tree(SPU_API)):
        if not isinstance(node, ast.FunctionDef):
            continue
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr in {"get", "post", "patch", "delete", "put"}
                and deco.args
                and isinstance(deco.args[0], ast.Constant)
            ):
                paths.append(str(deco.args[0].value))
    return paths


def test_the_static_route_is_declared_before_the_dynamic_one():
    """`/spus/size-templates` 排在 `/spus/{spu_id}` 后面的话,FastAPI 会先匹配到
    动态段,拿 "size-templates" 去 parse UUID —— 一个静态端点变成 422。

    按**声明顺序**判,不是按文本里第一次出现的位置:模块文档字符串里就写着
    这两个路径,于是文本版的守卫比较的是两句注释,改哪边都不会红。
    """
    routes = _declared_routes()
    assert "/size-templates" in routes, f"没有静态路由,当前:{routes}"
    static_at = routes.index("/size-templates")
    dynamic = [i for i, path in enumerate(routes) if "{" in path]
    assert dynamic, "没有动态路由了?那这条守卫该跟着删"
    assert static_at < min(dynamic), (
        f"静态路由排在动态路由后面了:{routes}"
    )


def test_both_new_models_are_registered_for_autogenerate():
    """没登记的模型 Alembic 看不见,而"看不见"不会报错,只会少建一张表。"""
    text = MODELS_INIT.read_text(encoding="utf-8")
    assert "from app.models.spu import ColorVariant, Spu" in text
    assert '"Spu"' in text and '"ColorVariant"' in text


# ================================================================ 迁移


def test_the_migration_follows_the_current_head():
    text = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0035"' in text
    assert 'down_revision: Union[str, None] = "0034"' in text


def test_the_downgrade_undoes_everything_the_upgrade_did():
    """P0-2 要求 `0001 → head → 回退 → head` 走一遍。

    升级里建了、降级里没撤的东西,只跑 upgrade 的机器上永远看不出来 ——
    看出来的那一刻是回退失败,而那通常发生在需要紧急回退的时候。
    """
    made: dict[str, set[str]] = {"table": set(), "index": set(), "column": set()}
    dropped: dict[str, set[str]] = {"table": set(), "index": set(), "column": set()}

    def first_arg(node: ast.Call) -> str | None:
        if node.args and isinstance(node.args[0], ast.Constant):
            return str(node.args[0].value)
        return None

    for fn_name, bucket in (("upgrade", made), ("downgrade", dropped)):
        fn = _func(MIGRATION, fn_name)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            op_name, arg = node.func.attr, first_arg(node)
            if arg is None:
                continue
            if op_name in ("create_table", "drop_table"):
                bucket["table"].add(arg)
            elif op_name in ("create_index", "drop_index"):
                bucket["index"].add(arg)
            elif op_name in ("add_column", "drop_column"):
                # add_column("products", sa.Column("spu_id", ...)) 的第二个实参才是列名
                if op_name == "add_column":
                    col = node.args[1] if len(node.args) > 1 else None
                    name = (
                        col.args[0].value
                        if isinstance(col, ast.Call)
                        and col.args
                        and isinstance(col.args[0], ast.Constant)
                        else None
                    )
                    if name:
                        bucket["column"].add(f"{arg}.{name}")
                else:
                    second = node.args[1] if len(node.args) > 1 else None
                    if isinstance(second, ast.Constant):
                        bucket["column"].add(f"{arg}.{second.value}")

    for kind in made:
        missing = sorted(made[kind] - dropped[kind])
        assert not missing, f"升级建了但降级没撤的 {kind}:{missing}"


def test_the_variant_size_uniqueness_exists_in_both_model_and_migration():
    """约束只写在模型上,库里就没有;只写在迁移里,autogenerate 下次会想把它删掉。"""
    assert "uq_products_variant_size" in PRODUCT_MODEL.read_text(encoding="utf-8")
    assert "uq_products_variant_size" in MIGRATION.read_text(encoding="utf-8")


def _foreign_key_policy(name: str) -> str | None:
    """迁移里某条外键的 ondelete。按 AST 取,不做窗口搜索。

    第一版是"在约束名前后 400 字符里找 RESTRICT",而**相邻那条外键**也是
    RESTRICT —— 把这一条改成 CASCADE,守卫仍然绿。
    """
    for node in ast.walk(_tree(MIGRATION)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "create_foreign_key":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != name:
            continue
        for kw in node.keywords:
            if kw.arg == "ondelete" and isinstance(kw.value, ast.Constant):
                return str(kw.value.value)
        return ""
    return None


def test_the_sku_rows_are_protected_from_a_color_delete():
    """删一个颜色不该连带删掉它下面九行 SKU —— 那些行上挂着七个域的东西。

    CASCADE 的意思是"删一个颜色,顺手把它下面九行 SKU 连同素材、生成任务、
    候选图、图片集、文案、草稿、发布记录一起删掉",而发出这个动作的人
    在界面上看到的只是一个颜色。
    """
    assert (
        _foreign_key_policy("fk_products_color_variant_id_color_variants") == "RESTRICT"
    )
    assert _foreign_key_policy("fk_products_spu_id_spus") == "RESTRICT"
