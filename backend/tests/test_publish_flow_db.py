"""幂等创建、更新与 Outbox 投递(任务 14)。需要真实 PostgreSQL。

判定层的九种响应形状在 `tests/pure/test_publish_policy.py` 里逐条穷举过了。
这一批验的是**另一件事**:那些判定落到三张表上之后,库里是不是真的对。
两者不能互相替代 —— 判定对而落库错的典型表现是「状态是 LISTED 但
external_spu_id 是空的」,那种行只有在真库上跑一遍才看得见。

不放进 `tests/pure/` 的理由同 `test_publishing_db.py`:唯一索引、外键、
默认值在 ORM 声明里看得见,但「声明了」和「库里真的建出来了」是两件事。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.channels import generic
from app.core.enums import (
    DraftStatus,
    PublishAttemptStatus,
    PublishOperation,
    PublishOutboxStatus,
    PublishStatus,
)
from app.core.errors import ErrorCode, ValidationError
from app.models.listing_copy import ListingDraft
from app.models.listing_image import ListingImageSet
from app.models.product import Product
from app.models.publishing import ChannelListing, PublishAttempt, PublishOutbox
from app.services import publish_service as svc
from tests.conftest import requires_db

pytestmark = requires_db


class _NoCloseSession:
    """让服务层的 `session.close()` 不要拆掉用例的夹具会话。

    服务层每段事务自己开会话、自己关 —— 那是生产环境该有的行为。用例里
    只有一个绑在外层事务上的会话,关掉它整个用例就没法继续断言了。
    代理掉 `close()` 而不是改服务层:改服务层等于为了测试放弃真实的
    会话生命周期,而那正是这一批要验的东西之一。
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        return None


def _factory(session):
    return lambda: _NoCloseSession(session)


def _product(session, *, sku: str = "SW-PUB-14", spu: str = "SW-014") -> Product:
    row = Product(spu=spu, sku=sku, name="测试泳衣")
    session.add(row)
    session.flush()
    return row


def _next_image_set(session, spu: str) -> ListingImageSet:
    """这个 SPU 的下一版图片集(默认集,channel/site 都是 NULL)。

    不能每次都新建一个 `version=1` 的集:`uq_listing_image_sets_version_coalesced`
    把 NULL 折成空串之后,`(spu, '', '', 1)` 全库只允许一份。而
    「创建之后」那一节的每条用例都要调两次 `_draft()`——「运营改了草稿再提交
    一次」正是它们要验的语义——于是第二次必然撞 UniqueViolation,那三条用例
    从落地那天起一次也没跑到过断言。

    版本号按生产的规则算:`image_set_rules.next_version()` 是同 scope 取
    max+1,`derived_from` 指向上一版。照抄这条规则而不是随手 +1,是因为
    图片集不可原地修改——「平台驳回的是哪一版图」就靠这条链回溯,夹具造出
    一个真实系统里不会出现的形状,等于把这批用例验的东西换掉了。
    """
    previous = session.scalars(
        select(ListingImageSet)
        .where(
            ListingImageSet.spu == spu,
            ListingImageSet.channel.is_(None),
            ListingImageSet.site.is_(None),
        )
        .order_by(ListingImageSet.version.desc())
    ).first()

    row = ListingImageSet(
        spu=spu,
        version=(previous.version + 1) if previous is not None else 1,
        derived_from=previous.id if previous is not None else None,
    )
    session.add(row)
    session.flush()
    return row


