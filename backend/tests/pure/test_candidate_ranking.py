"""轮内候选图预排序(需求第十三章)。"""
from __future__ import annotations

import io
from functools import cache

from app.core.enums import EvaluationDepth
from app.evaluators.ranking import (
    average_hash,
    hamming_distance,
    rank_candidates,
    realism_score,
    similarity_score,
)
from tests.pure._helpers import jpeg_bytes


@cache
def _noisy(seed: int, size=(256, 320)) -> bytes:
    """有细节的图。用固定 seed,保证测试可复现。

    加缓存是因为同一颗 seed 在本文件里要用很多次,而生成一张 256x320 的
    随机图并不比被测代码便宜。返回的是 bytes,不可变,共享安全。
    """
    import random

    from PIL import Image

    rnd = random.Random(seed)
    img = Image.new("RGB", size)
    img.putdata([
        (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
        for _ in range(size[0] * size[1])
    ])
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ---------- 感知哈希 ----------

def test_average_hash_is_deterministic():
    data = jpeg_bytes(300, 400)
    assert average_hash(data) == average_hash(data)


def test_identical_images_have_zero_distance():
    data = _noisy(1)
    assert hamming_distance(average_hash(data), average_hash(data)) == 0


def test_hash_survives_rescaling():
    """均值哈希对缩放稳健,否则不同尺寸的候选图没法比。"""
    from PIL import Image

    original = _noisy(7, (256, 256))
    with Image.open(io.BytesIO(original)) as img:
        buf = io.BytesIO()
        img.resize((128, 128)).save(buf, format="JPEG", quality=90)
        rescaled = buf.getvalue()

    assert hamming_distance(average_hash(original), average_hash(rescaled)) <= 12


def test_different_images_have_nonzero_distance():
    assert hamming_distance(average_hash(_noisy(1)), average_hash(_noisy(999))) > 0


# ---------- 相似度 ----------

def test_similarity_is_100_for_identical_image():
    data = _noisy(3)
    assert similarity_score(average_hash(data), [average_hash(data)]) == 100.0


def test_similarity_takes_the_closest_reference():
    """商品有正反细节多张原图,一张试穿正面图本来就只该像其中一张。"""
    target = _noisy(5)
    close = average_hash(target)
    far = average_hash(_noisy(88))
    assert similarity_score(close, [far, close]) == 100.0


def test_similarity_without_reference_is_neutral():
    assert similarity_score(average_hash(_noisy(2)), []) == 50.0


def test_similarity_is_bounded():
    value = similarity_score(average_hash(_noisy(1)), [average_hash(_noisy(2))])
    assert 0.0 <= value <= 100.0


# ---------- 真实性代理指标 ----------

def test_flat_colour_image_scores_low_on_realism():
    """纯色块明显不可用,应该被压到很低。"""
    flat = jpeg_bytes(256, 256, color=(128, 128, 128))
    assert realism_score(flat) < realism_score(_noisy(4))


def test_realism_is_deterministic():
    data = _noisy(6)
    assert realism_score(data) == realism_score(data)


def test_realism_is_bounded():
    for data in (jpeg_bytes(256, 256, (0, 0, 0)), jpeg_bytes(256, 256, (255, 255, 255)), _noisy(9)):
        assert 0.0 <= realism_score(data) <= 100.0


def test_overexposed_image_is_penalised():
    blown = jpeg_bytes(256, 256, color=(255, 255, 255))
    balanced = jpeg_bytes(256, 256, color=(128, 128, 128))
    assert realism_score(blown) < realism_score(balanced)


# ---------- 排序 ----------

def test_ranking_result_shape():
    """结果形状:候选数量、完整评分的名额、rank 连续性。

    需求第十三章:前两名完整评分,其余快速硬错误检查;名额可配;
    至少留一个完整评分的名额(否则一轮下来没有任何一张被真正评过)。

    (原先这几条各是一个用例、每个都重新生成并处理四张图,是纯测试里
    最明显的耗时区域之一。合并成一张表之后只跑一遍像素计算。)
    """
    candidates = [(f"c{i}", _noisy(i)) for i in range(4)]
    references = [_noisy(0)]

    #  完整评分名额, 候选数, 期望的 FULL 数量, 说明
    cases = [
        (2, 4, 2, "默认:前两名完整评分,后两名快速检查"),
        (3, 4, 3, "名额可配"),
        (0, 1, 1, "名额为 0 也至少留一个,否则没有一张被真正评过"),
    ]
    for full_count, take, expected_full, why in cases:
        ranked = rank_candidates(candidates[:take], references, full_evaluation_count=full_count)

        assert len(ranked) == take, why
        assert [r.rank for r in ranked] == list(range(take)), f"rank 必须连续: {why}"

        depths = [r.depth for r in ranked]
        assert depths.count(EvaluationDepth.FULL) == expected_full, why
        # FULL 必须排在前面,不能和 QUICK 交错
        assert depths == (
            [EvaluationDepth.FULL] * expected_full
            + [EvaluationDepth.QUICK] * (take - expected_full)
        ), why

    # 没有参考图时依然要给出完整的一份排序,不能少了谁
    assert len(rank_candidates(candidates, [])) == 4


def test_candidate_closest_to_the_product_ranks_first():
    reference = _noisy(42)
    candidates = [("far", _noisy(7)), ("exact", reference), ("other", _noisy(13))]
    ranked = rank_candidates(candidates, [reference])
    assert ranked[0].key == "exact"


def test_ranking_can_be_disabled():
    """需求第十三章原文:该功能必须可以关闭,以便比较成本和准确率。"""
    candidates = [(f"c{i}", _noisy(i)) for i in range(4)]
    ranked = rank_candidates(candidates, [_noisy(0)], enabled=False)

    assert all(r.depth is EvaluationDepth.FULL for r in ranked), "关闭后全部走完整评分"
    assert [r.key for r in ranked] == ["c0", "c1", "c2", "c3"], "关闭后保持原顺序"
    assert all(r.similarity == 0.0 and r.realism == 0.0 for r in ranked), "关闭后不做像素计算"


def test_ranking_is_deterministic():
    candidates = [(f"c{i}", _noisy(i)) for i in range(4)]
    references = [_noisy(0)]
    first = [r.to_dict() for r in rank_candidates(candidates, references)]
    for _ in range(5):
        assert [r.to_dict() for r in rank_candidates(candidates, references)] == first


def test_ranking_order_does_not_depend_on_input_order():
    candidates = [(f"c{i}", _noisy(i)) for i in range(4)]
    references = [_noisy(0)]
    forward = [r.key for r in rank_candidates(candidates, references)]
    backward = [r.key for r in rank_candidates(list(reversed(candidates)), references)]
    assert forward == backward


def test_empty_candidate_list_is_handled():
    assert rank_candidates([], [_noisy(0)]) == []
