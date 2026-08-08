"""发布 Outbox 租约的**真库并发行为**(A45-batch17-2 / P2-1)。

## 为什么这一组必须存在

`docs/STATUS.md` 的「已知限制」里,批次那条链路有一行写着重叠执行仍可能发生、
但**状态互踩已经关掉**(A43 / BLOCK-02,令牌)。发布这条链路当时没有同样的
一行 —— 不是因为它没有同样的缺口,是因为没有人把它记上去。缺口原样在:

    1. worker A 领走,租约 LEASE_SECONDS = 180 秒,发出真实外部调用
    2. 调用挂住超过 180 秒
    3. `claim_due()` 把「LEASED 且租约过期」当成"A 崩了"重新发出
    4. worker B 领走同一行、发同一把幂等键的同一份报文、成功,
       落 DONE / SUCCEEDED / LISTED + external_spu_id
    5. A 的调用这时才超时返回,**无条件**覆写:
       DONE → DEAD、SUCCEEDED → UNKNOWN、LISTED → SUBMIT_RESULT_UNKNOWN

第 5 步覆盖掉的是较新的事实。商品在平台上是真的上架了(幂等键保证不会
多出一件),但界面会说"结果未知、需人工",而人工唯一能做的 RECONCILE
是一次本来不必发生的付费调用。

## 为什么不能用 `session` 夹具

同 `test_batch_lease_concurrency_db.py`:那个夹具把整个用例包在一个最后
回滚的事务里,而"两个 worker 互不相交"必须由**两条真连接**来证 ——
同一个事务里跑两遍 `claim_due` 看得见彼此的未提交改动,测出来的绿是假的。
这一组自己开两条连接、真提交,teardown 里按主键删干净。

## 这一组守的是什么

1. **`SKIP LOCKED` 真的生效**:两个 worker 领到不相交的行。
2. **活租约领不走**:这是"不重复投递"的第一道防线。
3. **过期租约可以接管**,并且接管会**换掉令牌**。
4. **迟到的那次落库影响 0 行,结果只进审计** —— 本批修的那一条。
5. **正常路径照样写得进去**:这一条不是凑数。判据要是写成
   `lease_until == 领取值`(续租之后必然不等),挡住的就不是迟到的调用,
   而是**每一次**调用 —— 那种坏法比缺口本身严重,而且四条只验缺口的
   用例全都会是绿的。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.channels import generic
from app.core.clock import utc_now
from app.core.enums import (
    AuditAction,
    DraftStatus,
    PublishAttemptStatus,
    PublishOutboxStatus,
    PublishStatus,
)
from app.models.audit_log import AuditLog
from app.models.listing_copy import ListingDraft
from app.models.listing_image import ListingImageSet
from app.models.product import Product
from app.models.publishing import ChannelListing, PublishAttempt, PublishOutbox
from app.services import publish_service as svc
from app.workflows import publish_policy as policy
from tests.conftest import requires_db

pytestmark = requires_db


class _NoCloseSession:
    """让服务层的 `session.close()` 不要拆掉用例手里的连接。

    与 `test_publish_flow_db.py` 里那个是同一个东西、同一条理由:服务层
    每段事务自己开自己关是生产该有的行为,而用例要在那之后继续断言。
    这里额外的一点是**两条连接必须各自保持独立**——所以 A 的工厂只发 A、
    B 的只发 B,不能共用一个。
    """

    def __init__(self, inner: Session):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        return None


def _factory(session: Session):
    return lambda: _NoCloseSession(session)


@pytest.fixture
def two_workers(engine):
    """两条**独立连接**,各自真提交。

    命名叫 worker 而不是 session:这一组要模拟的就是两个进程,
    而"它们是不是同一个事务"正是被测的东西。
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    a: Session = factory()
    b: Session = factory()
    listings: list[uuid.UUID] = []
    outboxes: list[uuid.UUID] = []
    products: list[uuid.UUID] = []
    drafts: list[uuid.UUID] = []
    image_sets: list[uuid.UUID] = []

    def make_pending(*, spu_in_payload: str | None = None) -> PublishOutbox:
        """造一行真的排到队里的 Outbox。**走 `enqueue()`,不手工拼三张表。**

        手工拼的话,幂等键、指纹、attempt 与 outbox 的 1:1 关系都得在用例里
        再实现一遍 —— 而那份实现和生产的那份哪天分叉,这一组会继续绿。
        """
        suffix = uuid.uuid4().hex[:8]
        product = Product(
            spu=f"SW-LEASE-{suffix}",
            sku=f"SKU-LEASE-{suffix}",
            name="租约测试泳衣",
            garment_type="BIKINI_SET",
        )
        a.add(product)
        a.flush()
        products.append(product.id)

        image_set = ListingImageSet(spu=product.spu, version=1)
        a.add(image_set)
        a.flush()
        image_sets.append(image_set.id)

        draft = ListingDraft(
            spu=product.spu,
            channel=generic.CHANNEL,
            site=generic.SITE,
            category_id=generic.CATEGORY_ID,
            status=DraftStatus.VALIDATED.value,
            image_set_id=image_set.id,
            source_fingerprint=f"fp-{suffix}",
            spec_version="1",
            mapped_payload={
                "header": {"spu": spu_in_payload or product.spu, "title": "Bikini"},
                "rows": [{"sku": product.sku}],
            },
        )
        a.add(draft)
        a.flush()
        drafts.append(draft.id)

        result = svc.enqueue(
            a, product=product, draft=draft, shop_id=f"shop-{suffix}", actor="tester"
        )
        a.commit()
        listings.append(result.listing.id)
        row = a.scalars(
            select(PublishOutbox).where(
                PublishOutbox.publish_attempt_id == result.attempt.id
            )
        ).one()
        outboxes.append(row.id)
        return row

    a.make_pending = make_pending  # type: ignore[attr-defined]
    try:
        yield a, b
    finally:
        for db in (a, b):
            db.rollback()
        if listings:
            attempts = list(
                a.scalars(
                    select(PublishAttempt.id).where(
                        PublishAttempt.channel_listing_id.in_(listings)
                    )
                )
            )
            if attempts:
                a.execute(
                    delete(PublishOutbox).where(
                        PublishOutbox.publish_attempt_id.in_(attempts)
                    )
                )
                a.execute(delete(PublishAttempt).where(PublishAttempt.id.in_(attempts)))
                a.execute(
                    delete(AuditLog).where(
                        AuditLog.entity_type == "PublishOutbox",
                        AuditLog.entity_id.in_(outboxes),
                    )
                )
            a.execute(delete(ChannelListing).where(ChannelListing.id.in_(listings)))
        if drafts:
            a.execute(delete(ListingDraft).where(ListingDraft.id.in_(drafts)))
        if image_sets:
            a.execute(delete(ListingImageSet).where(ListingImageSet.id.in_(image_sets)))
        if products:
            a.execute(delete(Product).where(Product.id.in_(products)))
        a.commit()
        a.close()
        b.close()


