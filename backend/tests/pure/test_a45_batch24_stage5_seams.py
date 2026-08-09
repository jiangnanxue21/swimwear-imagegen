"""A45-batch24(阶段 5 三处接缝):投影的触发边界、文案的颜色粒度、预览与导出同源。

## 三笔账的共同形状:**上游算对了,而最后一跳只接了其中一条路**

§3.43 点过这一类,§3.53 又点了三次。这一批的三条是同一族的第十到第十二次,
但形状比前面几次更隐蔽 —— 前面几次是"没人接",这三条是**接了一半**:

    投影   接在 `confirm()` 上 —— 人工确认那条路。而 CONFIRMED 不只由它产生
    幂等   键接了颜色(经事实版本集) —— 而存储没接,于是键与槽位不同粒度
    图片   预览接了已存映射 —— 而最终导出仍在自己挑图,两套规则各自都合理

接了一半比完全没接更难发现:每一条都有真库用例覆盖着**接上的那一半**,
于是套件全绿、文档写"已验证",而另一半在生产里持续出错。所以本文件的
守卫一律写成**全称形式**(所有路径 / 所有颜色 / 只有一个来源),
而不是"这条路对不对"。

## 三节

    一   投影挂在事实的写入边界上,不挂在某一条调用路径上
    二   文案的键、存储、批量去重三处的作用域粒度必须一致
    三   行级图片只能从已存映射翻译 —— 重新推断要变成做不到的事
"""
from __future__ import annotations

import ast
import inspect

from app.channels import generic
from app.workbench import batch as batch_rules
from app.workflows import copy_idempotency as ci
from tests.pure._helpers import BACKEND_ROOT

ATTR_SERVICE = BACKEND_ROOT / "app" / "attributes" / "service.py"
WB_SERVICE = BACKEND_ROOT / "app" / "workbench" / "service.py"
COPY_SERVICE = BACKEND_ROOT / "app" / "listings" / "copy_service.py"
CHANNEL = BACKEND_ROOT / "app" / "channels" / "generic" / "__init__.py"
MIGRATION = BACKEND_ROOT / "migrations" / "versions" / "0051_copy_colour_dimension.py"
MODEL = BACKEND_ROOT / "app" / "models" / "listing_copy.py"


def _fn(path, name: str) -> ast.FunctionDef:
    return next(
        n
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == name
    )


def _body(fn: ast.FunctionDef) -> str:
    """函数体源码,**去掉 docstring**。

    不去掉的话解释性文字会喂真断言 —— 本仓同型第十次。这一批里它咬过一次:
    `set_value` 的 docstring 里写着 `_project_colour_name`,而当时那句话
    描述的是"应该",不是"已经"。
    """
    statements = list(fn.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    return "\n".join(ast.unparse(s) for s in statements)


# ======================================================== 一、投影的触发边界


def test_every_path_that_writes_a_confirmed_fact_goes_through_one_writer():
    """**本节的前提**:`set_value` 真的是唯一写入口。

    投影挂在写入边界上才有意义,而"写入边界只有一个"这件事在 batch24 之前
    只是 `set_value` docstring 里的一句自述。这里把它变成断言:
    全仓构造 `ProductAttributeValue(...)` 的地方只有那一个函数。

    多一处构造点的话,那条路写出来的 CONFIRMED 颜色事实又不投影 ——
    正是 F-1 换个位置重演。
    """
    tree = ast.parse(ATTR_SERVICE.read_text(encoding="utf-8"))
    owners: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and getattr(inner.func, "id", "") == "ProductAttributeValue"
            ):
                owners.append(node.name)
    assert owners == ["set_value"], (
        f"`ProductAttributeValue` 的构造点变成了 {sorted(set(owners))} —— "
        "写入边界不止一个,挂在边界上的投影就漏了那几条路"
    )


