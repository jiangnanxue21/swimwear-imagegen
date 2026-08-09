"""A45-batch29(阶段 6 批次 6-4):七步向导 + 刷新恢复 + AC-05 服务端门禁。

## 五节

    零   `StepResult` 的位置参数事故 —— 本批开工第一件事就撞上了它
    一   向导判定不重算任何一步(它只翻译与定位)
    二   AC-01:每一步都进得去,而进不去时说得出是谁挡着
    三   AC-05:绕过前置直接调后面的动作,服务端按同一份判定拒绝
    四   接线三跳:判定 -> 端点 -> 出参(前端那一跳在 Vitest)

## 第零节为什么排在最前面

6-4 要读每一步的 `issues` 来回答"是谁挡着",而读的时候发现 `SETUP` 与
`PLAN` 两步的 issues 恒为空 —— batch27 把它们塞进了 `summary`
(第三个位置参数)。三个后果同时发生,而两批之内没有任何测试看见:

    出参里 `summary` 是对象数组,前端类型写的是 string
    `blocking_count` 漏数那两步的 BLOCKING 问题
    按 issue 取因由的判定(包括本批的 `gate_reason`)全部落空

它是**接线时被顶出来的**,与 batch28 那次(`_plan`/`_copy` 判 BLOCKED)
同型:判定层自己的穷举全绿,错的是它与出参合起来的样子。
"""
from __future__ import annotations

import ast

from app.workbench import wizard
from app.workbench.color_flow import ColorView, evaluate_colors
from app.workbench.flow import (
    ACTION_STEP,
    STEP_ORDER,
    AttributeFacts,
    CopyFacts,
    FlowStep,
    ImageSetFacts,
    Issue,
    IssueLevel,
    MaterialFacts,
    NextActionCode,
    PlanFacts,
    ProductFlow,
    SetupFacts,
    StepResult,
    StepState,
    evaluate,
)
from tests.pure._helpers import BACKEND_ROOT, expect_raises

WIZARD = BACKEND_ROOT / "app" / "workbench" / "wizard.py"
API = BACKEND_ROOT / "app" / "api" / "workbench.py"


def _ready_material() -> MaterialFacts:
    return MaterialFacts(
        total=3,
        usable=3,
        gate_roles=frozenset({"PRODUCT_FRONT"}),
        usable_roles=frozenset({"PRODUCT_FRONT", "PRODUCT_BACK", "DETAIL"}),
    )


def _flow(**kwargs) -> ProductFlow:
    base = {
        "product_id": "p",
        "sku": "SKU-1",
        "spu": "SPU-1",
        "setup": SetupFacts(has_spu_ref=True, has_colour_ref=True),
        "material": _ready_material(),
        "attribute": AttributeFacts(required_fields=(), extraction_count=1),
        "plan": PlanFacts(has_active_plan=True, plan_is_colour_level=True, has_model=True),
    }
    base.update(kwargs)
    return ProductFlow(**base)


# ======================================================== 零、位置参数事故


def test_a_step_summary_is_always_a_string():
    """七步的 `summary` **在每一种组合下**都是字符串。

    这一条是本批第一条守卫,因为它是本批第一件撞上的事。写成"跑一遍
    若干种输入然后逐步断言类型",而不是只测出问题的那两步 —— 缺陷本身
    就是"没有任何测试读那两步的 summary",只补那两步等于把窗口切窄到
    刚好盖住已知的那一处。
    """
    cases = [
        ProductFlow(product_id="p", sku="s", spu="S"),  # 全空:每一步都在最差档
        _flow(),
        _flow(setup=SetupFacts(has_spu_ref=False, has_colour_ref=True)),
        _flow(setup=SetupFacts()),
        _flow(plan=PlanFacts(has_active_plan=False)),
        _flow(plan=PlanFacts(has_active_plan=True, has_model=False)),
        _flow(plan=PlanFacts()),
        _flow(
            attribute=AttributeFacts(required_fields=("fabric",), extraction_count=0),
        ),
        _flow(
            image_set=ImageSetFacts(exists=True, status="APPROVED", version=2),
            copy=CopyFacts(exists=True, status="APPROVED", version=1),
        ),
    ]
    for flow in cases:
        for step in evaluate(flow).steps:
            assert isinstance(step.summary, str), (
                f"{step.step} 的 summary 是 {type(step.summary).__name__} —— "
                "第三个位置参数是 summary,issues 是第四个。"
                "出参里它会变成一个对象数组,而前端类型写的是 string"
            )


