"""A45-batch23(阶段 5 批次 5-4):颜色结构化字段的确认流与投影。

## 这一批还的是一笔躲了几批的账,而它躲过去的方式是**一个不存在的字段名**

`ColorVariant.display_name` 的列注释从落列那天起就写着「唯一写入点是属性
服务在 `standard_color_name` 被确认时」。`audit_column_writers.LEDGER` 上
记着它,还款日阶段 5,理由是「要等 owner_id 切 UUID」。

两句都有问题:

    前置    owner_id 切 UUID 在**迁移 0046** 就做完了。
            `validation.owner_for()` 今天对 VARIANT 层字段强制要求
            owner 是 `color_variants.id`,拿不到当场抛错
    字段名  **全仓没有 `standard_color_name`。** grep 只命中那几句话本身。
            注册表里的 VARIANT 层颜色字段是 `primary_color`

第二条才是这笔账真正的挡箭牌:照那句话去接线的人会先找那个字段、找不到,
然后多半得出"还缺前置"的结论,于是这一批又躲过去一次。

## 守卫分三段

    投影口径   None 与空串不是一回事(后者会擦掉界面上正在显示的颜色名)
    触发口径   只有 VARIANT 层的那一个字段触发,而且它真的在注册表里
    接线       调用点在事实写入边界 `set_value` 里(同一个事务、两条
               确认路径共用一跳,batch24 挪的),列的写入点只有一个
"""
from __future__ import annotations

import ast

from app.attributes import colour_projection as cp
from app.attributes.registry import REGISTRY
from app.core.enums import OwnerType
from tests.pure._helpers import BACKEND_ROOT

LAYER = BACKEND_ROOT / "app" / "attributes" / "colour_projection.py"
SERVICE = BACKEND_ROOT / "app" / "attributes" / "service.py"
MODEL = BACKEND_ROOT / "app" / "models" / "spu.py"
LEDGER = BACKEND_ROOT / "tools" / "audit_column_writers.py"
#: 前端那两页也写着同一句话。**守卫原来只扫四个后端路径**,于是
#: A52 走查时在 `.tsx` 里发现了两处新写的 `standard_color_name` ——
#: 那个幽灵字段名躲过这条守卫的方式,是搬到了它看不见的目录里。
FRONTEND = [
    BACKEND_ROOT.parent / "frontend" / "src" / "pages" / "SpuCreatePage.tsx",
    BACKEND_ROOT.parent / "frontend" / "src" / "pages" / "SpuDetailPage.tsx",
]


# ---------------------------------------------------------------- 一、投影口径


def test_a_blank_fact_does_not_wipe_the_displayed_name():
    """**本文件的第一条。** 空值判「不写」,不是「写空串」。

    `str(value or "")` 一行就能"跑通",而它在值为空时会**静默清空**
    一个正在被七个前端位置显示的字段 —— 运营看到的是所有颜色名同时消失,
    而他刚才做的只是确认了一条别的属性。
    """
    for empty in (None, "", "   ", {}, {"value": ""}, [], 0):
        assert cp.projected_name(empty) is None, f"{empty!r} 被投影成了一个值"


def test_both_stored_shapes_are_understood():
    """裸字符串与 `{"value": ...}` 两种形状都认。

    两种都认不是纵容:投影是**读取方**,它不该成为第三个决定"事实值长什么样"
    的地方。认不出的形状返回 None(不写),而不是把 `{'value': 'black'}`
    原样显示给运营 —— 那个形状在本批开发时真实出现过。
    """
    assert cp.projected_name("Black") == "Black"
    assert cp.projected_name({"value": "Black"}) == "Black"
    assert cp.projected_name("  Black  ") == "Black"
    assert cp.projected_name({"value": {"nested": 1}}) is None
    assert cp.projected_name(["Black"]) is None


