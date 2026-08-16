"""证据合并(M4,§6.2)。**零依赖纯函数。**

输入是一堆证据 + 已有的值 + 配置，输出是一个决策。不碰数据库、不发网络、
不看时间——所以「这张图为什么被判成 V_NECK」永远能在一个不带数据库的
单元测试里复现。

## 来源优先级(高 → 低)

    1  MANUAL              人工确认。**永不被自动覆盖**
    2  SUPPLIER / IMPORTED  供应商给的结构化字段(不是描述文本)
    3  EXTRACTED 原始素材   主证据
    4  EXTRACTED AI 生成图  只做一致性旁证,不能单独定值
    5  DEFAULT              类目默认值,仅用于非关键字段

分层而不是打分:把「供应商说的」和「模型看图说的」放进同一个加权和里，
意味着足够多的模型证据可以压过一条供应商数据——而那两者的可信度
不是同一个量纲上的东西。

## AI 图不能单独定值

硬规矩 6。AI 生成图可能改变领型、颜色、遮盖度，因此它不是「一张证明力
稍弱的图」，而是「一张可能已经不是这件商品的图」。

只有 AI 证据时结论是 `INSUFFICIENT`（转人工），不是「置信度低一点的结论」。
而 AI 与原图**不一致**时不判 CONFLICT——那通常意味着生成图改变了商品外观，
是生成链路的质量信号，应当被单独标记(`AI_DIVERGENCE`)并进 Dashboard。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.core.enums import AttributeSource, MediaSource

#: 同层内部分歧超过这个比例就判 CONFLICT。
#:
#: 0.34 的含义是「三张图里有一张不同意就够了」。定得更松(比如 0.5)的话，
#: 二比一的分歧会被当成多数通过——而属性字段上的二比一通常说明
#: 那个特征本身就有歧义(MIDI 还是 MAXI),正是最该让人看一眼的情况。
MERGE_DISSENT_MAX = 0.34

#: 来源分层。数字小的层赢，同层才比较票数。
SOURCE_TIER: Mapping[str, int] = {
    AttributeSource.MANUAL.value: 1,
    AttributeSource.SUPPLIER.value: 2,
    AttributeSource.IMPORTED.value: 2,
    AttributeSource.EXTRACTED.value: 3,     # 原始素材;AI 图在下面降层
    AttributeSource.DERIVED.value: 4,
    AttributeSource.DEFAULT.value: 5,
}

#: AI 生成图的证据降一层。它不是「弱一点的原始素材」，
#: 而是「可能已经不是这件商品」的素材。
AI_TIER = 4


class MergeOutcome(StrEnum):
    """合并结论。**不是属性状态** —— 状态由 `decision.decide_status` 定。

    分开是因为这两件事回答的问题不同:这里回答「证据指向哪个值」，
    那里回答「这个值够不够格被采信」。合并成一个的话，
    调整阈值会连带改变「选哪个值」，而那两件事不该耦合。
    """

    #: 有明确结论
    RESOLVED = "RESOLVED"
    #: 最高层内部分歧过大
    CONFLICT = "CONFLICT"
    #: 与已有的人工值不一致 —— **永不自动覆盖**
    CONFLICTS_WITH_MANUAL = "CONFLICTS_WITH_MANUAL"
    #: 只有 AI 图证据,不足以定值
    INSUFFICIENT = "INSUFFICIENT"
    #: 一条可用证据都没有
    NO_EVIDENCE = "NO_EVIDENCE"


@dataclass(frozen=True)
class EvidenceItem:
    """参与合并的一条证据。

    刻意做得很窄:只有合并规则真正要用的字段。传整个 ORM 对象进来的话，
    这个函数就会开始依赖数据库的形状，而它的全部价值在于不依赖。
    """

    field_name: str
    normalized_value: str | None
    #: 已校准权重。None 表示「尚未校准」，不是 0 分：当同层证据全部为 None
    #: 时只用等权投票选一个 CANDIDATE；是否采信仍由 decision 层按
    #: system_confidence=None 判定。显式 0.0 仍表示这条证据没有证明力。
    weight: float | None = 1.0
    source: str = AttributeSource.EXTRACTED.value
    #: 素材来源。AI 生成图会被降层
    media_source: str = MediaSource.MANUAL_UPLOAD.value
    media_asset_id: str | None = None
    evidence_id: str | None = None

    @property
    def tier(self) -> int:
        base = SOURCE_TIER.get(self.source, 5)
        if (
            self.source == AttributeSource.EXTRACTED.value
            and self.media_source == MediaSource.AI_GENERATED.value
        ):
            return AI_TIER
        return base

    @property
    def is_ai(self) -> bool:
        return self.media_source == MediaSource.AI_GENERATED.value


@dataclass(frozen=True)
class MergeResult:
    field_name: str
    outcome: MergeOutcome
    value: str | None = None
    #: 支撑这个结论的证据 id
    evidence_ids: tuple[str, ...] = ()
    #: 参与投票的证据总数与同意数。进 agreement_factor
    total_votes: int = 0
    agreeing_votes: int = 0
    #: 原图与 AI 图结论不一致。**不是 CONFLICT** ——
    #: 它通常意味着生成图改变了商品外观,是生成链路的质量信号
    ai_divergence: bool = False
    reasons: tuple[str, ...] = ()
    #: 被淘汰的候选值,给界面做对照
    rejected_values: tuple[str, ...] = ()


def merge_field(
    field_name: str,
    evidence: Sequence[EvidenceItem],
    *,
    existing_value: str | None = None,
    existing_source: str | None = None,
    dissent_max: float = MERGE_DISSENT_MAX,
) -> MergeResult:
    """合并一个字段的全部证据。

    算法(§6.2):
      1. 按字段收集证据,按优先级分层;
      2. 取最高层,同层内按权重投票;
      3. 同层分歧比例 > `dissent_max` -> CONFLICT;
      4. 结论与已有 MANUAL 值不同 -> CONFLICTS_WITH_MANUAL,**永不自动覆盖**;
      5. 原图与 AI 图不一致 -> 不判 CONFLICT,标 `ai_divergence` 并降权。
    """
    usable = [e for e in evidence if e.normalized_value]
    if not usable:
        return MergeResult(
            field_name=field_name,
            outcome=MergeOutcome.NO_EVIDENCE,
            reasons=("没有可用证据(全部归一化失败或为空)",),
        )

    top_tier = min(e.tier for e in usable)
    tier_items = [e for e in usable if e.tier == top_tier]

    # 只有 AI 证据:不足以定值(硬规矩 6)
    if tier_items and all(e.is_ai for e in tier_items):
        return MergeResult(
            field_name=field_name,
            outcome=MergeOutcome.INSUFFICIENT,
            total_votes=len(tier_items),
            reasons=(
                "本字段只有 AI 生成图的证据。AI 图可能改变领型、颜色、遮盖度,"
                "不能作为唯一来源;请补一张原始素材或人工确认",
            ),
        )

    # 同层按权重投票。校准回答「模型自报置信度对应多少真实准确率」，缺席时
    # 不能自动确认，但也不能把模型已经看见的值从候选表里抹掉。只有在**同层
    # 全部未校准**时退回等权；选出的值随后仍带 system_confidence=None，
    # decision 层只能把它写成 CANDIDATE。
    #
    # 混合状态不退回：只让已有校准权重的证据参与。否则一条未校准证据会被
    # 临时赋成 1.0，反而压过一条真实校准为 0.7 的证据。
    all_uncalibrated = all(item.weight is None for item in tier_items)
    scores: Counter[str] = Counter()
    for item in tier_items:
        vote_weight = 1.0 if all_uncalibrated else (item.weight or 0.0)
        scores[item.normalized_value] += max(0.0, vote_weight)  # type: ignore[index]
    if not scores or all(v <= 0 for v in scores.values()):
        # 明确算出的权重全部为零。不猜一个值出来。未校准不是这一档，
        # 它由上面的 all_uncalibrated 等权分支处理。
        return MergeResult(
            field_name=field_name,
            outcome=MergeOutcome.INSUFFICIENT,
            total_votes=len(tier_items),
            reasons=("全部已校准证据的权重为零,不据此定值",),
        )

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    winner, winner_score = ranked[0]
    total_score = sum(scores.values())
    dissent = 1.0 - (winner_score / total_score) if total_score else 1.0

    agreeing = sum(1 for e in tier_items if e.normalized_value == winner)
    rejected = tuple(v for v, _ in ranked[1:])

    if dissent > dissent_max:
        return MergeResult(
            field_name=field_name,
            outcome=MergeOutcome.CONFLICT,
            value=winner,
            total_votes=len(tier_items),
            agreeing_votes=agreeing,
            rejected_values=rejected,
            evidence_ids=tuple(
                e.evidence_id for e in tier_items if e.evidence_id
            ),
            reasons=(
                f"同层证据分歧 {dissent:.0%} 超过上限 {dissent_max:.0%};"
                f"候选值:{winner}(胜出)、{'、'.join(rejected)}",
            ),
        )

    # AI 分歧:不影响结论,但要标记出来
    ai_values = {e.normalized_value for e in usable if e.is_ai and e.normalized_value}
    divergent = bool(ai_values) and winner not in ai_values

    reasons = [f"最高优先级层(第 {top_tier} 层)中 {agreeing}/{len(tier_items)} 条证据支持 {winner}"]
    if all_uncalibrated:
        reasons.append("本字段尚未校准,仅按等权选择候选值;不会自动确认")
    if divergent:
        reasons.append(
            f"AI 生成图给出的是 {'、'.join(sorted(ai_values))},与原始素材不一致 —— "
            "这通常意味着生成图改变了商品外观,已标记 AI_DIVERGENCE"
        )

    # 与人工值冲突:**永不自动覆盖**(规则 4)
    if (
        existing_source == AttributeSource.MANUAL.value
        and existing_value is not None
        and existing_value != winner
    ):
        return MergeResult(
            field_name=field_name,
            outcome=MergeOutcome.CONFLICTS_WITH_MANUAL,
            value=winner,
            total_votes=len(tier_items),
            agreeing_votes=agreeing,
            ai_divergence=divergent,
            rejected_values=rejected,
            evidence_ids=tuple(e.evidence_id for e in tier_items if e.evidence_id),
            reasons=(
                f"合并结论 {winner} 与人工确认的 {existing_value} 不一致;"
                "人工值不会被自动覆盖,请人工裁决",
                *reasons,
            ),
        )

    return MergeResult(
        field_name=field_name,
        outcome=MergeOutcome.RESOLVED,
        value=winner,
        total_votes=len(tier_items),
        agreeing_votes=agreeing,
        ai_divergence=divergent,
        rejected_values=rejected,
        evidence_ids=tuple(e.evidence_id for e in tier_items if e.evidence_id),
        reasons=tuple(reasons),
    )


def merge_all(
    evidence: Sequence[EvidenceItem],
    *,
    existing: Mapping[str, tuple[str | None, str | None]] | None = None,
    dissent_max: float = MERGE_DISSENT_MAX,
) -> dict[str, MergeResult]:
    """按字段分组合并。``existing`` 是 {字段: (当前值, 当前来源)}。"""
    grouped: dict[str, list[EvidenceItem]] = {}
    for item in evidence:
        grouped.setdefault(item.field_name, []).append(item)
    current = existing or {}
    results: dict[str, MergeResult] = {}
    for name, items in grouped.items():
        value, source = current.get(name, (None, None))
        results[name] = merge_field(
            name, items, existing_value=value, existing_source=source,
            dissent_max=dissent_max,
        )
    return results
