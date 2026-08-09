"""A45-batch25(阶段 6 批次 6-1):颜色子态判定层。

## 这一批为什么先做判据、再做这一层

阶段 6 的验收是 AC-01/05/14~17,而 §14.1 写着这六条「沿用 v3.0 原文,
当前不可得」。阶段 5 遇到过同一件事,处理方式是按仓内条文**重述并签认**
(AC-10/11/18/19 的「阶段 5 仓内执行版」)。不先做这件事就开工,
就是 §14.1 那句话点名禁止的「对着不可得的判据开发」——
而它的代价刚刚在 batch24 付过一次:5-3 与 5-5 都"交付"了,各差一半。

颜色子态被排在阶段 6 的第一批,理由是**输入已经齐了**:颜色集、
颜色→SKU→图片映射、颜色级事实、按颜色的文案(0051)、按颜色的覆盖率,
batch19~24 全部落地。而七步增维要动 113 处 `FlowStep` 引用并**改掉
完成度口径**(5 步 × 20 → 七步),那是 6-2 单独一批的事。

## 四节

    一   范围口径与 §6.7 / AC-18 同源(只有 ACTIVE 拦路)
    二   「没算过」与「没做」分得开
    三   不重算任何上游口径 —— 汇总,不推断
    四   与 6-2 的接口:颜色级步骤两处必须一致
"""
from __future__ import annotations

import ast
import inspect

from app.workbench import color_flow as cf
from app.workbench.flow import FlowStep, StepState
from tests.pure._helpers import BACKEND_ROOT

MODULE = BACKEND_ROOT / "app" / "workbench" / "color_flow.py"


def _view(**kw) -> cf.ColorView:
    """一个各步都齐的颜色。每条用例只改自己要验的那一项。"""
    base = dict(
        variant_id="v-red",
        variant_code="RED",
        sellable_status="ACTIVE",
        is_active=True,
        skus=("SPU-RED-S",),
        sample_count=3,
        confirmed_fields=frozenset({"primary_color"}),
        missing_required_fields=frozenset(),
        conflicting_fields=frozenset(),
        has_plan=True,
        plan_is_colour_level=True,
        owned_image_count=2,
        missing_angles=frozenset(),
        skus_missing_primary=frozenset(),
        has_copy=True,
        copy_stale=False,
    )
    base.update(kw)
    return cf.ColorView(**base)


# ======================================================== 一、范围口径


def test_a_complete_colour_is_done_on_every_step():
    result = cf.evaluate_color(_view())
    assert [s.state for s in result.steps] == [cf.ColorSubState.DONE] * len(cf.COLOR_STEPS)
    assert result.current_step is None
    assert not result.is_blocking


def test_only_active_colours_block_the_spu():
    """§6.7 / AC-18 的范围口径。**停售的颜色缺图是预期,不是故障。**

    这一条不是显示细节:PLANNED 的颜色按定义就是"样品还没到",
    把它算进阻断的话,任何一个刚建档的颜色都会让整个 SPU 变红,
    而运营对此无能为力 —— 那正是 `variant_coverage` 的文档里
    记着的"那不是修复,是停产"。
    """
    for status, active in (("PLANNED", False), ("PAUSED", False), ("DISABLED", False)):
        view = _view(sellable_status=status, is_active=active, sample_count=0)
        result = cf.evaluate_color(view)
        assert result.by_step[FlowStep.MATERIAL].state is cf.ColorSubState.BLOCKED, (
            "非 ACTIVE 的颜色也要如实显示它缺什么 —— 运营要看得见"
        )
        assert not result.is_blocking, f"{status} 的颜色拦住了整个 SPU"


def test_the_rollup_counts_only_active_colours():
    rollup = cf.evaluate_colors(
        [
            _view(variant_id="v-a", variant_code="AAA"),
            _view(
                variant_id="v-b", variant_code="BBB",
                sellable_status="PLANNED", is_active=False, sample_count=0,
            ),
        ]
    )
    assert rollup.active_count == 1
    assert rollup.blocking_codes == ()
    assert cf.blocking_summary(rollup) == ""
    assert len(rollup.colors) == 2, "非 ACTIVE 的颜色被从结果里删掉了 —— 它要显示,只是不拦路"


