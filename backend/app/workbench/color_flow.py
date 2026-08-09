"""颜色子态判定(PRD §13 阶段 6「flow 增维」的颜色那一半)。**零依赖纯函数。**

## 它回答的问题

`flow.py` 回答的是「**这件商品**做到哪一步了」。而 SPU 是多颜色的,运营在
向导里问的是另一个问题:

    这个 SPU 的**每个颜色**各卡在哪里
    哪几个颜色拦着整个 SPU 走不下去
    我现在该先补哪个颜色

一个 SPU 级的百分数回答不了它。九色 SPU 显示「图片集 30%」时,运营不知道
是九个颜色各差一点,还是八个齐了、只有一个颜色一张图都没有 —— 这两种情况
要做的事完全不同(前者等生成,后者去传样品)。

## 为什么它不是 `flow.py` 的一个参数

因为**层不同**:`flow.py` 的输入是一件商品(一行 SKU),而颜色子态的输入是
一个 SPU 下的颜色集。把颜色维塞进 `ProductFlow` 会让「这一件的下一步」
与「这个颜色的下一步」共用一个返回值,而它们在向导里是两个并列的东西。

## 与既有判定的关系:**不重算,只汇总**

每一步的口径都已经有唯一的一份实现,这里一份都不重写:

    ACTIVE 范围      `upstream_snapshot.active_colors` 的同一条(§6.7)
    图片覆盖         `image_set_rules.coverage()` 算好的 `VariantCoverage`
    映射齐不齐       草稿落库的 `color_sku_image_map`(AC-19 的那一份)
    颜色事实         VARIANT 层的 CONFIRMED 事实(§8.1)

调用方把这些**已经算好的结果**翻译成下面的 `ColorView` 递进来。在这里重算
任何一条的表现都是"向导说齐了、门禁说缺" —— 本仓 §3.43 那一族的又一次。

## 三条边界

1. **只判 ACTIVE 颜色的阻断。** PLANNED/PAUSED/DISABLED 出现在结果里
   (运营要看见它们存在),但**不进 `blocking_colors()`** —— 与 §6.7 / AC-18
   的范围口径同一条。停售的颜色缺图不该拦住整个 SPU 上架。
2. **不做回落。** 某一步的输入没给(`None`),判 `UNKNOWN` 而不是 `TODO`。
   「没算过」与「算过,是空的」必须分得开 —— 这是 batch19 的
   `upstream_versions IS NULL` 已经付过一次学费的那条。
3. **不给百分数。** 颜色子态是**状态**,不是进度。给百分数会诱使调用方
   把它和 `flow.completion` 相加或取平均,而两者的权重口径不同,
   合出来的数字没有定义。七步增维(6-2)会重新定义完成度,那时再说。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

# 步骤枚举复用 `flow.FlowStep` —— 颜色子态不另立一套步骤名。
# 立第二套的表现是向导上颜色那一行写「图片」、商品那一行写「图片集」,
# 而两者指的是同一步;更远的代价是 6-2 加两步时要改两个地方。
from app.workbench.flow import FlowStep, StepState

#: 颜色维**有子态**的步骤。
#:
#: 七步里另外那些是 SPU 级的,不进颜色子态:
#:
#:     SETUP    建档是 SPU 的事(颜色是它的产物,不是它的子态)
#:     DRAFT    草稿一个 SPU 一份(它的颜色维已经在 color_sku_image_map 里)
#:
#: `PLAN`(选模特与生成方案)是颜色级的:方案按颜色存,颜色没有自己的
#: 那一份时回落到 SPU 级(`generation_plans` 上那条 COALESCE 唯一索引)。
#: 它在 batch27 的七步增维里进 `flow.FlowStep`,同一批进这张表 ——
#: 6-1 留的那条守卫钉着"两处必须一起改",而它在增维那天确实红了。
COLOR_STEPS: tuple[FlowStep, ...] = (
    FlowStep.MATERIAL,
    FlowStep.ATTRIBUTE,
    FlowStep.PLAN,
    FlowStep.IMAGE_SET,
    FlowStep.COPY,
)



class ColorSubState(StrEnum):
    """一个颜色在某一步上的状态。

    取值刻意与 `flow.StepState` **不完全相同**:颜色维多一个 `UNKNOWN`,
    少一个 `IN_PROGRESS`。

    多 `UNKNOWN`:颜色维的输入来自草稿快照,而草稿可能建于快照列之前
    (`upstream_versions IS NULL`)。那时候的正确答案是「没算过」,
    不是「没做」—— 后者会让运营去补一个其实已经补好的颜色。

    少 `IN_PROGRESS`:颜色维没有"进行中"这个可观测量。素材在传、图在生成
    这些是任务级的状态,挂在任务上,不挂在颜色上。硬造一个的话它只能靠
    「有一部分」来判,而"九个 SKU 里有三个有图"到底算不算进行中没有定义。
    """

    UNKNOWN = "UNKNOWN"
    TODO = "TODO"
    BLOCKED = "BLOCKED"
    NEEDS_CONFIRM = "NEEDS_CONFIRM"
    DONE = "DONE"


#: 颜色子态 -> 商品级步骤状态。
#:
#: 汇总时用它把颜色维翻译回 `flow.StepState`,而不是各写一套映射。
#: `UNKNOWN` 映射成 `TODO` 而不是 `BLOCKED`:没算过不是错,重新生成
#: 一次草稿就有了;判成阻断会让一批老草稿在向导里全部变红。
SUBSTATE_TO_STEP_STATE: Mapping[ColorSubState, StepState] = {
    ColorSubState.UNKNOWN: StepState.TODO,
    ColorSubState.TODO: StepState.TODO,
    ColorSubState.BLOCKED: StepState.BLOCKED,
    ColorSubState.NEEDS_CONFIRM: StepState.NEEDS_CONFIRM,
    ColorSubState.DONE: StepState.DONE,
}

#: 子态的"坏"程度,汇总时取最坏的那个。
#:
#: 顺序里 `BLOCKED` 比 `TODO` 坏,而 `UNKNOWN` 最轻 —— 与上面那张表同向:
#: 一个没算过的颜色不该把整个 SPU 的那一步拉成阻断。
_SEVERITY: Mapping[ColorSubState, int] = {
    ColorSubState.DONE: 0,
    ColorSubState.UNKNOWN: 1,
    ColorSubState.TODO: 2,
    ColorSubState.NEEDS_CONFIRM: 3,
    ColorSubState.BLOCKED: 4,
}


@dataclass(frozen=True)
class ColorView:
    """一个颜色的输入。**调用方翻译好递进来,这里不查任何东西。**

    每个字段都可以是 `None`,含义一律是「这一步没算过」而不是「是空的」。
    """

    variant_id: str
    variant_code: str
    #: `SellableStatus` 的取值。本层不 import 那个枚举(零依赖),
    #: 由 `is_active` 表达范围口径 —— 与 `upstream_snapshot.ColorUpstream` 同形
    sellable_status: str
    is_active: bool

    #: 这个颜色下的 SKU 全集(`products.sku`)。空 = 建了颜色还没建 SKU
    skus: tuple[str, ...] = ()

    #: 有多少张属于这个颜色的**原始样品**素材。None = 没查
    sample_count: int | None = None
    #: 颜色级事实里已确认的字段名。None = 没查
    confirmed_fields: frozenset[str] | None = None
    #: 这个颜色还缺哪些必需字段(调用方按注册表算好)。None = 没查
    missing_required_fields: frozenset[str] = frozenset()
    #: 有冲突待人决定的字段。有值就是 NEEDS_CONFIRM
    conflicting_fields: frozenset[str] = frozenset()

    #: 这个颜色自有的启用图片项数。None = 没查
    owned_image_count: int | None = None
    #: `image_set_rules.coverage()` 算出的缺角度。**不在这里重算**
    missing_angles: frozenset[str] = frozenset()
    #: 落库映射里这个颜色缺主图的 SKU。None = 映射没算过(UNPROVEN)
    skus_missing_primary: frozenset[str] | None = None

    #: 这个颜色有没有生效的生成方案。None = 没查
    has_plan: bool | None = None
    #: 生效的那一份是颜色级的还是 SPU 级回落来的
    plan_is_colour_level: bool = False

    #: 这个颜色有没有一版可用的文案(0051 之后文案按颜色分槽)。None = 没查
    has_copy: bool | None = None
    #: 文案存在但已过期(上游事实变了)
    copy_stale: bool = False


@dataclass(frozen=True)
class ColorStepResult:
    step: FlowStep
    state: ColorSubState
    #: 一句给运营看的话。**空字符串表示这一步没什么要说的**,不是没算
    detail: str = ""


@dataclass(frozen=True)
class ColorResult:
    variant_id: str
    variant_code: str
    is_active: bool
    steps: tuple[ColorStepResult, ...] = ()
    #: 这个颜色的"当前步" —— `COLOR_STEPS` 里第一个不是 DONE 的
    current_step: FlowStep | None = None

    @property
    def by_step(self) -> Mapping[FlowStep, ColorStepResult]:
        return {s.step: s for s in self.steps}

    @property
    def is_blocking(self) -> bool:
        """这个颜色拦不拦着 SPU 走下去。

        **只有 ACTIVE 的颜色算数**(§6.7 / AC-18):停售的颜色缺图是预期,
        不是故障。这一条写在属性里而不是让调用方自己判 —— 让调用方判的话,
        "要不要把 PAUSED 也算上"会在每个调用点各回答一次。
        """
        return self.is_active and any(
            s.state is ColorSubState.BLOCKED for s in self.steps
        )


# ================================================================ 逐步判定


def _material(view: ColorView) -> ColorStepResult:
    if view.sample_count is None:
        return ColorStepResult(FlowStep.MATERIAL, ColorSubState.UNKNOWN, "样品数没查")
    if view.sample_count <= 0:
        # 阻断而不是 TODO:没有样品,后面每一步都做不了,而且**做不了的原因
        # 在这里**。判 TODO 的话运营会在属性页看到"等识别",而永远等不到
        return ColorStepResult(
            FlowStep.MATERIAL, ColorSubState.BLOCKED, "这个颜色还没有原始样品"
        )
    return ColorStepResult(FlowStep.MATERIAL, ColorSubState.DONE)


def _attribute(view: ColorView, *, material_ok: bool) -> ColorStepResult:
    if view.confirmed_fields is None:
        return ColorStepResult(FlowStep.ATTRIBUTE, ColorSubState.UNKNOWN, "颜色事实没查")
    if view.conflicting_fields:
        names = "、".join(sorted(view.conflicting_fields))
        return ColorStepResult(
            FlowStep.ATTRIBUTE, ColorSubState.NEEDS_CONFIRM, f"待决定:{names}"
        )
    if view.missing_required_fields:
        names = "、".join(sorted(view.missing_required_fields))
        # 缺必需事实时,是"还没做"还是"做不了"取决于上一步:样品都没有的话
        # 识别跑不起来,这时候说 TODO 会让运营在属性页干等
        state = ColorSubState.TODO if material_ok else ColorSubState.BLOCKED
        return ColorStepResult(FlowStep.ATTRIBUTE, state, f"缺:{names}")
    return ColorStepResult(FlowStep.ATTRIBUTE, ColorSubState.DONE)


def _plan(view: ColorView, *, upstream_ok: bool) -> ColorStepResult:
    """这个颜色有没有生效的生成方案(batch27 随七步增维一起接上)。

    回落到 SPU 级的方案**算完成**,但要说出来:回落不是问题,而运营
    在"为什么这个颜色的图和别的颜色风格不一样"时需要知道它用的是
    SPU 那一份。
    """
    if view.has_plan is None:
        return ColorStepResult(FlowStep.PLAN, ColorSubState.UNKNOWN, "方案没查")
    if not view.has_plan:
        # 上一步没就绪时判 **TODO(等上一步)**,不是 BLOCKED。
        #
        # BLOCKED 在这一层的含义是"做不了,而且要做的事在**这个颜色**上"
        # —— `blocking_codes` 是运营的待办清单,它只有在"点进去就有事做"
        # 时才有用。把"还没轮到"也算进去的话,一个刚建档的 SPU 每个颜色
        # 都在拦路,清单立刻退化成噪音,而真正缺图的那个颜色淹在里面。
        # 口径与 `_image_set` 的"等上一步"一致 —— 三处必须同向。
        return ColorStepResult(
            FlowStep.PLAN,
            ColorSubState.TODO,
            "这个颜色还没有生成方案" if upstream_ok else "等属性确认",
        )
    if not view.plan_is_colour_level:
        return ColorStepResult(FlowStep.PLAN, ColorSubState.DONE, "用的是 SPU 级方案")
    return ColorStepResult(FlowStep.PLAN, ColorSubState.DONE)


def _image_set(view: ColorView, *, upstream_ok: bool) -> ColorStepResult:

    if view.owned_image_count is None and view.skus_missing_primary is None:
        return ColorStepResult(FlowStep.IMAGE_SET, ColorSubState.UNKNOWN, "图片没查")

    if view.owned_image_count is not None and view.owned_image_count <= 0:
        # §6.5 / BLOCK-02:缺图就是缺图,**不回退用别的颜色或通用图**。
        # 这一条与 `image_set_rules.coverage()` 和 batch24 的行级图片桶
        # 是同一个口径,三处必须同向
        return ColorStepResult(
            FlowStep.IMAGE_SET, ColorSubState.BLOCKED, "这个颜色没有自己的图"
        )
    if view.missing_angles:
        names = "、".join(sorted(view.missing_angles))
        return ColorStepResult(FlowStep.IMAGE_SET, ColorSubState.BLOCKED, f"缺角度:{names}")
    if view.skus_missing_primary:
        names = "、".join(sorted(view.skus_missing_primary))
        return ColorStepResult(
            FlowStep.IMAGE_SET, ColorSubState.BLOCKED, f"缺主图的 SKU:{names}"
        )
    if not upstream_ok:
        return ColorStepResult(FlowStep.IMAGE_SET, ColorSubState.TODO, "等上一步")
    return ColorStepResult(FlowStep.IMAGE_SET, ColorSubState.DONE)


def _copy(view: ColorView, *, upstream_ok: bool) -> ColorStepResult:
    if view.has_copy is None:
        return ColorStepResult(FlowStep.COPY, ColorSubState.UNKNOWN, "文案没查")
    if not view.has_copy:
        # 同 `_plan`:"还没轮到"是 TODO,不是阻断
        return ColorStepResult(
            FlowStep.COPY,
            ColorSubState.TODO,
            "这个颜色还没有文案" if upstream_ok else "等属性确认",
        )
    if view.copy_stale:
        # 过期不是阻断:重新生成一次就好,而它要花钱,所以由运营决定什么时候点
        return ColorStepResult(FlowStep.COPY, ColorSubState.TODO, "文案已过期,需重新生成")
    return ColorStepResult(FlowStep.COPY, ColorSubState.DONE)


def evaluate_color(view: ColorView) -> ColorResult:
    """判定一个颜色。**向导的颜色行与聚合 API 读同一个结果。**"""
    material = _material(view)
    material_ok = material.state is ColorSubState.DONE

    attribute = _attribute(view, material_ok=material_ok)
    attribute_ok = attribute.state is ColorSubState.DONE

    plan = _plan(view, upstream_ok=attribute_ok)
    image_set = _image_set(view, upstream_ok=attribute_ok)
    copy_step = _copy(view, upstream_ok=attribute_ok)

    steps = (material, attribute, plan, image_set, copy_step)

    ordered = tuple(
        next(s for s in steps if s.step is step) for step in COLOR_STEPS
    )
    current = next(
        (s.step for s in ordered if s.state is not ColorSubState.DONE), None
    )
    return ColorResult(
        variant_id=view.variant_id,
        variant_code=view.variant_code,
        is_active=view.is_active,
        steps=ordered,
        current_step=current,
    )


# ================================================================ 汇总


@dataclass(frozen=True)
class SpuColorRollup:
    """一个 SPU 的颜色维汇总。"""

    colors: tuple[ColorResult, ...] = ()
    #: 每一步取**最坏**的那个 ACTIVE 颜色的状态
    worst_by_step: Mapping[FlowStep, ColorSubState] = field(default_factory=dict)
    #: 拦着 SPU 的 ACTIVE 颜色码。**排序过**,给运营看的清单要稳定
    blocking_codes: tuple[str, ...] = ()

    @property
    def active_count(self) -> int:
        return sum(1 for c in self.colors if c.is_active)


def evaluate_colors(views: Iterable[ColorView]) -> SpuColorRollup:
    """判定一个 SPU 的全部颜色并汇总。

    **汇总只看 ACTIVE 颜色**(§6.7 / AC-18),而 `colors` 里两种都留着:
    运营要看见"这个 SPU 还有两个 PLANNED 颜色",只是它们不拦路。

    颜色按 `variant_id` 排序,与 `upstream_snapshot.active_colors` 同一条
    理由:这个结果会进聚合 API 的响应,顺序随查询顺序变的话,
    前端的 key 会在没有任何变化时抖动。
    """
    results = tuple(
        evaluate_color(v) for v in sorted(views, key=lambda v: v.variant_id)
    )
    active = [r for r in results if r.is_active]

    worst: dict[FlowStep, ColorSubState] = {}
    for step in COLOR_STEPS:
        states = [r.by_step[step].state for r in active if step in r.by_step]
        worst[step] = (
            max(states, key=lambda s: _SEVERITY[s]) if states else ColorSubState.UNKNOWN
        )

    return SpuColorRollup(
        colors=results,
        worst_by_step=worst,
        blocking_codes=tuple(sorted(r.variant_code for r in results if r.is_blocking)),
    )


def rollup_step_states(rollup: SpuColorRollup) -> Mapping[FlowStep, StepState]:
    """把颜色维汇总翻译回商品级步骤状态。

    给 6-2 的七步增维用:商品级那一行的状态要能被颜色维**下压**——
    九个颜色里有一个缺图,SPU 的图片集那一步就不该显示 DONE。
    不下压的话向导上会出现"这一步绿了、而点进去有一个颜色是红的"。
    """
    return {step: SUBSTATE_TO_STEP_STATE[state] for step, state in rollup.worst_by_step.items()}


def blocking_summary(rollup: SpuColorRollup) -> str:
    """一句话:哪几个颜色拦着。**空串表示没有颜色拦路。**

    写在这里而不是前端拼:同一句话会出现在向导、列表页悬浮提示和批量
    跳过理由三个地方,拼三次就会分叉成三种说法。
    """
    if not rollup.blocking_codes:
        return ""
    codes = "、".join(rollup.blocking_codes)
    return f"{len(rollup.blocking_codes)} 个颜色拦着:{codes}"


def colors_needing(rollup: SpuColorRollup, step: FlowStep) -> Sequence[ColorResult]:
    """某一步上还没完成的 ACTIVE 颜色。向导的「先补哪个」按它排。"""
    return tuple(
        c
        for c in rollup.colors
        if c.is_active
        and step in c.by_step
        and c.by_step[step].state is not ColorSubState.DONE
    )
