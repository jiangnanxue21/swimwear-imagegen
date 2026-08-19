"""a47 §5 守卫:生成方案必须**真正控制出图**,不是只在验收时成立。

## 这一批修的是什么

阶段 4 落下 `GenerationPlan` 之后,系统的实际行为是:

    运营配方案:要求 正面 / 30° / 背面
            ↓
    出图:按弹窗里手填的参数跑,方案的角度没人告诉生成器
            ↓
    验收:按方案检查(`upstream_collect` 读 `gp.required_angles`)—— 缺背面
            ↓
    运营看到"图片集不完整",而他并没有做错任何事

六个方案参数里只有 `budget_cap` 生效;`scene` / `pose` / `angles_json`
在建任务与执行链路里**一次都没有被读过**。这是本轮唯一一处业务正确性问题。

## 守卫分三段,因为这一条有三种断法

    一  判定层    参数怎么合并、冲突怎么表述 —— 可穷举,放纯函数
    二  接线      `create_task` 真的读了 `plan_row` 而不是入参(AST)
    三  身份      `override_plan` 在 handler 里被显式判过(AST)

**四条等式与 override 分支归真库用例**(`tests/test_a47_plan_governs_db.py`),
这里验不了 —— 那份文件必须存在,本文件末尾钉着它。
"""
from __future__ import annotations

import ast

from app.workflows import generation_plan as gp
from tests.pure._helpers import BACKEND_ROOT

SERVICE = BACKEND_ROOT / "app" / "services" / "generation_service.py"
ENDPOINT = BACKEND_ROOT / "app" / "api" / "generation_tasks.py"


def _func(path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name
    )


def _plan(**kwargs) -> gp.PlanView:
    base = dict(
        plan_id="p-1",
        provider="fashn",
        model_template_id="m-1",
        scene="海边",
        pose="站姿",
        angles=gp.normalize_angles({"FRONT": 2, "BACK": 1}),
    )
    base.update(kwargs)
    return gp.PlanView(**base)  # type: ignore[arg-type]


# ================================================ 一、判定层(穷举)


def test_the_governed_field_list_exists_in_exactly_one_place():
    """清单只有一份。

    各调用点手抄一份的表现是:审计说「按方案执行」,而某个字段其实没换 ——
    两句话都出自同一次请求,而它们互相矛盾。
    """
    assert gp.PLAN_GOVERNED_FIELDS == ("provider", "model_template_id")


def test_a_request_that_differs_from_the_plan_is_reported_not_swallowed():
    """§5.2 硬约束 3:冲突不静默。"""
    conflicts = gp.plan_overrides(
        _plan(), {"provider": "mock", "model_template_id": "m-2"}
    )

    assert [c.field for c in conflicts] == ["provider", "model_template_id"]
    assert conflicts[0].as_payload() == {
        "field": "provider",
        "requested": "mock",
        "plan": "fashn",
        # **恒为 plan。**这个键存在的意义就是让审计里那句话有主语:
        # "请求 mock,方案 fashn,按 fashn 执行"
        "applied": "plan",
    }


def test_a_request_that_agrees_with_the_plan_produces_no_noise():
    assert gp.plan_overrides(_plan(), {"provider": "fashn", "model_template_id": "m-1"}) == ()


def test_not_asking_for_anything_is_not_a_conflict():
    """**没传 ≠ 要求它是空的。**

    把没传算成冲突会让每一次正常的建任务都在审计里留下两条噪音,
    而真正的冲突会淹没在里面 —— 一条留痕一旦开始产生噪音就等于没有。
    """
    assert gp.plan_overrides(_plan(), {"provider": None, "model_template_id": ""}) == ()


def test_the_uuid_and_its_string_form_are_the_same_value():
    """调用点两侧一边是 ORM 列、一边是接口入参,形状本来就不同。"""
    assert gp.plan_overrides(_plan(model_template_id="m-1"), {"model_template_id": "m-1"}) == ()


def test_the_round_size_comes_from_the_angles_not_from_the_caller():
    """§5.2:`angles_json` 决定角度集合与张数。"""
    assert gp.governed_candidate_count(_plan(), fallback=4) == 3


def test_a_plan_without_angles_falls_back_instead_of_asking_for_zero_images():
    """没有角度是**方案本身的问题**,由 `plan_problems` 报。

    在这里再判一次会把一份坏方案的表现从"报错"变成"提交了一个不出图的
    任务" —— 后者要到运营等了十分钟之后才被发现。
    """
    assert gp.governed_candidate_count(_plan(angles=()), fallback=4) == 4
    assert any(p.code == gp.PLAN_NO_ANGLES for p in gp.plan_problems(_plan(angles=())))


