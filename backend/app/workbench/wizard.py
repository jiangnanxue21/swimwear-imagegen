"""一体化向导的判定(PRD §13 阶段 6 批次 6-4 / AC-01 / AC-05 / AC-16)。**零依赖纯函数。**

## 它回答的三个问题

    这七步现在能不能点            -> `step_is_open` / `WizardStepView.is_open`
    点不了的话是什么挡着          -> `gate_reason`,取自那一步自己的判定结果
    绕过前置直接调后面的动作呢    -> `check_action`,服务端按**同一份**判定拒绝

前两个是 AC-01(主流程不离开向导),第三个是 AC-05(不许伪造完成)。
两者共用一个 `FlowResult`,这一点是刻意的 —— AC-05 的原话最后一句就是
「判定层与前端读**同一个**结果」。

## 为什么这一层不自己判任何一步

`flow.evaluate()` 已经逐步给出了 `StepState`,`color_flow` 已经逐颜色给出了
子态。本模块**一条都不重算**,它做的只有两件事:

    翻译   `StepState` -> 「这一步现在能不能进」这个布尔
    定位   `NextActionCode` -> 它属于哪一步(读 `flow.ACTION_STEP`,不另立表)

重算的表现在本仓有过十四次记录(§3.43 那一族):向导说"可以做了"、
服务端说"前置不满足",而两句话都出自本系统 —— 运营会信界面那句,
然后在同一个按钮上点五次。

## 「能不能进」为什么不是 `state is not BLOCKED` 一句话

素材步是例外,而这个例外是 `flow.StepState` 的文档写死的:

    除素材步以外,`BLOCKED` 一律指**上游未就绪**,不携带任何 issue
    素材步的 `BLOCKED` 指**缺少必备素材**,携带具体缺什么的阻断问题

也就是说素材步的红色是「这一步有活要干」,别的步的红色是「还没轮到」。
一律按 `not BLOCKED` 判的话,向导的**第一步**在最需要它的时候(一张图
都没有)是关着的 —— 运营打开向导,七步全灰,无处下手。

例外集写成常量 `SELF_BLOCKING_STEPS` 而不是一句 `if step is MATERIAL`:
它是一条会被继承的语义(将来若有第二个"自己就是待办"的步骤),
而常量能被穷举测试指着看。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.workbench.color_flow import ColorSubState, SpuColorRollup, colors_needing
from app.workbench.flow import (
    ACTION_LABELS,
    ACTION_STEP,
    STEP_LABELS,
    STEP_ORDER,
    FlowResult,
    FlowStep,
    IssueLevel,
    NextActionCode,
    StepState,
)

#: `BLOCKED` 表示「这一步自己有活要干」的步骤。**其余步骤的 BLOCKED 是"等上游"。**
#:
#: 判据不是本模块发明的,是 `flow.StepState` 的文档字符串写死的那条例外。
#: 两处不同向的表现:向导第一步在一张图都没有时是灰的,而那正是运营
#: 打开向导要做的第一件事。
SELF_BLOCKING_STEPS: frozenset[FlowStep] = frozenset({FlowStep.MATERIAL})

#: 每一步一句「这一步在做什么」。**七步穷举**,由守卫钉着。
#:
#: 为什么不复用 `STEP_LABELS`:那是两个字的名字(「素材」「属性」),
#: 用来做标签页与徽标;向导要回答的是"我在这一屏该干什么",
#: 而一个两字名词回答不了。缺一条的表现是向导上某一步只有名字没有说明,
#: 而第一次用的人恰恰卡在那一步。
STEP_INTRO: Mapping[FlowStep, str] = {
    FlowStep.SETUP: "把这一行 SKU 挂到它所属的 SPU 与颜色上",
    FlowStep.MATERIAL: "按颜色上传原始样品;门禁只认人工确认过的角色",
    FlowStep.ATTRIBUTE: "跑识别,然后逐字段确认共享事实与颜色事实",
    FlowStep.PLAN: "给这个颜色选模特与生成参数;没有颜色级的就用 SPU 那一份",
    FlowStep.IMAGE_SET: "按颜色生成、核对并批准图片",
    FlowStep.COPY: "生成并批准 SPU Listing 与颜色字段",
    FlowStep.DRAFT: "形成完整草稿并导出上架文件",
}


@dataclass(frozen=True)
class WizardStepView:
    """向导轨道上的一格。"""

    step: FlowStep
    label: str
    intro: str
    state: StepState
    #: 这一步现在能不能进
    is_open: bool
    #: 进不去的原因。**`is_open` 为真时必须是空串** —— 一句常驻的
    #: "等上一步"会被读成"这一步永远做不了"
    gate_reason: str
    is_current: bool
    #: 这一步允许触发的动作码
    actions: tuple[NextActionCode, ...] = ()
    #: 这一步上还没完成的 ACTIVE 颜色码(有颜色维时才有)。
    #: **空元组与"没有颜色维"是两回事**,后者由 `has_color_axis` 表达
    pending_colors: tuple[str, ...] = ()
    has_color_axis: bool = False


@dataclass(frozen=True)
class ActionVerdict:
    """一次动作请求的判定。**服务端与前端读同一个。**"""

    action: NextActionCode
    step: FlowStep
    allowed: bool
    reason: str


@dataclass(frozen=True)
class WizardView:
    steps: tuple[WizardStepView, ...]
    current_step: FlowStep
    #: 当前这一步上"先补哪个颜色"。None = 这一步没有颜色维待办,或没有颜色维
    focus_color: str | None
    allowed_actions: tuple[NextActionCode, ...]
    #: 阻断清单。**直接来自 `FlowResult.issues`,不重新分级**
    blocking: tuple[tuple[str, str, FlowStep], ...]


# ================================================================ 判定


def step_is_open(result: FlowResult, step: FlowStep) -> bool:
    """这一步现在能不能进。见模块文档里的那条例外。"""
    state = result.step_state(step)
    if state is not StepState.BLOCKED:
        return True
    return step in SELF_BLOCKING_STEPS


def gate_reason(result: FlowResult, step: FlowStep) -> str:
    """进不去的原因。**能进的时候返回空串。**

    ## 为什么理由不在这里拼

    「等上一步」这句话说了等于没说 —— 运营要知道的是等**哪一步**、
    那一步差什么。而这两件事 `flow` 已经算过了:阻断它的必然是 `STEP_ORDER`
    里排在前面、且状态不是 DONE 的第一步,那一步自己的 `summary` 就是答案。

    在这里另写一段"按 facts 推原因"的代码,就是给同一个问题造第二个答案 ——
    而两个答案会在上游改判定的那天分叉,且两边都不报错。
    """
    if step_is_open(result, step):
        return ""
    blocker = next(
        (
            s
            for s in result.steps
            if s.step in STEP_ORDER[: STEP_ORDER.index(step)]
            and s.state is not StepState.DONE
        ),
        None,
    )
    if blocker is None:
        # 走不到:非自阻断步的 BLOCKED 按定义就是上游未就绪。留一句如实的话
        # 而不是空串 —— 空串在界面上是"没有原因",那比一句笼统的话更难查
        return "上游还没就绪"
    return f"等「{STEP_LABELS[blocker.step]}」:{blocker.summary or blocker.state.value}"


def actions_of(step: FlowStep) -> tuple[NextActionCode, ...]:
    """这一步有哪些动作码。**读 `flow.ACTION_STEP`,不另立一张表。**

    另立的表现是新增一个动作码之后,向导上那一步少一个按钮 —— 而
    `ACTION_STEP` 是完整的,所以没有任何测试会红。
    """
    return tuple(code for code, owner in ACTION_STEP.items() if owner is step)


def check_action(result: FlowResult, action: NextActionCode) -> ActionVerdict:
    """能不能执行这个动作。**AC-05 的落点:服务端与向导读同一份判定。**

    `DONE` 不是一个动作,它是"没有待办"的占位码;把它当动作请求过来
    一律拒绝,而不是当成允许 —— 允许的话调用方会拿它当"随便做点什么"的通行证。
    """
    step = ACTION_STEP[action]
    if action is NextActionCode.DONE:
        return ActionVerdict(action, step, False, "「已完成」不是一个可执行的动作")
    if step_is_open(result, step):
        return ActionVerdict(action, step, True, "")
    return ActionVerdict(action, step, False, gate_reason(result, step))


def allowed_actions(result: FlowResult) -> tuple[NextActionCode, ...]:
    """当前允许触发的全部动作码,按 `STEP_ORDER` 排。

    排序是刻意的:前端按它渲染按钮,顺序随字典遍历变的话,同一件商品
    两次刷新会得到不同的按钮排列,而运营是靠位置点的。
    """
    out: list[NextActionCode] = []
    for step in STEP_ORDER:
        if not step_is_open(result, step):
            continue
        out.extend(
            code
            for code in actions_of(step)
            if check_action(result, code).allowed
        )
    return tuple(out)


def focus_color(rollup: SpuColorRollup | None, step: FlowStep) -> str | None:
    """这一步上"先补哪个颜色"。

    取 `color_flow.colors_needing` 的第一个 —— 那个函数的文档写的就是
    「向导的『先补哪个』按它排」,本批是它的第一个调用点。

    `None` 有两种来历,而它们在界面上要做的事一样(不高亮任何颜色):
    没有颜色维(存量行),或这一步的 ACTIVE 颜色都完成了。
    """
    if rollup is None:
        return None
    pending = colors_needing(rollup, step)
    return pending[0].variant_code if pending else None


def _pending_colors(rollup: SpuColorRollup | None, step: FlowStep) -> tuple[str, ...]:
    if rollup is None:
        return ()
    return tuple(c.variant_code for c in colors_needing(rollup, step))


def build(result: FlowResult, *, rollup: SpuColorRollup | None = None) -> WizardView:
    """组装向导视图。**向导、总览页与列表页读的是同一个 `FlowResult`。**"""
    steps = tuple(
        WizardStepView(
            step=step,
            label=STEP_LABELS[step],
            intro=STEP_INTRO[step],
            state=result.step_state(step),
            is_open=step_is_open(result, step),
            gate_reason=gate_reason(result, step),
            is_current=step is result.current_step,
            actions=tuple(
                code for code in actions_of(step) if check_action(result, code).allowed
            ),
            pending_colors=_pending_colors(rollup, step),
            # 颜色维只覆盖 `color_flow.COLOR_STEPS` 那几步;SETUP 与 DRAFT
            # 是 SPU 级的。这个布尔让前端能把"这一步没有颜色维"和
            # "有颜色维、都做完了"分开显示 —— 后者是成绩,前者不是
            has_color_axis=(
                rollup is not None
                and any(step in c.by_step for c in rollup.colors)
            ),
        )
        for step in STEP_ORDER
    )
    return WizardView(
        steps=steps,
        current_step=result.current_step,
        focus_color=focus_color(rollup, result.current_step),
        allowed_actions=allowed_actions(result),
        blocking=tuple(
            (i.code, i.message, i.target_step)
            for i in result.issues
            if i.level is IssueLevel.BLOCKING
        ),
    )


# ================================================================ 出参


def serialize(
    view: WizardView, *, cost: Mapping[str, object] | None = None
) -> dict[str, object]:
    """向导视图 -> 接口出参。

    `cost` 由 6-5 的费用预估填进来。它作为**参数**而不是在这里现算:
    本模块零依赖(算钱要读价目表与上游用量),而 AC-15 要求向导"一次请求
    拿到七步状态、颜色子态、阻塞项与费用预估" —— 一次请求不等于一个模块。

    `cost=None` 的含义是**没算**,不是"不要钱"。前端据此显示"未预估",
    与本仓「没算过 ≠ 没做」那条同向。
    """
    return {
        "current_step": view.current_step.value,
        "focus_color": view.focus_color,
        "allowed_actions": [a.value for a in view.allowed_actions],
        "blocking": [
            {"code": code, "message": message, "target_step": step.value}
            for code, message, step in view.blocking
        ],
        "steps": [
            {
                "step": s.step.value,
                "label": s.label,
                "intro": s.intro,
                "state": s.state.value,
                "is_open": s.is_open,
                "gate_reason": s.gate_reason,
                "is_current": s.is_current,
                "actions": [
                    {"code": a.value, "label": ACTION_LABELS[a]} for a in s.actions
                ],
                "pending_colors": list(s.pending_colors),
                "has_color_axis": s.has_color_axis,
            }
            for s in view.steps
        ],
        "cost": cost,
    }


def worst_color_state(
    rollup: SpuColorRollup | None, step: FlowStep
) -> ColorSubState | None:
    """这一步上最坏的那个 ACTIVE 颜色子态。没有颜色维时 `None`。

    留给调用方做"这一步绿了,而点进去有一个颜色是红的"这类提示。

    **不在这里把它并进 `WizardStepView.state`。** 这句话上一版就在,
    而它当时与 `color_flow.rollup_step_states()` 的文档("要能被颜色维下压")
    正面冲突 —— 两份规格打架,而两边都没有生产调用点(审阅 F-1)。

    裁定写在 `color_flow` 顶部的「合并规则」一节,结论是这一句:
    SPU 级步骤的下压发生在 `flow` 里(由 `_no_done_while_blocking` 兜住),
    行级步骤不下压。所以这一层只并列显示,不合并 —— 合并会造出第三种
    步骤状态(向导一份、总览页一份、列表页一份),而 AC-15 的原话是
    「三处不得出现互相矛盾的状态」。

    前端要区分"商品级绿 / 颜色级红"读的是 `pending_colors` 与
    `has_color_axis`(`WizardRail` 上那个徽标),不是第二个 state。
    """
    if rollup is None:
        return None
    return rollup.worst_by_step.get(step)


def action_codes_by_step() -> Mapping[FlowStep, Sequence[NextActionCode]]:
    """动作码按步骤分组。给守卫穷举用 —— 有了它,"某个动作码没有归属步骤"
    这件事会在测试里当场红,而不是在向导上表现为一个点不动的按钮。"""
    return {step: actions_of(step) for step in STEP_ORDER}