def test_the_projection_cap_matches_both_the_column_and_the_validator():
    """三个 128 必须是同一个数:列宽、校验层上限、投影上限。

    **今天投影的截断分支走不到** —— `validation.MAX_TEXT_LENGTH` 也是 128,
    超长的值在 `confirm()` 里就被拒了。这一条守的不是截断本身,是那条不变量:

        校验层放宽到 256 而投影没跟上 → 投影静默截断 → 界面上的颜色名
        与事实值不是同一个字符串,而没有任何东西会报错

    真库那一侧因此断言的是**校验层拒绝**,不是"投影截断了" ——
    两处各验自己真正负责的那一半。
    """
    from app.attributes.validation import MAX_TEXT_LENGTH

    assert cp.MAX_LENGTH == MAX_TEXT_LENGTH == 128, (
        f"投影上限 {cp.MAX_LENGTH} 与校验层上限 {MAX_TEXT_LENGTH} 不一致 —— "
        "宽的那一侧会让另一侧静默截断"
    )
    assert "String(128)" in MODEL.read_text(encoding="utf-8"), (
        "`ColorVariant.display_name` 的列宽变了,而上面两个数没跟着变"
    )
    assert cp.projected_name("x" * 500) == "x" * cp.MAX_LENGTH


def test_an_unchanged_name_is_not_rewritten():
    """值没变就不写。

    每次确认都赋值一次的话,`color_variants` 这一行每次都进 UPDATE、
    `updated_at` 跟着动,而「这个颜色最近改过什么」在审计里就看不出来了。
    """
    assert cp.needs_update("Black", "Black") is False
    assert cp.needs_update("", "Black") is True
    assert cp.needs_update("Blue", "Black") is True
    assert cp.needs_update("Black", None) is False, "None 是「不写」,不是「清空」"


# ---------------------------------------------------------------- 二、触发口径


def test_the_source_field_actually_exists_and_is_variant_scoped():
    """触发字段真的在注册表里,而且真的是 VARIANT 层。

    **这一条是本批的核心。** 那笔账躲了几批,靠的正是一个不存在的字段名
    (`standard_color_name`)。把字段名定死在常量里还不够 —— 常量也可以
    写错;所以这里去注册表里查它。

    注册表把颜色挪到 SPU 层的那天,这条会当场变红,而不是让投影安静地
    再也不触发(那时 `display_name` 会重新变回恒空,而没有任何东西报错)。
    """
    spec = REGISTRY.get(cp.SOURCE_FIELD)
    assert spec is not None, (
        f"`{cp.SOURCE_FIELD}` 不在注册表里 —— "
        "这正是 `standard_color_name` 那句话让这笔账躲了几批的方式"
    )
    assert spec.owner_type is OwnerType.VARIANT, (
        f"`{cp.SOURCE_FIELD}` 的 owner 是 {spec.owner_type},不是 VARIANT;"
        "owner_id 不再是 color_variants.id,投影会写到一个不存在的行上"
    )


def test_the_non_existent_field_name_is_gone_from_live_code():
    """`standard_color_name` 不许再作为**触发条件**出现在活代码里。

    引述并驳斥是允许的(本仓的既有写法):那几处现在都写着「不是它,
    是 `primary_color`」。判据因此是**同一行里有没有驳斥**,
    而不是"这个词一次都不许出现" —— 后者会逼人删掉真话。
    """
    for path in (MODEL, SERVICE, LAYER, LEDGER, *FRONTEND):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "standard_color_name" not in line:
                continue
            assert "不是" in line or "没有这个字段" in line or "primary_color" in line, (
                f"{path.name} 里有一句没驳斥的 `standard_color_name`:{line.strip()}"
            )


def test_a_list_valued_colour_field_never_triggers_the_projection():
    """只有那一个字段触发,判据不是"字段名里有 color"。

    按名字模糊匹配的话 `secondary_colors` 也会写进来,而那是一个列表,
    投影出来是一串逗号显示在颜色名的位置上。
    """
    assert cp.SOURCE_FIELD != "secondary_colors"
    body = _code_of(_confirm_projection_fn())
    assert "colour_projection.SOURCE_FIELD" in body, (
        "触发判据不是那个常量 —— 按名字模糊匹配会让 secondary_colors 也写进来"
    )
    assert "secondary_colors" not in body


# ---------------------------------------------------------------- 三、接线


