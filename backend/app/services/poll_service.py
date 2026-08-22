"""平台状态轮询与驳回回流(方案 v4.1 任务 15 / 4.1 节 D / 7.3 节 / 7.8 节)。

提交那一半在 `publish_service.py`:我们把商品发上去,平台回一个响应。
这一半回答的是**之后**的问题:平台审核完了没有、结论是什么、被拒了要怎么办。

两者共用同一套事务边界纪律(7.8 节),因为它们踩的是同一个坑:
一个数据库事务不许跨越一次外部调用。

## 事务边界

    领取事务    `claim_pollable` + `_reserve`:选出到期的行,推进 `next_poll_at`,
                commit
    事务外      真的去问平台
    新事务      每一件一个事务保存结果

## 「推进 next_poll_at」就是租约,所以这张表不需要租约列

`publish_outbox` 有 `lease_until`,因为投递是**写**:worker 崩在半路时,那一行
必须停在 LEASED 让别人知道"这次可能已经发出去了"。轮询是**读**,崩在半路
什么也没发生 —— 于是"领取"可以退化成一件更简单的事:先把下次可问的时间推到
未来再去问。崩了的后果是这一行晚一个退避周期被重新问到,而那正是我们想要的
行为,不需要任何回收机制。

反过来说,`next_poll_at` **必须在外部调用之前就提交**。放在之后的话,两个
worker 会同时问同一行(各自都看到它到期),而轮询虽然便宜,平台的限流配额
不便宜 —— 那是 4.1 节 D 要求「状态轮询有退避」的另一半理由。

## 驳回回流的落点

平台说 REJECTED 时,这里除了改状态,还会往 `platform_rejections` 写一条 ——
走 `platform_service.record_api_rejection()`,而不是运营手工录入用的
`record_rejection()`。理由写在那个函数的文档里(API 上架的商品可能一次
Excel 都没导过,而 `locate_export()` 会因此拒绝记录)。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels import registry
from app.core.clock import utc_now
from app.core.enums import PublishOutboxStatus, PublishStatus
from app.core.logging import get_logger
from app.models.listing_copy import ListingDraft
from app.models.product import Product
from app.models.publishing import ChannelListing, PublishAttempt, PublishOutbox
from app.workbench import platform_service
from app.workflows import poll_policy as policy

logger = get_logger(__name__)

#: 一次扫描最多问几行。比投递的 20 大,因为轮询是只读的、也不花钱;
#: 但仍然有上限 —— 无上限的批次会让一次扫描的耗时随上架量线性增长,
#: 而 beat 的下一拍不会等它
DEFAULT_POLL_LIMIT = 50


def _now() -> datetime:
    """现在。同 `publish_service._now`,两者都只是 `core/clock.utc_now` 的转发。

    别在这里改写成 `datetime.now(UTC).replace(...)`:理由见 `core/clock.py` 顶部。
    """
    return utc_now()


# ================================================================ 领取


@dataclass(frozen=True)
class _Probe:
    """一次待发的状态查询,**脱离 session 之后仍然完整**。

    同 `publish_service._Call`:带着 ORM 对象跨事务用的话,访问任何一个未加载
    的属性都会隐式开一个新事务,而那个事务会一直开到调用返回 —— 正是 7.8 节
    禁止的那种跨越,且没有任何地方看得出来。
    """

    listing_id: UUID
    channel: str
    external_spu_id: str
    current_status: str
    poll_count: int


def claim_pollable(
    session: Session, *, limit: int = DEFAULT_POLL_LIMIT, now: datetime | None = None
) -> list[ChannelListing]:
    """选出到期该问平台的 listing,并锁住它们。

    `FOR UPDATE SKIP LOCKED` 的理由同 `publish_service.claim_due`:多个 worker
    各自拿到互不相交的一批,而不是排队等同一批。

    `next_poll_at IS NULL` **显式放行**,而且排序上排在最前。这条是照抄
    `claim_due` 踩过的坑:SQL 里 `NULL <= now` 求值是 NULL 不是真,只写
    `<= moment` 的话,一行没有排期的 listing 会**永远**轮不到,而它在库里
    看起来完全正常 —— 状态是 SUBMITTED,没有错误,只是从来不动。
    迁移 0024 给存量行填了 `created_at`,所以这条路径今天走不到;
    但"今天没有人写 NULL"不是一个能靠代码保证的前提,而这一列可空。
    """
    moment = now or _now()
    stmt = (
        select(ChannelListing)
        .where(
            ChannelListing.status.in_(sorted(policy.POLLABLE_STATUSES)),
            # 没有 external_spu_id 就没有可问的对象。判据与
            # `poll_policy.should_poll` 一致,这里再写一次是因为过滤必须
            # 发生在 SQL 里 —— 取回来再在 Python 里筛,limit 就废了:
            # 一批全是没有 ID 的行会把额度占满,而真正该问的一行也没取到
            ChannelListing.external_spu_id.is_not(None),
            ChannelListing.external_spu_id != "",
            (ChannelListing.next_poll_at.is_(None))
            | (ChannelListing.next_poll_at <= moment),
        )
        .order_by(ChannelListing.next_poll_at.asc().nulls_first())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(stmt))


def _reserve(session: Session, listing: ChannelListing, *, now: datetime) -> _Probe | None:
    """把下次可问的时间推到未来,并把这次查询需要的一切取出来。

    **推进发生在问之前**,这是本模块的租约(见模块顶部)。所以即使这次问出来
    的结果是"状态没变",退避也已经生效了 —— 不需要在保存结果那一步再算一次。
    """
    external = (listing.external_spu_id or "").strip()
    if not external:
        # `claim_pollable` 已经在 SQL 里滤过,走到这里说明并发下有人刚清空了
        # 这一列。不抛错:一次轮询扫描不该因为一行数据异常而整批中断
        return None

    count = (listing.poll_attempt_count or 0) + 1
    listing.poll_attempt_count = count
    listing.next_poll_at = now + timedelta(seconds=policy.poll_backoff_seconds(count))
    return _Probe(
        listing_id=listing.id,
        channel=listing.channel,
        external_spu_id=external,
        current_status=listing.status,
        poll_count=count,
    )


# ================================================================ 扫描


def poll_due(
    session_factory: Callable[[], Session],
    *,
    limit: int = DEFAULT_POLL_LIMIT,
    now: datetime | None = None,
    options: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """扫一遍到期的 listing,逐个问平台。返回本次统计。

    三段事务,中间**故意断开**(7.8 节):

        领取 + 推进 next_poll_at   一个事务,commit   ← checkpoint
        问平台                      没有事务
        保存结果                    每一件一个事务,commit

    每件一个事务而不是整批一个,理由同 `publish_service.run_due`:整批一个的话
    第 30 件出错会让前 29 件问到的结果一起悬在未提交状态,而那 29 次调用已经
    真的发生了 —— 平台的配额花了,库里什么都没有。
    """
    moment = now or _now()
    stats = {"claimed": 0, "changed": 0, "unchanged": 0, "rejected": 0, "unanswered": 0}

    session = session_factory()
    try:
        rows = claim_pollable(session, limit=limit, now=moment)
        probes = [p for p in (_reserve(session, r, now=moment) for r in rows) if p is not None]
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    stats["claimed"] = len(probes)
    for probe in probes:
        outcome = _ask(probe, options=options or {})
        # **不传 moment**(同 `publish_service.run_due`)。一批 50 件、每件一次
        # 真实外部调用,跑完可能是几分钟后。拿批次开始的时刻去写
        # `last_polled_at`,那一列就系统性地偏早 —— 而它的全部用途就是回答
        # 「我们对平台的了解是什么时候的」。退避也会被白白压缩掉那几分钟。
        bucket = _save(session_factory, probe, outcome, now=now)
        stats[bucket] = stats.get(bucket, 0) + 1
    return stats


def _ask(probe: _Probe, *, options: Mapping[str, Any]) -> policy.PollOutcome:
    """真的去问平台。**这里没有打开的事务。**

    任何异常都收敛成"这轮没问到"而不是往上抛:抛出去的话整批扫描会中断,
    而后面那些行要等到下一拍才有人问。更重要的是,轮询失败**什么也没发生** ——
    把它变成一个异常,会让一次平台抖动在日志里长得像一次数据故障。
    """
    try:
        transport = registry.transport_for(probe.channel)
        resp = transport.poll(probe.external_spu_id, options)
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        # 只记异常类型,不记原文:客户端库的异常原文常常整条带着 URL 与凭证
        logger.warning(
            "the poll request itself failed",
            extra={
                "extra_fields": {"event": "publish.poll_transport_failed",
                    "listing_id": str(probe.listing_id),
                    "channel": probe.channel,
                    "error": type(exc).__name__,
                }
            },
        )
        return policy.transport_failure()

    return policy.classify_poll(
        status_code=resp.status_code,
        body=resp.body,
        current_status=probe.current_status,
    )


def _save(
    session_factory: Callable[[], Session],
    probe: _Probe,
    outcome: policy.PollOutcome,
    *,
    now: datetime | None = None,
) -> str:
    """在**新事务**里保存一次轮询的结果。返回统计分桶名。

    `now=None` 时取当下:时刻要以**这一件问完的时刻**为准(见 `poll_due`)。
    """
    moment = now or _now()
    session = session_factory()
    try:
        listing = session.get(ChannelListing, probe.listing_id)
        if listing is None:
            session.rollback()
            return "unanswered"
        bucket = apply_poll_outcome(session, listing=listing, outcome=outcome, now=moment)
        session.commit()
        return bucket
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ================================================================ 落库


def apply_poll_outcome(
    session: Session,
    *,
    listing: ChannelListing,
    outcome: policy.PollOutcome,
    now: datetime | None = None,
    actor: str = "system:poller",
) -> str:
    """把一次轮询的结论落到 listing 上(必要时回流驳回)。返回统计分桶名。

    ## 没问到的时候,这个函数几乎什么都不做

    `listing_status is None` 是"别动"(见 `PollOutcome` 的文档)。此时连
    `last_polled_at` 都不更新 —— 那一列的含义是"我们对平台的了解是什么时候
    的",而一次失败的轮询没有更新任何了解。写上去的话,界面上会显示
    "刚刚同步过",而实际上已经连不上平台好几个小时了,那是最坏的一种错误:
    它让人不去查。
    """
    moment = now or _now()

    if outcome.platform_status:
        # 平台原话,照存不翻译。本地状态是我们的解读,这一列是平台说的
        listing.last_platform_status = outcome.platform_status

    if outcome.needs_human:
        logger.warning(
            "the poll came back with something a human should look at",
            extra={
                "extra_fields": {"event": "publish.poll_needs_attention",
                    "listing_id": str(listing.id),
                    "external_spu_id": listing.external_spu_id,
                    "local_status": listing.status,
                    "platform_status": outcome.platform_status,
                    "error_code": outcome.error_code,
                }
            },
        )

    if outcome.listing_status is None:
        session.flush()
        return "unanswered" if not outcome.answered else "unchanged"

    if _delivery_pending(session, listing) and (
        outcome.listing_status != PublishStatus.PLATFORM_REJECTED.value
    ):
        # **我们自己发起的操作还没落地,轮询没有资格给它下结论。**
        #
        # `publish_policy._status_for` 早就写下了这件事的一半:
        #
        #     DELIST 不看平台的 platform_status:下架成功时平台通常仍然
        #     返回那条商品记录,带着 `LISTED` 之类的字样 —— 照着翻译会把
        #     一次成功的下架记成「在架」。
        #
        # 投递那条路能防住,是因为它知道自己刚发的是什么操作。轮询不知道:
        # `classify_poll` 只拿到状态码和 body,平台回 `LISTED` 它就映射成
        # `LISTED`。而 `DELISTING` / `UPDATING` / `REPUBLISHING` 全都在
        # `POLLABLE_STATUSES` 里,所以下面这串是常态而不是边角:
        #
        #     运营点下架 -> DELISTING,outbox 排队
        #     轮询节拍先到(它和投递节拍互相独立,下架请求可能还没发出去)
        #     平台说 LISTED(它当然还在架,请求还没到)
        #     -> 状态翻成 LISTED,页面从「下架中」变回「在架」
        #     -> LISTED 不在 POLLABLE_STATUSES 里,这一行掉出轮询集合
        #
        # 判据用「有没有未落地的 outbox 行」而不是「状态在不在某个集合里」:
        # 后者会在投递以 DEAD 收场、且没写回 listing 状态时把这一行永久钉死,
        # 而前者是自限的 —— outbox 一结算,轮询立刻恢复全部权威。
        #
        # `PLATFORM_REJECTED` 例外放行:平台明确拒了,这不是「你的请求还没
        # 处理完」能解释的,而且 `_reflow_rejection` 必须跑起来。
        logger.info(
            "a delivery is in flight; deferring this poll",
            extra={
                "extra_fields": {"event": "publish.poll_deferred_to_delivery",
                    "listing_id": str(listing.id),
                    "local_status": listing.status,
                    "polled_status": outcome.listing_status,
                    "platform_status": outcome.platform_status,
                }
            },
        )
        # A45-#16:**这一支也要推进 `last_polled_at`。**
        #
        # 上面已经写了 `last_platform_status`(平台原话),而这里原来直接返回,
        # 于是两列不同步:界面可以出现「平台说 LISTED」配一个几小时前的
        # 「最近同步于」—— 而本函数开头那段注释批评的正是这种误读形态
        # (「刚刚同步过」是最坏的错误,因为它让人不去查)。
        #
        # 注意方向:开头那段说的是**没问到**的时候不许写,而这里是**问到了、
        # 只是不采信它的结论**。"我们对平台的了解是什么时候的"这个含义在
        # 这一支上成立 —— 我们确实刚问过,也确实拿到了平台原话。
        listing.last_polled_at = moment
        session.flush()
        return "unchanged"

    listing.last_polled_at = moment

    if outcome.listing_status == listing.status:
        # 状态没变。退避继续增长(`poll_attempt_count` 在领取时已经加过),
        # 于是一个审核了两小时的商品会被越问越稀 —— 那正是 4.1 节 D 要的退避
        session.flush()
        return "unchanged"

    previous = listing.status
    listing.status = outcome.listing_status
    # 平台动了,退避归零:接下来很可能还会再动(REVIEWING -> LISTED 常常
    # 紧接着发生)。不归零的话,一个刚从审核转成在架的商品要等一小时才被
    # 再问一次,而那一小时里我们对它的了解是错的
    listing.poll_attempt_count = 0
    listing.next_poll_at = moment + timedelta(
        seconds=policy.poll_backoff_seconds(0)
    )
    session.flush()

    logger.info(
        "the platform side status changed",
        extra={
            "extra_fields": {"event": "publish.poll_status_changed",
                "listing_id": str(listing.id),
                "from": previous,
                "to": listing.status,
                "platform_status": outcome.platform_status,
            }
        },
    )

    if outcome.rejection is not None:
        _reflow_rejection(session, listing, outcome.rejection, actor=actor)
        return "rejected"
    return "changed"


def _delivery_pending(session: Session, listing: ChannelListing) -> bool:
    """这一行还有没有没结算完的投递。

    `publish_outbox` 不直接挂 listing,要经 `publish_attempts` 转一跳
    (`ix_publish_attempts_listing_started` 覆盖了这一列)。未结算 =
    `PENDING`(排队中或退避中)或 `LEASED`(某个 worker 正拿着它在发)。
    `DONE` / `DEAD` 都是结算过的:前者拿到了答案,后者放弃了自动重试 ——
    两种情况下轮询都该重新拿回状态的话语权。

    一次轮询多一条 join 查询。这个代价可以接受:同一轮里每一行本来就要
    打一次真实的外部调用,一条走索引的存在性查询在它旁边看不见。
    """
    return session.scalar(
        select(PublishOutbox.id)
        .join(PublishAttempt, PublishAttempt.id == PublishOutbox.publish_attempt_id)
        .where(
            PublishAttempt.channel_listing_id == listing.id,
            PublishOutbox.status.in_(
                (
                    PublishOutboxStatus.PENDING.value,
                    PublishOutboxStatus.LEASED.value,
                )
            ),
        )
        .limit(1)
    ) is not None


def _reflow_rejection(
    session: Session,
    listing: ChannelListing,
    signal: policy.RejectionSignal,
    *,
    actor: str,
) -> None:
    """把一次平台驳回送进既有的修复流程(4.1 节 D「驳回进入修复流程」)。

    ## 回流失败不回滚状态

    找不到草稿或商品行时,这里只记警告,不抛错。listing 已经被改成
    PLATFORM_REJECTED,而那个状态是**对的** —— 平台确实拒了。因为台账写不进去
    就把状态一起回滚,结果是商品继续显示"审核中",而它其实已经被拒了;
    两个坏结果里,"状态对但台账缺一条"是可以靠日志补的那一个。
    """
    draft = (
        session.get(ListingDraft, listing.draft_id) if listing.draft_id else None
    )
    product = session.get(Product, listing.product_id)
    if draft is None or product is None:
        logger.warning(
            "skipped the rejection reflow",
            extra={
                "extra_fields": {"event": "publish.rejection_reflow_skipped",
                    "listing_id": str(listing.id),
                    "draft_id": str(listing.draft_id) if listing.draft_id else None,
                    "reason": "草稿或商品行缺失,驳回记不进台账",
                    "category": signal.category.value,
                }
            },
        )
        return

    platform_service.record_api_rejection(
        session,
        draft,
        product,
        category=signal.category,
        platform_code=signal.code,
        platform_note=signal.note,
        actor=actor,
    )


# ================================================================ 显式轮询


def poll_one(
    session_factory: Callable[[], Session],
    listing_id: UUID,
    *,
    options: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """问一行的状态,**不看它到期没有、也不看它的本地状态**。

    调用方是 4.1 节 H 的清单核对(`cleanup_service.verify`)和将来发布页上的
    「刷新状态」按钮。它们要问的是"平台上现在到底还有没有这个商品",而那个
    问题对已经 LISTED、甚至已经 DELISTED 的行同样成立 —— 那两个状态都不在
    `POLLABLE_STATUSES` 里,走 `poll_due` 一辈子也问不到它们。

    做成单独一个函数而不是给 `poll_due` 加一个 `force=True`,理由同
    `publish_service.reconcile_unknown`:两种意图在代码里应该长得不一样。
    定时扫描要节制(退避、批量上限、跳过不该问的),人点一下按钮要立刻回答。
    """
    session = session_factory()
    try:
        listing = session.get(ChannelListing, listing_id)
        if listing is None or not (listing.external_spu_id or "").strip():
            return "unanswered"
        probe = _Probe(
            listing_id=listing.id,
            channel=listing.channel,
            external_spu_id=str(listing.external_spu_id).strip(),
            current_status=listing.status,
            poll_count=listing.poll_attempt_count or 0,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    outcome = _ask(probe, options=options or {})
    return _save(session_factory, probe, outcome, now=now)


def pollable_statuses() -> tuple[str, ...]:
    """给接口层与看板用。排序是为了输出稳定,不然快照测试会随机红。"""
    return tuple(sorted(policy.POLLABLE_STATUSES))


__all__ = [
    "DEFAULT_POLL_LIMIT",
    "apply_poll_outcome",
    "claim_pollable",
    "poll_due",
    "poll_one",
    "pollable_statuses",
]
