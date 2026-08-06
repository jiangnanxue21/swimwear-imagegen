"""回执生命周期的**真库行为**测试(评审第 2 / 3 条,以及第 18 条要的那类测试)。

## 为什么这一组必须真的 commit

被测的核心是 `_claim_in_flight` 的**独立会话**:它刻意在业务事务之外提交,
因为标记存在的全部理由就是"进程在模型调用中途死掉之后它还在"。业务事务里
写的标记会随崩溃一起回滚,那等于没写。

于是这一组不能用 `session` 夹具 —— 那个夹具把整个用例包在一个最后回滚的
事务里,独立会话根本看不见它建的商品(外键当场失败),而独立会话提交的行
也不会被那次回滚清掉。A15 把 `_mark_in_flight` 列为"没有行为级测试,它要真库",
这里补上:自己开真会话、真提交,teardown 里按主键删干净。

## 这一组守的是什么

原来的实现用"再插一行"表达同一条回执的生命周期:

    _mark_in_flight  裸 INSERT,失败只记日志
    _write_receipt   只有旧状态恰好是 IN_FLIGHT 才 UPDATE,否则 INSERT
                     -> 撞唯一约束 -> except 分支只加 reuse_count,**不碰 status**

而重试时行已经存在,那次 INSERT 必然冲突。两处合起来的后果是:
`FAILED_BILLED` 的条目重试成功之后,回执**永远停在 FAILED_BILLED**,
下一次重试再付一次费,循环不收敛。

`test_billed_failure_then_successful_retry_ends_at_succeeded` 是这一组的主线。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.batch_job import BatchActionReceipt
from app.models.product import Product
from app.workbench import batch as rules
from app.workbench import batch_service as bs
from app.workbench.batch import BatchAction
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def committed(engine, monkeypatch):
    """真会话 + 真提交,teardown 里删掉自己造的行。

    不复用 `session` 夹具:见模块注释。
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    # `_claim_in_flight` 用的是 `app.db.session.SessionLocal` —— 它按
    # `settings.sqlalchemy_url`(主库)建,而这一组跑在 `*_test` 库上。
    # 不重定向的话标记会写进主库,测试库里查不到,认领"成功"却什么都没有。
    # 生产里两者本来就是同一个库,所以这只是夹具的接线,不改被测行为。
    import app.db.session as db_session_module

    monkeypatch.setattr(db_session_module, "SessionLocal", factory)

    db: Session = factory()
    keys: list[str] = []
    products: list[uuid.UUID] = []

    def make_product(sku: str) -> Product:
        product = Product(
            spu="SPU-RECEIPT",
            sku=sku,
            name="回执测试泳衣",
            garment_type="BIKINI_SET",
        )
        db.add(product)
        db.commit()
        products.append(product.id)
        return product

    def track(key: str) -> str:
        keys.append(key)
        return key

    db.make_product = make_product  # type: ignore[attr-defined]
    db.track = track  # type: ignore[attr-defined]
    try:
        yield db
    finally:
        db.rollback()
        if keys:
            db.execute(
                delete(BatchActionReceipt).where(
                    BatchActionReceipt.idempotency_key.in_(keys)
                )
            )
        if products:
            db.execute(delete(Product).where(Product.id.in_(products)))
        db.commit()
        db.close()


def _row(db: Session, key: str) -> BatchActionReceipt | None:
    db.expire_all()
    return db.scalars(
        select(BatchActionReceipt).where(BatchActionReceipt.idempotency_key == key)
    ).first()


def _claim(db: Session, key: str, product: Product) -> bool:
    return bs._claim_in_flight(
        key=key,
        action=BatchAction.GENERATE_COPY,
        scope_key=product.sku,
        product_id=product.id,
        actor="tester",
    )


