"""发布领域三张表。需要真实 PostgreSQL。

这一批不能放进 `tests/pure/` —— 它要验的正是**数据库层**的约束:
唯一索引、外键的 RESTRICT/CASCADE 差别、默认值。这些在 ORM 声明里看得见,
但「声明了」和「库里真的建出来了」是两件事,而它们分叉的那次代价很高:
上一轮评审第 20 条,部分唯一索引只存在于迁移里,ORM 建的库没有,
于是并发冲突在测试里永远复现不出来,生产环境会 500。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.enums import (
    PublishAttemptStatus,
    PublishOperation,
    PublishOutboxStatus,
    PublishStatus,
)
from app.models.product import Product
from app.models.publishing import ChannelListing, PublishAttempt, PublishOutbox
from app.workflows.publish_idempotency import build_publish_idempotency_key
from tests.conftest import requires_db

pytestmark = requires_db


def _product(session, sku: str = "SW-PUB-1") -> Product:
    p = Product(spu="SPU-PUB", sku=sku, name="测试泳衣")
    session.add(p)
    session.flush()
    return p


def _listing(session, product, *, site: str = "US", shop_id: str = "shop-1") -> ChannelListing:
    listing = ChannelListing(
        channel="shein", site=site, shop_id=shop_id, product_id=product.id
    )
    session.add(listing)
    session.flush()
    return listing


def _key(product, *, operation: str = PublishOperation.CREATE.value, site: str = "US") -> str:
    return build_publish_idempotency_key(
        channel="shein",
        shop_id="shop-1",
        site=site,
        operation=operation,
        product_id=str(product.id),
        draft_fingerprint="fp-1",
    )


# ---------------------------------------------------------------- ChannelListing


def test_listing_defaults_to_draft_and_gets_identity(session):
    listing = _listing(session, _product(session))
    assert listing.id is not None
    assert listing.status == PublishStatus.DRAFT.value
    assert listing.external_spu_id is None, "还没提交就不该有外部 ID"


def test_one_row_per_channel_shop_site_product(session):
    """同一个商品在同一店铺同一站点只能有一行。

    没有这条,一次重复点击就产生第二行,而「这个商品上架了吗」从此
    需要先决定信哪一行 —— 那种问题只能靠人去平台上核对。
    """
    product = _product(session)
    _listing(session, product)
    session.add(
        ChannelListing(channel="shein", site="US", shop_id="shop-1", product_id=product.id)
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_the_same_product_can_live_on_several_sites(session):
    """站点不同就是不同的一行 —— 美国站的上架不该挡住欧洲站。

    断言的是"两行真的都在库里",不是"flush 没抛异常"。后者在 autoflush
    已经发生过时会退化成空操作,于是这条用例静默通过 —— 而它守的恰恰是
    一个**不该报错**的场景,没有异常可以指望。
    """
    product = _product(session)
    _listing(session, product, site="US")
    _listing(session, product, site="DE")
    session.flush()

    sites = {
        row.site
        for row in session.query(ChannelListing).filter_by(product_id=product.id)
    }
    assert sites == {"US", "DE"}


def test_deleting_a_product_that_is_live_is_refused(session):
    """RESTRICT 不是风格选择:CASCADE 在这里等于「删一行本地数据,
    平台上多一个没人认领的商品」,正是 4.1 节 H 要防的东西。
    """
    product = _product(session)
    _listing(session, product)
    session.delete(product)
    with pytest.raises(IntegrityError):
        session.flush()


# ---------------------------------------------------------------- PublishAttempt


def test_the_same_idempotency_key_cannot_be_inserted_twice(session):
    """「重试不会重复创建」(4.1 节 D)最终落在这一条上。

    应用层的先查后写在并发下挡不住 —— 两个请求可以同时查到「没有」。
    数据库的唯一索引挡得住。
    """
    product = _product(session)
    listing = _listing(session, product)
    key = _key(product)
    for _ in range(2):
        session.add(
            PublishAttempt(
                channel_listing_id=listing.id,
                operation=PublishOperation.CREATE.value,
                idempotency_key=key,
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


def test_create_and_update_attempts_coexist(session):
    """键含 operation,所以创建和更新是两次独立的尝试,互不幂等。

    同上:这是一个"不该报错"的场景,靠 flush 不抛异常表达等于什么都没验。
    落到两行**各自的幂等键真的不同**上 —— 那才是"键含 operation"这句话
    的实际内容。
    """
    product = _product(session)
    listing = _listing(session, product)
    for operation in (PublishOperation.CREATE.value, PublishOperation.UPDATE.value):
        session.add(
            PublishAttempt(
                channel_listing_id=listing.id,
                operation=operation,
                idempotency_key=_key(product, operation=operation),
            )
        )
    session.flush()

    rows = session.query(PublishAttempt).filter_by(channel_listing_id=listing.id).all()
    assert {r.operation for r in rows} == {
        PublishOperation.CREATE.value,
        PublishOperation.UPDATE.value,
    }
    assert len({r.idempotency_key for r in rows}) == 2


def test_attempt_defaults_to_pending(session):
    product = _product(session)
    listing = _listing(session, product)
    attempt = PublishAttempt(
        channel_listing_id=listing.id,
        operation=PublishOperation.CREATE.value,
        idempotency_key=_key(product),
    )
    session.add(attempt)
    session.flush()
    assert attempt.status == PublishAttemptStatus.PENDING.value
    assert attempt.finished_at is None


def test_unknown_result_keeps_its_snapshots(session):
    """超时那条路径:结果未知,但请求快照必须留下来。

    7.4 节要求「先查询平台或使用幂等键确认」—— 确认时要拿这份快照
    和平台上的东西比对。没有它,只能靠人回忆当时发了什么。
    """
    product = _product(session)
    listing = _listing(session, product)
    attempt = PublishAttempt(
        channel_listing_id=listing.id,
        operation=PublishOperation.CREATE.value,
        idempotency_key=_key(product),
        status=PublishAttemptStatus.UNKNOWN.value,
        safe_request_snapshot={"title": "bikini", "token": "***"},
        error_code="TIMEOUT",
    )
    session.add(attempt)
    session.flush()
    session.refresh(attempt)
    assert attempt.safe_request_snapshot["token"] == "***", "快照必须是脱敏后的"
    assert attempt.status == PublishAttemptStatus.UNKNOWN.value


# ---------------------------------------------------------------- PublishOutbox


def test_outbox_row_defaults_are_ready_to_schedule(session):
    product = _product(session)
    listing = _listing(session, product)
    attempt = PublishAttempt(
        channel_listing_id=listing.id,
        operation=PublishOperation.CREATE.value,
        idempotency_key=_key(product),
    )
    session.add(attempt)
    session.flush()

    row = PublishOutbox(publish_attempt_id=attempt.id)
    session.add(row)
    session.flush()
    assert row.status == PublishOutboxStatus.PENDING.value
    assert row.attempt_count == 0
    assert row.lease_until is None, "没人领的行,租约必须为空"


def test_one_outbox_row_per_attempt(session):
    """一次尝试只排一次队。两行 outbox 指向同一次尝试 = 发两遍。"""
    product = _product(session)
    listing = _listing(session, product)
    attempt = PublishAttempt(
        channel_listing_id=listing.id,
        operation=PublishOperation.CREATE.value,
        idempotency_key=_key(product),
    )
    session.add(attempt)
    session.flush()

    session.add(PublishOutbox(publish_attempt_id=attempt.id))
    session.add(PublishOutbox(publish_attempt_id=attempt.id))
    with pytest.raises(IntegrityError):
        session.flush()


def test_outbox_requires_a_real_attempt(session):
    """孤儿 outbox 行会被调度器取出来,然后找不到要发什么。"""
    session.add(PublishOutbox(publish_attempt_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        session.flush()
