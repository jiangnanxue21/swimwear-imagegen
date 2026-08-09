"""A45-batch14-13:§11 第一行,按作用域的失败归集。

## §11 那一行要三件事,只有第三件没落地

    run = PARTIAL_SUCCESS       `terminal_status_for` 已经给了(14-12)
    成功颜色的建议正常入队       合并层与 `queue_policy` 的事(§11 批)
    失败颜色明确列出,可单独重试   **本批**

第三件此前**一个字都不成立**:跑完之后没有任何地方记得哪个颜色全军覆没。
`succeeded_count` / `failed_count` 是两个标量,而「3 张图散着失败」与
「B 色的 3 张全失败」在它们眼里一模一样 —— 前者每个颜色都还有证据,
后者 B 色一条证据都没有,而 §6.3 的完成条件要求每个 ACTIVE 颜色都有
确认过的颜色事实。运营看到的是一件卡住但没有理由的商品。

## 事后反推不出来,所以这件事欠一列

「这个 run 的输入里哪几张没留下证据行」看起来能反推出失败清单。它不能:
`plan_evidence` 在模型只答了目标清单之外的字段时**返回空计划**,于是一张
**调用成功、扣了钱**的图同样一条证据行都没有。两种情况在库里长得一模一样,
而它们一个该重试、一个不该。规格写在
`test_listing_the_failed_scopes_needs_a_column_and_here_is_which_one`。

## 三处穷举

    四张图 × 三种归属 × 成败的全部组合    共 1215 组,逐组比对失败清单
    「全军覆没」判据的边界                 (total, succeeded) 的全部小值组合
    作用域划法与指纹模块的一致性           同一组素材,两个模块给出同一组作用域
"""
from __future__ import annotations

import ast
import itertools
import random

from app.attributes import run_state as rs
from app.attributes import scope_fingerprint as fp
from app.core.enums import ExtractionRunStatus
from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"
S = ExtractionRunStatus
R = rs.ImageResult


def _wired_entry(module: str) -> tuple[str, ...]:
    """从 `verify_delivery.WIRED_MODULES` 里取出某个模块登记的函数名。

    **用 AST 取,不用字符串切。**第一版是
    `block[block.index(module):][:...index("),")]` —— 而登记项的注释里
    有一个 `(归属外键不存在),`,`),` 当场提前命中,后面的名字被切没了。

    后果不是这条守卫红,是它**变松**:正向断言碰巧还在截断范围内,
    而「这几个不许登记」的反向断言被截断喂成了平凡真。
    §3.26 第四节那句话在这里第六次成立 —— 按字符串在源码里找东西,
    找到的从来不保证是你以为的那个位置。
    """
    source = (BACKEND_ROOT / "tools" / "verify_delivery.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
            continue
        if node.target.id != "WIRED_MODULES" or node.value is None:
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=False):
            if isinstance(key, ast.Constant) and key.value == module:
                return tuple(
                    e.value for e in value.elts if isinstance(e, ast.Constant)
                )
    raise AssertionError(f"WIRED_MODULES 里找不到 {module}")


