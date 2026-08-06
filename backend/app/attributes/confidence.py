"""system_confidence 的计算(M3 骨架,§6.3)。**纯函数,零依赖。**

    system_confidence =
        calibrated(model_confidence, 字段, 模型, 提示词版本)   校准后的概率
      × agreement_factor                                      多图一致性
      × quality_factor                                        图片清晰度
      × visibility_prior(字段)                                 字段可见性先验
      × source_factor(素材来源)                                AI 图打折

## 为什么不直接用模型自报的 confidence

模型说 0.93,那对应多高的真实准确率?不同字段、不同模型、不同提示词版本的
答案完全不同,而且**没有任何理由认为它们相同**。拿一个未经校准的数字去和
0.92 比大小,那条阈值就是个装饰。

## 未校准 = None,不是 0

这是本模块最重要的一条。返回 0 会让它看起来像「置信度很低」,于是被当成
一个普通的低分值处理;而实际含义是「这个数字根本没有意义」。
fail closed 的前提是这两种情况在类型上就分得开。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.attributes.contracts import ConfidenceBreakdown
from app.attributes.registry import REGISTRY
from app.core.enums import MediaSource

#: 素材来源的折扣(§1 硬规矩 6)。
#:
#: AI 生成图可能改变领型、颜色、遮盖度 —— 它对属性的证明力天然低于原始素材。
#: 打折而不是直接排除:AI 图和原图结论一致时是有价值的旁证,
#: 不一致时那本身就是生成质量的信号(AI_DIVERGENCE)。
SOURCE_FACTOR: Mapping[str, float] = {
    MediaSource.MANUAL_UPLOAD.value: 1.0,
    MediaSource.IMPORTED_URL.value: 1.0,
    MediaSource.SUPPLIER_FEED.value: 1.0,
    MediaSource.PLATFORM_SYNC.value: 0.9,
    MediaSource.AI_GENERATED.value: 0.8,
}

#: 多图一致性。n = 有该字段证据的图数。
#:
#: 单图 0.85 而不是 1.0:一张图看出来的东西没有交叉验证。
#: 过半分歧直接归零 —— 那种情况下"多数意见"没有意义,该转人工。
def agreement_factor(total: int, agreeing: int) -> float:
    if total <= 0 or agreeing <= 0:
        return 0.0
    if agreeing == total:
        if total >= 3:
            return 1.0
        if total == 2:
            return 0.95
        return 0.85          # 单图
    ratio = agreeing / total
    if ratio > 0.5:
        return 0.7
    return 0.0               # 过半分歧


def quality_factor(quality: Mapping[str, object] | None) -> float:
    """由确定性图像指标算出的 0.6-1.0。

    **不是模型打的分。** 用一个不确定的量去修正另一个不确定的量没有意义。

    拿不到指标时返回 0.85 而不是 1.0:没测过的图不该享受"清晰"的待遇。
    """
    if not quality:
        return 0.85
    score = 1.0
    sharpness = quality.get("sharpness")
    if isinstance(sharpness, (int, float)):
        score *= max(0.6, min(1.0, float(sharpness)))
    if quality.get("has_watermark") is True:
        # 有水印说明这张图多半是从别处抓的,内容可能被改过
        score *= 0.9
    return round(max(0.6, min(1.0, score)), 3)


@dataclass(frozen=True)
class CalibrationBin:
    """一条校准记录。"""

    bin_lower: float
    bin_upper: float
    observed_accuracy: float
    sample_count: int
    calibration_version: str


#: 一个字段开自动确认所需的最少样本(§6.5)。
#: 每箱 ≥ 30、每字段合计 ≥ 200,否则该字段不开自动确认。
MIN_SAMPLES_PER_BIN = 30
MIN_SAMPLES_PER_FIELD = 200


def calibrated(
    model_confidence: float | None,
    bins: Sequence[CalibrationBin],
) -> tuple[float | None, str | None]:
    """查分箱表。返回 (校准后的概率, 校准版本)。

    **拿不到就返回 None** —— 没有校准数据时,任何数字都是编的。
    样本不足同样返回 None:30 个样本估出来的准确率,置信区间宽到没法用。
    """
    if model_confidence is None or not bins:
        return None, None
    total = sum(b.sample_count for b in bins)
    if total < MIN_SAMPLES_PER_FIELD:
        return None, None
    for b in bins:
        if b.bin_lower <= model_confidence < b.bin_upper or (
            model_confidence >= b.bin_upper == 1.0
        ):
            if b.sample_count < MIN_SAMPLES_PER_BIN:
                return None, None
            return float(b.observed_accuracy), b.calibration_version
    return None, None


def compute(
    *,
    field_name: str,
    model_confidence: float | None,
    bins: Sequence[CalibrationBin],
    total_images: int,
    agreeing_images: int,
    quality: Mapping[str, object] | None,
    media_source: str,
) -> ConfidenceBreakdown:
    """算出因子分解。**每个因子都留下来。**

    不是为了好看 —— 前端要能回答运营的那个问题:
    「0.71 是因为只有一张图,还是因为图太糊?」这两种情况人要做的事
    完全不同(补一张图 vs 重拍),只给一个总分等于没告诉他。
    """
    spec = REGISTRY.get(field_name)
    value, _version = calibrated(model_confidence, bins)
    return ConfidenceBreakdown(
        calibrated=value,
        agreement_factor=agreement_factor(total_images, agreeing_images),
        quality_factor=quality_factor(quality),
        visibility_prior=spec.visibility_prior if spec else 1.0,
        source_factor=SOURCE_FACTOR.get(media_source, 0.8),
    )
