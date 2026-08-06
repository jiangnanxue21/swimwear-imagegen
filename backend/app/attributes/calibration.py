"""分箱校准(M4,§6.5)。**零依赖纯函数。**

把「模型说 0.93」翻译成「这一箱里的真实准确率是 0.87」。

## 数据从哪来

不需要额外的标注项目:日常的人工确认与冲突解决天然产出标注 ——
`SUGGESTED → 人工采纳` 是一个正样本,`SUGGESTED → 人工否决` 是一个负样本。
`build_bins()` 就是把这些记录聚成分箱表。

## 为什么要分箱而不是拟合一条曲线

分箱能直接回答「这一箱有几个样本」,而样本量正是能不能开自动确认的判据。
拟合出来的曲线在数据稀疏的区间照样给得出一个漂亮的数字,
而那个数字没有任何依据 —— 这恰恰是校准要解决的问题本身。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.attributes.confidence import (
    MIN_SAMPLES_PER_BIN,
    MIN_SAMPLES_PER_FIELD,
    CalibrationBin,
)

#: 默认分箱边界。高置信区间切得更细 —— 阈值都在 0.9 以上,
#: 0.3 和 0.4 之间的差别对决策没有任何影响,而 0.92 和 0.95 之间有。
DEFAULT_BIN_EDGES: tuple[float, ...] = (0.0, 0.5, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0)


@dataclass(frozen=True)
class LabelledSample:
    """一条标注样本:模型当时说了什么,人工最后认了什么。"""

    model_confidence: float
    #: 人工采纳了模型给的值就是 True
    correct: bool


def bin_index(confidence: float, edges: Sequence[float] = DEFAULT_BIN_EDGES) -> int:
    """落在第几箱。超出范围的钳到两端。"""
    value = max(0.0, min(1.0, float(confidence)))
    for i in range(len(edges) - 1):
        if edges[i] <= value < edges[i + 1]:
            return i
    return len(edges) - 2      # value == 1.0 归最后一箱


def build_bins(
    samples: Sequence[LabelledSample],
    *,
    calibration_version: str,
    edges: Sequence[float] = DEFAULT_BIN_EDGES,
) -> list[CalibrationBin]:
    """把标注样本聚成分箱表。空箱不产出记录。

    空箱不补零:一条 `sample_count=0, observed_accuracy=0` 的记录
    会被 `calibrated()` 当成「有校准数据但准确率是 0」,
    而实际情况是「这一箱没有任何样本」。两者的处理完全不同。
    """
    buckets: dict[int, list[LabelledSample]] = {}
    for s in samples:
        buckets.setdefault(bin_index(s.model_confidence, edges), []).append(s)

    result: list[CalibrationBin] = []
    for index in sorted(buckets):
        items = buckets[index]
        correct = sum(1 for s in items if s.correct)
        result.append(
            CalibrationBin(
                bin_lower=edges[index],
                bin_upper=edges[index + 1],
                observed_accuracy=round(correct / len(items), 3),
                sample_count=len(items),
                calibration_version=calibration_version,
            )
        )
    return result


@dataclass(frozen=True)
class CalibrationStatus:
    """一个 (字段, 模型, 提示词版本) 组合的校准状态。给运维页面用。"""

    field_name: str
    model_name: str
    prompt_version: str
    total_samples: int
    usable_bins: int
    thin_bins: int
    calibration_version: str | None
    #: 够不够格开自动确认
    ready: bool
    reason: str


def evaluate_readiness(
    *,
    field_name: str,
    model_name: str,
    prompt_version: str,
    bins: Sequence[CalibrationBin],
) -> CalibrationStatus:
    """这个组合能不能开自动确认。

    两条门槛(§6.5):每字段合计 ≥ 200 样本、每箱 ≥ 30。
    达不到就只出建议,不自动确认 —— 而且**要说清楚差在哪**,
    否则运维只会看到一个「未就绪」然后不知道该做什么。
    """
    total = sum(b.sample_count for b in bins)
    usable = sum(1 for b in bins if b.sample_count >= MIN_SAMPLES_PER_BIN)
    thin = len(bins) - usable
    version = bins[0].calibration_version if bins else None

    if not bins:
        return CalibrationStatus(
            field_name, model_name, prompt_version, 0, 0, 0, None, False,
            "没有任何校准数据。自动确认关闭,全部只出建议",
        )
    if total < MIN_SAMPLES_PER_FIELD:
        return CalibrationStatus(
            field_name, model_name, prompt_version, total, usable, thin, version, False,
            f"样本合计 {total},不足 {MIN_SAMPLES_PER_FIELD};"
            f"还需要约 {MIN_SAMPLES_PER_FIELD - total} 条人工确认记录",
        )
    if usable == 0:
        return CalibrationStatus(
            field_name, model_name, prompt_version, total, usable, thin, version, False,
            f"每一箱的样本都不足 {MIN_SAMPLES_PER_BIN},分箱估计不可用",
        )
    return CalibrationStatus(
        field_name, model_name, prompt_version, total, usable, thin, version, True,
        f"已就绪:{usable} 个箱可用"
        + (f",另有 {thin} 个箱样本不足(落在那些箱的判断仍只出建议)" if thin else ""),
    )
