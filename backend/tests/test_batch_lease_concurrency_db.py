"""批次条目租约的**真库并发行为**(任务 17 / 18,A42 补)。

## 为什么这一组必须存在

`docs/STATUS.md` 里有一行写了很久:「**并发下两个 worker 真的互不相交、
崩溃后真的能回收,这两件事至今没有跑过一次。**」

离线门禁只覆盖到两头:`tests/pure/test_batch_lease.py` 用真值断言钉判定
(`lease_expired` / `reclaim_verdict` / 那条不变量),再用 AST 钉接线
(`claim_items` 里有没有 `skip_locked`)。**中间那段没有任何东西覆盖** ——
而 `FOR UPDATE SKIP LOCKED` 恰恰是一个"写错了也照样跑得通、只在两个 worker
撞上的那一次才露馅"的东西,并且那一次没有人在看屏幕。

## 为什么不能用 `session` 夹具

那个夹具把整个用例包在一个最后回滚的事务里。两个 worker 互不相交这件事
**必须**由两个真连接来证:同一个事务里跑两遍 `claim_items`,第二遍本来就
看得见第一遍的未提交改动,测出来的绿是假的。这一组自己开两条连接、真提交,
teardown 里按主键删干净 —— 与 `test_batch_receipt_lifecycle_db.py` 同一套路数。

## 这一组守的是什么

1. **两个 worker 各领各的**(`SKIP LOCKED` 真的生效,不是排队也不是重叠)。
2. **租约没过期时,别人领不到**。这是"不重复付费"的唯一防线。
3. **租约过期后可以被接管**,包括 `lease_until IS NULL` 的存量残骸(§3.13)。
4. **回收器仍然不问 owner 死活,但旧 worker 已经写不进来了(A43)。**
   `reap_expired_leases` 的取数条件没变(只看 `lease_until`)—— 那是对的:
   系统里没有"哪些 worker 还活着"的名单,用 owner 判定"能不能抢"要先有
   那份名单。变的是**落库**那一端:回收时吊销 `lease_token`,原 worker
   跑完之后的条件更新影响 0 行,结果只进审计。

   下面 `test_the_reaper_does_not_ask_whether_the_owner_is_still_alive`
   在 A42 里记录的是一个**已知缺口**,断言写着"旧 worker 最终可以覆盖状态"。
   A43 把那半条断言翻过来了:抢活照旧,覆盖不再发生。
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import OperationalError
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
def two_workers(engine):
    """两条**独立连接**,各自真提交。

    命名刻意叫 worker 而不是 session:这一组要模拟的就是两个进程,
    而"它们是不是同一个事务"正是被测的东西。
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    a: Session = factory()
    b: Session = factory()
    jobs: list[uuid.UUID] = []
    products: list[uuid.UUID] = []

    def make_batch(item_count: int, *, action: BatchAction = BatchAction.GENERATE_COPY):
        job = BatchJob(action=action.value, status="QUEUED", created_by="tester")
        a.add(job)
        a.flush()
        for _ in range(item_count):
            product = Product(
                spu="SPU-LEASE",
                sku=f"SKU-LEASE-{uuid.uuid4().hex[:8]}",
                name="租约测试泳衣",
                garment_type="BIKINI_SET",
            )
            a.add(product)
            a.flush()
            products.append(product.id)
            a.add(
                BatchJobItem(
                    batch_id=job.id,
                    product_id=product.id,
                    sku=product.sku,
                    spu=product.spu,
                    scope_key=product.sku,
                    status=BatchItemStatus.PENDING.value,
                    idempotency_key=f"lease-{uuid.uuid4().hex}",
                    attempts=0,
                )
            )
        a.commit()
        jobs.append(job.id)
        return job

    a.make_batch = make_batch  # type: ignore[attr-defined]
    try:
        yield a, b
    finally:
        for db in (a, b):
            db.rollback()
        if jobs:
            a.execute(delete(BatchJobItem).where(BatchJobItem.batch_id.in_(jobs)))
            a.execute(delete(BatchJob).where(BatchJob.id.in_(jobs)))
        if products:
            a.execute(delete(Product).where(Product.id.in_(products)))
        a.commit()
        a.close()
        b.close()


def _job_for(db: Session, job_id: uuid.UUID) -> BatchJob:
    """在**另一条**连接上重新取同一个批次。跨 session 传 ORM 对象是无效的。"""
    return db.scalars(select(BatchJob).where(BatchJob.id == job_id)).one()


def _items(db: Session, job_id: uuid.UUID) -> list[BatchJobItem]:
    db.expire_all()
    return list(
        db.scalars(
            select(BatchJobItem)
            .where(BatchJobItem.batch_id == job_id)
            .order_by(BatchJobItem.created_at, BatchJobItem.sku)
        )
    )


