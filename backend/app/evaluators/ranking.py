"""轮内候选图预排序(需求第十三章)。

流程:比较候选图与商品原图的相似度 -> 比较人体与摄影真实性 -> 输出排名 ->
前两名做完整评分,后两名只做快速硬错误检查。

目的是**省钱**:完整评分要调视觉大模型,一轮四张全评是四次调用。
预排序用的是本地像素统计,几毫秒、零成本。

必须可关闭(需求第十三章原文),以便对比成本与准确率 ——
关掉后所有候选图都走完整评分,是这套机制的对照组。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.enums import EvaluationDepth

#: 感知哈希边长。8x8 = 64 位,对缩放和轻微色偏稳健,够用来排序。
HASH_EDGE = 8


def average_hash(image_bytes: bytes) -> int:
    """64 位均值哈希。像素大于均值记 1。"""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as img:
        small = img.convert("L").resize((HASH_EDGE, HASH_EDGE), Image.Resampling.BILINEAR)
        # "L" 模式下与 `getdata()` 逐位相同(0-255 的行优先序列)。
        # 不用 getdata 是因为它在 Pillow 12 起弃用、14 移除;不用替代品
        # get_flattened_data 是因为那是 12 才有的,而依赖声明允许 >=11。
        pixels = list(small.tobytes())

    mean = sum(pixels) / len(pixels)
    bits = 0
    for index, value in enumerate(pixels):
        if value > mean:
            bits |= 1 << index
    return bits


def hamming_distance(left: int, right: int) -> int:
    return bin(left ^ right).count("1")


def similarity_score(candidate_hash: int, reference_hashes: list[int]) -> float:
    """与商品原图的相似度,0-100。取与最接近的一张原图的距离。

    取最小距离而不是平均:商品有正面、背面、细节多张原图,
    一张试穿正面图本来就只该像其中一张。
    """
    if not reference_hashes:
        return 50.0  # 没有参考图时不做倾向性判断
    best = min(hamming_distance(candidate_hash, ref) for ref in reference_hashes)
    return round(max(0.0, 100.0 - (best / 64.0) * 100.0), 2)


def realism_score(image_bytes: bytes) -> float:
    """人体与摄影真实性的廉价代理指标,0-100。

    真正的真实性判断要靠视觉模型。这里只用两个统计量做粗筛:
    细节丰富度(相邻像素差的均值)与色彩饱和分布。
    纯色块、糊成一片、过曝这三类明显不可用的图会被压到很低。

    它只用于**排序**,不参与分档 —— 别把统计量当质量判断。
    """
    import io

    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(image_bytes)) as img:
        rgb = img.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
        grey = rgb.convert("L")
        # 同 `average_hash`:"L" 模式下 tobytes 与 getdata 等价,且不吃弃用警告
        pixels = list(grey.tobytes())
        stat = ImageStat.Stat(rgb)

    # 细节丰富度:相邻像素差
    diffs = [abs(pixels[i] - pixels[i - 1]) for i in range(1, len(pixels))]
    detail = sum(diffs) / len(diffs) if diffs else 0.0
    detail_score = min(100.0, detail * 6.0)

    # 亮度居中最好,过暗过曝都扣分
    brightness = sum(stat.mean) / 3.0
    exposure_score = max(0.0, 100.0 - abs(brightness - 128.0) * 0.78)

    return round(detail_score * 0.6 + exposure_score * 0.4, 2)


@dataclass(frozen=True)
class RankedCandidate:
    """预排序结果。``depth`` 决定这张图接下来做完整评分还是快速检查。"""

    key: str
    rank: int
    similarity: float
    realism: float
    rank_score: float
    depth: EvaluationDepth

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "rank": self.rank,
            "similarity": self.similarity,
            "realism": self.realism,
            "rank_score": self.rank_score,
            "depth": self.depth.value,
        }


def rank_candidates(
    candidates: list[tuple[str, bytes]],
    reference_images: list[bytes],
    *,
    enabled: bool = True,
    full_evaluation_count: int = 2,
) -> list[RankedCandidate]:
    """给一轮候选图排名。

    ``enabled=False`` 时不做任何像素计算,全部标记为完整评分 ——
    这是需求第十三章要求的对照组开关,用来比较成本与准确率。
    """
    if not enabled:
        return [
            RankedCandidate(
                key=key,
                rank=index,
                similarity=0.0,
                realism=0.0,
                rank_score=0.0,
                depth=EvaluationDepth.FULL,
            )
            for index, (key, _) in enumerate(candidates)
        ]

    reference_hashes = [average_hash(data) for data in reference_images]

    scored: list[tuple[str, float, float, float]] = []
    for key, data in candidates:
        similarity = similarity_score(average_hash(data), reference_hashes)
        realism = realism_score(data)
        # 相似度权重更高:衣服不对的图再好看也没用
        rank_score = round(similarity * 0.6 + realism * 0.4, 2)
        scored.append((key, similarity, realism, rank_score))

    # 排序键带上 key,保证同分时顺序稳定可复现
    scored.sort(key=lambda row: (-row[3], row[0]))

    limit = max(1, full_evaluation_count)
    return [
        RankedCandidate(
            key=key,
            rank=index,
            similarity=similarity,
            realism=realism,
            rank_score=rank_score,
            depth=EvaluationDepth.FULL if index < limit else EvaluationDepth.QUICK,
        )
        for index, (key, similarity, realism, rank_score) in enumerate(scored)
    ]
