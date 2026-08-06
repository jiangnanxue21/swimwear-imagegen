"""发布页的状态派生(方案 v4.1 任务 18 / 4.1 节 F / 6.4 节)。

## 为什么必须有这一层

CLAUDE.md 硬规则 4:**前端不许推测状态。** 后端返回 `display_status` /
`next_action` / `blocking_reasons` / `allowed_actions`,前端只展示和触发。

发布链路是这条规则最容易破的地方,因为「现在到底怎么样了」散在四处:

    ChannelListing.status     商品在渠道上的**当前事实**
    PublishAttempt.status     最近一次尝试的**结局**(历史,写完不改)
    PublishOutbox.status      还会不会**自动再投**
    PlatformRejection         平台驳回了没有、关掉了没有

让前端自己拼这四样,拼错的方式很多,而最贵的那种已经写在
`EnqueueResult.reused_terminal` 的注释里:一次退避耗尽落进 DEAD 的提交,
listing 仍然写着 SUBMITTING、attempt 仍然写着 IN_FLIGHT —— 只看这两样的界面
会说「正在处理」,而实际上什么都不会再发生。**运营会一直等一件已经死掉的事。**

那不是一个能靠前端小心一点避免的问题:四个来源里没有任何一个单独知道
"这件事死了",结论只在**组合**里。所以判定必须在后端做一次,并且做在
一个能被穷举的地方。

## 为什么放在 workflows/ 而不是 services/

这里零依赖(只碰 `core.enums` / `core.errors`),因此能在 `tests/pure/` 里
被穷举 —— 而穷举正是"某个状态组合下按钮不该亮"这类问题唯一能被覆盖的方式。
同 `publish_policy` / `poll_policy`:纯判定和事务编排分开,理由见那两个模块顶部。

## 一条硬规矩:不许为了接口形状完整填常量

CLAUDE.md 硬规则 4 的后半句。本模块每个输出字段都必须能从 `PublishFacts`
的某个取值推出来;`PublishFacts` 的每个字段都必须来自真实的库内行。
反面教材是 `describe_extractors()` 曾经硬编码 `configured: true`。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.core.enums import (
    PublishAttemptStatus,
    PublishOutboxStatus,
    PublishStatus,
)


class PublishDisplayStatus(StrEnum):
    """运营看到的发布状态。**与 `PublishStatus` 刻意不是一一对应。**

    `PublishStatus` 有 24 个取值,它服务的是状态机;运营需要区分的没有那么多,
    但**多出**了一个状态机里根本不存在的取值:`STALLED`。

    多出来的那个才是这个枚举存在的理由。如果只是给 24 个状态换个中文名,
    直接做一张文案表就够了,不需要一个判定模块。
    """

    NOT_SUBMITTED = "NOT_SUBMITTED"          # 还没提交过
    NEEDS_FIX = "NEEDS_FIX"                  # 校验没过,改了才能提交
    DRY_RUN_DONE = "DRY_RUN_DONE"            # 干跑完成 —— 没有发出去任何东西
    QUEUED = "QUEUED"                        # 已排队,等投递
    IN_FLIGHT = "IN_FLIGHT"                  # 正在发
    PLATFORM_REVIEWING = "PLATFORM_REVIEWING"  # 平台处理中
    LISTED = "LISTED"                        # 已上架
    PLATFORM_REJECTED = "PLATFORM_REJECTED"  # 平台驳回
    SUBMIT_FAILED = "SUBMIT_FAILED"          # 提交失败(平台明确拒绝)
    RESULT_UNKNOWN = "RESULT_UNKNOWN"        # 超时,不知道平台收到没有
    #: 不会再有任何自动进展了。**状态机里没有这个状态** —— 它是
    #: 「listing 说在途 + outbox 说不再投」这个组合的名字。见模块顶部
    STALLED = "STALLED"
    DELISTING = "DELISTING"                  # 下架中
    DELISTED = "DELISTED"                    # 已下架
    PAUSED = "PAUSED"                        # 已暂停
    CLOSED = "CLOSED"                        # 已取消 / 已归档


DISPLAY_STATUS_LABELS: Mapping[str, str] = {
    PublishDisplayStatus.NOT_SUBMITTED: "未提交",
    PublishDisplayStatus.NEEDS_FIX: "待修复",
    PublishDisplayStatus.DRY_RUN_DONE: "干跑完成(未提交)",
    PublishDisplayStatus.QUEUED: "已排队",
    PublishDisplayStatus.IN_FLIGHT: "提交中",
    PublishDisplayStatus.PLATFORM_REVIEWING: "平台审核中",
    PublishDisplayStatus.LISTED: "已上架",
    PublishDisplayStatus.PLATFORM_REJECTED: "平台驳回",
    PublishDisplayStatus.SUBMIT_FAILED: "提交失败",
    PublishDisplayStatus.RESULT_UNKNOWN: "结果未知",
    PublishDisplayStatus.STALLED: "已停住,不会自动继续",
    PublishDisplayStatus.DELISTING: "下架中",
    PublishDisplayStatus.DELISTED: "已下架",
    PublishDisplayStatus.PAUSED: "已暂停",
    PublishDisplayStatus.CLOSED: "已关闭",
}


class ActionCode(StrEnum):
    """界面上真的存在的按钮。**每一个都对应 `api/publish.py` 里的一个端点。**

    这个约束不是洁癖:`allowed_actions` 一旦出现一个没有端点的动作,
    前端会渲染出一个点了报 404 的按钮,而那种错误只有运营会碰到。
    `tests/pure/test_publish_view.py` 里有一条用例按端点表核对这个枚举。
    """

    SUBMIT = "SUBMIT"      # POST /publish/products/{product_id}/submit
    DRY_RUN = "DRY_RUN"    # 同上,dry_run=true
    POLL = "POLL"          # POST /publish/listings/{listing_id}/poll
    DELIST = "DELIST"      # POST /publish/listings/{listing_id}/delist
    # ---- A45-batch12-2 / EX-04:两条恢复动作 ----
    #
    # 它们补的是同一个洞:CREATE 结果未知、或者退避耗尽落 DEAD 之后,
    # **界面上一个可点的东西都没有**。`reconcile_unknown()` 在服务层躺了
    # 很久,全仓唯一的调用点是一条数据库测试;DEAD 则连服务函数都没有。
    #
    # 没有出口的后果不是"少一个按钮":运营要把这件商品弄上架,只能去改草稿、
    # 换输入,从而**制造一把新的幂等键** —— 而那正是真正会在平台上多出一件
    # 商品的操作。闸门没有出口时,人会自己找一条更危险的路绕过去。
    RECONCILE = "RECONCILE"    # POST /publish/listings/{listing_id}/reconcile
    REDELIVER = "REDELIVER"    # POST /publish/listings/{listing_id}/redeliver


ACTION_LABELS: Mapping[str, str] = {
    ActionCode.SUBMIT: "提交",
    ActionCode.DRY_RUN: "干跑",
    ActionCode.POLL: "刷新状态",
    ActionCode.DELIST: "下架",
    # 措辞刻意不叫"重试":那两个字会让人以为点了就重新提交一次。
    # 这两个动作都**复用原幂等键**,不会创建第二件商品,而说清这一点
    # 正是它们敢被放在界面上的前提
    ActionCode.RECONCILE: "去平台核对结果",
    ActionCode.REDELIVER: "重新投递",
}


class NextActionCode(StrEnum):
    """推荐动作。**同一时刻只会有一个。**

    比 `ActionCode` 多三个不是按钮的取值。少了它们的话,「等着就行」和
    「需要人去查」会被迫共用一个"没有按钮可点"的表示,而这两件事对运营
    完全不同:前者不用管,后者不管就烂在那儿。
    """

    SUBMIT = "SUBMIT"                        # 去提交
    FIX_DRAFT = "FIX_DRAFT"                  # 去修草稿
    POLL = "POLL"                            # 刷新一下看看
    RESOLVE_REJECTION = "RESOLVE_REJECTION"  # 去工作台处理驳回
    INVESTIGATE = "INVESTIGATE"              # 需要人工核实,没有按钮
    # A45-batch12-2 / EX-04:这两条以前都被压进 INVESTIGATE,而那个值的
    # 含义是「需要人工核实,**没有按钮**」。现在有按钮了,继续说没有
    # 等于把刚接出来的出口藏起来 —— 界面会照着 next_action 提示运营
    RECONCILE = "RECONCILE"                  # 带原幂等键去平台确认
    REDELIVER = "REDELIVER"                  # 带原幂等键重新投递
    WAIT = "WAIT"                            # 等平台,不用管
    NONE = "NONE"                            # 没有待办


NEXT_ACTION_LABELS: Mapping[str, str] = {
    NextActionCode.SUBMIT: "提交上架",
    NextActionCode.FIX_DRAFT: "修复草稿后再提交",
    NextActionCode.POLL: "刷新平台状态",
    NextActionCode.RESOLVE_REJECTION: "处理平台驳回",
    NextActionCode.INVESTIGATE: "人工核实后再操作",
    NextActionCode.RECONCILE: "去平台核对这次提交的结果",
    NextActionCode.REDELIVER: "确认平台可用后重新投递",
    NextActionCode.WAIT: "等待平台处理",
    NextActionCode.NONE: "无待办",
}


class BlockerCode(StrEnum):
    """阻断原因。出现任何一条,`next_action` 都不会是 `SUBMIT`。"""

    DRAFT_NOT_SUBMITTABLE = "DRAFT_NOT_SUBMITTABLE"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    DELIVERY_EXHAUSTED = "DELIVERY_EXHAUSTED"
    UNRESOLVED_REJECTION = "UNRESOLVED_REJECTION"
    #: 任务 20 的缺口。见下方 `_blockers()` 里的长注释 —— 这一条不是
    #: 业务规则,是一个**已知还没修的洞**,如实报出来而不是假装没有
    REJECTION_CANNOT_AUTO_CLOSE = "REJECTION_CANNOT_AUTO_CLOSE"


BLOCKER_LABELS: Mapping[str, str] = {
    BlockerCode.DRAFT_NOT_SUBMITTABLE: "草稿当前状态不允许提交",
    BlockerCode.RESULT_UNKNOWN: "上次提交结果未知,平台可能已经收到",
    BlockerCode.DELIVERY_EXHAUSTED: "投递重试已用尽,需要人工介入",
    BlockerCode.UNRESOLVED_REJECTION: "有未解决的平台驳回",
    BlockerCode.REJECTION_CANNOT_AUTO_CLOSE: "这条驳回系统关不掉,只能人工标记",
}

BLOCKER_ADVICE: Mapping[str, str] = {
    BlockerCode.DRAFT_NOT_SUBMITTABLE: "回工作台把草稿改到「已校验」或「已批准」",
    BlockerCode.RESULT_UNKNOWN: "先刷新状态确认平台到底有没有收到,不要直接重发",
    BlockerCode.DELIVERY_EXHAUSTED: "查异常队列里那一行的错误,处理完再重新提交",
    BlockerCode.UNRESOLVED_REJECTION: "按驳回原因改内容,改完在工作台关闭这条驳回",
    BlockerCode.REJECTION_CANNOT_AUTO_CLOSE: (
        "API 自动上架不产生导出留痕,而关闭闸门认的证据是导出。"
        "改完之后在工作台手工标记解决(任务 20 会把「重新提交」也算作证据)"
    ),
}


@dataclass(frozen=True)
class Blocker:
    code: BlockerCode
    label: str
    advice: str


@dataclass(frozen=True)
class PublishFacts:
    """派生所需的全部输入。**每个字段都来自一行真实的库内数据。**

    做成一个扁平的普通数据结构,而不是直接传 ORM 对象,有两个原因:
    一是 `tests/pure/` 里造不出 ORM 对象(那里连 sqlalchemy 都没装);
    二是这样才看得见这个判定到底依赖多少东西 —— 传 ORM 对象的话,
    "又多读了一列"这件事在 diff 里是不可见的。
    """

    #: `ChannelListing.status`。还没有 listing 行时传 None
    listing_status: str | None = None
    #: 拿到过外部商品 ID 就说明那次提交**真的到达过平台**。
    #: 它是「能不能下架」「能不能轮询」的前提,不是装饰字段
    external_spu_id: str | None = None
    #: 外部 ID 是 Simulator 造的 —— 真实平台上并不存在这个商品
    simulated: bool = False
    #: 草稿能不能提交(`publish_service.SUBMITTABLE_DRAFT_STATUSES`)。
    #: 由调用方判定后传进来,本模块不重新实现一遍那条规则
    draft_submittable: bool = False
    #: 最近一次尝试的状态(`PublishAttemptStatus`)
    attempt_status: str | None = None
    #: 那次尝试对应的 outbox 行状态(`PublishOutboxStatus`)
    outbox_status: str | None = None
    #: 未解决的驳回条数(`UNRESOLVED_REJECTION_STATUSES` 口径,含
    #: FIXED_PENDING_EXPORT —— 运营点过"我改好了"仍然算未解决)
    unresolved_rejections: int = 0
    #: 这些未解决的驳回里,系统有没有可能自己看到"已修复"的证据。
    #: None = 没有未解决的驳回,这个问题不适用
    rejection_auto_closeable: bool | None = None


@dataclass(frozen=True)
class PublishView:
    """派生结果。**这就是接口返回给前端的那四个字段。**"""

    display_status: PublishDisplayStatus
    display_status_label: str
    next_action: NextActionCode
    next_action_label: str
    blocking_reasons: tuple[Blocker, ...]
    allowed_actions: tuple[ActionCode, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "display_status": self.display_status.value,
            "display_status_label": self.display_status_label,
            "next_action": self.next_action.value,
            "next_action_label": self.next_action_label,
            "blocking_reasons": [
                {"code": b.code.value, "label": b.label, "advice": b.advice}
                for b in self.blocking_reasons
            ],
            "allowed_actions": [a.value for a in self.allowed_actions],
        }


#: `PublishStatus` -> 默认展示状态。**必须覆盖枚举的每一个取值。**
#:
#: 用一张全表而不是 if/elif 级联,是为了让"新加一个 PublishStatus 却忘了
#: 决定它怎么显示"这件事可以被一条测试抓住(见 test_publish_view.py)。
#: 级联写法在这种情况下会静悄悄走进 else 分支,而 else 分支给出的答案
#: 一定是错的 —— 它是为**已知**状态准备的兜底。
BASE_DISPLAY: Mapping[str, PublishDisplayStatus] = {
    PublishStatus.DRAFT.value: PublishDisplayStatus.NOT_SUBMITTED,
    PublishStatus.MAPPING.value: PublishDisplayStatus.NOT_SUBMITTED,
    PublishStatus.VALIDATING.value: PublishDisplayStatus.NOT_SUBMITTED,
    PublishStatus.VALIDATED.value: PublishDisplayStatus.NOT_SUBMITTED,
    PublishStatus.VALIDATION_FAILED.value: PublishDisplayStatus.NEEDS_FIX,
    PublishStatus.REVISING.value: PublishDisplayStatus.NEEDS_FIX,
    PublishStatus.DRY_RUN_VALIDATED.value: PublishDisplayStatus.DRY_RUN_DONE,
    PublishStatus.DRY_RUN_COMPLETED.value: PublishDisplayStatus.DRY_RUN_DONE,
    PublishStatus.UPLOADING_IMAGES.value: PublishDisplayStatus.IN_FLIGHT,
    PublishStatus.SUBMITTING.value: PublishDisplayStatus.IN_FLIGHT,
    PublishStatus.SUBMITTED.value: PublishDisplayStatus.PLATFORM_REVIEWING,
    PublishStatus.SUBMIT_RESULT_UNKNOWN.value: PublishDisplayStatus.RESULT_UNKNOWN,
    PublishStatus.SUBMIT_FAILED.value: PublishDisplayStatus.SUBMIT_FAILED,
    PublishStatus.PLATFORM_REVIEWING.value: PublishDisplayStatus.PLATFORM_REVIEWING,
    PublishStatus.PLATFORM_REJECTED.value: PublishDisplayStatus.PLATFORM_REJECTED,
    PublishStatus.LISTED.value: PublishDisplayStatus.LISTED,
    PublishStatus.UPDATING.value: PublishDisplayStatus.IN_FLIGHT,
    PublishStatus.UPDATE_FAILED.value: PublishDisplayStatus.SUBMIT_FAILED,
    PublishStatus.PAUSED.value: PublishDisplayStatus.PAUSED,
    PublishStatus.DELISTING.value: PublishDisplayStatus.DELISTING,
    PublishStatus.DELISTED.value: PublishDisplayStatus.DELISTED,
    PublishStatus.REPUBLISHING.value: PublishDisplayStatus.IN_FLIGHT,
    PublishStatus.CANCELLED.value: PublishDisplayStatus.CLOSED,
    PublishStatus.ARCHIVED.value: PublishDisplayStatus.CLOSED,
}

#: 「还在路上」的展示状态。`STALLED` 的判定只在这几个状态上做 ——
#: 一条 DEAD 的 outbox 配一个 LISTED 的 listing 不是停住,那是**已经成了**
#: (投递成功之后 outbox 也可能因为后续更新失败而 DEAD)。
_IN_PROGRESS: frozenset[PublishDisplayStatus] = frozenset(
    {
        PublishDisplayStatus.QUEUED,
        PublishDisplayStatus.IN_FLIGHT,
        PublishDisplayStatus.DELISTING,
    }
)

#: 不会再有自动进展的尝试状态。与 `publish_service.TERMINAL_ATTEMPT_STATUSES`
#: 同口径,但**故意不 import 它** —— 那个模块要 sqlalchemy,一 import
#: 本模块就进不了 `tests/pure/`。两处必须一致,由
#: `test_terminal_attempt_statuses_agree_with_the_service` 钉着。
_TERMINAL_ATTEMPTS: frozenset[str] = frozenset(
    {
        PublishAttemptStatus.FAILED.value,
        PublishAttemptStatus.UNKNOWN.value,
        PublishAttemptStatus.ABORTED.value,
    }
)


def _display(facts: PublishFacts) -> PublishDisplayStatus:
    """先查表,再按跨来源的组合修正。修正只有两处,都写在下面。"""
    if not facts.listing_status:
        # 连 listing 行都没有 = 从没走到过发布这一步
        return PublishDisplayStatus.NOT_SUBMITTED

    base = BASE_DISPLAY.get(facts.listing_status)
    if base is None:
        # 查不到只可能是有人加了 PublishStatus 没加表项。**不猜**:
        # 猜一个"大概是在途吧"会让界面给出一个自信而错误的答案,
        # 而 NEEDS_FIX 至少会把人引到能看见原始状态的地方
        return PublishDisplayStatus.NEEDS_FIX

    # 修正一:排队中还是发出去了。listing 两者都记 SUBMITTING(见
    # PublishStatus.DELISTING 的注释:队列这件事的权威表示是 outbox 行),
    # 所以这个区分只能在这里做
    if base is PublishDisplayStatus.IN_FLIGHT:
        if facts.outbox_status == PublishOutboxStatus.PENDING.value:
            base = PublishDisplayStatus.QUEUED

    # 修正二:**本模块存在的理由。** listing 说在途,而 outbox 说不会再投了
    # (或者那次尝试自己已经是终态),那就不是在途 —— 是停住了。
    # 少了这一步,界面会说"提交中"直到有人手工去翻库
    if base in _IN_PROGRESS and _stalled(facts):
        return PublishDisplayStatus.STALLED
    return base


#: Outbox 还会自动往前走的状态。**判定链路死没死时,这一档优先于一切。**
#:
#: `PENDING` 是等下一次投递,`LEASED` 是已经被某个 worker 领走了 —— 两者
#: 都意味着"后台会继续处理这一行",而这正是 `STALLED` 的反面。
_ACTIVE_OUTBOX: frozenset[str] = frozenset(
    {
        PublishOutboxStatus.PENDING.value,
        PublishOutboxStatus.LEASED.value,
    }
)


def _stalled(facts: PublishFacts) -> bool:
    """这条链路还会不会自己往前走。

    ## 活跃 Outbox 一票否决(A45-batch12-3 / REG-05)

    先看 outbox 还在不在投递路径上。在的话直接判"没停" —— 不管上一次
    attempt 落在什么状态。

    这一条是 `redeliver_dead()` 逼出来的。重新投递之后库里长这样:

        Listing  SUBMITTING
        Outbox   PENDING        <- worker 马上就会取
        Attempt  FAILED         <- 上一次的结局,历史,写完不改

    只看后两样的话 `attempt_status in _TERMINAL_ATTEMPTS` 成立,页面显示
    `STALLED` + `INVESTIGATE`,并且因为不在 `hard_stops` 里还会**放开普通
    SUBMIT 按钮**。于是运营看到的是"重新投递没生效",然后点普通提交 ——
    而那条路会算出一把新的幂等键,也就是真的可能在平台上多出一件商品。
    重新投递给的出口,反被界面推回了它想避开的那条路。

    `PublishAttempt` 是历史,`PublishOutbox` 是**当下还会不会发**。问
    "会不会自己往前走"时,权威来源只能是后者。

    ## 没有活跃 Outbox 时,两个来源任意一个说"到头了"就算到头

    outbox DEAD 表示不会再投,attempt 落在终态表示那一次已经有结论了。
    取**或**不取且,是因为这两件事在时间上不同步 —— 投递失败先写 attempt,
    再决定 outbox 要不要重试,中间那一瞬只有一边是终态。
    """
    if facts.outbox_status in _ACTIVE_OUTBOX:
        return False
    if facts.outbox_status == PublishOutboxStatus.DEAD.value:
        return True
    return facts.attempt_status in _TERMINAL_ATTEMPTS


def _blockers(facts: PublishFacts) -> tuple[Blocker, ...]:
    codes: list[BlockerCode] = []

    if not facts.draft_submittable:
        codes.append(BlockerCode.DRAFT_NOT_SUBMITTABLE)

    if facts.listing_status == PublishStatus.SUBMIT_RESULT_UNKNOWN.value:
        # 4.1 节 D「API 超时后的未知结果可恢复」。这一条阻断的是**重发**:
        # 平台可能已经收到了,直接重发就是在赌幂等键真的被平台实现了
        codes.append(BlockerCode.RESULT_UNKNOWN)

    if facts.outbox_status == PublishOutboxStatus.DEAD.value:
        codes.append(BlockerCode.DELIVERY_EXHAUSTED)

    if facts.unresolved_rejections > 0:
        codes.append(BlockerCode.UNRESOLVED_REJECTION)
        # 任务 20 的缺口,如实报出来。
        #
        # `resolve_gate()` 认的"驳回已修复"证据是**驳回之后有一次新的导出**,
        # 而 API 自动上架的商品根本不走导出 —— 于是驳回记得进去、关不掉。
        # 现在的处置是人在工作台手工标记解决。
        #
        # 不报的话,界面上会出现一条"改完就能关"的暗示,而运营改完之后
        # 会发现关不掉,然后开始怀疑是自己操作错了。把已知的洞报出来,
        # 比让人自己撞上去便宜 —— 这也是硬规则 4 后半句的直接后果:
        # 状态字段必须追溯到真实来源,而这里的真实来源就是"闸门关不上"
        if facts.rejection_auto_closeable is False:
            codes.append(BlockerCode.REJECTION_CANNOT_AUTO_CLOSE)

    return tuple(
        Blocker(code=c, label=BLOCKER_LABELS[c], advice=BLOCKER_ADVICE[c])
        for c in codes
    )


def _allowed(facts: PublishFacts, display: PublishDisplayStatus) -> tuple[ActionCode, ...]:
    """哪些按钮该亮。

    干跑单列出来:它**不发任何东西**,所以草稿状态之外没有别的前提,
    连"上次提交结果未知"都不该挡住它 —— 那正是运营想先看一眼报文的时候。
    """
    actions: list[ActionCode] = []

    if facts.draft_submittable:
        actions.append(ActionCode.DRY_RUN)

    # 能提交的条件:草稿可提交,且没有"必须先搞清楚"的阻断。
    # 未解决的驳回**不挡提交** —— 改完内容重新提交正是解决驳回的方式,
    # 挡住它等于告诉运营"你得先关掉驳回才能修驳回"
    hard_stops = {
        PublishDisplayStatus.RESULT_UNKNOWN,
        PublishDisplayStatus.IN_FLIGHT,
        PublishDisplayStatus.QUEUED,
        PublishDisplayStatus.DELISTING,
    }
    exhausted = facts.outbox_status == PublishOutboxStatus.DEAD.value
    if facts.draft_submittable and display not in hard_stops and not exhausted:
        actions.append(ActionCode.SUBMIT)

    # 轮询和下架都要求**真的到达过平台**。没有外部 ID 时这两个按钮点了
    # 也是空转:`poll_one` 会直接返回 unanswered,下架没有目标
    if (facts.external_spu_id or "").strip():
        actions.append(ActionCode.POLL)
        if display not in {PublishDisplayStatus.DELISTED, PublishDisplayStatus.CLOSED}:
            actions.append(ActionCode.DELIST)

    # ---- A45-batch12-2 / EX-04:两条恢复动作 ----
    #
    # 判据是**这条链路有没有停住,以及停在哪一种不确定上**,不是 display:
    # display 会把两种停住合成一个 STALLED,而它们的恢复方式相反。
    #
    #     attempt = UNKNOWN   不知道到没到 -> 带原键去确认(RECONCILE)
    #     outbox  = DEAD      明确知道没到 -> 带原键重新投递(REDELIVER)
    #
    # 两者可能同时成立(一次 UNKNOWN 之后 outbox 也被判死)。此时两个按钮
    # 都给出来是对的:先确认再决定要不要重投,正是那种情况下该走的顺序。
    if facts.attempt_status == PublishAttemptStatus.UNKNOWN.value:
        actions.append(ActionCode.RECONCILE)
    if facts.outbox_status == PublishOutboxStatus.DEAD.value:
        actions.append(ActionCode.REDELIVER)

    return tuple(actions)


def _next_action(
    facts: PublishFacts,
    display: PublishDisplayStatus,
    blockers: tuple[Blocker, ...],
    allowed: tuple[ActionCode, ...],
) -> NextActionCode:
    """一件事:现在最该做什么。**顺序即优先级,不要随便调。**"""
    codes = {b.code for b in blockers}

    # 停住的链路排第一。它是唯一一个"看起来在动、其实不会动"的情况,
    # 而其它每一条建议都默认系统还在往前走
    # A45-batch12-2 / EX-04:能修的排在"去查一下"前面。
    #
    # 这两条以前都落到 INVESTIGATE("需要人工核实,没有按钮")。那句话在
    # 端点接出来之前是准确的,现在不是了 —— 而一个说着"没有按钮"的提示
    # 会让运营根本不去找那个按钮,于是新出口等于没接。
    #
    # 顺序:先确认再重投。UNKNOWN 时重投是在赌平台真的实现了幂等键,
    # 而确认这件事本身就能回答"到底要不要重投"
    if ActionCode.RECONCILE in allowed:
        return NextActionCode.RECONCILE
    if ActionCode.REDELIVER in allowed:
        return NextActionCode.REDELIVER
    if display is PublishDisplayStatus.STALLED:
        return NextActionCode.INVESTIGATE
    if display is PublishDisplayStatus.RESULT_UNKNOWN:
        # 先查,别重发。理由见 BLOCKER_ADVICE[RESULT_UNKNOWN]
        return NextActionCode.POLL
    if display is PublishDisplayStatus.PLATFORM_REJECTED:
        return NextActionCode.RESOLVE_REJECTION
    if BlockerCode.DELIVERY_EXHAUSTED in codes:
        return NextActionCode.INVESTIGATE
    if display in {PublishDisplayStatus.QUEUED, PublishDisplayStatus.IN_FLIGHT,
                   PublishDisplayStatus.DELISTING}:
        return NextActionCode.WAIT
    if display is PublishDisplayStatus.PLATFORM_REVIEWING:
        # 平台在审。能做的只有刷新 —— 而刷新是真有用的:轮询有退避,
        # 人点一下比等下一次定时扫描快
        return NextActionCode.POLL
    if BlockerCode.DRAFT_NOT_SUBMITTABLE in codes:
        return NextActionCode.FIX_DRAFT
    if ActionCode.SUBMIT in allowed and display in {
        PublishDisplayStatus.NOT_SUBMITTED,
        PublishDisplayStatus.DRY_RUN_DONE,
        PublishDisplayStatus.SUBMIT_FAILED,
        PublishDisplayStatus.DELISTED,
    }:
        return NextActionCode.SUBMIT
    if facts.unresolved_rejections > 0:
        return NextActionCode.RESOLVE_REJECTION
    return NextActionCode.NONE


def build_view(facts: PublishFacts) -> PublishView:
    """四个来源的取值 -> 界面要的四个字段。**接口层只调这一个函数。**"""
    display = _display(facts)
    blockers = _blockers(facts)
    allowed = _allowed(facts, display)
    nxt = _next_action(facts, display, blockers, allowed)
    return PublishView(
        display_status=display,
        display_status_label=DISPLAY_STATUS_LABELS[display],
        next_action=nxt,
        next_action_label=NEXT_ACTION_LABELS[nxt],
        blocking_reasons=blockers,
        allowed_actions=allowed,
    )


# ================================================================ 排队结果的措辞


class EnqueueNoticeCode(StrEnum):
    """一次提交请求的**真实结局**。四种,不是两种。"""

    QUEUED = "QUEUED"                  # 新建了尝试,排上了
    DRY_RUN = "DRY_RUN"                # 构造了报文,没有发
    ALREADY_IN_PROGRESS = "ALREADY_IN_PROGRESS"  # 沿用了在途的那一条
    #: 沿用的那一条**已经死了**。见下方 `describe_enqueue()` 的注释
    ALREADY_STALLED = "ALREADY_STALLED"


@dataclass(frozen=True)
class EnqueueNotice:
    code: EnqueueNoticeCode
    message: str
    #: 这次调用之后,系统会不会真的去发一次请求。
    #: 前端据此决定要不要开始轮询进度 —— 对着一件不会发生的事轮询,
    #: 转圈会一直转下去
    will_deliver: bool


def describe_enqueue(*, reused: bool, reused_terminal: bool, dry_run: bool) -> EnqueueNotice:
    """`EnqueueResult` 的三个布尔 -> 给运营看的一句话。

    ## 为什么 `reused_terminal` 必须在这里被读到

    A27 交接把它列成了本轮必须补的缺口:字段算得对、位置也对,但**没有
    任何读取点**。而它要防的结局非常具体 —— 幂等键含草稿指纹,草稿不变
    键就不变,于是一条退避耗尽落进 DEAD 的提交,运营再点一次「提交」
    仍然走进 `_reuse`,`reused=True` 照旧。只看 `reused` 的界面会说
    「已提交,正在处理」,而实际上什么都没发生,而且**永远**不会发生。

    所以这里把"沿用"拆成两句话,并且用 `will_deliver` 明确回答
    "接下来会不会真的发一次" —— 那是前端唯一需要的那一位信息。
    """
    if dry_run:
        return EnqueueNotice(
            code=EnqueueNoticeCode.DRY_RUN,
            message="已构造报文,未发送。检查无误后再正式提交。",
            will_deliver=False,
        )
    if reused and reused_terminal:
        return EnqueueNotice(
            code=EnqueueNoticeCode.ALREADY_STALLED,
            message=(
                "这次提交沿用了同一份内容上一次的尝试,而那次尝试已经结束且不会再重试。"
                "**再点提交不会有任何变化** —— 先处理上一次的失败原因,"
                "或者改动草稿内容(内容变了才会算作一次新的提交)。"
            ),
            will_deliver=False,
        )
    if reused:
        return EnqueueNotice(
            code=EnqueueNoticeCode.ALREADY_IN_PROGRESS,
            message="这份内容已经提交过,正在处理中。没有重复发送。",
            will_deliver=True,
        )
    return EnqueueNotice(
        code=EnqueueNoticeCode.QUEUED,
        message="已排队,稍后自动提交。",
        will_deliver=True,
    )


__all__ = [
    "ACTION_LABELS",
    "BLOCKER_ADVICE",
    "BLOCKER_LABELS",
    "DISPLAY_STATUS_LABELS",
    "NEXT_ACTION_LABELS",
    "BASE_DISPLAY",
    "ActionCode",
    "Blocker",
    "BlockerCode",
    "EnqueueNotice",
    "EnqueueNoticeCode",
    "NextActionCode",
    "PublishDisplayStatus",
    "PublishFacts",
    "PublishView",
    "build_view",
    "describe_enqueue",
]
