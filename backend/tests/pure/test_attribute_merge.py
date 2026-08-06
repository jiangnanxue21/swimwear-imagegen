"""M4:合并规则、校准分箱、投影约束(零三方依赖)。

合并规则是整条链路上**最需要能穷举**的部分:它决定一个模型的猜测
会不会变成一条真的发到平台上的商品属性。所以它是纯函数,
所以这里能把每个分支都跑一遍。
"""
from __future__ import annotations

import ast

from app.attributes.calibration import (
    DEFAULT_BIN_EDGES,
    LabelledSample,
    bin_index,
    build_bins,
    evaluate_readiness,
)
from app.attributes.confidence import (
    MIN_SAMPLES_PER_BIN,
    MIN_SAMPLES_PER_FIELD,
    CalibrationBin,
)
from app.core.enums import AttributeSource, MediaSource
from app.extractors.merge import (
    MERGE_DISSENT_MAX,
    EvidenceItem,
    MergeOutcome,
    merge_all,
    merge_field,
)
from tests.pure._helpers import BACKEND_ROOT

APP_DIR = BACKEND_ROOT / "app"


def _ev(value, *, weight=0.9, source=AttributeSource.EXTRACTED.value,
        media=MediaSource.MANUAL_UPLOAD.value, eid=None):
    return EvidenceItem(
        field_name="neckline_type",
        normalized_value=value,
        weight=weight,
        source=source,
        media_source=media,
        evidence_id=eid or f"e-{value}-{weight}",
    )


# ---------------------------------------------------------------- 来源优先级


def test_supplier_data_beats_more_numerous_model_evidence():
    """分层,不是加权求和。

    把「供应商说的」和「模型看图说的」放进同一个加权和里,意味着
    足够多的模型证据可以压过一条供应商数据 —— 而那两者的可信度
    不是同一个量纲上的东西。
    """
    result = merge_field("neckline_type", [
        _ev("HALTER", source=AttributeSource.SUPPLIER.value, weight=0.5),
        _ev("V_NECK", weight=0.95),
        _ev("V_NECK", weight=0.95),
        _ev("V_NECK", weight=0.95),
    ])
    assert result.outcome is MergeOutcome.RESOLVED
    assert result.value == "HALTER", "三条模型证据压过了一条供应商数据"


def test_manual_value_is_never_overwritten():
    """人工值不一致时判 CONFLICTS_WITH_MANUAL,**不改值**(§6.2 规则 4)。"""
    result = merge_field(
        "neckline_type",
        [_ev("V_NECK", weight=0.99), _ev("V_NECK", weight=0.99)],
        existing_value="HALTER",
        existing_source=AttributeSource.MANUAL.value,
    )
    assert result.outcome is MergeOutcome.CONFLICTS_WITH_MANUAL
    assert "人工值不会被自动覆盖" in " ".join(result.reasons)


def test_manual_value_that_agrees_is_not_a_conflict():
    result = merge_field(
        "neckline_type",
        [_ev("V_NECK", weight=0.9)],
        existing_value="V_NECK",
        existing_source=AttributeSource.MANUAL.value,
    )
    assert result.outcome is MergeOutcome.RESOLVED


# ---------------------------------------------------------------- AI 图


def test_ai_only_evidence_cannot_decide_a_value():
    """只有 AI 图证据时不足以定值(硬规矩 6)。

    AI 生成图可能改变领型、颜色、遮盖度 —— 它不是"一张证明力稍弱的图",
    而是"一张可能已经不是这件商品的图"。
    """
    result = merge_field("neckline_type", [
        _ev("V_NECK", media=MediaSource.AI_GENERATED.value, weight=0.99),
        _ev("V_NECK", media=MediaSource.AI_GENERATED.value, weight=0.99),
    ])
    assert result.outcome is MergeOutcome.INSUFFICIENT
    assert result.value is None
    assert "AI" in " ".join(result.reasons)


