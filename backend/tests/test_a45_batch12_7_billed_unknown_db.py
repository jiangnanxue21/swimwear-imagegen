"""重复扣费残窗的**真库演练**(PRD v3.1.1 §13 阶段 P0 第五条)。

P0 那一条逐字要求三件事:`BILLED_RESULT_UNKNOWN` 闸(`MAX_BILLED_EXECUTIONS = 2`)
在真库演练、`tools/resolve_billed_unknown.py` 解除通道在真库演练、以及
「worker 死在付费调用与写回执之间」有**双 session** 用例。此前这三件事的覆盖
全在 `tests/pure/`,而纯测试对着的是 `receipt_route()` 那个纯函数 ——
它拿到的 `executions` / `call_dispatched` 是用例自己造的整数和布尔值。

**这一组要验的恰恰是那两个值从哪来。** 它们由两次独立事务写进
`batch_action_receipts`,而独立事务存在的全部理由就是「业务事务回滚之后
它还在」。用 `session` 夹具(把用例包在一个最后回滚的事务里)测不了这件事:
那种写法下标记会跟着回滚,于是「独立事务」和「普通写」完全等价。

所以这一组和 `test_batch_receipt_lifecycle_db.py` 一样自己开真会话、真提交,
teardown 按主键删干净。

## 三条链路的分工(别合并)

    _claim_in_flight    这条作用域**开跑**过几次      -> executions
    _mark_provider_call **这一次**把钱花出去了没有   -> provider_call_at
    _write_receipt      结论                          -> status

batch12-2 / EX-02 把前两者拆开的理由是:压在同一个 IN_FLIGHT 上时,回收之后
只能靠猜。猜没花钱就自动重跑(可能重复计费),猜花了钱就一律转人工
(基础设施抖一下停一批)。这一组按这个分工逐条钉住。

## 状态

**已写,未执行。** 整改环境没有 PostgreSQL。与 batch12-4 的 6 条、
batch12-5 的 7 条同一批口径,见 `docs/STATUS.md` 阶段 P0 那一节。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.enums import AuditAction
from app.models.audit_log import AuditLog
from app.models.batch_job import BatchActionReceipt
from app.models.product import Product
from app.workbench import batch as rules
from app.workbench import batch_service as bs
from app.workbench.batch import BatchAction
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def drill(engine, monkeypatch):
    """两条**各自提交**的连接,外加一个把主库重定向到测试库的补丁。

    `worker` 扮演那个会死掉的执行体,`ops` 扮演事后来看的人。两者必须是
    不同的连接:同一个连接里读得到自己未提交的写,而这一组要验的正是
    「另一个进程看得见吗」。
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    # `_claim_in_flight` / `_mark_provider_call` / 解除脚本都走
    # `app.db.session.SessionLocal`(按主库建),而这一组跑在 `*_test` 库上。
    # 不重定向的话标记会写进主库,测试库里查不到 —— 认领"成功"却什么都没有。
    # 生产里两者本来就是同一个库,所以这只是夹具接线,不改被测行为。
    import app.db.session as db_session_module

    monkeypatch.setattr(db_session_module, "SessionLocal", factory)

    worker: Session = factory()
    ops: Session = factory()
    keys: list[str] = []
    products: list[uuid.UUID] = []

    def make_product(sku: str) -> Product:
        product = Product(
            spu="SPU-BILLED-UNKNOWN",
            sku=sku,
            name="费用不明演练泳衣",
            garment_type="BIKINI_SET",
        )
        worker.add(product)
        worker.commit()
        products.append(product.id)
        return product

    def track(key: str) -> str:
        keys.append(key)
        return key

    worker.make_product = make_product  # type: ignore[attr-defined]
    worker.track = track  # type: ignore[attr-defined]
    try:
        yield worker, ops
    finally:
        for db in (worker, ops):
            db.rollback()
        if keys:
            worker.execute(
                delete(BatchActionReceipt).where(
                    BatchActionReceipt.idempotency_key.in_(keys)
                )
            )
            worker.execute(
                delete(AuditLog).where(AuditLog.entity_type == "BatchActionReceipt")
            )
        if products:
            worker.execute(delete(Product).where(Product.id.in_(products)))
        worker.commit()
        for db in (worker, ops):
            db.close()


