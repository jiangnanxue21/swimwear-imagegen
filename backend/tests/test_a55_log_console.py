"""运行日志控制台 a55 的行为守卫(`docs/LOG-CONSOLE.md` 第十三章)。

## 为什么这几条不住在 `tests/pure/`

那个目录的硬约束是"只有标准库"——`run_pure_tests.py` 在没有 pytest /
fastapi / sqlalchemy 的机器上也要能跑。而下面这几条要真的调一次
`app/api/ops_logs.list_logs`,那个模块 import fastapi。

于是 a53/a54 那一轮里,所有关于这个端点的守卫都是 **AST 断言** ——
读源码、比行号、找子串。这不是偷懒,是那个目录逼出来的。

**而这一轮的头号发现正是一条 AST 守卫看走了眼:**

    test_the_stream_is_filtered_before_it_is_shaped   断言 `_matches` 行号 < `_shape` 行号

那个形状从 a54 起一直成立,与此同时调用方循环里还留着一行无条件的
`json.dumps`,对全窗 5000 条各做一遍。守卫一直是绿的,开销一次没降 ——
因为它判的是代码长什么样,不是它做了什么。

所以这个文件存在的意义是:**凡是"做了什么"能被观察的,就别去比代码长什么样。**
"""
from __future__ import annotations

import json as json_module

import pytest
from fastapi import HTTPException

from app.api import ops_logs


def _call_list_logs(**overrides):
    """直接调 `list_logs`,**每个参数都显式给值**。

    不能只传想改的那几个:FastAPI 的默认值是 `Query(...)` 实例,不是 `None`。
    直接调用时那些实例会原样进到函数体里,而 `if domain and ...` 对一个
    `Query` 对象是**真**,于是每一行都被筛掉 —— 测试会得到一张空列表,
    并且看起来像"筛选逻辑写错了"。
    """
    args = dict(
        domain=None, event=None, level=None, service=None,
        request_id=None, task_id=None, q=None, since=None, until=None, limit=200,
    )
    args.update(overrides)
    return ops_logs.list_logs(**args)


def test_the_stream_really_stops_serialising_the_whole_window():
    """**跟随模式的开销与 `limit` 成正比,不与 `cap` 成正比。**(a55)

    ## 这条守卫替换掉了 a54 的 `test_the_stream_is_filtered_before_it_is_shaped`

    那一条断言的是 `list_logs` 里 `_matches` 的调用**行号**小于 `_shape` 的。
    它一直是绿的。而调用方的循环里还留着一行:

        for row in rows:
            raw = json.dumps(...)          # ← 无条件,全窗 5000 条各一次
            if not _matches(row, raw=raw): continue

    `_shape` 确实挪到了筛选后面,**而真正贵的那次 dumps 没有**。形状对了,
    开销一次没降,守卫看不见 —— 因为它判的是代码长什么样,不是它做了什么。

    **所以这一条数调用次数。** 它是本轮唯一一条"守卫自己看走了眼"的记录,
    留着这段文字是给下一个想改回 AST 断言的人看的。
    """
    window = 500
    rows = [
        (
            json_module.dumps(
                {
                    "ts": f"2026-08-18T10:{index // 60:02d}:{index % 60:02d}+0800",
                    "level": "INFO",
                    "logger": "app.llm.transport",
                    "domain": "llm",
                    "event": "llm.request_completed",
                    "message": "done",
                    "service": "api",
                    "seq": f"beef-{index}",
                },
                ensure_ascii=False,
            ),
            {
                "ts": f"2026-08-18T10:{index // 60:02d}:{index % 60:02d}+0800",
                "level": "INFO",
                "logger": "app.llm.transport",
                "domain": "llm",
                "event": "llm.request_completed",
                "message": "done",
                "service": "api",
                "seq": f"beef-{index}",
            },
        )
        for index in range(window)
    ]

    calls = {"n": 0}
    real_dumps = json_module.dumps

    def counting_dumps(*args, **kwargs):
        calls["n"] += 1
        return real_dumps(*args, **kwargs)

    limit = 20
    original_read, original_len, original_json = (
        ops_logs.read_ring,
        ops_logs.ring_length,
        ops_logs.json,
    )
    try:
        ops_logs.read_ring = lambda *_a, **_k: (rows, None)  # type: ignore[assignment]
        ops_logs.ring_length = lambda *_a, **_k: window  # type: ignore[assignment]

        class _Shim:
            dumps = staticmethod(counting_dumps)

        ops_logs.json = _Shim  # type: ignore[assignment]
        page = _call_list_logs(limit=limit)
    finally:
        ops_logs.read_ring = original_read  # type: ignore[assignment]
        ops_logs.ring_length = original_len  # type: ignore[assignment]
        ops_logs.json = original_json  # type: ignore[assignment]

    assert len(page["items"]) == limit
    assert calls["n"] <= limit + 5, (
        f"序列化了 {calls['n']} 次,而只有 {limit} 条会被返回 —— "
        "全窗序列化又回来了。跟随模式 3 秒一拍,这笔开销是常驻的"
    )