# ---------------------------------------------------------------- 互不相交


def test_skip_locked_gives_each_worker_a_disjoint_set(two_workers):
    """**这一组存在的头号理由。**

    `FOR UPDATE SKIP LOCKED` 没写对时的表现不是报错:两个 worker 会捞到
    同一批 PENDING、同时置 RUNNING、同时开跑,最后靠 `_execute` 里的
    advisory lock 挡下来 —— 挡是挡住了,代价是一半条目落成
    `CONCURRENT_IN_FLIGHT`,运营看到"跑了一半失败了"。而这一切只在
    两个 worker 撞上的那一次发生,那一次没有人在看屏幕。
    """
    a, b = two_workers
    job = a.make_batch(6)  # type: ignore[attr-defined]

    # A 领一件但**不提交** —— 行锁还握在 A 手里
    claimed_a = bs.claim_items(a, job, owner="worker-A", limit=1)
    assert len(claimed_a) == 1

    # 给 B 装一个短锁超时。**没有它,`skip_locked` 被删掉时这条用例会挂住**,
    # 而挂住的 CI 只能靠整体超时收场,报出来的是"作业超时"不是"锁语义错了"。
    # 三秒足够 —— 正确实现下 B 根本不会等锁。
    b.execute(text("SET LOCAL lock_timeout = '3s'"))

    job_b = _job_for(b, job.id)
    try:
        claimed_b = bs.claim_items(b, job_b, owner="worker-B", limit=1)
    except OperationalError as exc:  # pragma: no cover - 只在变异时走到
        pytest.fail(
            "B 在等 A 的行锁,说明领取语句丢了 SKIP LOCKED:"
            "两个 worker 会排队而不是各领各的。"
            f"原始错误:{type(exc).__name__}"
        )
    assert len(claimed_b) == 1, "SKIP LOCKED 没生效:B 要么被阻塞、要么什么都没拿到"

    ids_a = {row.id for row in claimed_a}
    ids_b = {row.id for row in claimed_b}
    assert not (ids_a & ids_b), (
        f"两个 worker 领到了同一条目 {ids_a & ids_b} —— "
        "重复投递不再安全,而任务 18 的自动重投正建立在这一条上"
    )
    a.commit()
    b.commit()


def test_a_second_worker_cannot_take_an_item_whose_lease_is_still_alive(two_workers):
    """租约没过期时别人领不到。**这是"不重复付费"的唯一防线。**

    回执表挡不住这件事:回执是付费调用**之后**才写的。
    """
    a, b = two_workers
    job = a.make_batch(1)  # type: ignore[attr-defined]

    claimed = bs.claim_items(a, job, owner="worker-A", limit=1)
    assert len(claimed) == 1
    a.commit()  # 租约必须先落库,别的 worker 才看得见

    job_b = _job_for(b, job.id)
    assert bs.claim_items(b, job_b, owner="worker-B", limit=1) == [], (
        "租约还活着,B 却把它领走了 —— 同一件事会第二次调用付费模型"
    )
    b.commit()


# ---------------------------------------------------------------- 过期与接管


def test_an_expired_lease_can_be_taken_over_by_another_worker(two_workers):
    """worker 死在半路时,它领走的那几件停在 RUNNING。

    只捞 PENDING 的话谁都碰不到它们 —— 那正是 A35 之前的行为,
    也是运营点"重试"得到 409 的原因。
    """
    a, b = two_workers
    job = a.make_batch(1)  # type: ignore[attr-defined]

    claimed = bs.claim_items(a, job, owner="worker-A", limit=1)
    item_id = claimed[0].id
    # 把租约调到过去,等价于"A 领完就死了,而且已经过了一个租约周期"
    claimed[0].lease_until = utc_now() - timedelta(seconds=1)
    a.commit()

    job_b = _job_for(b, job.id)
    taken = bs.claim_items(b, job_b, owner="worker-B", limit=1)
    b.commit()

    assert [row.id for row in taken] == [item_id]
    assert taken[0].lease_owner == "worker-B"
    assert taken[0].attempts == 2, "接管必须累加 attempts,否则 MAX_ITEM_ATTEMPTS 封不住顶"


