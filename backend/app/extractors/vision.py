"""真实多模态属性抽取器(任务 7 / PRD v3.1 §13 阶段 3,A45-batch14)。

一个抽取器,同样的四种后端:OpenAI Responses、OpenAI 兼容 Chat Completions、
火山方舟豆包、阿里云百炼千问 VL。与评分器(`evaluators/vision.py`)一样,
只按 ``EXTRACTOR_MODEL_API_STYLE`` 分两个适配器,**不按厂商名分支**。

## 与评分器共用什么、不共用什么

共用(M2 抽出的那套,一行不重写):

    app/llm/transport.py        重试、退避、Retry-After、错误归类、客户端生命周期
    app/llm/images.py           SSRF 校验、大小与格式检查、EXIF 旋转、base64
    app/llm/endpoint_trust.py   "要不要 Key"与"账记谁头上"(A45-batch14 抽出)

不共用:

    配置              EXTRACTOR_MODEL_* 独立一组,**不回退到 VISION_MODEL_***。
                      两个能力用同一个端点是常态,但那要运维显式填两遍 ——
                      静默共享的代价是换评分模型时识别模型跟着换,而识别的
                      校准分箱按 (字段 × 模型 × Prompt) 存,模型悄悄换掉之后
                      所有分箱作废、置信度全部退回"未校准",没有任何提示。
                      (`TEXT_MODEL_*` 独立成组是同一条先例。)
    Schema 与提示词    extractors/schema.py(§5.2:两套 Schema 本就分离)
    请求形状          一次一张图(base.py:「一次调用只看一张图,而且不告诉它
                      已有的值」),没有参考图/候选图之分

三条不能动的规矩,与评分器逐条对应:

1. 模型输出一律过 `schema.parse_extraction_payload`,解析不了就抛错,
   **不给空结果** —— "解析失败"和"模型真的什么都没看出来"必须可区分,
   前者该重试,后者该换图。
2. 结论(值采不采信、进不进确认队列)由合并层与决策表算,模型只提供证据。
   提示词里明确要求它不要输出结论。
3. 日志与留档里**不出现** API Key、图片字节、base64、完整请求体。
   可以出现:模型名、响应 ID、token 用量、finish reason、HTTP 状态码。
"""
from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from app.core.logging import get_logger
from app.extractors.base import ImageExtractionResult
from app.extractors.schema import (
    EXTRACTION_SCHEMA_VERSION,
    ExtractionParseError,
    api_ready_schema,
    build_extraction_prompt,
    build_extraction_schema,
    parse_extraction_payload,
    redact_for_log,
)
from app.llm import endpoint_trust
from app.llm.images import ImageResolver, PreparedImage
from app.llm.transport import (
    LLMRequest,
    MultimodalClient,
    extract_chat_text,
    extract_responses_text,
    find_finish_reason,
    find_refusal,
    normalize_endpoint,
)
from app.providers._config import provider_flag, provider_float, provider_int, provider_setting
from app.providers.errors import (
    NotConfiguredError,
    ProviderContentSafetyError,
    ProviderError,
    ProviderInputError,
)

logger = get_logger(__name__)

API_STYLE_RESPONSES = "responses"
API_STYLE_CHAT = "chat_completions"
API_STYLES = (API_STYLE_RESPONSES, API_STYLE_CHAT)

FORMAT_JSON_SCHEMA = "json_schema"
FORMAT_JSON_OBJECT = "json_object"
FORMAT_PROMPT_ONLY = "prompt_only"
RESPONSE_FORMATS = (FORMAT_JSON_SCHEMA, FORMAT_JSON_OBJECT, FORMAT_PROMPT_ONLY)

#: 发给厂商的结构化输出名。带版本号,换 Schema 时一起换 ——
#: 有的端点按这个名字缓存 Schema,同名不同形状会拿到旧缓存。
EXTRACTION_FORMAT_NAME = f"attribute_extraction_v{EXTRACTION_SCHEMA_VERSION}"