def test_the_stream_is_ordered_by_time_not_by_arrival():
    """**按时间排,不按到达顺序排。**(a55)

    环形是多个进程 LPUSH 进同一个键的,列表顺序是**到达顺序**:worker 与 API
    各自 fire-and-forget 写入(0.2 秒超时),排队与网络抖动都会打乱先后。
    而链路模式的横幅上写着「按时间顺读,旧在上」—— 一条 API 领取、worker
    执行、API 回写的任务链路,展示顺序可能是错的,**而界面正在向你保证
    它是对的**。

    时间戳解析不出来的排在末尾并带 `ts_unparsed`:不静默丢,也不假装它在
    某个位置 —— 兜底一个时间会让它安静地落在某处,而"它到底该排哪"
    恰恰是答不出来的那件事。
    """
    def make(ts, seq):
        row = {
            "ts": ts,
            "level": "INFO",
            "logger": "app.main",
            "domain": "app",
            "message": seq,
            "service": "api",
            "seq": seq,
        }
        return json_module.dumps(row, ensure_ascii=False), row

    # 故意乱序:到达顺序是 中 / 旧 / 新 / 坏
    rows = [
        make("2026-08-18T10:00:30+0800", "middle"),
        make("2026-08-18T10:00:10+0800", "oldest"),
        make("2026-08-18T10:00:50+0800", "newest"),
        make("not-a-timestamp", "broken"),
    ]
    original_read, original_len = ops_logs.read_ring, ops_logs.ring_length
    try:
        ops_logs.read_ring = lambda *_a, **_k: (rows, None)  # type: ignore[assignment]
        ops_logs.ring_length = lambda *_a, **_k: len(rows)  # type: ignore[assignment]
        page = _call_list_logs(limit=10)
    finally:
        ops_logs.read_ring = original_read  # type: ignore[assignment]
        ops_logs.ring_length = original_len  # type: ignore[assignment]

    order = [one["seq"] for one in page["items"]]
    assert order[:3] == ["newest", "middle", "oldest"], f"没有按时间排:{order}"
    assert order[-1] == "broken", "时间解析不出来的那条要沉到末尾,不许混进正常序列里"
    assert page["items"][-1]["ts_unparsed"] is True, "沉底了但没说为什么 —— 那是另一种静默"
    assert page["items"][0]["ts_unparsed"] is False


