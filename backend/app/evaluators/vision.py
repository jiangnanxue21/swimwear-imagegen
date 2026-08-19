"""真实视觉大模型评分器(需求第十章)。

一个评分器,四种后端:OpenAI Responses、OpenAI 兼容 Chat Completions、
火山方舟豆包、阿里云百炼千问 VL。它们的差别只有两处 —— 请求形状和响应形状 ——
所以这里只按 ``VISION_MODEL_API_STYLE`` 分两个适配器,**不按厂商名分支**。
厂商名一旦进了业务判断,换一家就要改代码,而换一家恰恰是这个模块存在的理由。

三条不能动的规矩,和 Mock 评分器完全一致:

1. 模型输出一律过 ``parse_evaluator_payload``,解析不了就抛错,不给默认分。
   评分器坏掉时静默给 80 分,会让烂图直接自动通过上网站 —— 最坏的失败模式。
2. ``overall_score`` 由后端按权重重算,模型自报的数只留档看漂移。
3. 等级和动作由 ``grade_candidate`` 决定,提示词里明确要求模型不要输出这些。

日志与 ``raw_response`` 里**不出现** API Key、图片字节、base64、完整请求体。
可以出现的是:模型名、响应 ID、token 用量、finish reason、HTTP 状态码、解析后的分数。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from app.core.enums import Audience, EvaluationDepth
from app.core.logging import get_logger
from app.core.vocab import (
    DEFAULT_VISION_MAX_OUTPUT_TOKENS,
    MAX_VISION_MAX_OUTPUT_TOKENS,
)
from app.evaluators.base import ImageQualityEvaluator
from app.evaluators.vision_schema import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_SYSTEM_PROMPT_MEN,
    build_response_schema,
    build_user_prompt,
    response_format_name,
)
from app.llm import endpoint_trust
from app.llm.images import ImageResolver, PreparedImage, compact_prepared_image
from app.llm.transport import (
    LLMRequest,
    MultimodalClient,
    canonical_json_bytes,
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

#: rule_set 里携带调用上下文的键。下划线开头表示"这不是规则,是这一次调用的环境" ——
#: 这样传是为了不改动 ImageQualityEvaluator.evaluate 的公开签名(所有评分器共用)。
DEPTH_KEY = "_evaluation_depth"
#: 运营在设置页编辑过的系统提示词。由 evaluation_service 从库里取好放进来 ——
#: 评分器跑在 Celery worker 里,让它自己连库会把评分层和数据库耦死,
#: 也会让所有测试都需要一个数据库。
PROMPT_KEY = "_system_prompt"
#: 本次评分的受众与规则包启用的硬错误码(PRD v2 §12.5 / §14.4)。
#:
#: 走 rule_set 传,与 PROMPT_KEY / DEPTH_KEY 同一条通道:evaluate() 的签名是
#: 所有评分器共用的公开接口,不该为受众改动它;而且评分器跑在 worker 里,
#: 必须能被无库测试,不能自己连库去查商品受众。
AUDIENCE_KEY = "_audience"
ENABLED_CODES_KEY = "_enabled_hard_fail_codes"

#: 由占位请求算出真实的非图片开销后,再留一点序列化余量。不是预留 1MB 这种
#: 拍脑袋常量:提示词与 Schema 变大时,占位请求会跟着变大。
REQUEST_BODY_SAFETY_MARGIN_BYTES = 16 * 1024
#: Base64 每 3 字节变 4 字符;再扣一点 padding 与 data URL MIME 前缀余量。
ENCODED_TO_RAW_BUDGET_RATIO = 0.74
CANDIDATE_BUDGET_PERCENT = 55
MAX_REQUEST_FIT_PASSES = 3

#: 判定为「输出被截断」的 finish_reason。
#:
#: 裸 `"incomplete"` 是 Responses API 的形状:`status="incomplete"` 但
#: `incomplete_details` 缺失或不带 reason 时,`find_finish_reason` 就返回它
#: (见 llm/transport.py)。漏掉这一项的表现很误导 —— 截断会一路掉到下面的
#: 空输出分支,报成「上游返回空输出」,把人引去查网络和鉴权,
#: 而真正该调的是 token 上限。
TRUNCATED_FINISH_REASONS = ("length", "max_output_tokens", "max_tokens", "incomplete")

#: 已经算「不必再降」的推理档位。停在这里的意义是别给一条做不到的建议。
LOW_REASONING_EFFORTS = ("low", "none", "minimal", "")


def recommended_max_output_tokens(current: int) -> int:
    """截断之后,建议把 `VISION_MODEL_MAX_OUTPUT_TOKENS` 调到多少。

    唯一的硬要求:**结果要么严格大于当前值,要么就别把它说成一条建议**。
    这里以前恒返回 `DEFAULT_VISION_MAX_OUTPUT_TOKENS`(8192),而 8192 正是
    默认值 —— 于是最常见的那次截断(默认配置跑 FULL 评分被截)给出的
    指示是「当前 8192,建议设为 8192」。运营照做,什么都没变,再截一次。

    低于默认值先抬到默认值;到了默认值之上翻倍递进,并封在设置页允许的
    上限上 —— 建议一个表单填不进去的数,和不给建议是一回事。
    """
    try:
        current = int(current)
    except (TypeError, ValueError):
        return DEFAULT_VISION_MAX_OUTPUT_TOKENS
    if current < DEFAULT_VISION_MAX_OUTPUT_TOKENS:
        return DEFAULT_VISION_MAX_OUTPUT_TOKENS
    return min(MAX_VISION_MAX_OUTPUT_TOKENS, current * 2)


def audience_from(rule_set: dict | None) -> Audience | None:
    """本次评分的受众。取不到就是 None —— 与提示词同一条降级思路:
    受众读失败退回受众前时代的行为(全集码 + 女装提示词),不让整批评分停摆。

    模块级函数而不是类方法:它不依赖任何实例状态,而挂在类上会让
    `system_prompt` 这类 staticmethod 只能写类名去调自己 —— 那正是
    这一版最初写错的地方(类名写成了另一个)。
    """
    raw = (rule_set or {}).get(AUDIENCE_KEY)
    try:
        return Audience(str(raw).upper()) if raw else None
    except ValueError:
        return None


def enabled_codes_from(rule_set: dict | None) -> list[str] | None:
    """规则包启用的硬错误码。空/缺失 -> None(退回全集)。"""
    raw = (rule_set or {}).get(ENABLED_CODES_KEY)
    if not raw:
        return None
    return [str(c).upper() for c in raw]


PROMPT_VERSION_KEY = "_system_prompt_version"

#: 本次评分用的是**哪一份**提示词(`vision_system_prompt` / `..._men`)。
#:
#: 单有 `PROMPT_VERSION_KEY` 归不了属:版本号是 `max(version) WHERE key=?` 按 key
#: **各自**自增的,于是女装 v3 与男装 v3 都写成 `"3"`。BE-302 的调用统计要按
#: (key, version) 聚合,只有版本号那一列的话,两份提示词的调用次数会加在一起 ——
#: 而那正是这张统计要回答的问题的反面(「换了提示词之后失败率有没有下降」)。
#:
#: 与 PROMPT_KEY / PROMPT_VERSION_KEY 同一条通道下发,取值仍然只由
#: `vision_schema.prompt_key_for` 一处推导 —— 归属抄一份的话,选键改了而
#: 统计的归属不跟着改,两边都不会红。
PROMPT_KEY_NAME = "_system_prompt_key"

# 这里原来有一份 RETRYABLE_STATUS / MAX_RETRY_AFTER_SECONDS 的本地定义,
# 取值与 `app.llm.transport` 那份一模一样,而文件顶部也 import 了它们。
# ruff 拆开看之后结论比"重复定义"更干脆:**两份都没有被这个模块用过**。
# 重试与 Retry-After 的处理全在 transport 层,vision 只是调用方。
# 所以本地定义和那两个 import 一起删掉,而不是留一个"看起来在用"的形状。
# (v4.1 Phase 0 由 ruff F811 + F401 揪出来;ruff 之前从没在流程里真跑过)

#: 判定本体在 `app/llm/endpoint_trust.py`(A45-batch14 抽出,识别层要用
#: 一字不差的同一套)。这里保留两个旧名字:`tests/pure/test_a45_batch8_fixes.py`
#: 直接从本模块 import `_is_private_host` 断言 A45-#36 的行为 —— 那条测试
#: 钉的是行为,行为没变,不该让它跟着搬家改一遍 import。
# A45-#36:`_PRIVATE_PREFIXES` 已删。它做的是主机名前缀匹配,
# 而那会把 `10.gpu.example.com` 判成私网。判定改走 `_is_private_host()`,
# 用 `ipaddress` 解析字面量 IP —— 顺带覆盖了 172.16/12 整段与 IPv6,
# 原来那串手写前缀只列到 172.19,本身就是不全的。
_LOCAL_HOSTNAMES = endpoint_trust.LOCAL_HOSTNAMES
_is_private_host = endpoint_trust.is_private_host


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class VisionEvaluatorConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    api_style: str = API_STYLE_RESPONSES
    response_format: str = FORMAT_JSON_SCHEMA
    timeout_seconds: float = 90.0
    max_image_bytes: int = 8 * 1024 * 1024
    max_request_bytes: int = 5 * 1024 * 1024
    max_reference_images: int = 4
    max_output_tokens: int = DEFAULT_VISION_MAX_OUTPUT_TOKENS
    max_retries: int = 2
    retry_base_seconds: float = 0.5
    full_image_detail: str = "high"
    quick_image_detail: str = "low"
    reasoning_effort: str = "low"
    send_public_urls: bool = False
    fail_closed: bool = True
    #: 显式声明端点无鉴权。自建但挂公网域名时用它,不必迁就主机名启发式
    allow_anonymous: bool = False
    allowed_hosts: str = ""

    @classmethod
    def from_settings(cls) -> VisionEvaluatorConfig:
        style = (
            (provider_setting("VISION_MODEL_API_STYLE", API_STYLE_RESPONSES) or "").strip().lower()
        )
        fmt = (
            (provider_setting("VISION_MODEL_RESPONSE_FORMAT", FORMAT_JSON_SCHEMA) or "")
            .strip()
            .lower()
        )
        return cls(
            api_key=provider_setting("VISION_MODEL_API_KEY"),
            base_url=provider_setting("VISION_MODEL_BASE_URL"),
            model=provider_setting("VISION_MODEL_NAME", "") or "",
            api_style=style if style in API_STYLES else API_STYLE_RESPONSES,
            response_format=fmt if fmt in RESPONSE_FORMATS else FORMAT_JSON_SCHEMA,
            timeout_seconds=provider_float("VISION_MODEL_TIMEOUT_SECONDS", 90.0),
            max_image_bytes=provider_int("VISION_MODEL_MAX_IMAGE_MB", 8) * 1024 * 1024,
            max_request_bytes=provider_int("VISION_MODEL_MAX_REQUEST_MB", 5) * 1024 * 1024,
            max_reference_images=provider_int("VISION_MODEL_MAX_REFERENCE_IMAGES", 4),
            max_output_tokens=provider_int(
                "VISION_MODEL_MAX_OUTPUT_TOKENS", DEFAULT_VISION_MAX_OUTPUT_TOKENS
            ),
            max_retries=provider_int("VISION_MODEL_MAX_RETRIES", 2),
            retry_base_seconds=provider_float("VISION_MODEL_RETRY_BASE_SECONDS", 0.5),
            full_image_detail=(provider_setting("VISION_MODEL_FULL_IMAGE_DETAIL", "high") or "high")
            .strip()
            .lower(),
            quick_image_detail=(provider_setting("VISION_MODEL_QUICK_IMAGE_DETAIL", "low") or "low")
            .strip()
            .lower(),
            reasoning_effort=(provider_setting("VISION_MODEL_REASONING_EFFORT", "low") or "low")
            .strip()
            .lower(),
            send_public_urls=provider_flag("VISION_MODEL_SEND_PUBLIC_URLS", False),
            fail_closed=provider_flag("VISION_MODEL_FAIL_CLOSED", True),
            allow_anonymous=provider_flag("VISION_MODEL_ALLOW_ANONYMOUS", False),
            allowed_hosts=provider_setting("DOWNLOAD_ALLOWED_HOSTS"),
        )

    @property
    def requires_api_key(self) -> bool:
        """这个端点需不需要 Key。

        ``VISION_MODEL_ALLOW_ANONYMOUS=true`` 是显式声明"这个端点不鉴权",优先级最高。
        没显式声明时按主机猜 —— 但猜错的方向是**多要一个 Key**,不是少要。


        判据是主机:回环、私网、以及没有点的容器服务名(``http://ollama:11434``)
        算自建,允许无鉴权;其余一律要求 Key。

        ## A45-#36:私网判定必须先解析成 IP,不能对主机名做前缀匹配

        原来是 `host.startswith(("10.", "192.168.", "172.16."…))`,而 `host`
        是**主机名**不是 IP。于是 `10.gpu.example.com` 这样一个完全公网的域名
        被判成自建 -> 无 Key 也 `configured=True` -> 评分器被选中 -> 每次调用
        都不带 `Authorization`,一路 401。

        **失败方向和上面那段自述正好相反**:那里写着"猜错的方向是多要一个 Key",
        而这一支猜错的方向是少要一个 Key,并且把凭据缺失伪装成配置完成。


        反过来做 —— 默认所有端点都不需要 Key —— 的后果是:忘了配 Key 时
        ``is_configured()`` 返回 True,评分器被选中,然后每一张候选图都因为 401 失败,
        而界面上显示的是"已配置"。宁可在配置期就说缺 Key。

        实现在 `llm/endpoint_trust.endpoint_requires_api_key`(A45-batch14 抽出,
        识别层用同一份)。这里只剩委托 —— 本地再写一遍就是把 A45-#36 留在
        第二份实现里等着复发。
        """
        return endpoint_trust.endpoint_requires_api_key(
            self.base_url, allow_anonymous=self.allow_anonymous
        )

    @property
    def configured(self) -> bool:
        if not self.base_url.strip() or not self.model.strip():
            return False
        return not (self.requires_api_key and not self.api_key.strip())

    def image_detail(self, depth: EvaluationDepth) -> str:
        return self.quick_image_detail if depth is EvaluationDepth.QUICK else self.full_image_detail

    def missing_fields(self) -> list[str]:
        missing = []
        if not self.base_url.strip():
            missing.append("VISION_MODEL_BASE_URL")
        if not self.model.strip():
            missing.append("VISION_MODEL_NAME")
        if self.requires_api_key and not self.api_key.strip():
            missing.append("VISION_MODEL_API_KEY")
        return missing


# --------------------------------------------------------------------------
# 纯函数:端点拼接、响应文本提取、错误归类
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 请求适配器
# --------------------------------------------------------------------------


#: 传输层的请求容器。实现在 `app/llm/transport.py`,这里只是个别名。
#:
#: 保留这个名字是因为它是评分器模块的公开契约:适配器返回它,
#: `tests/pure/test_vision_evaluator.py` 直接断言它的字段。M2 是一次
#: **纯重构**,验收判据里明确写着「现有测试零修改全绿」——
#: 让调用方跟着改 import,就等于用测试的改动量掩盖了行为可能已经变了。
VisionRequest = LLMRequest


class _Adapter:
    """请求适配器基类。按 API Style 选,不按厂商选。"""

    style: str = ""

    def __init__(self, config: VisionEvaluatorConfig) -> None:
        self.config = config

    def endpoint(self) -> str:
        raise NotImplementedError

    def build(
        self,
        *,
        depth: EvaluationDepth,
        system_prompt: str,
        user_prompt: str,
        references: list[PreparedImage],
        candidate: PreparedImage,
        audience: Audience | None = None,
        allowed_hard_fail_codes: Sequence[str] | None = None,
    ) -> VisionRequest:
        raise NotImplementedError

    def extract_text(self, payload: dict[str, Any]) -> str:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key.strip():
            headers["Authorization"] = f"Bearer {self.config.api_key.strip()}"
        return headers

    @staticmethod
    def _ordered_images(
        references: list[PreparedImage], candidate: PreparedImage
    ) -> list[PreparedImage]:
        return [*references, candidate]


class ResponsesAdapter(_Adapter):
    """OpenAI Responses API,以及兼容它的端点(含火山方舟)。"""

    style = API_STYLE_RESPONSES

    def endpoint(self) -> str:
        return normalize_endpoint(self.config.base_url, "responses")

    def build(
        self,
        *,
        depth: EvaluationDepth,
        system_prompt: str,
        user_prompt: str,
        references: list[PreparedImage],
        candidate: PreparedImage,
        audience: Audience | None = None,
        allowed_hard_fail_codes: Sequence[str] | None = None,
    ) -> VisionRequest:
        detail = self.config.image_detail(depth)
        content: list[dict[str, Any]] = [{"type": "input_text", "text": user_prompt}]

        # 顺序是有意义的:先标签后图片,模型才知道哪张是哪张。
        # 参考图全部在前,候选图最后 —— 提示词里也是这么描述的,两边必须一致。
        for index, image in enumerate(references, start=1):
            content.append({"type": "input_text", "text": f"REFERENCE_IMAGE_{index}"})
            content.append(
                {
                    "type": "input_image",
                    "image_url": image.payload_url(),
                    "detail": detail,
                }
            )
        content.append({"type": "input_text", "text": "CANDIDATE_IMAGE"})
        content.append(
            {"type": "input_image", "image_url": candidate.payload_url(), "detail": detail}
        )

        body: dict[str, Any] = {
            "model": self.config.model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": content},
            ],
            "max_output_tokens": self.config.max_output_tokens,
            "stream": False,
            "store": False,
        }

        if self.config.response_format == FORMAT_JSON_SCHEMA:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": response_format_name(depth, audience),
                    "strict": True,
                    "schema": build_response_schema(depth, allowed_hard_fail_codes),
                }
            }
        elif self.config.response_format == FORMAT_JSON_OBJECT:
            body["text"] = {"format": {"type": "json_object"}}

        effort = self.config.reasoning_effort
        if effort and effort != "none":
            body["reasoning"] = {"effort": effort}
        else:
            # 只在非推理模式下发 temperature:推理模型普遍拒收这个参数,
            # 无条件发送会换来一个 400,而 400 不重试 —— 整轮评分当场失败
            body["temperature"] = 0

        return VisionRequest(
            url=self.endpoint(),
            headers=self.headers(),
            body=body,
            images=self._ordered_images(references, candidate),
        )

    def extract_text(self, payload: dict[str, Any]) -> str:
        return extract_responses_text(payload)


class ChatCompletionsAdapter(_Adapter):
    """OpenAI 兼容的 Chat Completions,含阿里云百炼兼容模式。"""

    style = API_STYLE_CHAT

    def endpoint(self) -> str:
        return normalize_endpoint(self.config.base_url, "chat/completions")

    def build(
        self,
        *,
        depth: EvaluationDepth,
        system_prompt: str,
        user_prompt: str,
        references: list[PreparedImage],
        candidate: PreparedImage,
        audience: Audience | None = None,
        allowed_hard_fail_codes: Sequence[str] | None = None,
    ) -> VisionRequest:
        detail = self.config.image_detail(depth)
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]

        for index, image in enumerate(references, start=1):
            content.append({"type": "text", "text": f"REFERENCE_IMAGE_{index}"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image.payload_url(), "detail": detail},
                }
            )
        content.append({"type": "text", "text": "CANDIDATE_IMAGE"})
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": candidate.payload_url(), "detail": detail},
            }
        )

        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            "max_tokens": self.config.max_output_tokens,
            "temperature": 0,
            "stream": False,
        }

        if self.config.response_format == FORMAT_JSON_SCHEMA:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_format_name(depth, audience),
                    "strict": True,
                    "schema": build_response_schema(depth, allowed_hard_fail_codes),
                },
            }
        elif self.config.response_format == FORMAT_JSON_OBJECT:
            body["response_format"] = {"type": "json_object"}

        return VisionRequest(
            url=self.endpoint(),
            headers=self.headers(),
            body=body,
            images=self._ordered_images(references, candidate),
        )

    def extract_text(self, payload: dict[str, Any]) -> str:
        return extract_chat_text(payload)


def build_adapter(config: VisionEvaluatorConfig) -> _Adapter:
    if config.api_style == API_STYLE_CHAT:
        return ChatCompletionsAdapter(config)
    return ResponsesAdapter(config)


# --------------------------------------------------------------------------
# 评分器
# --------------------------------------------------------------------------


class VisionModelImageQualityEvaluator(ImageQualityEvaluator):
    """视觉大模型评分器。"""

    evaluator_name = "vision"
    is_simulator = False
    display_name = "视觉大模型评分器"
    #: 每一次调用都是一次付费的大模型请求。见 `ImageQualityEvaluator.billable`。
    billable = True

    @property
    def usage_provider(self) -> str:
        """计费主体。**从配置的 base_url 推,不写死。**

        一轮 4 张候选 × 3 轮 = 最多 12 次视觉调用,次数是图片生成调用的数倍,
        所以这一列填错的代价不是"少一个标签",是整块开销被归到错误的名下。

        推导规则很窄:取 host 去掉 `api.` 前缀和顶级域,`api.openai.com` -> `openai`。
        推不出来时退回评分器名,**不猜** —— 一个猜出来的厂商名会让人拿着它去
        对不存在的账单。实现在 `llm/endpoint_trust.provider_label_from_base_url`
        (A45-batch14 抽出,识别层的账要记进**同一套名字**,否则同一个端点的
        识别费和评分费会在 `/spend` 页分列在两个厂商下)。
        """
        raw = str(getattr(self.config, "base_url", "") or "")
        return endpoint_trust.provider_label_from_base_url(raw, fallback=self.evaluator_name)

    def __init__(
        self,
        config: VisionEvaluatorConfig | None = None,
        *,
        storage: Any | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.config = config or VisionEvaluatorConfig.from_settings()
        self._storage = storage
        self._adapter = build_adapter(self.config)
        # 传输层是共用的(M2):重试、退避、错误归类、客户端生命周期
        # 全在 app/llm/transport.py,识别层用的是同一个类
        self._transport = MultimodalClient(
            self.config, name=self.evaluator_name, http_client=http_client
        )

    @property
    def _http_client(self):
        """注入的 HTTP 客户端。委托给传输层,不自己再存一份。

        存两份状态迟早会不一致,而表现是"关掉的是另一个客户端"。
        """
        return self._transport._http_client

    # ---------------- 配置 ----------------

    def is_configured(self) -> bool:
        return self.config.configured

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

    async def evaluate(
        self,
        product_assets: list[str],
        candidate_image: str,
        product_metadata: dict,
        rule_set: dict,
    ) -> dict:
        if not self.is_configured():
            raise NotConfiguredError(
                "视觉大模型评分器未配置,缺少:" + ", ".join(self.config.missing_fields()),
                provider=self.evaluator_name,
            )

        depth = self._depth_from(rule_set)
        # 受众与启用码来自本商品的规则包(§14.4):男装的请求体里不该出现
        # STRAP_CHANGED —— 摆在可选列表里,模型迟早会挑一个用
        audience = audience_from(rule_set)
        allowed_codes = enabled_codes_from(rule_set)
        references, candidate = await self._prepare_images(product_assets, candidate_image)

        user_prompt = build_user_prompt(
            depth=depth,
            product_metadata=product_metadata or {},
            reference_count=len(references),
            allowed_hard_fail_codes=allowed_codes,
        )
        request = await self._fit_request_to_budget(
            depth=depth,
            audience=audience,
            allowed_hard_fail_codes=allowed_codes,
            system_prompt=self.system_prompt(rule_set),
            user_prompt=user_prompt,
            references=references,
            candidate=candidate,
        )

        started = time.monotonic()
        response_payload, status = await self._send_with_retries(request)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        # 元数据**先于输出完整性检查与解析构造**。HTTP 请求这一步已经成功了 —— 响应 ID、
        # 厂商实际路由到的模型、token 用量、耗时全都拿到了,而它们恰恰是
        # 事后拿着响应 ID 找厂商查一次异常调用的全部依据,且只在这一刻存在。
        # 截断也是一次已经计费的成功 HTTP 调用,不能只给解析失败留这些字段。
        meta = self._build_metadata(
            response=response_payload,
            request=request,
            status=status,
            depth=depth,
            elapsed_ms=elapsed_ms,
            prompt_version=(rule_set or {}).get(PROMPT_VERSION_KEY),
        )

        try:
            text = self._extract_or_fail(response_payload, status=status)
        except ProviderError as exc:
            if getattr(exc, "vision_meta", None) is None:
                exc.vision_meta = meta
            if getattr(exc, "duration_ms", None) is None:
                exc.duration_ms = elapsed_ms
            raise

        from app.evaluators.scoring import EvaluationParseError

        try:
            payload = self.parse_model_text(text)
        except EvaluationParseError as exc:
            # 只补,不覆盖:上层如果已经填过就以上层的为准
            if getattr(exc, "vision_meta", None) is None:
                exc.vision_meta = meta
            if getattr(exc, "duration_ms", None) is None:
                exc.duration_ms = elapsed_ms
            logger.warning(
                "vision response parse failed",
                extra={
                    "extra_fields": {"event": "eval.response_parse_failed",
                        "response_id": meta.get("response_id"),
                        "model": meta.get("model"),
                        "http_status": status,
                        "duration_ms": elapsed_ms,
                    }
                },
            )
            raise

        self._store_metadata(payload, meta=meta, request=request)
        return payload

    @staticmethod
    def system_prompt(rule_set: dict | None = None) -> str:
        """这一次要用的系统提示词。

        运营改过就用运营那版,没改过用内置默认。取不到也用内置默认 ——
        提示词读失败不该让整批评分停摆,那是一个可以降级的输入,
        不像"模型输出解析失败"那种必须失败关闭的情况。
        """
        value = (rule_set or {}).get(PROMPT_KEY)
        if isinstance(value, str) and value.strip():
            return value
        # 兜底也要分受众:取不到运营那版时,男装不能落到女装那份已校准的
        # 提示词上 —— 那份里一句男装检查项都没有
        if audience_from(rule_set) is Audience.MEN:
            return DEFAULT_SYSTEM_PROMPT_MEN
        return DEFAULT_SYSTEM_PROMPT

    @staticmethod
    def _depth_from(rule_set: dict) -> EvaluationDepth:
        raw = (rule_set or {}).get(DEPTH_KEY)
        if isinstance(raw, EvaluationDepth):
            return raw
        try:
            return EvaluationDepth(str(raw).upper())
        except (ValueError, AttributeError):
            return EvaluationDepth.FULL

    async def _prepare_images(
        self, product_assets: list[str], candidate_image: str
    ) -> tuple[list[PreparedImage], PreparedImage]:
        """参考图在前、候选图在后,顺序与提示词描述保持一致。"""
        if not candidate_image or not str(candidate_image).strip():
            raise ProviderInputError("候选图地址为空,无法评分", provider=self.evaluator_name)

        assets = [a for a in (product_assets or []) if a and str(a).strip()]
        if not assets:
            raise ProviderInputError(
                "没有可用的商品参考图,无法判断商品一致性",
                provider=self.evaluator_name,
            )

        resolver = self._resolver()
        limit = max(1, self.config.max_reference_images)
        # 截断而不是报错:参考图多是好事,超出上限时保留前几张(顺序即重要性)
        selected = assets[:limit]
        if len(assets) > len(selected):
            # 静默截断会让人以为八张都发了。配了没生效,而且无从得知
            logger.warning(
                "reference images truncated to the configured limit",
                extra={
                    "extra_fields": {"event": "eval.reference_images_truncated",
                        "provided": len(assets),
                        "sent": len(selected),
                        "limit": limit,
                    }
                },
            )
        references = [
            await resolver.resolve(
                asset,
                role=f"REFERENCE_IMAGE_{index}",
                prefer_public_url=self.config.send_public_urls,
            )
            for index, asset in enumerate(selected, start=1)
        ]
        candidate = await resolver.resolve(
            str(candidate_image),
            role="CANDIDATE_IMAGE",
            prefer_public_url=self.config.send_public_urls,
        )
        return references, candidate

    def _build_request(
        self,
        *,
        depth: EvaluationDepth,
        audience: Audience | None,
        allowed_hard_fail_codes: Sequence[str] | None,
        system_prompt: str,
        user_prompt: str,
        references: list[PreparedImage],
        candidate: PreparedImage,
    ) -> VisionRequest:
        return self._adapter.build(
            depth=depth,
            audience=audience,
            allowed_hard_fail_codes=allowed_hard_fail_codes,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            references=references,
            candidate=candidate,
        )

    @staticmethod
    def _request_body_bytes(request: VisionRequest) -> int:
        # 和 `_send_once` 用同一个序列化器。自己再写一遍 json.dumps 的表现是:
        # 参数哪天分叉了,预算检查会继续给出一个"通过",而线上是 413
        return len(canonical_json_bytes(request.body))

    @staticmethod
    def _placeholder(image: PreparedImage) -> PreparedImage:
        if not image.inline:
            return image
        return PreparedImage(
            role=image.role,
            mime_type=image.mime_type,
            byte_size=0,
            data_url=f"data:{image.mime_type};base64,",
            source_byte_size=image.source_byte_size,
        )

    @staticmethod
    def _allocate_inline_budgets(
        images: list[PreparedImage], total_raw_bytes: int
    ) -> dict[str, int]:
        """候选图优先,并把小图没用完的预算自动归还给其他图片。

        公网 URL 不在 ``images`` 里,不会因为“图片数量”被误分走请求体预算。
        候选图先占图片预算的 55%,全部参考图共享 45%;若任一原图本来就小于
        它的份额,按真实大小结算,空余再次按权重分配。
        """
        if not images or total_raw_bytes <= 0:
            return {}
        references = [image for image in images if image.role != "CANDIDATE_IMAGE"]
        weights = {
            image.role: (
                Fraction(CANDIDATE_BUDGET_PERCENT, 100)
                if image.role == "CANDIDATE_IMAGE"
                else Fraction(
                    100 - CANDIDATE_BUDGET_PERCENT,
                    100 * max(1, len(references)),
                )
            )
            for image in images
        }
        unresolved = {image.role: image for image in images}
        budgets: dict[str, int] = {}
        remaining = total_raw_bytes
        while unresolved:
            weight_total = sum(weights[role] for role in unresolved)
            settled = []
            for role, image in unresolved.items():
                share = int(remaining * weights[role] / weight_total)
                if image.byte_size <= share:
                    budgets[role] = image.byte_size
                    remaining -= image.byte_size
                    settled.append(role)
            if not settled:
                roles = list(unresolved)
                assigned = 0
                for role in roles[:-1]:
                    share = max(1, int(remaining * weights[role] / weight_total))
                    budgets[role] = share
                    assigned += share
                budgets[roles[-1]] = max(1, remaining - assigned)
                break
            for role in settled:
                unresolved.pop(role)
        return budgets

    async def _fit_request_to_budget(
        self,
        *,
        depth: EvaluationDepth,
        audience: Audience | None,
        allowed_hard_fail_codes: Sequence[str] | None,
        system_prompt: str,
        user_prompt: str,
        references: list[PreparedImage],
        candidate: PreparedImage,
    ) -> VisionRequest:
        """先发原图形状;确实超限时才压缩,并按真实 JSON 二次收敛。"""
        build_kwargs = {
            "depth": depth,
            "audience": audience,
            "allowed_hard_fail_codes": allowed_hard_fail_codes,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        }
        request = self._build_request(
            **build_kwargs, references=references, candidate=candidate
        )
        original_size = self._request_body_bytes(request)
        if original_size <= self.config.max_request_bytes:
            return request

        all_images = [*references, candidate]
        inline_images = [image for image in all_images if image.inline]
        if not inline_images:
            self._ensure_request_fits(request)

        placeholder_refs = [self._placeholder(image) for image in references]
        placeholder_candidate = self._placeholder(candidate)
        placeholder_request = self._build_request(
            **build_kwargs,
            references=placeholder_refs,
            candidate=placeholder_candidate,
        )
        encoded_budget = (
            self.config.max_request_bytes
            - self._request_body_bytes(placeholder_request)
            - REQUEST_BODY_SAFETY_MARGIN_BYTES
        )
        if encoded_budget <= 0:
            raise ProviderInputError(
                "VISION_MODEL_MAX_REQUEST_MB 太小,不足以容纳评分提示词和响应 Schema",
                provider=self.evaluator_name,
                detail={"limit_bytes": self.config.max_request_bytes},
            )
        raw_budget = max(1, int(encoded_budget * ENCODED_TO_RAW_BUDGET_RATIO))

        by_role = {image.role: image for image in inline_images}
        fitted_request = request
        for _attempt in range(MAX_REQUEST_FIT_PASSES):
            budgets = self._allocate_inline_budgets(inline_images, raw_budget)
            compacted: dict[str, PreparedImage] = {}
            for role, image in by_role.items():
                compacted[role] = await asyncio.to_thread(
                    compact_prepared_image,
                    image,
                    max_bytes=budgets[role],
                )
            fitted_refs = [compacted.get(image.role, image) for image in references]
            fitted_candidate = compacted.get(candidate.role, candidate)
            fitted_request = self._build_request(
                **build_kwargs,
                references=fitted_refs,
                candidate=fitted_candidate,
            )
            fitted_size = self._request_body_bytes(fitted_request)
            if fitted_size <= self.config.max_request_bytes:
                logger.info(
                    "vision request fitted to endpoint body budget",
                    extra={
                        "extra_fields": {"event": "llm.request_fitted",
                            "original_body_bytes": original_size,
                            "fitted_body_bytes": fitted_size,
                            "limit_bytes": self.config.max_request_bytes,
                            "inline_count": len(inline_images),
                        }
                    },
                )
                return fitted_request
            # 始终从原始 PreparedImage 重新编码,不对上一轮 JPEG 再压一次。
            # 真实超出比例决定下一轮预算,再留 3% 收敛余量。
            raw_budget = max(
                1,
                int(
                    raw_budget
                    * self.config.max_request_bytes
                    / fitted_size
                    * 0.97
                ),
            )

        self._ensure_request_fits(fitted_request)
        return fitted_request  # pragma: no cover - 上一行只在符合预算时返回

    def _ensure_request_fits(self, request: VisionRequest) -> None:
        """发送前按实际 JSON 复核,不把已知超限请求交给厂商换一个 400。

        「实际」这个词现在是字面意思:`canonical_json_bytes` 的返回值就是
        `_send_once` 原样 `content=` 发出去的那一串。
        """
        body_bytes = len(canonical_json_bytes(request.body))
        if body_bytes <= self.config.max_request_bytes:
            return
        raise ProviderInputError(
            f"视觉评分请求体 {body_bytes // 1024} KB,超过配置总上限 "
            f"{self.config.max_request_bytes // 1024} KB;"
            "请减少参考图或启用 VISION_MODEL_SEND_PUBLIC_URLS",
            provider=self.evaluator_name,
            detail={
                "request_body_bytes": body_bytes,
                "limit_bytes": self.config.max_request_bytes,
                "image_count": len(request.images),
                "inline_count": sum(1 for image in request.images if image.inline),
            },
        )

    # ---------------- HTTP ----------------

    async def _send_with_retries(self, request: VisionRequest) -> tuple[dict[str, Any], int]:
        """发送并重试。**退避策略在 `app/llm/transport.py`(M2),这里一行都没有。**

        把 `self._send_once` 传下去,是为了让子类覆写单次往返仍然生效 ——
        `tests/pure/test_vision_evaluator.py` 的 `_ScriptedEvaluator` 正是
        这么测重试策略的:它要断言的是"重试了几次、什么错不该重试",
        而不是 httpx 的行为。
        """
        return await self._transport.send(request, send_once=self._send_once)

    async def _send_once(self, request: VisionRequest, client: Any = None):
        """一次往返。实现在传输层,这里只是个可覆写的钩子。"""
        return await self._transport._send_once(request, client)

    # 错误归类的三个方法同样只是转发。
    #
    # 保留它们是因为 `tests/pure/test_vision_evaluator.py` 直接在评分器实例上
    # 调用 `_error_from_status` 来断言"429 归限流、403 归鉴权、
    # 提示 json_schema 不支持时要建议降级"这一整套映射 —— 那些断言测的是
    # **行为**,而 M2 是一次纯重构,行为一个字都不该变。
    #
    # 实现只有一份,在 `app/llm/transport.py`。

    def _error_from_status(self, status: int, body: Any, headers: Any) -> ProviderError:
        return self._transport._error_from_status(status, body, headers)

    def _error_message(self, headline: str, *, status: int | None, extra: str = "") -> str:
        return self._transport._error_message(headline, status=status, extra=extra)

    def _error_detail(self, *, status: int | None) -> dict[str, Any]:
        return self._transport._error_detail(status=status)

    def _retry_delay(self, attempt: int, exc: ProviderError) -> float:
        """退避时长。同样只是转发 —— 测试直接断言"Retry-After 被尊重但有上限"。"""
        return self._transport._retry_delay(attempt, exc)

    # ---------------- 响应 ----------------

    def _extract_or_fail(self, payload: dict[str, Any], *, status: int) -> str:
        refusal = find_refusal(payload)
        if refusal:
            raise ProviderContentSafetyError(
                self._error_message("模型拒绝作答", status=status, extra=refusal),
                provider=self.evaluator_name,
                detail=self._error_detail(status=status),
            )

        finish = find_finish_reason(payload).lower()
        if finish in ("content_filter", "sensitive"):
            raise ProviderContentSafetyError(
                self._error_message("命中内容安全策略", status=status, extra=finish),
                provider=self.evaluator_name,
                detail=self._error_detail(status=status),
            )
        if finish in TRUNCATED_FINISH_REASONS:
            # 截断的 JSON 一定解析失败。与其让它在 parser 里炸出一个含混的
            # "不是合法 JSON",不如在这里说清楚是被截断了、该调哪个配置
            current_tokens = self.config.max_output_tokens
            recommended = recommended_max_output_tokens(current_tokens)
            effort = (self.config.reasoning_effort or "").strip().lower()
            detail = {
                **self._error_detail(status=status),
                "finish_reason": finish,
                "configured_max_output_tokens": current_tokens,
                "configured_reasoning_effort": self.config.reasoning_effort,
                "recommended_max_output_tokens": recommended,
                #: 建议值封在这里。写进 detail 是为了让前端能解释
                #: "为什么不建议再调大" —— 否则到顶之后那条建议看起来像漏了
                "max_output_tokens_ceiling": MAX_VISION_MAX_OUTPUT_TOKENS,
                "automatic_retry": False,
            }
            if recommended > current_tokens:
                advice = (
                    "建议把 VISION_MODEL_MAX_OUTPUT_TOKENS "
                    f"从 {current_tokens} 调到 {recommended}"
                )
            else:
                # 已经顶到设置页允许的上限。这时候再说"调大"就是一条执行不了的
                # 建议 —— 表单会直接打回。剩下能动的只有推理档位和评分深度。
                advice = (
                    f"VISION_MODEL_MAX_OUTPUT_TOKENS 已是允许的上限 "
                    f"{MAX_VISION_MAX_OUTPUT_TOKENS},无法再调大"
                )
            if effort not in LOW_REASONING_EFFORTS:
                # 只在真的还能降的时候才说。已经是 low/none 还建议"降到 low/none",
                # 和上面那条"把 8192 改成 8192"是同一类空话
                advice += f";也可把 VISION_MODEL_REASONING_EFFORT 从 {effort} 降到 low,腾出输出预算"
            raise ProviderInputError(
                self._error_message(
                    f"输出被截断;当前 VISION_MODEL_MAX_OUTPUT_TOKENS={current_tokens}。"
                    f"{advice}。"
                    "系统未自动重试,避免产生第二次模型费用",
                    status=status,
                    extra=finish,
                ),
                provider=self.evaluator_name,
                detail=detail,
            )

        text = self._adapter.extract_text(payload)
        if not text.strip():
            raise ProviderInputError(
                self._error_message(
                    "上游返回空输出", status=status, extra=finish or "无 finish_reason"
                ),
                provider=self.evaluator_name,
                detail=self._error_detail(status=status),
            )
        return text

    def _build_metadata(
        self,
        *,
        response: dict[str, Any],
        request: VisionRequest,
        status: int,
        depth: EvaluationDepth,
        elapsed_ms: int,
        prompt_version: int | None = None,
    ) -> dict[str, Any]:
        """从**已经拿到**的响应里提取可安全留档的元信息。

        刻意不碰 payload:这一步必须能在解析成功之前完成,否则解析失败时
        这些字段就一起丢了。挂载由 `_attach_metadata` 负责。

        取值优先响应体里的 ``model``(厂商实际路由到的那一个,可能和请求里写的
        不同),拿不到才退回配置里的模型名。
        """
        actual_model = str(response.get("model") or self.config.model or "")
        usage = response.get("usage")
        return {
            "api_style": self.config.api_style,
            "response_format": self.config.response_format,
            "http_status": status,
            "response_id": response.get("id"),
            "model": actual_model,
            "finish_reason": find_finish_reason(response) or None,
            "usage": usage if isinstance(usage, dict) else None,
            "depth": depth.value,
            # 三个月后要解释"这批图当时为什么判 C",靠的就是这个版本号
            "prompt_version": prompt_version,
            "duration_ms": elapsed_ms,
            "images": [image.describe() for image in request.images],
        }

    def _attach_metadata(
        self,
        payload: dict[str, Any],
        *,
        response: dict[str, Any],
        request: VisionRequest,
        status: int,
        depth: EvaluationDepth,
        elapsed_ms: int,
        prompt_version: int | None = None,
    ) -> None:
        """构造元数据并挂到 payload 上。签名保持不变,内部走 `_build_metadata`。"""
        self._store_metadata(
            payload,
            meta=self._build_metadata(
                response=response,
                request=request,
                status=status,
                depth=depth,
                elapsed_ms=elapsed_ms,
                prompt_version=prompt_version,
            ),
            request=request,
        )

    def _store_metadata(
        self,
        payload: dict[str, Any],
        *,
        meta: dict[str, Any],
        request: VisionRequest,
    ) -> None:
        """用真实响应的 model 覆写 model_name,并挂一份可安全留档的元信息。

        **覆写而不是补空。** 以前的做法是「模型没填才填」,于是模型填一个错名字
        就能让审计记录指向一个根本没参与评分的模型 —— 而这个字段的全部用途
        就是回答「这批图当时是谁打的分」。让被审计的对象自己填这个字段,
        等于没有审计。

        取值优先响应体里的 ``model``(厂商实际路由到的那一个,可能和请求里写的
        不同),拿不到才退回配置里的模型名。评分字段本身一个都不动。

        写进 ``_vision_meta`` 这个后端专属命名空间,而不是和模型输出平铺在一起:
        解析层据此就能分辨"这个值是我们自己写的"还是"模型说的",不必靠约定。
        """
        actual_model = str(meta.get("model") or "")
        # 模型如果自作主张输出了 model_name,直接丢掉 —— 它不该有发言权
        payload.pop("model_name", None)
        payload["_vision_meta"] = meta

        logger.info(
            "vision evaluation completed",
            extra={
                "extra_fields": {"event": "eval.completed",
                    "model": actual_model or self.config.model,
                    "api_style": self.config.api_style,
                    "depth": meta.get("depth"),
                    "http_status": meta.get("http_status"),
                    "duration_ms": meta.get("duration_ms"),
                    "image_count": len(request.images),
                }
            },
        )

    @staticmethod
    def parse_model_text(text: str) -> dict:
        """把模型返回的文本解析成评分 dict。散文和半截 JSON 会在这里被拒绝。"""
        from app.evaluators.scoring import _coerce_payload

        return _coerce_payload(text)

    def build_prompt(
        self, product_metadata: dict, rule_set: dict, *, reference_count: int = 1
    ) -> str:
        """完整提示词(系统 + 用户),供调试脚本和提示词预览使用。

        ``reference_count`` 是真参数,不再写死 —— 预览页要能看到"发三张参考图时
        提示词长什么样",写死 1 的预览和实际发出去的不是一个东西。
        """
        depth = self._depth_from(rule_set)
        user = build_user_prompt(
            depth=depth,
            product_metadata=product_metadata or {},
            reference_count=max(1, reference_count),
            allowed_hard_fail_codes=enabled_codes_from(rule_set),
        )
        return f"{self.system_prompt(rule_set)}\n\n{user}"

    # ---------------- 连接测试 ----------------

    async def test_connection(self) -> dict[str, Any]:
        """发一个最小请求确认端点、鉴权和模型名都对。

        用内存里的 8×8 图,不碰任何真实商品图 ——
        连接测试是运维随手点的按钮,不该产生业务级的推理开销。
        """
        base: dict[str, Any] = {
            "evaluator": self.evaluator_name,
            "configured": self.is_configured(),
            "reachable": False,
            "model": self.config.model,
            "api_style": self.config.api_style,
            "latency_ms": None,
            "message": "",
        }
        if not self.is_configured():
            base["message"] = "未配置,缺少:" + ", ".join(self.config.missing_fields())
            return base

        started = time.monotonic()
        try:
            request = self._build_probe_request()
            # 客户端生命周期归传输层(M2 已经把它迁过去了)。这里以前调的是
            # `self._client()` —— 评分器上根本没有这个方法,于是每次探针都是
            # 一个 AttributeError,被下面那个宽泛的 except 转成"连接测试失败",
            # 一个完全可达的服务被报成不可达,而错误信息里没有半点线索
            async with self._transport.client() as client:
                payload, _status = await self._send_once(request, client)
            base["latency_ms"] = int((time.monotonic() - started) * 1000)
            text = self._adapter.extract_text(payload)
            base["reachable"] = True
            base["model"] = str(payload.get("model") or self.config.model)
            base["message"] = (
                "连接正常,模型已返回内容"
                if text.strip()
                else "连接正常,但模型没有返回文本内容,请检查模型名与输出格式配置"
            )
        except ProviderError as exc:
            base["latency_ms"] = int((time.monotonic() - started) * 1000)
            base["message"] = exc.message
        except Exception as exc:  # noqa: BLE001 - 后台页面不能因为自检打不开
            base["latency_ms"] = int((time.monotonic() - started) * 1000)
            base["message"] = f"连接测试失败:{type(exc).__name__}"
            # 走到这里的都不是 ProviderError,也就是说**不是上游的问题**,
            # 而是我们自己这一侧的 bug。带上堆栈:只记一个类名的话,
            # 「探针把可达服务报成不可达」这种故障在日志里和真的不可达长得一样
            logger.exception(
                "vision test_connection failed with an unexpected error",
                extra={
                    "extra_fields": {
                        "event": "eval.test_connection_failed",
                        "error": type(exc).__name__,
                    }
                },
            )
        return base

    def _build_probe_request(self) -> VisionRequest:
        """最小可用请求:一张 8×8 的图 + 一句"返回 {"ok": true}"。"""
        from app.llm.images import to_data_url

        probe_image = PreparedImage(
            role="PROBE_IMAGE",
            mime_type="image/png",
            byte_size=len(PROBE_PNG),
            data_url=to_data_url(PROBE_PNG, "image/png"),
        )
        instruction = '这是一次连通性测试。只输出这个 JSON,不要任何其它内容:{"ok": true}'

        if self.config.api_style == API_STYLE_CHAT:
            body: dict[str, Any] = {
                "model": self.config.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": instruction},
                            {
                                "type": "image_url",
                                "image_url": {"url": probe_image.payload_url(), "detail": "low"},
                            },
                        ],
                    }
                ],
                "max_tokens": 64,
                "temperature": 0,
                "stream": False,
            }
        else:
            body = {
                "model": self.config.model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": instruction},
                            {
                                "type": "input_image",
                                "image_url": probe_image.payload_url(),
                                "detail": "low",
                            },
                        ],
                    }
                ],
                "max_output_tokens": 64,
                "stream": False,
                "store": False,
            }

        return VisionRequest(
            url=self._adapter.endpoint(),
            headers=self._adapter.headers(),
            body=body,
            images=[probe_image],
        )


#: 8×8 的 PNG,用于连接测试。写死字节而不是现场用 Pillow 画,
#: 是为了让连接测试在缺 Pillow 的裁剪环境里也能用。
PROBE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d29"
    "dc000000154944415478da636c686860c00698187080c129010030170190d47b"
    "15c60000000049454e44ae426082"
)