def _reload(db: Session, row_id: uuid.UUID) -> PublishOutbox:
    db.expire_all()
    return db.scalars(select(PublishOutbox).where(PublishOutbox.id == row_id)).one()


def _succeeded(external_spu_id: str) -> policy.Outcome:
    """一次成功投递的结论。**照 `Outcome` 今天的契约构造。**

    这几条用例原来传的是 `succeeded=True` —— 而 `succeeded` 早就不是构造
    参数了,它是一个由 `attempt_status` 推出来的只读属性(见
    `publish_policy.Outcome`)。传它抛 `TypeError`,而抛点在
    "迟到 worker 覆盖不了赢家""令牌吊销后结果被拒收"这些目标断言**之前**:
    于是这两条性质本轮一次都没有被真正验过,报告上却是两条红。

    收进一个构造函数而不是在四处各修一遍:契约再变时只有这里要改,
    而"四处里改漏一处"正是它上次没跟上契约的原因。`retriable=False`
    是必填项也是对的语义 —— 一次已经成功的投递不存在原样重发。
    """
    return policy.Outcome(
        attempt_status=PublishAttemptStatus.SUCCEEDED.value,
        listing_status=PublishStatus.LISTED.value,
        retriable=False,
        platform_status="LISTED",
        external_spu_id=external_spu_id,
    )


def _stale_moment() -> datetime:
    """一个**比租约还老**的时刻。租约从它起算,到现在必然已经过期。"""
    return utc_now() - timedelta(seconds=policy.LEASE_SECONDS + 60)