def test_a_truncated_list_says_so():
    """**截断必须显形。**(a55)

    上一版是 `if len(items) < limit: append`,超出就停,响应里没有任何字段
    说明这件事。而 a54 刚把域计数改成按全窗算 —— 于是命中 800 条、limit 200 时,
    左侧域轨道显示 800,右侧列表显示 200,**两个数字互相矛盾,页面上没有
    一处解释**。

    同页那行「窗口里最早的一条是 X」说的是全窗边界,而列表因为被截断,实际
    起点比 X 晚得多 —— 那句话在此刻是在误导人。所以 `shown_oldest_ts`
    和 `oldest_ts` 必须是两个数,谁也不冒充谁。
    """
    rows = []
    for index in range(50):
        row = {
            "ts": f"2026-08-18T10:00:{index:02d}+0800",
            "level": "INFO",
            "logger": "app.main",
            "domain": "app",
            "message": "x",
            "service": "api",
            "seq": str(index),
        }
        rows.append((json_module.dumps(row, ensure_ascii=False), row))

    original_read, original_len = ops_logs.read_ring, ops_logs.ring_length
    try:
        ops_logs.read_ring = lambda *_a, **_k: (rows, None)  # type: ignore[assignment]
        ops_logs.ring_length = lambda *_a, **_k: len(rows)  # type: ignore[assignment]
        page = _call_list_logs(limit=10)
    finally:
        ops_logs.read_ring = original_read  # type: ignore[assignment]
        ops_logs.ring_length = original_len  # type: ignore[assignment]

    assert page["matched"] == 50, "命中总数没报出来 —— 界面只能按显示的那一屏猜"
    assert page["truncated"] is True
    assert len(page["items"]) == 10
    assert page["shown_oldest_ts"] != page["oldest_ts"], (
        "列表起点和窗口边界是两个数。合成一个的话,那行「更早的不是没发生」"
        "会在被截断时变成一句假话"
    )
    assert page["services_seen"] == {"api": 50}


def test_the_time_window_refuses_a_value_it_cannot_read():
    """时间窗解析不出来就 400,**不当没填**。

    当没填的话,调用方会拿到一整窗的结果并以为"那就是那段时间里发生的全部事"
    —— 而这一页反复申明的那句话正是:界面不许说一件没发生的事。一个被静默
    忽略的时间窗说的恰好是这种话。
    """
    assert ops_logs._parse_bound(None, "since") is None
    assert ops_logs._parse_bound("  ", "since") is None
    assert ops_logs._parse_bound("2026-08-18T10:00:00+08:00", "since") is not None
    with pytest.raises(HTTPException) as caught:
        ops_logs._parse_bound("yesterday", "since")
    assert caught.value.status_code == 400


def test_an_unknown_access_mode_really_fails_to_construct():
    """真的构造一次 `Settings`,验拼错的模式名在**启动期**就炸。

    `tests/pure/` 那边只能做源码断言(没有 pydantic),这一条补上执行。
    两条都留着:源码那条在裁剪环境里也跑得动,这条证明它真的生效。
    """
    from app.core.config import Settings

    with pytest.raises(ValueError, match="OPS_LOG_RING_ACCESS"):
        Settings(OPS_LOG_RING_ACCESS="warn-only")


def test_the_access_mode_reaches_the_meta_endpoint():
    """`/meta` 必须把当前模式报出来。

    **窗口可以少装东西,但不许让人把「我没收」读成「没发生」。** 默认档下
    `http` 域会显得很空,而那个"空"是一次取舍的结果,不是一段安静的时间 ——
    界面要能说出区别,前提是后端先说。
    """
    original = ops_logs.read_ring
    try:
        ops_logs.read_ring = lambda *_a, **_k: ([], None)  # type: ignore[assignment]
        meta = ops_logs.logs_meta()
    finally:
        ops_logs.read_ring = original  # type: ignore[assignment]
    assert meta["ring"]["access_mode"] in ("errors", "all")
    assert "services_seen" in meta, "自检第一步是「窗口里有没有 worker」,这个数不能少"


def test_held_comes_from_llen_not_from_the_parsed_rows():
    """`/logs` 的 `ring.held` = 真实 LLEN。坏行的差要真的存在。

    monkeypatch 出"Redis 里 8 条、解析成功 5 条"的局面:a55 会报 held=5
    (差恒为 0,`read_ring` 文档里那句"坏行体现在差里"是空话),现在必须报 8。
    """
    rows = [
        (f'{{"ts": "2026-08-18T09:00:0{i}+0800", "level": "INFO", "seq": "aa-{i}"}}',
         {"ts": f"2026-08-18T09:00:0{i}+0800", "level": "INFO", "seq": f"aa-{i}"})
        for i in range(5)
    ]
    original_read, original_len = ops_logs.read_ring, ops_logs.ring_length
    try:
        ops_logs.read_ring = lambda *_a, **_k: (rows, None)  # type: ignore[assignment]
        ops_logs.ring_length = lambda *_a, **_k: 8  # type: ignore[assignment]
        page = _call_list_logs()
    finally:
        ops_logs.read_ring = original_read  # type: ignore[assignment]
        ops_logs.ring_length = original_len  # type: ignore[assignment]
    assert page["ring"]["held"] == 8, (
        f"held={page['ring']['held']} —— 拿解析条数冒充了 LLEN,坏行的差恒为 0"
    )
    assert page["matched"] == 5


