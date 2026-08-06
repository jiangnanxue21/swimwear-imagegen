"""发布幂等键。方案 v4.1 第 7.4 节。

这一批刻意放在 `tests/pure/` 而不是需要真库的那一层:键的推导是纯函数,
而它守的规则(CREATE 和 UPDATE 不能同键)是**只在生产上才会暴露**的那类问题。
纯测试能在任何机器上秒级跑完,包括还没装依赖的新人机器 —— 门禁越靠前越好。
"""
from __future__ import annotations

from app.workflows.publish_idempotency import (
    build_publish_idempotency_key,
    build_request_fingerprint,
)
from tests.pure._helpers import expect_raises

BASE = dict(
    channel="shein",
    shop_id="shop-001",
    site="US",
    operation="CREATE",
    product_id="11111111-1111-1111-1111-111111111111",
    draft_fingerprint="d41d8cd98f00",
    spec_checksum="spec-v3",
)


def test_same_inputs_produce_same_key():
    assert build_publish_idempotency_key(**BASE) == build_publish_idempotency_key(**BASE)


def test_create_and_update_never_share_a_key():
    """这条是本模块存在的理由。

    v1 的键不含 operation,于是同一份草稿的创建和更新算出同一个键 ——
    要么更新被当成重复创建吞掉(改了价格,平台上还是旧的),
    要么创建被当成更新放过去(平台上没这个商品,更新报 404)。
    """
    create = build_publish_idempotency_key(**BASE)
    update = build_publish_idempotency_key(**{**BASE, "operation": "UPDATE"})
    republish = build_publish_idempotency_key(**{**BASE, "operation": "REPUBLISH"})
    delist = build_publish_idempotency_key(**{**BASE, "operation": "DELIST"})
    assert len({create, update, republish, delist}) == 4


def test_missing_operation_is_refused_loudly():
    """漏了 operation 不能静默算出一个「看起来合法」的键。"""
    expect_raises(
        ValueError,
        build_publish_idempotency_key,
        **{**BASE, "operation": ""},
    )


def test_every_scope_field_changes_the_key():
    """渠道、店铺、站点任意一个不同,就是不同的一次提交。

    少哪一个都有具体后果:漏 channel,同一份草稿发两个渠道时第二个被吞;
    漏 shop_id,测试店铺和生产店铺互相幂等掉(4.1 节 H 要求两者凭证分离,
    而幂等键是这个分离在应用层的对应物);漏 site,美国站的提交会挡住欧洲站。
    """
    baseline = build_publish_idempotency_key(**BASE)
    for field, other in (
        ("channel", "generic"),
        ("shop_id", "shop-002"),
        ("site", "DE"),
        ("product_id", "22222222-2222-2222-2222-222222222222"),
        ("draft_fingerprint", "ffffffffffff"),
        ("spec_checksum", "spec-v4"),
    ):
        assert build_publish_idempotency_key(**{**BASE, field: other}) != baseline, field


def test_external_listing_id_separates_relist_from_first_listing():
    """下架后重新上架,不该和第一次上架幂等掉。"""
    first = build_publish_idempotency_key(**BASE)
    relist = build_publish_idempotency_key(**{**BASE, "external_listing_id": "EXT-9"})
    assert first != relist


def test_scope_fields_are_normalised():
    """渠道大小写、站点大小写、两边空格,不该产生两个键。

    这三样在实际调用里来自三个不同的地方(配置文件、前端下拉、平台回包),
    大小写不一致是迟早的事,而它导致的是一次重复创建。
    """
    noisy = {**BASE, "channel": " SHEIN ", "site": "us", "operation": " create "}
    assert build_publish_idempotency_key(**noisy) == build_publish_idempotency_key(**BASE)


def test_explicit_key_wins_and_is_truncated():
    explicit = "x" * 200
    key = build_publish_idempotency_key(**BASE, explicit_key=explicit)
    assert key == "x" * 128


def test_key_carries_a_prefix_that_tells_it_apart_from_generation_keys():
    """审计日志里两种键会并排出现,前缀是唯一能一眼分开它们的东西。"""
    assert build_publish_idempotency_key(**BASE).startswith("pub-")


def test_request_fingerprint_ignores_dict_key_order():
    """payload 是同一份内容,序列化顺序不该让指纹变化。

    否则「超时重试发出去的和上次是不是同一份」永远答否,而那个答案
    决定了要不要进人工异常队列。
    """
    a = {"title": "bikini", "price": 19.9, "skus": [{"id": "s1"}, {"id": "s2"}]}
    b = {"skus": [{"id": "s1"}, {"id": "s2"}], "price": 19.9, "title": "bikini"}
    assert build_request_fingerprint(a) == build_request_fingerprint(b)


def test_request_fingerprint_changes_when_content_changes():
    a = {"title": "bikini", "price": 19.9}
    b = {"title": "bikini", "price": 21.9}
    assert build_request_fingerprint(a) != build_request_fingerprint(b)


def test_fingerprint_and_idempotency_key_are_not_the_same_thing():
    """两者分工不同,不该被后来的人合并掉:
    键决定「要不要发」,指纹决定「发出去的是什么」。
    """
    key = build_publish_idempotency_key(**BASE)
    fingerprint = build_request_fingerprint({"channel": "shein"})
    assert key != fingerprint
    assert not fingerprint.startswith("pub-")