def test_the_constructor_refuses_the_swapped_positional_arguments():
    """构造期就拦住那一行,不只靠上面那条穷举。

    上面那条挡的是今天这七步;这一条挡的是**下一个人写第八步**时手滑的
    同一行 —— 它不需要那个人恰好也写一条测试。
    """
    issue = Issue(
        level=IssueLevel.BLOCKING,
        code="X",
        message="m",
        target_step=FlowStep.SETUP,
    )
    err = expect_raises(
        TypeError,
        StepResult,
        FlowStep.SETUP,
        StepState.NEEDS_CONFIRM,
        (issue,),
    )
    assert "summary" in str(err)


def test_the_blocking_count_now_sees_the_plan_step():
    """没配方案的商品**数得出那一条阻断**。

    这是上面那个事故最贵的后果:`blocking_count` 数的是 `result.issues`,
    而 PLAN 的 BLOCKING 问题从来没进去过 —— 列表页显示"阻断 0",
    运营按阻断数排序找今天要做的商品,这一件永远排在最后。
    """
    result = evaluate(_flow(plan=PlanFacts(has_active_plan=False)))
    codes = [i.code for i in result.issues]
    assert "PLAN_MISSING" in codes, f"PLAN 的阻断问题还是没进 issues:{codes}"
    assert result.blocking_count >= 1


def test_waiting_for_upstream_still_carries_no_issue():
    """"等上游"仍然不产出 issue —— 修位置参数没有把这条规矩一起改掉。

    `StepState` 的文档写着:除素材步以外,BLOCKED 一律指上游未就绪,
    且不携带任何 issue。`_evaluate_plan` 原来带着一条
    `PLAN_UPSTREAM_NOT_READY`,而它从没被人看见过(它在 summary 里)。
    修的时候顺手把它按规矩收掉了 —— 这条守卫钉住"收掉了",
    否则下一次有人"顺手补回来",一件刚建档的商品会显示"阻断 2",
    而运营能做的只有一件事。
    """
    result = evaluate(
        ProductFlow(
            product_id="p",
            sku="s",
            spu="S",
            setup=SetupFacts(has_spu_ref=True, has_colour_ref=True),
            material=_ready_material(),
            attribute=AttributeFacts(required_fields=("fabric",), extraction_count=0),
        )
    )
    plan = next(s for s in result.steps if s.step is FlowStep.PLAN)
    assert plan.state is StepState.BLOCKED
    assert plan.issues == (), f"等上游的 PLAN 步产出了 issue:{plan.issues}"


# ======================================================== 一、不重算