def test_a_null_lease_is_claimable_because_it_is_pre_migration_debris(two_workers):
    """`lease_until IS NULL` 必须算已过期(§3.13)。

    SQL 里 `NULL <= now` 求值是 NULL 不是真,漏掉这一条,0025 迁移之前
    留下的 RUNNING 残骸**永远领不到** —— 而那批行正是这一整套代码要修的东西。
    判定口径在 `batch.lease_expired()` 一处,这里验的是 SQL 侧与它同向。
    """
    a, b = two_workers
    job = a.make_batch(1)  # type: ignore[attr-defined]

    row = _items(a, job.id)[0]
    row.status = BatchItemStatus.RUNNING.value  # 存量残骸的样子
    row.lease_until = None
    row.lease_owner = None
    a.commit()

    job_b = _job_for(b, job.id)
    taken = bs.claim_items(b, job_b, owner="worker-B", limit=1)
    b.commit()
    assert len(taken) == 1, "NULL 租约被当成了「永不过期」,存量残骸会被永久封存"


# ---------------------------------------------------------------- 回收器


def test_the_reaper_puts_an_abandoned_item_back_in_the_queue(two_workers):
    a, b = two_workers
    job = a.make_batch(1)  # type: ignore[attr-defined]

    claimed = bs.claim_items(a, job, owner="dead-worker", limit=1)
    claimed[0].lease_until = utc_now() - timedelta(seconds=1)
    a.commit()

    stats = bs.reap_expired_leases(b)
    b.commit()

    assert stats["requeued"] == 1
    row = _items(a, job.id)[0]
    assert row.status == BatchItemStatus.PENDING.value
    assert row.lease_owner is None and row.lease_until is None


def test_the_reaper_gives_up_after_the_cap_and_says_why(two_workers):
    """超过 `MAX_ITEM_ATTEMPTS` 之后落 `WORKER_LOST`,交给人。

    上限存在的理由是钱:一件能打死 worker 的条目会被无限回收,
    每一轮都可能是一次真金白银的调用,而没有人在看。
    """
    a, b = two_workers
    job = a.make_batch(1)  # type: ignore[attr-defined]

    row = _items(a, job.id)[0]
    row.status = BatchItemStatus.RUNNING.value
    row.attempts = rules.MAX_ITEM_ATTEMPTS
    row.lease_owner = "dead-worker"
    row.lease_until = utc_now() - timedelta(seconds=1)
    a.commit()

    stats = bs.reap_expired_leases(b)
    b.commit()

    assert stats["gave_up"] == 1
    reaped = _items(a, job.id)[0]
    assert reaped.status == BatchItemStatus.FAILED.value
    assert reaped.error_code == "WORKER_LOST"
    assert reaped.error_message, "放弃是一个会改状态的动作,必须留下一句能解释的话"


def test_the_reaper_does_not_ask_whether_the_owner_is_still_alive(two_workers):
    """回收器照旧不问 owner 死活 —— **但旧 worker 已经写不进来了(A43)。**

    这条用例在 A42 里记录的是一个已知缺口,后半段断言写着
    "落库回调不校验租约归属"。BLOCK-02 修完之后,前半段不变、后半段翻转:

        抢活    照旧。系统里没有"哪些 worker 还活着"的名单,
                用 owner 判定"能不能抢"要先有那份名单 —— 那是另一个问题
        覆盖    不再发生。回收时 `lease_token` 被吊销,
                A 的条件更新影响 0 行,它的结果只进审计

    换句话说:**重叠执行仍然可能发生(钱可能真的花了两次),但状态互踩不会。**
    前者由租约时长和续租控制,后者由令牌控制,两件事不要混在一起看。
    """
    a, b = two_workers
    job = a.make_batch(1)  # type: ignore[attr-defined]

    # A 领走并且**还活着**(下面不会调 settle,模拟它正在跑)
    claimed = bs.claim_items(a, job, owner="worker-A-still-alive", limit=1)
    token_a = claimed[0].lease_token
    assert token_a is not None, "领取必须盖一个令牌,否则条件更新没有凭证"
    claimed[0].lease_until = utc_now() - timedelta(seconds=1)  # 只是超时了
    a.commit()

    stats = bs.reap_expired_leases(b)
    b.commit()

    assert stats["requeued"] == 1, "取数条件没变:租约过期就可以被抢"
    stolen = _items(a, job.id)[0]
    assert stolen.status == BatchItemStatus.PENDING.value
    assert stolen.lease_token is None, "回收 = 吊销令牌。这一句就是交接本身"

    # A 跑完了,拿着旧令牌来落库 —— 影响 0 行
    a.expire_all()
    row_a = _items(a, job.id)[0]
    applied = bs.apply_outcome(
        a, row_a, _succeeded(row_a), owner="worker-A-still-alive", token=token_a
    )
    a.commit()

    assert applied is False, "执行权已经交接,落库必须失败"
    final = _items(b, job.id)[0]
    assert final.status == BatchItemStatus.PENDING.value, (
        "A 的结果不能改变一个已经由别人接管的状态"
    )
    assert final.paid_calls == 0, "旧 worker 不得再次累加付费次数"


