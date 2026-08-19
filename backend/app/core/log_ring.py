"""运行日志的环形缓冲(`docs/LOG-CONSOLE.md` §4)。

## 为什么是 Redis 的定长列表

排障要的是「现在、这台、最近几千条」。为此上 Loki/ELK 是杀鸡用牛刀;写进
PostgreSQL 则把写放大带进每一次 LLM 调用(一次评分 = 4 张候选 × 3 条生命周期
日志)。而 Redis 已经是硬依赖(Celery broker、readiness 探针都在),
且 **Celery worker 与 API 是不同进程** —— 进程内环形缓冲会漏掉最有价值的
那部分日志(`tasks` 域 59 个调用点全在 worker 里)。

跨进程、有界、已经在部署面里的,只有 Redis。

## 口径:诊断窗口,不是归档

Redis 重启即清空。**这是可接受的** —— 归档仍然是 stdout + 外部收集器,
那条链路一个字节没动。界面必须把窗口边界说出来(`oldest_ts`),
查不到早于它的不是没发生,是滚出窗口了。

## 日志绝不反噬业务

这是本模块唯一的硬要求,拆成四条:

1. **超时 0.2 秒。** 连接和读写都是。Redis 卡住不能让一次出图调用跟着卡住。
2. **任何异常静默吞掉,进程内计数器 +1。** 但**不瞎**:计数随 `/meta` 暴露
   (`dropped_since_boot`),掉了多少条要能看见。
3. **失败后进冷却期。** 少了这条,Redis 挂掉时每一条日志都要付 0.2 秒 ——
   一个"日志系统把应用拖垮"的经典形状,而它恰恰发生在出事的时候。
4. **handler 内部不产生任何 logging 调用。** redis-py 自己的 logger 钉到
   WARNING 之上并 `propagate=False`,否则一次写失败会打一条日志、
   那条日志又走进这个 handler、再失败一次 —— 递归。

## 脱敏没有第二套

写进环形的**就是** `JsonFormatter` 的产物。脱敏在 formatter 里已经完成,
这里不再有第二套逻辑,也就没有第二套漏法。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from app.core.log_events import ACCESS_EVENT

#: 环形列表的键。单键,不按域分片 —— 分片之后"最近 200 条"要跨 15 个键归并,
#: 而归并需要一个全局序,那正是 Redis 列表已经免费给的东西。
RING_KEY = "ops:log_ring"

#: 一次失败之后多久再试。见模块文档第 3 条。
_FAILURE_COOLDOWN_SECONDS = 5.0

#: 连接与读写超时。两个都要设:只设 connect 的话,连上之后卡在读上一样会挂住。
_SOCKET_TIMEOUT_SECONDS = 0.2

#: 控制台自己那几个端点的前缀。`API_PREFIX` 可配,所以真正的值由
#: `setup_logging` 传进来;这里只是"没人告诉我时的默认"。
OPS_API_PREFIX = "/api/ops/"


class AccessNoiseFilter(logging.Filter):
    """**访问日志不许把诊断窗口冲干净。**(a55 扩,a54 首版)

    ## a54 只修了自指那一半

    a54 发现的是:`app/main.py` 的中间件对每个请求写一条
    `http.request_completed`,**包括 `/api/ops/logs` 自己**;而运行日志页的
    跟随模式是 3 秒一拍。一个开着的标签页 = 20 条/分钟 = 1200 条/小时,
    cap 5000 —— 约四小时后环形里 100% 是"某人在看运行日志"。

    修法当时是挡住 `/api/ops/` 前缀。**而前端有 17 处 `refetchInterval`:**

        TaskDetailPage        三个轮询器,2~3 秒一拍
        WorkbenchBatchPage    两个,2~3 秒
        ReviewQueuePage       15 秒 · SystemStatusPage 15 秒 · DashboardPage 30 秒

    打开一个任务详情页 ≈ 90 请求/分钟 = **5400 条/小时**,而 cap 是 5000。
    一个开着任务详情页的标签,一小时内把整个窗口洗一遍 —— 而排障的人
    恰恰会开着任务详情页。

    **修了自指那一半比不修更危险**:这些行标了 routine,在流视角里折进
    计数条;`held/cap` 显示 5000/5000,看起来非常健康,于是没人再怀疑它。

    ## 所以判据从"路径"换成"这条访问日志有没有诊断价值"

        errors(默认)   只有 >=400 的访问日志进环形
        all             全进(a54 的行为,给"我就是要看请求流水"的场景留着)

    三条边界一条没变:

    - **只挡 2xx/3xx。** `/api/ops/logs` 自己 500 了是要看见的,那不是噪音。
    - **只挡环形,不挡 stdout。** 归档面一个字节没动 —— 采集端仍然收到
      全部访问日志。这里去掉的只是"诊断窗口里没有诊断价值的那部分"。
    - **界面必须说出来。** `/meta` 报 `ring.access_mode`,页面在 `http` 域上
      画一行小字。窗口可以少装东西,但不许让人把"我没收"读成"没发生" ——
      那是这一整页反复申明不许说的那句话。
    """

    #: 兼容期别名。a54 的名字,外部若有引用不至于当场 ImportError。
    def __init__(self, prefix: str = OPS_API_PREFIX, *, mode: str = "errors") -> None:
        super().__init__()
        self.prefix = prefix
        self.mode = mode if mode in ("errors", "all") else "errors"

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - 覆盖标准库
        fields = getattr(record, "extra_fields", None)
        if not isinstance(fields, dict) or fields.get("event") != ACCESS_EVENT:
            return True
        status = fields.get("status")
        # 4xx / 5xx 永远放行 —— 两种模式下都一样。出错的请求是诊断信息,
        # 不是噪音,而"把出错的那一条也一起挡掉"是这个过滤器唯一不可接受的失败。
        if isinstance(status, int) and status >= 400:
            return True
        if self.mode == "all":
            # a54 的行为:只挡自指。留着这一档是因为"看请求流水"确实是
            # 一种真实用法,只是不该是默认 —— 默认那一档要保护窗口。
            return not str(fields.get("path") or "").startswith(self.prefix)
        return False


#: a54 的名字。改名的理由见 `AccessNoiseFilter` 的文档 —— 语义从"挡自指"
#: 扩成了"按模式挡访问噪音",而旧名字会让读的人以为它只管 `/api/ops/`。
SelfTrafficFilter = AccessNoiseFilter


class RingStats:
    """进程内计数。**不落 Redis** —— 它要回答的正是"连不上 Redis 时掉了多少"。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.dropped = 0
        self.written = 0
        self.last_error: str | None = None

    def drop(self, exc: BaseException | None = None) -> None:
        with self._lock:
            self.dropped += 1
            if exc is not None:
                # 只留类型名。异常原文里可能带连接串,而连接串里可能带密码。
                self.last_error = type(exc).__name__

    def ok(self) -> None:
        with self._lock:
            self.written += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "dropped_since_boot": self.dropped,
                "written_since_boot": self.written,
                "last_error": self.last_error,
            }