def test_the_blocking_list_is_sorted_and_named_by_code():
    rollup = cf.evaluate_colors(
        [
            _view(variant_id="v-z", variant_code="ZED", sample_count=0),
            _view(variant_id="v-a", variant_code="AMB", owned_image_count=0),
        ]
    )
    assert rollup.blocking_codes == ("AMB", "ZED"), "阻断清单没有排序,前端 key 会抖"
    assert "2 个颜色拦着" in cf.blocking_summary(rollup)


def test_colours_come_back_in_a_stable_order():
    """顺序按 `variant_id`,与 `upstream_snapshot.active_colors` 同一条理由。

    这个结果会进聚合 API 的响应。顺序随查询顺序变的话,前端的列表 key
    会在没有任何业务变化时重排 —— 表现是运营正在看的那一行跳走了。
    """
    ids = [c.variant_id for c in cf.evaluate_colors(
        [_view(variant_id="v-c"), _view(variant_id="v-a"), _view(variant_id="v-b")]
    ).colors]
    assert ids == ["v-a", "v-b", "v-c"]


# ======================================================== 二、没算过 ≠ 没做


def test_a_step_that_was_never_computed_is_unknown_not_todo():
    """batch19 已经为这条付过一次学费(`upstream_versions IS NULL`)。

    合成一个取值的表现是:一批建于快照列之前的老草稿,在向导里显示成
    「这些颜色都还没做」—— 而它们做完了,只是没人算过。运营会去重做一遍。
    """
    result = cf.evaluate_color(
        _view(sample_count=None, confirmed_fields=None, has_plan=None,
              owned_image_count=None, skus_missing_primary=None, has_copy=None)
    )
    assert [s.state for s in result.steps] == [cf.ColorSubState.UNKNOWN] * len(
        cf.COLOR_STEPS
    )
    for step in result.steps:
        assert step.detail, f"{step.step} 判了 UNKNOWN 却没说是哪一项没查"


def test_unknown_never_becomes_a_block():
    """没算过的颜色不拦路,也不把 SPU 那一步拉成阻断。

    反方向(判成 BLOCKED)会让升级当天所有老草稿一起变红,
    而它们一条都不是真的坏了。与 0050 / 0051 对存量数据的方向一致:
    宁可少报,不可把「未知」显示成「错」。
    """
    view = _view(sample_count=None, confirmed_fields=None,
                 owned_image_count=None, skus_missing_primary=None, has_copy=None)
    assert not cf.evaluate_color(view).is_blocking
    rollup = cf.evaluate_colors([view])
    assert rollup.blocking_codes == ()
    assert cf.SUBSTATE_TO_STEP_STATE[rollup.worst_by_step[FlowStep.MATERIAL]] is StepState.TODO


def test_missing_facts_block_only_when_the_upstream_cannot_run():
    """缺必需事实时,「还没做」与「做不了」取决于上一步。

    样品都没有的话识别跑不起来,这时候说 TODO 会让运营在属性页干等 ——
    而他要做的事在素材页。
    """
    without_sample = cf.evaluate_color(
        _view(sample_count=0, missing_required_fields=frozenset({"primary_color"}))
    )
    assert without_sample.by_step[FlowStep.ATTRIBUTE].state is cf.ColorSubState.BLOCKED

    with_sample = cf.evaluate_color(
        _view(missing_required_fields=frozenset({"primary_color"}))
    )
    assert with_sample.by_step[FlowStep.ATTRIBUTE].state is cf.ColorSubState.TODO


def test_a_conflict_asks_a_person_instead_of_guessing():
    result = cf.evaluate_color(_view(conflicting_fields=frozenset({"pattern_type"})))
    step = result.by_step[FlowStep.ATTRIBUTE]
    assert step.state is cf.ColorSubState.NEEDS_CONFIRM
    assert "pattern_type" in step.detail, "待决定却不说是哪个字段"