def _claim_at(db: Session, row: PublishOutbox, moment: datetime) -> PublishOutbox:
    """把 `row` 的待领取时间对齐到 `moment`,然后在那个时刻领取它。

    ## 为什么必须有这一步

    `claim_due(now=moment)` 的取数条件是 `next_attempt_at <= moment`,而
    `enqueue()` 写进去的是**当下**。于是拿一个过去的 `moment` 去领一行
    刚建好的 outbox,结果恒为空列表 —— 后面那句 `claimed[0]` 抛 IndexError。

    失败发生在租约接管、迟到 worker 条件落库这些**目标断言之前**,所以这
    三条用例过去既证明不了 fencing 成立,也证明不了它不成立:红是红的,
    红的原因却是用例自己的时钟没对齐。一条永远红在 setup 上的并发用例,
    和没有这条用例的区别只在报告的行数上。

    往回调 `next_attempt_at` 而不是往前调 `utc_now()`:后者要动全局时钟,
    而这一组的另一半(B 在**当下**接管)恰恰需要真实的当下。
    """
    db.execute(
        text("UPDATE publish_outbox SET next_attempt_at = :t WHERE id = :i"),
        {"t": moment, "i": row.id},
    )
    db.commit()
    claimed = svc.claim_due(db, limit=10, now=moment)
    assert claimed, (
        "在模拟时刻领不到这一行 —— 待领取时间没有和模拟时钟对齐。"
        "这不是被测行为出了问题,是这条用例还没走到它要验的地方"
    )
    assert row.id in {r.id for r in claimed}
    return next(r for r in claimed if r.id == row.id)


# ---------------------------------------------------------------- 互不相交


def test_skip_locked_gives_each_worker_a_disjoint_set(two_workers):
    """`FOR UPDATE SKIP LOCKED` 写错时的表现不是报错,是两个 worker 捞到同一行、
    同时置 LEASED、同时把**同一份报文**发给平台。幂等键会挡住重复创建,
    代价是一次白花的调用和一条谁也解释不清的 409 流水。"""
    a, b = two_workers
    first = a.make_pending()  # type: ignore[attr-defined]
    second = a.make_pending()  # type: ignore[attr-defined]

    claimed_a = svc.claim_due(a, limit=1)
    assert len(claimed_a) == 1

    # 给 B 装一个短锁超时。**没有它,`skip_locked` 被删掉时这条用例会挂住**,
    # 而挂住的 CI 只报"作业超时",不报"锁语义错了"
    b.execute(text("SET LOCAL lock_timeout = '3s'"))
    try:
        claimed_b = svc.claim_due(b, limit=1)
    except OperationalError as exc:  # pragma: no cover - 只在变异时走到
        pytest.fail(
            "B 在等 A 的行锁,说明 `claim_due` 丢了 SKIP LOCKED:"
            f"两个 worker 会排队而不是各领各的。原始错误:{type(exc).__name__}"
        )
    assert len(claimed_b) == 1, "SKIP LOCKED 没生效:B 要么被阻塞、要么什么都没拿到"
    assert {r.id for r in claimed_a}.isdisjoint({r.id for r in claimed_b}), (
        "两个 worker 领到了同一行 —— 同一次提交会被投递两遍"
    )
    assert {first.id, second.id} == {claimed_a[0].id, claimed_b[0].id}
    a.rollback()
    b.rollback()


def test_a_live_lease_cannot_be_taken_by_a_second_worker(two_workers):
    """租约没过期时别人领不到。**这是"不重复投递"的第一道防线。**"""
    a, b = two_workers
    row = a.make_pending()  # type: ignore[attr-defined]

    claimed = svc.claim_due(a, limit=10)
    call = svc._lease(a, claimed[0], now=utc_now())
    assert call is not None
    a.commit()

    assert svc.claim_due(b, limit=10) == [], (
        "活着的租约被第二个 worker 领走了 —— 同一份报文会被发两遍"
    )
    assert _reload(b, row.id).lease_token == call.lease_token
    b.rollback()


