"""派发重试与卡死判定的策略。仅标准库,可独立测试。

放在 workflows 层而不是 service 层,理由和 `evaluators/decision.py` 一样:
**这些是判断,不是动作**。判断可以在任何机器上直接跑测试,动作不行 ——
service 层一 import 就会拉起 SQLAlchemy 和数据库连接。

把它们混在 dispatch_service 里的代价很具体:退避曲线、放弃上限、卡死阈值
这几个数决定了系统在 Broker 挂掉和 worker 猝死时的行为,而这正是最难在
线上复现、最需要靠测试锁住的部分。
"""
from __future__ import annotations

#: 连续失败多少次之后放弃自动重试。
#: 配合下面的退避曲线,10 次大约覆盖半小时 —— Broker 挂了半小时还没回来,
#: 那不是重试能解决的问题,应该有人去看。继续刷日志只会淹没真正的告警。
MAX_DISPATCH_ATTEMPTS = 10

#: 退避上限。再长就失去"兜底"的意义了。
MAX_BACKOFF_SECONDS = 600


def backoff_seconds(attempts: int) -> int:
    """第 ``attempts`` 次失败之后要等多久再试。

    指数退避但封顶:Broker 抖一下时几秒内就恢复投递,真挂了也不会
    每秒撞一次库。封顶很重要 —— 不封顶的话第 20 次失败要等 12 天,
    等于悄悄放弃,却还显示成"待重试"。
    """
    if attempts <= 0:
        return 0
    return min(MAX_BACKOFF_SECONDS, 2 ** min(attempts, 16))


# ---------------------------------------------------------------- 卡死判定
#
# ## A45-batch12-5 / NEW-03:这一段原来是三个手写的数字
#
# 上一版写的是:
#
#     LONGEST_LEGAL_PAUSE_SECONDS = 300 + 4 * 30 + 4 * 90   # = 780
#
# 三个系数全部低于系统**自己允许**的值:
#
#     4 张候选图    接口是 `le=8`(schemas/generation.py)
#     4 × 90 秒     视觉评分单次 90 秒,但内部还有 VISION_MODEL_MAX_RETRIES=2 次重试,
#                   也就是一张图最多 3 × 90 = 270 秒
#     没有算 fetch  轮询超时之后还要 `fetch_results()`,那是另一次外部调用
#
# 于是一个**完全合法**的 8 张候选任务,正常评分要跑 8 × 270 = 2160 秒,
# 而卡死阈值是 1800 —— 回收器会在它正在花钱评分的时候把它判成卡死。
#
# ## 修法不是"把 780 改成 2700"
#
# 那几个系数全是**可配置**的(设置页可改 `VISION_MODEL_TIMEOUT_SECONDS`、
# `VISION_MODEL_MAX_RETRIES`、`FASHN_POLL_TIMEOUT_SECONDS`),再写死一个更大的
# 常量只是把同一个 bug 推迟到下一次调参。所以这里给的是**算法**:
# 具体数值由 `workflows/phase_budget.py` 在运行时按当前配置代入。
#
# ## 判据从"整段耗时"改成"两次心跳之间的间隔"
#
# 更要紧的是判据本身。回收器看的是 `updated_at`,而 `updated_at` 在**每个
# 提交点**都会推进。所以要拿来和阈值比的不是"一整轮最长多久",而是
# **相邻两个提交点之间最长多久**:
#
#     PROVIDER_RUNNING   get_status + fetch_results 之间没有提交点  -> 两者之和
#     DOWNLOADING        整段候选落库在一个保存点里,中途不能提交    -> N × 单张下载
#     SCORING            逐张之间有心跳提交(见 evaluation_service)  -> 一张图的上限
#
# 逐张心跳是这次一起补上的:没有它,SCORING 这一格就是 N 张的总和,
# 阈值必须抬到 2700 秒以上,而那意味着一个真死掉的 worker 要 45 分钟才有人管。
# 心跳把"正常但慢"和"死了"区分开,两边同时变好。


def longest_legal_pause_seconds(
    *,
    poll_timeout_seconds: float,
    fetch_timeout_seconds: float,
    candidate_count: int,
    download_timeout_seconds: float,
    score_timeout_seconds: float,
    score_attempts: int,
) -> int:
    """相邻两个提交点之间最长的**合法**停顿(秒)。

    取三段的最大值而不是求和:它们之间都隔着提交点,不会同时压在一格里。

    ``score_attempts`` 是**总次数**(初次 + 内部重试),不是重试次数 ——
    传 `VISION_MODEL_MAX_RETRIES` 会正好少算一次,而"少算一次"就是上一版
    那个 bug 的形状。
    """
    return max(
        1,
        int(
            max(
                # 查状态 + 取结果:中间没有提交点
                max(0.0, poll_timeout_seconds) + max(0.0, fetch_timeout_seconds),
                # 整批候选下载:整段在一个保存点里,中途提交会毁掉保存点语义
                max(1, candidate_count) * max(0.0, download_timeout_seconds),
                # 单张评分(含模型内部重试):逐张之间有心跳
                max(0.0, score_timeout_seconds) * max(1, score_attempts),
            )
            + 0.999
        ),
    )