def test_waiting_for_an_earlier_step_is_never_a_block():
    """「还没轮到」是 TODO,不是 BLOCKED —— **三处必须同向**。

    这条是 batch28 接聚合 API 时当场发现的:`_plan` 与 `_copy` 把"上一步
    没就绪"判成 BLOCKED,而 `_image_set` 判 TODO。后果很具体 ——
    一个刚建档的 SPU,**每个颜色都在拦路**,`blocking_codes` 立刻退化成
    噪音,而真正缺图的那个颜色淹在里面。

    BLOCKED 在这一层的含义是"做不了,而且要做的事在**这个颜色**上"。
    `blocking_codes` 是运营的待办清单,它只有在"点进去就有事做"时才有用。
    """
    # 属性没确认 -> 方案、图片、文案都还没轮到
    view = _view(
        confirmed_fields=frozenset(),
        missing_required_fields=frozenset({"primary_color"}),
        has_plan=False,
        owned_image_count=0,
        has_copy=False,
    )
    result = cf.evaluate_color(view)
    for step in (FlowStep.PLAN, FlowStep.COPY):
        assert result.by_step[step].state is cf.ColorSubState.TODO, (
            f"{step} 把「等上一步」判成了阻断 —— 刚建档的 SPU 会每个颜色都拦路"
        )
        assert result.by_step[step].detail, f"{step} 说等上一步却不说等什么"


def test_a_stale_copy_is_not_a_block():

    """过期文案重新生成一次就好,而它要花钱 —— 什么时候点由运营决定。

    判成阻断的表现是:上游事实一变,整个 SPU 立刻不能导出,
    而运营并不总是想为一个措辞改动再付一次费。
    """
    step = cf.evaluate_color(_view(copy_stale=True)).by_step[FlowStep.COPY]
    assert step.state is cf.ColorSubState.TODO
    assert step.detail


# ======================================================== 三、汇总,不重算


def test_the_module_does_not_recompute_any_upstream_rule():
    """本层只汇总已经算好的结果,**一条上游口径都不重算**。

    重算的表现是"向导说齐了、门禁说缺" —— §3.43 那一族,而这一族在
    batch24 刚刚以 AC-19 的形状出现过一次(预览按一套规则挑图、
    导出按另一套)。所以这里直接钉住:本模块不许出现那几个名字。
    """
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    # 标准库不算依赖(`flow.py` 文件头对"零依赖"下过同一个定义:
    # 不碰基础设施,不是不 import 任何东西)。这里钉的是 **app.\* 那一侧**
    app_imports = {m for m in imported if m.split(".")[0] == "app"}
    assert app_imports == {"app.workbench.flow"}, (
        f"颜色子态层多 import 了 {sorted(app_imports - {'app.workbench.flow'})} —— "
        "它只能汇总调用方递进来的结果,自己去查等于给同一条口径造第二个答案"
    )


    # **先剥注释再扫文本。** 不剥的话本模块文件头那句"`coverage()` 算好的"
    # 会喂饱下面的断言 —— 本仓同型第十四次,§3.54 刚记过一次,而它在这里
    # 又当场发生了一遍。凡是读源码的守卫,第一步就该剥 docstring。
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]
    body = "\n".join(
        ast.unparse(n)
        for n in tree.body
        if not isinstance(n, (ast.Import, ast.ImportFrom))
    )
    for banned in ("coverage(", "build_color_sku_image_map", "select(", "session"):
        assert banned not in body, f"本层出现了 {banned} —— 它应该是纯判定"



def test_the_worst_active_colour_decides_the_step():
    """九个颜色里有一个缺图,SPU 的那一步就不该显示 DONE。

    不下压的表现是向导上「图片集 ✅」而点进去有一个颜色是红的 ——
    运营会先信那个绿勾。
    """
    rollup = cf.evaluate_colors(
        [_view(variant_id="v-a", variant_code="AAA"),
         _view(variant_id="v-b", variant_code="BBB", owned_image_count=0)]
    )
    assert rollup.worst_by_step[FlowStep.IMAGE_SET] is cf.ColorSubState.BLOCKED
    assert (
        cf.SUBSTATE_TO_STEP_STATE[rollup.worst_by_step[FlowStep.IMAGE_SET]]
        is StepState.BLOCKED
    )
    assert rollup.worst_by_step[FlowStep.MATERIAL] is cf.ColorSubState.DONE