STATS = RingStats()


def _quiet_redis_logger() -> None:
    """把 redis-py 的 logger 钉住。见模块文档第 4 条 —— 这是防递归,不是降噪。"""
    for name in ("redis", "redis.connection", "redis.client"):
        noisy = logging.getLogger(name)
        noisy.setLevel(logging.CRITICAL)
        noisy.propagate = False


class _ClientHolder:
    """懒建的 Redis 客户端 + 冷却期。

    **不复用 Celery 的连接池。** 那个池的超时是按任务投递调的(秒级),
    而这里要的是 0.2 秒就放弃;共用一个池意味着两种需求里必然有一种被将就,
    而被将就的那一种是"日志不许拖慢业务"。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client: Any | None = None
        self._blocked_until = 0.0

    def get(self, url: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            if now < self._blocked_until:
                return None
            if self._client is not None:
                return self._client
        try:
            import redis  # 延迟导入:裁剪环境(纯测试、离线门禁)不装 redis 也要能起

            _quiet_redis_logger()
            client = redis.Redis.from_url(
                url,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
                # 环形里存的是 JSON 文本,解码交给 redis-py 省一次手工 decode;
                # 解不出来的字节按 replace 处理 —— 一条乱码好过整次读取抛异常
                decode_responses=True,
            )
        except Exception as exc:  # noqa: BLE001 - 建不出客户端也不许炸掉调用方
            self.fail(exc)
            return None
        with self._lock:
            self._client = client
        return client

    def fail(self, exc: BaseException | None = None) -> None:
        STATS.drop(exc)
        with self._lock:
            self._blocked_until = time.monotonic() + _FAILURE_COOLDOWN_SECONDS
            self._client = None

    def reset(self) -> None:
        """给测试用。生产路径不调 —— 冷却期是刻意的。"""
        with self._lock:
            self._client = None
            self._blocked_until = 0.0


_HOLDER = _ClientHolder()


def get_client(url: str) -> Any | None:
    """带冷却期的共享 Redis 客户端。拿不到返回 None,**不抛**。

    公开它是为了让 `llm/payload_store.py` 不必去 import `_HOLDER`:
    旁挂库和环形要的是同一件东西(0.2 秒放弃、失败进冷却、不复用 Celery 的池),
    而跨模块拿一个私有名意味着这层共享关系没有任何东西钉着。
    """
    return _HOLDER.get(url)


def note_failure(exc: BaseException | None = None) -> None:
    """把一次写失败记在账上并进冷却期。给旁挂库用 —— 它以前一条都不记。"""
    _HOLDER.fail(exc)


def reset_client_for_tests() -> None:
    _HOLDER.reset()


class RingHandler(logging.Handler):
    """把 `JsonFormatter` 的产物 LPUSH 进定长列表。

    ``seq`` 是**进程内**单调计数 + 进程标识。它只为两件事存在:跟随模式的
    增量去重,和同一毫秒内多条日志的稳定排序。**不承诺全局连续** ——
    多个 worker 各有各的计数,而把它做成全局连续需要一次额外的 INCR,
    也就是把日志写从一次往返变成两次。
    """

    def __init__(self, *, url: str, cap: int, key: str = RING_KEY) -> None:
        super().__init__()
        self.url = url
        self.cap = max(1, int(cap))
        self.key = key
        self._proc = f"{os.getpid():x}"
        self._seq = 0
        self._seq_lock = threading.Lock()

    def _next_seq(self) -> str:
        with self._seq_lock:
            self._seq += 1
            return f"{self._proc}-{self._seq}"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            formatter = self.formatter
            payload = (
                formatter.build_payload(record)  # type: ignore[attr-defined]
                if hasattr(formatter, "build_payload")
                else {"message": record.getMessage()}
            )
            payload["seq"] = self._next_seq()
            line = json.dumps(payload, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - 格式化失败也不许影响业务
            STATS.drop(exc)
            return

        client = _HOLDER.get(self.url)
        if client is None:
            STATS.drop()
            return
        try:
            pipe = client.pipeline(transaction=False)
            pipe.lpush(self.key, line)
            pipe.ltrim(self.key, 0, self.cap - 1)
            pipe.execute()
        except Exception as exc:  # noqa: BLE001 - 见模块文档第 2 / 3 条
            _HOLDER.fail(exc)
            return
        STATS.ok()

    # `logging.Handler.handleError` 默认往 stderr 打印堆栈。那会在 Redis 挂掉时
    # 给每一条日志配一份堆栈,把 stdout 采集面淹掉 —— 而 stdout 是归档面,
    # 也就是出事时唯一还靠得住的那一面。这里彻底闭嘴,账记在 STATS 上。
    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - 覆盖标准库
        STATS.drop()


def read_ring(
    url: str, *, limit: int, key: str = RING_KEY
) -> tuple[list[tuple[str, dict[str, Any]]], str | None]:
    """读最近 ``limit`` 条,**新在前**。返回 ([(原始行, 解析后), ...], 错误类型名)。

    ## 为什么把原始行也带出来(a55 改)

    上一版返回的是 `list[dict]` —— LRANGE 拿回来的那个字符串 `json.loads`
    完就丢了。于是 `api/ops_logs.py` 想做 `q` 子串匹配时,手上只有 dict,
    只能**重新 `json.dumps` 一遍**,而且是对全窗 5000 条每条都做:

        for row in rows:
            raw = json.dumps(...)      # ← 无条件,5000 次
            if not _matches(row, raw=raw): continue

    a54 那一轮把 `_shape()` 挪到了筛选后面并加了一条守卫,但守卫断言的是
    **`_matches` 的调用行号小于 `_shape` 的** —— 形状对了,而真正贵的那次
    `dumps` 还留在前面。守卫一直是绿的,开销一次没降。

    `_matches` 的文档字符串当时就写着「`q` 直接匹配 LRANGE 拿回来的那一行
    原文」。**代码没有这么做,而现在它可以了。**

    错误不抛。读不到时界面要说"环形不可用",而不是画一张空列表 ——
    空列表的意思是"这段时间没有日志",那是一句没发生的事(硬规则第 4 条)。
    """
    client = _HOLDER.get(url)
    if client is None:
        return [], STATS.snapshot()["last_error"] or "RedisUnavailable"
    try:
        raw = client.lrange(key, 0, max(0, limit - 1))
    except Exception as exc:  # noqa: BLE001
        _HOLDER.fail(exc)
        return [], type(exc).__name__

    rows: list[tuple[str, dict[str, Any]]] = []
    for item in raw or []:
        try:
            parsed = json.loads(item)
        except (TypeError, ValueError):
            # 解不出来的那一条跳过,但**不静默**:它会体现在 held 与实际条数的
            # 差里。整批抛掉的话,一条坏行会让整个查看器变空。
            continue
        if isinstance(parsed, dict):
            rows.append((item if isinstance(item, str) else str(item), parsed))
    return rows, None


def ring_length(url: str, *, key: str = RING_KEY) -> int | None:
    client = _HOLDER.get(url)
    if client is None:
        return None
    try:
        return int(client.llen(key))
    except Exception as exc:  # noqa: BLE001
        _HOLDER.fail(exc)
        return None
