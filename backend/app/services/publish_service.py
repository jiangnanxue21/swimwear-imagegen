"""自动创建、更新与幂等(方案 v4.1 任务 14 / 4.1 节 D / 7.8 节)。

关键路径 `0 → 11 → 12/13 → 14 → 15/16 → 20 → 24` 上,这是第一个把 Simulator、
发布域三张表和幂等键接成**真的会写库**的链路的任务。它同时碰三样东西:
外部调用、事务边界、Outbox。

## 事务边界(7.8 节,不许改)

    业务事务      enqueue():建 listing / attempt / outbox。**只 flush,不 commit**
                  —— 事务归调用方(API 或 use case)所有
    checkpoint    投递前:领租约 + 把 attempt 标成 IN_FLIGHT,commit
    事务外        真正发请求。一个数据库事务不许跨越一次外部调用
    新事务        保存结果

`claim_due` 与 `apply_outcome` 之间**没有**打开的事务,这是刻意的。中间挂掉的后果是
一行 LEASED 的 outbox 停在那里,租约过期后被别人重新领走 —— 而这正是
`lease_until` 存在的理由。反过来,如果把外部调用包在事务里,挂掉的后果是
「平台上有商品、本地事务回滚了」,那种不一致没有任何机制能自动收敛。

## 幂等的三道防线

    幂等键        同一次逻辑提交算出同一个键(`workflows/publish_idempotency.py`)
    唯一索引      `uq_publish_attempts_idempotency_key` —— 并发下应用层的先查后写
                  挡不住,数据库挡得住
    平台 409      前两道都漏了,平台会告诉我们「这个键已经创建过资源 X」,
                  而 409 算**接受**(见 `publish_policy`)

三道都指向同一件事:4.1 节 D 的「重试不会重复创建」。任何一道单独存在都不够 ——
第一道防不住并发,第二道防不住跨进程重放,第三道要依赖平台真的实现了幂等。

## 干跑不进这条链路

干跑的定义是「构造了报文但不发出去」。所以 `enqueue(dry_run=True)` **不建
outbox 行**,直接把构造好的报文返回给调用方预览,listing 落在
`DRY_RUN_COMPLETED`。把干跑也塞进 outbox 交给 Simulator 模拟的话,
就没有任何一条路径在验证「我们真的没发」。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.channels import registry
from app.channels.base import PreparedRequest, PublishOperationRef
from app.core.clock import utc_now
from app.core.enums import (
    AuditAction,
    DraftStatus,
    PublishAttemptStatus,
    PublishOperation,
    PublishOutboxStatus,
    PublishStatus,
)
from app.core.errors import ErrorCode, ValidationError
from app.core.logging import get_logger, redact
from app.db.locks import advisory_xact_lock
from app.listings.contracts import MappedListing
from app.models.listing_copy import ListingDraft
from app.models.product import Product
from app.models.publishing import ChannelListing, PublishAttempt, PublishOutbox
from app.services import audit
from app.workflows import publish_policy as policy
from app.workflows.publish_idempotency import (
    build_publish_idempotency_key,
    build_request_fingerprint,
)

logger = get_logger(__name__)

#: 允许提交的草稿状态。BUILDING 还没校验完,INVALID 有阻断错误,
#: STALE 的快照已经过期 —— 三者提交上去都是在浪费一次平台配额,
#: 而 STALE 更糟:它会把**旧内容**发上去,而页面上显示的是新内容
SUBMITTABLE_DRAFT_STATUSES: frozenset[str] = frozenset(
    {DraftStatus.VALIDATED.value, DraftStatus.APPROVED.value}
)

#: 不会再有任何进展的尝试状态。`_reuse` 用它区分「正在处理」和「已经死了」。
#: 从 attempt 与 outbox **两边**判:attempt 记的是那一次尝试的结局,
#: outbox 记的是还会不会再投 —— 一条 UNKNOWN 的 attempt 配一行 DEAD 的 outbox
#: 意味着"我们不知道结果,而且不会自动再试了",那对运营同样是个终点。
TERMINAL_ATTEMPT_STATUSES: frozenset[str] = frozenset(
    {
        PublishAttemptStatus.FAILED.value,
        PublishAttemptStatus.UNKNOWN.value,
        PublishAttemptStatus.ABORTED.value,
    }
)

#: advisory lock 的命名空间。锁的粒度是「一个商品在一个店铺的一个站点」,
#: 不是整张表 —— 上架 SW-001 不该挡住 SW-002
_LOCK_NS = "publish.listing"


def _now() -> datetime:
    """现在。转发 `core/clock.utc_now()` —— 全仓只有那一份「现在几点」。

    为什么必须只有一份(以及 naive/timestamptz 的错配会怎么无声出事),
    见 `core/clock.py` 顶部。不要在这里重新解释一遍:两份解释会各自漂移,
    而漂移之后没人知道该信哪一份。
    """
    return utc_now()


# ================================================================ 业务事务


@dataclass(frozen=True)
class EnqueueResult:
    """`enqueue()` 的结果。**调用方据此决定要不要 commit,以及要不要触发投递。**"""

    listing: ChannelListing
    operation: PublishOperation
    idempotency_key: str
    prepared: PreparedRequest
    #: 干跑时为 None —— 干跑不排队
    attempt: PublishAttempt | None = None
    outbox: PublishOutbox | None = None
    #: True = 这次没有新建尝试,沿用了同一个幂等键既有的那一条。
    #: 调用方要据此决定给运营看什么:「已提交,正在处理」而不是「已重新提交」
    reused: bool = False
    #: True = 沿用的那一条**已经是终态**(失败/已放弃),不会再有任何进展。
    #:
    #: 这两种"沿用"必须分开。幂等键含草稿指纹,草稿不变键就不变,于是一条
    #: 退避耗尽落进 DEAD 的提交,运营再点一次「提交」仍然走进 `_reuse` ——
    #: 只看 `reused` 的话界面会说「已提交,正在处理」,而实际上什么都没发生,
    #: 而且**永远**不会发生。运营会一直等一件已经死掉的事。
    reused_terminal: bool = False
    dry_run: bool = False


def enqueue(
    session: Session,
    *,
    product: Product,
    draft: ListingDraft,
    shop_id: str,
    actor: str,
    requested_operation: PublishOperation | str | None = None,
    dry_run: bool = False,
    options: Mapping[str, Any] | None = None,
    test_batch_tag: str | None = None,
) -> EnqueueResult:
    """把一次提交排进队列。**在业务事务内调用,由调用方一起提交。**

    这里刻意只 `flush()` 不 `commit()`:一旦这里提交了,就等于把「意图」和
    「业务数据」拆成了两个事务,Outbox 也就白做了(同 `dispatch_service.enqueue`)。

    ## 为什么先上 advisory lock

    `channel_listings` 上有唯一约束 `(channel, shop_id, site, product_id)`,
    但「查不到就新建」这个读-改-写在并发下会让两个请求都走进新建分支,
    其中一个撞唯一约束返回 500。锁负责让**正常并发**不产生 500,
    索引负责让**异常写入**不产生脏数据 —— 两者都要,理由见 `db/locks.py`。
    """
    # DELIST **不看草稿状态**。4.1 节 H 的清理预案要下架的恰恰是那批草稿早已
    # 过期的商品(测试期间上游改过图、改过文案),而 `_assert_submittable` 会把
    # STALE 挡在门外。用一条「防止发错内容」的规则去挡一个**根本不发内容**的
    # 操作,结果是清理预案在最需要它的时候执行不下去,而残留在平台上的商品
    # 没有任何其它办法下架。下架的目标由 external_spu_id 确定,草稿是新是旧
    # 对它没有任何影响。
    if _requested(requested_operation) is not PublishOperation.DELIST:
        _assert_submittable(draft)

    channel = draft.channel
    site = draft.site
    advisory_xact_lock(session, _LOCK_NS, channel, shop_id, site, str(product.id))

    listing = _get_or_create_listing(
        session, channel=channel, site=site, shop_id=shop_id, product=product, draft=draft
    )
    if test_batch_tag:
        # 只在显式带了批次号时写,**不许**用 None 覆盖既有值:一次没带批次号的
        # 更新会把这一行从「本轮测试创建了哪些外部商品」的清单里抹掉,而那份
        # 清单漏一行,就没人说得清平台上到底还剩几个(4.1 节 H 要防的结局本身)
        listing.test_batch_tag = test_batch_tag

    try:
        operation = policy.resolve_operation(
            external_spu_id=listing.external_spu_id, requested=requested_operation
        )
    except policy.PublishPolicyError as exc:
        # 判定层不认识 HTTP。翻译在这里做一次,不在每个调用点各做一次
        raise ValidationError(str(exc), code=exc.code, http_status=409) from exc

    # 下架报文**不带内容**。目标由 URL 里的 external_spu_id 确定,而把一份
    # 可能已经过期的 payload 塞进下架请求,平台若照单更新,我们就在下架的
    # 同时把旧内容发了上去 —— 一次清理动作变成了一次内容回退,而且没有任何
    # 地方会提示。空报文也让「下架不需要草稿是新的」这件事在代码里看得见
    mapped = (
        MappedListing(header={}, rows=())
        if operation is PublishOperation.DELIST
        else _mapped_from_draft(draft)
    )
    prepared = registry.build_request(
        channel,
        mapped,
        PublishOperationRef(
            operation=operation,
            channel_listing_id=str(listing.id),
            external_spu_id=listing.external_spu_id,
        ),
    )
    key = build_publish_idempotency_key(
        channel=channel,
        shop_id=shop_id,
        site=site,
        operation=operation.value,
        product_id=str(product.id),
        draft_fingerprint=draft.source_fingerprint or "",
        spec_checksum=draft.spec_version or "",
        external_listing_id=listing.external_spu_id,
    )
    prepared = _with_key(prepared, key)
    fingerprint = build_request_fingerprint(dict(prepared.body))

    if dry_run:
        # 干跑止于这里。不建 attempt、不建 outbox、不产生平台 ID ——
        # `DRY_RUN_STATES` 要求干跑分支不存在通往 SUBMITTED / LISTED 的路径
        listing.status = PublishStatus.DRY_RUN_COMPLETED.value
        session.flush()
        _audit(session, actor, listing, operation, key, dry_run=True, reused=False)
        return EnqueueResult(
            listing=listing,
            operation=operation,
            idempotency_key=key,
            prepared=prepared,
            dry_run=True,
        )

    existing = session.scalar(
        select(PublishAttempt).where(PublishAttempt.idempotency_key == key)
    )
    if existing is not None:
        return _reuse(session, listing, existing, operation, key, prepared, fingerprint)

    attempt = PublishAttempt(
        channel_listing_id=listing.id,
        operation=operation.value,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        status=PublishAttemptStatus.PENDING.value,
        safe_request_snapshot=_safe_snapshot(prepared),
    )
    session.add(attempt)
    session.flush()

    outbox = PublishOutbox(
        publish_attempt_id=attempt.id,
        status=PublishOutboxStatus.PENDING.value,
        next_attempt_at=_now(),
        attempt_count=0,
    )
    session.add(outbox)

    listing.status = _queued_status(operation)
    session.flush()
    _audit(session, actor, listing, operation, key, dry_run=False, reused=False)

    return EnqueueResult(
        listing=listing,
        operation=operation,
        idempotency_key=key,
        prepared=prepared,
        attempt=attempt,
        outbox=outbox,
    )


def _reuse(
    session: Session,
    listing: ChannelListing,
    existing: PublishAttempt,
    operation: PublishOperation,
    key: str,
    prepared: PreparedRequest,
    fingerprint: str,
) -> EnqueueResult:
    """同一个幂等键已经有一条尝试。**不新建,沿用它。**

    这就是 4.1 节 D 的「重试不会重复创建」在应用层的落点:运营连点两次
    「提交」,第二次走到这里,库里不会多出一条尝试,平台也不会多收一次请求。

    ## 指纹对不上意味着什么

    幂等键的输入里已经含草稿指纹(`build_publish_idempotency_key`),所以草稿
    改了键就会变。**键相同而报文指纹不同,说明报文构造本身不确定** ——
    同样的输入构造出了两份不同的 payload。那是缺陷,不是业务情况:放过去的话
    平台收到的内容与我们记录的快照对不上,而出事之后我们会拿着错的快照去对账。
    """
    if existing.request_fingerprint and existing.request_fingerprint != fingerprint:
        raise ValidationError(
            f"幂等键 {key} 已存在,但这次构造出的报文与上次不同 —— "
            "同样的输入必须构造出同样的报文。这是报文构造的缺陷,不要重试。",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=500,
        )

    outbox = session.scalar(
        select(PublishOutbox).where(PublishOutbox.publish_attempt_id == existing.id)
    )
    logger.info(
        "reused an existing publish attempt for the same idempotency key",
        extra={
            "extra_fields": {"event": "publish.enqueue_reused",
                "listing_id": str(listing.id),
                "attempt_status": existing.status,
                "operation": operation.value,
            }
        },
    )
    return EnqueueResult(
        listing=listing,
        operation=operation,
        idempotency_key=key,
        prepared=prepared,
        attempt=existing,
        outbox=outbox,
        reused=True,
        reused_terminal=(
            existing.status in TERMINAL_ATTEMPT_STATUSES
            or (outbox is not None and outbox.status == PublishOutboxStatus.DEAD.value)
        ),
    )


# ================================================================ 投递


@dataclass(frozen=True)
class _Call:
    """一次待发的调用,**脱离 session 之后仍然完整**。

    外部调用必须发生在事务之外,所以调用需要的一切都得先取出来变成普通数据。
    带着 ORM 对象跨事务用的话,访问任何一个未加载的属性都会隐式开一个新事务,
    而那个事务会一直开到调用返回 —— 正是 7.8 节禁止的「一个数据库事务跨越
    长时间外部调用」,且没有任何地方看得出来。
    """

    outbox_id: UUID
    attempt_id: UUID
    listing_id: UUID
    channel: str
    operation: str
    attempt_count: int
    prepared: PreparedRequest
    #: 领取时盖上的租约到期时刻。**只用于排查,不再做任何判据。**
    #:
    #: 它当过一阵子续租的条件。那个判据本身是对的(时间戳微秒级,别人重领
    #: 必然盖成另一个值),但它只在续租之前成立 —— 续租那一次 UPDATE 会把
    #: 库里的值改掉,而 `_Call` 里这个值不动。落库时再拿它比,比的是一个
    #: 我们自己刚刚作废掉的值。判据换成了下面的令牌
    lease_until: datetime | None = None
    #: 领取时生成的 fencing token(迁移 0047)。**续租与落库唯一的归属判据。**
    #: 为 None 表示这次调用没有执行权(存量行、或者构造方没走领取路径),
    #: 续租与落库必须在发 SQL 前显式拒绝。不能依赖 `column == None`:
    #: SQLAlchemy 会把它编译成 `IS NULL`,反而命中存量无主行
    lease_token: UUID | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


def claim_due(
    session: Session, *, limit: int = 20, now: datetime | None = None
) -> list[PublishOutbox]:
    """领一批到期的待发行,并锁住它们。

    `FOR UPDATE SKIP LOCKED` 是关键:多个 worker 同时跑时各自拿到互不相交的
    一批,而不是排队等同一批 —— 后者会让吞吐等于单实例,横向扩容毫无意义。

    同时挑出**租约已过期**的 LEASED 行:领走它的 worker 崩了。不挑的话
    那一行会永久卡住,而表现是「这个商品一直显示提交中」。

    ## `next_attempt_at IS NULL` 必须显式放行

    这一列可空,而 SQL 里 `NULL <= now` 求值是 NULL,不是真 —— 只写
    `next_attempt_at <= moment` 的话,一行没有排期的 PENDING 会**永远**
    领不到,而且它在库里看起来完全正常:状态是 PENDING,没有错误,
    只是从来不动。`enqueue()` 现在总会填上时间,所以这条路径今天走不到;
    但「今天没有人写 NULL」不是一个能靠代码保证的前提,而这种漏行没有征兆。
    空值按「现在就可以发」处理,与该列的默认语义一致。
    """
    moment = now or _now()
    stmt = (
        select(PublishOutbox)
        .where(
            PublishOutbox.status.in_(
                [PublishOutboxStatus.PENDING.value, PublishOutboxStatus.LEASED.value]
            ),
            (PublishOutbox.next_attempt_at.is_(None))
            | (PublishOutbox.next_attempt_at <= moment),
            (PublishOutbox.lease_until.is_(None)) | (PublishOutbox.lease_until <= moment),
        )
        # NULL 既然当作「现在就可以发」,排序上就得排在前面。默认 ASC 把
        # NULL 放最后,那会让上面那条放行形同虚设:行领得到,但永远排在
        # 队尾,超过 limit 时依然轮不到
        .order_by(PublishOutbox.next_attempt_at.nulls_first())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(stmt))


def _lease(session: Session, row: PublishOutbox, *, now: datetime) -> _Call | None:
    """给一行上租约,并把它需要的一切取出来。

    同时把 attempt 标成 IN_FLIGHT:7.7 节要求外部调用前必须持久化 IN_FLIGHT。
    这条标记的意义在进程崩溃时才显现 —— 没有它,一次「发出去了但没记下来」
    的调用在库里看起来和「从没发生过」完全一样。
    """
    attempt = session.get(PublishAttempt, row.publish_attempt_id)
    if attempt is None:
        # 外键是 CASCADE,所以这不该发生。真发生了说明有人手工删过数据
        row.status = PublishOutboxStatus.DEAD.value
        row.last_error = "对应的 attempt 不存在"
        return None
    listing = session.get(ChannelListing, attempt.channel_listing_id)
    if listing is None:
        row.status = PublishOutboxStatus.DEAD.value
        row.last_error = "对应的 listing 不存在"
        return None

    row.status = PublishOutboxStatus.LEASED.value
    lease_until = now + timedelta(seconds=policy.LEASE_SECONDS)
    row.lease_until = lease_until
    # 每一次领取生成一个新令牌(迁移 0047)。**上一任的令牌从此作废** ——
    # 它的落库会打在 `WHERE lease_token = :token` 上,影响 0 行。
    # 令牌由领取方直接生成而不是读一次再加一:"读-改-写"在两个 worker
    # 之间正是这里要防的那件事。与 `batch_service.claim_items` 逐字同源
    lease_token = uuid4()
    row.lease_token = lease_token
    row.attempt_count = (row.attempt_count or 0) + 1

    attempt.status = PublishAttemptStatus.IN_FLIGHT.value
    attempt.started_at = attempt.started_at or now

    snapshot = dict(attempt.safe_request_snapshot or {})
    return _Call(
        outbox_id=row.id,
        attempt_id=attempt.id,
        listing_id=listing.id,
        channel=listing.channel,
        operation=attempt.operation,
        attempt_count=row.attempt_count,
        lease_until=lease_until,
        lease_token=lease_token,
        prepared=PreparedRequest(
            method=str(snapshot.get("method") or "POST"),
            path=str(snapshot.get("path") or ""),
            query=dict(snapshot.get("query") or {}),
            body=dict(snapshot.get("body") or {}),
            headers=dict(snapshot.get("headers") or {}),
            idempotency_key=attempt.idempotency_key,
        ),
    )


def run_due(
    session_factory: Callable[[], Session],
    *,
    limit: int = 20,
    now: datetime | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """扫一遍到期的 outbox,把它们发出去。返回本次统计。

    三段事务,中间**故意断开**:

        领取 + 标 IN_FLIGHT   一个事务,commit  ← 7.8 的 checkpoint
        发请求                 没有事务
        保存结果               每一件一个事务,commit

    每件一个事务而不是整批一个:整批一个的话第 12 件超时会让前 11 件的结果
    一起悬在未提交状态,而那 11 次调用已经真的发生了 —— 钱花了、平台上有东西了,
    库里什么都没有。同 `batch_service.run_batch` 的取舍。
    """
    moment = now or _now()
    # `lost_lease` 预置成 0 而不是用到时才建:调用方(运维页、Celery 任务的
    # 返回值)会直接读这几个键,少一个键就是一个 KeyError,而它恰好只在
    # "从来没丢过租约"的正常情况下不出现 —— 那种缺陷只会在出事那天暴露
    stats = {
        "claimed": 0,
        "succeeded": 0,
        "retry": 0,
        "failed": 0,
        "unknown": 0,
        "lost_lease": 0,
        # `stale` 与 `lost_lease` 是同一件事的两个时刻:租约在**发之前**丢的
        # 记 lost_lease(没花钱),在**发之后**才发现丢了的记 stale(钱花了、
        # 结论只进审计)。分开计数是因为处置不同 —— 前者说明续租窗口偏紧,
        # 后者说明真实调用耗时已经越过 LEASE_SECONDS,那是要调参数的信号
        "stale": 0,
    }

    session = session_factory()
    try:
        rows = claim_due(session, limit=limit, now=moment)
        calls = [c for c in (_lease(session, r, now=moment) for r in rows) if c is not None]
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    stats["claimed"] = len(calls)
    for call in calls:
        # **发之前先续租**(BLOCK-16)。
        #
        # 上面那个事务给这一批 20 行都盖了 `now + LEASE_SECONDS`(180 秒),
        # 然后这里逐件发真实外部请求。一次调用按 30 秒算,排在第 7 位之后的
        # 那些记录,轮到它时租约**已经过期** —— 而 `claim_due` 恰恰会把
        # 「LEASED 且租约过期」的行当成"领它的 worker 崩了"重新发出去。
        # 于是同一次上架被发两遍:平台侧多一个商品,或者幂等键撞上之后
        # 我们把别人那次的结果记成自己的。
        #
        # 续租是一个条件 UPDATE:只有当库里的 `lease_until` 仍然是**我们**
        # 盖的那个值时才延期。被别人重领过的话 rowcount = 0,这一件直接跳过 ——
        # 让新的持有者去发,而不是两个都发。
        if not _renew_lease(session_factory, call):
            stats["lost_lease"] = stats.get("lost_lease", 0) + 1
            logger.warning(
                "lease lost before dispatch; skipping to avoid a duplicate publish",
                extra={
                    "extra_fields": {"event": "publish.lease_lost_before_dispatch",
                        "outbox_id": str(call.outbox_id),
                        "attempt_id": str(call.attempt_id),
                    }
                },
            )
            continue
        outcome = _invoke(call, options=options or {})
        # **不传 moment。** 一批 20 件、每件一次真实外部调用,跑完可能是几分钟后。
        # 拿批次开始的时刻去算 `next_attempt_at = moment + 退避`,退避就被
        # 白白压缩掉了那几分钟;`finished_at` 记的也会是批次开始时刻,
        # 于是"这次调用花了多久"从一开始就是错的。
        # `now` 参数留给测试注入,正常路径让 `_save` 自己取。
        bucket = _save(session_factory, call, outcome, now=now)
        stats[bucket] = stats.get(bucket, 0) + 1
    return stats


def _renew_lease(
    session_factory: Callable[[], Session], call: _Call, *, now: datetime | None = None
) -> bool:
    """把这一行的租约续到"从现在起再 LEASE_SECONDS"。**续不上就别发。**

    条件 UPDATE 而不是"读出来改一下":读改写之间同样有窗口,而这个函数
    存在的全部意义就是关掉窗口。

    ## 判据从 `lease_until` 换成了 `lease_token`(A45-batch17-2)

    原来的判据是"库里的 `lease_until` 仍然等于我们领取时盖的那个值"。
    那句话**在这一行是对的**(时间戳微秒级,别人重领必然盖成另一个值),
    问题在它只对这一次成立:下面这条 UPDATE 一旦跑成功,库里的值就被我们
    自己改掉了,而 `call.lease_until` 不动。于是同一个判据到了
    `_save()` 那一步会**恒假** —— 那正是落库端一直没有归属校验的原因,
    也正是那个缺口不能靠"把同一个条件抄过去"来关的原因。

    令牌在续租时不变、在重领时必变,两处可以用同一份判据,
    与 `batch_service.renew_lease` / `apply_outcome` 是同一套口径。

    返回 False 有三种情况,处理方式相同(跳过):

        令牌被别人重领     对方会去发,我们再发就是发两遍
        状态已经不是 LEASED   别的路径(取消、置死)动过它
        自己没有令牌       存量行,或者调用方没走领取路径 —— 都没有执行权

    **单独一个短事务。** 借用外面那个 session 的话,这个更新会一直不提交,
    直到本轮全部调用结束 —— 那就等于没续。
    """
    # SQLAlchemy 会把 ``column == None`` 编译成 ``IS NULL``,不是
    # ``column = NULL``。因此不能依赖 SQL 三值逻辑把无令牌调用挡在外面:
    # 存量的 NULL 行反而会被它匹配。无令牌就是没有执行权,在开事务前拒绝。
    if call.lease_token is None:
        return False
    moment = now or _now()
    session = session_factory()
    try:
        result = session.execute(
            update(PublishOutbox)
            .where(
                PublishOutbox.id == call.outbox_id,
                PublishOutbox.status == PublishOutboxStatus.LEASED.value,
                PublishOutbox.lease_token == call.lease_token,
            )
            .values(lease_until=moment + timedelta(seconds=policy.LEASE_SECONDS))
        )
        session.commit()
        return bool(result.rowcount)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _invoke(call: _Call, *, options: Mapping[str, Any]) -> policy.Outcome:
    """真正发出去。**这里没有打开的事务。**

    任何异常都收敛成「结果未知」而不是往上抛:抛出去的话整批投递会中断,
    而这一件的 outbox 停在 LEASED —— 要等租约过期才有人管。更重要的是,
    异常被吞掉之后 Celery 显示成功是 7.8 节明令禁止的,所以这里不是「吞」:
    结论会被完整地写进 attempt 与 outbox。
    """
    try:
        transport = registry.transport_for(call.channel)
        resp = transport.submit(call.prepared, options)
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        # 只记异常类型,不记原文:客户端库的异常原文常常整条带着 URL 与凭证
        return policy.classify_transport_failure(
            operation=call.operation, error=type(exc).__name__
        )

    if resp.timed_out:
        return policy.classify_transport_failure(
            operation=call.operation, error=resp.error or "timeout"
        )
    return policy.classify_response(
        status_code=resp.status_code,
        body=resp.body,
        headers=resp.headers,
        operation=call.operation,
    )


def _save(
    session_factory: Callable[[], Session],
    call: _Call,
    outcome: policy.Outcome,
    *,
    now: datetime | None = None,
) -> str:
    """在**新事务**里保存一次投递的结果。返回统计分桶名。

    `now=None` 时取当下 —— 退避与结束时间要以**这一件做完的时刻**为准,
    不是整批开始的时刻(见 `run_due`)。

    ## 归属校验(A45-batch17-2 / P2-1):这个函数原来没有 WHERE

    它原来是"取三行、无条件写三行"。于是下面这条路是通的:

        1. worker A 领走,租约 180 秒,发出真实外部调用
        2. 调用挂住超过 180 秒
        3. `claim_due()` 把「LEASED 且租约过期」当成"A 崩了"重新发出
        4. worker B 领走同一行、发同一把幂等键的同一份报文、成功,
           落 DONE / SUCCEEDED / LISTED + external_spu_id
        5. A 的调用这时才超时返回 —— CREATE 超时被判成不可重试的 UNKNOWN,
           于是 A **无条件**把 attempt 从 SUCCEEDED 改成 UNKNOWN、
           outbox 从 DONE 改成 DEAD、listing 从 LISTED 退回 SUBMIT_RESULT_UNKNOWN

    第 5 步覆盖掉的是**较新的事实**:商品在平台上确实上架了,而界面会说
    "结果未知、需人工"。批次链路的同型缺口在 A43 / BLOCK-02 用令牌关掉了,
    发布这条当时没跟着改 —— `DECISIONS.md` §3.19 论证的是"不会重复创建"
    (那一条今天仍然成立,三道防线都在),不覆盖"结果不会被回写"。

    现在的形状与 `batch_service.apply_outcome` 一致:**执行权不在自己手上时,
    结果只进审计,不进这三张表。** 它是一份 stale outcome —— 调用真的发生过,
    记录要留,但它不能改变一个已经由别人决定了的状态。
    """
    moment = now or _now()
    session = session_factory()
    try:
        row = session.get(PublishOutbox, call.outbox_id)
        attempt = session.get(PublishAttempt, call.attempt_id)
        listing = session.get(ChannelListing, call.listing_id)
        if row is None or attempt is None or listing is None:
            session.rollback()
            return "failed"
        if not _still_holds(session, call):
            _record_stale_outcome(session, call, outcome, listing=listing)
            session.commit()
            return "stale"
        bucket = apply_outcome(
            session, row=row, attempt=attempt, listing=listing, outcome=outcome, now=moment
        )
        session.commit()
        return bucket
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _still_holds(session: Session, call: _Call) -> bool:
    """这次调用现在还有没有这一行的执行权。**判据只有令牌。**

    ## 为什么不带 `status == LEASED`

    带上它,`reconcile_unknown` 那条路会全线落不了库:确认走的是一行
    **DEAD** 的 outbox(结果未知且不可重试时 `apply_outcome` 把它落成 DEAD),
    而确认本身是一次真实调用、它的结论必须写得进去。令牌自己就够:
    重领会换掉它,落终态会清掉它(见 `apply_outcome`),人工重投会吊销它
    (见 `redeliver_dead`)—— 三种"执行权易主"都表现为同一件事,
    条件更少反而判得更准。

    ## 为什么是 `SELECT … FOR UPDATE` 而不是一条条件 UPDATE

    批次那边用的是条件 UPDATE + rowcount,因为它一次只写一张表。这里的
    `apply_outcome` 要写三张(outbox / attempt / listing),一条 UPDATE 的
    rowcount 管不到另外两张 —— 校验和写入之间必须没有窗口,否则挡住的只是
    三分之一。行锁在本事务提交前一直握着,而 `claim_due` 用的是
    `FOR UPDATE SKIP LOCKED`:一个想在这中间重领的 worker 会跳过这一行,
    不会排队等,也不会插进来。

    `call.lease_token` 为 None 时(存量行、或者构造 `_Call` 时没走领取路径)
    必须显式返回 False。SQLAlchemy 会把 ``column == None`` 编译成
    ``IS NULL``,若直接拼进 WHERE,反而会匹配存量 NULL 行。方向取安全的一侧:
    宁可让一次结果只进审计,也不要让它盖掉别人的结论。
    """
    if call.lease_token is None:
        return False
    return (
        session.scalar(
            select(PublishOutbox)
            .where(
                PublishOutbox.id == call.outbox_id,
                PublishOutbox.lease_token == call.lease_token,
            )
            .with_for_update()
        )
        is not None
    )


def _record_stale_outcome(
    session: Session,
    call: _Call,
    outcome: policy.Outcome,
    *,
    listing: ChannelListing,
) -> None:
    """执行权已经丢了,但这次调用确实发生过。**记录,不覆盖。**

    进审计而不是只打日志,理由与 `batch_service._record_stale_outcome` 同:
    这次调用的结论在业务表里**不会**留下任何痕迹(那正是本次修复要的),
    于是"平台那边到底发生了什么"没有落脚点。审计是追加的,它是唯一
    还能回答这个问题的地方。

    日志里同时点名 WARNING:这件事意味着同一次提交被投递了两遍 ——
    幂等键保证平台上不会多出商品,但它不是正常运行的一部分,
    值得有人去看一眼 `LEASE_SECONDS` 和真实调用耗时的关系。
    """
    audit.record(
        session,
        actor="system",
        action=AuditAction.UPDATE,
        entity_type="PublishOutbox",
        entity_id=call.outbox_id,
        payload={
            "action": "stale_outcome",
            "publish_attempt_id": str(call.attempt_id),
            "channel_listing_id": str(listing.id),
            "operation": call.operation,
            "attempt_count": call.attempt_count,
            # 结论本身照记 —— 它是这次调用真实拿到的东西,只是不再有资格
            # 决定这一行的状态
            "attempt_status": outcome.attempt_status,
            "platform_status": outcome.platform_status,
            "external_spu_id": outcome.external_spu_id,
            "error_code": outcome.error_code,
            "error_message": (outcome.error_message or "")[:500],
        },
    )
    logger.warning(
        "discarded a delivery outcome that no longer matches the current attempt",
        extra={
            "extra_fields": {"event": "publish.stale_outcome",
                "outbox_id": str(call.outbox_id),
                "attempt_id": str(call.attempt_id),
                "listing_id": str(listing.id),
                "attempt_status": outcome.attempt_status,
            }
        },
    )


def apply_outcome(
    session: Session,
    *,
    row: PublishOutbox,
    attempt: PublishAttempt,
    listing: ChannelListing,
    outcome: policy.Outcome,
    now: datetime | None = None,
) -> str:
    """把一次投递的结论落到三张表上。返回统计分桶名。

    ## 「发布失败不改变本地为成功」(4.1 节 D)

    这条在代码里的落点是下面那个 `if`:只有**接受了且拿到了外部 ID**才写
    external_spu_id 与成功状态。少一个条件都不行 —— 接受但没有 ID 时,
    `publish_policy` 已经把它判成 UNKNOWN,这里再兜一次是因为这条约束
    值得有两处独立的守卫:它错了没有任何报错,只会让一个不存在的商品
    在我们的看板上显示成已上架。
    """
    moment = now or _now()
    final = outcome
    if outcome.retriable and policy.exhausted(row.attempt_count or 0):
        final = policy.settle_exhausted(outcome)

    attempt.status = final.attempt_status
    attempt.error_code = final.error_code
    attempt.error_message = (final.error_message or None)
    attempt.safe_response_snapshot = redact(
        {
            "platform_status": final.platform_status,
            "external_spu_id": final.external_spu_id,
            "message": final.error_message,
        }
    )
    if final.attempt_status != PublishAttemptStatus.IN_FLIGHT.value:
        attempt.finished_at = moment

    if final.platform_status:
        # 平台原话,照存不翻译。本地状态是我们的解读,这一列是平台说的
        listing.last_platform_status = final.platform_status
    if final.listing_status is not None:
        listing.status = final.listing_status

    if final.succeeded and final.external_spu_id:
        _record_external_id(listing, final.external_spu_id)
        if final.external_sku_ids:
            listing.external_sku_ids = dict(final.external_sku_ids)
        row.status = PublishOutboxStatus.DONE.value
        row.lease_until = None
        # 落终态就吊销令牌:这一件已经有结论了,任何还拿着旧令牌的执行体
        # 都不该再写它(`_still_holds` 会判假)。与批次那边 `_outcome_values`
        # 里那一行同源
        row.lease_token = None
        row.next_attempt_at = None
        row.last_error = None
        session.flush()
        return "succeeded"

    if final.retriable:
        wait = policy.backoff_seconds(
            row.attempt_count or 0, retry_after=final.retry_after_seconds
        )
        row.status = PublishOutboxStatus.PENDING.value
        row.lease_until = None
        row.lease_token = None
        row.next_attempt_at = moment + timedelta(seconds=wait)
        row.last_error = (final.error_message or "")[:2000]
        session.flush()
        return "retry"

    # 不可重试:进人工。DEAD 不是「失败」的同义词,是「需要人来看一眼」
    row.status = PublishOutboxStatus.DEAD.value
    row.lease_until = None
    row.lease_token = None
    row.next_attempt_at = None
    row.last_error = (final.error_message or "")[:2000]
    session.flush()
    logger.warning(
        "giving up on this delivery after repeated failures",
        extra={
            "extra_fields": {"event": "publish.delivery_dead",
                "listing_id": str(listing.id),
                "attempt_status": final.attempt_status,
                "error_code": final.error_code,
                "attempts": row.attempt_count,
            }
        },
    )
    return "unknown" if final.needs_human else "failed"


# ================================================================ 未知结果的确认


def reconcile_unknown(
    session_factory: Callable[[], Session],
    attempt_id: UUID,
    *,
    options: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """确认一次结果未知的提交到底有没有到达平台(7.4 节)。

    ## 它为什么是一条**单独的**函数,而不是重试路径上的一个开关

    自动重试和「带着同一个幂等键去确认」在网络层是同一个动作:发同一份报文。
    差别全在语义 —— 前者预期平台会执行它,后者预期平台会说「这个键我见过」。
    做成一个 `confirm=True` 的参数,两种意图在代码里就长得一模一样,而
    7.4 节禁止的正是把「确认」误当成「重发创建」。让它们是两个函数名,
    调用点在做哪一件事就一眼看得出来。

    ## 为什么这样是安全的

    幂等键不变,所以平台要么返回 409(带回既有 ID)、要么返回它上一次的结果。
    两种都是确认,都不会多出商品。真正危险的是**换一个键**再发一次 ——
    那才是重新创建,而这个函数从头到尾没有算过新键。
    """
    moment = now or _now()
    session = session_factory()
    try:
        attempt = session.get(PublishAttempt, attempt_id)
        if attempt is None:
            raise ValidationError("找不到这条提交尝试", code=ErrorCode.NOT_FOUND, http_status=404)
        if attempt.status != PublishAttemptStatus.UNKNOWN.value:
            raise ValidationError(
                f"只有结果未知的尝试才需要确认,这一条是 {attempt.status}",
                code=ErrorCode.INPUT_INVALID,
                http_status=409,
            )
        listing = session.get(ChannelListing, attempt.channel_listing_id)
        row = session.scalar(
            select(PublishOutbox).where(PublishOutbox.publish_attempt_id == attempt.id)
        )
        if listing is None or row is None:
            raise ValidationError(
                "这条尝试缺少 listing 或 outbox,无法确认",
                code=ErrorCode.INTERNAL_ERROR,
                http_status=500,
            )
        # 确认也是一次真实的对外调用,必须计数。
        #
        # 不计数的话 `policy.exhausted()` 对确认路径永远不成立 —— 一个平台
        # 一直不给准信的 listing 可以被无限次"确认",而每一次都是一次真的
        # 请求。重试有上限、确认没有,这件事不该只存在于某个人的记忆里。
        row.attempt_count = (row.attempt_count or 0) + 1
        # **确认也要领一个令牌**(A45-batch17-2)。
        #
        # `_save()` 从本批起要求执行权,而确认这条路原来一个都没领 ——
        # 不领的话它的结论会被当成 stale 丢进审计,也就是"点了确认、
        # 界面纹丝不动",而那比被覆写更难查。
        #
        # **不动 `status`。** 确认走的通常是一行 DEAD 的 outbox,把它改成
        # LEASED 会让进程恰好死在这中间的那一次变成:租约过期 → `claim_due`
        # 重新领走 → 自动再发一次。DEAD 的语义就是"不再自动投递",
        # 一次人工确认不该把它悄悄改回自动重试。令牌不需要状态配合:
        # 它自己就能回答"这次调用还有没有资格落库"。
        row.lease_token = uuid4()
        call = _Call(
            outbox_id=row.id,
            attempt_id=attempt.id,
            listing_id=listing.id,
            channel=listing.channel,
            operation=attempt.operation,
            attempt_count=row.attempt_count,
            lease_token=row.lease_token,
            prepared=PreparedRequest(
                method=str((attempt.safe_request_snapshot or {}).get("method") or "POST"),
                path=str((attempt.safe_request_snapshot or {}).get("path") or ""),
                # query 不能漏。`_lease()` 里带了,这里以前没带 —— 于是「确认」
                # 发出去的报文和原始报文不是同一份,而 7.4 节要防的正是这个。
                # 今天 `generic.build_request` 恒发 query={} 所以打不出来,
                # 而这恰恰最危险:第一个用 query 的真实渠道接进来时,
                # 出问题的是确认路径,也就是只在结果未知时才走到的那条。
                query=dict((attempt.safe_request_snapshot or {}).get("query") or {}),
                body=dict((attempt.safe_request_snapshot or {}).get("body") or {}),
                headers=dict((attempt.safe_request_snapshot or {}).get("headers") or {}),
                idempotency_key=attempt.idempotency_key,
            ),
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    outcome = _invoke(call, options=options or {})
    return _save(session_factory, call, outcome, now=moment)


# ================================================================ 明确失败的重新投递


def redeliver_dead(
    session: Session,
    attempt_id: UUID,
    *,
    actor: str,
    note: str,
    now: datetime | None = None,
) -> PublishOutbox:
    """把一条已经放弃自动投递的 Outbox 重新排队(A45-batch12-2 / EX-04)。

    ## 为什么必须有这条路

    退避耗尽(`MAX_DELIVERY_ATTEMPTS = 6`)之后 Outbox 落 DEAD。DEAD 的语义
    是「不再自动投递」,不是「这件事结束了」—— 平台限流两小时、网关抽风、
    凭据临时过期,六次之内没恢复的都会落到这里,而它们**过一会儿就好了**。

    但 DEAD 之前没有任何出口:`claim_due` 不取 DEAD,`enqueue()` 碰到相同
    幂等键会命中终态 attempt 直接 `reused_terminal=True` 返回、不建新 Outbox。
    于是运营唯一能做的事是**改草稿制造一把新幂等键**再提交一次 —— 而那正是
    真正会在平台上多出一件商品的操作。闸门没有出口时,人会自己找一条更危险的路。

    ## 为什么复用原 attempt 而不是新建

    幂等键挂在 `PublishAttempt` 上。复用它意味着重新投递发的是**同一份报文、
    同一把键**:平台要么执行它,要么告诉我们这个键见过。新建 attempt 会算出
    新键,那不叫重试,那叫再上架一次。

    与 `reconcile_unknown` 的分工:那一条处理「不知道到没到」,这一条处理
    **「明确知道没到」** —— 六次都收到了平台的错误响应。两者都不新建键。

    ## 为什么必须填说明,以及为什么行锁

    说明进审计:这是一个由人决定「再发一次」的动作,事后要能回答是谁、
    依据什么。行锁挡的是两个运营同时点 —— 没有它,两次 UPDATE 都会成功,
    投递计数被清两次,worker 可能领到两次。
    """
    moment = now or _now()
    attempt = session.get(PublishAttempt, attempt_id)
    if attempt is None:
        raise ValidationError("找不到这条提交尝试", code=ErrorCode.NOT_FOUND, http_status=404)
    if not (note or "").strip():
        raise ValidationError(
            "重新投递必须填写说明(为什么认为现在发得出去),它会进审计",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
        )
    row = session.scalar(
        select(PublishOutbox)
        .where(PublishOutbox.publish_attempt_id == attempt.id)
        # 与 `claim_due` 同一条理由:两个运营同时点的话,没有锁的两次 UPDATE
        # 都会成功
        .with_for_update()
    )
    if row is None:
        raise ValidationError(
            "这条尝试没有对应的投递记录,无法重新投递",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=500,
        )
    if row.status != PublishOutboxStatus.DEAD.value:
        # 只放行 DEAD。PENDING / LEASED 还在自动重试路径上,再排一次只会
        # 让同一份报文被投两遍;DONE 重排则是真的会多一件商品
        raise ValidationError(
            f"只有已放弃投递(DEAD)的记录才需要重新投递,这一条是 {row.status}",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    # `attempt_count` 归零:退避与上限从这一刻重新开始算。
    #
    # 不归零的话这一行会立刻再次 `exhausted()`,重新投递等于什么都没做 ——
    # 而那种\"点了没反应\"是最难排查的一类。归零安全的前提是这个动作由人发起:
    # 自动路径永远走不到这里,所以上限约束的\"无人值守\"语义没有被破坏。
    row.attempt_count = 0
    row.status = PublishOutboxStatus.PENDING.value
    row.next_attempt_at = moment
    row.lease_until = None
    # **吊销令牌**(A45-batch17-2):重新排队意味着执行权回到队列手里。
    # 还有一次确认在飞的话,它回来时会发现自己的令牌已经作废,结果只进审计 ——
    # 人工发起的这个动作赢,而不是被一个几分钟前发出去的调用悄悄盖掉。
    # 与批次回收器 `reap_expired_leases` 吊销令牌是同一个动作
    row.lease_token = None
    # 保留 last_error:它是\"上一次为什么死的\",重新投递不该把它抹掉 ——
    # 这一次要是又死了,两次的原因对比正是排查的依据
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="PublishOutbox",
        entity_id=row.id,
        payload={
            "action": "redeliver_dead",
            "publish_attempt_id": str(attempt.id),
            "operation": attempt.operation,
            # 键本身不入审计正文(它由上游指纹算出,进日志没有意义且可能很长),
            # 但\"复用了原键\"这件事必须记下来 —— 它是\"没有多上架一件\"的依据
            "reused_idempotency_key": True,
            "previous_error": (row.last_error or "")[:500],
            "note": note.strip()[:500],
        },
    )
    logger.warning(
        "the redelivery gave up as well",
        extra={
            "extra_fields": {"event": "publish.redeliver_dead",
                "outbox_id": str(row.id),
                "attempt_id": str(attempt.id),
                "actor": actor,
            }
        },
    )
    return row


# ================================================================ 内部


def _requested(requested: PublishOperation | str | None) -> PublishOperation | None:
    """把调用方声明的操作类型解出来,**解不开就当没声明**。

    这里刻意不校验:真正的校验在 `policy.resolve_operation` 里,而它需要
    listing 才能判断(有没有 external_spu_id)。这个函数只回答一个更早的
    问题 —— 「要不要先跑草稿状态检查」,而那个问题在拿到 listing 之前就得
    有答案。解不开时返回 None,于是走正常路径去 `resolve_operation`,
    由它抛出那条带错误码的异常。在这里抢先抛,错误信息会更差。
    """
    if requested is None:
        return None
    try:
        return PublishOperation(str(requested).upper())
    except ValueError:
        return None


def _queued_status(operation: PublishOperation) -> str:
    """排队之后 listing 落在哪个状态。

    写成表而不是 if 链,理由同 `generic._ROUTES`:加一个操作类型时漏改一处的
    代价是「下架排队了,页面上显示提交中」,而运营会以为在上架。

    注意四种操作只落三个状态:CREATE 与其余的区别在这里不重要,重要的是
    **它们都不是终态**。真正的结论由平台给,经 `apply_outcome` 或轮询写入。
    """
    return {
        PublishOperation.UPDATE: PublishStatus.UPDATING.value,
        PublishOperation.REPUBLISH: PublishStatus.REPUBLISHING.value,
        PublishOperation.DELIST: PublishStatus.DELISTING.value,
    }.get(operation, PublishStatus.SUBMITTING.value)


def _assert_submittable(draft: ListingDraft) -> None:
    if draft.status in SUBMITTABLE_DRAFT_STATUSES:
        return
    if draft.status == DraftStatus.STALE.value:
        raise ValidationError(
            "草稿已过期:上游的属性、图片集或文案变过了。"
            "提交上去的会是旧内容,而页面上显示的是新内容。请先刷新草稿。",
            code=ErrorCode.DRAFT_STALE,
            http_status=409,
        )
    raise ValidationError(
        f"草稿状态 {draft.status} 不允许提交;可提交的状态:"
        f"{', '.join(sorted(SUBMITTABLE_DRAFT_STATUSES))}",
        code=ErrorCode.INPUT_INVALID,
        http_status=409,
    )


def _get_or_create_listing(
    session: Session,
    *,
    channel: str,
    site: str,
    shop_id: str,
    product: Product,
    draft: ListingDraft,
) -> ChannelListing:
    listing = session.scalar(
        select(ChannelListing).where(
            ChannelListing.channel == channel,
            ChannelListing.shop_id == shop_id,
            ChannelListing.site == site,
            ChannelListing.product_id == product.id,
        )
    )
    if listing is None:
        listing = ChannelListing(
            channel=channel,
            site=site,
            shop_id=shop_id,
            product_id=product.id,
            draft_id=draft.id,
            status=PublishStatus.VALIDATED.value,
        )
        session.add(listing)
        session.flush()
        return listing
    # 指向最新的草稿。历史草稿仍然被 attempt 的快照引着,不会丢
    listing.draft_id = draft.id
    return listing


def _mapped_from_draft(draft: ListingDraft) -> MappedListing:
    """用草稿里**已经校验过的那份** mapped_payload 构造映射结果。

    不在这里重跑 `map_fields`:重跑会读当时的上游,而上游可能已经变了 ——
    那样提交上去的内容与运营在页面上批准的那一份不是同一份,且没有任何
    地方会提示。草稿过期由 `STALE` 负责发现(见 `_assert_submittable`),
    不由这里悄悄地重新映射来「修正」。
    """
    payload = draft.mapped_payload or {}
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    if not header and not rows:
        raise ValidationError(
            "草稿还没有映射结果,不能提交。先刷新草稿。",
            code=ErrorCode.CHANNEL_FIELD_MISSING,
            http_status=409,
        )
    return MappedListing(
        header=dict(header),
        rows=tuple(dict(r) for r in rows if isinstance(r, dict)),
    )


def _record_external_id(listing: ChannelListing, external_spu_id: str) -> None:
    """外部 ID **只在第一次拿到时写入**,之后不许被响应覆盖。

    ## 这条守卫挡的是什么

    一次成功的 UPDATE 也会带回一个 external_spu_id。真实平台通常原样回显,
    但没有任何东西保证这一点 —— 当前的 Simulator 就会为每次 UPDATE 生成一个
    新的 `SIM-...` ID(它的 ID 里编码了创建时刻,而更新发生在另一个时刻)。

    照单全收的代价很具体:listing 上那一列被改成了另一个值,而**平台上那个
    真正存在的商品**从此不在我们的任何清单里。4.1 节 H 的清理预案靠这一列
    列出「本轮测试创建了哪些外部商品」,于是它会漏掉一个,而且没人说得清
    漏了几个 —— 那正是 4.1 节 H 要防的结局本身。

    不一致时记警告而不是抛错:这次更新在平台上是**成功**的,把它翻成失败会
    让运营重试,而重试改变不了任何事。需要人去核对的是那两个 ID,不是这次提交。
    """
    if not listing.external_spu_id:
        listing.external_spu_id = external_spu_id
        return
    if listing.external_spu_id != external_spu_id:
        logger.warning(
            "the platform reported a different external id than the one on record",
            extra={
                "extra_fields": {"event": "publish.external_id_mismatch",
                    "listing_id": str(listing.id),
                    "known": listing.external_spu_id,
                    "returned": external_spu_id,
                }
            },
        )


def _with_key(req: PreparedRequest, key: str) -> PreparedRequest:
    """把幂等键塞进报文。

    `build_request` 刻意不算它 —— 算它需要 shop_id / site / 草稿指纹,
    而那些是运行期上下文,不属于「字段映射」这件事(见 generic.build_request)。
    """
    return PreparedRequest(
        method=req.method,
        path=req.path,
        query=dict(req.query),
        body=dict(req.body),
        headers=dict(req.headers),
        idempotency_key=key,
    )


def safe_preview(req: PreparedRequest) -> dict[str, Any]:
    """脱敏报文,给接口层。**这是 `_safe_snapshot` 唯一的对外出口。**

    6.4 节「发布前」要求页面能看到干跑请求,而干跑的全部产出就是这份报文。
    公开一个薄封装而不是让 `api/` 直接调私有函数:私有名一改,调用点是
    静态查不出来的(它在另一个包里),而这份报文是**出事之后第一个要看的
    东西** —— 它不该依赖一个随时可能被重命名的名字。

    不另起一套脱敏逻辑。两套脱敏迟早会漂移,而漂移的方向一定是新的那套
    漏掉一个字段 —— 漏掉的那次没有任何征兆,直到某个密钥出现在界面上。
    """
    return _safe_snapshot(req)


def _safe_snapshot(req: PreparedRequest) -> dict[str, Any]:
    """脱敏后的请求快照。4.1 节 D 的「请求和响应脱敏审计」。

    `headers` 也过一遍 `redact()`:`build_request` 现在不放凭证,但真实
    Adapter 将来会在 header 里带签名,而那时没有人会回来改这一行。
    列名带 `safe_` 前缀就是为了让「直接塞原始请求」看起来不对劲。
    """
    return redact(
        {
            "method": req.method,
            "path": req.path,
            "query": dict(req.query),
            "body": dict(req.body),
            "headers": dict(req.headers),
        }
    )  # type: ignore[return-value]


def _audit(
    session: Session,
    actor: str,
    listing: ChannelListing,
    operation: PublishOperation,
    key: str,
    *,
    dry_run: bool,
    reused: bool,
) -> None:
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="ChannelListing",
        entity_id=listing.id,
        payload={
            "operation": operation.value,
            "idempotency_key": key,
            "dry_run": dry_run,
            "reused": reused,
            "channel": listing.channel,
            "site": listing.site,
            # 4.1 节 H:真实提交要留下可检索的标记。审计里也留一份,
            # 这样即使 listing 行后来被改动过,批次归属仍然查得到
            "test_batch_tag": listing.test_batch_tag,
        },
    )