def _named_fn(name: str) -> ast.FunctionDef:
    return next(
        n
        for n in ast.walk(ast.parse(SERVICE.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == name
    )


def _code_of(fn: ast.FunctionDef) -> str:
    """函数体的源码,**去掉 docstring**。

    不去掉的话解释性文字会喂真断言 —— 本仓的同型第九次
    (§3.26 / 14-13 / 14-22 / batch18 C2 / batch19 caller / batch20 B2 /
    batch21 的 docstring / batch22 的 coverage 扫描)。
    这一批里它咬了两次:`_project_colour_name` 的 docstring 里写着
    「这里不 commit」和「不是"字段名里有 color"」,两句都被断言当成了代码。
    """
    statements = list(fn.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    return "\n".join(ast.unparse(stmt) for stmt in statements)


def _confirm_projection_fn() -> ast.FunctionDef:
    return _named_fn("_project_colour_name")


def test_the_projection_runs_inside_the_fact_write_boundary():
    """投影调用点在 `set_value`(事实的唯一写入边界)里,且**不自己提交**。

    batch23 把它挂在 `confirm()` 上 —— 只覆盖人工确认。而 CONFIRMED 不只
    由 confirm 产生:`apply_evidence` 走 `decide_status` 的自动确认档写出的
    同样是 CONFIRMED,那条路当时不投影(§3.54 / F-1)。挂在写入边界上,
    「所有产生 CONFIRMED 颜色事实的路径都触发投影」才是结构保证,
    不再依赖每个调用方记得调一次。

    同时钉住**只有这一处调用**:confirm / apply_evidence 各自再挂一份的话,
    下一次挪动边界时会只剩其中一份被挪 —— 两条路又分叉回 batch23 之前。
    """
    set_value_body = _code_of(_named_fn("set_value"))
    assert "_project_colour_name" in set_value_body, (
        "`set_value` 没有调用投影 —— 自动确认出的颜色 `display_name` 会恒为空"
    )
    assert "AttributeStatus.CONFIRMED" in set_value_body, (
        "投影没有按 CONFIRMED 状态触发 —— SUGGESTED/CANDIDATE 也会写显示名"
    )
    for path_owner in ("confirm", "apply_evidence"):
        assert "_project_colour_name" not in _code_of(_named_fn(path_owner)), (
            f"`{path_owner}` 里又出现了一份投影调用 —— 调用点必须只在写入边界一处"
        )

    body = _code_of(_confirm_projection_fn())
    assert "commit" not in body, (
        "投影自己提交了 —— 它必须跟着 set_value 所在的事务走"
    )


def test_the_projection_is_the_only_writer_of_the_column():
    """全仓只有这一处写 `display_name`(那句列注释的字面要求)。

    判据是 **AST 的赋值目标**,不是文本:注释里到处写着这个列名,
    按文本扫会被它们喂真(同型第九次)。
    """
    hits: list[str] = []
    for path in (BACKEND_ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "display_name":
                    hits.append(path.name)
    assert hits == ["service.py"], (
        f"`display_name` 的写入点变成了 {sorted(set(hits))} —— "
        "它是投影列,多一个写入点就有两个互相矛盾的颜色名"
    )


def test_a_missing_colour_row_does_not_roll_back_the_confirmation():
    """颜色行查不到时静默跳过,不抛错。

    属性写入本身**已经成功了**;为了一列显示副本把它回滚,表现是
    运营点确认收到 500,而事实其实是可以写的。
    """
    body = _code_of(_confirm_projection_fn())
    assert "raise" not in body, (
        "投影里有 raise —— 一列显示副本不该让事实写入失败"
    )
    assert "return" in body


def test_the_ledger_row_is_repaid():
    """`audit_column_writers.LEDGER` 里不再有这一条。

    台账只记**还没有写入路径**的列。留着一条已经还清的账,下一个读它的人
    会以为缺口还开着,然后重做一遍已经做完的事(§3.42 的反方向)。
    """
    src = LEDGER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # `LEDGER: dict[str, str] = {...}` 是 **AnnAssign**(带类型注解),不是
    # `Assign`。第一版只找后者,`next()` 当场 StopIteration —— 那种失败
    # 看起来像"守卫写错了",而它其实是在说"这张表的声明形状变了"。两种都收。
    ledger = next(
        node.value
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", "") == "LEDGER"
        )
        or (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "LEDGER" for t in node.targets)
        )
    )
    keys = {
        k.value
        for k in ledger.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }
    assert "ColorVariant.display_name" not in keys, (
        "台账上还留着这一条,而写入点已经接上了"
    )
