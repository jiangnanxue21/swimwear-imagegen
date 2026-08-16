"""HTTP 往返层(M2 从 evaluators/vision.py 抽出)。

一次多模态调用里,**和"评什么"无关**的那一半:

    端点拼接        base_url 与 /responses、/chat/completions 的拼法
    发送与重试      指数退避 + 抖动 + 尊重 Retry-After(但有上限)
    错误归类        HTTP 状态码 -> 限流/鉴权/额度/内容安全/超时
    响应文本提取    Responses 与 Chat Completions 两种形状
    客户端生命周期  注入的归调用方关,自己建的自己关

## 为什么必须共用

识别层(M3)要发的是同一种请求:同一个端点、同一套鉴权、同样会被限流、
同样需要退避。它自己写一遍的话,写出来的多半是 `httpx.post` 加一个
`try/except` —— 没有退避、不认 Retry-After、把 429 当成永久失败,
于是识别在高峰期整批失败,而评分层明明已经解决过这个问题。

## 不在这里的东西

**请求适配器(ResponsesAdapter / ChatCompletionsAdapter)留在评分层。**
它们的 `build()` 签名里带 `EvaluationDepth`,而且要调 `build_response_schema`
产出评分专用的 JSON Schema —— 那是"评什么"的一部分。放进来会让
`app/llm` 反向依赖 `app/evaluators`,而依赖方向正是 M2 的验收判据之一。

识别层将来会有自己的适配器,共用的是下面这个 `MultimodalClient`。

依赖方向:本模块**不得** import `app/evaluators`、`app/extractors`、`app/channels`。
"""
from __future__ import annotations