def _fn(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


class _Asset:
    """素材行的最小替身。`color_variant_id` **故意可缺席** —— 那一列是
    阶段 2 的归属外键,今天不存在,而判定必须在它不存在时也答得出来。"""

    def __init__(self, colour=..., hint=None):
        if colour is not ...:
            self.color_variant_id = colour
        if hint is not None:
            self.variant_hint = hint


# ================================================================ 一、归集本体


def _expected_failed(rows) -> tuple[str, ...]:
    """守卫这一侧独立算一遍「哪个颜色一次都没成功」。

    独立重写而不是调被测函数,是这条穷举的全部意义。写法也刻意不同:
    这里先按颜色分组再判,实现是边遍历边判。
    """
    colours = {r.scope for r in rows if r.scope}
    out = []
    for colour in sorted(colours):
        mine = [r for r in rows if r.scope == colour]
        if mine and not any(r.succeeded for r in mine):
            out.append(colour)
    return tuple(out)


def test_the_whole_four_image_space_is_enumerated():
    """四张图 × 三种归属(A / B / 通用)× 成败,逐组比对。

    失败清单是一个全称命题(「**任何**一次 run 里,一次都没成功的颜色
    都要被列出来」),挑样本证不了它。
    """
    placements = (None, "A", "B")
    checked = 0
    for scopes in itertools.product(placements, repeat=4):
        for oks in itertools.product((True, False), repeat=4):
            rows = [
                R(scope=s, succeeded=ok)
                for s, ok in zip(scopes, oks, strict=False)
            ]
            got = rs.outcome_by_scope(rows)
            assert got.failed_scopes == _expected_failed(rows), (
                f"{scopes} / {oks}:算出 {got.failed_scopes},"
                f"应该是 {_expected_failed(rows)}"
            )
            checked += 1
    assert checked == 3**4 * 2**4, checked


def test_the_enumeration_is_not_vacuous():
    """穷举不许平凡通过(batch14-8 / X1 的教训)。

    判定塌成「永远返回空清单」时,上面那条会**照样全绿** —— 因为大多数组合
    的正确答案确实是空。所以这里单独钉住:非空清单、单颜色清单、双颜色清单
    都真的出现过。
    """
    seen = set()
    placements = (None, "A", "B")
    for scopes in itertools.product(placements, repeat=4):
        for oks in itertools.product((True, False), repeat=4):
            rows = [
                R(scope=s, succeeded=ok)
                for s, ok in zip(scopes, oks, strict=False)
            ]
            seen.add(len(rs.outcome_by_scope(rows).failed_scopes))
    assert seen == {0, 1, 2}, f"失败清单的规模只出现过 {sorted(seen)}"


def test_a_colour_with_one_success_is_not_a_failed_scope():
    """**判据是「一次都没成功」,不是「有失败」。**

    写成后者的话,5 张图里失败 1 张的颜色也会进重试清单,而那个颜色
    已经有 4 张图的证据了 —— 重试它是为一个已经答上来的问题再付一次钱,
    而且付的是整个作用域的钱。
    """
    rows = [R("A", True), R("A", False), R("A", False), R("A", False)]
    out = rs.outcome_by_scope(rows)
    assert out.failed_scopes == ()
    assert out.retry_scope is None
    assert out.status is S.PARTIAL_SUCCESS


def test_a_colour_that_lost_every_image_is_listed():
    rows = [R("A", True), R("A", True), R("B", False), R("B", False)]
    out = rs.outcome_by_scope(rows)
    assert out.failed_scopes == ("B",)
    assert out.status is S.PARTIAL_SUCCESS, "§11 明写这种 run 是 PARTIAL_SUCCESS"


def test_the_lost_everything_boundary_is_enumerated():
    """「这个作用域全军覆没了吗」的两个边界逐组钉死。

    这条守卫是变异逼出来的:判定原来长在 `outcome_by_scope` 的循环里,
    而那里的 `total > 0` **没有任何用例够得着**(颜色是从确实有图的行里
    数出来的)。删掉它不会有任何东西变红 —— 而它正是「没有图的颜色
    不算失败」这条规则的载体。提成有名字的判定之后就能穷举了。
    """
    seen = set()
    for total in range(0, 5):
        for succeeded in range(0, total + 1):
            outcome = rs.ScopeOutcome(
                scope="A",
                total=total,
                succeeded=succeeded,
                failed=total - succeeded,
                status=S.FAILED,
            )
            got = rs.scope_lost_everything(outcome)
            assert got is (total > 0 and succeeded == 0), (
                f"total={total} succeeded={succeeded} 判成 {got}"
            )
            seen.add(got)
    assert seen == {True, False}, "判定塌成了一个常量"


def test_a_colour_with_no_images_at_all_is_not_a_failure():
    """一个颜色一张图都没有,不是「失败」—— 那是 §6.2 样品完整度的问题
    (`media/sample_completeness.py`),下一步是去拍照,不是重试识别。

    混进来的后果是运营点「重试这个颜色」,而重试一个没有图的作用域
    只会再得到一次空结果,并且**再花一次钱**。
    """
    out = rs.outcome_by_scope([R("A", True)])
    assert out.failed_scopes == ()
    assert [s.scope for s in out.scopes] == [rs.SHARED, "A"], (
        "没有图的颜色不该出现在成绩单里"
    )


def test_the_failed_list_is_sorted():
    """八个颜色乱序进,清单必须逐次一样。

    不排序的表现是同一次 run 两次读出来的重试范围顺序不同,而重试范围
    进幂等键 —— 顺序一变,同一次重试会算出两个键(1/8! 才会蒙对)。
    """
    colours = [f"c{i}" for i in range(8)]
    baseline = None
    shuffler = random.Random(20260806)
    for _ in range(12):
        order = list(colours)
        shuffler.shuffle(order)
        rows = [R(c, False) for c in order]
        got = rs.outcome_by_scope(rows).failed_scopes
        assert got == tuple(sorted(colours))
        baseline = baseline or got
        assert got == baseline


# ================================================================ 二、重试范围


def test_the_retry_scope_covers_exactly_the_failed_colours():
    rows = [R("A", True), R("B", False), R("C", False)]
    out = rs.outcome_by_scope(rows)
    assert out.retry_scope is not None
    assert out.retry_scope.kind == rs.SCOPE_VARIANTS
    assert out.retry_scope.variant_ids == ("B", "C")


def test_nothing_failed_means_no_retry_scope():
    """没有失败颜色时不许给一个重试范围。

    给了的话,界面上会出现一个「重试」按钮,而它重试的是一批已经成功的图 ——
    再跑一遍、再付一次,结论一个字都不会变。
    """
    assert rs.outcome_by_scope([R("A", True), R(None, True)]).retry_scope is None


def test_the_retry_scope_is_never_all_or_shared():
    """§11 的原话是「可**单独**重试该颜色作用域」。

    退回 ALL 或共享的话,成功的那些颜色跟着再买一次 —— 而那正是把
    「部分失败」这条动线做成单独一档的全部理由。
    """
    for rows in (
        [R("A", True), R("B", False)],
        [R("A", False), R("B", False)],
        [R("A", False), R(None, True)],
    ):
        scope = rs.outcome_by_scope(rows).retry_scope
        assert scope is not None
        assert scope.kind == rs.SCOPE_VARIANTS, f"重试范围退化成 {scope.kind}"
        assert scope.token != rs.canonical_scope(all_variants=True).token
        assert scope.token != rs.canonical_scope().token


def test_the_retry_scope_is_a_real_key_scope():
    """重试范围要能直接拿去建幂等键 —— 否则「单独重试」只是一句话。

    这一条把本批和 14-12 的 §9.2 接上:重试范围比原来那次窄,
    所以它算出来的键必然不同于原 run 的键,不会被判成「同意图」而复用。
    """
    rows = [R("A", True), R("B", False)]
    retry = rs.outcome_by_scope(rows).retry_scope
    fingerprints = {rs.SHARED: "s", "A": "afp", "B": "bfp"}
    key = rs.idempotency_key(
        spu_id="spu-1",
        scope=retry,
        fingerprints=fingerprints,
        model_version="m",
        prompt_version="p",
        requested_fields=["color"],
    )
    whole = rs.idempotency_key(
        spu_id="spu-1",
        scope=rs.canonical_scope(all_variants=True),
        fingerprints=fingerprints,
        model_version="m",
        prompt_version="p",
        requested_fields=["color"],
    )
    assert key != whole, "重试那一次和整批那一次算出了同一个键"


# ================================================================ 三、作用域划法


def test_the_shared_scope_covers_every_image():
    """§6.3:「共享字段跨**全部**证据合并」。

    共享只算未归属的图的话,一个三色 SPU 的共享事实会只看见通用图 ——
    而通用图可能一张都没有,于是共享作用域凭空变成 FAILED。
    """
    rows = [R("A", True), R("B", True), R(None, False)]
    shared = rs.outcome_by_scope(rows).scopes[0]
    assert shared.scope is rs.SHARED
    assert shared.total == 3
    assert shared.succeeded == 2


def test_a_generic_image_belongs_to_no_colour_subset():
    """通用图只进共享。让它进每个颜色的话,「一张通用图失败」会把每个颜色
    都算成有失败 —— D1(作用域过宽)正是这个形状。"""
    rows = [R("A", True), R(None, False)]
    out = rs.outcome_by_scope(rows)
    colour = [s for s in out.scopes if s.scope == "A"][0]
    assert colour.total == 1 and colour.failed == 0
    assert out.failed_scopes == ()


def test_the_shared_scope_never_enters_the_failed_list():
    """共享覆盖全部图,所以它「全军覆没」等价于整个 run 失败 ——
    那时该做的是整体重跑,不是「重试某个作用域」。"""
    out = rs.outcome_by_scope([R(None, False), R(None, False)])
    assert out.status is S.FAILED
    assert out.failed_scopes == ()
    assert out.retry_scope is None


def test_the_two_modules_cut_the_same_scopes():
    """作用域的划法必须和指纹模块**逐个作用域**对得上。

    分叉的表现极隐蔽:一个模块说「这次识别覆盖了 A 和 B」,另一个说
    「指纹只有 A 变了」,而没有任何地方会把两句话放在一起比较。
    """

    class _Ev:
        def __init__(self, ident, colour):
            self.asset_id = ident
            self.content_hash = f"h{ident}"
            self.source = "MANUAL_UPLOAD"
            self.role = "PRODUCT_FRONT"
            self.status = "READY"
            self.color_variant_id = colour

    assets = [_Ev(1, "A"), _Ev(2, "B"), _Ev(3, None)]
    from_fingerprint = set(fp.fingerprints(assets))
    from_outcome = {
        s.scope
        for s in rs.outcome_by_scope(
            [rs.image_result(a, succeeded=True) for a in assets]
        ).scopes
    }
    assert from_outcome == from_fingerprint, (
        f"归集切出 {sorted(map(str, from_outcome))},"
        f"指纹切出 {sorted(map(str, from_fingerprint))}"
    )


# ================================================================ 四、作用域取值


def test_the_scope_comes_from_the_attribution_column_not_the_hint():
    """**读 `color_variant_id`,不读 `variant_hint`。**

    表上今天只有 hint,所以拿它接线是最省事的一条路 —— 而它让模型猜出来的
    值决定重试范围,而重试范围决定下一次付多少钱。§4.8 明写 hint 是
    「识别建议位」,14-9 与 14-10 两次拒绝接线是同一条理由。
    """
    assert rs.scope_of(_Asset(colour=None, hint="RED")) is None, (
        "归属为空时拿 hint 顶上了 —— 模型猜的颜色决定了重试范围"
    )
    assert rs.scope_of(_Asset(colour="real-uuid", hint="RED")) == "real-uuid"
    source = (APP / "attributes" / "run_state.py").read_text(encoding="utf-8")
    body = ast.get_source_segment(source, _fn(ast.parse(source), "scope_of")) or ""
    assert "variant_hint" not in body.split('"""')[-1], "判定体里出现了 variant_hint"


def test_a_missing_attribution_column_does_not_explode():
    """归属外键**今天不存在**,判定必须在它不存在时也答得出来。

    写成属性访问会在列落库前当场 AttributeError;写死 `None` 则会在列落库
    **之后**继续瞎。`getattr` 是唯一两头都对的写法 —— 与
    `evidence_rules.asset_is_extraction_input` 同一处理。
    """
    assert rs.scope_of(_Asset()) is None
    tree = ast.parse((APP / "attributes" / "run_state.py").read_text(encoding="utf-8"))
    fn = _fn(tree, "scope_of")
    names = {
        n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "getattr" in names, "作用域取值没有走 getattr"


def test_blank_attributions_are_normalised_away():
    """空串、空格不是一个真实的颜色 id。归一掉,否则它们会各自变成
    一个「颜色」,而重试范围里会出现一个点不动的东西。"""
    for blank in ("", "   ", None):
        assert rs.scope_of(_Asset(colour=blank)) is None
    out = rs.outcome_by_scope([R("", False), R("  ", False)])
    assert out.failed_scopes == ()


# ================================================================ 五、和整体判定的关系


def test_the_run_status_is_the_shared_scope_and_not_a_second_algorithm():
    """整体状态**就是**共享作用域那一档,不是另起一套。

    另起一套的话,`models/attribute.py` 那个派生属性(按计数)和这里
    会给出两个答案,而它们从来不会被放在一起比较。
    """
    placements = (None, "A", "B")
    for scopes in itertools.product(placements, repeat=3):
        for oks in itertools.product((True, False), repeat=3):
            rows = [
                R(scope=s, succeeded=ok)
                for s, ok in zip(scopes, oks, strict=False)
            ]
            out = rs.outcome_by_scope(rows)
            assert out.status is out.scopes[0].status
            # 与按计数的那条路逐组一致
            assert out.status is rs.terminal_status_for(
                image_count=len(rows),
                succeeded=sum(1 for r in rows if r.succeeded),
                failed=sum(1 for r in rows if not r.succeeded),
            )


def test_an_empty_run_is_failed_not_complete():
    """一张图都没跑的 run 不是成功。与 `terminal_status_for` 同向。"""
    out = rs.outcome_by_scope([])
    assert out.status is S.FAILED
    assert out.scopes == (out.scopes[0],)
    assert out.failed_scopes == ()


# ================================================================ 六、接线


def test_the_loop_records_every_image_with_the_asset_it_came_from():
    """两条分支都要记,而且都要带着 `asset`。

    少记失败那条:任何颜色都不会被算成全军覆没。
    少记成功那条:每个颜色都被算成全军覆没,重试范围变成全部。
    记了但丢掉 `asset`(自己造一个 scope=None 的):作用域信息当场没了,
    而这三种坏法**都不会报错**。
    """
    source = (APP / "attributes" / "service.py").read_text(encoding="utf-8")
    fn = _fn(ast.parse(source), "run_extraction")
    calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "image_result"
    ]
    assert len(calls) == 2, f"逐图成绩的落点有 {len(calls)} 处,应该是成功/失败各一处"
    flags = set()
    for call in calls:
        assert call.args and isinstance(call.args[0], ast.Name), (
            "image_result 的第一个入参不是那张素材本身 —— 作用域信息会丢"
        )
        for kw in call.keywords:
            if kw.arg == "succeeded" and isinstance(kw.value, ast.Constant):
                flags.add(kw.value.value)
    assert flags == {True, False}, f"成败两条分支没有都记:{flags}"
    appended = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "outcomes"
        and node.func.attr == "append"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "image_outcome"
    ]
    assert len(appended) == 2, (
        f"逐图成绩算了两份，却只有 {len(appended)} 份进入归集清单；"
        "成功/失败任一分支漏 append 都会把失败颜色算错"
    )