#: 识别提示词版本。校准分箱按 (字段 × 模型 × Prompt) 查(§6.3:未校准的组合
#: 一律不自动确认),所以**改一次提示词必须 bump 一次** —— 不 bump 的话,
#: 新提示词下的置信度会被旧提示词攒出来的分箱校准,数字看着有依据,依据是错的。
EXTRACTION_PROMPT_VERSION = f"vision-{EXTRACTION_SCHEMA_VERSION}"

#: 传进 `extract(options=...)` 的受众键。识别目标已按受众过滤(§18.3),
#: 这个键让 Schema 与提示词里的**枚举取值**也按受众收窄 —— 泳裤的请求体里
#: 不该出现比基尼专属的款式取值,摆在可选列表里,模型迟早会挑一个用。
AUDIENCE_KEY = "_audience"


@dataclass(frozen=True)
class ExtractorModelConfig:
    """识别模型配置。键独立于评分器,理由见模块顶部。"""

    api_key: str = ""
    base_url: str = ""
    model: str = ""
    api_style: str = API_STYLE_RESPONSES
    response_format: str = FORMAT_JSON_SCHEMA
    timeout_seconds: float = 60.0
    max_image_bytes: int = 8 * 1024 * 1024
    max_output_tokens: int = 1500
    max_retries: int = 2
    retry_base_seconds: float = 0.5
    #: 识别只有一档 detail,没有评分那种 QUICK/FULL 之分:识别的产出会进
    #: 商品事实,没有"粗看一眼"这个合法档位
    image_detail: str = "high"
    reasoning_effort: str = "low"
    send_public_urls: bool = False
    allow_anonymous: bool = False
    allowed_hosts: str = ""

    @classmethod
    def from_settings(cls) -> ExtractorModelConfig:
        style = (
            provider_setting("EXTRACTOR_MODEL_API_STYLE", API_STYLE_RESPONSES) or ""
        ).strip().lower()
        fmt = (
            provider_setting("EXTRACTOR_MODEL_RESPONSE_FORMAT", FORMAT_JSON_SCHEMA) or ""
        ).strip().lower()
        return cls(
            api_key=provider_setting("EXTRACTOR_MODEL_API_KEY"),
            base_url=provider_setting("EXTRACTOR_MODEL_BASE_URL"),
            model=provider_setting("EXTRACTOR_MODEL_NAME", "") or "",
            api_style=style if style in API_STYLES else API_STYLE_RESPONSES,
            response_format=fmt if fmt in RESPONSE_FORMATS else FORMAT_JSON_SCHEMA,
            timeout_seconds=provider_float("EXTRACTOR_MODEL_TIMEOUT_SECONDS", 60.0),
            max_image_bytes=provider_int("EXTRACTOR_MODEL_MAX_IMAGE_MB", 8) * 1024 * 1024,
            max_output_tokens=provider_int("EXTRACTOR_MODEL_MAX_OUTPUT_TOKENS", 1500),
            max_retries=provider_int("EXTRACTOR_MODEL_MAX_RETRIES", 2),
            retry_base_seconds=provider_float("EXTRACTOR_MODEL_RETRY_BASE_SECONDS", 0.5),
            image_detail=(
                provider_setting("EXTRACTOR_MODEL_IMAGE_DETAIL", "high") or "high"
            ).strip().lower(),
            reasoning_effort=(
                provider_setting("EXTRACTOR_MODEL_REASONING_EFFORT", "low") or "low"
            ).strip().lower(),
            send_public_urls=provider_flag("EXTRACTOR_MODEL_SEND_PUBLIC_URLS", False),
            allow_anonymous=provider_flag("EXTRACTOR_MODEL_ALLOW_ANONYMOUS", False),
            allowed_hosts=provider_setting("DOWNLOAD_ALLOWED_HOSTS"),
        )

    @property
    def requires_api_key(self) -> bool:
        """判定在 `llm/endpoint_trust.py`,与评分器同一份(A45-#36 的教训)。"""
        return endpoint_trust.endpoint_requires_api_key(
            self.base_url, allow_anonymous=self.allow_anonymous
        )

    @property
    def configured(self) -> bool:
        if not self.base_url.strip() or not self.model.strip():
            return False
        return not (self.requires_api_key and not self.api_key.strip())

    def missing_fields(self) -> list[str]:
        missing = []
        if not self.base_url.strip():
            missing.append("EXTRACTOR_MODEL_BASE_URL")
        if not self.model.strip():
            missing.append("EXTRACTOR_MODEL_NAME")
        if self.requires_api_key and not self.api_key.strip():
            missing.append("EXTRACTOR_MODEL_API_KEY")
        return missing


