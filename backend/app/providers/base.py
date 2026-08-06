"""通用图像生成 Provider 抽象(需求第五章)。

刻意不做成"虚拟试穿专用"接口:同一套结构要能同时表达 VIRTUAL_TRY_ON 与
PRODUCT_TO_MODEL,将来加背景替换、批量换色也不用改调用方。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.enums import UnitsSource


class GenerationMode(StrEnum):
    VIRTUAL_TRY_ON = "virtual_try_on"
    PRODUCT_TO_MODEL = "product_to_model"


class ExternalTaskStatus(StrEnum):
    """外部任务状态的统一表示。各家原始状态字符串由适配器映射到这里。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass
class GenerationRequest:
    mode: GenerationMode
    garment_image_url: str
    model_image_url: str | None = None
    background_image_url: str | None = None
    prompt: str | None = None
    negative_prompt: str | None = None
    candidate_count: int = 4
    width: int | None = None
    height: int | None = None
    seed: int | None = None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerationCandidate:
    """Provider 返回的单张候选图。

    与 ORM 模型同名但不是一回事:这是传输结构,ORM 的那个是持久化结构。
    编排层负责把它落库并下载到自有存储。
    """

    external_id: str | None
    image_url: str | None
    local_path: str | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderUsage:
    """厂商对**这一次调用**的计费量的自述。

    `units` 用的是厂商的计价口径,不是候选图张数 —— 两者常常不等。
    FASHN 的官方参考表(`docs/vendor/fashn-skill/reference.md`「Credits」)::

        tryon-max  balanced  1k:2  2k:3  4k:4     (× num_images)
                   quality   1k:3  2k:4  4k:5

    也就是说默认档一张图是 **2 个额度**,quality/4k 是 **5 个**。
    而这两个旋钮(`FASHN_RESOLUTION` / `generation_mode`)运营在设置页能改。
    拿"我们收到几张图"当计费量,少记的倍数会跟着配置在 2 到 5 之间浮动,
    **而且方向是少记** —— 预算横幅一路绿着,账单是它的好几倍。

    `units is None` = 厂商没报。这和"厂商报了 0"是两件事,别合并:
    前者要退回推算并如实标记,后者是一次真的没花钱的调用。
    """

    #: 厂商报的计费量。None = 它没报(或报得不完整,见 `fashn.usage_from_candidates`)
    units: int | None = None
    #: 原样留证,供排查用。不参与判定
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def reported(self) -> bool:
        return self.units is not None


def settle_billable_units(
    usage: ProviderUsage | None, *, inferred: int
) -> tuple[int, str]:
    """定这一笔记几个计费单位,以及**这个数字是谁说的**。

    ## 为什么第二个返回值不能省

    §10.2 第 5 条准入是「用量记录与 Provider 后台账单条数一致」。对账的人
    发现某一行对不上时,第一个要回答的问题是"这一行的数是厂商说的还是我们
    猜的"——前者对不上要去查厂商,后者对不上是我们的算法错了,两条路完全
    相反。不落这一列的话每一行看起来同样权威,而**大部分行其实是猜的**。

    ## 退回推算是安全方向,但必须留痕

    厂商没报时用推算值,不是"就当没花钱"(那会让金额凭空消失)。
    但推算值必须被标成推算值 —— 否则下一个人会拿一张全是估算的表去和
    账单逐行核对,核不上再去查一遍代码才发现问题出在口径上。

    这个函数是纯的、零依赖,所以它能被 `tests/pure/` 直接跑到。
    """
    if usage is not None and usage.units is not None:
        return max(int(usage.units), 0), UnitsSource.PROVIDER.value
    return max(int(inferred), 0), UnitsSource.INFERRED.value


@dataclass(frozen=True)
class ProviderCapabilities:
    supported_modes: set[GenerationMode]
    supports_multiple_candidates: bool
    supports_seed: bool
    supports_cancel: bool
    supports_webhook: bool
    #: 单次调用能返回的最大候选数;超过时编排层拆成多次调用
    max_candidates_per_call: int = 4

    def supports(self, mode: GenerationMode) -> bool:
        return mode in self.supported_modes