def test_the_wizard_layer_imports_only_the_two_decision_modules():
    """向导层只 import `flow` 与 `color_flow`。

    它多 import 一个 service / 模型 / sqlalchemy,就意味着它开始自己查东西,
    而它存在的全部理由是"翻译已经算好的结果"。
    """
    tree = ast.parse(WIZARD.read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    allowed = {
        "__future__",
        "collections.abc",
        "dataclasses",
        "app.workbench.flow",
        "app.workbench.color_flow",
    }
    assert modules <= allowed, f"向导层多 import 了:{sorted(modules - allowed)}"


def test_the_wizard_does_not_re_derive_any_step_state():
    """向导不自己判任何一步的状态。

    判据:模块里不出现对 `facts` 的判断(它拿不到 facts),也不出现
    第二张"哪一步依赖哪一步"的表 —— 依赖关系已经由 `flow.evaluate()`
    的调用顺序表达过一次了。
    """
    src = WIZARD.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 先剥 docstring:本仓同型事故第十五次的形状是"守卫被自己的注释喂饱"
    body = "\n".join(
        ast.unparse(node)
        for node in tree.body
        if not (
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        )
    )
    for forbidden in ("upstream_ready", "material_ready", "has_active_plan"):
        assert forbidden not in body, (
            f"向导层出现了 `{forbidden}` —— 它在重算 `flow` 已经算过的东西"
        )


def test_the_action_table_is_not_copied():
    """动作 -> 步骤读 `flow.ACTION_STEP`,不另抄一张。

    抄一张的表现:新增一个动作码之后向导上那一步少一个按钮,
    而 `ACTION_STEP` 是全的,所以没有任何测试会红。
    """
    covered = {code for step in STEP_ORDER for code in wizard.actions_of(step)}
    assert covered == set(ACTION_STEP), (
        f"动作码没有全部归到步骤上:缺 {sorted(set(ACTION_STEP) - covered)}"
    )


def test_every_step_has_an_intro():
    """七步穷举,不多不少。

    缺一条的表现是向导上某一步只有名字没有说明,而第一次用的人恰恰
    卡在那一步 —— 与 batch26 给 `STEP_LABELS` 立的那条同型。
    """
    assert set(wizard.STEP_INTRO) == set(STEP_ORDER)
    assert all(wizard.STEP_INTRO[s].strip() for s in STEP_ORDER)


# ======================================================== 二、AC-01


def test_the_first_step_is_open_even_when_there_is_nothing_at_all():
    """一张图都没有时,向导的**素材步仍然进得去**。

    这是 `SELF_BLOCKING_STEPS` 存在的全部理由。一律按 `not BLOCKED` 判,
    运营打开一件刚建档的商品会看到七步全灰 —— 而他要做的第一件事
    (传图)恰恰在那个灰掉的格子里。
    """
    view = wizard.build(evaluate(ProductFlow(product_id="p", sku="s", spu="S")))
    material = next(s for s in view.steps if s.step is FlowStep.MATERIAL)
    assert material.state is StepState.BLOCKED
    assert material.is_open, "素材步被判成了「还没轮到」"
    assert material.gate_reason == "", "进得去的步骤不该带一句常驻的挡路理由"


def test_a_closed_step_names_the_step_that_is_blocking_it():
    """进不去的步骤说得出是**哪一步**挡着,而不是一句"等上一步"。

    "等上一步"说了等于没说:运营要知道等的是哪一步、那一步差什么,
    否则他会在向导上逐格点过去找。
    """
    view = wizard.build(
        evaluate(
            ProductFlow(
                product_id="p",
                sku="s",
                spu="S",
                setup=SetupFacts(has_spu_ref=True, has_colour_ref=True),
                material=_ready_material(),
                attribute=AttributeFacts(
                    required_fields=("fabric",), extraction_count=0
                ),
            )
        )
    )
    image_set = next(s for s in view.steps if s.step is FlowStep.IMAGE_SET)
    assert not image_set.is_open
    assert "属性" in image_set.gate_reason, (
        f"挡路理由没有点名是哪一步:{image_set.gate_reason!r}"
    )


def test_an_open_step_offers_at_least_one_action():
    """能进的步骤至少给得出一个动作码。

    给不出的话向导上那一屏是空的 —— AC-01 要求"不离开向导完成主流程",
    而一屏没有任何可点的东西就是在把人往别处赶。
    """
    view = wizard.build(evaluate(_flow()))
    for step in view.steps:
        if step.is_open:
            assert step.actions, f"{step.step} 能进却没有任何动作"


def test_the_focus_colour_comes_from_the_colour_layer():
    """"先补哪个颜色"取自 `color_flow.colors_needing`。

    那个函数的文档写的就是「向导的『先补哪个』按它排」,而在本批之前
    它**零调用点** —— 又一次"算好了没人读"。
    """
    rollup = evaluate_colors(
        [
            ColorView(
                variant_id="v2",
                variant_code="BLU",
                sellable_status="ACTIVE",
                is_active=True,
                sample_count=0,
            ),
            ColorView(
                variant_id="v1",
                variant_code="RED",
                sellable_status="ACTIVE",
                is_active=True,
                sample_count=3,
                confirmed_fields=frozenset(),
            ),
        ]
    )
    assert wizard.focus_color(rollup, FlowStep.MATERIAL) == "BLU"
    # 没有颜色维时是 None,而不是一个凭空的第一个颜色
    assert wizard.focus_color(None, FlowStep.MATERIAL) is None


def test_the_colour_axis_never_overwrites_the_step_state():
    """颜色维**不下压**商品级步骤状态。

    下压会造出第三种步骤状态(向导一份、总览页一份、列表页一份),
    而 AC-15 的原话是"三处不得出现互相矛盾的状态"。所以颜色维单独一列。
    """
    result = evaluate(_flow())
    rollup = evaluate_colors(
        [
            ColorView(
                variant_id="v1",
                variant_code="RED",
                sellable_status="ACTIVE",
                is_active=True,
                sample_count=0,  # 这个颜色缺样品
            )
        ]
    )
    view = wizard.build(result, rollup=rollup)
    for step in view.steps:
        assert step.state is result.step_state(step.step), (
            f"{step.step} 的状态被颜色维改写了 —— 向导与总览页会显示两个答案"
        )


# ======================================================== 三、AC-05


def test_bypassing_the_prerequisite_is_refused_by_the_same_judgement():
    """属性没确认时,直接请求"生成文案"被拒,理由与向导上那句**一模一样**。

    AC-05 的最后一句是「判定层与前端读同一个结果」。两句话不同的表现是
    界面说"等属性确认",409 说"还没有任何已确认的属性" —— 运营会以为
    这是两个问题,然后去找第二个不存在的开关。
    """
    result = evaluate(
        ProductFlow(
            product_id="p",
            sku="s",
            spu="S",
            setup=SetupFacts(has_spu_ref=True, has_colour_ref=True),
            material=_ready_material(),
            attribute=AttributeFacts(required_fields=("fabric",), extraction_count=0),
        )
    )
    verdict = wizard.check_action(result, NextActionCode.GENERATE_COPY)
    assert not verdict.allowed
    assert verdict.reason == wizard.gate_reason(result, FlowStep.COPY)
    assert verdict.reason, "拒绝了却说不出原因"


def test_done_is_not_an_executable_action():
    """`DONE` 请求过来一律拒绝。

    它是"没有待办"的占位码。当成允许的话,调用方会拿它当一张万能通行证
    —— 而它归属的步骤是 DRAFT,也就是"导出"那一格。
    """
    assert not wizard.check_action(evaluate(_flow()), NextActionCode.DONE).allowed


def test_every_action_code_gets_a_verdict():
    """每一个动作码都判得出允许与否,没有一个落在判定之外。

    落在外面的表现不是报错,是那个按钮在向导上永远不出现 ——
    而不出现的按钮没有人会去报缺陷。
    """
    result = evaluate(_flow())
    for code in NextActionCode:
        verdict = wizard.check_action(result, code)
        assert verdict.action is code
        assert verdict.allowed or verdict.reason, f"{code} 拒绝了却没有理由"


def test_the_http_boundary_enforces_the_gate_on_the_forward_actions():
    """本文件这几个写端点都调了那道闸。

    只写判定不接线的话,AC-05 就退回成"前端置灰" —— 而 AC-05 的原话
    正是"而不是仅前端置灰"。

    ## 这一条**不再声称自己是全部**(F-2)

    上一版它只点名三个函数,而当时全仓恰好只有那三个调用点 —— 于是这条
    断言读起来像"HTTP 边界覆盖住了",实际证明的只是"这三个过了"。
    `image_sets:approve` / `attributes:extract_attributes` 等五个前进端点
    当时一个都没过闸,而这条测试永远不会红。

    「过闸的是不是全部」现在由 `tests/test_a45_batch31_action_gate.py` 按
    `GATED_ENDPOINTS` / `UNGATED_ENDPOINTS` 两张表对**全部写路由**穷举。
    这里留下的是本文件那几个的定点断言 —— 它便宜,而且失败时指得更准。
    """
    tree = ast.parse(API.read_text(encoding="utf-8"))
    functions = {
        node.name: ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    for name, action in (
        ("generate_copy", "GENERATE_COPY"),
        ("approve_copy", "APPROVE_COPY"),
        ("build_draft", "BUILD_DRAFT"),
        ("export_draft", "EXPORT"),
    ):
        assert name in functions, f"端点 {name} 不见了"
        assert "action_gate.ensure_allowed(" in functions[name], (
            f"{name} 没有过 AC-05 那道闸"
        )
        assert action in functions[name], f"{name} 过的闸不是 {action}"


def test_the_gate_is_shared_and_not_re_implemented_per_router():
    """闸只有一份实现,四个 router 调的是同一个函数。

    每个 router 各写一份的表现是它们慢慢分叉:一个用 `dry_run=True`,
    另一个忘了,于是同一件商品在两个端点上得到两种判定。

    实现本身与"它读不读判定层"的断言在
    `tests/test_a45_batch31_action_gate.py` —— 闸搬了家,守卫跟着搬。
    """
    src = API.read_text(encoding="utf-8")
    assert "from app.api import action_gate" in src
    assert "def _ensure_action_allowed(" not in src, (
        "本文件又写了一份私有闸 —— 那正是 F-2 修掉的形状"
    )


def test_no_endpoint_accepts_a_completion_flag_from_the_client():
    """**没有任何请求体接受"这一步已完成"这类字段**(AC-05 前半句)。

    接受一个的表现最难查:前端传 `completed: true`,后端老实照做,
    而流程判定还在说这一步没做完 —— 两个答案里运营会信按钮那个。
    """
    tree = ast.parse(API.read_text(encoding="utf-8"))
    banned = {"completed", "completion", "is_done", "done", "step_state", "current_step"}
    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(
            isinstance(base, ast.Name) and base.id == "BaseModel" for base in node.bases
        ):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id in banned:
                    problems.append(f"{node.name}.{stmt.target.id}")
    assert not problems, f"请求体里出现了前端传入的完成标记:{problems}"


# ======================================================== 四、接线


def test_the_wizard_reaches_the_http_response():
    """向导块真的从 HTTP 出得去,而且**没有新开端点**。

    §3.43 那一族十四次的形状都是"少最后一跳"。第二句是 AC-15:
    新开 `/wizard` 就是给同一份判定造第二个来源。
    """
    src = API.read_text(encoding="utf-8")
    assert "wizard_block = _wizard_block(" in src, "详情端点没有组装向导块"
    assert '"wizard": wizard_block' in src, "详情端点没有下发向导块"
    assert '/products/{product_id}/wizard"' not in src, (
        "开了第二个端点 —— 同一份判定会有两个来源"
    )


def test_the_wizard_and_the_colour_axis_share_one_read():
    """颜色维与费用预估读**同一次取数**。

    各取一次的表现是向导上写着「BLU 缺 3 个角度」,而下面的预估按 2 张
    算钱 —— 两个数并排显示且不相等,两边都不报错。
    """
    src = API.read_text(encoding="utf-8")
    assert "views, rollup = _color_axis(" in src
    assert "color_rollup.views_and_rollup(" in src


def test_the_serialised_wizard_has_everything_the_front_end_needs():
    """出参形状:七步 + 当前步 + 允许动作 + 阻塞清单 + 费用位。

    少一个键的表现是前端那一段静默不渲染(可选链一路 undefined),
    而 batch24 刚为同型付过账。
    """
    payload = wizard.serialize(wizard.build(evaluate(_flow())))
    assert set(payload) == {
        "current_step",
        "focus_color",
        "allowed_actions",
        "blocking",
        "steps",
        "cost",
    }
    assert len(payload["steps"]) == len(STEP_ORDER)
    first = payload["steps"][0]
    assert set(first) == {
        "step",
        "label",
        "intro",
        "state",
        "is_open",
        "gate_reason",
        "is_current",
        "actions",
        "pending_colors",
        "has_color_axis",
    }
    # `cost=None` 的含义是「没算」,不是「不要钱」
    assert payload["cost"] is None