def _row(db: Session, key: str) -> BatchActionReceipt | None:
    db.expire_all()
    return db.scalars(
        select(BatchActionReceipt).where(BatchActionReceipt.idempotency_key == key)
    ).first()


def _route(db: Session, key: str, action: BatchAction) -> rules.ReceiptRoute:
    """按**库里那一行**判路,不按用例手写的整数。

    这是这一组存在的理由:`receipt_route()` 的纯测试覆盖的是判定,
    这里覆盖的是喂给它的两个值真的落库了、真的跨进程可见。
    """
    row = _row(db, key)
    return rules.receipt_route(
        None if row is None else row.status,
        action,
        executions=1 if row is None else (row.executions or 1),
        call_dispatched=(True if row is None else row.provider_call_at is not None),
    )


def _claim(key: str, product: Product, action: BatchAction) -> bool:
    return bs._claim_in_flight(
        key=key,
        action=action,
        scope_key=product.sku,
        product_id=product.id,
        actor="drill",
    )


# ============================================ 一、worker 死在付费调用与写回执之间


def test_the_in_flight_marker_survives_the_business_transaction_rollback(drill):
    """**这一条是整组的主线。** 标记必须活过业务事务的回滚。

    崩溃的形状就是「业务事务没提交」。标记要是写在同一个事务里,它会跟着
    消失,于是下一次执行读到的是「没有回执」-> `EXECUTE` -> 再花一次钱,
    而这一次的钱已经花过了。

    `_claim_in_flight` 用独立会话正是为了这件事。用 `session` 夹具(整体回滚)
    验不了它 —— 那种写法下这条断言恒真,而它恒真的原因是根本没测到。
    """
    worker, ops = drill
    product = worker.make_product("SKU-BU-CRASH")  # type: ignore[attr-defined]
    key = worker.track(f"drill:crash:{uuid.uuid4()}")  # type: ignore[attr-defined]

    assert _claim(key, product, BatchAction.EXTRACT) is True
    assert bs._mark_provider_call(key=key) is True

    # worker 死了:业务事务什么都没提交
    worker.rollback()

    seen = _row(ops, key)
    assert seen is not None, (
        "另一个连接看不见这条标记 —— 独立事务失效了。"
        "崩溃之后没有任何东西记得这次可能已经花掉的钱"
    )
    assert seen.status == rules.ReceiptStatus.IN_FLIGHT.value
    assert seen.executions == 1
    assert seen.provider_call_at is not None, (
        "付费调用时刻没落库。少了它,回收只能靠猜 —— 而猜没花钱就是自动重跑"
    )


def test_a_dispatched_call_stops_at_reconciliation_on_the_very_first_recovery(drill):
    """请求发出去过 -> **第一次**回收就停,不消耗额度、不自动重跑。

    batch12-2 / EX-02 之前这里会走 `_billed_again_or_stop()`,而
    `executions == 1` 时它答 `EXECUTE_AGAIN_BILLED` —— 一次由回收器发起、
    没有任何人决定过的付费重跑。
    """
    worker, ops = drill
    product = worker.make_product("SKU-BU-DISPATCHED")  # type: ignore[attr-defined]
    key = worker.track(f"drill:dispatched:{uuid.uuid4()}")  # type: ignore[attr-defined]

    _claim(key, product, BatchAction.EXTRACT)
    bs._mark_provider_call(key=key)
    worker.rollback()

    assert _route(ops, key, BatchAction.EXTRACT) is rules.ReceiptRoute.NEEDS_RECONCILIATION
    assert _row(ops, key).executions == 1, (
        "停下来不该消耗付费额度:`executions` 记的是开跑过几次,"
        "而判定停下来跟还剩几次额度无关"
    )


