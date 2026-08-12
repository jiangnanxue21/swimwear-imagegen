"""Mock Provider:离线开发、测试与演示用(需求第十七章)。

它是唯一在阶段 3 完整可运行的 Provider,也是后面接真实 Provider 时的对照基线。

真的画图,不是返回占位 URL —— 用 Pillow 合成一张"模特试穿"效果图。
这样上传、下载、去重、多尺寸导出整条链路都能在没有任何外部服务时跑通。

行为可通过 GenerationRequest.options 控制,用于演示各种失败分支:
    {"mock_outcome": "success" | "fail" | "timeout" | "empty" | "safety" | "rate_limit"}
    {"mock_delay_seconds": 0.5}
不传时默认成功。
"""
from __future__ import annotations

import asyncio
import io
import random
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw

from app.providers.base import (
    ExternalTaskStatus,
    GenerationCandidate,
    GenerationMode,
    GenerationRequest,
    ImageGenerationProvider,
    ProviderCapabilities,
    work_units,
)
from app.providers.errors import (
    ProviderContentSafetyError,
    ProviderGenerationError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)

CANVAS = (768, 1024)


@dataclass
class _MockJob:
    request: GenerationRequest
    outcome: str
    created_at: float
    status: ExternalTaskStatus
    cancelled: bool = False


