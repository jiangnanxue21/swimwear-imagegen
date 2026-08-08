"""清理预案的两道安全边界(A45-batch18 / P1-3 + P1-4)。

## 这一组守的是哪两个洞

    P1-3   清单强制 `external_spu_id IS NOT NULL AND != ''`,于是
           **最需要核对的那一类行从清单上消失了** ——
           CREATE 到达了平台、响应在返回 ID 前超时,本地落
           `SUBMIT_RESULT_UNKNOWN`,而平台上留着一个孤儿商品
    P1-4   `_require_scope()` 用 `any(...)`,预览和执行共用同一道闸。
           `delist --channel GENERIC --apply` 会把该渠道下全部店铺、
           全部批次的真实商品排进下架队列

两个洞的共同点是:**它们都让一个用来防事故的模块自己变成事故来源**,
而模块顶部的文档字符串明确写着它不能是。

## 为什么这几条能留在 `tests/pure/`

被测的是判定:作用域够不够、`clean` 该不该为真。`_require_scope` 是纯函数;
`CleanupReport` 是 frozen dataclass,构造它不需要库。真库那一半(查询真的捞得到
无 ID 的行、`run_delist` 真的拒绝 channel-only)补在
`tests/test_poll_and_delist_db.py`,不另开文件:它要复用那边已经建好的
listing / draft 夹具,而"无 ID 的行上不上清单"原本就是那个文件里的一条用例
—— 那条用例断言的正是被修掉的行为,所以修复必须落在它旁边,让下一个人
一眼看见两种无 ID 场景的区别。两边缺一不可:判定对而查询漏掉那一类行,
或查询对而 `clean` 仍然说干净,都是这次要修的洞原样重现。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.core.errors import ValidationError
from app.services import cleanup_service as svc
from tests.pure._helpers import expect_raises

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SERVICE = BACKEND_ROOT / "app" / "services" / "cleanup_service.py"


# ================================================================ 作用域


def test_no_scope_at_all_is_still_refused():
    """既有护栏没被这次改动放宽。"""
    expect_raises(
        ValidationError,
        svc._require_scope,
        test_batch_tag=None,
        shop_id=None,
        channel=None,
    )


def test_channel_alone_can_look_but_cannot_touch():
    """**这一条是 P1-4 的全部内容。**

    渠道是"这个平台"的口径,不是"这一批 / 这一家"的口径。只给它就执行,
    等于一次参数遗漏(少打一个 `--tag`)扩大成一次生产批量下架 ——
    而在平台上下架通常撤不回来。

    预览不受限:看一眼整个渠道有哪些行,恰恰是决定要不要动手之前该做的事。
    """
    svc._require_scope(test_batch_tag=None, shop_id=None, channel="GENERIC")
    expect_raises(
        ValidationError,
        svc._require_scope,
        test_batch_tag=None,
        shop_id=None,
        channel="GENERIC",
        destructive=True,
    )


def test_a_batch_tag_or_a_shop_unlocks_the_destructive_path():
    """两个都是"这一批 / 这一家"的口径,都够。

    批次号是首选(4.1 节 H 要的就是"本轮测试创建了哪些"),但专用测试
    店铺同样把作用域收敛到一个人能负责的范围内。
    """
    svc._require_scope(test_batch_tag="uat-2026-08", shop_id=None, channel=None, destructive=True)
    svc._require_scope(test_batch_tag=None, shop_id="shop-test", channel=None, destructive=True)
    # 渠道 + 批次也行:渠道在这里只是进一步收窄
    svc._require_scope(
        test_batch_tag="uat-2026-08", shop_id=None, channel="GENERIC", destructive=True
    )


def test_the_destructive_check_runs_before_the_plan_is_built():
    """拒绝要发生在读库之前。

    排在后面的话,一次 channel-only 的调用会先把整个渠道的行读出来才被拒 ——
    拒对了,但日志里留下的是一次看起来"差一点就执行了"的全渠道扫描,
    而下一个人会照着它去放宽这道闸。
    """
    src = SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_delist"
    )
    body = ast.get_source_segment(src, fn) or ""
    assert "destructive=True" in body, "run_delist 必须走严格作用域"
    assert body.index("destructive=True") < body.index("plan_delist("), (
        "先拒绝,再读库"
    )


# ================================================================ 不确定行


def _row(**kw) -> svc.ExternalListing:
    import uuid

    base = dict(
        listing_id=uuid.uuid4(),
        channel="GENERIC",
        shop_id="shop-test",
        site="US",
        spu="SW-1",
        sku="SW-1-M",
        external_spu_id="",
        status="SUBMIT_RESULT_UNKNOWN",
        last_platform_status=None,
        test_batch_tag="uat-2026-08",
        created_at=None,
        simulated=False,
        occupying=True,
        delistable=False,
    )
    base.update(kw)
    return svc.ExternalListing(**base)


def test_an_unreconciled_row_makes_the_report_dirty():
    """**这一条是 P1-3 的全部内容。**

    `clean` 是 4.1 节 H 真正要的那个结论,也是 CLI 的退出码。
    它在"我不知道"的时候必须说 False —— 一个把不确定说成干净的布尔量,
    比没有这个布尔量更危险,因为它会让人停止核对。
    """
    report = svc._report([_row(needs_reconcile=True)])
    assert report.unreconciled == 1
    assert report.clean is False


def test_an_unreconciled_row_is_not_counted_as_occupying():
    """两个数回答两个问题,不能混。

    `occupying` 问"平台上还占着几个位置",而这一类行的答案是**不知道** ——
    把它算进去会让运营以为平台上确实有这么多商品,然后去后台找一个
    可能根本不存在的东西;不算进去而又不单独报,就是这次要修的洞。
    """
    report = svc._report([_row(needs_reconcile=True)])
    assert report.occupying == 0
    assert report.unreconciled == 1


def test_a_fully_delisted_batch_is_clean_again():
    """修好之后不能反过来变成"永远不干净"。

    一个恒为 False 的结论和一个恒为 True 的结论一样没用:两次之后
    没有人会再看它。
    """
    assert svc._report([]).clean is True
    assert svc._report([_row(occupying=False, delistable=False)]).clean is True


def test_the_report_dict_carries_the_unreconciled_count():
    """CLI 与接口都读这个 dict。数字埋在 rows 里等于没有 —— 没人会去数。"""
    assert svc._report([_row(needs_reconcile=True)]).as_dict()["unreconciled"] == 1


def test_an_unreconciled_row_is_never_auto_delistable():
    """没有外部 ID 就构造不出下架报文。**它不许自动下架,但必须留在清单上。**

    这两件事同时成立才对:自动下架掉一个我们连 ID 都没有的商品是不可能的,
    而让它从清单上消失正是 4.1 节 H 要防的那个结局。
    """
    row = _row(needs_reconcile=True)
    assert row.delistable is False
    assert row.needs_reconcile is True


def test_the_inventory_query_no_longer_filters_unknown_rows_away():
    """接线:查询条件里必须出现 `SUBMIT_RESULT_UNKNOWN`。

    上面几条判定全绿而查询仍然只捞有 ID 的行时,`unreconciled` 会恒为 0 ——
    一条永远为真的 `clean`,和修复前完全等价。
    """
    src = SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "inventory"
    )
    body = ast.get_source_segment(src, fn) or ""
    assert "SUBMIT_RESULT_UNKNOWN" in body
    assert "or_(" in body, "两类行是并集,不是交集"