def test_an_expired_lease_is_taken_over_and_the_token_changes(two_workers):
    """租约过期后可以接管,**并且接管会换掉令牌**。

    换令牌是本批的关键:接管本身早就成立(那是 `lease_until` 的用途),
    但接管之后原持有者仍然写得进来 —— 那才是 P2-1。
    """
    a, b = two_workers
    row = a.make_pending()  # type: ignore[attr-defined]

    stale_moment = _stale_moment()
    first = svc._lease(a, _claim_at(a, row, stale_moment), now=stale_moment)
    assert first is not None
    a.commit()

    retaken = svc.claim_due(b, limit=10)
    assert len(retaken) == 1, "租约过期了却没人接管 —— 这一行会永久停在「提交中」"
    second = svc._lease(b, retaken[0], now=utc_now())
    assert second is not None
    b.commit()

    assert second.lease_token != first.lease_token, (
        "接管没有换令牌:原持有者的迟到结论仍然有资格覆盖新的结论"
    )
    assert _reload(a, row.id).lease_token == second.lease_token


# ---------------------------------------------------------------- 迟到的结论


def test_a_late_worker_cannot_overwrite_a_newer_conclusion(two_workers):
    """**这一组存在的头号理由(P2-1)。**

    B 落成功之后,A 那次超时返回的调用不许把三张表改回去。
    """
    a, b = two_workers
    row = a.make_pending()  # type: ignore[attr-defined]

    stale_moment = _stale_moment()
    late_call = svc._lease(a, _claim_at(a, row, stale_moment), now=stale_moment)
    assert late_call is not None
    a.commit()

    # B 接管
    retaken = svc.claim_due(b, limit=10)
    winner = svc._lease(b, retaken[0], now=utc_now())
    assert winner is not None
    b.commit()

    # **先确认这条用例真的走到了要验的地方 —— 而且要在 B 落终态之前确认。**
    #
    # `bucket == "stale"` 只说明 `_save` 认为 A 没有执行权;它说不出
    # "A 手里的令牌确实已经被换掉了"。两者分开断言的理由是本轮这几条用例
    # 的失败方式:它们在领取那一步就抛了,而报告里看到的是一条红的
    # fencing 用例 —— 一条红的用例和一条验过的用例长得很像,但含义相反。
    #
    # 位置在这里、不在 `_save` 之后:换令牌是**领取**(`_lease`)干的事,
    # 而落终态**按设计吊销令牌**(`publish_service.apply_outcome` 里那句
    # `row.lease_token = None`,与批次那边 `_outcome_values` 同源)。
    # 把这两条搬到 B 成功之后去查,查到的必然是 NULL —— 上一版就红在这里,
    # 红的位置在 A 的 fencing 断言**之前**,于是这条用例每一次都停在
    # 前提检查上,而报告上它长得就像"fencing 没守住"。
    assert late_call.lease_token != winner.lease_token, (
        "两次领取拿到了同一把令牌 —— 那么下面对 A 的断言即便通过,"
        "通过的原因也不是 fencing"
    )
    assert _reload(b, row.id).lease_token == winner.lease_token, (
        "库里的令牌不是 B 那一把 —— 接管没有真的落库,这条用例失去了前提"
    )

    # B 跑完、成功
    assert (
        svc._save(
            _factory(b),
            winner,
            _succeeded("EXT-WINNER-1"),
        )
        == "succeeded"
    )

    # 落终态吊销令牌。**这一条不是顺带断言,它就是下面那条 stale 的机制**:
    # `_still_holds` 的判据只有令牌,而这一列变成 NULL 之后,任何还拿着
    # 旧令牌的执行体都判不成"还握着这一行"(那个函数对 `call.lease_token`
    # 为 None 的情形也显式返回 False,方向取的是安全一侧)。
    # 少了这一条,A 被挡住的原因在报告里只剩一个 bucket 名字。
    assert _reload(b, row.id).lease_token is None, (
        "落终态没有吊销令牌 —— 还拿着旧令牌的执行体仍然写得进这一行"
    )

    # A 的调用这时才超时返回
    bucket = svc._save(
        _factory(a),
        late_call,
        policy.classify_transport_failure(operation=late_call.operation, error="timeout"),
    )
    assert bucket == "stale", f"迟到的结论被当成正常结果处理了(bucket={bucket})"

    fresh = _reload(b, row.id)
    assert fresh.status == PublishOutboxStatus.DONE.value, (
        "已经 DONE 的投递被一次迟到的超时改回去了 —— 商品在平台上好好的,"
        "界面上却说结果未知需人工"
    )
    attempt = b.scalars(
        select(PublishAttempt).where(PublishAttempt.id == late_call.attempt_id)
    ).one()
    assert attempt.status == PublishAttemptStatus.SUCCEEDED.value, (
        "尝试记录被改写了 —— 「尝试写完不改」是枚举那一节的硬约束"
    )
    listing = b.scalars(
        select(ChannelListing).where(ChannelListing.id == late_call.listing_id)
    ).one()
    assert listing.status == PublishStatus.LISTED.value
    assert listing.external_spu_id == "EXT-WINNER-1"