def _draft(
    session,
    product: Product,
    *,
    spu_in_payload: str | None = None,
    status: str = DraftStatus.VALIDATED.value,
    fingerprint: str = "fp-1",
) -> ListingDraft:
    """一份可提交的草稿。

    `spu_in_payload` 单独给,是因为 Simulator 按**报文里的 spu 前缀**选场景
    (`SIM-RATELIMIT-001` 触发限流)。让它和 `Product.spu` 解耦,用例就能在
    不污染商品数据的前提下挑场景。
    """
    image_set = _next_image_set(session, product.spu)

    draft = ListingDraft(
        spu=product.spu,
        channel=generic.CHANNEL,
        site=generic.SITE,
        category_id=generic.CATEGORY_ID,
        status=status,
        image_set_id=image_set.id,
        source_fingerprint=fingerprint,
        spec_version="1",
        mapped_payload={
            "header": {"spu": spu_in_payload or product.spu, "title": "Bikini"},
            "rows": [{"sku": product.sku}],
        },
    )
    session.add(draft)
    session.flush()
    return draft


def _enqueue(session, product, draft, **kw):
    return svc.enqueue(
        session, product=product, draft=draft, shop_id="shop-test", actor="tester", **kw
    )


# ---------------------------------------------------------------- 排队


def test_enqueue_writes_all_three_tables_in_one_business_transaction(session):
    product = _product(session)
    draft = _draft(session, product)

    result = _enqueue(session, product, draft)

    assert result.operation is PublishOperation.CREATE
    assert result.listing.status == PublishStatus.SUBMITTING.value
    assert result.attempt.status == PublishAttemptStatus.PENDING.value
    assert result.outbox.status == PublishOutboxStatus.PENDING.value
    # 幂等键落在尝试上,而不是只活在内存里
    assert result.attempt.idempotency_key == result.idempotency_key


def test_the_request_snapshot_is_stored_and_carries_no_credentials(session):
    """4.1 节 D:请求和响应脱敏审计。列名带 safe_ 前缀就是为了让塞原文显得不对劲。"""
    product = _product(session)
    draft = _draft(session, product)
    result = _enqueue(session, product, draft)

    snapshot = result.attempt.safe_request_snapshot
    assert snapshot["method"] == "POST"
    assert snapshot["body"]["header"]["spu"] == product.spu
    assert "authorization" not in {k.lower() for k in snapshot["headers"]}


def test_pressing_submit_twice_does_not_create_a_second_attempt(session):
    """4.1 节 D:重试不会重复创建。运营连点两次,平台只收到一次。"""
    product = _product(session)
    draft = _draft(session, product)

    first = _enqueue(session, product, draft)
    second = _enqueue(session, product, draft)

    assert second.reused is True
    assert second.attempt.id == first.attempt.id
    assert session.query(PublishAttempt).count() == 1
    assert session.query(PublishOutbox).count() == 1


def test_a_dry_run_produces_a_request_but_never_queues_it(session):
    """干跑的定义是「构造了报文但不发出去」。"""
    product = _product(session)
    draft = _draft(session, product)

    result = _enqueue(session, product, draft, dry_run=True)

    assert result.dry_run is True
    assert result.attempt is None
    assert result.prepared.path == "/v1/listings"
    assert result.prepared.idempotency_key
    assert session.query(PublishOutbox).count() == 0
    assert result.listing.status == PublishStatus.DRY_RUN_COMPLETED.value
    assert result.listing.external_spu_id is None


def test_a_stale_draft_is_refused_before_anything_is_written(session):
    """STALE 草稿会把旧内容发上去,而页面上显示的是新内容。"""
    product = _product(session)
    draft = _draft(session, product, status=DraftStatus.STALE.value)

    with pytest.raises(ValidationError) as exc:
        _enqueue(session, product, draft)

    assert exc.value.code is ErrorCode.DRAFT_STALE
    assert session.query(ChannelListing).count() == 0


def test_one_listing_row_per_channel_shop_site_product(session):
    """第二次提交沿用同一行 —— 两行的话「这个商品上架了吗」就没有答案。"""
    product = _product(session)
    draft = _draft(session, product)

    first = _enqueue(session, product, draft)
    second = _enqueue(session, product, draft)

    assert first.listing.id == second.listing.id
    assert session.query(ChannelListing).count() == 1


# ---------------------------------------------------------------- 投递