def test_the_new_owner_decides_the_final_state(two_workers):
    """A 超时被回收、B 接管跑完、A 才返回 —— 最终状态只由 B 决定。

    BLOCK-02 的验收标准逐条对应:
    A 的条件更新影响 0 行;最终状态由 B 决定;`paid_calls` 不被 A 再次累加。
    """
    a, b = two_workers
    job = a.make_batch(1)  # type: ignore[attr-defined]

    claimed_a = bs.claim_items(a, job, owner="worker-A", limit=1)
    token_a = claimed_a[0].lease_token
    claimed_a[0].lease_until = utc_now() - timedelta(seconds=1)
    a.commit()

    # 回收 + B 接管
    bs.reap_expired_leases(b)
    b.commit()
    claimed_b = bs.claim_items(b, job, owner="worker-B", limit=1)
    token_b = claimed_b[0].lease_token
    b.commit()
    assert token_b != token_a, "每次领取必须换一个新令牌"

    # B 正常落库
    row_b = _items(b, job.id)[0]
    assert bs.apply_outcome(b, row_b, _succeeded(row_b), owner="worker-B", token=token_b)
    b.commit()

    # A 现在才跑完
    a.expire_all()
    row_a = _items(a, job.id)[0]
    assert not bs.apply_outcome(
        a, row_a, _succeeded(row_a), owner="worker-A", token=token_a
    )
    a.commit()

    final = _items(b, job.id)[0]
    assert final.status == BatchItemStatus.SUCCEEDED.value
    assert final.paid_calls == 1, (
        "两次执行只记一次 —— 另一次进审计。业务行上的付费计数属于"
        "当前有效的那一次执行"
    )


def test_renewal_fails_once_the_lease_has_been_handed_over(two_workers):
    """续租是**花钱之前**的那道门。交接之后它必须返回 False。"""
    a, b = two_workers
    job = a.make_batch(1)  # type: ignore[attr-defined]

    claimed = bs.claim_items(a, job, owner="worker-A", limit=1)
    item_id, token = claimed[0].id, claimed[0].lease_token
    a.commit()

    # 还持有时:续租成功,并把到期时刻往后推
    before = _items(b, job.id)[0].lease_until
    assert bs.renew_lease(a, item_id, owner="worker-A", token=token)
    a.commit()
    assert _items(b, job.id)[0].lease_until >= before

    # 被抢走之后:续租失败
    a.expire_all()
    row = _items(a, job.id)[0]
    row.lease_until = utc_now() - timedelta(seconds=1)
    a.commit()
    bs.reap_expired_leases(b)
    b.commit()

    a.expire_all()
    assert not bs.renew_lease(a, item_id, owner="worker-A", token=token), (
        "续租失败是唯一一个'已经知道自己出局、但还没花钱'的时刻"
    )


def _succeeded(row: BatchJobItem) -> rules.ItemOutcome:
    """一条成功回执。三条用例共用,免得断言被构造代码淹掉。"""
    return rules.ItemOutcome(
        item=rules.PlannedItem(
            product_id=str(row.product_id),
            sku=row.sku,
            spu=row.spu,
            scope_key=row.scope_key,
            executable=True,
            idempotency_key=row.idempotency_key,
        ),
        status=BatchItemStatus.SUCCEEDED,
        code=None,
        message="",
        target_step=None,
        target_ref=None,
        paid_calls=1,
        reused=False,
        payload={},
    )


# ---------------------------------------------------------------- 领取批量


def test_one_item_per_claim_keeps_the_lease_invariant_true_in_the_database(two_workers):
    """`CLAIM_CHUNK = 1` 的真库表现:一次领取只盖一个租约。

    A42 之前一次领 10 件、盖**同一个** `lease_until`,而整批是顺序跑的 ——
    第 3 件开跑时租约就到期了。这条用例把"一次领取的条目数"和
    "它们共用几个到期时刻"一起验了:两个数都必须是 1。
    """
    a, _ = two_workers
    job = a.make_batch(5)  # type: ignore[attr-defined]

    claimed = bs.claim_items(a, job, owner="worker-A")
    a.commit()

    assert len(claimed) == rules.CLAIM_CHUNK == 1
    assert len({row.lease_until for row in claimed}) == 1
    assert (
        rules.ITEM_LEASE_SECONDS
        > rules.CLAIM_CHUNK * rules.LONGEST_LEGAL_ITEM_SECONDS
    ), "不变量在真库这一侧也必须成立,不能只在导入期 assert 里成立"
    remaining = [
        row for row in _items(a, job.id) if row.status == BatchItemStatus.PENDING.value
    ]
    assert len(remaining) == 4, "领了一件,其余四件必须原封不动留在队列里"