def test_the_colour_projection_hangs_on_the_write_boundary_not_on_a_caller():
    """投影调用点在 `set_value` 里,按 CONFIRMED 触发,且**只有这一处**。

    batch23 把它挂在 `confirm()` 上,于是 `apply_evidence` 经 `decide_status`
    自动确认写出的颜色事实不投影 —— `display_name` 恒空,而后端 5 处读、
    前端 7 处显示,没有一处会报错(§3.54 一节)。
    """
    src = ATTR_SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    callers = sorted(
        {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            for inner in ast.walk(node)
            if isinstance(inner, ast.Call)
            and getattr(inner.func, "id", "") == "_project_colour_name"
        }
    )
    assert callers == ["set_value"], (
        f"投影的调用点是 {callers} —— 它必须只挂在写入边界上。"
        "挂在某几条调用路径上的话,漏掉的那条会安静地写出空颜色名"
    )
    # 状态门按 **AST 结构**验,不按文本。
    #
    # 文本版(`"AttributeStatus.CONFIRMED" in body`)验不出来:`set_value`
    # 里另有两处 `confirmed_by=... if status is AttributeStatus.CONFIRMED`,
    # 于是把状态门整个改成 `if status is not None:` 之后那句断言仍然绿。
    # 这是本仓第十三次"守卫看着像在验、其实在验别的东西",
    # 而这一次是变异脚本先发现的(B2 验不红)。
    gate = None
    for node in ast.walk(_fn(ATTR_SERVICE, "set_value")):
        if not isinstance(node, ast.If):
            continue
        called = any(
            isinstance(inner, ast.Call)
            and getattr(inner.func, "id", "") == "_project_colour_name"
            for inner in ast.walk(node)
        )
        if called:
            gate = node
            break
    assert gate is not None, "投影不在任何条件分支里 —— 状态门没了"
    assert "AttributeStatus.CONFIRMED" in ast.unparse(gate.test), (
        f"投影的状态门是 `{ast.unparse(gate.test)}` —— 它必须只在 CONFIRMED 时触发,"
        "否则 SUGGESTED 的模型猜测会被写进正式颜色名"
    )



def test_the_automatic_confirmation_path_reaches_the_same_boundary():
    """自动确认那条路真的经过 `set_value`,而不是自己写行。

    这条与上一条合起来才是全称命题:边界唯一 + 自动确认走边界
    = 自动确认必然投影。少了这一条,`apply_evidence` 哪天改成自己构造
    ORM 行,上一条仍然绿。
    """
    body = _body(_fn(ATTR_SERVICE, "apply_evidence"))
    assert "set_value(" in body, "`apply_evidence` 不再经过 `set_value` 写值"
    assert "decide_status(" in body, (
        "`apply_evidence` 不再走决策表 —— 自动确认这一档的来源没了"
    )


# ======================================================== 二、文案的颜色粒度


def test_the_key_the_slot_and_the_dedupe_agree_on_the_scope():
    """键、存储槽、批量去重键三处的粒度必须是同一个。

    F-6 的形状是**键比槽细**:键含颜色(经事实版本集),槽只到 SPU,
    于是多颜色 SPU 每次点生成都判"不命中"并付一次费,两个颜色的文案
    互相把对方挤成历史版本。

    反方向同样有代价:批量去重键比槽粗的话(`ACTION_SCOPE` 仍是 `spu`),
    三颜色 SPU 的批量生成只给第一个颜色出稿,另外两个记成"已跳过" ——
    界面读起来完全正常,而那两个颜色到导出时才发现没有文案。

    **两侧都不报错,所以这三处写在一条断言里。**
    """
    assert "color_variant_id" in set(ci.CopyUnit.__dataclass_fields__), (
        "幂等单元丢了颜色轴"
    )
    model = MODEL.read_text(encoding="utf-8")
    assert '"spu", "channel", "site", "locale", "color_variant_id", "version",' in model, (
        "`listing_copies` 的唯一键没带颜色 —— 键按颜色分而槽不分,"
        "每个颜色的第一次生成都会把上一个颜色的当前版挤成历史"
    )
    assert batch_rules.ACTION_SCOPE[batch_rules.BatchAction.GENERATE_COPY] == "spu_colour", (
        "批量去重仍按 SPU —— 三颜色 SPU 只会出一稿文案,另外两个被跳过"
    )
    assert "colour:" in _body(_fn(BACKEND_ROOT / "app" / "workbench" / "batch.py", "scope_key")), (
        "`scope_key` 没有把颜色算进去,`ACTION_SCOPE` 的那一档形同虚设"
    )