def test_the_late_call_still_leaves_a_trace(two_workers):
    """失去执行权 ≠ 这次调用没发生过。**审计里必须留得下。**

    这一条与上一条是一对:上一条要的是"别写进业务表",这一条要的是
    "别当没发生过"。只做前者的话,一次真实的对外调用在系统里彻底消失。
    """
    a, b = two_workers
    row = a.make_pending()  # type: ignore[attr-defined]

    stale_moment = _stale_moment()
    late_call = svc._lease(a, _claim_at(a, row, stale_moment), now=stale_moment)
    assert late_call is not None
    a.commit()

    winner = svc._lease(b, svc.claim_due(b, limit=10)[0], now=utc_now())
    assert winner is not None
    b.commit()
    svc._save(
        _factory(b),
        winner,
        _succeeded("EXT-WINNER-2"),
    )
    svc._save(
        _factory(a),
        late_call,
        policy.classify_transport_failure(operation=late_call.operation, error="timeout"),
    )

    entries = list(
        b.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "PublishOutbox",
                AuditLog.entity_id == row.id,
                AuditLog.action == AuditAction.UPDATE.value,
            )
        )
    )
    stale = [e for e in entries if (e.payload or {}).get("action") == "stale_outcome"]
    assert len(stale) == 1, (
        "被丢弃的那次调用没有在审计里留下任何东西 —— "
        "「平台那边到底发生了什么」从此无处可查"
    )
    assert stale[0].payload["publish_attempt_id"] == str(late_call.attempt_id)


# ---------------------------------------------------------------- 正常路径没被误伤


def test_the_normal_path_still_writes_through(two_workers):
    """**这一条不是凑数。**

    归属判据要是退回 `lease_until == 领取值`,上面四条会全绿(缺口确实堵住了),
    而**每一次**正常投递的结论也会一起落不了库 —— `_renew_lease()` 在发请求
    之前就把库里那个值改掉了。那种坏法比 P2-1 严重,却只有这一条看得见。
    """
    a, _ = two_workers
    row = a.make_pending()  # type: ignore[attr-defined]

    call = svc._lease(a, svc.claim_due(a, limit=10)[0], now=utc_now())
    assert call is not None
    a.commit()

    assert svc._renew_lease(_factory(a), call) is True, "续租续不上 —— 正常投递会被当成失去租约跳过"
    assert (
        svc._save(
            _factory(a),
            call,
            _succeeded("EXT-NORMAL-1"),
        )
        == "succeeded"
    ), "续租之后正常的结论落不进去了 —— 归属判据挡错了对象"

    fresh = _reload(a, row.id)
    assert fresh.status == PublishOutboxStatus.DONE.value
    assert fresh.lease_token is None, "落终态没有吊销令牌"


def test_a_row_whose_token_was_revoked_by_hand_refuses_the_in_flight_result(two_workers):
    """人工重新投递会吊销令牌,还在飞的那次调用回来时写不进去。

    这条守的是运营的决定优先:`redeliver_dead` 是一个人按下去的动作,
    它不该被一个几分钟前发出去的调用悄悄盖掉。
    """
    a, b = two_workers
    row = a.make_pending()  # type: ignore[attr-defined]

    call = svc._lease(a, svc.claim_due(a, limit=10)[0], now=utc_now())
    assert call is not None
    a.commit()

    # 另一条连接上把这一行置死,然后人工重投(重投只接受 DEAD)
    target = _reload(b, row.id)
    target.status = PublishOutboxStatus.DEAD.value
    b.commit()
    svc.redeliver_dead(b, call.attempt_id, actor="ops", note="平台恢复了")
    b.commit()

    bucket = svc._save(
        _factory(a),
        call,
        _succeeded("EXT-RACE-1"),
    )
    assert bucket == "stale", "令牌已被吊销,这次结果仍然写了进去"
    assert _reload(b, row.id).status == PublishOutboxStatus.PENDING.value, (
        "运营刚排进队的那一行被一次迟到的成功改成了 DONE"
    )