def test_ai_divergence_is_flagged_but_not_a_conflict():
    """原图与 AI 图不一致 -> 标记,不判 CONFLICT(§6.2 规则 5)。

    它通常意味着**生成图改变了商品外观**,是生成链路的质量信号,
    不是属性识别出了问题。判成 CONFLICT 会让人去查属性,而该查的是出图。
    """
    result = merge_field("neckline_type", [
        _ev("V_NECK", weight=0.9),
        _ev("V_NECK", weight=0.9),
        _ev("HALTER", media=MediaSource.AI_GENERATED.value, weight=0.9),
    ])
    assert result.outcome is MergeOutcome.RESOLVED
    assert result.value == "V_NECK"
    assert result.ai_divergence is True
    assert "AI_DIVERGENCE" in " ".join(result.reasons)


def test_ai_agreeing_with_originals_is_not_divergence():
    result = merge_field("neckline_type", [
        _ev("V_NECK", weight=0.9),
        _ev("V_NECK", media=MediaSource.AI_GENERATED.value, weight=0.9),
    ])
    assert result.ai_divergence is False


# ---------------------------------------------------------------- 分歧


def test_split_within_the_top_tier_is_a_conflict():
    """同层分歧超过上限判 CONFLICT。

    属性字段上的二比一通常说明那个特征本身就有歧义(MIDI 还是 MAXI),
    正是最该让人看一眼的情况。
    """
    result = merge_field("neckline_type", [
        _ev("V_NECK", weight=0.9),
        _ev("HALTER", weight=0.9),
    ])
    assert result.outcome is MergeOutcome.CONFLICT
    assert result.rejected_values == ("HALTER",) or result.value == "HALTER"


def test_small_dissent_still_resolves():
    """三比一(25% 分歧)在默认上限 34% 之内,照常出结论。"""
    assert MERGE_DISSENT_MAX > 0.25
    result = merge_field("neckline_type", [
        _ev("V_NECK", weight=1.0, eid="a"),
        _ev("V_NECK", weight=1.0, eid="b"),
        _ev("V_NECK", weight=1.0, eid="c"),
        _ev("HALTER", weight=1.0, eid="d"),
    ])
    assert result.outcome is MergeOutcome.RESOLVED
    assert result.value == "V_NECK"
    assert result.agreeing_votes == 3 and result.total_votes == 4


def test_zero_weight_evidence_produces_no_value():
    """全部权重为零(未校准)时不硬凑一个值出来。

    这是 fail closed 在合并层的落点:未校准 -> system_confidence 为 None
    -> 权重 0 -> INSUFFICIENT。
    """
    result = merge_field("neckline_type", [_ev("V_NECK", weight=0.0)])
    assert result.outcome is MergeOutcome.INSUFFICIENT


def test_unnormalized_evidence_is_ignored():
    """归一失败的证据不参与合并 —— 模型自造的枚举值不能流到下游。"""
    result = merge_field("neckline_type", [_ev(None), _ev(None)])
    assert result.outcome is MergeOutcome.NO_EVIDENCE


def test_merge_is_deterministic_on_ties():
    """同分时按值排序,保证可复现。"""
    a = merge_field("neckline_type", [_ev("A", weight=1.0), _ev("B", weight=1.0)])
    b = merge_field("neckline_type", [_ev("B", weight=1.0), _ev("A", weight=1.0)])
    assert a.value == b.value


def test_merge_all_groups_by_field():
    results = merge_all([
        EvidenceItem(field_name="neckline_type", normalized_value="V_NECK", weight=0.9),
        EvidenceItem(field_name="back_style", normalized_value="CROSS_BACK", weight=0.9),
    ])
    assert set(results) == {"neckline_type", "back_style"}


# ---------------------------------------------------------------- 校准


def test_bins_are_finer_at_the_top():
    """阈值都在 0.9 以上,那一段必须切得更细。

    0.3 和 0.4 之间的差别对决策没有任何影响,0.92 和 0.95 之间有。
    """
    top = [e for e in DEFAULT_BIN_EDGES if e >= 0.85]
    bottom = [e for e in DEFAULT_BIN_EDGES if e < 0.85]
    assert len(top) >= len(bottom)