def test_the_merge_rule_is_written_down_and_has_exactly_one_answer():
    """**F-1 的守卫:合并规则必须唯一。**

    上一版这里有两份规格互相矛盾:`rollup_step_states()` 说"要下压",
    `wizard.worst_color_state()` 说"不下压",而两边都没有生产调用点。
    AC-15 问的"商品级与颜色级冲突时是否有明确、唯一的合并规则",
    当时的答案是"规则写了两份,一份都没接"。

    这条钉住裁定后的状态:那个没人调的翻译函数不许回来,而裁定要
    写在模块文档里能被读到。
    """
    assert not hasattr(cf, "rollup_step_states"), (
        "`rollup_step_states` 回来了 —— 它是那条与 `wizard` 打架的规格。"
        "要下压请改 `flow`,理由见 color_flow 顶部的「合并规则」一节"
    )
    assert "合并规则" in (cf.__doc__ or ""), "裁定没有写在模块文档里"


def test_no_step_is_done_while_carrying_a_blocking_issue():
    """**F-1 的根因守卫:带着阻断问题的一步不许显示 DONE。**

    这一条是跨步骤的结构不变量,所以它穷举 `FlowStep` 而不是点名图片集 ——
    上一版正是"只有图片集那个分支破了例",而判定层自己的穷举全绿。

    走 `flow.evaluate()` 而不是逐个 `_evaluate_*`:不变量落在装配那一步上,
    这条断言要问的是**最终出参**里有没有这种组合。
    """
    from app.workbench.flow import (
        AttributeFacts,
        CopyFacts,
        DraftFacts,
        ImageSetFacts,
        IssueLevel,
        MaterialFacts,
        PlanFacts,
        ProductFlow,
        SetupFacts,
        StepState,
        evaluate,
    )

    def _base(**kwargs) -> ProductFlow:
        base = dict(
            product_id="p",
            sku="SKU-1",
            spu="SPU-1",
            setup=SetupFacts(has_spu_ref=True, has_colour_ref=True),
            material=MaterialFacts(
                total=3,
                usable=3,
                gate_roles=frozenset({"PRODUCT_FRONT"}),
                usable_roles=frozenset(
                    {"PRODUCT_FRONT", "PRODUCT_BACK", "DETAIL"}
                ),
            ),
            attribute=AttributeFacts(required_fields=(), extraction_count=1),
            plan=PlanFacts(
                has_active_plan=True, plan_is_colour_level=True, has_model=True
            ),
            image_set=ImageSetFacts(exists=True, status="APPROVED", version=1),
            copy=CopyFacts(exists=True, status="APPROVED", version=1),
        )
        base.update(kwargs)
        return ProductFlow(**base)

    #: 每一项都是"这一步本来会判 DONE,但它带着一条阻断问题"。
    #:
    #: **F-1 那一条排在第一个** —— 已批准的图片集少了一个颜色的图。
    cases = [
        (
            "已批准图片集缺一个颜色的配图(F-1 原形)",
            _base(
                image_set=ImageSetFacts(
                    exists=True,
                    status="APPROVED",
                    version=1,
                    violation_codes=("MISSING_IMAGE_FOR_VARIANT",),
                )
            ),
        ),
        (
            "已批准图片集没有主图",
            _base(
                image_set=ImageSetFacts(
                    exists=True,
                    status="APPROVED",
                    version=1,
                    violation_codes=("MISSING_PRIMARY_FOR_VARIANT",),
                )
            ),
        ),
        (
            "已批准图片集引用了被隔离的素材",
            _base(
                image_set=ImageSetFacts(
                    exists=True,
                    status="APPROVED",
                    version=1,
                    violation_codes=("UNUSABLE_ASSET",),
                )
            ),
        ),
        (
            "已批准文案有硬失败",
            _base(
                copy=CopyFacts(
                    exists=True,
                    status="APPROVED",
                    version=1,
                    blocking_violations=1,
                )
            ),
        ),
        (
            "已导出草稿有校验错误",
            _base(
                draft=DraftFacts(
                    exists=True, status="EXPORTED", exported=True, error_count=1
                )
            ),
        ),
    ]

    for name, product_flow in cases:
        result = evaluate(product_flow)
        for step in result.steps:
            has_blocking = any(
                i.level is IssueLevel.BLOCKING for i in step.issues
            )
            assert not (has_blocking and step.state is StepState.DONE), (
                f"[{name}] {step.step} 带着阻断问题却显示 DONE —— "
                "运营会先信那个绿勾,然后在导出时被门禁拦住"
            )