# --------------------------------------------------------------------------
# 请求适配器。按 API Style 选,不按厂商选 —— 与评分层同一条规矩。
# 不复用评分层的适配器类:它们的 build() 签名带 EvaluationDepth 与参考图/候选图,
# 那是"评什么"的一部分;识别的请求是一张图 + 一份识别 Schema。
# --------------------------------------------------------------------------


class _Adapter:
    style: str = ""

    def __init__(self, config: ExtractorModelConfig) -> None:
        self.config = config

    def endpoint(self) -> str:
        raise NotImplementedError

    def build(
        self,
        *,
        prompt: str,
        image: PreparedImage,
        schema: dict[str, Any],
    ) -> LLMRequest:
        raise NotImplementedError

    def extract_text(self, payload: dict[str, Any]) -> str:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"
        return headers


class ResponsesAdapter(_Adapter):
    """OpenAI Responses API,以及兼容它的端点(含火山方舟)。"""

    style = API_STYLE_RESPONSES

    def endpoint(self) -> str:
        return normalize_endpoint(self.config.base_url, "responses")

    def build(
        self, *, prompt: str, image: PreparedImage, schema: dict[str, Any]
    ) -> LLMRequest:
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": prompt},
            # 只有一张图也要标签:提示词说的是"这张图片",标签让"这张"有所指。
            # 评分层的 REFERENCE/CANDIDATE 标签是同一个道理
            {"type": "input_text", "text": "PRODUCT_IMAGE"},
            {
                "type": "input_image",
                "image_url": image.payload_url(),
                "detail": self.config.image_detail,
            },
        ]
        body: dict[str, Any] = {
            "model": self.config.model,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": self.config.max_output_tokens,
            "stream": False,
            "store": False,
        }
        if self.config.response_format == FORMAT_JSON_SCHEMA:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": EXTRACTION_FORMAT_NAME,
                    "strict": True,
                    "schema": schema,
                }
            }
        elif self.config.response_format == FORMAT_JSON_OBJECT:
            body["text"] = {"format": {"type": "json_object"}}

        effort = self.config.reasoning_effort
        if effort and effort != "none":
            body["reasoning"] = {"effort": effort}
        else:
            # 只在非推理模式下发 temperature:推理模型普遍拒收这个参数,
            # 无条件发送会换来一个 400,而 400 不重试 —— 整次识别当场失败
            body["temperature"] = 0

        return LLMRequest(
            url=self.endpoint(), headers=self.headers(), body=body, images=[image]
        )

    def extract_text(self, payload: dict[str, Any]) -> str:
        return extract_responses_text(payload)


class ChatCompletionsAdapter(_Adapter):
    """OpenAI 兼容的 Chat Completions,含阿里云百炼兼容模式。"""

    style = API_STYLE_CHAT

    def endpoint(self) -> str:
        return normalize_endpoint(self.config.base_url, "chat/completions")

    def build(
        self, *, prompt: str, image: PreparedImage, schema: dict[str, Any]
    ) -> LLMRequest:
        content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {"type": "text", "text": "PRODUCT_IMAGE"},
            {
                "type": "image_url",
                "image_url": {
                    "url": image.payload_url(),
                    "detail": self.config.image_detail,
                },
            },
        ]
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": self.config.max_output_tokens,
            "temperature": 0,
            "stream": False,
        }
        if self.config.response_format == FORMAT_JSON_SCHEMA:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": EXTRACTION_FORMAT_NAME,
                    "strict": True,
                    "schema": schema,
                },
            }
        elif self.config.response_format == FORMAT_JSON_OBJECT:
            body["response_format"] = {"type": "json_object"}

        return LLMRequest(
            url=self.endpoint(), headers=self.headers(), body=body, images=[image]
        )

    def extract_text(self, payload: dict[str, Any]) -> str:
        return extract_chat_text(payload)


