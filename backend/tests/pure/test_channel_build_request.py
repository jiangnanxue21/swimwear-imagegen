"""`generic.build_request` —— 报文构造(任务 13)。

这一批盯的是「干跑与真实提交走同一段构造逻辑」这条设计的落点。
`base.py` 顶部记着切开的理由:v1 只有 submit(),干跑就只能是
"submit 里加一个 if",而那两条分支迟早长出差异 —— 干跑通过、真实提交报错。
"""
from __future__ import annotations

from app.channels.base import PublishOperationRef
from app.channels.generic import build_request
from app.core.enums import PublishOperation
from app.listings.contracts import MappedListing
from tests.pure._helpers import expect_raises

MAPPED = MappedListing(
    header={"spu": "SW-001", "title": "Bikini", "price": "19.90"},
    rows=({"sku": "SW-001-BLK-S"}, {"sku": "SW-001-BLK-M"}),
)


def _req(operation, external_spu_id=None):
    return build_request(
        MAPPED,
        PublishOperationRef(operation=operation, external_spu_id=external_spu_id),
    )


def test_create_posts_to_the_collection():
    r = _req(PublishOperation.CREATE)
    assert (r.method, r.path) == ("POST", "/v1/listings")


def test_update_puts_to_the_resource():
    r = _req(PublishOperation.UPDATE, external_spu_id="EXT-9")
    assert (r.method, r.path) == ("PUT", "/v1/listings/EXT-9")


def test_republish_and_delist_have_their_own_routes():
    assert _req(PublishOperation.REPUBLISH, "EXT-9").path.endswith("/republish")
    assert _req(PublishOperation.DELIST, "EXT-9").path.endswith("/delist")


def test_update_without_an_external_id_fails_here_not_at_the_platform():
    """少了它,路径里的占位符会被渲染成字面量,请求发到不存在的地址,
    平台返回 404 —— 而 404 在这条链路上会被读成"商品不见了",
    于是有人去手工重建。
    """
    for op in (PublishOperation.UPDATE, PublishOperation.REPUBLISH, PublishOperation.DELIST):
        expect_raises(ValueError, _req, op, None)


def test_body_carries_the_operation_even_though_the_path_implies_it():
    """刻意的冗余:排查时看到的是报文快照,而快照里没有操作类型的话,
    "这次是创建还是更新"要靠猜路径。
    """
    assert _req(PublishOperation.CREATE).body["operation"] == "CREATE"


def test_header_and_rows_come_through_unchanged():
    body = _req(PublishOperation.CREATE).body
    assert body["header"] == dict(MAPPED.header)
    assert body["rows"] == [dict(r) for r in MAPPED.rows]


def test_no_credentials_anywhere_in_the_prepared_request():
    """这个对象会被脱敏后存进 PublishAttempt.safe_request_snapshot,
    而快照是会被人翻出来看的东西(7.5 节)。签名在 submit() 里现算。
    """
    r = _req(PublishOperation.CREATE)
    blob = f"{r.headers}{r.body}{r.query}".lower()
    for word in ("authorization", "signature", "secret", "token", "apikey", "api_key"):
        assert word not in blob


def test_build_request_is_pure():
    """同样的输入构造两次,结果逐字段相同 —— 这是快照测试成立的前提。"""
    a, b = _req(PublishOperation.CREATE), _req(PublishOperation.CREATE)
    assert (a.method, a.path, a.body, a.headers) == (b.method, b.path, b.body, b.headers)


def test_idempotency_key_is_not_computed_here():
    """算它需要 shop_id / site / 草稿指纹,那些是运行期上下文,
    不属于"字段映射"这件事。留空由上一层塞。
    """
    assert _req(PublishOperation.CREATE).idempotency_key is None
