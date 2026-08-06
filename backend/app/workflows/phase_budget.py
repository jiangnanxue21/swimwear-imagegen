"""阶段租约与卡死阈值的**运行时**取值(A45-batch12-5 / NEW-03)。

`workflows/dispatch_policy.py` 给的是算法和交付默认值,这里负责把**当前配置**
代进去。两层分开的理由和 `evaluators/decision.py` 一样:算法要能在一个没有
数据库、没有 pydantic 的环境里被质询,取值要能跟着设置页改。

## 为什么必须是运行时读,不能在 import 时算一次

`FASHN_POLL_TIMEOUT_SECONDS`、`VISION_MODEL_TIMEOUT_SECONDS`、
`VISION_MODEL_MAX_RETRIES` 三个都在 `core/settings_schema.py` 里,也就是
**运营在设置页上能改**。import 时算一次的话:

    运营把视觉超时从 90 调到 180(页面写着"改完就生效")
    → 单张评分上限变成 540 秒
    → 租约与卡死阈值仍然按 90 秒那一版算
    → 回收器开始误杀正在正常评分的任务,而且每次都发生在花钱之后

这正是上一版"把 4 写死"那个 bug 的第二种形态:数字换了位置,毛病一样。
所以这里的四个函数都是**函数**,不是常量 —— 每次调用重新读一遍。

读取走 `providers/_config` 的 `provider_float` / `provider_int`:全系统读配置
只有那一个入口,设置页的覆盖值对所有调用点同时生效。它只依赖标准库,
因此这个模块在纯测试运行器里也能 import。

## 一个刻意的保守选择:候选张数取上限,不取任务自己的值

预算是给回收器和租约用的,而回收器扫的是**全表**,它手上没有"这一条任务
要出几张图"。取 `MAX_CANDIDATE_COUNT` 意味着 1 张图的任务也享受 8 张的余量,
代价是它死掉之后多躺一会儿;反过来按实际张数算,代价是 8 张的任务被误杀,
而那一侧要重新花一次 Provider 的钱。两个代价不对称,取贵的那一侧。
"""
from __future__ import annotations

from app.core.field_limits import MAX_CANDIDATE_COUNT
from app.providers._config import provider_float, provider_int
from app.workflows import dispatch_policy as policy

#: 单张候选图的下载超时。与 `tasks/generation_tasks.DOWNLOAD_TIMEOUT_SECONDS`
#: 是同一个数 —— 那边是 httpx 的实际参数,这边是预算的输入。
#:
#: 没有让预算去 import 那个常量:`generation_tasks` 一 import 就会拉起
#: sqlalchemy 和整条 Celery 链路,而这个模块要能在纯测试里被求值。
#: 两处一致由 `tests/pure/test_a45_batch12_5_fixes.py` 静态比对守住。
DOWNLOAD_TIMEOUT_SECONDS = 30.0


def budget_inputs() -> dict[str, float]:
    """把当前配置整理成 `dispatch_policy` 那两个纯函数的入参。"""
    return {
        "poll_timeout_seconds": provider_float("FASHN_POLL_TIMEOUT_SECONDS", 300.0),
        "fetch_timeout_seconds": provider_float("FASHN_REQUEST_TIMEOUT_SECONDS", 60.0),
        "candidate_count": MAX_CANDIDATE_COUNT,
        "download_timeout_seconds": DOWNLOAD_TIMEOUT_SECONDS,
        "score_timeout_seconds": provider_float("VISION_MODEL_TIMEOUT_SECONDS", 90.0),
        # **总次数 = 1 次初调 + N 次内部重试。**
        # 直接传 `VISION_MODEL_MAX_RETRIES` 会正好少算一次,而"少算一次"
        # 就是 NEW-03 那个 bug 的形状(4 × 90 而不是 4 × 3 × 90)。
        "score_attempts": 1 + max(0, provider_int("VISION_MODEL_MAX_RETRIES", 2)),
    }


def longest_legal_pause_seconds() -> int:
    """相邻两个提交点之间最长的合法停顿。回收器拿它和心跳间隔比。"""
    return policy.longest_legal_pause_seconds(**budget_inputs())  # type: ignore[arg-type]


def longest_legal_phase_seconds() -> int:
    """一次续跑从头到尾的最长合法耗时。**租约靠续期覆盖它,不是靠长度。**"""
    return policy.longest_legal_phase_seconds(**budget_inputs())  # type: ignore[arg-type]


def stall_threshold_seconds() -> int:
    """卡死判定阈值。配置一改,它跟着改。"""
    return policy.stall_threshold_seconds(longest_legal_pause_seconds())


def phase_lease_seconds() -> int:
    """阶段租约时长。

    它**不**覆盖 `longest_legal_phase_seconds()`,这是刻意的:租约越长,
    一个真死掉的 worker 挡住接管的时间就越长。够长的那一半由续期负责 ——
    每个提交点续一次,租约就永远只需要覆盖一格。

    代价是"续期必须真的接上"。这条前提由两样东西守着:
    `tasks/generation_tasks._heartbeat()` 的调用点,以及
    `tests/pure/test_a45_batch12_5_fixes.py` 里那条静态断言。
    """
    return policy.phase_lease_seconds(longest_legal_pause_seconds())


def describe() -> dict[str, int]:
    """当前预算的全貌。给日志、诊断接口和测试用,不参与任何判断。"""
    pause = longest_legal_pause_seconds()
    return {
        "longest_legal_pause_seconds": pause,
        "longest_legal_phase_seconds": longest_legal_phase_seconds(),
        "stall_threshold_seconds": policy.stall_threshold_seconds(pause),
        "phase_lease_seconds": policy.phase_lease_seconds(pause),
    }