def test_a_naive_timestamp_no_longer_crashes_the_stream():
    """环形里混一条无时区 ts、URL 里手打无时区 since —— 都不许 500。

    a55 在这两条路径上都是 `TypeError: can't compare offset-naive and
    offset-aware datetimes`:排序炸在 `picked.sort`,时间窗炸在 `_matches`。
    """
    rows = [
        ('{"ts": "2026-08-18T09:00:02+0800", "level": "INFO", "seq": "aa-2"}',
         {"ts": "2026-08-18T09:00:02+0800", "level": "INFO", "seq": "aa-2"}),
        ('{"ts": "2026-08-18T09:00:01", "level": "INFO", "seq": "bb-1"}',
         {"ts": "2026-08-18T09:00:01", "level": "INFO", "seq": "bb-1"}),  # 第三方 handler:无时区
    ]
    original_read, original_len = ops_logs.read_ring, ops_logs.ring_length
    try:
        ops_logs.read_ring = lambda *_a, **_k: (rows, None)  # type: ignore[assignment]
        ops_logs.ring_length = lambda *_a, **_k: len(rows)  # type: ignore[assignment]
        # 这条只验 naive 与 aware 能共存,不验服务器本机时区。边界放在两条
        # 记录之前:UTC runner 与东八区开发机都应命中两条;原来的 08:00
        # 在 UTC runner 上晚于 09:00+0800 的绝对时刻,正确过滤成 1 条,
        # 反而把测试目的写成了依赖机器时区的结论。
        page = _call_list_logs(since="2026-08-18T00:00:00")  # 手打,无时区
    finally:
        ops_logs.read_ring = original_read  # type: ignore[assignment]
        ops_logs.ring_length = original_len  # type: ignore[assignment]
    assert len(page["items"]) == 2, "无时区的行或时间窗把流弄丢/弄炸了"


def test_a_mistyped_level_is_a_400():
    """`?level=WARN` 是 400,不是静默拉回整窗。"""
    original_read, original_len = ops_logs.read_ring, ops_logs.ring_length
    try:
        ops_logs.read_ring = lambda *_a, **_k: ([], None)  # type: ignore[assignment]
        ops_logs.ring_length = lambda *_a, **_k: 0  # type: ignore[assignment]
        try:
            _call_list_logs(level="WARN")
        except HTTPException as exc:
            assert exc.status_code == 400
            assert "WARN" in str(exc.detail)
        else:
            raise AssertionError("level=WARN 被静默接受 —— 它会变成 rank 20,等于不筛")
    finally:
        ops_logs.read_ring = original_read  # type: ignore[assignment]
        ops_logs.ring_length = original_len  # type: ignore[assignment]


def test_same_second_entries_keep_arrival_order_by_counter():
    """同一秒、同一进程的第 7~10 条:新在前时 10 必须排在 7 前面。"""
    ts = "2026-08-18T09:00:00+0800"
    rows = [
        (f'{{"ts": "{ts}", "level": "INFO", "seq": "aa-{n}"}}',
         {"ts": ts, "level": "INFO", "seq": f"aa-{n}"})
        for n in (7, 10, 2)
    ]
    original_read, original_len = ops_logs.read_ring, ops_logs.ring_length
    try:
        ops_logs.read_ring = lambda *_a, **_k: (rows, None)  # type: ignore[assignment]
        ops_logs.ring_length = lambda *_a, **_k: len(rows)  # type: ignore[assignment]
        page = _call_list_logs()
    finally:
        ops_logs.read_ring = original_read  # type: ignore[assignment]
        ops_logs.ring_length = original_len  # type: ignore[assignment]
    assert [one["seq"] for one in page["items"]] == ["aa-10", "aa-7", "aa-2"], (
        "同秒 tie-break 还是字符串序 —— 10 排到 7 后面去了"
    )