def test_the_aggregation_is_actually_read():
    """归集算了得有人读,否则它是「算好了没人读」那一类。

    读它的条件必须是 `outcome.failed_scopes` 本身 —— 换成常量之后
    这一整段就是死代码,而两侧的测试全绿(batch14 的 M10 同型)。
    """
    source = (APP / "attributes" / "service.py").read_text(encoding="utf-8")
    fn = _fn(ast.parse(source), "run_extraction")
    guarded = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Attribute)
        and node.test.attr == "failed_scopes"
    ]
    assert guarded, "没有任何一处以 failed_scopes 为条件 —— 归集算了没人读"
    dumped = ast.dump(guarded[0])
    assert "retry_scope" in dumped, (
        "报了失败颜色却没报重试范围 —— 读日志的人得自己拼一次,"
        "拼宽一档就是为已经答上来的颜色再付一次钱"
    )


# ================================================================ 七、持久化


def test_failed_scope_outcomes_are_persisted_for_retry_and_display():
    """逐图结果与失败颜色必须落库，不能从 evidence 缺席反推。

    成功调用也可能因目标字段为空而不写 evidence，所以只有逐图 checkpoint
    能同时回答「跑没跑、成没成、属于哪个作用域」。`failed_scopes` 是同一份
    checkpoint 经 `outcome_by_scope` 归集后的可展示、可精确重试结论。

    `requested_scope` 仍只表示付费前的请求范围，不能拿来装失败范围；否则
    `ALL` 请求里只有 B 色全失败时，下一次仍会把所有颜色重新付费一遍。
    """
    model = (APP / "models" / "attribute.py").read_text(encoding="utf-8")
    tree = ast.parse(model)
    columns: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ProductAttributeExtraction":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    columns.add(stmt.target.id)
    assert {"input_asset_ids", "image_outcomes", "failed_scopes"} <= columns
    # `requested_scope` 在(14-20),但它只能装**请求的**作用域。
    # 归集结果落到它身上的话,重试范围会宽成整批 —— 见上面第三节
    assert "requested_scope" in columns, (
        "`requested_scope` 不见了 —— 幂等键里的作用域那一维会没地方存"
    )
    service = (APP / "attributes" / "service.py").read_text(encoding="utf-8")
    assert "row.image_outcomes = list(persisted_outcomes)" in service
    assert "row.failed_scopes = list(outcome.failed_scopes)" in service
    assert "requested_scope=identity.scope_token" in service, (
        "`requested_scope` 写的不是 `canonical_scope()` 的产物"
    )
    for misuse in ("requested_scope = outcome", "requested_scope=outcome"):
        assert misuse not in service, (
            "归集结果被写进了 `requested_scope` —— 那一列记的是请求的作用域,"
            "不是失败的作用域;混用会让重试范围宽成整批"
        )
    # 两个标量仍保留给聚合指标，但不再承担失败范围的事实来源。
    assert {"succeeded_count", "failed_count"} <= columns


