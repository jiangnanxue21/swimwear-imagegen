"""平台状态轮询的判定(方案 v4.1 任务 15 / 4.1 节 D / 7.3 节)。

和 `publish_policy.py` 的分工是「写」与「读」:

    publish_policy   我们**提交**了一次,平台的那个响应算什么
    poll_policy      我们**问**了一次「那个商品现在怎么样」,答案算什么

分成两个模块而不是往 `publish_policy` 里再加一节,理由是这两件事的失败语义
完全相反。提交失败要重试、要退避、要在次数用尽时放弃;轮询失败**什么都不该做** ——
本地状态原样不动,等下一轮再问。合在一起的话,「用尽次数后落一个有结论的状态」
那套逻辑会很自然地被套到轮询上,而那正是本模块要防的头号错误(见
`classify_poll` 的「问不到 ≠ 状态变了」)。

放在 workflows 层的理由同 `dispatch_policy.py` / `publish_policy.py`:
**这些是判断,不是动作**。只有标准库和枚举,没有 session、没有网络。

## 平台状态的翻译点仍然只有一个

`PLATFORM_STATUS_MAP` 不在这里重写一份,而是从 `publish_policy` 引进来。
两处各存一份的下场是「平台说 REVIEWING 我们记成什么」有两个答案,而它们
会在某一次单边修改之后开始分叉 —— 提交路径记 PLATFORM_REVIEWING、轮询路径
记 SUBMITTED,同一个商品的状态取决于最后一次是谁写的。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.enums import PublishStatus, RejectCategory
from app.core.errors import ErrorCode
from app.workflows.publish_policy import PLATFORM_STATUS_MAP

#: 还需要继续问平台的本地状态。
#:
#: 判据是「平台侧还可能自己发生变化」,不是「我们还没看到最终结果」:
#:
#:     SUBMITTED              平台收下了,还没告诉我们审核结论
#:     PLATFORM_REVIEWING     明确在审核
#:     UPDATING / REPUBLISHING / DELISTING   我们发起的动作还没被平台确认
#:     SUBMIT_RESULT_UNKNOWN  提交时超时了。**有 external_spu_id 时**问一次
#:                            就能问出结论 —— 这正是 7.4 节「先查询平台」的
#:                            那条路径(另一条是带同一个幂等键去确认,
#:                            由 `publish_service.reconcile_unknown` 走)
#:
#: `LISTED` 不在里面,而这是一个需要说清楚的取舍:上架之后平台仍然可能
#: 单方面下架一个商品。持续轮询全部在架商品的代价随上架量线性增长,而收益
#: 只在极少数情况下出现 —— 首期不做,靠 4.1 节 H 的清单核对(`cleanup_service`
#: 会显式轮询,不看这张表)兜住"平台上到底还剩什么"这个问题。
#: `PLATFORM_REJECTED` 同理不在里面:它等的是人,不是平台。
POLLABLE_STATUSES: frozenset[str] = frozenset(
    {
        PublishStatus.SUBMITTED.value,
        PublishStatus.PLATFORM_REVIEWING.value,
        PublishStatus.UPDATING.value,
        PublishStatus.REPUBLISHING.value,
        PublishStatus.DELISTING.value,
        PublishStatus.SUBMIT_RESULT_UNKNOWN.value,
    }
)

#: 两次轮询之间最短等多久。比 Simulator 的审核周期(60 秒)短一截,
#: 否则人工测试时"审核中"这个中间态会被整段跳过,而前端那条分支就又变成
#: 写好了没被走过的状态。
MIN_POLL_INTERVAL_SECONDS = 20

#: 退避上限。**轮询不封顶到放弃,只封顶到稀疏**,理由见 `poll_backoff_seconds`。
MAX_POLL_INTERVAL_SECONDS = 3600


@dataclass(frozen=True)
class RejectionSignal:
    """平台在轮询里告诉我们的一次驳回。

    `category` 用 `RejectCategory` 而不是平台原话:驳回回流要回答的问题是
    「该去哪个标签页改」,而平台原话回答不了这个 —— 它是给人看的。原话另存
    `note`,一字不改地贴进 `PlatformRejection.platform_note`。
    """

    category: RejectCategory
    code: str | None
    note: str | None


@dataclass(frozen=True)
class PollOutcome:
    """问了一次的结论。**只描述该记什么,不描述怎么记。**

    `listing_status` 为 None 是这个类里最重要的一个取值,含义是**别动本地状态**。
    轮询失败、平台说了个我们不认识的状态、平台暂时不可达 —— 三种都归它。
    把「没问到」和「问到了、还是老样子」合并成同一个表示,第一种就会悄悄
    推进或回退状态机,而那种错误没有任何征兆。
    """

    #: 平台原话,照存不翻译(落 `ChannelListing.last_platform_status`)
    platform_status: str | None = None
    #: None = **不要动** listing 的本地状态
    listing_status: str | None = None
    rejection: RejectionSignal | None = None
    #: True = 这一次真的拿到了平台的答复(而不是网络层或平台自身出错)
    answered: bool = False
    #: True = 自动流程推不动了,要人看一眼。**它不改状态**,只负责被记下来
    needs_human: bool = False
    error_code: str | None = None
    error_message: str | None = None


def should_poll(*, status: str, external_spu_id: str | None) -> bool:
    """这一行还要不要问平台。

    两个条件缺一不可,而第二个是常被漏掉的那个:没有 external_spu_id 就
    **没有可问的对象**。少了这条判断,轮询器会对着一批刚提交、还没拿到 ID 的
    行反复构造请求,而平台只会回 404 —— 于是日志里堆满 404,真正的 404
    (商品在平台上消失了)从此没人看得见。
    """
    if not (external_spu_id or "").strip():
        return False
    return status in POLLABLE_STATUSES


def poll_backoff_seconds(poll_count: int) -> int:
    """问过 ``poll_count`` 次之后,下一次隔多久再问。

    ## 为什么这条曲线**不通往放弃**

    投递(`publish_policy.exhausted`)用尽次数会放弃,理由是每一次重试都真的
    打在平台上、都在花钱和消耗风控配额。轮询不一样:它是一次读,而且读的是
    「平台现在怎么说」。放弃轮询的后果是本地看板与平台永久分叉 —— 一个在
    平台上已经上架的商品,在我们这边永远显示"审核中",而 4.1 节 H 的清单核对
    正是靠这些状态来判断"是不是全都下架了"。

    所以这里封顶的是**频率**不是次数:一小时问一次,便宜到可以一直问下去。
    真正卡住的行由「审核中超过 N 小时」这类看板指标去暴露,那是给人看的信号,
    不是让系统偷偷停手的理由。
    """
    if poll_count <= 0:
        return MIN_POLL_INTERVAL_SECONDS
    grown = MIN_POLL_INTERVAL_SECONDS * (2 ** min(poll_count, 16))
    return min(MAX_POLL_INTERVAL_SECONDS, grown)


def classify_poll(
    *,
    status_code: int,
    body: Mapping[str, Any] | None = None,
    current_status: str | None = None,
) -> PollOutcome:
    """把平台对一次状态查询的回答翻译成结论。

    ## 问不到 ≠ 状态变了

    这是本函数唯一真正难的地方,而且四条失败分支里每一条都在重复它:
    平台限流、平台 5xx、鉴权过期、连接不上 —— 全部返回 `listing_status=None`。
    这些情况下我们对那个商品的了解和问之前**一模一样**,把它标成任何状态都是
    在编造信息。最危险的具体形态是把一次 5xx 记成 SUBMIT_FAILED:商品在平台上
    好好上着架,而运营看到"提交失败"之后会去重新提交一次。

    ## 不认识的平台状态也不动

    提交路径遇到不认识的状态会落 `SUBMITTED`(默认值),因为那时至少知道
    「平台收下了」。轮询路径没有这个兜底 —— 我们问的就是状态,答案不认识就是
    没有答案。原话仍然存进 `last_platform_status`,查得到、能拿去补映射表。
    """
    payload = dict(body or {})
    platform_status = _clean(payload.get("platform_status"))

    # ---- 2xx:平台回答了
    if 200 <= status_code < 300:
        mapped = (
            PLATFORM_STATUS_MAP.get(platform_status.strip().upper())
            if platform_status
            else None
        )
        if mapped is None:
            return PollOutcome(
                platform_status=platform_status,
                listing_status=None,
                answered=True,
                # 平台答了但我们读不懂,这是映射表该补一行的信号,不是故障
                needs_human=bool(platform_status),
                error_code=(
                    ErrorCode.CHANNEL_FIELD_MISSING.value if platform_status else None
                ),
                error_message=(
                    f"平台状态 {platform_status!r} 不在 PLATFORM_STATUS_MAP 里,本地状态不动"
                    if platform_status
                    else "平台回了 200 但没带 platform_status"
                ),
            )

        rejection = (
            _rejection_from(payload)
            if mapped == PublishStatus.PLATFORM_REJECTED.value
            else None
        )
        return PollOutcome(
            platform_status=platform_status,
            # 已经是这个状态时仍然把它填上:调用方据此判断"变没变"要比较
            # 新旧两个值,而不是靠这里返回 None —— None 在本类里已经有
            # 另一个含义了(别动),两种含义共用一个取值就没法区分
            listing_status=mapped,
            rejection=rejection,
            answered=True,
        )

    # ---- 404:平台说没有这个商品。**这不等于它被下架了**
    if status_code == 404:
        return PollOutcome(
            listing_status=None,
            answered=True,
            needs_human=True,
            error_code=ErrorCode.NOT_FOUND.value,
            error_message=(
                "平台查不到这个 external_spu_id。可能是它真的被删了,也可能是"
                "我们拿着 A 店铺的 ID 去问了 B 店铺。**不记成已下架** —— "
                "4.1 节 H 的清单核对靠这一列判断商品还在不在,猜错的代价是"
                "一个仍然挂在平台上的商品从清单里消失"
            ),
        )

    # ---- 429 / 5xx:平台暂时答不了。等下一轮,状态不动
    if status_code == 429 or status_code >= 500:
        return PollOutcome(
            listing_status=None,
            error_code=(
                ErrorCode.CHANNEL_RATE_LIMITED.value
                if status_code == 429
                else ErrorCode.PROVIDER_SERVICE_ERROR.value
            ),
            error_message=f"平台暂时回答不了({status_code}),本地状态保持 {current_status}",
        )

    # ---- 401 / 403:凭证问题。换凭证之前问一万次都是同一个结果
    if status_code in (401, 403):
        return PollOutcome(
            listing_status=None,
            needs_human=True,
            error_code=ErrorCode.CHANNEL_AUTH_EXPIRED.value,
            error_message="轮询鉴权失败,渠道凭证需要更新",
        )

    return PollOutcome(
        listing_status=None,
        needs_human=True,
        error_code=ErrorCode.PROVIDER_SERVICE_ERROR.value,
        error_message=f"平台对状态查询返回了 {status_code}",
    )


def transport_failure() -> PollOutcome:
    """连接不上平台。

    与 `publish_policy.classify_transport_failure` 的对比值得写下来:提交超时
    是一件**严重**的事(平台可能已经建好了商品),轮询超时什么也没发生。
    所以这里不产生 UNKNOWN、不进人工队列、不改状态 —— 就是下一轮再问。
    """
    return PollOutcome(
        listing_status=None,
        error_code=ErrorCode.PROVIDER_SERVICE_ERROR.value,
        error_message="连接不上平台,本轮轮询跳过,状态不动",
    )


def _rejection_from(body: Mapping[str, Any]) -> RejectionSignal:
    """从驳回响应里取出分诊需要的三件东西。

    类目解析不出来时落 `OTHER` 而不是抛错:平台**已经驳回**了,这是既成事实,
    而一个我们不认识的类目字符串不该让这条驳回记不进台账 —— 那样商品会顶着
    "审核中"沉底,而它其实已经被拒了。原话在 note 里,人看得到。
    """
    raw = _clean(body.get("reject_category"))
    try:
        category = RejectCategory(str(raw).upper()) if raw else RejectCategory.OTHER
    except ValueError:
        category = RejectCategory.OTHER
    return RejectionSignal(
        category=category,
        code=_clean(body.get("reject_code")),
        note=_clean(body.get("reject_message")) or _clean(body.get("message")),
    )


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
