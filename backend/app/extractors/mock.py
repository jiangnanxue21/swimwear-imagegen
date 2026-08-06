"""Mock 抽取器(M3)。

存在的理由和 Mock 评分器一样:**整条链路必须能在不接任何外部服务的情况下跑通**。
`make smoke` 会走它,CI 也会走它。

它不是"随便返回点东西":输出是**按素材内容确定性派生**的,
所以同一张图每次识别得到同样的证据 —— 否则合并与冲突的测试没法复现。
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from app.attributes.registry import REGISTRY
from app.extractors.base import FieldObservation, ImageExtractionResult

MOCK_MODEL_NAME = "mock-extractor-v1"
MOCK_PROMPT_VERSION = "mock-1"


def _pick(seed: str, options: tuple[str, ...]) -> str:
    """按种子确定性地选一个。同一张图永远得到同一个值。"""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return options[digest[0] % len(options)]


def _confidence(seed: str) -> float:
    """0.55 - 0.98 之间的确定性数值。

    刻意覆盖阈值两侧:识别出来的东西里既要有够格自动确认的,
    也要有只够进建议队列的,否则决策表的分支在 Mock 下永远走不全。
    """
    digest = hashlib.sha256(("c:" + seed).encode("utf-8")).digest()
    return round(0.55 + (digest[1] % 44) / 100.0, 3)


class MockAttributeExtractor:
    """按素材指纹确定性地产出证据。"""

    name = "mock"
    #: 环境真实性判定读它(硬规则 4:问实现自己,不问名单)。
    #: A45-batch14 起注册表的 `is_simulator` / `configured` 都问到这里
    is_simulator = True
    #: Mock 不花钱。`run_extraction` 据此决定要不要逐次记流水
    billable = False

    def is_configured(self) -> bool:
        """Mock 不需要任何配置 —— 这句话对它字面为真,而 `is_simulator=True`
        保证真实性判定不会因此把一个纯 Mock 环境报成 REAL(判定顺序见
        `core/environment.facet_fidelity`:模拟器先于配置)。"""
        return True

    def declared_versions(self) -> tuple[str, str]:
        """两个常量。Mock 的输出按素材内容确定性派生,所以「同一份输入
        得到同一份结果」对它字面成立 —— 幂等复用在 Mock 下是安全的。

        它照样实现这个钩子而不是留空:`make smoke` 与 CI 走的都是 Mock,
        只在 vision 上接幂等的话,**这条路径在门禁里一次都不会被走到**。
        """
        return MOCK_MODEL_NAME, MOCK_PROMPT_VERSION

    def extract(
        self,
        *,
        image: Any,
        target_fields: tuple[str, ...],
        enum_defs: Mapping[str, tuple[str, ...]],
        options: dict[str, Any] | None = None,
    ) -> ImageExtractionResult:
        # 用素材指纹当种子。`image` 可以是 PreparedImage,也可以是
        # 测试里直接传的字符串 —— Mock 不该对输入形状挑剔
        seed = getattr(image, "sha256", None) or getattr(image, "role", None) or str(image)

        fields: list[FieldObservation] = []
        unreadable: list[str] = []
        for name in target_fields:
            spec = REGISTRY.get(name)
            if spec is None:
                continue
            allowed = enum_defs.get(name)
            if allowed:
                value: Any = _pick(f"{seed}:{name}", tuple(allowed))
            elif spec.multi_value:
                value = [_pick(f"{seed}:{name}:0", ("WHITE", "BLACK", "GOLD"))]
            else:
                value = _pick(f"{seed}:{name}", ("navy", "coral", "ivory"))

            # 一部分字段故意报"看不清":整条链路必须能处理这种情况,
            # 而不是只在所有字段都识别成功时才跑得通
            if _pick(f"{seed}:{name}:readable", ("yes", "yes", "yes", "no")) == "no":
                unreadable.append(name)
                continue

            fields.append(
                FieldObservation(
                    name=name,
                    value=value,
                    confidence=_confidence(f"{seed}:{name}"),
                    evidence=f"Mock:依据素材指纹 {str(seed)[:8]} 派生",
                )
            )

        return ImageExtractionResult(
            fields=tuple(fields),
            unreadable_fields=tuple(unreadable),
            model_name=MOCK_MODEL_NAME,
            prompt_version=MOCK_PROMPT_VERSION,
            duration_ms=1,
            meta={"mock": True},
        )