def test_the_wired_half_is_registered_and_the_unwired_half_is_not():
    """接线登记两个方向都钉:少登记一个不会有任何征兆,而少登记正是
    「写好了没人调」最常见的来路。

    ## A45-batch14-20:欠的那三个还上了

    原来这条守卫的反向断言点着 `idempotency_key` / `reuse_verdict` /
    `canonical_scope` —— 理由是「它们今天没有调用点,登记了门禁会红,
    而让它变绿最省事的做法是拿 product_id 凑一个假作用域」。

    迁移 0040 把五列落下去之后那个借口消失,三个全部接线并登记。

    ## A45-batch14-21:`facts_stale` / `changed_scopes` 这一层也还上了

    上一版这条守卫的反向断言点着这两个,理由是「要等属性值行也带上指纹列,
    **那是阶段 4 的事**」。迁移 0042 把
    `product_attribute_values.input_fingerprint` 落下去,两个都接线并登记。

    **这条欠账暴露过一个新的失效方向,值得在这里记一句。** 阶段 4 落码了,
    而这件事没做 —— 守卫没有变红,因为它钉的是"没接线"这个事实,不钉
    "什么时候该接"。还款日只写在这段文档里,到期不还没有任何机制会响。
    §3.37 立的规矩和 `verify_delivery` 那条「欠账守卫都在还款日之内」
    是这件事的补丁:**判据落在别人身上的守卫,守不住自己。**

    反向断言现在点的是那条捷径本身:不许拿 run 行的指纹去填属性值 ——
    两者的作用域根本不同,而它是当初让这条门禁"最省事变绿"的那个做法。
    """
    entry = _wired_entry("app/attributes/run_state.py")
    for wired in (
        "terminal_status_for",
        "run_is_authoritative",
        "outcome_by_scope",
        "canonical_scope",
        "idempotency_key",
        "reuse_verdict",
        "unique_index_predicate",
    ):
        assert wired in entry, f"{wired} 没有被登记成接线"

    fingerprint = _wired_entry("app/attributes/scope_fingerprint.py")
    for wired in ("fingerprints", "views", "facts_stale", "changed_scopes"):
        assert wired in fingerprint, f"{wired} 没有被登记成接线"

    # 反向:那条捷径不许被走。`apply_evidence` 里如果出现
    # `extraction.input_fingerprint`,写进属性值的就是**这次 run 吃进去的
    # 整批素材**的共享指纹 —— 共享事实与各颜色事实会共用同一个答案,
    # 于是给颜色 A 补一张图会 stale 掉 B 色事实。D1 从后门原样回来,
    # 而且不报错:每一条事实都带着一个看起来很像的 64 位哈希。
    service = (APP / "attributes" / "service.py").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(service))
        if isinstance(n, ast.FunctionDef) and n.name == "apply_evidence"
    )
    body = "\n".join(
        ast.unparse(n)
        for n in fn.body
        if not (
            isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        )
    )
    assert "extraction.input_fingerprint" not in body, (
        "事实的指纹取自 run 行 —— 那是「这次 run 吃了哪批素材」,"
        "不是「这条事实按哪批素材立起来」。共享与颜色事实会共用同一个答案,"
        "给 A 色补图会 stale 掉 B 色事实(D1 从后门回来)"
    )