def test_a_successful_create_lands_the_external_id(session):
    """外部商品 ID 落库 —— 4.1 节 H 的清理清单靠它列出本轮创建了哪些商品。"""
    product = _product(session)
    draft = _draft(session, product)
    result = _enqueue(session, product, draft)
    session.flush()

    stats = svc.run_due(_factory(session))

    assert stats["claimed"] == 1
    assert stats["succeeded"] == 1
    session.refresh(result.listing)
    session.refresh(result.outbox)
    assert result.listing.external_spu_id.startswith("SIM-")
    assert result.listing.status == PublishStatus.LISTED.value
    assert result.listing.last_platform_status == "LISTED"
    assert result.outbox.status == PublishOutboxStatus.DONE.value


def test_rate_limiting_reschedules_without_touching_the_listing(session):
    """429:这次没送到,商品还在排队。标成失败会让运营以为要人工处理。"""
    product = _product(session)
    draft = _draft(session, product, spu_in_payload="SIM-RATELIMIT-001")
    result = _enqueue(session, product, draft)
    session.flush()

    stats = svc.run_due(_factory(session))

    assert stats["retry"] == 1
    session.refresh(result.listing)
    session.refresh(result.outbox)
    assert result.listing.status == PublishStatus.SUBMITTING.value
    assert result.listing.external_spu_id is None
    assert result.outbox.status == PublishOutboxStatus.PENDING.value
    assert result.outbox.next_attempt_at is not None
    assert result.outbox.attempt_count == 1


def test_auth_failure_stops_instead_of_hammering_the_platform(session):
    """凭证不改,重发一万次还是同一个结果,而每次都在给风控加分。"""
    product = _product(session)
    draft = _draft(session, product, spu_in_payload="SIM-AUTH-001")
    result = _enqueue(session, product, draft)
    session.flush()

    stats = svc.run_due(_factory(session))

    assert stats["failed"] == 1
    session.refresh(result.listing)
    session.refresh(result.outbox)
    assert result.listing.status == PublishStatus.SUBMIT_FAILED.value
    assert result.listing.external_spu_id is None
    assert result.outbox.status == PublishOutboxStatus.DEAD.value


def test_a_failed_publish_never_reads_as_success_locally(session):
    """4.1 节 D 最后一条。失败之后本地必须看得出来是失败。"""
    product = _product(session)
    draft = _draft(session, product, spu_in_payload="SIM-FIELD-001")
    result = _enqueue(session, product, draft)
    session.flush()

    svc.run_due(_factory(session))

    session.refresh(result.listing)
    assert result.listing.status == PublishStatus.VALIDATION_FAILED.value
    assert result.listing.external_spu_id is None
    session.refresh(result.attempt)
    assert result.attempt.status == PublishAttemptStatus.FAILED.value
    assert result.attempt.finished_at is not None


def test_an_idempotency_conflict_is_recorded_as_success(session):
    """409 是好消息:平台把上一次创建的资源还给我们了。"""
    product = _product(session)
    draft = _draft(session, product, spu_in_payload="SIM-CONFLICT-001")
    result = _enqueue(session, product, draft)
    session.flush()

    stats = svc.run_due(_factory(session))

    assert stats["succeeded"] == 1
    session.refresh(result.listing)
    assert result.listing.external_spu_id is not None
    session.refresh(result.attempt)
    assert result.attempt.status == PublishAttemptStatus.SUCCEEDED.value


def test_async_review_does_not_claim_the_product_is_live(session):
    """「提交成功」不等于「上架成功」。"""
    product = _product(session)
    draft = _draft(session, product, spu_in_payload="SIM-REVIEW-001")
    result = _enqueue(session, product, draft)
    session.flush()

    svc.run_due(_factory(session))

    session.refresh(result.listing)
    assert result.listing.status == PublishStatus.PLATFORM_REVIEWING.value
    assert result.listing.last_platform_status == "REVIEWING"
    assert result.listing.external_spu_id is not None


