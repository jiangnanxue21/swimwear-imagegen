"""FASHN 适配器(需求第五章 / 阶段 5)。

**本文件的每一个端点、字段名、状态字符串、错误代码都来自你提供的官方 skill**
(`docs/vendor/fashn-skill/SKILL.md` 与 `docs/vendor/fashn-skill/reference.md`,
标注校验于 TypeScript SDK `fashn@0.15.0`),
没有任何一处是凭记忆写的。文档没写的东西一律标 TODO 并保守处理,不猜。

## 接口形状

FASHN 只有一个预测端点,靠 ``model_name`` 区分能力::

    POST /v1/run           {"model_name": ..., "inputs": {...}}  -> {"id", "error"}
    GET  /v1/status/{id}   -> {"id", "status", "output", "error"}
    GET  /v1/credits       -> {"credits": {"total", "subscription", "on_demand"}}

鉴权是 ``Authorization: Bearer <key>``。

## 两处需要解释的实现选择

**一、轮询放在 Provider 内部。** 编排层的调用顺序是 submit -> get_status -> fetch_results,
各调用一次(Mock 是瞬时的,阶段 3 这样够用)。FASHN 一次生成要 20-120 秒,
因此 ``fetch_results`` 内部自己轮询到终态才返回,语义等同官方 SDK 的 ``subscribe()``。
代价是这一轮的 Celery 任务会阻塞最多 ``FASHN_POLL_TIMEOUT_SECONDS``;
阶段 5 接受这个代价(需求第二十三章:不提前做并发优化)。

**二、product-to-model 靠多次提交凑候选数。** 官方文档里 ``tryon-max`` 有
``num_images``(1-4),``tryon-v1.6`` 有 ``num_samples``(1-4),而 ``product-to-model``
的参数表**没有**这一项。所以那个模式下一次调用只出一张图,要 4 张就提交 4 次、
每次换一个 seed,外部任务 ID 用逗号拼起来。宁可多发几次请求,
也不给文档里不存在的字段瞎填一个名字。
"""
from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.providers._config import (
    provider_flag,
    provider_float,
    provider_int,
    provider_setting,
)
from app.providers.base import (
    ExternalTaskStatus,
    GenerationCandidate,
    GenerationMode,
    GenerationRequest,
    ImageGenerationProvider,
    ProviderCapabilities,
    ProviderUsage,
    work_units,
)
from app.providers.errors import (
    NotConfiguredError,
    ProviderAuthError,
    ProviderContentSafetyError,
    ProviderError,
    ProviderGenerationError,
    ProviderInputError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

# --------------------------------------------------------------------------
# 端点与协议常量(reference.md「Prediction lifecycle」)
# --------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.fashn.ai/v1"
RUN_PATH = "/run"
STATUS_PATH = "/status"
CREDITS_PATH = "/credits"

#: 实际消耗的额度在这个响应头里(reference.md「Credits」)。失败的预测不计费。
CREDITS_HEADER = "x-fashn-credits-used"

#: 多次提交时用它拼接子任务 ID。选逗号是因为 FASHN 的 id 是 UUID 形态,不含逗号。
COMPOSITE_SEPARATOR = ","

#: FASHN 原始状态 -> 统一状态。
#: starting / in_queue / processing / completed / failed 来自 REST;
#: canceled / time_out 是 SDK subscribe() 才会解析出的额外终态,一并映射以防万一。
STATUS_MAP: dict[str, ExternalTaskStatus] = {
    "starting": ExternalTaskStatus.PENDING,
    "in_queue": ExternalTaskStatus.PENDING,
    "processing": ExternalTaskStatus.RUNNING,
    "completed": ExternalTaskStatus.SUCCEEDED,
    "failed": ExternalTaskStatus.FAILED,
    "canceled": ExternalTaskStatus.CANCELLED,
    "time_out": ExternalTaskStatus.FAILED,
}

TERMINAL_STATUSES = {
    ExternalTaskStatus.SUCCEEDED,
    ExternalTaskStatus.FAILED,
    ExternalTaskStatus.CANCELLED,
}

#: 作业**开始前**的 API 错误(HTTP 4xx/5xx,体形如 {"error": "<Code>", "message": ...})
API_ERROR_MAP: dict[str, type[ProviderError]] = {
    "BadRequest": ProviderInputError,
    "UnauthorizedAccess": ProviderAuthError,
    "NotFound": ProviderError,
    "RateLimitExceeded": ProviderRateLimitError,
    "ConcurrencyLimitExceeded": ProviderRateLimitError,
    "OutOfCredits": ProviderQuotaError,
    "InternalServerError": ProviderError,
}

#: 作业**跑了但失败**的运行时错误(HTTP 200 + status=failed + error.name)
RUNTIME_ERROR_MAP: dict[str, type[ProviderError]] = {
    # 图取不到或解不开:换个 Provider 也是同样的图,重试无意义
    "ImageLoadError": ProviderInputError,
    "ContentModerationError": ProviderContentSafetyError,
    # 姿态检测不出来:同一张模特图重试还是检测不出来,得换模特模板
    "PoseError": ProviderInputError,
    "InputValidationError": ProviderInputError,
    # 上游供应商拒绝/失败,文档明确建议改输入后重试
    "ThirdPartyError": ProviderGenerationError,
    "3rdPartyProviderError": ProviderGenerationError,
    # 内部处理失败,文档明确说重试(失败不计费)
    "PipelineError": ProviderGenerationError,
    "InternalServerError": ProviderError,
    "PollingTimeout": ProviderTimeoutError,
}

#: 按 HTTP 状态码兜底。文档没列到的代码走这里,而不是笼统地报服务错误。
HTTP_STATUS_FALLBACK: dict[int, type[ProviderError]] = {
    400: ProviderInputError,
    401: ProviderAuthError,
    403: ProviderAuthError,
    404: ProviderError,
    422: ProviderInputError,
    429: ProviderRateLimitError,
}

#: 文档「Common input parameters」允许的画幅枚举
ASPECT_RATIOS: tuple[str, ...] = (
    "21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16",
)


# --------------------------------------------------------------------------
# 模型能力表
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FashnModel:
    """一个 ``model_name`` 的输入契约。

    字段名逐条对应 reference.md 的参数表 —— 表里没有的键一律不发,
    多发一个 FASHN 不认识的键只会换来 ``BadRequest``。
    """

    model_name: str
    #: 商品图的字段名。tryon-max 叫 product_image,tryon-v1.6 叫 garment_image
    product_field: str
    #: 模特图字段名;None 表示该模型不接受模特图
    model_image_field: str | None
    #: 是否**必须**有模特图
    requires_model_image: bool
    #: 一次调用产出多张的字段名;None 表示文档里没有这个能力
    candidate_field: str | None
    max_per_call: int
    #: 速度/质量档位的字段名与取值(tryon-v1.6 叫 mode,其余叫 generation_mode)
    quality_field: str | None
    quality_values: tuple[str, ...]
    supports_prompt: bool
    supports_resolution: bool
    supports_aspect_ratio: bool


TRYON_MAX = FashnModel(
    model_name="tryon-max",
    product_field="product_image",
    model_image_field="model_image",
    requires_model_image=True,
    candidate_field="num_images",
    max_per_call=4,
    quality_field="generation_mode",
    quality_values=("balanced", "quality"),
    supports_prompt=True,
    supports_resolution=True,
    supports_aspect_ratio=True,
)

TRYON_V16 = FashnModel(
    model_name="tryon-v1.6",
    product_field="garment_image",
    model_image_field="model_image",
    requires_model_image=True,
    candidate_field="num_samples",
    max_per_call=4,
    quality_field="mode",
    quality_values=("performance", "balanced", "quality"),
    supports_prompt=False,       # 参数表里没有 prompt
    supports_resolution=False,   # 输出固定 864x1296
    supports_aspect_ratio=False,
)

PRODUCT_TO_MODEL = FashnModel(
    model_name="product-to-model",
    product_field="product_image",
    model_image_field="model_image",  # 传了就进 try-on 模式,不传则新生成一个人
    requires_model_image=False,
    candidate_field=None,  # 参数表里没有 num_images,因此靠多次提交凑数
    max_per_call=1,
    quality_field="generation_mode",
    quality_values=("fast", "balanced", "quality"),
    supports_prompt=True,
    supports_resolution=True,
    supports_aspect_ratio=True,  # 仅 standard 模式生效,try-on 模式下 FASHN 会忽略
)

logger = get_logger(__name__)

TRYON_MODELS: dict[str, FashnModel] = {m.model_name: m for m in (TRYON_MAX, TRYON_V16)}

#: 这些键由编排层掌握,任务参数不得覆盖 —— 覆盖了会让「这轮到底提交了什么」无法追溯
ALWAYS_PROVIDER_MANAGED = frozenset(
    {
        "product_image", "garment_image", "model_image", "seed",
        "num_images", "num_samples", "prompt",
    }
)

#: FASHN 参数表里存在、且允许任务级传入的字段。
#: 保守起见只放这些:官方文档里没有的键传过去会让整次调用 400。
TASK_TUNABLE_FIELDS = frozenset({"segmentation_free", "moderation_level", "return_base64"})


def _tryon_model() -> FashnModel:
    """试穿用哪个模型。默认旗舰 ``tryon-max``,可切到便宜快的 ``tryon-v1.6``。"""
    name = provider_setting("FASHN_TRYON_MODEL", TRYON_MAX.model_name).strip()
    return TRYON_MODELS.get(name, TRYON_MAX)


def model_for(mode: GenerationMode) -> FashnModel:
    if mode is GenerationMode.PRODUCT_TO_MODEL:
        return PRODUCT_TO_MODEL
    return _tryon_model()


# --------------------------------------------------------------------------
# 纯函数工具(可单测,不发网络请求)
# --------------------------------------------------------------------------

#: 分批提交中途失败时,已经提交成功的那几个外部 ID 挂在异常 detail 的这个键上。
#: 编排层按它把凭证落库 —— 名字写成常量而不是各处敲字符串,
#: 因为拼错一个字母的后果是「凭证看起来记下来了,其实没有」,而且不报错。
PARTIAL_IDS_KEY = "partial_external_ids"

#: **已经发出去几个 POST**(A45-batch18 / P2-2)。
#:
#: 与 `PARTIAL_IDS_KEY` 是两个数,差别正是这次要修的洞:
#: 四次提交中第四次失败时,受理成功的 ID 有 3 个,而**发出去的请求是 4 个** ——
#: 第四个已经到了 FASHN 那里,很可能已经计费。按 ID 数记账少记一次;
#: 而在第一个 POST 之前就失败的那种(素材下载失败、输入校验不过),
#: 一个请求都没发,却按默认值记了 1 次。
#:
#: 键缺席 = 这个异常在 `submit()` 之外抛出(`_require_config` /
#: `validate_request` / 缺模特图),同样是 preflight。判定在
#: `providers/call_accounting.py`,不在调用点上各自 if 一遍。
ATTEMPTED_CALLS_KEY = "attempted_calls"


def _with_partial_ids(
    exc: ProviderError, ids: list[str], *, attempted: int = 0
) -> ProviderError:
    """把已受理的外部 ID **与已发出的请求数**挂到异常上,
    **保持异常类型与重试策略不变**。

    重建一个同类异常而不是原地改 `detail`:`AppError` 的 detail 是构造时
    定下来的,原地改会绕过它自己的组装逻辑。类型必须保住 —— 重试与切换
    策略挂在类上(`RetryPolicy`),换成基类会让「内容安全」这种绝不能自动
    重试的错误变成可重试的。

    ID 列表为空时**不再原样返回**(A45-batch18 / P2-2)。原来那句
    "不给一条正常的失败凭空加一个空列表字段"只顾到了 ID:一次
    "第一个 POST 就失败"的调用确实没有 ID,但它**已经发出去一个请求**,
    而那正是记账要的数。所以现在 ID 为空时仍然挂次数 —— 包括 0,
    因为 0 是"确认没发出去"这个结论本身,和"没人报"必须分得开
    (见 `providers/call_accounting.failed_submit_units`)。
    """
    detail = {k: v for k, v in (exc.detail or {}).items() if k != "provider"}
    if ids:
        detail[PARTIAL_IDS_KEY] = list(ids)
    detail[ATTEMPTED_CALLS_KEY] = int(attempted)
    return type(exc)(exc.message, provider=exc.provider, detail=detail)


def batch_sizes(total: int, max_per_call: int) -> list[int]:
    """把候选数拆成若干次调用。

    >>> batch_sizes(4, 4)
    [4]
    >>> batch_sizes(4, 1)
    [1, 1, 1, 1]
    >>> batch_sizes(6, 4)
    [4, 2]
    """
    if total < 1:
        return []
    per = max(1, max_per_call)
    full, remainder = divmod(total, per)
    sizes = [per] * full
    if remainder:
        sizes.append(remainder)
    return sizes


def nearest_aspect_ratio(width: int | None, height: int | None) -> str | None:
    """把宽高吸附到 FASHN 允许的画幅枚举。

    任务上存的是像素尺寸,FASHN 只收枚举,两者之间必然有损。
    这里选最接近的一档并在 metadata 里留痕,而不是悄悄丢掉用户的意图。
    """
    if not width or not height or width <= 0 or height <= 0:
        return None
    target = width / height
    best = min(
        ASPECT_RATIOS,
        key=lambda r: abs(target - (int(r.split(":")[0]) / int(r.split(":")[1]))),
    )
    return best


def data_uri(payload: bytes, mime_type: str = "image/jpeg") -> str:
    """按文档要求带上前缀:``data:image/jpg;base64,<...>``,少了前缀会被判 ImageLoadError。"""
    return f"data:{mime_type};base64," + base64.b64encode(payload).decode("ascii")


def error_from_api(status_code: int, body: Any) -> ProviderError:
    """把「作业开始前」的 API 错误映射成本项目的错误分类。"""
    code = ""
    message = ""
    if isinstance(body, dict):
        code = str(body.get("error") or "")
        message = str(body.get("message") or "")
    cls = API_ERROR_MAP.get(code) or HTTP_STATUS_FALLBACK.get(status_code, ProviderError)
    label = code or f"HTTP {status_code}"
    return cls(f"FASHN {label}:{message or '无附加说明'}", provider="fashn")


def error_from_runtime(error: Any) -> ProviderError:
    """把 ``status=failed`` 时的 ``error: {name, message}`` 映射成本项目的错误分类。"""
    name, message = "", ""
    if isinstance(error, dict):
        name = str(error.get("name") or "")
        message = str(error.get("message") or "")
    elif error:
        message = str(error)
    cls = RUNTIME_ERROR_MAP.get(name, ProviderGenerationError)
    return cls(f"FASHN 生成失败 {name or '未知错误'}:{message or '无附加说明'}", provider="fashn")


def aggregate_status(statuses: list[ExternalTaskStatus]) -> ExternalTaskStatus:
    """多个子预测的状态汇总成一个。

    只要还有一个没跑完就算没跑完;全部终态时**只要有一张成功就算成功** ——
    4 张里坏了 1 张不该让另外 3 张陪葬,编排层本来就容忍部分候选缺失。
    """
    if not statuses:
        return ExternalTaskStatus.UNKNOWN
    if any(s not in TERMINAL_STATUSES for s in statuses):
        return ExternalTaskStatus.RUNNING
    if any(s is ExternalTaskStatus.SUCCEEDED for s in statuses):
        return ExternalTaskStatus.SUCCEEDED
    if all(s is ExternalTaskStatus.CANCELLED for s in statuses):
        return ExternalTaskStatus.CANCELLED
    return ExternalTaskStatus.FAILED


class FashnImageGenerationProvider(ImageGenerationProvider):
    provider_name = "fashn"
    is_simulator = False
    # `_prepare_image` 的非 HTTP 分支会从 settings.storage_dir 安全读取并
    # 内联为 data-URI。声明这一点后,编排层不再把本地素材绕成 localhost URL。
    accepts_local_storage_paths = True
    # FASHN 的状态响应把生成图放在这个官方 CDN。代理 fake-IP 模式下它可能
    # 解析到 198.18/15;只信任这个精确域名,不放开网段或任意 *.fashn.ai。
    trusted_result_hosts = frozenset({"cdn.fashn.ai"})
    display_name = "FASHN"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        # 构造注入优先,便于测试与将来的多账号支持;未传则回落到环境配置
        self.api_key = provider_setting("FASHN_API_KEY") if api_key is None else api_key
        raw_url = provider_setting("FASHN_BASE_URL") if base_url is None else base_url
        # 端点在文档里是确定的,因此 base_url 只是覆盖项,不是必填项
        self.base_url = (raw_url or DEFAULT_BASE_URL).rstrip("/")

    # ---------------- 能力与配置 ----------------

    def capabilities(self) -> ProviderCapabilities:
        tryon = _tryon_model()
        return ProviderCapabilities(
            supported_modes={GenerationMode.VIRTUAL_TRY_ON, GenerationMode.PRODUCT_TO_MODEL},
            # product-to-model 单次只出一张,靠多次提交补齐,对编排层仍呈现为"支持多候选"
            supports_multiple_candidates=True,
            supports_seed=True,
            # 文档里没有取消预测的端点。没有就是没有,不假装支持。
            supports_cancel=False,
            # /run?webhook_url= 是文档明确支持的;我们目前仍走轮询,因为还没有公网回调端点
            supports_webhook=True,
            max_candidates_per_call=max(tryon.max_per_call, PRODUCT_TO_MODEL.max_per_call),
        )

    def is_configured(self) -> bool:
        # 只认 Key:base_url 有确定的官方默认值,不该因为没填而显示"未配置"
        return bool(self.api_key)

    def _require_config(self) -> None:
        if not self.is_configured():
            raise NotConfiguredError(
                "FASHN 未配置,缺少:FASHN_API_KEY(见 https://app.fashn.ai/api)",
                provider=self.provider_name,
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ---------------- HTTP ----------------

    def _client(self):
        # httpx 延迟导入:能力声明与配置检查要在没装 httpx 的环境也能工作
        import httpx

        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=provider_float("FASHN_REQUEST_TIMEOUT_SECONDS", 60.0),
            headers=self._headers(),
        )

    async def _request(self, method: str, path: str, **kwargs) -> tuple[Any, dict[str, str]]:
        import httpx

        try:
            async with self._client() as client:
                response = await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            # 超时不等于对方没收到。恢复流程必须先查状态再决定是否重提(需求第九章)
            raise ProviderTimeoutError(
                f"FASHN 请求超时:{method} {path}", provider=self.provider_name
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"FASHN 网络错误:{type(exc).__name__}", provider=self.provider_name
            ) from exc

        try:
            body = response.json()
        except ValueError:
            body = {"message": response.text[:200]}

        if response.status_code >= 400:
            raise error_from_api(response.status_code, body)
        return body, dict(response.headers)

    # ---------------- 素材 ----------------

    def _is_own_storage_url(self, url: str) -> bool:
        """这个地址是不是**我们自己的存储**发出来的。

        只有自己发的地址才允许原样转给 FASHN。理由是转发一个地址等于把
        "对方最终会取到什么内容"的决定权交出去:任意外部 URL 可以在我们校验
        之后改变响应、可以 302 到别处、可以对不同来源 IP 返回不同内容。
        我们自己的存储是内容寻址且不覆盖的,不存在这个问题。
        """
        from app.core.config import settings

        bases = [
            (provider_setting("S3_PUBLIC_BASE_URL", "") or "").rstrip("/"),
            (getattr(settings, "PUBLIC_BASE_URL", "") or "").rstrip("/"),
        ]
        return any(base and url.startswith(base + "/") for base in bases)

    async def _prepare_image(self, url: str) -> str:
        """把素材变成 FASHN 收得下的形式:HTTPS URL 或 base64 data-URI。

        默认转 base64。原因很实在:开发期素材地址是 http://localhost:8000/...,
        FASHN 的服务器根本访问不到。等存储换成公网可达的对象存储,
        把 FASHN_SEND_PUBLIC_URLS 打开就能省掉这次编码。

        ## 打开那个开关也不等于原样转发任意地址

        以前只要是 https 就直接发出去,一个字节都没读过。而这个 URL 的来源
        包括数据库里的素材路径和上一轮的 Provider 响应 —— 后者是外部输入。
        转发它意味着:内容没校验(可能根本不是图片、可能超限)、地址可以
        在我们身后 302 到内网、DNS 可以在校验之后改指向。

        所以现在分两种:

            我们自己存储发出的地址   原样转发。内容寻址 + 不覆盖,取到的一定是那张图
            其余一切地址             走加固过的下载路径读进来,再内联发出去

        内联比"先复制到受控存储再转发"更严:厂商拿到的是我们**验过的那份字节**,
        而不是一个之后还可能变的地址;也不会在存储里攒下一堆没人认领的中转对象。
        """
        if not url:
            raise ProviderInputError("素材地址为空", provider=self.provider_name)
        if url.startswith("data:"):
            return url

        if (
            url.startswith("https://")
            and provider_flag("FASHN_SEND_PUBLIC_URLS", False)
            and self._is_own_storage_url(url)
        ):
            return url

        limit = provider_int("FASHN_MAX_IMAGE_MB", 10) * 1024 * 1024
        payload, mime_type = await self._read_bytes(url, limit=limit)
        if len(payload) > limit:
            raise ProviderInputError(
                f"素材超过 {limit // 1024 // 1024} MB,无法内联提交",
                provider=self.provider_name,
            )
        return data_uri(payload, mime_type)

    async def _read_bytes(self, url: str, *, limit: int | None = None) -> tuple[bytes, str]:
        """读素材字节。``limit`` 是**边读边算**的硬上限,不是读完再判断。

        以前用 ``response.content`` 一次性把响应读进内存,再去比大小 ——
        限制形同虚设:一个声称 10 KB、实际返回 2 GB 的地址,在触发上限之前
        就已经把 worker 的内存吃光了。
        """
        import httpx

        if url.startswith(("http://", "https://")):
            from urllib.parse import urljoin

            from app.core.net_safety import (
                MAX_REDIRECTS,
                UnsafeDownloadURL,
                check_download_url,
                pinned_transport,
            )

            allowed = provider_setting("DOWNLOAD_ALLOWED_HOSTS")
            try:
                current = check_download_url(url, allowed_hosts=allowed)
            except UnsafeDownloadURL as exc:
                raise ProviderInputError(
                    f"拒绝读取素材:{exc}", provider=self.provider_name
                ) from exc

            # 三层,各管一件事,缺一不可(和候选图下载同一套):
            #   check_download_url  早失败、给清楚的错误信息
            #   逐跳跟随            每一跳重新校验 —— follow_redirects=True 时
            #                       httpx 在库内部完成跳转,只校验最初那个 URL
            #                       等于没校验:公网地址 302 到 169.254.169.254,
            #                       实例凭据就出去了
            #   pinned_transport    连接真正打到已校验的那个 IP,堵住 DNS Rebinding
            #
            # 以前这里是 follow_redirects=True 且没有钉 IP,前两层都缺。
            try:
                async with httpx.AsyncClient(
                    timeout=provider_float("FASHN_REQUEST_TIMEOUT_SECONDS", 60.0),
                    follow_redirects=False,
                    transport=pinned_transport(allowed_hosts=allowed, is_async=True),
                ) as client:
                    for _ in range(MAX_REDIRECTS + 1):
                        async with client.stream("GET", current) as response:
                            if response.is_redirect:
                                location = response.headers.get("location", "")
                                if not location:
                                    raise ProviderInputError(
                                        "素材地址重定向但没有 Location 头",
                                        provider=self.provider_name,
                                    )
                                # 基于逻辑地址 current 补全,不能用 response.url:
                                # 钉 IP 之后那个 URL 的主机名已经是裸 IP,
                                # 拿它做 base 会丢掉原始 Host 和 SNI
                                current = urljoin(current, location)
                                try:
                                    current = check_download_url(
                                        current, allowed_hosts=allowed
                                    )
                                except UnsafeDownloadURL as exc:
                                    raise ProviderInputError(
                                        f"素材地址重定向到了不允许的位置:{exc}",
                                        provider=self.provider_name,
                                    ) from None
                                continue

                            response.raise_for_status()
                            declared = response.headers.get("content-length")
                            if (
                                limit is not None
                                and declared
                                and declared.isdigit()
                                and int(declared) > limit
                            ):
                                raise ProviderInputError(
                                    f"素材声明大小超过 {limit // 1024 // 1024} MB,拒绝读取",
                                    provider=self.provider_name,
                                )
                            chunks: list[bytes] = []
                            total = 0
                            async for chunk in response.aiter_bytes():
                                total += len(chunk)
                                if limit is not None and total > limit:
                                    raise ProviderInputError(
                                        f"素材超过 {limit // 1024 // 1024} MB,已中止读取",
                                        provider=self.provider_name,
                                    )
                                chunks.append(chunk)
                            mime_type = response.headers.get(
                                "content-type", "image/jpeg"
                            ).split(";")[0]
                            return b"".join(chunks), mime_type or "image/jpeg"

                    raise ProviderInputError(
                        f"素材地址重定向超过 {MAX_REDIRECTS} 跳,拒绝继续跟随",
                        provider=self.provider_name,
                    )
            except ProviderInputError:
                raise
            except httpx.HTTPError as exc:
                raise ProviderInputError(
                    f"读取素材失败:{url[:120]}", provider=self.provider_name
                ) from exc

        # 存储相对路径(编排层给的是 storage_path 时走这里)
        from app.core.config import settings
        from app.core.paths import resolve_within  # 防路径穿越(需求第十九章)

        try:
            resolved = resolve_within(settings.storage_dir, url)
        except Exception as exc:  # noqa: BLE001
            raise ProviderInputError(
                "素材路径不合法,拒绝读取", provider=self.provider_name
            ) from exc
        if not resolved.is_file():
            raise ProviderInputError(f"素材不存在:{url[:120]}", provider=self.provider_name)
        # 本地分支也要守 limit。这个函数的 docstring 说 limit 是「边读边算的
        # 硬上限,不是读完再判断」—— 那句话以前只对 HTTP 那一半成立:
        # 本地文件直接 `read_bytes()` 全读进内存,再由调用方比大小,
        # 也就是"读完再判断"。文件是自有存储、上传时校验过大小,所以风险低,
        # 但一个说谎的 docstring 会让下一个人以为这里已经守住了。
        if limit is not None and resolved.stat().st_size > limit:
            raise ProviderInputError(
                f"素材超过 {limit // 1024 // 1024} MB,已中止读取",
                provider=self.provider_name,
            )
        suffix = resolved.suffix.lower()
        mime_type = "image/png" if suffix == ".png" else "image/jpeg"
        return resolved.read_bytes(), mime_type

    # ---------------- 请求映射 ----------------

    async def build_inputs(
        self,
        request: GenerationRequest,
        model: FashnModel,
        *,
        count: int,
        seed: int | None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        """构造 ``inputs``。只放该模型参数表里真实存在的键。

        `prompt` 传了就用它,不传退回 `request.prompt`。这个参数是角度
        work-unit 的落点(2026-08-11 评审):同一份请求按角度拆成几次提交,
        每次带的是"本次只出 FRONT"那一版提示词,而 `request.prompt` 仍然是
        进幂等键的那份基础提示词 —— 两者不是一回事,所以不能在这里就地覆盖。
        """
        inputs: dict[str, Any] = {
            model.product_field: await self._prepare_image(request.garment_image_url),
        }

        if request.model_image_url and model.model_image_field:
            inputs[model.model_image_field] = await self._prepare_image(request.model_image_url)

        effective_prompt = request.prompt if prompt is None else prompt
        if model.supports_prompt and effective_prompt:
            inputs["prompt"] = effective_prompt

        if model.candidate_field and count > 1:
            inputs[model.candidate_field] = min(count, model.max_per_call)

        if seed is not None:
            # 文档:0 到 2^32-1
            inputs["seed"] = int(seed) % (2**32)

        if model.supports_aspect_ratio:
            ratio = nearest_aspect_ratio(request.width, request.height)
            if ratio:
                inputs["aspect_ratio"] = ratio

        if model.supports_resolution:
            resolution = provider_setting("FASHN_RESOLUTION").strip().lower()
            if resolution in {"1k", "2k", "4k"}:
                inputs["resolution"] = resolution

        if model.quality_field:
            quality = provider_setting("FASHN_GENERATION_MODE").strip().lower()
            if quality in model.quality_values:
                inputs[model.quality_field] = quality

        output_format = provider_setting("FASHN_OUTPUT_FORMAT", "png").strip().lower()
        if output_format in {"png", "jpeg"}:
            inputs["output_format"] = output_format

        # 任务级覆盖放在最后:全局配置是默认值,单次任务与修复策略可以盖掉它。
        # 以前完全不读 request.options,于是 B/C 档修复策略写进数据库的
        # upscale_pattern / framing 之类参数一个都没生效 —— 日志和审核页显示
        # 「已按策略重生」,发出去的请求却和上一轮一模一样。
        inputs.update(self._task_overrides(request, model))
        return inputs

    @staticmethod
    def _task_overrides(request: GenerationRequest, model: FashnModel) -> dict[str, Any]:
        """从 request.options 里挑出这个模型真的认识的参数。

        白名单而不是照单全收:FASHN 对未知字段是直接报错的,
        把修复策略里的自造键原样透传过去,只会让整轮调用 400 掉。
        不认识的键记一条日志就丢掉 —— 让人能查到「为什么这个参数没生效」。
        """
        options = getattr(request, "options", None) or {}
        if not isinstance(options, dict):
            return {}

        allowed: dict[str, Any] = {}
        for key, value in options.items():
            name = str(key).strip()
            if name in ALWAYS_PROVIDER_MANAGED:
                # 图片、seed、数量由编排层决定,不允许被任务参数覆盖
                continue
            if name == "resolution" and model.supports_resolution:
                if str(value).lower() in {"1k", "2k", "4k"}:
                    allowed[name] = str(value).lower()
                continue
            if name in {"generation_mode", "mode"} and model.quality_field:
                if str(value).lower() in model.quality_values:
                    allowed[model.quality_field] = str(value).lower()
                continue
            if name == "output_format" and str(value).lower() in {"png", "jpeg"}:
                allowed[name] = str(value).lower()
                continue
            if name == "aspect_ratio" and model.supports_aspect_ratio:
                allowed[name] = str(value)
                continue
            if name in TASK_TUNABLE_FIELDS:
                allowed[name] = value
                continue
            logger.info(
                "dropping unsupported FASHN task option",
                extra={"extra_fields": {"option": name, "model": model.model_name}},
            )
        # 注:结果一律走 CDN URL 再由编排层下载入库(需求第十九章:尽快下载到
        # 自有存储)。return_base64 会让响应体变得巨大且 60 分钟过期,明确不用。
        # —— 这段说明原来挂在 `return allowed` **之后**的一句 `return inputs` 上,
        # 那行永远执行不到,`inputs` 在本函数里也根本不存在(ruff F821)。
        # 死代码删掉,说明留下:它记的是一个仍然生效的设计决定。
        return allowed

    # ---------------- 生命周期 ----------------

    async def submit(self, request: GenerationRequest) -> str:
        self._require_config()
        self.validate_request(request)

        model = model_for(request.mode)
        if model.requires_model_image and not request.model_image_url:
            raise ProviderInputError(
                f"{model.model_name} 必须提供模特图", provider=self.provider_name
            )

        # 先按**角度**拆,再按每家模型的单次上限拆(2026-08-11 评审)。
        #
        # 顺序不能反。反过来(先按上限拆再摊角度)会让一次 `num_images=3`
        # 的调用横跨两个角度,而那一次只能带一句提示词 —— 于是"这三张里
        # 前两张是正面、第三张是背面"变成一句我们自己相信、模型没听见的话。
        #
        # `work_units()` 在没有方案时给出一个 `angle=None` 的单元,
        # 于是这段代码在存量路径上等价于原来的 `batch_sizes(candidate_count, ...)`。
        plan: list[tuple[str | None, int]] = []
        for unit in work_units(request):
            for size in batch_sizes(unit.count, model.max_per_call):
                plan.append((unit.prompt, size))
        ids: list[str] = []
        #: **发出去几个 POST**,不是受理成功几个(A45-batch18 / P2-2)。
        #: 在 `_request` **之前**加,不在之后 —— 计的是"发出去了几次",
        #: 不是"成功了几次":FASHN 在收到请求那一刻就可能开始计费,
        #: 超时不退钱。与识别链路 `llm/transport` 的 `on_attempt` 同一条口径
        attempted = 0
        try:
            for index, (unit_prompt, size) in enumerate(plan):
                # 每次提交换一个 seed:同 seed 同输入会得到同一张图,拿 4 张一样的没有意义
                seed = None if request.seed is None else request.seed + index
                # 素材准备在计数之前:它失败时一个请求都没发出去
                inputs = await self.build_inputs(
                    request, model, count=size, seed=seed, prompt=unit_prompt
                )
                attempted += 1
                body, _ = await self._request(
                    "POST", RUN_PATH, json={"model_name": model.model_name, "inputs": inputs}
                )
                if isinstance(body, dict) and body.get("error"):
                    raise error_from_runtime(body["error"])
                prediction_id = (body or {}).get("id") if isinstance(body, dict) else None
                if not prediction_id:
                    raise ProviderError("FASHN 未返回预测 ID", provider=self.provider_name)
                ids.append(str(prediction_id))
        except ProviderError as exc:
            # **已经提交成功的那几个不能跟着异常一起消失。**
            #
            # `product-to-model` 的参数表里没有 num_images,所以 4 张候选 = 4 次
            # 独立提交(`max_per_call=1`)。第 2 次失败时,第 1 次已经在 FASHN
            # 那边创建、开始跑、并且很可能已经计费 —— 而它的 prediction_id
            # 只存在于这个局部列表里。异常一抛,ID 没了:
            #
            #     我们查不到它     没有 ID 就调不了 status/result
            #     它也不会消失     FASHN 那边照跑,照扣钱
            #     人工重试         又是 4 次新提交,第一次那张永远没人认领
            #
            # 这正是 `external_task_id` 被称作「已经花过钱的凭证」的原因
            # (见 tasks/generation_tasks.py 里立刻 commit 那一段)。凭证必须
            # 跟着异常一起出去,由编排层落库,而不是在这里被吞掉。
            #
            # 不在这里写库:providers 层不认识 session,这是架构契约。
            raise _with_partial_ids(exc, ids, attempted=attempted) from None

        return COMPOSITE_SEPARATOR.join(ids)

    @staticmethod
    def split_external_id(external_task_id: str) -> list[str]:
        return [part for part in (external_task_id or "").split(COMPOSITE_SEPARATOR) if part]

    async def _poll_once(self, prediction_id: str) -> tuple[dict[str, Any], int | None]:
        body, headers = await self._request("GET", f"{STATUS_PATH}/{prediction_id}")
        credits = headers.get(CREDITS_HEADER)
        try:
            used = int(float(credits)) if credits is not None else None
        except (TypeError, ValueError):
            used = None
        return (body if isinstance(body, dict) else {}), used

    async def get_status(self, external_task_id: str) -> ExternalTaskStatus:
        self._require_config()
        ids = self.split_external_id(external_task_id)
        if not ids:
            return ExternalTaskStatus.UNKNOWN

        statuses: list[ExternalTaskStatus] = []
        for prediction_id in ids:
            body, _ = await self._poll_once(prediction_id)
            raw = str(body.get("status") or "").lower()
            statuses.append(STATUS_MAP.get(raw, ExternalTaskStatus.UNKNOWN))
        return aggregate_status(statuses)

    async def fetch_results(self, external_task_id: str) -> list[GenerationCandidate]:
        """轮询到终态后返回候选图。

        语义等同官方 SDK 的 ``subscribe()``:提交之后自己等,等到终态为止。
        全部子预测都失败时抛出**第一条**运行时错误 —— 错误分类决定了编排层
        是重试、换 Provider 还是转人工,笼统地报"生成失败"会丢掉这个信息。
        """
        self._require_config()
        ids = self.split_external_id(external_task_id)
        if not ids:
            raise ProviderInputError("外部任务 ID 为空", provider=self.provider_name)

        interval = provider_float("FASHN_POLL_INTERVAL_SECONDS", 2.0)
        deadline = time.monotonic() + provider_float("FASHN_POLL_TIMEOUT_SECONDS", 300.0)

        pending = list(ids)
        finished: dict[str, dict[str, Any]] = {}
        credits: dict[str, int | None] = {}

        while pending:
            still_running: list[str] = []
            for prediction_id in pending:
                body, used = await self._poll_once(prediction_id)
                status = STATUS_MAP.get(str(body.get("status") or "").lower(),
                                        ExternalTaskStatus.UNKNOWN)
                if status in TERMINAL_STATUSES:
                    finished[prediction_id] = body
                    credits[prediction_id] = used
                else:
                    still_running.append(prediction_id)

            pending = still_running
            if not pending:
                break
            if time.monotonic() >= deadline:
                raise ProviderTimeoutError(
                    f"FASHN 轮询超时,仍未完成:{', '.join(pending)}",
                    provider=self.provider_name,
                )
            await asyncio.sleep(interval)

        candidates: list[GenerationCandidate] = []
        failures: list[Any] = []
        for prediction_id in ids:
            body = finished.get(prediction_id, {})
            status = STATUS_MAP.get(str(body.get("status") or "").lower(),
                                    ExternalTaskStatus.UNKNOWN)
            if status is not ExternalTaskStatus.SUCCEEDED:
                failures.append(body.get("error") or {"name": str(status)})
                continue
            outputs = body.get("output") or []
            if isinstance(outputs, str):
                outputs = [outputs]
            for index, image_url in enumerate(outputs):
                candidates.append(
                    GenerationCandidate(
                        external_id=f"{prediction_id}#{index}",
                        image_url=str(image_url),
                        local_path=None,
                        metadata={
                            "provider": self.provider_name,
                            "prediction_id": prediction_id,
                            "credits_used": credits.get(prediction_id),
                        },
                    )
                )

        if not candidates and failures:
            raise error_from_runtime(failures[0])
        return candidates

    # ---------------- 用量 ----------------

    def usage_from_candidates(
        self, candidates: list[GenerationCandidate]
    ) -> ProviderUsage:
        """把候选图上带的额度还原成**这一次调用**的计费量。

        ## 一条 prediction 的额度只能算一次

        `fetch_results` 把每条 prediction 的 `x-fashn-credits-used` 抄在**它产出
        的每一张**候选图上(那是给排查用的现场,不是给求和用的)。于是
        `num_images=4` 那一次,4 张候选各自带着同一个 per-prediction 总额 ——
        直接 `sum()` 就是 **4 倍高估**。

        这个坑今天还没人踩进去,因为还没有人读过这个字段。它写在这里的意义
        正是:读法只有这一处,而这一处按 `prediction_id` 去重。

        ## 少一个数就整笔不算权威

        某条 prediction 出了图却没带额度(响应头缺失、格式没认出来)时,
        求和的结果是一个**偏小**的数,而它看起来和真数一模一样。
        少记钱这个方向的错误没有人会去发现 —— 所以整笔退回推算并标
        `inferred`,让对账的人知道这一行要自己去查。

        ## 一张图都没有时也不算权威

        FASHN 的文档写着「失败的预测不计费」,所以"全失败 = 0 额度"多半是对的。
        但**我们没读到那个 0**,是自己推出来的。冒充厂商权威去写一个 0,
        和写一个偏小的数是同一类错误,只是更彻底。
        """
        if not candidates:
            return ProviderUsage(detail={"reason": "no_candidates"})

        per_prediction: dict[str, int] = {}
        for candidate in candidates:
            metadata = candidate.metadata or {}
            prediction_id = metadata.get("prediction_id")
            used = metadata.get("credits_used")
            if not prediction_id or not isinstance(used, int):
                # 分不了组、或者这一组没带数 —— 两种都让求和结果偏小
                return ProviderUsage(
                    detail={
                        "reason": "incomplete",
                        "predictions": len(per_prediction),
                        "candidates": len(candidates),
                    }
                )
            per_prediction[str(prediction_id)] = used

        return ProviderUsage(
            units=sum(per_prediction.values()),
            detail={
                "reason": "reported",
                "header": CREDITS_HEADER,
                "predictions": len(per_prediction),
                "candidates": len(candidates),
            },
        )

    # ---------------- 自检 ----------------

    async def test_connection(self) -> dict[str, Any]:
        """连接自检。用 ``GET /v1/credits`` —— 它只读、不排队、不产生费用。"""
        if not self.is_configured():
            return {
                "provider": self.provider_name,
                "configured": False,
                "reachable": None,
                "message": "未配置 FASHN_API_KEY",
                "base_url": self.base_url,
            }

        try:
            body, _ = await self._request("GET", CREDITS_PATH)
        except ProviderError as exc:
            return {
                "provider": self.provider_name,
                "configured": True,
                "reachable": False,
                "message": exc.message,
                "base_url": self.base_url,
            }

        credits = (body or {}).get("credits") or {}
        total = credits.get("total")
        return {
            "provider": self.provider_name,
            "configured": True,
            "reachable": True,
            "message": (
                f"连接正常,剩余额度 {total}" if total is not None else "连接正常"
            ),
            "base_url": self.base_url,
            "tryon_model": _tryon_model().model_name,
        }