import json
import random
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from app.core.logging import get_logger
from app.llm import payload_store
from app.llm.images import PreparedImage
from app.llm.redaction import safe_payload_for_log, safe_request_summary
from app.providers.errors import (
    ProviderAuthError,
    ProviderContentSafetyError,
    ProviderError,
    ProviderInputError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

logger = get_logger(__name__)

#: 可重试的 HTTP 状态码。408 是请求超时,429 是限流,5xx 是对方的问题。
RETRYABLE_STATUS: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

#: 尊重 Retry-After,但不无条件尊重:见过返回 3600 的实现,那会把整轮生成挂死。
MAX_RETRY_AFTER_SECONDS = 30.0

_llm_call_id: ContextVar[str] = ContextVar("llm_call_id", default="-")
_llm_attempt: ContextVar[int] = ContextVar("llm_attempt", default=0)


def _payload_logging_enabled() -> bool:
    """开关放在系统配置，不要求把整个应用切到 DEBUG。"""
    try:
        from app.core.config import settings

        return bool(settings.LLM_LOG_PAYLOADS)
    except Exception:  # noqa: BLE001 - 裁剪环境仍要能导入传输层
        return False


def canonical_json_bytes(value: Any) -> bytes:
    """请求体的**唯一**权威字节形式。

    这个函数存在的理由是一致性,不是复用。以前的分工是:预算检查用
    `json.dumps(..., ensure_ascii=False, separators=(",", ":"))` 自己算一遍长度,
    真正发送时交给 httpx 的 `json=` 参数 —— 而 httpx 用的是自己的序列化参数
    (分隔符带空格、ensure_ascii 默认行为也不同)。两串字节不一样长。

    后果是预算检查形同虚设:pre-flight 说 4.9MB 通过,上线发出去的是另一个
    尺寸,厂商回 413。而 413 会被归类成上游错误,排查方向从一开始就是错的 ——
    没人会怀疑那个"已经检查过"的预算。

    现在只有这一个序列化点:预算按它算,`_send_once` 也把它的返回值原样
    `content=` 发出去。两者字面上是同一串字节,不是"应该一样"。

    序列化失败时退回 repr:这条路径只服务于日志与长度估算,不能因为一个
    不可序列化的值把调用炸掉;真正发送前 httpx 那一侧会自己再抛。
    """
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return repr(value).encode("utf-8", errors="replace")


#: 旧名,保留给传输层内部的日志调用点。
_json_bytes = canonical_json_bytes


def _response_summary(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    usage = body.get("usage")
    return {
        "response_id": body.get("id"),
        "response_model": body.get("model"),
        "finish_reason": find_finish_reason(body),
        "usage": usage if isinstance(usage, dict) else None,
    }


class TransportConfig(Protocol):
    """`MultimodalClient` 需要配置提供的字段。

    **用 Protocol 而不是基类。** M2 的验收判据里有一条「无子类特判」:
    评分层的 `VisionEvaluatorConfig` 和识别层将来的配置类是两套不同的
    环境变量,让它们继承同一个基类只会逼出一堆 `isinstance` 分支。
    鸭子类型下两边各自声明字段,传输层不需要知道自己在为谁工作。
    """

    api_key: str
    base_url: str
    model: str
    api_style: str
    timeout_seconds: float
    max_retries: int
    retry_base_seconds: float


@dataclass
class LLMRequest:
    """一次请求的全部内容。分开保存 URL / 头 / body,便于测试单独断言。"""

    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    images: list[PreparedImage] = field(default_factory=list)

    def safe_body_summary(self) -> dict[str, Any]:
        """可以进日志的请求摘要。**不含** messages / input —— 那里面是 base64。"""
        return safe_request_summary(
            url=self.url,
            body=self.body,
            image_count=len(self.images),
            inline_count=sum(1 for i in self.images if i.inline),
        )


def normalize_endpoint(base_url: str, suffix: str) -> str:
    """把 base_url 和路径拼成完整端点,并消掉重复的路径段。

    要处理的现实是:运维填 base_url 时习惯带上版本号(``.../v1``、``.../api/v3``),
    而文档里的端点又写成 ``/v1/responses``。两者一拼就成了 ``/v1/v1/responses``,
    换来一个 404 —— 而 404 在这一层看起来像"模型名写错了",能查很久。
    """
    base = (base_url or "").strip().rstrip("/")
    path = (suffix or "").strip().strip("/")
    if not base:
        return f"/{path}"
    if not path:
        return base

    base_segments = [s for s in urlparse(base).path.split("/") if s]
    path_segments = [s for s in path.split("/") if s]

    # 从最长的重叠开始试:base 尾部与 path 头部相同的部分只保留一份
    overlap = min(len(base_segments), len(path_segments))
    while overlap > 0:
        if base_segments[-overlap:] == path_segments[:overlap]:
            path_segments = path_segments[overlap:]
            break
        overlap -= 1

    return "/".join([base, *path_segments]) if path_segments else base


def _collect_text_fragments(node: Any, out: list[str]) -> None:
    """深度遍历,把所有看起来是输出文本的片段收集起来。

    刻意不假设 ``output[0].content[0]`` 存在:推理模型会在前面插入 reasoning 块,
    有的实现还会插入 tool_call 占位。按下标取值在开发机上能跑通,
    换个模型或打开推理就会变成"响应为空"。
    """
    if isinstance(node, dict):
        node_type = node.get("type")
        if node_type == "refusal":
            return  # refusal 由调用方单独处理,不当成正文
        if node_type in ("reasoning", "thinking"):
            return  # 思考内容不是答案,混进来会破坏 JSON
        text = node.get("text")
        if isinstance(text, str) and text.strip():
            out.append(text)
        for key in ("content", "output", "parts"):
            if key in node:
                _collect_text_fragments(node[key], out)
        return
    if isinstance(node, list):
        for item in node:
            _collect_text_fragments(item, out)


def extract_responses_text(payload: dict[str, Any]) -> str:
    """从 Responses API 响应里取出模型正文。"""
    convenience = payload.get("output_text")
    if isinstance(convenience, str) and convenience.strip():
        return convenience
    if isinstance(convenience, list):
        joined = "".join(str(x) for x in convenience if isinstance(x, str))
        if joined.strip():
            return joined

    fragments: list[str] = []
    _collect_text_fragments(payload.get("output"), fragments)
    return "".join(fragments)


def extract_chat_text(payload: dict[str, Any]) -> str:
    """从 Chat Completions 响应里取出模型正文。content 可能是字符串也可能是数组。"""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        fragments: list[str] = []
        _collect_text_fragments(content, fragments)
        return "".join(fragments)
    return ""


def find_refusal(payload: dict[str, Any]) -> str | None:
    """模型明确拒答时返回拒答理由。两种 API 的字段名不同,一起找。"""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and message.get("refusal"):
            return str(message["refusal"])[:300]

    stack: list[Any] = [payload.get("output")]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "refusal" and node.get("refusal"):
                return str(node["refusal"])[:300]
            stack.extend(node.get("content") or [])
        elif isinstance(node, list):
            stack.extend(node)
    return None


def find_finish_reason(payload: dict[str, Any]) -> str:
    """两种 API 的"为什么停下来"。"""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")

    if payload.get("status") == "incomplete":
        details = payload.get("incomplete_details")
        if isinstance(details, dict) and details.get("reason"):
            return str(details["reason"])
        return "incomplete"
    return ""


def summarize_error_body(body: Any, *, limit: int = 300) -> str:
    """把错误响应压成一句可以安全展示的话。

    厂商的错误体千奇百怪:有的是 JSON,有的是网关返回的 HTML 错误页,有的是纯文本。
    这里只取"像是错误消息"的那部分并截断 —— 完整 body 可能把请求内容原样回显,
    那里面有 base64。
    """
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or error.get("type")
            if message:
                return str(message)[:limit]
        for key in ("message", "msg", "detail"):
            if body.get(key):
                return str(body[key])[:limit]
        return json.dumps(body, ensure_ascii=False)[:limit]
    if isinstance(body, str):
        text = body.strip()
        if text.lower().startswith(("<!doctype", "<html")):
            return "上游返回了 HTML 错误页(通常是网关或地址写错)"
        return text[:limit]
    return ""


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS




class MultimodalClient:
    """把 `LLMRequest` 发出去,带重试与错误归类。

    调用方提供一个满足 `TransportConfig` 的配置对象和一个名字
    (进错误详情,用于回答"是哪一层发的这个请求")。
    """

    def __init__(
        self,
        config: TransportConfig,
        *,
        name: str = "llm",
        http_client: Any | None = None,
    ) -> None:
        self.config = config
        self.name = name
        self._http_client = http_client

    async def send(
        self,
        request: LLMRequest,
        *,
        send_once: Any | None = None,
        on_attempt: Any | None = None,
    ) -> tuple[dict[str, Any], int]:
        """发送并按策略重试。

        ``send_once`` 允许调用方替换"一次往返"的实现,默认用自己的。
        重试策略(退避、抖动、Retry-After、什么错不该重试)只有这一份,
        换往返实现不影响它 —— 这两件事本来就该分开:识别层将来要接
        批量接口时,需要的是另一种往返,而不是另一套退避。

        ## ``on_attempt``:**这一层是唯一知道"发出去了几次"的地方**
        ## (A45-batch18 / P1-2)

        重试对调用方是透明的 —— 这正是它的价值,也正是记账的洞:
        `max_retries=2` 时一次业务调用最多发出 3 个请求,而调用方只看到
        一个返回值或一个异常,于是费用台账固定按 1 次记。

        回调在**每一次往返之前**触发,参数是第几次(从 1 开始)。
        放在 try 之前而不是之后:计的是"发出去了几次",不是"成功了几次" ——
        厂商在收到请求那一刻就开始计费,超时不退钱。真正的判定在
        `extractors/call_accounting.py`(纯模块),这里只负责如实报数。

        回调抛异常不吞:一个坏掉的计数器把一次正常调用变成失败,
        比少一个数字更容易被发现,而这正是想要的方向。
        """
        import asyncio

        round_trip = send_once or self._send_once

        attempt = 0
        last_error: ProviderError | None = None
        call_id = uuid4().hex[:12]
        request_bytes = _json_bytes(request.body)
        request_fields: dict[str, Any] = {
            "event": "llm.request_prepared",
            "llm_call_id": call_id,
            "request_body_bytes": len(request_bytes),
            "request_sha256_16": sha256(request_bytes).hexdigest()[:16],
            "timeout_seconds": self.config.timeout_seconds,
            "max_attempts": self.config.max_retries + 1,
            **request.safe_body_summary(),
        }
        if _payload_logging_enabled():
            request_fields["request_body"] = safe_payload_for_log(request.body)
        # 旁挂库是**另一个去向**,不是这个开关的另一半:上面那行进 stdout
        # (归档面,默认关),下面这次进本机 Redis(诊断窗口,默认开、有 TTL)。
        # 两者的脱敏是同一个函数,两者的图片正文都永不落盘 —— 见 payload_store。
        payload_store.capture_request(
            call_id,
            provider=self.name,
            model=self.config.model,
            endpoint=request.url,
            headers=request.headers,
            body=request.body,
            request_sha256_16=request_fields["request_sha256_16"],
            request_body_bytes=len(request_bytes),
            # 用 `describe()` 而不是另写一份摘要:那个方法已经是"可以安全写进
            # 日志"的那一份,刻意不含 url 与 base64。图片的 sha256_16 不在这里 ——
            # 它在脱敏后的 body 里(`safe_payload_for_log` 把 data URL 换成了
            # `{redacted, mime_type, base64_chars, sha256_16}`),那才是真正
            # 发出去的那一份的摘要。
            images=[one.describe() for one in request.images],
        )
        logger.info(
            "llm request prepared",
            extra={"extra_fields": request_fields},
        )

        # 客户端在循环**外**建一次。以前每次尝试新建再关掉,三次重试就是三次
        # TCP + TLS 握手 —— 限流退避本来就在等,再叠上握手纯属白等
        async with self._client() as client:
            while attempt <= self.config.max_retries:
                attempt_number = attempt + 1
                attempt_started = time.monotonic()
                call_token = _llm_call_id.set(call_id)
                attempt_token = _llm_attempt.set(attempt_number)
                try:
                    if on_attempt is not None:
                        on_attempt(attempt + 1)
                    logger.info(
                        "llm request attempt started",
                        extra={
                            "extra_fields": {"event": "llm.attempt_started",
                                "llm_call_id": call_id,
                                "attempt": attempt_number,
                                "max_attempts": self.config.max_retries + 1,
                                "request_body_bytes": len(request_bytes),
                                **request.safe_body_summary(),
                            }
                        },
                    )
                    body, status = await round_trip(request, client)
                    logger.info(
                        "llm request completed",
                        extra={
                            "extra_fields": {"event": "llm.request_completed",
                                "llm_call_id": call_id,
                                "attempt": attempt_number,
                                "http_status": status,
                                "duration_ms": int((time.monotonic() - attempt_started) * 1000),
                                **_response_summary(body),
                            }
                        },
                    )
                    return body, status
                except ProviderError as exc:
                    last_error = exc
                    logger.warning(
                        "llm request attempt failed",
                        extra={
                            "extra_fields": {"event": "llm.attempt_failed",
                                "llm_call_id": call_id,
                                "attempt": attempt_number,
                                "max_attempts": self.config.max_retries + 1,
                                "duration_ms": int((time.monotonic() - attempt_started) * 1000),
                                "error_type": type(exc).__name__,
                                "error_code": str(exc.code),
                                "retriable": exc.policy.retriable,
                                "http_status": (exc.detail or {}).get("http_status"),
                            }
                        },
                    )
                    if not exc.policy.retriable or attempt >= self.config.max_retries:
                        raise
                    delay = self._retry_delay(attempt, exc)
                    logger.warning(
                        "llm request retrying",
                        extra={
                            "extra_fields": {"event": "llm.retrying",
                                "attempt": attempt + 1,
                                "max_retries": self.config.max_retries,
                                "delay_seconds": round(delay, 2),
                                "reason": type(exc).__name__,
                                **request.safe_body_summary(),
                            }
                        },
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                finally:
                    _llm_attempt.reset(attempt_token)
                    _llm_call_id.reset(call_token)

        raise last_error or ProviderError("模型请求失败", provider=self.name)

    def _retry_delay(self, attempt: int, exc: ProviderError) -> float:
        """指数退避 + 抖动。抖动是为了避免一整轮四张候选图同时重试、同时再被限流。"""
        retry_after = (exc.detail or {}).get("retry_after")
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
        base = self.config.retry_base_seconds * (2**attempt)
        return base + random.uniform(0, self.config.retry_base_seconds)

    def client(self):
        """客户端生命周期的**公开**入口。

        调用方(连接探针那类只发一次请求、不需要重试的场景)用这个,
        而不是伸手去拿 `_client`:私有名一改,那边就是一个 `AttributeError`,
        而它多半会被某个宽泛的 except 吃掉,表现成"服务不可达"。
        """
        return self._client()

    @asynccontextmanager
    async def _client(self):
        """拿一个 HTTP 客户端。

        注入进来的归调用方关闭 —— 测试和长驻服务都会复用同一个客户端,
        在这里替它关掉会让第二次调用拿到一个已关闭的连接池。
        自己建的自己关。
        """
        if self._http_client is not None:
            yield self._http_client
            return

        import httpx

        client = httpx.AsyncClient(timeout=self.config.timeout_seconds)
        try:
            yield client
        finally:
            await client.aclose()

    async def _send_once(self, request: LLMRequest, client: Any) -> tuple[dict[str, Any], int]:
        import httpx

        # 客户端由 _client() 管生命周期,这里只负责一次往返
        #
        # 用 `content=` 而不是 `json=`:预算检查算的必须**就是**发出去的这串
        # 字节。交给 httpx 的 `json=` 序列化,它会按自己的参数再编一次,
        # 于是 pre-flight 通过的请求在线上是另一个尺寸(见 canonical_json_bytes)。
        # Content-Type 由适配器在 headers 里显式写好,不依赖 `json=` 代设。
        try:
            response = await client.post(
                request.url,
                headers=request.headers,
                content=canonical_json_bytes(request.body),
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                self._error_message("请求超时", status=None, extra=type(exc).__name__),
                provider=self.name,
                detail=self._error_detail(status=None),
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                self._error_message("网络请求失败", status=None, extra=type(exc).__name__),
                provider=self.name,
                detail=self._error_detail(status=None),
            ) from exc

        status = response.status_code
        response_bytes = bytes(response.content)
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text

        response_fields: dict[str, Any] = {
            "event": "llm.response_received",
            "llm_call_id": _llm_call_id.get(),
            "attempt": _llm_attempt.get(),
            "http_status": status,
            "response_body_bytes": len(response_bytes),
            "content_type": response.headers.get("content-type"),
            "upstream_request_id": (
                response.headers.get("x-request-id")
                or response.headers.get("request-id")
                or response.headers.get("x-trace-id")
            ),
            **_response_summary(body),
        }
        if _payload_logging_enabled():
            response_fields["response_body"] = safe_payload_for_log(body)
        # 非 JSON 的那一次恰恰是最需要原文的那一次:上游返回网关 HTML 错误页、
        # 一段散文、被截断的 JSON —— 那时摘要里只剩 http_status 和 content_type,
        # finish_reason 与 usage 全是空。所以这次留痕不看 body 是什么形状。
        payload_store.capture_attempt(
            _llm_call_id.get(),
            attempt=_llm_attempt.get(),
            http_status=status,
            duration_ms=None,
            content_type=response.headers.get("content-type"),
            upstream_request_id=response_fields["upstream_request_id"],
            body=body,
        )
        logger.info(
            "llm http response received",
            extra={"extra_fields": response_fields},
        )

        if status >= 400:
            raise self._error_from_status(status, body, response.headers)

        if not isinstance(body, dict):
            raise ProviderError(
                self._error_message(
                    "上游返回的不是 JSON 对象",
                    status=status,
                    extra=summarize_error_body(body),
                ),
                provider=self.name,
                detail=self._error_detail(status=status),
            )
        return body, status

    def _error_from_status(self, status: int, body: Any, headers: Any) -> ProviderError:
        summary = summarize_error_body(body)
        detail = self._error_detail(status=status)

        retry_after = None
        try:
            raw = headers.get("retry-after") if headers is not None else None
            if raw is not None:
                retry_after = float(str(raw).strip())
        except (TypeError, ValueError, AttributeError):
            retry_after = None
        if retry_after is not None:
            detail["retry_after"] = retry_after

        message = self._error_message("上游返回错误", status=status, extra=summary)
        lowered = summary.lower()

        if status in (401, 403):
            return ProviderAuthError(message, provider=self.name, detail=detail)
        if status == 429:
            # 有的实现用 429 表达"余额不足",那种重试多久都不会好
            if any(w in lowered for w in ("quota", "insufficient", "balance", "欠费", "余额")):
                return ProviderQuotaError(message, provider=self.name, detail=detail)
            return ProviderRateLimitError(message, provider=self.name, detail=detail)
        if status == 402:
            return ProviderQuotaError(message, provider=self.name, detail=detail)
        if any(w in lowered for w in ("content_filter", "content policy", "safety", "风控")):
            return ProviderContentSafetyError(message, provider=self.name, detail=detail)
        if is_retryable_status(status):
            return ProviderError(message, provider=self.name, detail=detail)
        if status == 400 and ("json_schema" in lowered or "response_format" in lowered):
            return ProviderInputError(
                self._error_message(
                    "上游不支持所请求的结构化输出格式;"
                    "把 VISION_MODEL_RESPONSE_FORMAT 改成 json_object 或 prompt_only 再试",
                    status=status,
                    extra=summary,
                ),
                provider=self.name,
                detail=detail,
            )
        return ProviderInputError(message, provider=self.name, detail=detail)

    def _error_message(self, headline: str, *, status: int | None, extra: str = "") -> str:
        """错误信息:评分器、模型、API Style、状态码、可安全展示的摘要。绝不含 Key。"""
        parts = [
            f"{self.name} 评分失败:{headline}",
            f"模型={self.config.model or '未配置'}",
            f"api_style={self.config.api_style}",
        ]
        if status is not None:
            parts.append(f"http={status}")
        if extra:
            parts.append(f"详情={extra}")
        return ";".join(parts)

    def _error_detail(self, *, status: int | None) -> dict[str, Any]:
        return {
            "evaluator": self.name,
            "model": self.config.model,
            "api_style": self.config.api_style,
            "http_status": status,
        }