def test_bin_index_covers_the_whole_range():
    assert bin_index(0.0) == 0
    assert bin_index(1.0) == len(DEFAULT_BIN_EDGES) - 2
    assert bin_index(-5) == 0 and bin_index(99) == len(DEFAULT_BIN_EDGES) - 2


def test_build_bins_omits_empty_buckets():
    """空箱不补零。

    一条 `sample_count=0, observed_accuracy=0` 会被 `calibrated()` 当成
    "有校准数据但准确率是 0",而实际是"这一箱没有任何样本"。
    """
    bins = build_bins(
        [LabelledSample(0.96, True), LabelledSample(0.96, False)],
        calibration_version="v1",
    )
    assert len(bins) == 1
    assert bins[0].sample_count == 2
    assert bins[0].observed_accuracy == 0.5


def test_readiness_says_what_is_missing():
    """未就绪时要说清楚差在哪,否则运维只看到"未就绪"却不知道该做什么。"""
    empty = evaluate_readiness(
        field_name="neckline_type", model_name="m", prompt_version="1", bins=[]
    )
    assert not empty.ready and "没有任何校准数据" in empty.reason

    thin = evaluate_readiness(
        field_name="neckline_type", model_name="m", prompt_version="1",
        bins=[CalibrationBin(0.9, 1.0, 0.95, 10, "v1")],
    )
    assert not thin.ready and str(MIN_SAMPLES_PER_FIELD) in thin.reason

    ok = evaluate_readiness(
        field_name="neckline_type", model_name="m", prompt_version="1",
        bins=[
            CalibrationBin(0.0, 0.9, 0.7, MIN_SAMPLES_PER_FIELD, "v1"),
            CalibrationBin(0.9, 1.0, 0.96, MIN_SAMPLES_PER_BIN, "v1"),
        ],
    )
    assert ok.ready and ok.usable_bins == 2


# ---------------------------------------------------------------- 投影


def test_projection_columns_are_marked():
    """8 个兼容投影列必须带程序可读的标记。

    靠注释约束不住任何东西 —— 下面那条 AST 扫描要靠这个标记
    才能知道哪些列是投影。
    """
    source = (APP_DIR / "models" / "product.py").read_text(encoding="utf-8")
    assert source.count('info={"projection": True}') == 8


def test_nobody_writes_the_projection_columns_directly():
    """除投影函数外,任何代码不得给这 8 个列赋值(§5.4 第一条防线)。

    双事实源的表现是"这个字段为什么是这个值"有两个互相矛盾的答案,
    而且两边都看起来对。
    """
    columns = {
        "primary_color", "secondary_colors", "pattern_type", "garment_type",
        "material", "strap_type", "neckline_type", "back_style",
    }
    allowed = {"attributes/service.py", "services/product_service.py",
               "services/product_import.py"}
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        rel = str(path.relative_to(APP_DIR))
        if rel in allowed or rel.startswith("models/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in columns
                    and isinstance(target.value, ast.Name)
                    and "product" in target.value.id.lower()
                ):
                    offenders.append(f"{rel}:{node.lineno} 直接写了 {target.attr}")
    assert not offenders, "投影列被直接写入 -> " + "; ".join(offenders)


def test_evaluation_reads_attributes_through_the_service():
    """生成链路改读 `ProductAttributeService`(M4 出口条件)。"""
    source = (APP_DIR / "services" / "evaluation_service.py").read_text(encoding="utf-8")
    assert "attr_service.metadata_for_evaluation" in source
    assert "product_metadata(product, session)" in source


def test_breakdown_is_keyed_by_the_winning_value():
    """因子分解必须对应**胜出值**,不能是"该字段第一条证据"。

    界面上"为什么是 0.71"展示的因子,运营据此判断该补一张图还是重拍。
    如果那份因子来自一条被淘汰的证据,判断的依据就是错的 ——
    而且错得完全看不出来。
    """
    source = (APP_DIR / "attributes" / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "apply_evidence"
    )
    body = ast.unparse(fn)
    assert "breakdowns.get((field_name, result.value))" in body, (
        "因子分解没有按胜出值取"
    )
    assert "per_field_breakdown" not in body, "又退回按字段存了"
