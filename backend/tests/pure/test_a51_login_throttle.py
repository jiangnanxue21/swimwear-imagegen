"""登录限流的判定与来源解析(a51)。

这两件事都只有边界值得测:限流错在"正好第 N 次"和"正好窗口边缘",
来源解析错在"取了 XFF 的哪一段"。两者都是纯函数,所以能穷举。
"""
from __future__ import annotations

from app.core.client_ip import UNKNOWN, client_key
from app.workflows.login_throttle import (
    MAX_FAILURES,
    PENALTY_SECONDS,
    WINDOW_SECONDS,
    evaluate,
    recent_failures,
)

# ================================================================ 判定


def test_no_history_is_allowed():
    assert evaluate([], 1000.0).allowed


def test_one_short_of_the_limit_is_still_allowed():
    """第 `MAX_FAILURES` 次失败本身要能发生 —— 它是让计数达到上限的那一次。

    写反的话(`<=` 而不是 `<`)阈值实际上少一次,而没有任何东西会报错。
    """
    now = 1000.0
    failures = [now - i for i in range(MAX_FAILURES - 1)]
    assert evaluate(failures, now).allowed


def test_hitting_the_limit_blocks_the_next_attempt():
    now = 1000.0
    failures = [now - i for i in range(MAX_FAILURES)]
    verdict = evaluate(failures, now)
    assert not verdict.allowed
    assert verdict.failures_in_window == MAX_FAILURES
    assert verdict.retry_after_seconds > 0


def test_the_penalty_counts_from_the_last_failure_not_the_window_start():
    """停手一分钟就放行;持续敲打的人不会因为窗口滚动被随机放进来一次。

    从窗口起点算的话,一个每秒敲一次的攻击者会在窗口滚过最早那条记录时
    短暂地拿到一次放行 —— 而那正是他要的。
    """
    now = 1000.0
    failures = [now - 200 + i for i in range(MAX_FAILURES)]  # 最后一次在 now-191
    verdict = evaluate(failures, now)
    # 最后一次失败已经是 191 秒前,远超惩罚期,所以放行
    assert verdict.allowed

    # 而刚刚失败过的,要等满惩罚期
    fresh = [*failures[:-1], now - 1]
    blocked = evaluate(fresh, now)
    assert not blocked.allowed
    assert blocked.retry_after_seconds == PENALTY_SECONDS - 1


def test_the_penalty_expires_on_its_own():
    """自解锁。人工解锁的闸在没人值班的夜里等于把系统关了。"""
    now = 1000.0
    failures = [now - PENALTY_SECONDS - 1] * MAX_FAILURES
    assert evaluate(failures, now).allowed


def test_failures_older_than_the_window_do_not_count():
    now = 10_000.0
    failures = [now - WINDOW_SECONDS - 1] * (MAX_FAILURES * 3)
    assert evaluate(failures, now).allowed
    assert evaluate(failures, now).failures_in_window == 0


def test_a_failure_exactly_on_the_window_edge_still_counts():
    """边界含在内。`>` 与 `>=` 写反只差一次机会,但差的是判定的确定性。"""
    now = 10_000.0
    assert len(recent_failures([now - WINDOW_SECONDS], now)) == 1
    assert len(recent_failures([now - WINDOW_SECONDS - 0.001], now)) == 0


def test_timestamps_from_the_future_are_dropped():
    """时钟回拨之后表里会有比 now 还晚的条目。

    不丢的话它们会在窗口里赖到未来那一刻,把一个本该解除的限制无限期延长 ——
    表现是"没人在攻击,但管理员一直登不进来",而且重启才好。
    """
    now = 1000.0
    assert recent_failures([now + 5], now) == []
    assert evaluate([now + 5] * MAX_FAILURES, now).allowed


def test_retry_after_is_rounded_up_and_never_zero():
    """`Retry-After: 0` 的意思是"现在就可以",而这里不是。

    向下取整会让按提示重试的客户端又撞一次 —— 然后它会再算一次、再撞一次。
    """
    now = 1000.0
    failures = [*[now - 300 + i for i in range(MAX_FAILURES - 1)], now - PENALTY_SECONDS + 0.4]
    verdict = evaluate(failures, now)
    assert not verdict.allowed
    assert verdict.retry_after_seconds >= 1


# ================================================================ 来源解析


def test_the_peer_is_used_when_proxy_headers_are_not_trusted():
    """默认不信头。信了才读,而信任的前提写在 `core/client_ip.py` 顶部。"""
    headers = {"x-real-ip": "1.2.3.4", "x-forwarded-for": "1.2.3.4"}
    assert client_key(headers, "10.0.0.9", trust_proxy=False) == "10.0.0.9"


def test_real_ip_wins_when_the_proxy_is_trusted():
    headers = {"x-real-ip": "1.2.3.4", "x-forwarded-for": "9.9.9.9, 5.6.7.8"}
    assert client_key(headers, "10.0.0.9", trust_proxy=True) == "1.2.3.4"


def test_forwarded_for_takes_the_last_hop_not_the_first():
    """**最后一段才是可信的那一段。**

    nginx 的 `$proxy_add_x_forwarded_for` 展开成
    `<客户端自己带来的 XFF>, <nginx 看见的对端>`。取第一段(很多示例这么写)
    等于让来源完全由攻击者指定 —— 每发一次换一个值就永远不会命中同一个键,
    限流当场作废,而且**看不出来**:日志里每条都有来源,只是每条都不一样。
    """
    headers = {"x-forwarded-for": "203.0.113.7, 198.51.100.2"}
    assert client_key(headers, "10.0.0.9", trust_proxy=True) == "198.51.100.2"


def test_an_empty_header_falls_through_to_the_peer():
    """头存在但值为空,不能当成"来源是空串"。

    否则所有这类请求会共用同一个键 —— 看起来像限流生效了,
    实际上是把真实来源也并进了同一个桶。
    """
    headers = {"x-real-ip": "  ", "x-forwarded-for": " , "}
    assert client_key(headers, "10.0.0.9", trust_proxy=True) == "10.0.0.9"


def test_no_peer_and_no_headers_is_a_named_placeholder():
    assert client_key({}, None, trust_proxy=True) == UNKNOWN
    assert client_key({}, "", trust_proxy=False) == UNKNOWN
