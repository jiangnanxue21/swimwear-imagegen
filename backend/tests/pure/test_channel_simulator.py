"""渠道 API Simulator 的九种行为,以及 `build_request`(任务 12 / 13)。

九种行为逐条都有用例 —— 方案 4.1 节 B 把它们列成清单,清单就该逐条对得上,
而不是"跑通了一条主路径然后相信其余八条也行"。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.channels.base import ChannelContext, ChannelListingRef, PreparedRequest
from app.channels.simulator import (
    CHANNEL,
    EXTERNAL_ID_PREFIX,
    REVIEW_SECONDS,
    SimScenario,
    SimulatorError,
    make_external_id,
    parse_external_id,
    pick_scenario,
    poll,
    simulate_poll,
    simulate_submit,
    submit,
)
from app.core.enums import PublishOperation, PublishStatus, RejectCategory
from tests.pure._helpers import expect_raises

T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

BODY = {
    "header": {"spu": "SW-001", "title": "Bikini", "price": "19.90"},
    "rows": [{"sku": "SW-001-BLK-S"}, {"sku": "SW-001-BLK-M"}],
}


def _submit(spu="SW-001", operation=PublishOperation.CREATE.value, key="pub-abc", **kw):
    body = {**BODY, "header": {**BODY["header"], "spu": spu}}
    return simulate_submit(
        spu=spu, operation=operation, idempotency_key=key, body=body, now=T0, **kw
    )


# ---------------------------------------------------------------- 场景选择


def test_default_path_is_success_not_failure():
    """默认失败的 Simulator 会让所有人第一次接入就卡住,然后怀疑自己的代码。"""
    assert pick_scenario(spu="SW-001", operation="CREATE") is SimScenario.CREATE_OK
    assert pick_scenario(spu="SW-001", operation="UPDATE") is SimScenario.UPDATE_OK


def test_spu_prefix_triggers_a_scenario_without_touching_config():
    """人工测试者建一个叫 SIM-RATELIMIT-001 的商品就能验限流,不必改配置重启。"""
    assert pick_scenario(spu="SIM-RATELIMIT-001", operation="CREATE") is SimScenario.RATE_LIMITED
    assert pick_scenario(spu="sim-reject-9", operation="CREATE") is SimScenario.PLATFORM_REJECTED


def test_explicit_override_beats_the_spu_prefix():
    picked = pick_scenario(spu="SIM-RATELIMIT-1", operation="CREATE", override="AUTH_FAILED")
    assert picked is SimScenario.AUTH_FAILED


def test_unknown_scenario_is_refused():
    expect_raises(SimulatorError, pick_scenario, spu="X", operation="CREATE", override="NOPE")


def test_scenario_choice_is_deterministic():
    """没有随机。一个随机失败的 Simulator 会让「这次红是代码问题还是抽到了失败」
    变成每天都要回答的问题。
    """
    picks = {pick_scenario(spu="SW-9", operation="CREATE") for _ in range(50)}
    assert len(picks) == 1


# ---------------------------------------------------------------- 九种行为


def test_1_create_returns_external_ids():
    r = _submit()
    assert r.status_code == 201 and r.ok
    assert r.body["external_spu_id"].startswith(EXTERNAL_ID_PREFIX)
    assert set(r.body["external_sku_ids"]) == {"SW-001-BLK-S", "SW-001-BLK-M"}
    assert r.publish_status == PublishStatus.LISTED.value


def test_2_update_is_a_different_route_and_status():
    r = _submit(operation=PublishOperation.UPDATE.value)
    assert r.status_code == 200 and r.ok
    assert r.body["operation"] == PublishOperation.UPDATE.value


def test_3_idempotent_conflict_hands_back_the_existing_resource():
    """409 是好消息:上一次其实成功了。不带回既有 ID 的话,调用方只能
    重试或人工介入,而两者都是错的。
    """
    r = _submit(spu="SIM-CONFLICT-1")
    assert r.status_code == 409
    assert r.body["external_spu_id"].startswith(EXTERNAL_ID_PREFIX)
    assert not r.retriable, "重发同一个键还是 409,只会死循环"


def test_3b_the_same_key_always_maps_to_the_same_resource():
    a = _submit(spu="SIM-CONFLICT-1", key="pub-same")
    b = _submit(spu="SIM-CONFLICT-1", key="pub-same")
    assert a.body["external_spu_id"] == b.body["external_spu_id"]


def test_4_rate_limited_carries_retry_after_and_does_not_move_local_state():
    r = _submit(spu="SIM-RATELIMIT-1")
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert r.retriable
    assert r.publish_status is None, "这次没送到,商品还在排队,不该标成失败"


def test_5_auth_failure_is_not_retriable():
    r = _submit(spu="SIM-AUTH-1")
    assert r.status_code == 401
    assert not r.retriable, "凭证不改,重发一万次还是同一个结果"
    assert r.publish_status == PublishStatus.SUBMIT_FAILED.value


def test_6_field_rejection_names_fields_that_really_exist_in_the_payload():
    """随便返回一个 field: "foo",驳回回流会因为找不到对应字段而静默丢弃 ——
    而那正是这个场景要验的东西。
    """
    r = _submit(spu="SIM-FIELD-1")
    assert r.status_code == 422
    named = {e["field"] for e in r.body["field_errors"]}
    assert named and named <= set(BODY["header"])
    assert r.publish_status == PublishStatus.VALIDATION_FAILED.value


def test_7_async_review_accepts_then_stays_reviewing():
    r = _submit(spu="SIM-REVIEW-1")
    assert r.status_code == 202
    assert r.publish_status == PublishStatus.PLATFORM_REVIEWING.value

    still = simulate_poll(external_spu_id=r.body["external_spu_id"], now=T0 + timedelta(seconds=5))
    assert still.publish_status == PublishStatus.PLATFORM_REVIEWING.value
    assert still.body["seconds_remaining"] > 0


def test_8_published_goes_live_after_the_review_window():
    r = _submit(spu="SIM-LIVE-1")
    later = T0 + timedelta(seconds=REVIEW_SECONDS + 1)
    done = simulate_poll(external_spu_id=r.body["external_spu_id"], now=later)
    assert done.publish_status == PublishStatus.LISTED.value


def test_9_platform_rejection_carries_a_category_that_maps_to_a_repair_tab():
    """驳回类目对齐 RejectCategory,驳回回流才能分诊到"该去哪个标签页改",
    而不是丢给运营一句平台原话。
    """
    r = _submit(spu="SIM-REJECT-1")
    later = T0 + timedelta(seconds=REVIEW_SECONDS + 1)
    done = simulate_poll(external_spu_id=r.body["external_spu_id"], now=later)
    assert done.publish_status == PublishStatus.PLATFORM_REJECTED.value
    assert done.body["reject_category"] == RejectCategory.IMAGE.value
    assert done.body["reject_code"] and done.body["reject_message"]


# ---------------------------------------------------------------- 无状态


def test_external_id_round_trips_scenario_and_creation_time():
    """状态编码在 ID 里,所以任何进程、任何时刻查同一个 ID 都得到一致答案。"""
    eid = make_external_id(scenario=SimScenario.ASYNC_REVIEW, spu="SW-1", now=T0)
    scenario, created = parse_external_id(eid)
    assert scenario is SimScenario.ASYNC_REVIEW
    assert int(created.timestamp()) == int(T0.timestamp())


def test_a_foreign_external_id_is_refused_not_defaulted():
    """把真实平台的 ID 传进 Simulator 必须当场停住 —— 降级成默认场景
    会让一次配置错误表现为"平台状态莫名其妙变成了 LISTED"。
    """
    for bad in ("SHEIN-9981", "", "SIM-oops", "SIM-CREATE_OK-notanumber-abcd1234"):
        expect_raises(SimulatorError, parse_external_id, bad)


def test_polling_is_stable_across_repeated_calls():
    r = _submit(spu="SIM-REVIEW-1")
    at = T0 + timedelta(seconds=10)
    first = simulate_poll(external_spu_id=r.body["external_spu_id"], now=at)
    second = simulate_poll(external_spu_id=r.body["external_spu_id"], now=at)
    assert first.publish_status == second.publish_status


# ---------------------------------------------------------------- Adapter 层


def _ctx(**kw) -> ChannelContext:
    base = dict(channel=CHANNEL, shop_id="shop-1", site="MAIN", application_id="app-1")
    base.update(kw)
    return ChannelContext(**base)


def test_dry_run_never_reaches_the_simulator():
    """干跑的定义是"构造了报文但不发出去"。交给 Simulator 模拟的话,
    就没有任何一条路径在验证"我们真的没发"。
    """
    req = PreparedRequest(method="POST", path="/v1/listings", body=BODY)
    result = submit(req, _ctx(dry_run=True))
    assert result.accepted
    assert result.external_spu_id is None
    assert result.platform_status == PublishStatus.DRY_RUN_COMPLETED.value


def test_conflict_counts_as_accepted_at_the_adapter_layer():
    req = PreparedRequest(
        method="POST",
        path="/v1/listings",
        body={**BODY, "header": {**BODY["header"], "spu": "SIM-CONFLICT-2"}},
        idempotency_key="pub-x",
    )
    result = submit(req, _ctx(dry_run=False))
    assert result.accepted, "判成失败会导致重试,而重试仍然是 409 —— 死循环"
    assert result.external_spu_id


def test_scenario_can_be_forced_through_context_options():
    req = PreparedRequest(method="POST", path="/v1/listings", body=BODY, idempotency_key="k")
    result = submit(req, _ctx(dry_run=False, options={"scenario": "AUTH_FAILED"}))
    assert not result.accepted
    assert result.platform_status == PublishStatus.SUBMIT_FAILED.value


def test_polling_without_an_external_id_is_a_bug_not_a_status():
    """上游把"提交了"和"提交成功了"混同了。返回一个状态会掩盖它。"""
    ref = ChannelListingRef(channel_listing_id="cl-1", channel=CHANNEL, shop_id="s", spu="SW-1")
    expect_raises(SimulatorError, poll, ref, _ctx(dry_run=False))