def _settle(
    db: Session,
    key: str,
    product: Product,
    *,
    status: rules.ReceiptStatus,
    paid_calls: int,
    payload: dict | None = None,
) -> None:
    bs._write_receipt(
        db,
        key=key,
        action=BatchAction.GENERATE_COPY,
        scope_key=product.sku,
        product_id=product.id,
        paid_calls=paid_calls,
        result=payload or {},
        actor="tester",
        status=status,
    )
    db.commit()


# ------------------------------------------------------------------ 正常闭环


def test_first_run_claims_then_settles_to_succeeded(committed):
    db = committed
    product = db.make_product("SKU-RCP-OK")
    key = db.track("k-first-run")

    assert _claim(db, key, product) is True
    row = _row(db, key)
    assert row is not None, "标记必须真的落盘,而不是只记一条日志"
    assert row.status == rules.ReceiptStatus.IN_FLIGHT.value
    assert row.paid_calls == 0

    _settle(db, key, product, status=rules.ReceiptStatus.SUCCEEDED, paid_calls=1)

    row = _row(db, key)
    assert row.status == rules.ReceiptStatus.SUCCEEDED.value
    assert row.paid_calls == 1


# ------------------------------------------------------- 主线:失败后重试成功


def test_billed_failure_then_successful_retry_ends_at_succeeded(committed):
    """已付费失败 -> 重试成功 -> 终态必须是 SUCCEEDED(评审第 2 条)。

    原实现在这里停在 FAILED_BILLED,于是第三次重试会**再付一次费**,
    而界面上永远显示这一件失败。
    """
    db = committed
    product = db.make_product("SKU-RCP-RETRY")
    key = db.track("k-billed-then-ok")

    # 第一次:调用发生了(计费 1),但后处理把它拒了
    assert _claim(db, key, product) is True
    _settle(
        db, key, product,
        status=rules.ReceiptStatus.FAILED_BILLED,
        paid_calls=1,
        payload={"raw": "模型产出,合规校验未过"},
    )
    assert _row(db, key).status == rules.ReceiptStatus.FAILED_BILLED.value

    # 第二次:重试。重新认领 —— 行已存在,靠 ON CONFLICT DO UPDATE 迁移状态,
    # 而不是靠再插一行(那次 INSERT 必然冲突)
    assert _claim(db, key, product) is True, "重试时认领不能因为行已存在就失败"
    row = _row(db, key)
    assert row.status == rules.ReceiptStatus.IN_FLIGHT.value
    assert row.paid_calls == 1, "认领不该抹掉历史上真的花过的钱"

    _settle(db, key, product, status=rules.ReceiptStatus.SUCCEEDED, paid_calls=1)

    row = _row(db, key)
    assert row.status == rules.ReceiptStatus.SUCCEEDED.value, (
        "重试成功之后回执必须收敛到 SUCCEEDED —— 停在 FAILED_BILLED 会让"
        "下一次重试再付一次费,而且永远不收敛"
    )
    assert row.paid_calls == 2, "两次真的调用,台账就该是 2(§7.3 要看的信号)"

    # 收敛之后再来一次:走复用,不再计费
    assert rules.receipt_route(row.status, BatchAction.GENERATE_COPY) is (
        rules.ReceiptRoute.REUSE_SUCCESS
    )


def test_stale_identity_map_does_not_freeze_the_status(committed):
    """缺陷真正的触发机制:业务会话身份映射里那份是旧状态。

    `_execute` 会先把回执读进业务会话(查路由那一步),`_claim_in_flight`
    随后用**独立会话**把它改成 IN_FLIGHT。不 refresh 的话,`_write_receipt`
    看到的仍是旧状态,于是走进"不是 IN_FLIGHT 就 INSERT"的死路。

    这条用例刻意复现那个顺序 —— 先读进身份映射,再认领,再落终态。
    """
    db = committed
    product = db.make_product("SKU-RCP-STALE")
    key = db.track("k-stale-identity")

    _claim(db, key, product)
    _settle(db, key, product, status=rules.ReceiptStatus.FAILED_BILLED, paid_calls=1)

    # 模拟 `_execute`:先把这一行读进业务会话(此刻它是 FAILED_BILLED)
    cached = bs._receipt(db, key)
    assert cached is not None
    assert cached.status == rules.ReceiptStatus.FAILED_BILLED.value

    # 独立会话把它改成 IN_FLIGHT —— 业务会话手里那份现在是旧的
    assert _claim(db, key, product) is True

    _settle(db, key, product, status=rules.ReceiptStatus.SUCCEEDED, paid_calls=1)

    assert _row(db, key).status == rules.ReceiptStatus.SUCCEEDED.value


