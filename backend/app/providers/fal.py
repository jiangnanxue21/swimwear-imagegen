"""fal.ai 适配器骨架(阶段 5 实现)。

同 FASHN:不虚构任何接口细节。fal 上不同模型的输入输出差异很大,
具体用哪个 model endpoint 需要你指定,不能由代码替你猜。
"""
from __future__ import annotations

from typing import Any

from app.providers._config import provider_setting
from app.providers.base import (
    ExternalTaskStatus,
    GenerationCandidate,
    GenerationMode,
    GenerationRequest,
    ImageGenerationProvider,
    ProviderCapabilities,
)
from app.providers.errors import NotConfiguredError

# TODO(阶段5) 需要你提供后再实现:
#   - 使用哪个 fal model endpoint(试穿模型 / 图生图模型,两者输入不同)
#   - 该 endpoint 的输入 schema 字段名
#   - 鉴权头格式
#   - 队列提交与状态轮询的路径
#   - 结果字段结构
#   - 是否支持 num_images 一次多候选
SUBMIT_PATH = "TODO"
STATUS_PATH = "TODO"
STATUS_MAP: dict[str, ExternalTaskStatus] = {}


class FalImageGenerationProvider(ImageGenerationProvider):
    provider_name = "fal"
    is_simulator = False
    display_name = "fal.ai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self.api_key = provider_setting("FAL_API_KEY") if api_key is None else api_key
        raw_url = provider_setting("FAL_BASE_URL") if base_url is None else base_url
        self.base_url = (raw_url or "").rstrip("/")

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modes={GenerationMode.PRODUCT_TO_MODEL},  # TODO(阶段5) 取决于所选模型
            supports_multiple_candidates=False,  # TODO(阶段5)
            supports_seed=False,                 # TODO(阶段5)
            supports_cancel=False,               # TODO(阶段5)
            supports_webhook=False,              # TODO(阶段5)
            max_candidates_per_call=1,
        )

    def is_configured(self) -> bool:
        return bool(self.api_key and self.base_url)

    def _require_config(self) -> None:
        if not self.is_configured():
            missing = [
                name
                for name, value in (("FAL_API_KEY", self.api_key), ("FAL_BASE_URL", self.base_url))
                if not value
            ]
            raise NotConfiguredError(
                f"fal.ai 未配置,缺少:{', '.join(missing)}", provider=self.provider_name
            )

    async def test_connection(self) -> dict[str, Any]:
        configured = self.is_configured()
        return {
            "provider": self.provider_name,
            "configured": configured,
            "reachable": None,
            "message": (
                "已填写 Key 与地址,但尚未指定 model endpoint(阶段 5)"
                if configured
                else "未配置 FAL_API_KEY / FAL_BASE_URL"
            ),
        }

    async def submit(self, request: GenerationRequest) -> str:
        self._require_config()
        self.validate_request(request)
        raise NotImplementedError("fal.ai 请求映射待实现(阶段 5)")

    async def get_status(self, external_task_id: str) -> ExternalTaskStatus:
        self._require_config()
        raise NotImplementedError("fal.ai 状态查询待实现(阶段 5)")

    async def fetch_results(self, external_task_id: str) -> list[GenerationCandidate]:
        self._require_config()
        raise NotImplementedError("fal.ai 结果拉取待实现(阶段 5)")