def test_a_marker_without_a_dispatched_call_is_free_to_rerun(drill):
    """反过来的一半:认领落了、调用没发出 -> 可以免费重跑。

    这条和上一条必须同时成立。把两者合并成「有 IN_FLIGHT 就转人工」的话,
    一次基础设施抖动会把一批根本没花钱的条目全部推给人对账。
    """
    worker, ops = drill
    product = worker.make_product("SKU-BU-NOTSENT")  # type: ignore[attr-defined]
    key = worker.track(f"drill:notsent:{uuid.uuid4()}")  # type: ignore[attr-defined]

    _claim(key, product, BatchAction.EXTRACT)  # 只认领,不 mark
    worker.rollback()

    row = _row(ops, key)
    assert row.provider_call_at is None
    assert _route(ops, key, BatchAction.EXTRACT) is rules.ReceiptRoute.EXECUTE


# ============================================ 二、MAX_BILLED_EXECUTIONS 那道闸


def test_the_execution_counter_really_increments_across_sessions(drill):
    """`executions` 由独立事务累加,而且跨连接看得见。

    闸门读的就是这个数。它要是不落库(或者只在本会话里 +1),
    `MAX_BILLED_EXECUTIONS` 就永远拦不住任何东西。
    """
    worker, ops = drill
    product = worker.make_product("SKU-BU-COUNT")  # type: ignore[attr-defined]
    key = worker.track(f"drill:count:{uuid.uuid4()}")  # type: ignore[attr-defined]

    _claim(key, product, BatchAction.EXTRACT)
    assert _row(ops, key).executions == 1
    _claim(key, product, BatchAction.EXTRACT)
    assert _row(ops, key).executions == 2, (
        "第二次认领没有把计数推上去 —— 那道闸读到的永远是 1"
    )


def test_the_billed_failure_gate_opens_once_and_then_stops(drill):
    """已付费失败 + 不可离线复校的动作:允许再自动跑一次,第二次停。

    `EXTRACT` 全失败意味着一条证据都没落,没有东西可以「重新校验」,
    所以它走的是付费重跑那条路 —— 也就是唯一受这个上限约束的一条。
    """
    worker, ops = drill
    product = worker.make_product("SKU-BU-GATE")  # type: ignore[attr-defined]
    key = worker.track(f"drill:gate:{uuid.uuid4()}")  # type: ignore[attr-defined]

    _claim(key, product, BatchAction.EXTRACT)
    bs._write_receipt(
        worker,
        key=key,
        action=BatchAction.EXTRACT,
        scope_key=product.sku,
        product_id=product.id,
        paid_calls=1,
        result={},
        actor="drill",
        status=rules.ReceiptStatus.FAILED_BILLED,
    )
    worker.commit()

    assert _row(ops, key).executions == 1
    assert _route(ops, key, BatchAction.EXTRACT) is rules.ReceiptRoute.EXECUTE_AGAIN_BILLED

    # 第二次开跑,而且**要跑完**。
    #
    # 上一版到 `_claim` 就去问路了(A45-batch14-6 修)。那一刻库里那行是
    # `IN_FLIGHT` + `provider_call_at IS NULL` —— `_claim_in_flight` 每次
    # 认领都把 `provider_call_at` 清成 NULL。于是 `receipt_route()` 走的是
    # IN_FLIGHT 那一支,答 `EXECUTE`("这一次压根没跑到花钱那一步,
    # 不消耗付费额度"),而不是付费上限那一支。
    #
    # 也就是说这条断言原来问的是"一次在飞的执行要不要拦",而它的文档字符串
    # 说的是"第二次**跑完也失败了**之后要不要再跑"。**代码是对的,问法不对。**
    # 补上第二张 FAILED_BILLED 回执,才是那句话描述的状态。
    _claim(key, product, BatchAction.EXTRACT)
    assert _row(ops, key).executions == rules.MAX_BILLED_EXECUTIONS
    bs._write_receipt(
        worker,
        key=key,
        action=BatchAction.EXTRACT,
        scope_key=product.sku,
        product_id=product.id,
        paid_calls=1,
        result={},
        actor="drill",
        status=rules.ReceiptStatus.FAILED_BILLED,
    )
    worker.commit()

    assert _route(ops, key, BatchAction.EXTRACT) is rules.ReceiptRoute.NEEDS_RECONCILIATION, (
        f"开跑 {rules.MAX_BILLED_EXECUTIONS} 次之后仍然允许自动付费重跑 —— "
        "那道闸在库里不生效"
    )