def test_nothing_is_claimed_twice_in_one_pass(session):
    """领走的行带租约,同一轮扫描不会把它再发一次。"""
    product = _product(session)
    draft = _draft(session, product)
    _enqueue(session, product, draft)
    session.flush()

    first = svc.run_due(_factory(session))
    second = svc.run_due(_factory(session))

    assert first["claimed"] == 1
    assert second["claimed"] == 0


# ---------------------------------------------------------------- 创建之后


def test_the_second_submission_becomes_an_update_with_a_different_key(session):
    """7.4 节:创建和更新不能共用同一个键。"""
    product = _product(session)
    draft = _draft(session, product)
    created = _enqueue(session, product, draft)
    session.flush()
    svc.run_due(_factory(session))
    session.refresh(created.listing)

    updated = _draft(session, product, fingerprint="fp-2")
    second = _enqueue(session, product, updated)

    assert second.operation is PublishOperation.UPDATE
    assert second.idempotency_key != created.idempotency_key
    assert second.listing.status == PublishStatus.UPDATING.value
    assert second.prepared.path.endswith(created.listing.external_spu_id)


def test_an_update_response_never_overwrites_the_known_external_id(session):
    """覆盖之后,平台上那个真正存在的商品就从我们的清理清单里消失了(4.1 节 H)。

    Simulator 每次 UPDATE 都会生成一个新的 `SIM-...` ID(它的 ID 里编码了
    时刻),所以这条用例真的会走到那个分支 —— 不是一条假想的防御。
    """
    product = _product(session)
    created = _enqueue(session, product, _draft(session, product))
    session.flush()
    svc.run_due(_factory(session))
    session.refresh(created.listing)
    original = created.listing.external_spu_id

    _enqueue(session, product, _draft(session, product, fingerprint="fp-update"))
    session.flush()
    svc.run_due(_factory(session))

    session.refresh(created.listing)
    assert created.listing.external_spu_id == original


def test_asking_to_create_something_that_already_exists_is_a_conflict(session):
    """已有 external_spu_id 却试图 CREATE —— `PUBLISH_DUPLICATE_CREATE` 的定义。"""
    product = _product(session)
    draft = _draft(session, product)
    _enqueue(session, product, draft)
    session.flush()
    svc.run_due(_factory(session))

    with pytest.raises(ValidationError) as exc:
        _enqueue(
            session,
            product,
            _draft(session, product, fingerprint="fp-3"),
            requested_operation=PublishOperation.CREATE,
        )

    assert exc.value.code is ErrorCode.PUBLISH_DUPLICATE_CREATE


# ---------------------------------------------------------------- 未知结果


def test_an_unknown_result_can_be_confirmed_with_the_same_key(session):
    """7.4 节:不得立即重新创建,先用幂等键确认。

    确认走的是**同一个键**,所以 Simulator 那边看到的是一次重复提交。
    这条路径存在的意义就是它不会造出第二个商品。
    """
    product = _product(session)
    draft = _draft(session, product)
    result = _enqueue(session, product, draft)
    session.flush()

    # 手工把它拨到「结果未知」——真实触发要靠网络中断,那不适合放进用例
    result.attempt.status = PublishAttemptStatus.UNKNOWN.value
    result.listing.status = PublishStatus.SUBMIT_RESULT_UNKNOWN.value
    result.outbox.status = PublishOutboxStatus.DEAD.value
    session.flush()

    svc.reconcile_unknown(_factory(session), result.attempt.id)

    session.refresh(result.attempt)
    session.refresh(result.listing)
    assert result.attempt.status == PublishAttemptStatus.SUCCEEDED.value
    assert result.listing.external_spu_id is not None
    assert session.query(PublishAttempt).count() == 1


def test_reconcile_refuses_anything_that_is_not_actually_unknown(session):
    """对一条已经有结论的尝试做「确认」,只会再发一次请求。"""
    product = _product(session)
    draft = _draft(session, product)
    result = _enqueue(session, product, draft)
    session.flush()

    with pytest.raises(ValidationError):
        svc.reconcile_unknown(_factory(session), result.attempt.id)
