"""登录失败限流的**判定**(a51)。零依赖,可穷举。

## 要挡的是什么

`/api/auth/login` 在此之前没有任何速率限制:没有计数、没有锁定、没有延迟。
这套系统只有 `admin` / `operator` 两个账号,口令是配置里的共享明文 ——
也就是说爆破面是"两个已知用户名 × 一个口令空间",而尝试次数不设上限。

登录函数本身写得很仔细(三种失败统一回同一句话、`hmac.compare_digest`
定长比较、不记密码、先 `session.clear()` 防 fixation),失败也记了一条
warning 日志。**但没有任何东西读那条日志去拦截** —— 日志是给事后查的,
不是防线。

## 判定必须发生在验密码**之前**

这一点写下来是因为它很容易写反。先验密码、密码对就放行、只对错的计数,
读起来更"友好",但那样限流**一次也没有限制过猜测速率** ——
攻击者每一发都仍然被完整地验了一次。

所以顺序是:先问这道闸,闸说不行就直接 429,**根本不去比对口令**。
代价是被限住的那一刻,正确口令也进不来;收益是猜测速率从"网络能跑多快"
降到"每 5 分钟 10 次"。

## 只数失败,成功清零

正常人不会连错十次。而"连错几次之后终于想起来了"是最常见的真实动线 ——
成功一次就把该键的计数清空,否则一个手滑过的人会在之后的一整个窗口里
带着惩罚走路。

## 键是 (来源, 用户名),而不是只有其中一个

    只按用户名   任何人都能把 admin 锁死 —— 一个未登录的陌生人可以让
                 真正的管理员登不进来。这不是防护,是把爆破面换成了拒绝服务面
    只按来源     同一台机器换着用户名猜,两个账号各自独立计数,阈值翻倍;
                 而更糟的是共用出口 IP 的办公室里一个人手滑会连累所有人

两者一起做键:锁的是"这个来源猜这个账号"这件事。别的来源、别的账号不受影响。

**来源怎么取见 `core/client_ip.py`** —— 它有自己的一整套前提,不在这里重复。

## 这一层不持有状态

失败时刻的列表由调用方保管(`api/auth.py` 的进程内表),这里只回答
"给定这些时刻和现在,放不放行、还要等多久"。这么分是为了它能在
`tests/pure/` 里被穷举:限流最容易错在边界上(正好第 N 次、正好窗口边缘、
时钟回拨),而那些边界只有纯函数才测得起。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: 往前看多久。窗口内的失败才算数。
WINDOW_SECONDS = 300

#: 窗口内允许几次失败。第 `MAX_FAILURES` 次失败本身仍然会被处理
#: (它是让计数达到上限的那一次),**下一次**才被拦。
MAX_FAILURES = 10

#: 达到上限之后,拦多久。
#:
#: 60 秒而不是"锁到管理员来解锁":自解锁的惩罚不需要任何人介入,
#: 而人工解锁的闸在没人值班的夜里等于把系统关了。60 秒把猜测速率压到
#: 每分钟 10 次上下,离线爆破一把口令要按年算,而手滑的人等一分钟。
PENALTY_SECONDS = 60


@dataclass(frozen=True)
class ThrottleVerdict:
    """放不放行,以及不放行时还要等多久。"""

    allowed: bool
    #: 还要等几秒。放行时恒为 0。**向上取整**,因为它要进 `Retry-After`,
    #: 而那个头是整秒 —— 向下取整会让客户端按提示重试时又撞一次。
    retry_after_seconds: int
    #: 触发这次判定的窗口内失败次数。放进日志用,不进响应体
    #: (告诉调用方"你还剩几次"等于帮它踩点)。
    failures_in_window: int


def recent_failures(failures: Sequence[float], now: float) -> list[float]:
    """窗口内的失败时刻,顺带丢掉过期的。

    **也丢掉未来的时刻**:时钟回拨(NTP 校时、虚拟机挂起恢复)会让表里
    留下比 `now` 还晚的条目。不丢的话它们会在窗口里赖到未来那一刻,
    把一个本该已经解除的限制无限期延长。
    """
    return [t for t in failures if now - WINDOW_SECONDS <= t <= now]


def evaluate(failures: Sequence[float], now: float) -> ThrottleVerdict:
    """这次登录尝试放不放行。

    `failures` 是该键**历史失败时刻**的列表(秒,单调递增与否都不要求)。
    """
    window = recent_failures(failures, now)
    if len(window) < MAX_FAILURES:
        return ThrottleVerdict(True, 0, len(window))

    # 从**最后一次失败**起算惩罚,不是从窗口起点。前者的含义是"停手一分钟
    # 就放你过",后者会让持续敲打的人在窗口滚动时随机地被放进来一次。
    last = max(window)
    remaining = PENALTY_SECONDS - (now - last)
    if remaining <= 0:
        return ThrottleVerdict(True, 0, len(window))
    # 向上取整,且至少 1 —— `Retry-After: 0` 的意思是"现在就可以",而这里不是。
    return ThrottleVerdict(False, max(1, int(remaining) + (1 if remaining % 1 else 0)), len(window))