def longest_legal_phase_seconds(
    *,
    poll_timeout_seconds: float,
    fetch_timeout_seconds: float,
    candidate_count: int,
    download_timeout_seconds: float,
    score_timeout_seconds: float,
    score_attempts: int,
) -> int:
    """一次续跑从头到尾最长的**合法**耗时(秒)。求和,不取最大值。

    这个数**不是**给卡死阈值用的(那个看的是心跳间隔),它回答的是另一个问题:
    "如果一次租约期间一次都不续期,租约得有多长才够用"。答案是 2700 秒以上,
    也就是说 —— 900 秒的租约必须靠续期才成立。`phase_budget` 用它做这条断言。
    """
    candidates = max(1, candidate_count)
    return int(
        max(0.0, poll_timeout_seconds)
        + max(0.0, fetch_timeout_seconds)
        + candidates * max(0.0, download_timeout_seconds)
        + candidates * max(0.0, score_timeout_seconds) * max(1, score_attempts)
        + 0.999
    )


#: 阈值相对最长合法停顿的余量倍数。
#:
#: 宁可晚回收,不可误杀:误杀一个正在跑的任务意味着白花一次 Provider 的钱,
#: 晚回收只是让一个已经死掉的任务多躺一会儿 —— 而那期间界面上它是"进行中",
#: 没有人会据此做决定。三倍是权衡后的数:够扛住一次外部调用比预期慢一倍,
#: 又不至于让真死掉的 worker 拖到没人管。
STALL_MARGIN = 3

#: 租约相对最长合法停顿的余量倍数。比卡死余量小 —— 租约到期只意味着
#: "可以接管",不代表对方一定死了,而接管本身还有 fencing 挡着。
LEASE_MARGIN = 3

#: 两条下限。配置调得很小时不该让阈值跟着塌到几十秒:那时候真正的风险
#: 不是"评分很慢",而是"库抖了一下、心跳晚了两秒"。
MIN_STALL_THRESHOLD_SECONDS = 1800
MIN_PHASE_LEASE_SECONDS = 900


def stall_threshold_seconds(longest_pause: int) -> int:
    """由最长合法停顿推出卡死阈值。**不许再手写一个常量。**"""
    return max(MIN_STALL_THRESHOLD_SECONDS, STALL_MARGIN * max(0, longest_pause))


def phase_lease_seconds(longest_pause: int) -> int:
    """由最长合法停顿推出阶段租约时长。

    只要租约在每个提交点续期,它就只需要覆盖**一格**,不需要覆盖整段。
    覆盖不了整段这件事是刻意的:租约越长,一个真死掉的 worker 挡住接管的
    时间就越长。
    """
    return max(MIN_PHASE_LEASE_SECONDS, LEASE_MARGIN * max(0, longest_pause))


#: 交付默认配置下的取值。运行时一律走 `workflows/phase_budget.py`(它读设置页
#: 的当前值),这里两个常量的用途只有两个:给下面的模块级不变量一个可求值的
#: 输入,以及给不方便拿配置的调用方一个保守默认。
#:
#:     FASHN_POLL_TIMEOUT_SECONDS      300   轮询等结果
#:     FASHN_REQUEST_TIMEOUT_SECONDS    60   取结果
#:     MAX_CANDIDATE_COUNT               8   一轮最多几张候选图
#:     DOWNLOAD_TIMEOUT_SECONDS         30   单张下载
#:     VISION_MODEL_TIMEOUT_SECONDS     90   单次视觉调用
#:     1 + VISION_MODEL_MAX_RETRIES      3   单张评分的总次数
DEFAULT_BUDGET_INPUTS: dict[str, float] = {
    "poll_timeout_seconds": 300.0,
    "fetch_timeout_seconds": 60.0,
    "candidate_count": 8,
    "download_timeout_seconds": 30.0,
    "score_timeout_seconds": 90.0,
    "score_attempts": 3,
}

#: 相邻两次心跳之间最长的合法停顿(默认配置)。
LONGEST_LEGAL_PAUSE_SECONDS = longest_legal_pause_seconds(**DEFAULT_BUDGET_INPUTS)  # type: ignore[arg-type]

#: 一次续跑从头到尾的最长合法耗时(默认配置)。租约靠续期覆盖它,不是靠长度。
LONGEST_LEGAL_PHASE_SECONDS = longest_legal_phase_seconds(**DEFAULT_BUDGET_INPUTS)  # type: ignore[arg-type]

#: 卡死判定阈值(默认配置)。运行时用 `phase_budget.stall_threshold_seconds()`。
STALL_THRESHOLD_SECONDS = stall_threshold_seconds(LONGEST_LEGAL_PAUSE_SECONDS)

# **不能用 `assert`。** 模块级 assert 在 `python -O` 下被整条剥离 ——
# 而这类"配置常量互相矛盾"的不变量,恰恰是要在**生产**(最可能开 -O 的地方)
# 拦住的那一类。原来三处都是 assert,也就是三条只在开发机上生效的守卫。
if STALL_THRESHOLD_SECONDS <= LONGEST_LEGAL_PAUSE_SECONDS:  # pragma: no cover
    raise RuntimeError(
        "卡死阈值必须大于最长合法停顿,否则会误杀正在正常跑的任务"
    )
if LONGEST_LEGAL_PHASE_SECONDS < LONGEST_LEGAL_PAUSE_SECONDS:  # pragma: no cover
    raise RuntimeError(
        "整段耗时不可能小于其中一格 —— 两个推导函数的输入对不上了"
    )