def test_a_revalidatable_action_is_not_charged_again(drill):
    """文案生成的已付费失败走离线复校验,不受上限约束,也不再花钱。"""
    worker, ops = drill
    product = worker.make_product("SKU-BU-REVALIDATE")  # type: ignore[attr-defined]
    key = worker.track(f"drill:revalidate:{uuid.uuid4()}")  # type: ignore[attr-defined]

    _claim(key, product, BatchAction.GENERATE_COPY)
    _claim(key, product, BatchAction.GENERATE_COPY)
    bs._write_receipt(
        worker,
        key=key,
        action=BatchAction.GENERATE_COPY,
        scope_key=product.sku,
        product_id=product.id,
        paid_calls=1,
        result={},
        actor="drill",
        status=rules.ReceiptStatus.FAILED_BILLED,
    )
    worker.commit()

    assert _row(ops, key).executions >= rules.MAX_BILLED_EXECUTIONS
    assert (
        _route(ops, key, BatchAction.GENERATE_COPY)
        is rules.ReceiptRoute.REUSE_BILLED_FAILURE
    ), "可离线复校验的动作被上限挡住了 —— 它根本不花钱,不该受这道闸约束"


# ============================================ 三、解除通道(闸门必须有出口)


def _resolve(argv: list[str]) -> int:
    """跑一遍解除脚本。它的逻辑全在 `main()` 里,入口就是 argv。"""
    import sys

    from tools import resolve_billed_unknown

    original = sys.argv
    sys.argv = ["resolve_billed_unknown", *argv]
    try:
        return resolve_billed_unknown.main()
    finally:
        sys.argv = original


def test_the_stuck_receipt_shows_up_in_the_listing(drill, capsys):
    """卡住的那一行必须列得出来,并且带上对账真正要用的时刻。

    取数条件跟着 `receipt_route` 一起改过(EX-02):新口径是「调用已发出」,
    旧口径是「额度耗尽」。只留旧口径的话,现在唯一会卡住的那一批
    **一条都列不出来** —— 闸门装上了、出口对不上号。
    """
    worker, _ = drill
    product = worker.make_product("SKU-BU-LIST")  # type: ignore[attr-defined]
    key = worker.track(f"drill:list:{uuid.uuid4()}")  # type: ignore[attr-defined]

    _claim(key, product, BatchAction.EXTRACT)
    bs._mark_provider_call(key=key)
    worker.rollback()

    assert _resolve([]) == 0
    out = capsys.readouterr().out
    assert key in out, "卡住的回执没有出现在清单里 —— 这个出口对不上号"
    assert "付费调用发出时刻" in out, (
        "清单没给出付费调用时刻。没有它,「去后台核对」就退化成"
        "在整天的请求里大海捞针"
    )


def test_applying_without_a_reconciliation_note_is_refused(drill):
    """解除必须填对账结论。这是唯一一个由人主动承担重复计费风险的动作。"""
    worker, ops = drill
    product = worker.make_product("SKU-BU-NONOTE")  # type: ignore[attr-defined]
    key = worker.track(f"drill:nonote:{uuid.uuid4()}")  # type: ignore[attr-defined]

    _claim(key, product, BatchAction.EXTRACT)
    bs._mark_provider_call(key=key)
    worker.rollback()

    assert _resolve(["--key", key, "--apply"]) == 1
    assert _row(ops, key) is not None, "拒绝之后回执还得在"