class ImageGenerationProvider(ABC):
    provider_name: str = "abstract"
    #: 面向运营的展示名
    display_name: str = ""
    #: 这一家产出的是不是**本地假图**。`core/environment` 的真实性判定读它。
    #:
    #: **默认 True(先假设是假的),两个方向的错误代价不对称:**
    #: 忘了在一个新的真 Provider 上写 `False` -> 状态条多喊一次"这是模拟环境",
    #: 人一眼看得见,当场就会来改;忘了在一个新的 Mock 上写 `True` -> 状态条
    #: 安静地说"真的在调外部出图服务,会产生费用",而运营会照着一批假图批准上架。
    #: 后者正是 `core/environment.py` 整个模块要防的那件事。
    #:
    #: 不写成注册表里的一张名单,是因为**名单和实现类会分开演化**:
    #: `describe_extractors` 那边就是名单式的,而它的注释自己承认
    #: "接真后端时这一行必须改成问它自己"。这里直接问它自己。
    is_simulator: bool = True

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        ...

    @abstractmethod
    def is_configured(self) -> bool:
        """缺 Key / 缺地址时返回 False。

        应用启动与 GET /api/providers 都依赖它,因此实现里只允许读配置,
        绝不能发网络请求 —— 否则外部服务挂掉会拖垮整个后台页面。
        """

    @abstractmethod
    async def submit(self, request: GenerationRequest) -> str:
        """提交生成,返回外部任务 ID。"""

    @abstractmethod
    async def get_status(self, external_task_id: str) -> ExternalTaskStatus:
        ...

    @abstractmethod
    async def fetch_results(self, external_task_id: str) -> list[GenerationCandidate]:
        ...

    def usage_from_candidates(
        self, candidates: list[GenerationCandidate]
    ) -> ProviderUsage:
        """这一次调用厂商实际收了多少计费量。**默认:它没报。**

        默认返回"没报"而不是返回候选数,和 `is_simulator = True` 是同一个
        取舍 —— 两个方向的错误代价不对称:

            新 Provider 忘了覆写   台账退回推算并标 `inferred`,对账时看得见
            默认冒充厂商权威       一张全是猜的表被当成和账单同源,而没有任何
                                   地方会说它不是

        `providers/` 层不认识 session(架构契约,见 `fashn.py` 分批提交那一段),
        所以这里只把厂商的话**翻译**出来,写库由编排层做。

        参数是候选图而不是原始响应:厂商的额度数今天就抄在候选图的
        `metadata` 上,重新发一次请求去问既多花钱也拿不到当时那次的数。
        """
        return ProviderUsage()

    async def cancel(self, external_task_id: str) -> bool:
        return False

    async def test_connection(self) -> dict[str, Any]:
        """连接自检。默认只报配置状态,联网检查由各适配器覆写。"""
        configured = self.is_configured()
        return {
            "provider": self.provider_name,
            "configured": configured,
            "reachable": None,
            "message": "已配置" if configured else "未配置",
        }

    def validate_request(self, request: GenerationRequest) -> None:
        """能力与请求的通用校验。适配器可在自己的 submit 里追加检查。"""
        from app.providers.errors import ProviderInputError

        caps = self.capabilities()
        if not caps.supports(request.mode):
            raise ProviderInputError(
                f"{self.provider_name} 不支持 {request.mode.value}",
                provider=self.provider_name,
            )
        if request.mode is GenerationMode.VIRTUAL_TRY_ON and not request.model_image_url:
            raise ProviderInputError(
                "虚拟试穿必须提供模特图", provider=self.provider_name
            )
        if not request.garment_image_url:
            raise ProviderInputError("缺少商品图", provider=self.provider_name)
        if request.candidate_count < 1:
            raise ProviderInputError("候选数必须大于 0", provider=self.provider_name)
        if request.candidate_count > 1 and not caps.supports_multiple_candidates:
            raise ProviderInputError(
                f"{self.provider_name} 单次只能生成 1 张候选图",
                provider=self.provider_name,
            )
        if request.seed is not None and not caps.supports_seed:
            raise ProviderInputError(
                f"{self.provider_name} 不支持指定 seed", provider=self.provider_name
            )