# ------------------------------------------------- 评审第 3 条:认领失败不许调模型


def test_claim_failure_refuses_to_call_the_paid_model(committed, monkeypatch):
    """标记落不下去时必须**停下**,不能带着"记不上"继续调用付费模型。

    原实现记一条 warning 就继续 —— 而标记存在的全部理由就是崩溃后还能知道
    这次花没花钱。记不上还继续,代价是一笔谁也对不上的费用。
    """
    db = committed
    product = db.make_product("SKU-RCP-NOCLAIM")
    key = db.track("k-claim-fails")

    class _Boom:
        def __call__(self):
            raise RuntimeError("库连不上")

    import app.db.session as db_session_module

    monkeypatch.setattr(db_session_module, "SessionLocal", _Boom())

    assert _claim(db, key, product) is False, "认领失败必须如实返回 False"
    assert _row(db, key) is None, "认领失败不该留下半条记录"


def test_claim_failure_is_not_swallowed_by_the_caller():
    """`_execute` 必须把认领失败当成中止条件,而不是继续往下调模型。

    这条不建库,只钉调用点的形状:认领结果被丢弃过一次,而那次丢弃在
    源码上看起来完全正常(函数原来返回 None)。
    """
    import inspect

    src = inspect.getsource(bs._execute)
    assert "if not _claim_in_flight(" in src, (
        "`_execute` 必须在认领失败时中止 —— 直接调用而不看返回值,"
        "等于把这条标记退化成一句日志"
    )
    claim_at = src.index("_claim_in_flight(")
    for executor in ("_exec_extract(", "_exec_generate_copy("):
        assert src.index(executor) > claim_at, f"{executor} 必须在认领之后"


# ----------------------------------------------- 未计费失败不得抹掉已发生的费用


def test_unbilled_failure_after_a_billed_one_does_not_downgrade(committed):
    """这一次没花钱,但这条作用域历史上花过 —— 不能降级成"等于没做过"。

    `FAILED_UNBILLED` 的语义是"重试该真的重跑"。把一条已经花过钱的回执
    落成它,台账就少算了一次,而少算的数字永远不会有人发现。
    """
    db = committed
    product = db.make_product("SKU-RCP-DOWNGRADE")
    key = db.track("k-no-downgrade")

    _claim(db, key, product)
    _settle(db, key, product, status=rules.ReceiptStatus.FAILED_BILLED, paid_calls=1)

    # 重试:这次连模型都没调到就失败了
    assert _claim(db, key, product) is True
    bs._settle_unbilled_failure(db, key=key)
    db.commit()

    row = _row(db, key)
    assert row.paid_calls == 1, "历史费用不能被抹成 0"
    assert row.status == rules.ReceiptStatus.FAILED_BILLED.value, (
        "历史上花过钱的回执不能降级成 FAILED_UNBILLED"
    )


def test_first_attempt_unbilled_failure_stays_unbilled(committed):
    """反过来:第一次就没花钱,那它确实等于没做过。"""
    db = committed
    product = db.make_product("SKU-RCP-UNBILLED")
    key = db.track("k-unbilled-first")

    _claim(db, key, product)
    bs._settle_unbilled_failure(db, key=key)
    db.commit()

    row = _row(db, key)
    assert row.status == rules.ReceiptStatus.FAILED_UNBILLED.value
    assert row.paid_calls == 0
    assert rules.receipt_route(row.status, BatchAction.GENERATE_COPY) is (
        rules.ReceiptRoute.EXECUTE
    )