def test_scene_pose_and_angles_reach_the_provider_through_the_prompt():
    """`GenerationTask` 没有这三列,而本轮禁止加列(PRD §2)。

    更要紧的是:出图 Provider 的请求里也**没有**"角度"这个参数 ——
    对一个试穿 / 文生图接口来说,"要正面和背面"只能经由提示词表达。
    """
    composed = gp.compose_prompt(
        "影棚灯光", scene="海边", pose="站姿", angles=_plan().angles
    )

    assert composed is not None
    assert composed.startswith("影棚灯光"), "运营写的那句话必须排在最前"
    assert "自然海滩日光场景" in composed
    assert "自然直立" in composed
    assert "FRONT×2" in composed and "BACK×1" in composed


def test_empty_plan_fields_do_not_leave_dangling_labels():
    """空标签会被模型当成一个真实约束去满足,而它背后什么都没有。"""
    composed = gp.compose_prompt("影棚灯光", scene="", pose=None, angles=())

    assert composed == "影棚灯光"
    assert "场景" not in composed and "姿势" not in composed


def test_nothing_at_all_stays_none_instead_of_becoming_an_empty_string():
    """None 是"没写",空串是"写了个空的"。`GenerationTask.prompt` 分得出。"""
    assert gp.compose_prompt(None) is None
    assert gp.compose_prompt(None, scene="", pose="") is None


def test_the_angle_order_follows_the_enum_not_the_input():
    """提示词进幂等键。按输入序排的话,换个键序就换一个任务。"""
    one = gp.compose_prompt(None, angles=gp.normalize_angles({"BACK": 1, "FRONT": 1}))
    two = gp.compose_prompt(None, angles=gp.normalize_angles({"FRONT": 1, "BACK": 1}))

    assert one == two


# ================================================ 二、接线:参数取自方案而非入参


def test_create_task_resolves_the_plan_before_it_checks_the_model_template():
    """顺序是硬的。

    方案会换掉 `model_template_id`,而 `_assert_assets_are_usable` 拿它去过
    §11 的授权闸。排在后面的表现是**按调用方的模板过闸、按方案的模板出图**
    —— 一道合规检查在产品里静默失效,而两侧都不报错。
    """
    source = SERVICE.read_text(encoding="utf-8")
    resolve_at = source.index("generation_plan_service.resolve_for")
    assert_at = source.index("_assert_assets_are_usable(\n        session, assets")

    assert resolve_at < assert_at, (
        "方案解析被排到了素材/模板检查后面 —— 授权闸会按一个不会被使用的"
        "模板放行"
    )


def test_the_provider_that_reaches_the_task_comes_from_the_plan():
    """**这一条是 §5 的落点。**

    改之前:`chosen = resolve(requested=<调用方传的 provider>)`,方案解析出来
    只用了 `budget_cap`。守卫钉的是"`provider` 这个名字在传给 `resolve` 之前
    被方案赋过值",而不是某一行的字面量 —— 后者换个变量名就失锚。
    """
    fn = _func(SERVICE, "create_task")
    assigned_from_plan = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", "") == "provider" for t in node.targets)
        and "plan_view" in ast.dump(node.value)
    ]

    assert assigned_from_plan, (
        "`provider` 不是从 `plan_view` 取的 —— 方案里的 Provider 又变回了"
        "一个算出来但没人读的字段"
    )