def build_adapter(config: ExtractorModelConfig) -> _Adapter:
    if config.api_style == API_STYLE_CHAT:
        return ChatCompletionsAdapter(config)
    return ResponsesAdapter(config)


# --------------------------------------------------------------------------
# 抽取器
# --------------------------------------------------------------------------


class VisionAttributeExtractor:
    """真实多模态属性抽取器。实现 `base.ProductAttributeExtractor` 协议。"""

    name = "vision"
    display_name = "视觉大模型抽取器"
    #: 环境真实性判定读它(硬规则 4:问实现自己,不问名单)
    is_simulator = False
    #: 每一次调用都是一次付费的大模型请求。`run_extraction` 据此逐次记流水
    billable = True

    def __init__(
        self,
        config: ExtractorModelConfig | None = None,
        *,
        storage: Any | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.config = config or ExtractorModelConfig.from_settings()
        self._storage = storage
        self._adapter = build_adapter(self.config)
        # 传输层与评分器是同一个类(M2):重试、退避、错误归类全在那边
        self._transport = MultimodalClient(
            self.config, name=self.name, http_client=http_client
        )

    # ---------------- 配置 ----------------

    def is_configured(self) -> bool:
        return self.config.configured

    def declared_versions(self) -> tuple[str, str]:
        """模型版本取**配置**(`EXTRACTOR_MODEL_NAME`),不取响应里的
        `result.model_name` —— 后者要付过钱才知道,而幂等键要挡的正是
        付钱之前那两下(见 `base.declared_versions` 与 `run_state` 模块文档)。

        没配模型名时返回空串:`idempotency_key()` 对空版本直接抛 ValueError,
        服务层据此**不建键**。这条路是真实存在的 —— `EXTRACTOR_BACKEND=vision`
        但没配 `EXTRACTOR_MODEL_NAME` 时,第一次调用本来就会以
        `NotConfiguredError` 失败,幂等在那之前就没有意义了。
        """
        return str(self.config.model or ""), EXTRACTION_PROMPT_VERSION

    @property
    def usage_provider(self) -> str:
        """开销记在谁名下。与评分器同一套推导(`llm/endpoint_trust`)——
        同一个端点的识别费和评分费必须落在 `/spend` 页的同一个厂商下。"""
        return endpoint_trust.provider_label_from_base_url(
            str(self.config.base_url or ""), fallback=self.name
        )

    @property
    def storage(self) -> Any:
        if self._storage is None:
            from app.core.config import settings
            from app.services.storage import build_storage

            self._storage = build_storage(
                settings.STORAGE_BACKEND,
                settings.storage_dir,
                settings.PUBLIC_BASE_URL,
                settings.API_PREFIX,
            )
        return self._storage

    def _resolver(self) -> ImageResolver:
        return ImageResolver(
            self.storage,
            max_bytes=self.config.max_image_bytes,
            allowed_hosts=self.config.allowed_hosts,
        )

    # ---------------- 主流程 ----------------

    def extract(
        self,
        *,
        image: Any,
        target_fields: tuple[str, ...],
        enum_defs: Mapping[str, tuple[str, ...]],
        options: dict[str, Any] | None = None,
    ) -> ImageExtractionResult:
        """协议要求的同步入口。

        内部是异步的(传输层与图片层都是 async),这里用 `asyncio.run` 收口。
        调用方是 `run_extraction`,它跑在同步路由(FastAPI 线程池)或
        Celery worker 里 —— 两处的当前线程都没有事件循环,`asyncio.run` 安全。
        真的在事件循环里调它会得到 RuntimeError 而不是死锁,那是对的失败方式:
        识别的异步化(§4.6)该发生在任务层,不是把这个函数变成协程。
        """
        import asyncio

        return asyncio.run(
            self.extract_async(
                image=image,
                target_fields=target_fields,
                enum_defs=enum_defs,
                options=options or {},
            )
        )

    async def extract_async(
        self,
        *,
        image: Any,
        target_fields: tuple[str, ...],
        enum_defs: Mapping[str, tuple[str, ...]],
        options: dict[str, Any],
    ) -> ImageExtractionResult:
        if not self.is_configured():
            raise NotConfiguredError(
                "视觉抽取器未配置,缺少:" + ", ".join(self.config.missing_fields()),
                provider=self.name,
            )
        if not target_fields:
            raise ProviderInputError("识别目标为空,没有要问模型的字段", provider=self.name)

        audience = _audience_from(options)
        # Schema 构建自带两道闸(非视觉字段 / 受众外字段直接抛错)。
        # 让它先于任何网络调用发生 —— 一个不该问的字段连请求都不该发出去
        schema = api_ready_schema(
            build_extraction_schema(tuple(target_fields), audience=audience)
        )
        prompt = build_extraction_prompt(tuple(target_fields), audience=audience)

        prepared = await self._prepare_image(image)
        request = self._adapter.build(prompt=prompt, image=prepared, schema=schema)

        started = time.monotonic()
        response_payload, status = await self._transport.send(
            request, send_once=self._send_once
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        text = self._extract_or_fail(response_payload, status=status)
        # 元数据先于解析构造 —— HTTP 已经成功,响应 ID / 用量 / 实际模型名
        # 是事后找厂商查这次调用的全部依据,解析失败时它们不该跟着丢
        meta = self._build_metadata(
            response=response_payload, status=status, elapsed_ms=elapsed_ms
        )
        parsed = self._parse_or_explain(text)

        actual_model = str(meta.get("model") or self.config.model or "")
        logger.info(
            "attribute extraction call completed",
            extra={
                "extra_fields": {
                    "model": actual_model,
                    "api_style": self.config.api_style,
                    "http_status": status,
                    "duration_ms": elapsed_ms,
                    "fields_returned": len(parsed.fields),
                    "missing_returned": len(parsed.missing)
                    + len(parsed.unreadable_fields),
                }
            },
        )
        # model_name 用响应体里的实际值覆写,不让被审计的对象自己填 ——
        # 与评分器 `_store_metadata` 的"覆写而不是补空"同一条理由
        return replace(
            parsed,
            model_name=actual_model or None,
            prompt_version=EXTRACTION_PROMPT_VERSION,
            duration_ms=elapsed_ms,
            meta=meta,
        )

    # ---------------- 输入 ----------------

    async def _prepare_image(self, image: Any) -> PreparedImage:
        """把 `run_extraction` 递进来的东西变成可发送的图。

        接受三种形状:已备好的 `PreparedImage`(测试与将来批量层)、
        字符串地址、带 `storage_path` 的对象(`MediaAsset`)。
        其余一律拒绝 —— 悄悄 `str()` 一个 ORM 对象会把 repr 当路径去读。
        """
        if isinstance(image, PreparedImage):
            return image
        reference = None
        if isinstance(image, str):
            reference = image
        else:
            path = getattr(image, "storage_path", None)
            if isinstance(path, str) and path.strip():
                reference = path
        if not reference:
            raise ProviderInputError(
                "无法识别的图片输入形状(需要 PreparedImage / 地址字符串 / 带 storage_path 的素材)",
                provider=self.name,
            )
        return await self._resolver().resolve(
            reference,
            role="PRODUCT_IMAGE",
            prefer_public_url=self.config.send_public_urls,
        )

    # ---------------- HTTP(转发到传输层,策略只有一份)----------------

    async def _send_once(self, request: LLMRequest, client: Any = None):
        return await self._transport._send_once(request, client)

    def _error_from_status(self, status: int, body: Any, headers: Any) -> ProviderError:
        return self._transport._error_from_status(status, body, headers)

    def _retry_delay(self, attempt: int, exc: ProviderError) -> float:
        return self._transport._retry_delay(attempt, exc)

    # ---------------- 响应 ----------------

    def _parse_or_explain(self, text: str):
        """解析模型输出;失败时把**原文摘要**挂到异常上再抛。

        原文只在这里有作用域 —— `extract()` 之外谁都拿不到它。不在这一层
        挂上去的话,服务层那条 catch-all 就只剩一个类型名可记,
        而"模型到底说了什么"是坏 JSON 唯一能查的东西。

        挂在 `detail` 上而不是新开一个属性:`ExtractionParseError.detail`
        的文档字符串写的就是"让识别记录里留得下到底哪里不合契约",
        这正是那件事的原文版。摘要已经过 `redact_for_log` 抹查询串 + 截断。
        """
        try:
            return parse_extraction_payload(text)
        except ExtractionParseError as exc:
            exc.detail = {
                **exc.detail,
                "raw_excerpt": redact_for_log(text),
                "raw_length": len(text),
            }
            raise

    def _extract_or_fail(self, payload: dict[str, Any], *, status: int) -> str:
        refusal = find_refusal(payload)
        if refusal:
            raise ProviderContentSafetyError(
                self._transport._error_message("模型拒绝作答", status=status, extra=refusal),
                provider=self.name,
                detail=self._transport._error_detail(status=status),
            )
        finish = find_finish_reason(payload).lower()
        if finish in ("content_filter", "sensitive"):
            raise ProviderContentSafetyError(
                self._transport._error_message("命中内容安全策略", status=status, extra=finish),
                provider=self.name,
                detail=self._transport._error_detail(status=status),
            )
        if finish in ("length", "max_output_tokens", "max_tokens"):
            # 截断的 JSON 一定解析失败。在这里说清楚是被截断、该调哪个配置,
            # 而不是让 parser 报一句含混的"不是合法 JSON"
            raise ProviderInputError(
                self._transport._error_message(
                    "输出被截断,请调大 EXTRACTOR_MODEL_MAX_OUTPUT_TOKENS",
                    status=status,
                    extra=finish,
                ),
                provider=self.name,
                detail=self._transport._error_detail(status=status),
            )
        text = self._adapter.extract_text(payload)
        if not text.strip():
            raise ProviderInputError(
                self._transport._error_message("模型返回了空响应", status=status),
                provider=self.name,
                detail=self._transport._error_detail(status=status),
            )
        return text

    def _build_metadata(
        self, *, response: dict[str, Any], status: int, elapsed_ms: int
    ) -> dict[str, Any]:
        """可安全留档的响应元信息。刻意不碰解析 —— 解析失败时它们不该丢。"""
        usage = response.get("usage")
        return {
            "api_style": self.config.api_style,
            "response_format": self.config.response_format,
            "http_status": status,
            "response_id": response.get("id"),
            "model": str(response.get("model") or self.config.model or ""),
            "finish_reason": find_finish_reason(response) or None,
            "usage": usage if isinstance(usage, dict) else None,
            "duration_ms": elapsed_ms,
        }


def _audience_from(options: Mapping[str, Any] | None):
    """本次识别的受众。取不到就是 None(全集枚举)——

    与评分器的 `audience_from` 同一条降级思路:受众读失败退回全集,
    不让整次识别停摆;真正的受众过滤(哪些**字段**进目标)在
    `run_extraction` 里早就做完了,这里只影响枚举取值的收窄。
    """
    from app.core.enums import Audience

    raw = (options or {}).get(AUDIENCE_KEY)
    if isinstance(raw, Audience):
        return raw
    try:
        return Audience(str(raw).upper()) if raw else None
    except ValueError:
        return None
