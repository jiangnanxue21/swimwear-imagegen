"""生成方案的判定层(PRD v3.1 §4.7 / §6.4 / §8.1)。**零依赖纯函数。**

## 它补的是 PRD 风险表 D2

> **GenerationPlan 缺实体**:§6.4 与 §7.3 都引用"生成方案"(SPU 默认 + 颜色覆盖、
> 方案指纹进幂等键),但 §4 数据模型没有定义它,方案无处持久化、指纹无从计算。

表由 `models/generation_plan.py` 落,**判定全在这里**。分成这两半的理由和
`workflows/publish_policy.py`、`workbench/batch.py` 顶部那两条一样:
"换了方案之后哪些颜色的图该重出"是一个能被穷举的问题,而它一旦写进服务层,
回答它就要先造一套库内数据 —— 于是没有人会去回答它。

## 四个判断,每一个都有过反面写法

**一、指纹用长度前缀编码,不用分隔符拼接。**
`("ab","c")` 与 `("a","bc")` 用 `"|"` 拼出来落在同一个碰撞族里。
指纹碰撞在这里的后果不抽象:**换了方案而幂等键没变**,于是"重新生成"
静默命中上一个任务,运营看到提交成功、拿到的是旧参数的图。
编码器直接复用 `attributes/scope_fingerprint.length_prefixed` —— 抄一份过去
的代价不是重复,是两个编码器会漂移(那个函数被提成公开的正是为了这件事)。

**二、"这个颜色当前生效的是哪份方案"必须算出来,不能查出来。**
§4.7 的唯一约束是 `UNIQUE(spu_id, COALESCE(color_variant_id,''))`,
也就是说库里同时存在 SPU 默认与若干颜色覆盖。**改 SPU 默认方案时,
只有没有覆盖的那些颜色该重出图** —— 有覆盖的颜色一个字都没变。
按"改了就全部过期"写,一个九色 SPU 调一次默认角度会重出八个颜色的全部候选,
每一张都是真实付费调用。`effective_fingerprints()` 是这条的落点。

**三、预算 fail-closed。**
`budget_cap` 有值而花费读不到时返回**拒绝**,不是放行。与
`attributes/calibration` 的 fail-closed、`scope_fingerprint.facts_stale`
的"算不出来就算过期"同一条口径:证明不了没超,就不能去花钱。

**四、角度是集合不是数量。**
§6.5 明写「必要角度按 `angles_json` 配置验收,**不是只数总张数**」。
数总张数在多颜色下会给出错误的绿灯:一个颜色出了 4 张正面图,总数达标,
而背面一张都没有 —— 而"缺背面图"这件事平台不会拦,消费者会。
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.attributes.scope_fingerprint import length_prefixed
from app.core.enums import ImageAngle
from app.core.field_limits import MAX_CANDIDATE_COUNT

#: 共享作用域的键。与 `scope_fingerprint.SHARED` 同一个理由:任何字符串
#: 都可能和一个真实的 `color_variant_id` 撞上,而撞上的表现是
#: "SPU 默认方案被某个颜色的覆盖顶掉",一个不报错只出错图的 bug。
SPU_DEFAULT: None = None

#: 方案指纹版本。算法或元素集合有任何改动都要动它 —— 不动的话新旧指纹
#: 会被直接比较,结果是**全库图片集一次性集体过期**,而运营看到的是
#: "所有颜色同时要求重出图",查不出原因。
PLAN_FINGERPRINT_VERSION = "v1"

#: 一份方案里所有角度的候选数之和的上限。
#:
#: 单一来源是 `MAX_CANDIDATE_COUNT`(`phase_budget` 推导租约时长用的也是它)。
#: 这里必须和它同源:方案说要出 24 张而一轮最多 8 张时,多出来的部分
#: 不会报错,只会永远缺角度 —— 而"缺角度"要到批准那一步才被 §6.5 拦下。
MAX_PLAN_CANDIDATES = MAX_CANDIDATE_COUNT


class BudgetVerdict(StrEnum):
    """预算判定(§6.4「预算通过(方案 budget_cap + 现有花费台账)」)。"""

    #: 没设上限,或者设了但还没花到
    ALLOWED = "ALLOWED"
    #: 已花 + 本次预估 > 上限
    EXCEEDED = "EXCEEDED"
    #: 设了上限,但花费读不到 —— 见模块文档第三条
    UNKNOWN = "UNKNOWN"


#: 拒绝创建任务的两种判定。放行只有一种,所以这张表按"不放行"列。
BUDGET_BLOCKING: frozenset[BudgetVerdict] = frozenset(
    {BudgetVerdict.EXCEEDED, BudgetVerdict.UNKNOWN}
)


@dataclass(frozen=True)
class AngleRequirement:
    """一个角度要出几张。

    `count` 是**该角度的候选数**,不是最终入集数:评分会淘汰,所以出 2 张
    留 1 张是常态。把它读成"最终要 2 张背面图"会让 §6.5 的角度验收
    在正常情况下永远不过。
    """

    angle: str
    count: int = 1


@dataclass(frozen=True)
class PlanView:
    """参与判定的一份方案。

    刻意做得很窄,只有规则真正要用的字段 —— 与 `image_set_rules.ItemView`
    同一条理由:传 ORM 行进来,这个模块就开始依赖数据库的形状,
    而它的全部价值在于不依赖。
    """

    plan_id: str
    #: None = SPU 默认方案;非空 = 该颜色的覆盖
    color_variant_id: str | None = None
    model_template_id: str | None = None
    provider: str = ""
    scene: str = ""
    pose: str = ""
    angles: tuple[AngleRequirement, ...] = ()
    budget_cap: Any = None
    status: str = "ACTIVE"


# ---------------------------------------------------------------- 角度


def normalize_angles(raw: Any) -> tuple[AngleRequirement, ...]:
    """把 `angles_json` 归一成判定用的形状。

    接受三种写法,因为这三种在真实调用点上都会出现:

        ["FRONT", "BACK"]                       只说角度,每个 1 张
        {"FRONT": 2, "BACK": 1}                 角度 -> 张数
        [{"angle": "FRONT", "count": 2}, ...]   接口入参的完整形状

    **未知角度直接丢掉,不抛错。**`angles_json` 是 JSONB,存量行与手工 SQL
    都可能塞进拼错的值;在判定层抛错的后果是整个 SPU 的图片集校验 500,
    而真正该发生的是"那个角度不算数"。拼错的角度会在角度覆盖里表现为
    "要求的角度比预期少",而那由 `plan_is_usable()` 单独报。

    排序按 `ImageAngle` 的定义序,不按输入序:指纹要对键序不敏感,
    否则前端调一下角度的展示顺序就会换一个幂等键。
    """
    counts: dict[str, int] = {}

    def put(angle: Any, count: Any) -> None:
        key = str(angle or "").strip().upper()
        if key not in ImageAngle.__members__:
            return
        try:
            number = int(count)
        except (TypeError, ValueError):
            number = 1
        # 0 或负数当成 1:方案里写了这个角度就是要它,写 0 是笔误而不是
        # "要这个角度但不出图" —— 后者的表达方式是不写这一条
        counts[key] = max(1, number)

    if isinstance(raw, Mapping):
        for angle, count in raw.items():
            put(angle, count)
    elif isinstance(raw, (list, tuple)):
        for entry in raw:
            if isinstance(entry, Mapping):
                put(entry.get("angle"), entry.get("count", 1))
            else:
                put(entry, 1)

    order = {a.value: i for i, a in enumerate(ImageAngle)}
    return tuple(
        AngleRequirement(angle=a, count=counts[a]) for a in sorted(counts, key=lambda x: order[x])
    )


def total_candidates(angles: Iterable[AngleRequirement]) -> int:
    """这份方案一轮要出几张。"""
    return sum(max(1, a.count) for a in angles)


def required_angles(angles: Iterable[AngleRequirement]) -> frozenset[str]:
    """验收要检查哪些角度(§6.5)。"""
    return frozenset(a.angle for a in angles)


def serialize_angles(angles: Iterable[AngleRequirement]) -> list[dict[str, Any]]:
    """回写 `angles_json` 的形状。**规范化之后的那一份**,不是入参原样。

    存原样的话,`{"FRONT":1}` 与 `[{"angle":"FRONT","count":1}]` 会在库里
    留下两种形状,而指纹一样 —— 于是"这两份方案一不一样"有两个答案。
    """
    return [{"angle": a.angle, "count": a.count} for a in angles]


# ---------------------------------------------------------------- 指纹


def plan_fingerprint(
    *,
    model_template_id: Any = None,
    provider: Any = None,
    scene: Any = None,
    pose: Any = None,
    angles: Iterable[AngleRequirement] = (),
    budget_cap: Any = None,
) -> str:
    """§4.7:「由上述字段派生,进生成幂等键(§9.3)」。

    **`color_variant_id` 不进指纹**,这一条容易写反。方案指纹回答的是
    "这份方案的参数是什么",而"它管哪个颜色"由归属列回答。把归属编进去
    的后果是:给颜色 A 和颜色 B 配了完全相同的参数,却得到两个不同的指纹,
    于是 §8.1「更换生成方案」那一行会在**什么都没换**的时候判两色图片集过期。

    **`budget_cap` 进指纹**,这一条容易写漏。它不影响出图长什么样,
    但它决定**能不能出** —— 上限从 10 元改到 100 元之后重新提交,
    应该是一次新的尝试而不是静默命中那个被预算拦下的旧任务。
    """
    parts = [
        length_prefixed(PLAN_FINGERPRINT_VERSION),
        length_prefixed(model_template_id),
        length_prefixed(_text(provider)),
        length_prefixed(_text(scene)),
        length_prefixed(_text(pose)),
        length_prefixed(_money(budget_cap)),
    ]
    parts += [length_prefixed(f"{a.angle}={a.count}") for a in angles]
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def fingerprint_of(plan: PlanView) -> str:
    """一份 `PlanView` 的指纹。入口只有这一个,免得调用点各自摊字段。"""
    return plan_fingerprint(
        model_template_id=plan.model_template_id,
        provider=plan.provider,
        scene=plan.scene,
        pose=plan.pose,
        angles=plan.angles,
        budget_cap=plan.budget_cap,
    )


# ---------------------------------------------------------------- 覆盖解析


def resolve_plan(plans: Iterable[PlanView], color_variant_id: str | None) -> PlanView | None:
    """某个颜色**当前生效**的方案:颜色覆盖优先,回落 SPU 默认。

    只看 ACTIVE 的:DRAFT 是编辑中的那一份(见 `GenerationPlanStatus`),
    ARCHIVED 是留着给指纹追溯的。拿 DRAFT 去创建任务等于让编辑器里
    改到一半的参数直接花钱。
    """
    wanted = _text_or_none(color_variant_id)
    active = [p for p in plans if p.status == "ACTIVE"]
    if wanted is not None:
        for plan in active:
            if _text_or_none(plan.color_variant_id) == wanted:
                return plan
    for plan in active:
        if _text_or_none(plan.color_variant_id) is None:
            return plan
    return None


def effective_fingerprints(
    plans: Iterable[PlanView], color_variant_ids: Iterable[str]
) -> dict[str, str]:
    """每个颜色**当前生效**的方案指纹。见模块文档第二条。

    没有任何方案可用的颜色**不出现在结果里**,而不是给一个空串:
    空串会和"方案被删掉了"算成同一件事,于是删掉一份方案不会让任何
    图片集过期 —— 而那正是最该重出图的一种变化。缺席由
    `image_set_stale_scopes()` 的并集比较兜住。
    """
    resolved: dict[str, str] = {}
    for variant in color_variant_ids:
        key = _text_or_none(variant)
        if key is None:
            continue
        plan = resolve_plan(plans, key)
        if plan is not None:
            resolved[key] = fingerprint_of(plan)
    return resolved


def image_state_scope(color_variant_id: str | None) -> str | None:
    """图片集这一侧的作用域键。与 `scope_fingerprint` 的作用域同形。"""
    return _text_or_none(color_variant_id)


def image_set_stale_scopes(
    before: Mapping[str, str], after: Mapping[str, str]
) -> frozenset[str]:
    """§8.1「更换生成方案(plan_fingerprint 变化)→ 对应作用域图片集过期」。

    入参是两次 `effective_fingerprints()`。比**并集**不是交集:
    只比交集的话,"某个颜色的方案被删掉"会因为那个键在 after 里不存在
    而被漏掉。这一条和 `scope_fingerprint.changed_scopes` 是同一个坑,
    也是同一条改法 —— 那边的注释里写着为什么。

    返回的是颜色 id 的集合,**不含 SPU 默认那一档**:图片集是按颜色批准的,
    "SPU 默认方案变了"这件事的落点是它管着的那些颜色,不是一个叫
    "通用图片集"的对象。
    """
    return frozenset(
        scope for scope in set(before) | set(after) if before.get(scope) != after.get(scope)
    )


# ---------------------------------------------------------------- 预算


def budget_verdict(
    *, budget_cap: Any, spent: Any, estimated: Any = 0
) -> BudgetVerdict:
    """§6.4 的预算闸。见模块文档第三条。

    `spent` 来自现有花费台账(`provider_usage_records`),`estimated` 是本次
    预估。两个都用 Decimal:金额用 float 比较会在边界上给出
    "已花 9.999999999 < 上限 10" 这种放行,而那正是上限存在的意义所在。

    ## `estimated=None` 是「估不出来」,不是「零」(2026-08-11 评审)

    这里原来写的是 `plan = _decimal(estimated) or Decimal(0)` —— 于是 None、
    0、和一个解析不出来的值全部塌成 0。而调用方
    (`generation_service.plan_budget_verdict`)当时**连 `estimated` 都没传**,
    所以这道闸实际执行的是:

        已花 9.9,上限 10,本次预计 5   ->   9.9 < 10   ->   放行

    也就是说它是「**已经超了以后不再生成**」,而不是「保证不超过预算」。
    §6.4 要的是后者。

    修法有两半,这一半是语义:`None` 单独一档,含义与 `spent=None` 完全
    一致 —— **证明不了这一次不会超,就不能去花钱**,所以它也走 UNKNOWN。
    与 `core/pricing` 反复写的那条同源:NULL 是"不知道",0 是"免费"。
    默认值仍然是 `0`(不是 None),这样没有预估概念的调用方行为不变;
    "我算不出来"必须由调用方**显式**说出来。

    另一半在服务层:预估要含**在途但还没计费**的任务,否则并发两次创建
    会各自看到同一个 `spent`(见 `spend.reserved_for_generation_plan`)。
    """
    cap = _decimal(budget_cap)
    if cap is None:
        return BudgetVerdict.ALLOWED
    used = _decimal(spent)
    if used is None:
        return BudgetVerdict.UNKNOWN
    if estimated is None:
        return BudgetVerdict.UNKNOWN
    plan = _decimal(estimated)
    if plan is None:
        # 传了个既不是 None 也解析不出来的东西(空串、乱字符串)。当成 0
        # 会把一个坏调用方悄悄变成"本次免费",所以同样按不知道处理
        return BudgetVerdict.UNKNOWN
    return BudgetVerdict.EXCEEDED if used + plan > cap else BudgetVerdict.ALLOWED


def budget_blocks(verdict: BudgetVerdict) -> bool:
    """拒不拒。单独一个函数是为了让"放行只有一种"这件事只写一次。"""
    return verdict in BUDGET_BLOCKING


# ---------------------------------------------------------------- 可用性


@dataclass(frozen=True)
class PlanProblem:
    code: str
    message: str


#: 方案本身的问题码。与图片集的 `ImageSetViolation` 分开:那边回答
#: "这一版图能不能发",这边回答"这份方案能不能拿去创建任务"。
PLAN_NO_ANGLES = "PLAN_NO_ANGLES"
PLAN_TOO_MANY_CANDIDATES = "PLAN_TOO_MANY_CANDIDATES"
PLAN_UNKNOWN_ANGLE_DROPPED = "PLAN_UNKNOWN_ANGLE_DROPPED"
PLAN_NO_PROVIDER = "PLAN_NO_PROVIDER"
#: 方案指定的 Provider 这个部署用不了(没实现,或者没配 Key)。
#:
#: 2026-08-11 评审:前端的 Provider 下拉硬编码着 `mock / fashn / comfyui`,
#: 而后端 `registry.IMPLEMENTED_PROVIDERS` 只有 `{mock, fashn}` 并且会明确拒绝
#: comfyui。于是运营能一路"选 comfyui -> 存草稿 -> 启用 -> 看起来一切正常",
#: 直到创建生成任务那一刻拿到 `CONFIG_INVALID` —— 而那时错误指向的是任务,
#: 不是那份三天前存下来的方案。
PLAN_PROVIDER_UNAVAILABLE = "PLAN_PROVIDER_UNAVAILABLE"


def plan_problems(
    plan: PlanView,
    *,
    raw_angle_count: int | None = None,
    usable_providers: Iterable[str] | None = None,
) -> list[PlanProblem]:
    """这份方案能不能拿去创建任务。

    `raw_angle_count` 是**归一化之前**有几条角度。传了它才能报出
    `PLAN_UNKNOWN_ANGLE_DROPPED` —— `normalize_angles` 静默丢掉未知角度
    (见那个函数的说明),不在这里补一句的话,"方案里写了 BACK 却拼成 BAKC"
    的表现是"背面图永远不缺",而没有任何地方会说为什么。

    `usable_providers` 是**这个部署当前能用的 Provider 名字**,由调用方从
    `providers/registry` 取。传进来而不是在这里去查,是因为本模块零依赖 ——
    "谁能用"是一个环境事实(装了什么、配了什么 Key),不是一个可穷举的判定。
    与 `channels` 那条"需要数据就让调用方传进来"同一条规矩。

    **`None` 表示"这次不查这一维"**,不是"一个都不能用":空集合是后者。
    两者分开是必须的 —— 合并之后,任何一个还没接上注册表的调用点都会把
    每一份方案报成 Provider 不可用。
    """
    problems: list[PlanProblem] = []
    if not plan.angles:
        problems.append(PlanProblem(PLAN_NO_ANGLES, "方案没有配置任何拍摄角度"))
    total = total_candidates(plan.angles)
    if total > MAX_PLAN_CANDIDATES:
        problems.append(
            PlanProblem(
                PLAN_TOO_MANY_CANDIDATES,
                f"方案一轮要出 {total} 张,超过单轮上限 {MAX_PLAN_CANDIDATES} 张",
            )
        )
    if raw_angle_count is not None and raw_angle_count > len(plan.angles):
        problems.append(
            PlanProblem(
                PLAN_UNKNOWN_ANGLE_DROPPED,
                f"有 {raw_angle_count - len(plan.angles)} 条角度不是已知取值,已忽略",
            )
        )
    wanted = _text(plan.provider)
    if not wanted:
        problems.append(PlanProblem(PLAN_NO_PROVIDER, "方案没有指定 Provider"))
    elif usable_providers is not None:
        allowed = {_text(name).lower() for name in usable_providers}
        if wanted.lower() not in allowed:
            problems.append(
                PlanProblem(
                    PLAN_PROVIDER_UNAVAILABLE,
                    f"这个部署现在用不了 Provider「{wanted}」"
                    f"(未实现或未配置);可选:{', '.join(sorted(allowed)) or '无'}",
                )
            )
    return problems


def plan_is_usable(
    plan: PlanView,
    *,
    raw_angle_count: int | None = None,
    usable_providers: Iterable[str] | None = None,
) -> bool:
    return not plan_problems(
        plan, raw_angle_count=raw_angle_count, usable_providers=usable_providers
    )


# ---------------------------------------------------------------- 方案决定出图参数


#: 方案说了算的那几个参数。**清单只有这一份**,`generation_service.create_task`
#: 与冲突留痕都读它 —— 各写一份的表现是"审计说按方案执行,而某个字段其实没换"。
#:
#: 不在这张表里的(`candidate_count` / `max_rounds` / `base_seed` / `prompt`)
#: 由调用方给,理由是方案里根本没有这几个字段。**唯一的例外是张数**:
#: `angles` 决定一轮出几张,所以有方案时 `candidate_count` 是**算出来的**,
#: 不是调用方给的 —— 见 `governed_candidate_count()`。
PLAN_GOVERNED_FIELDS: tuple[str, ...] = ("provider", "model_template_id")


@dataclass(frozen=True)
class PlanOverride:
    """调用方传的与方案不同的一处。**按方案执行,并且说出来。**

    静默覆盖是这条规则最容易写出来的形状,也是最坏的一种:排障的人拿着
    请求体对着出参,两边对不上,而没有任何地方解释为什么。
    """

    field: str
    requested: str
    plan: str

    def as_payload(self) -> dict[str, str]:
        """进审计 payload 的形状。`applied` 恒为 `plan` —— 见模块文档。"""
        return {
            "field": self.field,
            "requested": self.requested,
            "plan": self.plan,
            "applied": "plan",
        }


def plan_overrides(plan: PlanView, requested: Mapping[str, Any]) -> tuple[PlanOverride, ...]:
    """方案与请求哪几处不同(§5.2 硬约束 3)。

    **调用方没传的不算冲突。** `requested` 里为 None 或空串的键表示"我没意见",
    而不是"我要求它是空的" —— 把没传算成冲突会让每一次正常的建任务都在审计里
    留下两条噪音,而真正的冲突会淹没在里面。

    比较一律走 `_text()`:`UUID` 与它的字符串形式是同一个值,
    而调用点两侧恰好一边是 ORM 列、一边是接口入参。
    """
    out: list[PlanOverride] = []
    for field_name in PLAN_GOVERNED_FIELDS:
        asked = _text(requested.get(field_name))
        if not asked:
            continue
        mine = _text(getattr(plan, field_name, None))
        if asked != mine:
            out.append(PlanOverride(field=field_name, requested=asked, plan=mine))
    return tuple(out)


def governed_candidate_count(plan: PlanView, *, fallback: int) -> int:
    """有方案时一轮出几张。

    §5.2:`angles_json` 决定角度集合与张数。**方案没配角度时回落到调用方的值**
    而不是回落到 0 —— `plan_problems()` 已经把"没有角度"报成方案本身的问题,
    在这里再判一次会让一份坏方案的表现从"报错"变成"提交了一个不出图的任务"。
    """
    total = total_candidates(plan.angles)
    return total if total > 0 else fallback


# ------------------------------------------------- 角度 work-unit(2026-08-11)


@dataclass(frozen=True)
class AngleUnit:
    """**一次 Provider 调用**要覆盖的角度与张数。

    ## 它补的是「按角度验收,却不按角度生成」

    改之前:方案写 `FRONT×2 / BACK×1`,系统的全部动作是把
    `candidate_count` 设成 3、往提示词里拼一句「拍摄角度:FRONT×2、BACK×1」,
    然后**一次**请求让 Provider 出 3 张。请求里没有角度这个概念,
    候选图上也没有记角度。于是模型完全可以给回 FRONT / FRONT / FRONT ——
    而 §6.5 的验收严格要求 FRONT 和 BACK 各有覆盖:

        方案按角度验收,生成并没有按角度执行。

    这不是"提示词写得不够好",是执行模型里少了一层:**角度是工作单元的维度**。
    拆开之后每个角度有自己的一次调用、自己的提示词、自己的目标张数,
    候选图也能按提交顺序对回它该覆盖的角度。

    `prompt` 是这一次调用真正要发的提示词(基础提示词 + 本次角度),
    在这里算好而不是留给各家 Provider 拼 —— 拼在 Provider 里意味着
    mock / fashn / comfyui 各有一份措辞,而提示词进幂等键。
    """

    #: None = 这份任务没有方案,按 `candidate_count` 一次出完(存量路径)
    angle: str | None
    count: int
    prompt: str | None


def unit_prompt(base_prompt: Any, *, angle: Any, count: Any = 1) -> str | None:
    """某一个角度那一次调用的提示词。

    角度为空时**原样返回**(含 None):没有方案的任务走的就是这条路,
    它的提示词不该因为经过了这个函数而变形 —— `build_idempotency_key`
    对 None 与 "" 归一,但 `GenerationTask.prompt` 列上两者不是一回事。

    有角度时在基础提示词后面追加一句"本次只出"。基础提示词里已经有方案
    的角度总表(`compose_prompt`),两句并存是刻意的:总表说的是"这份方案
    要哪些角度",这一句说的是"**这一次**只要哪个" —— 后者才是这次调用的
    约束,而前者给模型上下文。删掉总表会改掉 `task.prompt`,
    也就改掉已有任务的幂等键。
    """
    head = _text(base_prompt)
    key = _text(angle).upper()
    if not key:
        return base_prompt if base_prompt is None else head
    try:
        number = max(1, int(count))
    except (TypeError, ValueError):
        number = 1
    directive = f"本次只出这个拍摄角度:{key}×{number}"
    return f"{head};{directive}" if head else directive


def angle_units(
    angles: Iterable[AngleRequirement],
    *,
    base_prompt: Any = None,
    fallback_count: int = 1,
) -> tuple[AngleUnit, ...]:
    """把方案的角度拆成一串工作单元。

    没有角度时返回**一个** `angle=None` 的单元,张数用 `fallback_count` ——
    也就是原来的行为。返回空元组是不行的:调用方会读成"这一轮不用出图",
    而正确含义是"这一轮没有角度维度"。

    顺序跟着 `normalize_angles` 排好的 `ImageAngle` 定义序,不跟输入序:
    候选图靠**提交顺序**对回角度(`angle_assignments`),顺序不稳定
    就等于对错。
    """
    wanted = tuple(angles)
    if not wanted:
        return (
            AngleUnit(
                angle=None,
                count=max(1, int(fallback_count)),
                prompt=base_prompt if base_prompt is None else _text(base_prompt),
            ),
        )
    return tuple(
        AngleUnit(
            angle=a.angle,
            count=max(1, int(a.count)),
            prompt=unit_prompt(base_prompt, angle=a.angle, count=a.count),
        )
        for a in wanted
    )


def angle_assignments(
    angles: Iterable[AngleRequirement], total: int
) -> tuple[str | None, ...]:
    """第 i 张候选图**本来该覆盖**哪个角度。

    按 `angle_units()` 的顺序与张数摊平:`FRONT×2 / BACK×1` 得到
    `("FRONT", "FRONT", "BACK")`。落在方案张数之外的下标给 `None` ——
    那些图确实没有目标角度(Provider 多给了,或者续跑时张数变过),
    硬塞一个角度进去会让"这张图覆盖了背面"变成一句没人说过的话。

    **位置对应是唯一可行的映射,而它的前提是提交顺序被保住**:
    每个单元一次(或几次)提交,取结果时按同样顺序摊平。FASHN 的
    `fetch_results` 按 `split_external_id` 的顺序遍历、Mock 按 index 渲染,
    两者都满足。将来接一家不保序的 Provider 时,要么让它自己在候选图的
    `metadata` 里回带角度,要么这个函数就不能用 —— 这一条写在这里,
    免得那一天有人以为"位置"是个安全假设。
    """
    flat: list[str | None] = []
    for unit in angle_units(angles):
        if unit.angle is None:
            break
        flat.extend([unit.angle] * unit.count)
    if len(flat) >= total:
        return tuple(flat[:total])
    return tuple(flat) + (None,) * (total - len(flat))


def compose_prompt(
    base_prompt: Any,
    *,
    scene: Any = None,
    pose: Any = None,
    angles: Iterable[AngleRequirement] = (),
) -> str | None:
    """把方案的场景 / 姿势 / 角度并进提示词。

    ## 为什么走提示词,而不是给 `GenerationRequest` 加字段

    `GenerationTask` 现有 32 列里没有 scene / pose / angles,而本轮**禁止加列**
    (PRD §2)。同样重要的是:出图 Provider(FASHN / ComfyUI / mock)的请求里
    也没有"角度"这个参数 —— 对一个文生图 / 试穿接口来说,"要正面和背面"
    这件事**只能**经由提示词表达。所以这不是一条绕路,它就是那条路。

    追溯靠已有的 `generation_plan_id` + `plan_fingerprint` 两列:
    "这批图是按哪份方案出的"答得出来,而那正是那两列该有的用途。

    ## 三条写法上的讲究

    1. **调用方的提示词排在最前**,方案的补充跟在后面。反过来的话,
       运营特意写的那句话会被一串固定后缀推到模型注意力的末尾。
    2. **空值一律跳过,不留下"场景:"这种空标签。** 空标签会被模型
       当成一个真实约束去满足,而它背后什么都没有。
    3. **角度按 `ImageAngle` 定义序输出**(`normalize_angles` 已经排好),
       不按输入序 —— 否则同一份方案换个键序就得到不同的提示词,
       而提示词进幂等键。

    全空时返回**原样**(含 None),不返回空串:None 与 "" 在
    `build_idempotency_key` 里被归一成同一个值,但在 `GenerationTask.prompt`
    列上不是 —— 一个是"没写",一个是"写了个空的"。
    """
    parts: list[str] = []
    head = _text(base_prompt)
    if head:
        parts.append(head)
    scene_text = _text(scene)
    if scene_text:
        parts.append(f"场景:{scene_text}")
    pose_text = _text(pose)
    if pose_text:
        parts.append(f"姿势:{pose_text}")
    wanted = tuple(angles)
    if wanted:
        parts.append(
            "拍摄角度:" + "、".join(f"{a.angle}×{a.count}" for a in wanted)
        )
    if not parts:
        return base_prompt if base_prompt is None else head
    return ";".join(parts)


# ---------------------------------------------------------------- 小工具


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _text_or_none(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money(value: Any) -> str:
    """金额进指纹时的规范形式。

    `10`、`10.0`、`"10.00"` 必须编成同一个串,否则同一份方案在
    "从接口传进来"和"从库里读回来"两条路上会算出两个指纹 ——
    而那两条路恰好是创建任务与判过期各走一条。
    """
    decimal = _decimal(value)
    return "" if decimal is None else format(decimal.normalize(), "f")
