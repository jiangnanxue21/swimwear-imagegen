"""发布提交的判定(方案 v4.1 任务 14 / 4.1 节 D / 7.4 节)。

放在 workflows 层的理由和 `dispatch_policy.py` 一样:**这些是判断,不是动作**。
「409 算不算成功」「超时之后能不能重发」「退避等多久」这几条决定了系统在平台
抽风时的行为,而那正是最难在线上复现、最需要靠测试锁住的部分。只要它们能查库,
就没人会写那个穷举测试。

所以本模块只有标准库和枚举,没有 session、没有网络。`publish_service.py` 负责
把这里的判断变成写库动作。

## 三条最容易写反的语义

    409 幂等冲突算「接受」   它是好消息:上一次其实成功了,平台把既有资源还给你。
                            判成失败的话重试会再发一次,那次仍是 409 —— 死循环,
                            且每一轮都在给平台风控加分。
    429 限流不改本地状态     这次没送到,商品还在排队。标成 SUBMIT_FAILED 会让
                            运营以为要人工处理。
    超时之后不许自动重发创建  平台**可能**已经收到。重发一次的代价是店铺里多一个
                            没人认领的商品(4.1 节 H 要防的正是这个)。

第三条有一个例外,而这个例外是有意义的:UPDATE / REPUBLISH / DELIST 的目标由
`external_spu_id` 确定,重发同一份内容只是把同样的修改再应用一次,不会多出商品。
只有 CREATE 的重发会凭空造东西。所以「超时能不能自动重试」取决于操作类型,
而不是一刀切 —— 一刀切的代价要么是创建重复,要么是所有更新都要人工点一次。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import (
    PublishAttemptStatus,
    PublishOperation,
    PublishStatus,
)
from app.core.errors import ErrorCode
from app.workflows import delivery_evidence

#: 一条 Outbox 记录最多投递几次。比派发 Outbox 的 10 次少,因为这里每一次
#: 都真的打在平台上:失败重试会累进平台的风控与限流配额,而派发失败只是
#: 本地队列没接住。6 次配合下面的退避大约覆盖 10 分钟 —— 平台挂 10 分钟
#: 还没回来,应该有人去看,而不是继续敲门。
MAX_DELIVERY_ATTEMPTS = 6

#: 退避上限。不封顶的话第 12 次要等一小时,等于悄悄放弃却还显示成「待重试」。
MAX_BACKOFF_SECONDS = 300

#: worker 领走一行之后的租约时长。必须**大于**一次外部调用的超时上限,
#: 否则租约会在调用还没返回时过期,另一个 worker 把同一次提交再发一遍 ——
#: 那是这张表存在的意义的反面。
LEASE_SECONDS = 180

#: 一次真实渠道调用(submit / poll)的**总**超时上限,秒。真实 transport 的
#: HTTP 客户端必须拿它当总预算,而不是各自写一个数。
#:
#: ## 为什么它现在就得存在,而不是等第一个真实渠道落地
#:
#: `channels/registry.py` 顶部把这件事论证得很完整:今天唯一的发送端是
#: Simulator(同步返回、不发真实外部调用),所以「调用挂住超过租约」不可达;
#: **第一个真实 HTTP transport 进那张表的那一刻它变可达** —— 调用挂住 >180 秒
#: → `publish_service.claim_due()` 判定 worker 崩了 → 同一份报文、同一把幂等键
#: 发第二遍。§3.19 论证的是「不会重复创建」,不覆盖「结果不会被回写」。
#:
#: 在此之前这条只是那段注释里的一句话,而注释的防线是「希望改的人读到它」。
#: 守卫需要一个真实的常量作对象 —— 这就是那个对象。配套的反向断言在
#: `tests/pure/test_transport_timeout_invariant.py`:`_TRANSPORTS` 里任何
#: `is_simulator=False` 的条目,其实现模块必须引用这个名字。于是第一个真实
#: transport 不认这条超时时**当场变红**,而不是上线之后重复提交。
#:
#: 取 60 而不是贴着租约取 179:两者之间要留下退避与一次重试的余地,
#: 而那个余地由下面的 `LEASE_HEADROOM_FACTOR` 说出来,不靠读数的人心算。
TRANSPORT_TIMEOUT_SECONDS = 60

#: 租约相对单次调用超时要留几倍余量。
#:
#: 2 的含义是:一次调用挂满超时之后,租约里还剩下同样长的一段时间用来退避、
#: 记账、把结论写回库。取 1(即只要求 timeout < lease)守不住这些 ——
#: 调用刚好挂满时租约已经贴着到期,回写会撞上另一个 worker 的重新领取。
LEASE_HEADROOM_FACTOR = 2

# 用 `if ... raise` 而不是 `assert`:模块级 assert 在 `python -O` 下被整条剥离,
# 而那正是生产(`tests/pure/test_a44_batch5_fixes.py` 有一条门禁禁止 app/ 下
# 出现模块级 assert,理由就是这个)。写法与 `workbench/batch.py` 的
# CLAIM_CHUNK 断言一致 —— **常量与不变量必须在同一个模块**,不然 assert
# 看不见它,而那正是这条从前只能是注释的原因。
if TRANSPORT_TIMEOUT_SECONDS * LEASE_HEADROOM_FACTOR >= LEASE_SECONDS:  # pragma: no cover
    raise RuntimeError(
        "单次渠道调用的超时上限必须显著小于发布租约:调用挂满超时之后,"
        "租约里还要有余量完成退避与结论回写。否则租约会在调用还没返回时过期,"
        "`claim_due()` 把这行当成 worker 崩了重新发出 —— 同一份报文、"
        "同一把幂等键发第二遍,而三道防线只保证不重复创建,不保证结果被回写"
    )

#: 平台状态字符串 -> 本地状态机。翻译放在这里,不放在 Adapter 里:
#: 每个渠道各译一次的话,「平台说 REVIEWING 我们记成什么」会有 N 个答案。
#: 平台的原话另存 `ChannelListing.last_platform_status`,不翻译、不覆盖 ——
#: 两者不一致时才查得出是我们解读错了还是平台改了。
PLATFORM_STATUS_MAP: Mapping[str, str] = {
    "LISTED": PublishStatus.LISTED.value,
    "LIVE": PublishStatus.LISTED.value,
    "REVIEWING": PublishStatus.PLATFORM_REVIEWING.value,
    "PROCESSING": PublishStatus.PLATFORM_REVIEWING.value,
    "REJECTED": PublishStatus.PLATFORM_REJECTED.value,
    "DELISTED": PublishStatus.DELISTED.value,
}


@dataclass(frozen=True)
class Outcome:
    """一次投递的结论。**只描述该记什么,不描述怎么记。**

    `listing_status` 为 None 是一个有含义的取值,不是「忘了填」:
    限流那条路径必须保持本地状态不变(见模块顶部第二条)。把它和
    「状态是 SUBMITTING」合并成同一个表示,限流就会悄悄推进状态机。
    """

    attempt_status: str
    #: None = **不要动** listing 的本地状态
    listing_status: str | None
    #: 这一次能不能原样重发。与 `attempt_status` 分开:UNKNOWN 也可能可重发
    #: (更新/下架),FAILED 一定不可重发
    retriable: bool
    external_spu_id: str | None = None
    external_sku_ids: Mapping[str, Any] = field(default_factory=dict)
    #: 平台原话,照存不翻译
    platform_status: str | None = None
    #: 平台给的 Retry-After。有它就用它 —— 我们自己算的退避没有平台清楚
    retry_after_seconds: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    #: 这一次投递的 typed 证据(P-06)。默认 UNKNOWN —— 大多数结论与"发没发出去"
    #: 无关,而**默认值必须是最保守的那一档**:填错方向的代价是重复创建。
    #: service 在结果事务里 append-only 落它,不覆盖历史行。
    delivery_evidence: str = delivery_evidence.UNKNOWN

    @property
    def succeeded(self) -> bool:
        return self.attempt_status == PublishAttemptStatus.SUCCEEDED.value

    @property
    def needs_human(self) -> bool:
        """不可自动推进、要人看一眼的结局。

        UNKNOWN 且不可重发 = 7.4 节的「人工异常队列」。
        """
        return (
            self.attempt_status == PublishAttemptStatus.UNKNOWN.value
            and not self.retriable
        )


# ---------------------------------------------------------------- 操作类型


class PublishPolicyError(ValueError):
    """判定层拒绝了这次提交。带结构化错误码,不靠消息片段匹配。

    继承 `ValueError` 而不是 `AppError`:本模块要能在没有 fastapi 的环境里
    被直接 import(见模块顶部)。`publish_service` 负责翻译成 HTTP 错误。
    """

    def __init__(self, message: str, *, code: ErrorCode) -> None:
        super().__init__(message)
        self.code = code


def resolve_operation(
    *,
    external_spu_id: str | None,
    requested: PublishOperation | str | None = None,
) -> PublishOperation:
    """决定这次提交是创建还是更新。**优先由状态推出,而不是由调用方声明。**

    ## 为什么不直接信调用方

    调用方拿到的往往是一个页面上的按钮。按钮说「提交」,而这个商品是不是已经
    在平台上了,只有库里那一列知道。让调用方声明操作类型,等于把
    「会不会重复创建」这个决定交给最不掌握信息的一层。

    ## 但显式声明错了要**报错**,不许静默改正

    `ErrorCode.PUBLISH_DUPLICATE_CREATE` 的注释写的就是这件事:已有
    external_spu_id 却试图 CREATE。静默改成 UPDATE 看起来更「贴心」,
    代价是调用方永远不知道自己的状态判断是错的 —— 而那个错误判断下一次
    会出现在一个我们没有兜住的地方。

    反方向同理:没有 external_spu_id 却要 UPDATE,说明上游以为这个商品已经
    上架了。这时候悄悄改成 CREATE 会往平台塞一个谁都没打算创建的商品。
    """
    has_external = bool((external_spu_id or "").strip())

    if requested is None:
        return PublishOperation.UPDATE if has_external else PublishOperation.CREATE

    op = PublishOperation(str(requested).upper())

    if op is PublishOperation.CREATE and has_external:
        raise PublishPolicyError(
            f"这个商品已经有 external_spu_id({external_spu_id}),不能再 CREATE。"
            "要改内容请用 UPDATE —— 再创建一次会在平台上多出一个商品。",
            code=ErrorCode.PUBLISH_DUPLICATE_CREATE,
        )

    if op is not PublishOperation.CREATE and not has_external:
        raise PublishPolicyError(
            f"{op.value} 需要 external_spu_id,但这个商品还没有成功创建过。先走 CREATE。",
            code=ErrorCode.CHANNEL_FIELD_MISSING,
        )

    return op


# ---------------------------------------------------------------- 响应分类


def classify_response(
    *,
    status_code: int,
    body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    operation: PublishOperation | str = PublishOperation.CREATE,
) -> Outcome:
    """把平台的一次 HTTP 响应翻译成结论。

    分支顺序是刻意的:先处理 409,再处理其余 4xx。反过来写的话
    「409 属于 4xx」会让幂等冲突落进失败分支,而那是本模块要防的头号错误。
    """
    body = dict(body or {})
    headers = dict(headers or {})
    op = PublishOperation(str(operation).upper())

    external_id = _clean(body.get("external_spu_id"))
    platform_status = _clean(body.get("platform_status"))

    # ---- 409:幂等冲突 = 上一次成功了
    if status_code == 409:
        if not external_id:
            # 平台说「这个键创建过资源」却不告诉我们是哪个 —— 我们无从落库,
            # 也就无从对账。这不是成功,是**未知**:必须有人去平台上看一眼。
            # 当成成功记下来的话,库里会有一条没有外部 ID 的 LISTED,
            # 而 4.1 节 H 的清理清单正是靠外部 ID 列出来的
            return Outcome(
                attempt_status=PublishAttemptStatus.UNKNOWN.value,
                listing_status=PublishStatus.SUBMIT_RESULT_UNKNOWN.value,
                retriable=False,
                error_code=ErrorCode.PUBLISH_RESULT_UNKNOWN.value,
                error_message="平台返回幂等冲突但没有带回 external_spu_id,无法对账",
            )
        return Outcome(
            attempt_status=PublishAttemptStatus.SUCCEEDED.value,
            listing_status=_status_for(op, platform_status, default=PublishStatus.SUBMITTED),
            retriable=False,
            external_spu_id=external_id,
            platform_status=platform_status,
            error_code=None,
            error_message="幂等冲突:该键已创建过商品,沿用平台返回的既有 ID",
        )

    # ---- 2xx:接受
    if 200 <= status_code < 300:
        if op is PublishOperation.CREATE and not external_id:
            # 创建成功却没有外部 ID,等于没有凭据证明这次到达过平台。
            # 记成 LISTED 的话,后续轮询、更新、下架全都没有目标
            return Outcome(
                attempt_status=PublishAttemptStatus.UNKNOWN.value,
                listing_status=PublishStatus.SUBMIT_RESULT_UNKNOWN.value,
                retriable=False,
                error_code=ErrorCode.PUBLISH_RESULT_UNKNOWN.value,
                error_message="平台返回成功但没有 external_spu_id,无法确认创建结果",
            )
        return Outcome(
            attempt_status=PublishAttemptStatus.SUCCEEDED.value,
            listing_status=_status_for(op, platform_status, default=PublishStatus.SUBMITTED),
            retriable=False,
            external_spu_id=external_id,
            external_sku_ids=dict(body.get("external_sku_ids") or {}),
            platform_status=platform_status,
        )

    # ---- 429:限流。**不改本地状态**
    if status_code == 429:
        return Outcome(
            attempt_status=PublishAttemptStatus.IN_FLIGHT.value,
            listing_status=None,
            retriable=True,
            retry_after_seconds=_retry_after(headers),
            error_code=ErrorCode.CHANNEL_RATE_LIMITED.value,
            error_message=_message(body, "平台限流"),
        )

    # ---- 401 / 403:凭证不对。改凭证之前重发一万次也是同一个结果
    if status_code in (401, 403):
        return Outcome(
            attempt_status=PublishAttemptStatus.FAILED.value,
            listing_status=PublishStatus.SUBMIT_FAILED.value,
            retriable=False,
            error_code=ErrorCode.CHANNEL_AUTH_EXPIRED.value,
            error_message=_message(body, "渠道鉴权失败"),
        )

    # ---- 422 / 400:字段被拒。进的是修复流程,不是重试流程
    if status_code in (400, 422):
        return Outcome(
            attempt_status=PublishAttemptStatus.FAILED.value,
            listing_status=PublishStatus.VALIDATION_FAILED.value,
            retriable=False,
            error_code=ErrorCode.CHANNEL_FIELD_MISSING.value,
            error_message=_message(body, "平台拒绝了报文里的字段"),
        )

    # ---- 5xx:平台自己出问题,原样重发有意义
    if status_code >= 500:
        return Outcome(
            attempt_status=PublishAttemptStatus.IN_FLIGHT.value,
            listing_status=None,
            retriable=True,
            retry_after_seconds=_retry_after(headers),
            error_code=ErrorCode.PROVIDER_SERVICE_ERROR.value,
            error_message=_message(body, f"平台返回 {status_code}"),
        )

    # ---- 其余 4xx:不认识,但明确是我们这边的问题,不重发
    return Outcome(
        attempt_status=PublishAttemptStatus.FAILED.value,
        listing_status=PublishStatus.SUBMIT_FAILED.value,
        retriable=False,
        error_code=ErrorCode.PUBLISH_REJECTED.value,
        error_message=_message(body, f"平台返回 {status_code}"),
    )


def classify_transport_failure(
    *,
    operation: PublishOperation | str,
    error: str = "",
    evidence: str = delivery_evidence.UNKNOWN,
) -> Outcome:
    """连接中断 / 超时 —— 我们**通常**不知道平台收到了没有。

    ## `evidence` 那一档:知道的时候不要说不知道(P-06)

    默认 `UNKNOWN`,行为与本参数存在之前逐字一致 —— 今天唯一的发送端是
    Simulator,它不传这个参数,所以现网路径一个字节都没变。

    只有 `NO_REQUEST_BYTES_SENT` 会**放宽**处置:第一个字节都没写出去时平台
    不可能收到,于是连 CREATE 都可以自动重发。这一档的结论形状照 429 那条:

        attempt_status = IN_FLIGHT   这次尝试还没有结论 —— 平台压根没参与
        listing_status = None        本地状态一个字都不动
        retriable      = True        包括 CREATE

    用 `IN_FLIGHT` 而不是 `ABORTED`/`FAILED`,是因为后两者都在
    `publish_service.TERMINAL_ATTEMPT_STATUSES` 里:那会让 `reused_terminal`
    判真,界面显示"这件事死了",而 Outbox 其实排着下一次重试。
    重试用尽时 `settle_exhausted()` 会把 IN_FLIGHT 收成 SUBMIT_FAILED ——
    一次都没发出去的提交,结论是失败,这是准确的。

    `REQUEST_BYTES_SENT` 不放宽:字节上了线就有可能被处理,这与 UNKNOWN 同档。
    它仍然值得单独记一笔 —— 排障时"发出去了没等到回音"和"连不上"是两件事。

    ## 为什么创建和其它操作分开处理

    7.4 节要求超时后不得立即重新创建。但那条规则的理由是「平台可能已经建好了,
    再发一次就多一个商品」,而这个理由只对 CREATE 成立:

        CREATE     目标由报文内容决定 —— 发两次可能得到两个商品
        UPDATE     目标由 external_spu_id 决定 —— 发两次是同一个商品改两次
        REPUBLISH  同上
        DELIST     同上,而且重复下架是无害的

    对后三种坚持人工确认,换不来任何安全,只会让每一次网络抖动都变成一张工单;
    而工单堆起来之后,人的处理方式必然是「全选、重试」—— 那时这条规则就从
    「防重复」退化成了「拖慢一切,然后被绕过」。
    """
    op = PublishOperation(str(operation).upper())
    proof = delivery_evidence.coerce(evidence)

    if delivery_evidence.proves_not_sent(proof):
        return Outcome(
            attempt_status=PublishAttemptStatus.IN_FLIGHT.value,
            listing_status=None,
            retriable=True,
            error_code=ErrorCode.PUBLISH_RESULT_UNKNOWN.value,
            error_message=(
                f"{op.value} 没有发出去({error or '连接阶段失败'})。"
                + delivery_evidence.describe(proof)
                + ",本地状态不变,已排入重试。"
            ),
            delivery_evidence=proof,
        )

    resendable = op is not PublishOperation.CREATE
    return Outcome(
        attempt_status=PublishAttemptStatus.UNKNOWN.value,
        listing_status=PublishStatus.SUBMIT_RESULT_UNKNOWN.value,
        retriable=resendable,
        error_code=ErrorCode.PUBLISH_RESULT_UNKNOWN.value,
        error_message=(
            f"{op.value} 提交结果未知({error or '连接中断'})。"
            + (
                "目标由 external_spu_id 确定,重发不会多出商品,已排入重试。"
                if resendable
                else "平台可能已经创建成功,**不自动重发** —— 先用同一个幂等键确认。"
            )
        ),
        delivery_evidence=proof,
    )


# ---------------------------------------------------------------- 重试与退避


def backoff_seconds(attempt_count: int, *, retry_after: int | None = None) -> int:
    """第 ``attempt_count`` 次投递失败之后要等多久。

    平台给了 Retry-After 就听平台的(仍然封顶)——它比我们清楚自己的配额窗口。
    我们自己算的那条曲线只是在平台没说话时的兜底。
    """
    if retry_after is not None and retry_after > 0:
        return min(MAX_BACKOFF_SECONDS, int(retry_after))
    if attempt_count <= 0:
        return 0
    return min(MAX_BACKOFF_SECONDS, 2 ** min(attempt_count, 16))


def exhausted(attempt_count: int) -> bool:
    """投递次数是否已经用尽。

    用尽之后 Outbox 进 DEAD 而不是继续重试:自动重试到永远会把一个配置错误
    变成一份持续增长的账单,而且没有任何时刻会有人被告知。
    """
    return attempt_count >= MAX_DELIVERY_ATTEMPTS


def settle_exhausted(outcome: Outcome) -> Outcome:
    """重试用尽时的最终判定。

    一次一直 429 的提交,每一轮的结论都是 IN_FLIGHT(还没有结论);用尽之后
    必须落成一个**有结论**的状态,否则这条尝试记录会永远停在「进行中」,
    而 `PUBLISH_ATTEMPT_UNRESOLVED` 那个集合正是用来找这种记录的。
    """
    if outcome.attempt_status == PublishAttemptStatus.IN_FLIGHT.value:
        return Outcome(
            attempt_status=PublishAttemptStatus.FAILED.value,
            listing_status=PublishStatus.SUBMIT_FAILED.value,
            retriable=False,
            error_code=outcome.error_code,
            error_message=(outcome.error_message or "") + f"(已重试 {MAX_DELIVERY_ATTEMPTS} 次)",
            delivery_evidence=outcome.delivery_evidence,
        )
    # UNKNOWN 用尽之后仍然是 UNKNOWN —— 那次调用确实不知道结果,
    # 重试次数用完不能把它改写成「失败」。历史不因为我们放弃了而改变
    return Outcome(
        attempt_status=outcome.attempt_status,
        listing_status=outcome.listing_status,
        retriable=False,
        external_spu_id=outcome.external_spu_id,
        external_sku_ids=outcome.external_sku_ids,
        platform_status=outcome.platform_status,
        error_code=outcome.error_code,
        error_message=outcome.error_message,
        delivery_evidence=outcome.delivery_evidence,
    )


# ---------------------------------------------------------------- 内部


def _status_for(
    op: PublishOperation, platform_status: str | None, *, default: PublishStatus
) -> str:
    """由操作类型与平台原话推出本地状态。

    DELIST 不看平台的 platform_status:下架成功时平台通常仍然返回那条商品记录,
    带着 `LISTED` 之类的字样 —— 照着翻译会把一次成功的下架记成「在架」。
    """
    if op is PublishOperation.DELIST:
        return PublishStatus.DELISTED.value
    if platform_status:
        mapped = PLATFORM_STATUS_MAP.get(platform_status.strip().upper())
        if mapped:
            return mapped
    # 平台说了一个我们不认识的状态。记成 SUBMITTED(收到了,含义待查)而不是
    # 猜一个 —— 原话已经存进 last_platform_status,查得到
    return default.value


def _retry_after(headers: Mapping[str, str]) -> int | None:
    for key, value in headers.items():
        if str(key).strip().lower() == "retry-after":
            try:
                return max(0, int(str(value).strip()))
            except (TypeError, ValueError):
                # 真实平台的 Retry-After 也可能是 HTTP-date。解析不了就当没给,
                # 走我们自己的退避曲线 —— 比抛错好:限流响应本身没有错
                return None
    return None


def _message(body: Mapping[str, Any], fallback: str) -> str:
    raw = _clean(body.get("message")) or _clean(body.get("error"))
    return (raw or fallback)[:2000]


def _clean(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
