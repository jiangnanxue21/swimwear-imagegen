"""付费调用期间,**别的会话看得见新的租约截止时间**吗(A45-batch18 / P2-1)。

## 这一组存在的理由

A43 加了 `renew_lease()`,调用点在付费调用之前 —— 那是整条链路上唯一一个
"已经知道自己出局、但还没花钱"的时刻。既有门禁
(`tests/pure/test_batch_lease.py::test_execution_renews_before_it_spends_money`)
用源码位置证明了**顺序**对:`renew_lease(` 出现在 `_execute(` 之前。

**顺序对不等于有效。** 那条 UPDATE 原来留在外层事务里,而外层事务要等
`_execute()` 跑完、结果保存完才提交(`batch_service.run_batch` 的 `persist`
回调)。也就是说整个付费调用期间,`reap_expired_leases` 在它自己的会话里
读到的仍然是**领取时**那个 `lease_until`。续租续了个寂寞:

    它挡得住   "我在开跑前就已经被回收了" —— 条件更新影响 0 行,当场停下
    它挡不住   "我正在跑,而回收器按旧的截止时间判我过期" —— 而这一种
               才是那条不变量真正要防的:回收一件仍在跑的条目 =
               同一件事第二次调用付费模型

源码扫描永远看不见这个区别,因为两种写法的**源码顺序完全一样**。只有
两条真连接能证。这与 `test_batch_lease_concurrency_db.py` 的开场理由同型:
`FOR UPDATE SKIP LOCKED` 也是"写错了照样跑得通、只在撞上那一次才露馅"。

## 为什么不能用 `session` 夹具

那个夹具把整个用例包在一个最后回滚的事务里。"另一个会话看得见"这件事
必须由两条真连接来证:同一个事务里读,本来就看得见自己未提交的改动,
测出来的绿是假的 —— 而假绿正是这一组要消灭的东西。

teardown 按主键删干净,与 `test_batch_lease_concurrency_db.py` 同一套路数。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import utc_now
from app.models.batch_job import BatchJob, BatchJobItem
from app.models.product import Product
from app.workbench import batch as rules
from app.workbench import batch_service as bs
from app.workbench.batch import BatchAction, BatchItemStatus
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def worker_and_reaper(engine):
    """两条独立连接:一条扮演正在跑的 worker,一条扮演回收器。

    命名刻意不叫 `session_a` / `session_b` —— 这一组要证的是这两个角色
    看到的**不是同一份事实**,名字应该说出这件事。
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    worker: Session = factory()
    reaper: Session = factory()
    jobs: list[uuid.UUID] = []
    products: list[uuid.UUID] = []

    def make_item():
        job = BatchJob(
            action=BatchAction.GENERATE_COPY.value, status="QUEUED", created_by="tester"
        )
        worker.add(job)
        worker.flush()
        product = Product(
            spu="SPU-RENEW",
            sku=f"SKU-RENEW-{uuid.uuid4().hex[:8]}",
            name="续租可见性测试泳衣",
            garment_type="BIKINI_SET",
        )
        worker.add(product)
        worker.flush()
        products.append(product.id)
        worker.add(
            BatchJobItem(
                batch_id=job.id,
                product_id=product.id,
                sku=product.sku,
                spu=product.spu,
                scope_key=product.sku,
                status=BatchItemStatus.PENDING.value,
                idempotency_key=f"renew-{uuid.uuid4().hex}",
                attempts=0,
            )
        )
        worker.commit()
        jobs.append(job.id)
        return job

    worker.make_item = make_item  # type: ignore[attr-defined]
    try:
        yield worker, reaper
    finally:
        for db in (worker, reaper):
            db.rollback()
        if jobs:
            worker.execute(delete(BatchJobItem).where(BatchJobItem.batch_id.in_(jobs)))
            worker.execute(delete(BatchJob).where(BatchJob.id.in_(jobs)))
        if products:
            worker.execute(delete(Product).where(Product.id.in_(products)))
        worker.commit()
        worker.close()
        reaper.close()


def _deadline_seen_by(db: Session, item_id: uuid.UUID):
    """在**另一条**连接上读这一行的租约截止时间。

    `expire_all()` 是必需的:ORM 身份映射会把上一次读到的对象原样还给我们,
    而那正好会让这个用例在两种实现下都变绿 —— 也就是完全失效。
    """
    db.rollback()  # 结束上一个隐式事务,否则读到的是这条连接的旧快照
    db.expire_all()
    return db.scalars(
        select(BatchJobItem.lease_until).where(BatchJobItem.id == item_id)
    ).one()


