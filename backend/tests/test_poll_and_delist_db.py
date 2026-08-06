"""轮询、驳回回流、下架与清理的落库验证(任务 15 / 16)。需要真实 PostgreSQL。

判定层的每一种平台回答在 `tests/pure/test_poll_policy.py` 里逐条穷举过了。
这一批验的是**另一件事**:那些判定落到 `channel_listings` 与
`platform_rejections` 上之后,库里是不是真的对。

两者不能互相替代。判定对而落库错的典型表现是「平台驳回了,状态也改成
PLATFORM_REJECTED 了,但台账里一条记录都没有」—— 工作台的阻断计数是从台账
数的,于是那个商品在界面上看起来毫无问题,而它其实卡死了。那种错只有在
真库上跑一遍才看得见。

夹具助手直接从 `test_publish_flow_db` 引进来:同一条链路的上半段在那边已经
建好了,复制一份的话,两边对"一份可提交的草稿长什么样"的理解会慢慢分叉,
而分叉之后这一批测的就不再是真实系统里会出现的状态。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.channels import simulator
from app.core.enums import DraftStatus, PublishOperation, PublishStatus, RejectionStatus
from app.core.errors import ValidationError
from app.models.platform_rejection import PlatformRejection
from app.models.publishing import ChannelListing, PublishOutbox
from app.services import cleanup_service, poll_service
from app.services import publish_service as svc
from app.workbench import platform_service
from app.workflows import poll_policy
from tests.conftest import requires_db
from tests.test_publish_flow_db import _draft, _factory, _product

pytestmark = requires_db

SHOP = "shop-cleanup"


def _live(session, *, sku: str, spu: str, trigger: str, tag: str | None = None):
    """走完整的提交路径,拿到一个已经在平台上的 listing。

    `trigger` 是报文里的 SPU 前缀,Simulator 靠它选场景 —— 于是"异步审核"
    和"平台驳回"这两条分支可以被点名跑到,而不是碰运气。

    不手工造 ChannelListing 行:手工造的行会绕过 `enqueue` 的全部约束,
    那样测试验的是一个真实系统里不存在的状态。
    """
    product = _product(session, sku=sku, spu=spu)
    draft = _draft(session, product, spu_in_payload=trigger)
    svc.enqueue(
        session,
        product=product,
        draft=draft,
        shop_id=SHOP,
        actor="tester",
        test_batch_tag=tag,
    )
    svc.run_due(_factory(session), limit=10)
    listing = session.query(ChannelListing).filter_by(product_id=product.id).one()
    session.refresh(listing)
    return product, draft, listing


def _later(seconds: int = simulator.REVIEW_SECONDS + 5) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


# ---------------------------------------------------------------- 领取


def test_a_listing_awaiting_review_is_claimed(session):
    _, _, listing = _live(session, sku="SW-P-1", spu="SW-P-1", trigger="SIM-REVIEW-1")

    assert listing.status == PublishStatus.PLATFORM_REVIEWING.value
    claimed = poll_service.claim_pollable(session, limit=10)
    assert listing.id in {c.id for c in claimed}


def test_claiming_pushes_the_next_poll_time_forward_before_asking(session):
    """租约就是 `next_poll_at`(见 poll_service 模块顶部)。

    推进发生在问平台之前,所以领完立刻就该看见它 —— 不需要等结果回来。
    """
    _, _, listing = _live(session, sku="SW-P-2", spu="SW-P-2", trigger="SIM-REVIEW-2")
    now = datetime.utcnow()

    rows = poll_service.claim_pollable(session, limit=10, now=now)
    probe = poll_service._reserve(session, rows[0], now=now)
    session.flush()

    assert probe is not None
    assert rows[0].next_poll_at > now
    assert rows[0].poll_attempt_count == 1


def test_a_listing_without_an_external_id_is_never_claimed(session):
    """没有可问的对象。留在候选集里只会让日志堆满 404,
    而真正的 404(商品在平台上消失了)从此没人看得见。
    """
    _, _, listing = _live(session, sku="SW-P-3", spu="SW-P-3", trigger="SIM-REVIEW-3")
    listing.external_spu_id = None
    session.flush()

    assert listing.id not in {c.id for c in poll_service.claim_pollable(session, limit=10)}


def test_a_listed_product_is_not_polled_on_a_schedule(session):
    """LISTED 不在轮询范围里 —— 理由写在 POLLABLE_STATUSES 的注释里。

    4.1 节 H 的清单核对要问它们,但那条路径走 `poll_one`,显式绕开这张表。
    """
    _, _, listing = _live(session, sku="SW-P-4", spu="SW-P-4", trigger="SIM-LIVE-4")
    listing.status = PublishStatus.LISTED.value
    session.flush()

    assert listing.id not in {c.id for c in poll_service.claim_pollable(session, limit=10)}


# ---------------------------------------------------------------- 收敛


def test_polling_moves_the_listing_when_review_finishes(session):
    """异步审核的收敛:提交成功 ≠ 上架成功,这一条把后半段跑通。"""
    _, _, listing = _live(session, sku="SW-P-5", spu="SW-P-5", trigger="SIM-LIVE-5")
    assert listing.status == PublishStatus.PLATFORM_REVIEWING.value

    stats = poll_service.poll_due(
        _factory(session), limit=10, options={"now": _later()}
    )

    session.refresh(listing)
    assert stats["claimed"] == 1
    assert listing.status == PublishStatus.LISTED.value
    assert listing.last_platform_status == "LISTED"
    assert listing.last_polled_at is not None


def test_a_poll_before_the_review_window_changes_nothing_but_still_backs_off(session):
    """平台说"还在审",这不是错误,但也不是进展。

    状态不动,退避继续增长 —— 一个审了两小时的商品会被越问越稀。
    """
    _, _, listing = _live(session, sku="SW-P-6", spu="SW-P-6", trigger="SIM-REVIEW-6")

    stats = poll_service.poll_due(_factory(session), limit=10)

    session.refresh(listing)
    assert stats["unchanged"] == 1
    assert listing.status == PublishStatus.PLATFORM_REVIEWING.value
    assert listing.poll_attempt_count == 1
    assert listing.next_poll_at is not None


def test_an_unanswered_poll_leaves_last_polled_at_alone(session):
    """那一列的含义是「我们对平台的了解是什么时候的」。

    一次失败的轮询没有更新任何了解。写上去的话界面会显示"刚刚同步过",
    而实际上已经连不上平台好几个小时了 —— 最坏的一种错误:它让人不去查。
    """
    _, _, listing = _live(session, sku="SW-P-7", spu="SW-P-7", trigger="SIM-REVIEW-7")
    before = listing.last_polled_at

    bucket = poll_service.apply_poll_outcome(
        session, listing=listing, outcome=poll_policy.transport_failure()
    )

    assert bucket == "unanswered"
    assert listing.last_polled_at == before


def test_a_404_never_writes_delisted(session):
    """查不到 ≠ 已下架。

    猜成"已下架"的代价是:一个仍然挂在平台上的商品从清理清单里消失,
    而那正是 4.1 节 H 要防的结局本身。
    """
    _, _, listing = _live(session, sku="SW-P-8", spu="SW-P-8", trigger="SIM-REVIEW-8")
    original = listing.status

    poll_service.apply_poll_outcome(
        session,
        listing=listing,
        outcome=poll_policy.classify_poll(status_code=404, body={}),
    )

    assert listing.status == original
    assert listing.status != PublishStatus.DELISTED.value


def test_a_status_change_resets_the_backoff(session):
    """平台动了,接下来很可能还会再动(REVIEWING -> LISTED 常常紧接着发生)。"""
    _, _, listing = _live(session, sku="SW-P-9", spu="SW-P-9", trigger="SIM-REVIEW-9")
    listing.poll_attempt_count = 7
    session.flush()

    poll_service.apply_poll_outcome(
        session,
        listing=listing,
        outcome=poll_policy.classify_poll(
            status_code=200, body={"platform_status": "LISTED"}
        ),
    )

    assert listing.status == PublishStatus.LISTED.value
    assert listing.poll_attempt_count == 0


# ---------------------------------------------------------------- 驳回回流


def _reject(session, listing, *, category: str = "IMAGE", code: str = "IMG_001"):
    outcome = poll_policy.classify_poll(
        status_code=200,
        body={
            "platform_status": "REJECTED",
            "reject_category": category,
            "reject_code": code,
            "reject_message": "主图背景不是纯白",
        },
    )
    return poll_service.apply_poll_outcome(session, listing=listing, outcome=outcome)


def test_a_platform_rejection_lands_in_the_existing_ledger(session):
    """落点是既有的 platform_rejections 表,不是一张新表 ——
    所以工作台不用改一行就能看见它、把它计进阻断项。
    """
    _, draft, listing = _live(session, sku="SW-R-1", spu="SW-R-1", trigger="SIM-REJECT-1")

    assert _reject(session, listing) == "rejected"

    row = session.query(PlatformRejection).filter_by(draft_id=draft.id).one()
    assert row.status == RejectionStatus.OPEN.value
    assert "IMG_001" in (row.platform_note or "")
    assert "主图背景不是纯白" in (row.platform_note or "")


def test_the_rejection_records_how_the_version_was_located(session):
    """API 上架的商品可能一次 Excel 都没导过,前三种把握一种都用不上。

    如实标成 publish_attempt,不冒充 audit —— 硬凑一个版本号比承认定位不到
    更糟:运营会按错的版本去改图。
    """
    _, draft, listing = _live(session, sku="SW-R-2", spu="SW-R-2", trigger="SIM-REJECT-2")

    _reject(session, listing)

    row = session.query(PlatformRejection).filter_by(draft_id=draft.id).one()
    assert row.located_by == "publish_attempt"
    # 草稿指纹进过幂等键,版本靠它钉住
    assert row.export_fingerprint == draft.source_fingerprint


def test_a_rejection_recorded_by_polling_needs_no_excel_export(session):
    """整个任务 15 里最容易被漏掉的一条。

    手工录入那条路径走 `locate_export()`,找不到导出记录就抛 409 ——
    照搬过去的后果是驳回**恰恰在最该被记下来的时候**记不进台账。
    这条用例的商品从头到尾没有任何导出记录。

    「没有导出记录」不是一张 `listing_exports` 表 —— 全仓没有那张表。导出是
    写进审计的:`AuditLog(entity_type="ListingDraft")` 里由
    `platform.export_entry()` 认得出来的那些行,加上草稿自己的 `exported_at`。
    这两样正是 `locate_export()` 仅有的两个输入,所以前置条件必须打在它们上,
    打在别处等于没验前置条件。
    """
    _, draft, listing = _live(session, sku="SW-R-3", spu="SW-R-3", trigger="SIM-REJECT-3")
    assert draft.exported_at is None
    assert platform_service._export_audit_entries(session, draft.id) == []

    _reject(session, listing)

    row = session.query(PlatformRejection).filter_by(draft_id=draft.id).one()
    # 记进去了,而且如实说明版本是怎么定位的:没有导出可指,就指提交尝试。
    # 这一条把「记下来了」和「没冒充成 audit」绑在一起 —— 只断言 count==1
    # 的话,一个把 located_by 硬凑成 audit 的实现也能过。
    assert row.located_by == "publish_attempt"
    assert row.export_at is None


def test_the_same_rejection_is_not_recorded_twice(session):
    """并发的两个 worker 可能同时拿到同一次驳回。

    记两条的后果是运营看到两个待办、改一次、剩一个永远关不掉的幽灵。
    """
    _, draft, listing = _live(session, sku="SW-R-4", spu="SW-R-4", trigger="SIM-REJECT-4")

    _reject(session, listing)
    listing.status = PublishStatus.PLATFORM_REVIEWING.value
    session.flush()
    _reject(session, listing)

    assert session.query(PlatformRejection).filter_by(draft_id=draft.id).count() == 1


def test_a_second_distinct_reason_does_get_its_own_row(session):
    """平台一次驳回多条原因是常事,那些必须各记一条。"""
    _, draft, listing = _live(session, sku="SW-R-5", spu="SW-R-5", trigger="SIM-REJECT-5")

    _reject(session, listing, category="IMAGE", code="IMG_001")
    listing.status = PublishStatus.PLATFORM_REVIEWING.value
    session.flush()
    _reject(session, listing, category="COPY", code="TXT_009")

    assert session.query(PlatformRejection).filter_by(draft_id=draft.id).count() == 2


def test_the_draft_is_marked_rejected_so_the_workbench_blocks_it(session):
    _, draft, listing = _live(session, sku="SW-R-6", spu="SW-R-6", trigger="SIM-REJECT-6")

    _reject(session, listing)

    session.refresh(draft)
    assert draft.platform_status == "REJECTED"


# ---------------------------------------------------------------- 下架


def test_delist_can_be_queued_for_a_stale_draft(session):
    """清理要下架的恰恰是那批草稿早已过期的商品。

    用一条"防止发错内容"的规则去挡一个**根本不发内容**的操作,结果是
    残留在平台上的商品没有任何办法下架。
    """
    product, draft, listing = _live(
        session, sku="SW-D-1", spu="SW-D-1", trigger="SIM-LIVE-D1"
    )
    draft.status = DraftStatus.STALE.value
    session.flush()

    svc.enqueue(
        session,
        product=product,
        draft=draft,
        shop_id=SHOP,
        actor="ops",
        requested_operation=PublishOperation.DELIST,
    )

    assert listing.status == PublishStatus.DELISTING.value


def test_a_delist_request_carries_no_listing_content(session):
    """把一份可能已经过期的 payload 塞进下架请求,平台若照单更新,
    我们就在下架的同时把旧内容发了上去 —— 一次清理变成一次内容回退。
    """
    product, draft, _ = _live(session, sku="SW-D-2", spu="SW-D-2", trigger="SIM-LIVE-D2")

    result = svc.enqueue(
        session,
        product=product,
        draft=draft,
        shop_id=SHOP,
        actor="ops",
        requested_operation=PublishOperation.DELIST,
    )

    snapshot = result.attempt.safe_request_snapshot or {}
    body = dict(snapshot.get("body") or {})
    assert body, "报文快照本身不该是空的 —— 空的是它的内容部分"
    assert not (body.get("header") or {})
    assert not (body.get("rows") or [])
    # 目标仍然在 —— 只是它在**路径**里,不在报文里
    listing = session.query(ChannelListing).filter_by(product_id=product.id).one()
    assert listing.external_spu_id in str(snapshot.get("path") or "")


def test_a_queued_delist_reaches_delisted_after_delivery(session):
    product, draft, listing = _live(
        session, sku="SW-D-3", spu="SW-D-3", trigger="SIM-LIVE-D3"
    )

    svc.enqueue(
        session,
        product=product,
        draft=draft,
        shop_id=SHOP,
        actor="ops",
        requested_operation=PublishOperation.DELIST,
    )
    svc.run_due(_factory(session), limit=10)

    session.refresh(listing)
    assert listing.status == PublishStatus.DELISTED.value


# ---------------------------------------------------------------- 清单


def test_cleanup_refuses_to_run_unscoped(session):
    """一次跑成全量的清理会把生产上所有在架商品下架,而那通常撤不回来。

    这个模块存在的目的是防止事故,它自己不能是事故的来源。
    """
    with pytest.raises(ValidationError):
        cleanup_service.inventory(session)


def test_the_inventory_lists_what_we_created_on_the_platform(session):
    _, _, listing = _live(session, sku="SW-C-1", spu="SW-C-1", trigger="SIM-LIVE-C1")

    report = cleanup_service.inventory(session, shop_id=SHOP)

    assert report.total == 1
    row = report.rows[0]
    assert row.external_spu_id == listing.external_spu_id
    assert row.simulated is True, "Simulator 造的 ID 必须被认出来"
    assert row.spu == "SW-C-1"


def test_simulated_listings_do_not_count_as_uncleaned(session):
    """它们从来没有出现在任何平台上。

    计进"没清干净"的话,这个布尔量在人工测试期间永远是 False,
    然后没人再看它。
    """
    _live(session, sku="SW-C-2", spu="SW-C-2", trigger="SIM-LIVE-C2")

    report = cleanup_service.inventory(session, shop_id=SHOP)

    assert report.simulated == 1
    assert report.real == 0
    assert report.clean is True


def test_a_batch_tag_narrows_the_inventory(session):
    """4.1 节 H 要的是"本轮测试创建了哪些",不是"库里一共有哪些"。"""
    _live(session, sku="SW-C-3", spu="SW-C-3", trigger="SIM-LIVE-C3", tag="uat-2026-08")
    _live(session, sku="SW-C-4", spu="SW-C-4", trigger="SIM-LIVE-C4")

    assert cleanup_service.inventory(session, test_batch_tag="uat-2026-08").total == 1
    assert cleanup_service.inventory(session, test_batch_tag="uat-other").total == 0
    assert cleanup_service.inventory(session, shop_id=SHOP).total == 2


def test_an_untagged_update_does_not_erase_the_batch_tag(session):
    """漏一行,就没人说得清平台上到底还剩几个 —— 4.1 节 H 要防的结局本身。"""
    product, draft, listing = _live(
        session, sku="SW-C-5", spu="SW-C-5", trigger="SIM-LIVE-C5", tag="uat-2026-08"
    )

    svc.enqueue(session, product=product, draft=draft, shop_id=SHOP, actor="tester")

    session.refresh(listing)
    assert listing.test_batch_tag == "uat-2026-08"


def test_a_listing_that_never_reached_the_platform_is_not_on_the_inventory(session):
    """没有外部 ID 就等于那次提交从没到达平台,它不占任何位置。

    列出来只会让清单变长、让真正要核对的行更难看见。
    """
    product = _product(session, sku="SW-C-6", spu="SW-C-6")
    draft = _draft(session, product, spu_in_payload="SIM-AUTH-C6")
    svc.enqueue(session, product=product, draft=draft, shop_id=SHOP, actor="tester")
    svc.run_due(_factory(session), limit=10)

    assert cleanup_service.inventory(session, shop_id=SHOP).total == 0


# ---------------------------------------------------------------- 清理


def test_planning_a_cleanup_queues_nothing(session):
    """"看一眼"和"动手"不共用一条代码路径 ——
    参数是最容易在复制粘贴时被改掉的东西。
    """
    _live(session, sku="SW-X-1", spu="SW-X-1", trigger="SIM-LIVE-X1")
    before = session.query(PublishOutbox).count()

    plan = cleanup_service.plan_delist(session, shop_id=SHOP)

    assert plan.total == 1
    assert session.query(PublishOutbox).count() == before


def test_running_a_cleanup_queues_the_still_listed_ones(session):
    _, _, listing = _live(session, sku="SW-X-2", spu="SW-X-2", trigger="SIM-LIVE-X2")
    listing.status = PublishStatus.LISTED.value
    session.flush()

    result = cleanup_service.run_delist(session, actor="ops", shop_id=SHOP)

    assert len(result["queued"]) == 1
    session.refresh(listing)
    assert listing.status == PublishStatus.DELISTING.value


def test_a_cleanup_run_ends_with_everything_delisted(session):
    """4.1 节 H 的验收:排队 -> 投递 -> 清单上没有一个还占着位置。"""
    _, _, listing = _live(session, sku="SW-X-3", spu="SW-X-3", trigger="SIM-LIVE-X3")
    listing.status = PublishStatus.LISTED.value
    session.flush()

    cleanup_service.run_delist(session, actor="ops", shop_id=SHOP)
    svc.run_due(_factory(session), limit=10)

    session.refresh(listing)
    assert listing.status == PublishStatus.DELISTED.value
    assert cleanup_service.plan_delist(session, shop_id=SHOP).total == 0


def test_a_listing_without_a_draft_is_named_not_silently_dropped(session):
    """一个自动流程处理不了的商品,不许悬空(4.1 节 H 最后一条)。"""
    _, _, listing = _live(session, sku="SW-X-4", spu="SW-X-4", trigger="SIM-LIVE-X4")
    listing.status = PublishStatus.LISTED.value
    listing.draft_id = None
    session.flush()

    plan = cleanup_service.plan_delist(session, shop_id=SHOP)

    assert plan.rows[0].delistable is False
    assert plan.rows[0].reason
    assert plan.not_delistable == 0, "模拟商品不计入真实残留"
