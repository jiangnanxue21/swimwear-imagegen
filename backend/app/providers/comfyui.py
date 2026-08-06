"""ComfyUI 适配器骨架(阶段 5 实现)。

与商用 API 不同,ComfyUI 的"接口"就是你自己的工作流 JSON。
因此这里唯一能提前确定的是:**节点 ID 必须来自配置,不得散落在业务代码里**
(需求第六章)。工作流本身要你提供,代码不替你编造节点结构。

配置形如 comfyui/config.yaml:
    comfyui:
      base_url: http://comfyui:8188
      workflow_file: workflows/virtual_tryon.json
      nodes:
        garment_image: "12"
        ...
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.providers._config import project_root, provider_setting
from app.providers.base import (
    ExternalTaskStatus,
    GenerationCandidate,
    GenerationMode,
    GenerationRequest,
    ImageGenerationProvider,
    ProviderCapabilities,
)
from app.providers.errors import NotConfiguredError

#: 工作流里必须被映射到的语义槽位
REQUIRED_NODE_SLOTS = (
    "garment_image",
    "model_image",
    "positive_prompt",
    "negative_prompt",
    "seed",
    "output",
)


@dataclass
class ComfyUIConfig:
    base_url: str = ""
    workflow_file: str = ""
    timeout_seconds: int = 300
    poll_interval_seconds: float = 2.0
    max_retries: int = 2
    nodes: dict[str, str] = field(default_factory=dict)

    @property
    def missing_slots(self) -> list[str]:
        return [slot for slot in REQUIRED_NODE_SLOTS if not self.nodes.get(slot)]

    @classmethod
    def load(cls, path: str | Path | None = None) -> ComfyUIConfig:
        """从 YAML 读配置。文件不存在时返回空配置,而不是抛错 ——
        没接 ComfyUI 的部署必须能正常启动(需求第十七章)。"""
        import yaml

        raw_path = Path(path or provider_setting("COMFYUI_CONFIG_FILE", "./comfyui/config.yaml"))
        if not raw_path.is_absolute():
            raw_path = project_root() / raw_path
        if not raw_path.exists():
            return cls(base_url=provider_setting("COMFYUI_BASE_URL"))

        data = yaml.safe_load(raw_path.read_text(encoding="utf-8")) or {}
        section = data.get("comfyui", {}) or {}
        return cls(
            base_url=provider_setting("COMFYUI_BASE_URL") or str(section.get("base_url", "")),
            workflow_file=str(section.get("workflow_file", "")),
            timeout_seconds=int(section.get("timeout_seconds", 300)),
            poll_interval_seconds=float(section.get("poll_interval_seconds", 2.0)),
            max_retries=int(section.get("max_retries", 2)),
            nodes={str(k): str(v) for k, v in (section.get("nodes") or {}).items()},
        )


class ComfyUIImageGenerationProvider(ImageGenerationProvider):
    provider_name = "comfyui"
    is_simulator = False
    display_name = "ComfyUI 工作流"

    def __init__(self, config: ComfyUIConfig | None = None) -> None:
        self.config = config or ComfyUIConfig.load()

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modes={GenerationMode.VIRTUAL_TRY_ON, GenerationMode.PRODUCT_TO_MODEL},
            supports_multiple_candidates=True,  # batch_size 可调
            supports_seed=True,                 # KSampler 节点必有 seed
            supports_cancel=True,               # /interrupt 与 /queue 删除
            supports_webhook=False,
            max_candidates_per_call=4,
        )

    def is_configured(self) -> bool:
        return bool(self.config.base_url) and not self.config.missing_slots

    def _require_config(self) -> None:
        if not self.config.base_url:
            raise NotConfiguredError(
                "ComfyUI 未配置 base_url(COMFYUI_BASE_URL 或 config.yaml)",
                provider=self.provider_name,
            )
        missing = self.config.missing_slots
        if missing:
            raise NotConfiguredError(
                f"ComfyUI 节点映射不完整,缺少:{', '.join(missing)}",
                provider=self.provider_name,
                detail={"missing_slots": missing},
            )

    async def test_connection(self) -> dict[str, Any]:
        missing = self.config.missing_slots
        if not self.config.base_url:
            message = "未配置 base_url"
        elif missing:
            message = f"节点映射缺少:{', '.join(missing)}"
        else:
            message = "配置完整,但工作流提交逻辑待实现(阶段 5)"
        return {
            "provider": self.provider_name,
            "configured": self.is_configured(),
            "reachable": None,
            "message": message,
            "base_url": self.config.base_url or None,
            "workflow_file": self.config.workflow_file or None,
        }

    async def submit(self, request: GenerationRequest) -> str:
        self._require_config()
        self.validate_request(request)
        # 阶段 5 的实现顺序:
        #   1. 读工作流 JSON
        #   2. POST /upload/image 上传商品图与模特图
        #   3. 按 config.nodes 替换对应节点的 inputs
        #   4. 写入 seed / 提示词 / 输出尺寸
        #   5. POST /prompt 提交,拿 prompt_id
        raise NotImplementedError(
            "ComfyUI 工作流提交待实现(阶段 5),需要先提供真实工作流 JSON 与节点 ID"
        )

    async def get_status(self, external_task_id: str) -> ExternalTaskStatus:
        self._require_config()
        raise NotImplementedError("ComfyUI 队列/历史查询待实现(阶段 5)")

    async def fetch_results(self, external_task_id: str) -> list[GenerationCandidate]:
        self._require_config()
        raise NotImplementedError("ComfyUI 输出下载待实现(阶段 5)")
