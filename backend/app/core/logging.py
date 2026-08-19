"""结构化 JSON 日志。带 request_id,且对密钥做脱敏(需求第十九章)。"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
from contextvars import ContextVar

from app.core.log_events import resolve_domain

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

#: 这个进程是谁。`setup_logging(service=...)` 设一次,formatter 每条读一次。
#:
#: ## 为什么必须有这个字段(a55 加)
#:
#: API、Celery worker、beat 全部 LPUSH 进同一个 `ops:log_ring` 键,而
#: `build_payload` 产出的顶层字段里没有任何一个能区分它们。于是运行日志页上
#: 「`gen` 域为什么是空的」这个问题**答不出来** —— 是没跑任务,还是 worker
#: 挂了?这两件事的下一步完全相反。
#:
#: a54 修的是「worker 的日志一条都没进过环形」;修完之后的新问题是
#: 「进来了但认不出是谁」,而排障的第一个问题恰恰是「哪个进程」。
#:
#: `seq` 里本来就有 pid(`f"{os.getpid():x}-{n}"`),但它没被解析、没暴露成
#: 可读字段、更不能筛 —— 一个只有写日志的人看得懂的编码不算答案。
_service_name: str = "api"
_process_id: int = os.getpid()

#: 命中这些片段的键名,其值在日志中一律替换为 ***
_SECRET_KEY_PATTERN = re.compile(
    r"(api[_-]?key|secret|password|(?:^|[_-])token(?:$|[_-])|authorization|credential|cookie)",
    re.IGNORECASE,
)
_REDACTED = "***"

#: 值级脱敏。`redact` 按键名判定,管不到自由文本 —— message 和异常堆栈
#: 都是自由文本,而它们恰恰是密钥最常出现的地方。
#:
#: 这里刻意**不**复用 `app/llm/redaction.py` 里那套:`core` 不该反向依赖 `llm`
#: (import-linter 会拦),而且日志格式化跑在任何异常处理路径上,多一条
#: 跨层导入就多一次循环导入风险。两边各自维护一份是有意的重复。
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}")
_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|sig|signature|password|credential)=)[^&\s]+"
)
#: `sk-` 前缀是 OpenAI 系与多数兼容端点的通用形状
_BARE_KEY = re.compile(r"\bsk-[A-Za-z0-9._\-]{12,}")


def scrub_text(value: object) -> object:
    """把自由文本里的密钥形状换成 ***。只处理字符串,其余原样返回。"""
    if not isinstance(value, str):
        return value
    value = _BEARER.sub(r"\1" + _REDACTED, value)
    value = _SECRET_QUERY.sub(r"\1" + _REDACTED, value)
    return _BARE_KEY.sub(_REDACTED, value)


def redact(value: object) -> object:
    """递归脱敏字典中的密钥字段。"""
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _SECRET_KEY_PATTERN.search(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def build_payload(self, record: logging.LogRecord) -> dict[str, object]:
        """一条日志的结构化形状。**`format` 与 RingHandler 共用这一份。**

        拆出来不是为了好看:环形缓冲要在同一份 payload 上再加一个 `seq`,
        而它拿到的如果是已经 `json.dumps` 过的字符串,就只能反序列化再序列化 ——
        一次调用两次编解码,而这条路径每条日志都要走。

        ## `event` 与 `domain` 是顶层字段(`docs/LOG-CONSOLE.md` §3)

        调用点写在 `extra_fields` 里,这里把它提上来,并推导出 `domain`:

            写了 event  ->  domain = 注册表里那个码的域
            没写 event  ->  domain = logger 前缀最长匹配

        兜底那一半是**迁移期的全部保障**:210 个调用点不可能一次迁完,
        而查看器里不该出现「未分类」。差别只是没迁的那些没有中文事件标签、
        不能按事件精筛 —— 迁移因此是"渐进补精度",不是"一次换血"。
        """
        extra_raw = getattr(record, "extra_fields", None)
        extra = redact(extra_raw) if isinstance(extra_raw, dict) else {}
        # `redact` 返回的是新字典,所以 pop 不会动到调用点那一份。
        raw_event = extra.pop("event", None) if isinstance(extra, dict) else None
        event = raw_event if isinstance(raw_event, str) and raw_event else None

        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "domain": resolve_domain(event, record.name),
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
            # 进程身份。**归档面也带**(这是本轮唯一一处动 stdout 的地方,
            # 而它是加字段不是改字段):采集端把 api 与 worker 的日志收进
            # 同一个索引时,同样需要这一列才分得开。
            "service": _service_name,
            "pid": _process_id,
        }
        if event:
            payload["event"] = event
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # message 与 exc 也要脱敏,但**不能靠 `redact`** —— 那个函数按键名判定,
        # 而这两个键名本身完全无辜,值却是纯字符串。以前这两项直接落盘:
        # 堆栈里最常见的一行正是带 Authorization 头或带签名查询串的请求信息,
        # 异常一抛,密钥就以明文进了日志,绕开了所有键名规则。
        payload["message"] = scrub_text(payload["message"])
        if "exc" in payload:
            payload["exc"] = scrub_text(payload["exc"])
        return payload

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.build_payload(record), ensure_ascii=False)


class ContextLoggerAdapter(logging.LoggerAdapter):
    """保留调用点传入的结构化字段。

    标准 ``LoggerAdapter.process`` 会用 adapter 自己的 ``extra`` 覆盖调用点的
    ``extra``。本项目的 adapter 初始化字段为空，结果是所有
    ``extra={"extra_fields": ...}`` 都被静默丢掉，只剩 message 和 request_id。
    """

    def process(self, msg: object, kwargs: dict) -> tuple[object, dict]:
        supplied = kwargs.get("extra")
        kwargs["extra"] = {
            **self.extra,
            **(supplied if isinstance(supplied, dict) else {}),
        }
        return msg, kwargs


def setup_logging(level: str = "INFO", *, service: str = "api") -> None:
    # 日志流必须是 UTF-8,而且必须 errors="replace"。
    #
    # `JsonFormatter` 用 `ensure_ascii=False`,也就是中文原样写进流里。开发机
    # 的 stdout 在中文 Windows 上默认是 GBK:轻则整份日志不是 UTF-8、采集端
    # 读出乱码,重则遇到一个 GBK 编不出的字符(日文名、emoji、某些商品标题)
    # 抛 UnicodeEncodeError —— logging 会吞掉它,**整条记录消失**。
    # 而这类记录往往正是出问题的那一条。
    #
    # 优先用 reconfigure(3.7+,不换对象、不影响别处已持有的 sys.stdout 引用);
    # 拿不到就退回包一层 TextIOWrapper。两条路都失败就用裸 stdout ——
    # 日志编码不对是缺陷,起不来是事故,这里不赌。
    # 进程身份先定下来 —— 它要被下面挂上去的两个 handler 共用,而
    # `_attach_ring_handler` 里那条"当前访问日志模式"的自述日志本身
    # 就需要它。
    global _service_name, _process_id  # noqa: PLW0603 - 进程级单例,见字段文档
    _service_name = (service or "api").strip() or "api"
    _process_id = os.getpid()

    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        try:
            stream = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )
        except (AttributeError, ValueError, OSError):
            stream = sys.stdout

    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _attach_ring_handler(root)
    # httpcore/httpx 在 DEBUG 下逐行打印请求头(含 Authorization)与响应体
    # 分块 —— 一次评分调用能刷出几百行,而图片是 base64。PIL 的 DEBUG 会为
    # 每个图像块打一行。这三个跟着 root 走到 DEBUG 的代价太大,单独钉住。
    for noisy in ("uvicorn.access", "multipart", "httpcore", "httpx", "PIL"):
        logging.getLogger(noisy).setLevel("WARNING")


def _attach_ring_handler(root: logging.Logger) -> None:
    """挂上环形缓冲那一个 handler(`docs/LOG-CONSOLE.md` §4)。

    **stdout 那条链路一个字节没动。** 这是第二个 handler,不是替换 —— 归档面
    仍然是 stdout + 外部收集器,环形只是一个"现在、这台、最近几千条"的
    诊断窗口。

    整段包在 try 里:日志系统起不来不许让应用起不来。这不是防御性编程的
    口癖,是这个 handler 的全部前提 —— 它连的是 Redis,而 Redis 在本机开发、
    纯测试、离线门禁三种场景里都可能根本不在。
    """
    try:
        from app.core.config import settings

        if not settings.OPS_LOG_RING_ENABLED:
            return
        from app.core.log_ring import AccessNoiseFilter, RingHandler

        ring = RingHandler(url=settings.REDIS_URL, cap=settings.OPS_LOG_RING_CAP)
        ring.setFormatter(JsonFormatter())
        # 访问噪音不进环形。挂在 handler 上而不是 logger 上:
        # stdout 那一份必须原样保留(归档面一个字节没动),被挡掉的只有
        # "诊断窗口里没有诊断价值的那部分"。理由全文见 AccessNoiseFilter。
        mode = settings.OPS_LOG_RING_ACCESS
        ring.addFilter(AccessNoiseFilter(f"{settings.API_PREFIX}/ops/", mode=mode))
        root.addHandler(ring)
        # **当前模式必须留一条痕。** 少了它,一个"http 域怎么这么空"的问题
        # 只能靠去读 `.env` 才答得出,而读 `.env` 的前提是先想到这里有个旋钮。
        # 这条日志自己是 `ops` 域的,不会被自己挡掉。
        logging.getLogger("app.core.logging").info(
            "ops log ring access mode resolved",
            extra={
                "extra_fields": {
                    "event": "ops.log_ring_access_filtered",
                    "access_mode": mode,
                    "cap": settings.OPS_LOG_RING_CAP,
                }
            },
        )
    except Exception:  # noqa: BLE001 - 见 docstring 最后一段
        return


def get_logger(name: str) -> logging.LoggerAdapter:
    return ContextLoggerAdapter(logging.getLogger(name), {})


def log(
    logger: logging.Logger | logging.LoggerAdapter,
    level: str,
    message: str,
    *,
    event: str | None = None,
    exc_info: bool = False,
    **fields: object,
) -> None:
    """写一条结构化日志。**把 `extra={"extra_fields": {...}}` 那层包裹收进来。**

    ## 为什么值得加这个函数(a55)

    调用点的正确写法是双层包裹:

        logger.warning("...", extra={"extra_fields": {"event": "...", "key": ...}})

    少写 `extra_fields` 那一层,`JsonFormatter` 就看不见这些字段 ——
    `logging` 会把它们挂到 record 上,然后**没有任何人去看**。不报错、
    不提示,那条日志只是比作者以为的少了一半,而作者是在出事时才会去读它的。

    a54 抓到 **14 处**这么写的。最贵的一条是
    `batch.billed_result_unknown_refusing_paid_retry`(已计费但结果未知,
    拒绝付费重试):它记的 `key` / `action` / `status` 一个都没落地,于是
    "到底是哪一件被拒了"在日志里查不到答案。

    修法当时是加一条 AST 守卫。**守卫留着,但守卫挡的是"已经写错了",
    这个函数挡的是"写得出错"。** 两者不冲突,后者更早。

    ## 本轮不迁移任何现有调用点

    217 处的机械改动会把这一轮的 diff 淹掉,而收益是逐步的:新写的调用点
    用它,老的等各自模块下次被碰到时顺手换。守卫两边都认。
    """
    payload: dict[str, object] = dict(fields)
    if event:
        payload["event"] = event
    logger.log(
        logging.getLevelName(level.upper()) if isinstance(level, str) else level,
        message,
        exc_info=exc_info,
        extra={"extra_fields": payload},
    )