def test_the_model_template_and_round_size_come_from_the_plan_too():
    fn = _func(SERVICE, "create_task")
    dumped = {
        target.id: ast.dump(node.value)
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert "plan_view" in dumped.get("model_template_id", "")
    assert "governed_candidate_count" in dumped.get("candidate_count", "")
    assert "compose_prompt" in dumped.get("prompt", "")


def test_both_plan_columns_and_the_key_hang_off_one_answer():
    """`plan_applied` 是唯一答案。

    分成两个判断的写法出现过,而 override 一来它们就会分叉:参数按调用方、
    列却记着方案 —— 于是"这个任务按方案跑了没有"有两个互相矛盾的答案。
    """
    fn = _func(SERVICE, "create_task")
    source = ast.get_source_segment(SERVICE.read_text(encoding="utf-8"), fn) or ""

    assert source.count("plan_applied") >= 5
    assert "plan_row.plan_fingerprint if plan_row else None" not in source, (
        "还有一处按 `plan_row` 判、而不是按 `plan_applied` 判"
    )


def test_the_budget_is_still_checked_when_the_plan_is_bypassed():
    """预算是花钱的闸,不是出图参数。

    绕过方案不等于绕过预算 —— 否则 `override_plan` 会成为超预算出图的后门,
    而它恰恰只有管理员能用,查起来最没人怀疑。

    ## 判据从"位置"换成"条件"(2026-08-11 评审)

    旧版断言的是 `_assert_budget(...)` 出现在 `plan_applied = ` **之前**,
    理由是"挪到后面会让 override 顺带绕过预算"。那是一个**代理判据**:
    它真正要守的不变量是「这道闸按 `plan_row` 判,不按 `plan_applied` 判」。

    这一轮预算闸必须算上"本次预估",而预估要 `provider` 与 `candidate_count`
    ——两者都在 `plan_applied` 那一段之后才定下来。于是位置必须往后挪,
    而不变量原样成立(条件仍是 `plan_row is not None`)。

    所以这里直接钉那个条件:找到包着 `_assert_budget` 的那个 `if`,
    要求它的判据提到 `plan_row` 且**不提** `plan_applied`。
    代理判据换成真判据之后,这条断言不再随一次正当重排而假红。
    """
    fn = _func(SERVICE, "create_task")

    guards = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.If)
        and any(
            isinstance(call.func, ast.Name) and call.func.id == "_assert_budget"
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )
    ]
    assert guards, "`create_task` 里找不到被 if 包着的 `_assert_budget` 调用"
    # 取最内层那个:`ast.walk` 会把外层 if 一起收进来
    condition = ast.unparse(min(guards, key=lambda n: n.end_lineno - n.lineno).test)

    assert "plan_row" in condition, (
        f"预算闸的判据不是 `plan_row`(现在是 `{condition}`)"
    )
    assert "plan_applied" not in condition, (
        f"预算闸改成按 `plan_applied` 判了(`{condition}`) —— override_plan "
        "会因此顺带绕过预算,而它只有管理员能用,查起来最没人怀疑"
    )


# ================================================ 三、override_plan 的身份判定


def test_the_endpoint_checks_the_identity_itself():
    """端点挂的是 `require_operator`,没有"require_admin 的调用点"这回事。

    v2.0 的 PRD 写的正是那句话,而它在这个仓库里不成立 —— 于是按它执行的人
    会以为已经有一道门,而实际上运营也能传这个开关。
    """
    fn = _func(ENDPOINT, "_assert_may_override_plan")
    source = ast.get_source_segment(ENDPOINT.read_text(encoding="utf-8"), fn) or ""

    assert "is_admin" in source
    assert "AUTH_FORBIDDEN" in source and "403" in source


def test_the_check_runs_before_anything_is_created():
    create = _func(ENDPOINT, "create_task")
    calls = [
        node.func.id
        for node in ast.walk(create)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert "_assert_may_override_plan" in calls
    # 只跳过文档字符串。写成"跳过所有 Expr"会把这次调用本身跳掉 ——
    # 它就是一个 `ast.Expr`,于是断言看的是下一句,恒失败(本文件第一版的错)
    body = list(create.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]

    assert "_assert_may_override_plan" in ast.dump(body[0]), (
        "身份判定不是这个 handler 的第一件事 —— 排在建任务后面等于先花钱再问"
    )


def test_a_silent_ignore_is_not_an_option():
    """静默忽略会让调试的人以为自己绕过去了。

    他拿到 201、拿到一个正常的任务,然后对着一组"我明明传了别的参数"的
    候选图查半天,而真正发生的是系统按方案跑了。
    **一个没有生效的开关比没有这个开关更贵。**
    """
    source = ENDPOINT.read_text(encoding="utf-8")
    fn = _func(ENDPOINT, "_assert_may_override_plan")
    body = ast.get_source_segment(source, fn) or ""

    assert "raise" in body, "非管理员那一支没有抛错 —— 那就是静默忽略"


# ================================================ 四、本批验不到什么


def test_the_four_equalities_live_in_a_db_suite_that_exists():
    """**这条守卫记的是边界,不是成绩。**

    §5.5 的四条等式(provider / model_template / 角度集合 / 指纹非空)与
    override 分支要一行真库数据才验得到。这台机器上没有 PostgreSQL,
    所以那份文件**写了、一次都没跑过** —— 那是本轮最要紧的欠账。

    这条守卫只保证它存在:删掉它、或者把欠账悄悄清掉时,这里会红。
    """
    db_suite = BACKEND_ROOT / "tests" / "test_a47_plan_governs_db.py"

    assert db_suite.exists(), "§5 的真库用例不见了 —— 那是四条等式唯一的真验证"
    text = db_suite.read_text(encoding="utf-8")
    assert "requires_db" in text
    for anchor in ("override_plan", "plan_fingerprint", "required_angles"):
        assert anchor in text, f"真库用例里没有 {anchor} 那一条"