def test_every_substate_has_a_step_state_and_a_severity():
    """两张表都必须穷举 —— 加一个取值而漏了映射,汇总会 KeyError 或静默错。"""
    for state in cf.ColorSubState:
        assert state in cf.SUBSTATE_TO_STEP_STATE, f"{state} 没有对应的步骤状态"
        assert state in cf._SEVERITY, f"{state} 没有严重度,汇总取最坏时会漏掉它"
    assert set(cf.SUBSTATE_TO_STEP_STATE.values()) <= set(StepState)


def test_the_layer_gives_no_percentage():
    """**刻意不给百分数。**

    颜色子态是状态,不是进度。给了百分数,调用方会把它和
    `flow.completion` 相加或取平均 —— 而两者的权重口径不同,
    合出来的数字没有定义。完成度口径要等 6-2 的七步增维重新定。
    """
    names = {n for n, _ in inspect.getmembers(cf)}
    for banned in ("completion", "progress", "percent", "COLOR_WEIGHT"):
        assert not any(banned in n.lower() for n in names), (
            f"本层导出了 {banned} —— 颜色维的百分数没有定义"
        )


# ======================================================== 四、与 6-2 的接口


def test_the_colour_level_steps_are_a_subset_of_the_product_steps():
    """颜色级步骤必须是商品级步骤的子集,且不另立一套步骤名。

    立第二套的表现是向导上颜色那一行写「图片」、商品那一行写「图片集」,
    而两者是同一步;更远的代价是 6-2 加两步时要改两个地方。
    """
    from app.workbench.flow import STEP_ORDER

    assert set(cf.COLOR_STEPS) < set(STEP_ORDER), "颜色级步骤不是商品级步骤的真子集"
    assert cf.COLOR_STEPS == tuple(s for s in STEP_ORDER if s in set(cf.COLOR_STEPS)), (
        "颜色级步骤的顺序与商品级不一致 —— 向导上两行会朝相反方向走"
    )
    assert FlowStep.DRAFT not in cf.COLOR_STEPS, (
        "草稿一个 SPU 一份,它的颜色维在 color_sku_image_map 里,不是颜色子态"
    )


def test_the_plan_step_landed_in_both_places_at_once():
    """**这条守卫在增维那天红过,红得对。**

    6-1 写它的时候 `PLAN` 还不在 `FlowStep` 里,它当时钉的是
    「留了名、并说清什么时候接」(`_PLAN_PENDING` 常量)。batch27 的七步
    增维把 `PLAN` 加进 `FlowStep` 的那一刻它就红了 —— 这正是它的用处:
    提醒改的人 `COLOR_STEPS` 也要跟着加,而**方案步是颜色级的**
    (方案按颜色存,颜色没有自己那份时回落到 SPU 级)。

    现在它换了方向:钉住两处**同时**有 PLAN。哪一天有人只从其中一处
    删掉它,这条会红。
    """
    assert FlowStep.PLAN in {s for s in FlowStep}
    assert FlowStep.PLAN in cf.COLOR_STEPS, (
        "`PLAN` 进了 FlowStep 而没进 COLOR_STEPS —— "
        "向导的颜色行会缺一步,而商品行有它"
    )
    assert not hasattr(cf, "_PLAN_PENDING"), (
        "`_PLAN_PENDING` 还留着 —— 它是一张欠条,账还了就该撕掉,"
        "留着的清单会让下一个人以为方案步还没接(§3.37)"
    )