def test_the_renewal_is_visible_to_another_session_before_the_paid_call(
    worker_and_reaper,
):
    """**这一组的头号用例。**

    模拟 `run_batch` 那一段的时序:领取(提交)→ 续租 → 提交 → 付费调用。
    断言在"付费调用还没开始"的那一刻,回收器那条连接已经读得到新的截止时间。

    没有那次提交的话,这里读到的仍是领取时那个值 —— 而回收器正是按它
    判定"这一行过期了,抢走"。
    """
    worker, reaper = worker_and_reaper
    job = worker.make_item()

    claimed = bs.claim_items(worker, job, owner="worker-a", limit=rules.CLAIM_CHUNK)
    assert len(claimed) == 1
    row = claimed[0]
    worker.commit()

    at_claim = _deadline_seen_by(reaper, row.id)
    assert at_claim is not None, "领取时就该盖上租约,否则任何人都能来抢"

    # 时间往前走一点,让续租算出来的 deadline 与领取时那个明确不同
    later = utc_now()
    assert bs.renew_lease(
        worker, row.id, owner="worker-a", token=row.lease_token, now=later
    )
    # **这一句就是被测的东西。** `run_batch` 在 `commit_each=True` 时
    # 会在续租之后、`_execute()` 之前做同样的事
    worker.commit()

    after_renew = _deadline_seen_by(reaper, row.id)
    assert after_renew > at_claim, (
        "另一个会话必须在付费调用开始之前就看到新的截止时间。"
        "读到旧值意味着续租只活在 worker 自己的事务里,"
        "而回收器会按旧值把一件正在跑的条目抢走 —— 那次调用的钱白花"
    )


def test_an_uncommitted_renewal_is_exactly_what_the_reaper_cannot_see(
    worker_and_reaper,
):
    """反向证明:**不提交时回收器确实读不到。**

    这条用例的作用是让上一条的绿有意义。没有它的话,上一条在"续租根本
    不需要提交也可见"的数据库上也会绿,而那种数据库不存在 ——
    但读用例的人无从判断这一点。

    它同时把这次修复的**根因**钉成一条可执行的断言:问题从来不是
    `renew_lease` 写错了,而是它写对了却没有被提交出去。
    """
    worker, reaper = worker_and_reaper
    job = worker.make_item()
    row = bs.claim_items(worker, job, owner="worker-a", limit=rules.CLAIM_CHUNK)[0]
    worker.commit()
    at_claim = _deadline_seen_by(reaper, row.id)

    assert bs.renew_lease(
        worker, row.id, owner="worker-a", token=row.lease_token, now=utc_now()
    )
    # 故意不提交 —— 这就是修复之前 `run_batch` 在整个付费调用期间的状态
    assert _deadline_seen_by(reaper, row.id) == at_claim, (
        "未提交的续租对别的会话不存在。这正是修复前那半小时里的真实情况"
    )
    worker.rollback()


def test_the_reaper_would_have_taken_a_running_item_under_the_old_deadline(
    worker_and_reaper,
):
    """把两件事接起来:**旧截止时间 + 正在跑 = 被抢走。**

    `reclaim_verdict` / `lease_expired` 的判定在纯层验过,`claim_items` 的
    `SKIP LOCKED` 在并发那一组验过。缺的一直是中间这一段:回收器按**哪个**
    截止时间做判定。这条用例证明它按库里那个 —— 于是"续租有没有提交"
    直接决定一件正在跑的条目会不会被回收。
    """
    worker, reaper = worker_and_reaper
    job = worker.make_item()
    row = bs.claim_items(worker, job, owner="worker-a", limit=rules.CLAIM_CHUNK)[0]
    worker.commit()

    # 把截止时间强行拨到过去,模拟"租约到期而 worker 仍在跑"
    stale = utc_now() - rules_timedelta()
    reaper.execute(
        BatchJobItem.__table__.update()
        .where(BatchJobItem.id == row.id)
        .values(lease_until=stale)
    )
    reaper.commit()

    taken = bs.reap_expired_leases(reaper)
    assert taken["scanned"] >= 1, (
        "过期租约必须能被回收 —— 否则 worker 真的死了就没人接手"
    )

    reaper.commit()
    worker.rollback()
    worker.expire_all()
    fresh = worker.scalars(
        select(BatchJobItem).where(BatchJobItem.id == row.id)
    ).one()
    assert fresh.lease_token != row.lease_token or fresh.lease_token is None, (
        "回收 = 吊销令牌。原 worker 跑完之后的条件更新必须影响 0 行,"
        "否则它会覆盖新 owner 的结果"
    )


def rules_timedelta():
    """比一个完整租约还长一点的时间差。

    单独抽成函数是为了跟着 `ITEM_LEASE_SECONDS` 走:A45-batch18 把它从
    1800 抬到了 3600,写死一个 `timedelta(hours=1)` 会在下一次调整时
    悄悄变成"刚好没过期",而那时这条用例会红在一个和它无关的断言上。
    """
    from datetime import timedelta

    return timedelta(seconds=rules.ITEM_LEASE_SECONDS + 60)
