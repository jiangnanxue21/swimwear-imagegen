"""结构化 JSON 日志。带 request_id,且对密钥做脱敏(需求第十九章)。"""
from __future__ import annotations

import io
import json
import logging
import re
import sys
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

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
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # message 与 exc 也要脱敏,但**不能靠 `redact`** —— 那个函数按键名判定,
        # 而这两个键名本身完全无辜,值却是纯字符串。以前这两项直接落盘:
        # 堆栈里最常见的一行正是带 Authorization 头或带签名查询串的请求信息,
        # 异常一抛,密钥就以明文进了日志,绕开了所有键名规则。
        payload["message"] = scrub_text(payload["message"])
        if "exc" in payload:
            payload["exc"] = scrub_text(payload["exc"])
        return json.dumps(payload, ensure_ascii=False)


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


def setup_logging(level: str = "INFO") -> None:
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
    # httpcore/httpx 在 DEBUG 下逐行打印请求头(含 Authorization)与响应体
    # 分块 —— 一次评分调用能刷出几百行,而图片是 base64。PIL 的 DEBUG 会为
    # 每个图像块打一行。这三个跟着 root 走到 DEBUG 的代价太大,单独钉住。
    for noisy in ("uvicorn.access", "multipart", "httpcore", "httpx", "PIL"):
        logging.getLogger(noisy).setLevel("WARNING")


def get_logger(name: str) -> logging.LoggerAdapter:
    return ContextLoggerAdapter(logging.getLogger(name), {})