def test_the_production_entry_actually_passes_a_colour():
    """生产接线真的传值,不是留一个恒为 None 的形参。

    这个形参在 batch21 就存在了,注释写着「今天恒为 None,要等 5-4」——
    5-4 交付之后没有人回来接。**留位置本身不是接线**,而恒为 None 的形参
    看起来和接好了一模一样:键仍然稳定、复用仍然工作。
    14-22 为同一件事付过一次账(列在、判定读着、没有写入路径)。
    """
    body = _body(_fn(WB_SERVICE, "_copy_unit"))
    assert "color_variant_id=variants.variant_id_for(product)" in body, (
        "`_copy_unit` 没有把商品的颜色身份传进幂等单元"
    )
    assert "color_variant_id=None" not in body

    for name in ("generate_copy", "save_copy_manual"):
        assert "color_variant_id=colour" in _body(_fn(WB_SERVICE, name)), (
            f"`{name}` 落库时没带颜色 —— 那一版会写进哨兵槽,"
            "下次按颜色查永远查不到它,于是每次都重新生成"
        )


def test_the_colour_is_an_equality_filter_with_no_fallback():
    """按颜色查是等值过滤,**不回落**到哨兵或别的颜色。

    回落的表现是:红色还没有文案时,界面把 0051 之前那版(可能写着别的
    颜色的标题)当成红色的当前文案显示,而运营看不出它不是自己这个颜色的。
    宁可多生成一次 —— 与 0050 对 NULL 键判 GENERATE 同一个方向。
    """
    for name in ("_current_copy",):
        body = _body(_fn(WB_SERVICE, name))
        assert "ListingCopy.color_variant_id == color_variant_id" in body, (
            f"`{name}` 没有按颜色过滤"
        )
    approved = _body(_fn(COPY_SERVICE, "latest_approved"))
    assert "ListingCopy.color_variant_id == color_variant_id" in approved
    for banned in ('== ""', "in (color_variant_id", "or_("):
        assert banned not in approved, f"`latest_approved` 里出现了回落形状:{banned}"


def test_the_version_sequence_is_partitioned_by_colour():
    """版本号按 (scope, 颜色) 分槽。

    不分的话两个颜色共用一条递增序列:红色第 1 版、蓝色第 2 版、
    红色第 3 版 —— 数字仍然唯一,而"这个颜色改到第几版了"再也答不出来,
    `_current_copy` 取 max 也会在两个颜色之间摆动。
    """
    body = _body(_fn(COPY_SERVICE, "_next_version"))
    assert "ListingCopy.color_variant_id == color_variant_id" in body
    save = _body(_fn(COPY_SERVICE, "save_copy"))
    assert "color_variant_id=color_variant_id" in save, "落行时没有写颜色列"
    # `ast.unparse` 会把双引号归一成单引号,所以这里按归一后的形状比
    assert "'listing_copy', plan.spu, channel, site, locale, color_variant_id" in save, (
        "串行锁的键没带颜色 —— 两个颜色的生成会互相排队,而它们不相干"
    )


def test_the_migration_keeps_the_sentinel_out_of_null():
    """0051 的新列不可空、默认哨兵 `""`,而不是 NULL。

    这一列进唯一键,而 PostgreSQL 把 NULL 当成互不相等 —— 用 NULL 的话
    版本号的唯一性会在存量槽上失效,同一个 (scope, NULL) 能插进两条
    version=1。哨兵没有这个性质。
    """
    src = MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert 'revision: str = "0051"' in src and 'down_revision: str | None = "0050"' in src
    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app" not in imported, "迁移 import 了 app.*"

    upgrade = _body(_fn(MIGRATION, "upgrade"))
    assert "nullable=False" in upgrade, "新列可空 —— NULL 在唯一键里互不相等"
    assert "server_default=''" in upgrade, "新列没有哨兵默认值,存量行会是 NULL"

    assert "create_unique_constraint" in upgrade
    downgrade = _body(_fn(MIGRATION, "downgrade"))
    assert "drop_column" in downgrade and "create_unique_constraint" in downgrade, (
        "降级没有把唯一约束还原 —— 降回去之后那张表少一条约束,而没人会发现"
    )