class MockImageGenerationProvider(ImageGenerationProvider):
    provider_name = "mock"
    #: 出的是 Pillow 画的本地图。**它是真的图片文件,和真实产出在界面上分不出来**
    is_simulator = True
    display_name = "Mock(离线演示)"

    def __init__(self, *, storage=None) -> None:
        # 进程内任务表。Mock 不需要持久化:进程重启后任务本来就该重跑。
        self._jobs: dict[str, _MockJob] = {}
        self._storage = storage
        self._counter = 0

    # ---------- 能力 ----------

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modes={GenerationMode.VIRTUAL_TRY_ON, GenerationMode.PRODUCT_TO_MODEL},
            supports_multiple_candidates=True,
            supports_seed=True,
            supports_cancel=True,
            supports_webhook=False,
            max_candidates_per_call=8,
        )

    def is_configured(self) -> bool:
        return True  # Mock 永远可用,这正是它存在的意义

    async def test_connection(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "configured": True,
            "reachable": True,
            "message": "Mock Provider 始终可用,无需配置",
        }

    # ---------- 生命周期 ----------

    async def submit(self, request: GenerationRequest) -> str:
        self.validate_request(request)

        outcome = str(request.options.get("mock_outcome", "success")).lower()
        if outcome == "rate_limit":
            raise ProviderRateLimitError("Mock:模拟限流", provider=self.provider_name)
        if outcome == "safety":
            raise ProviderContentSafetyError("Mock:模拟内容安全拦截", provider=self.provider_name)

        delay = float(request.options.get("mock_delay_seconds", 0) or 0)
        if delay:
            await asyncio.sleep(min(delay, 5.0))

        self._counter += 1
        external_id = f"mock-{int(time.time() * 1000)}-{self._counter:04d}"
        self._jobs[external_id] = _MockJob(
            request=request,
            outcome=outcome,
            created_at=time.time(),
            status=ExternalTaskStatus.RUNNING,
        )
        return external_id

    async def get_status(self, external_task_id: str) -> ExternalTaskStatus:
        job = self._jobs.get(external_task_id)
        if job is None:
            # 查不到的外部 ID 一律 UNKNOWN,由编排层决定是重提还是失败
            return ExternalTaskStatus.UNKNOWN
        if job.cancelled:
            return ExternalTaskStatus.CANCELLED
        if job.outcome == "timeout":
            # 永远停在 RUNNING,用来演示超时分支
            return ExternalTaskStatus.RUNNING
        if job.outcome == "fail":
            return ExternalTaskStatus.FAILED
        return ExternalTaskStatus.SUCCEEDED

    async def fetch_results(self, external_task_id: str) -> list[GenerationCandidate]:
        job = self._jobs.get(external_task_id)
        if job is None:
            raise ProviderGenerationError(
                f"Mock:找不到外部任务 {external_task_id}", provider=self.provider_name
            )
        if job.cancelled:
            return []
        if job.outcome == "fail":
            raise ProviderGenerationError("Mock:模拟生成失败", provider=self.provider_name)
        if job.outcome == "timeout":
            raise ProviderTimeoutError("Mock:模拟超时", provider=self.provider_name)
        if job.outcome == "empty":
            return []  # 提交成功但没返回图,编排层应触发重生

        request = job.request
        base_seed = request.seed if request.seed is not None else random.randint(1, 10**9)
        # 按角度单元摊平(2026-08-11 评审)。没有方案时 `work_units()` 给一个
        # `angle=None` 的单元、张数等于 `candidate_count` —— 与改之前逐张相同。
        #
        # Mock 只有一次 submit,所以这里不是"多次调用",而是把同一次调用的
        # 产出按角度分段。**分段顺序必须和 `angle_units()` 一致**:编排层
        # (`generation_tasks._persist_candidates`)按位置把候选图对回角度,
        # 顺序错了就是每一张的角度都记错。
        plan: list[str | None] = []
        for unit in work_units(request):
            plan.extend([unit.angle] * max(1, int(unit.count)))
        return [
            self._render(
                request,
                base_seed + index,
                index,
                external_task_id,
                angle=plan[index] if index < len(plan) else None,
            )
            for index in range(request.candidate_count)
        ]

    async def cancel(self, external_task_id: str) -> bool:
        job = self._jobs.get(external_task_id)
        if job is None:
            return False
        job.cancelled = True
        job.status = ExternalTaskStatus.CANCELLED
        return True

    # ---------- 出图 ----------

    def _render(
        self,
        request: GenerationRequest,
        seed: int,
        index: int,
        external_id: str,
        *,
        angle: str | None = None,
    ) -> GenerationCandidate:
        """画一张确定性的假"试穿图"。

        不变量:图像字节 = f(seed, index, mode, width, height, angle)。
        任何随时间变化的东西都不许进入像素,否则幂等与重放没法验证。
        `angle` 可以进像素 —— 它由方案决定,同一份方案每次都一样。

        **角度画在图上是刻意的。** Mock 出的是本地假图,人眼分不出它们的
        差别;把目标角度印上去之后,人工测试时"这一批图有没有按角度出"
        变成一件看一眼就知道的事,而不是要去翻 `candidate_metadata`。
        """
        rnd = random.Random(seed)
        width = request.width or CANVAS[0]
        height = request.height or CANVAS[1]

        img = Image.new("RGB", (width, height), (238, 238, 236))
        draw = ImageDraw.Draw(img)

        # 背景:横向渐变带,模拟影棚灯光
        for y in range(0, height, 4):
            shade = 236 - int(18 * (y / height))
            draw.rectangle((0, y, width, y + 4), fill=(shade, shade, shade - 2))

        # 人形轮廓
        cx = width // 2
        head_r = width // 12
        skin = (222, 190, 165)
        draw.ellipse((cx - head_r, height // 10, cx + head_r, height // 10 + 2 * head_r), fill=skin)
        draw.rounded_rectangle(
            (cx - width // 7, height // 10 + 2 * head_r, cx + width // 7, int(height * 0.72)),
            radius=width // 20,
            fill=skin,
        )
        # 腿
        draw.rectangle(
            (cx - width // 12, int(height * 0.7), cx - width // 40, int(height * 0.95)),
            fill=skin,
        )
        draw.rectangle(
            (cx + width // 40, int(height * 0.7), cx + width // 12, int(height * 0.95)),
            fill=skin,
        )

        # 泳衣色块:颜色由 seed 决定,便于肉眼区分不同候选
        garment = (rnd.randint(20, 210), rnd.randint(20, 210), rnd.randint(20, 210))
        draw.rounded_rectangle(
            (cx - width // 7, int(height * 0.30), cx + width // 7, int(height * 0.66)),
            radius=width // 30,
            fill=garment,
        )

        # 只画确定性输入。external_id 含时间戳,画进像素会破坏
        # "同 seed 同图" 这个不变量,而幂等与重放测试正依赖它 —— 它只进 metadata。
        draw.text((16, 14), f"MOCK  seed={seed}  #{index + 1}", fill=(60, 60, 62))
        draw.text((16, 32), f"{request.mode.value}  {width}x{height}", fill=(130, 130, 132))
        if angle:
            draw.text((16, 50), f"angle={angle}", fill=(60, 60, 62))

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        payload = buf.getvalue()

        return GenerationCandidate(
            external_id=f"{external_id}#{index}",
            image_url=None,      # Mock 不经过网络,直接给字节
            local_path=None,
            metadata={
                "seed": seed,
                "index": index,
                "width": width,
                "height": height,
                "provider": self.provider_name,
                # 这一张**打算**覆盖的角度。编排层落库时优先用它,拿不到才
                # 退回按位置推(见 `_persist_candidates`)—— 一家保序的
                # Provider 两种口径一致,一家不保序的只有这一条能用
                "target_angle": angle,
                "inline_bytes": payload,  # 编排层优先用它,省掉一次下载
            },
        )