def test_a_dry_run_changes_nothing(drill):
    """默认干跑。少了这条,一次手滑就是一笔重复的付费调用。"""
    worker, ops = drill
    product = worker.make_product("SKU-BU-DRY")  # type: ignore[attr-defined]
    key = worker.track(f"drill:dry:{uuid.uuid4()}")  # type: ignore[attr-defined]

    _claim(key, product, BatchAction.EXTRACT)
    bs._mark_provider_call(key=key)
    worker.rollback()

    assert _resolve(["--key", key, "--note", "后台无该 request"]) == 0
    assert _row(ops, key) is not None, "干跑把行删掉了"


def test_resolving_reopens_the_scope_and_leaves_an_audit_trail(drill):
    """闸门的出口:删掉回执之后,下一次执行按「没做过」正常走。

    审计是这条动作唯一的留存 —— 行被删了,操作者与对账结论只剩审计还记得。
    """
    worker, ops = drill
    product = worker.make_product("SKU-BU-RESOLVE")  # type: ignore[attr-defined]
    key = worker.track(f"drill:resolve:{uuid.uuid4()}")  # type: ignore[attr-defined]

    _claim(key, product, BatchAction.EXTRACT)
    bs._mark_provider_call(key=key)
    worker.rollback()
    assert _route(ops, key, BatchAction.EXTRACT) is rules.ReceiptRoute.NEEDS_RECONCILIATION

    note = "FASHN 后台查 2026-08-04 无该 request,未计费"
    assert _resolve(["--key", key, "--note", note, "--apply"]) == 0

    assert _row(ops, key) is None, "回执没被删掉 —— 这条作用域仍然卡着"
    assert _route(ops, key, BatchAction.EXTRACT) is rules.ReceiptRoute.EXECUTE, (
        "解除之后仍然走不通。闸门没有出口就是死胡同:"
        "幂等键由动作 + 作用域 + 上游指纹算出来,不会自己变"
    )

    ops.expire_all()
    trail = ops.scalars(
        select(AuditLog).where(
            AuditLog.entity_type == "BatchActionReceipt",
            AuditLog.action == AuditAction.DELETE.value,
        )
    ).all()
    assert trail, "删除没有留下审计 —— 那笔钱花在哪就再也没人答得上来"
    assert any(note[:20] in str(row.payload) for row in trail), (
        "审计里没有对账结论。留一条不带结论的删除记录,等于只记了「谁删的」"
    )


def test_a_succeeded_receipt_is_never_deleted_by_this_tool(drill):
    """只解除 IN_FLIGHT。删掉一条 SUCCEEDED 会让已成功的动作重新付费执行。

    这是这个脚本最容易造成的伤害,所以它必须自己挡住。
    """
    worker, ops = drill
    product = worker.make_product("SKU-BU-SUCCESS")  # type: ignore[attr-defined]
    key = worker.track(f"drill:success:{uuid.uuid4()}")  # type: ignore[attr-defined]

    _claim(key, product, BatchAction.GENERATE_COPY)
    bs._write_receipt(
        worker,
        key=key,
        action=BatchAction.GENERATE_COPY,
        scope_key=product.sku,
        product_id=product.id,
        paid_calls=1,
        result={"ok": True},
        actor="drill",
        status=rules.ReceiptStatus.SUCCEEDED,
    )
    worker.commit()

    assert _resolve(["--key", key, "--note", "手滑了", "--apply"]) == 1
    assert _row(ops, key) is not None
    assert _route(ops, key, BatchAction.GENERATE_COPY) is rules.ReceiptRoute.REUSE_SUCCESS