# ======================================================== 三、预览与导出同源


def test_the_mapper_cannot_re_infer_the_row_images():
    """`map_fields` 的 `image_map` 是**必需**关键字参数。

    给默认值的写法(不传就照旧自己挑)会让这次修复只在记得传的调用点上
    生效,漏传的地方继续静默走老路 —— 表现和修复前一模一样。
    与 `export_preview.image_preview` 的签名里只有 `mapping` 同一手法:
    把"重新推断"变成一件**做不到**的事,而不是一件不该做的事。
    """
    params = inspect.signature(generic.map_fields).parameters
    assert "image_map" in params, "`map_fields` 不再接受已存映射"
    assert params["image_map"].default is inspect.Parameter.empty, (
        "`image_map` 有默认值 —— 漏传的调用点会安静地回到自己挑图那条路"
    )
    assert params["image_map"].kind is inspect.Parameter.KEYWORD_ONLY


def test_the_row_bucket_reads_only_the_stored_map():
    """行级图片桶只翻译映射里点名的素材,不自己挑、不回落。

    原来它按 `sort_order` 取第一张、取不到回落到 SPU 通用图,而映射按
    §6.5 挑(显式 `is_primary`、通用图只进附图位、缺图就是缺图)。
    两套规则各自都合理,合在一起的后果是一份合法且 READY 的图片集里
    预览显示 A 图、Excel 用 B 图,**而两边都不报错**。
    """
    body = _body(_fn(CHANNEL, "_row_image_bucket"))
    assert "image_map" in body and "skus" in body, "行级桶没有读映射"
    for banned in ("sort_order", "variant_id is None", "is_primary"):
        assert banned not in body, (
            f"行级桶里又出现了自己挑图的痕迹({banned}) —— "
            "挑图规则只能有一个来源,否则预览与导出必然再次分叉"
        )


def test_the_draft_builds_the_map_before_it_maps_the_fields():
    """一次计算,两个消费者。

    `build_draft` 必须先算出 `color_sku_image_map`,再把**同一个对象**
    传给 `map_fields`,最后两者一起落库(一列给预览、`mapped_payload`
    给导出)。顺序反过来的话映射还没算出来,于是又要给映射层留一个
    "没有映射时自己挑"的后门 —— 那正是这次拆掉的东西。
    """
    fn = _fn(WB_SERVICE, "build_draft")
    body = _body(fn)
    build_at = body.index("upstream_snapshot.build_color_sku_image_map")
    map_at = body.index("generic.map_fields")
    assert build_at < map_at, (
        "`map_fields` 排在映射构建之前 —— 它拿不到映射,只能自己挑图"
    )
    assert "image_map=color_sku_image_map" in body, (
        "传给映射层的不是刚算出来的那一份 —— 两个消费者又各读各的了"
    )
    assert "row.color_sku_image_map = color_sku_image_map" in body, (
        "落库的不是同一份 —— 预览读到的与导出用的会是两次不同的计算"
    )


def test_the_preview_and_the_export_name_the_same_column():
    """预览与导出的追溯链最终指向同一列。

    预览读 `row.color_sku_image_map`,导出读 `row.mapped_payload`,
    而后者由 `map_fields(image_map=同一份)` 算出。这条断言钉的是
    **预览没有绕过那一列去重新推断** —— 它只有 `mapping` 一个入参,
    但调用点仍可能递一份现算的映射进去。
    """
    body = _body(_fn(WB_SERVICE, "draft_preview"))
    assert "export_preview.image_preview(row.color_sku_image_map)" in body, (
        "预览读的不是落库的那一列"
    )
    for banned in ("build_color_sku_image_map", "upstream_collect", "coverage("):
        assert banned not in body, (
            f"预览里出现了 {banned} —— 重新推断的预览永远自洽,"
            "而导出用的是草稿生成那一刻存下来的那些"
        )
