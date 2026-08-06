"""评分器接口与结构化评分结果(需求第十章)。

评分结果**必须是结构化数据**,不接受不可解析的自由文本。
因此这一层做两件事:

1. 定义与需求书完全一致的抽象方法 ``evaluate(...) -> dict``;
2. 在基类上提供 ``evaluate_structured(...)``,把 dict 过一遍严格解析,
   得到带类型的 ``EvaluationResult``。

编排层只用后者。任何评分器返回了模型自由发挥的散文,都会在解析处炸掉,
而不是把一段无法追责的文本写进数据库。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import (
    EvaluationDepth,
    Grade,
    ProblemSeverity,
    RecommendedAction,
)

#: 评分维度。顺序即报表展示顺序,增删维度必须同步 scoring.DEFAULT_WEIGHTS。
SCORE_DIMENSIONS: tuple[str, ...] = (
    "garment_identity",       # 商品身份一致性
    "structural_fidelity",    # 结构一致性
    "color_fidelity",         # 颜色一致性
    "pattern_fidelity",       # 图案一致性
    "anatomy_realism",        # 人体真实性
    "face_realism",           # 面部真实性
    "skin_realism",           # 皮肤真实性
    "lighting_realism",       # 光照真实性
    "background_realism",     # 背景真实性
    "composition",            # 构图
    "website_usability",      # 网站可用性
)

DIMENSION_LABELS: dict[str, str] = {
    "garment_identity": "商品身份一致性",
    "structural_fidelity": "结构一致性",
    "color_fidelity": "颜色一致性",
    "pattern_fidelity": "图案一致性",
    "anatomy_realism": "人体真实性",
    "face_realism": "面部真实性",
    "skin_realism": "皮肤真实性",
    "lighting_realism": "光照真实性",
    "background_realism": "背景真实性",
    "composition": "构图",
    "website_usability": "网站可用性",
}


@dataclass(frozen=True)
class EvaluationProblemItem:
    """单条问题。

    ``hard_fail`` 与 ``severity`` 是两个维度:硬错误一定是 CRITICAL,
    但 CRITICAL 不一定是需求第十一章列出的硬错误代码(例如评分器自定义的严重问题)。
    分档时两者都会被考虑。
    """

    code: str
    severity: ProblemSeverity
    message: str
    dimension: str | None = None
    hard_fail: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "dimension": self.dimension,
            "hard_fail": self.hard_fail,
        }


@dataclass
class EvaluationResult:
    """一张候选图的完整评分结论。

    ``overall_score`` 永远是后端按权重算出来的;评分器自报的总分只留在
    ``model_reported_overall`` 里,用于监控大模型的打分漂移(需求第十章)。
    """

    scores: dict[str, float]
    overall_score: float
    hard_fail: bool = False
    hard_fail_codes: list[str] = field(default_factory=list)
    problems: list[EvaluationProblemItem] = field(default_factory=list)
    uncertain_items: list[str] = field(default_factory=list)
    depth: EvaluationDepth = EvaluationDepth.FULL
    evaluator: str = ""
    model_name: str | None = None
    model_reported_overall: float | None = None
    #: 由 rules.grade_candidate 填写,评分器自己不做分档
    grade: Grade | None = None
    recommended_action: RecommendedAction | None = None
    grade_reasons: list[str] = field(default_factory=list)
    duration_ms: int | None = None
    #: §6.5 事实一致性维度组的逐项结论。
    #:
    #: **单独一个字段而不是塞进 `problems`**:problems 只记"出了什么问题",
    #: 而这一组里"查了没问题"和"没查"必须在审核页上分得开 —— 一份看起来
    #: 全绿的报告如果实际只查了两项,审核的人会误以为其余七项都过了。
    #: 判定与形状在 `evaluators/fact_consistency.py`,这里只搬运。
    fact_findings: tuple[Any, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def worst_severity(self) -> ProblemSeverity | None:
        if not self.problems:
            return None
        order = {ProblemSeverity.MINOR: 0, ProblemSeverity.MAJOR: 1, ProblemSeverity.CRITICAL: 2}
        return max((p.severity for p in self.problems), key=lambda s: order[s])

    def to_dict(self) -> dict[str, Any]:
        """需求第十章示例格式。写库与 API 都用它。"""
        return {
            "hard_fail": self.hard_fail,
            "hard_fail_codes": list(self.hard_fail_codes),
            "scores": dict(self.scores),
            "overall_score": self.overall_score,
            "uncertain_items": list(self.uncertain_items),
            "problems": [p.to_dict() for p in self.problems],
            "recommended_action": (
                self.recommended_action.value if self.recommended_action else None
            ),
            "grade": self.grade.value if self.grade else None,
            "depth": self.depth.value,
            "evaluator": self.evaluator,
            "model_name": self.model_name,
            "model_reported_overall": self.model_reported_overall,
            "fact_consistency": [f.to_dict() for f in self.fact_findings],
        }


class ImageQualityEvaluator(ABC):
    """评分器接口(需求第十章)。

    ``evaluate`` 的签名与需求书逐字一致,返回原始 dict。
    编排层不直接调用它,而是调用下面的 ``evaluate_structured``。
    """

    evaluator_name: str = "abstract"
    display_name: str = ""

    #: 这个评分器给的分是不是**本地编的**。`core/environment` 的真实性判定读它。
    #:
    #: **默认 True(先假设是假的)**,与 `providers/base.py` 那一行同一条理由,
    #: 而且这一侧的代价更直接:Mock 按文件指纹给分,真实商品图有相当比例会被
    #: 判成 A 档,于是自动通过、自动出图、自动发布。本模块所在文件的注册表
    #: 顶部那段注释讲的就是这件事 —— 它讲的是**退回** Mock 的危险,而状态条
    #: 说错的危险是同一个,只是运营连"我在用 Mock"都不知道。
    #:
    #: 注意它和下面的 `costs_money` 不是同一件事,方向也不是简单相反:
    #: 一个配错了的真评分器(UNCONFIGURED)不花钱,但它也不是模拟器。
    is_simulator: bool = True

    #: 这个评分器的每一次调用是不是**要花钱**。
    #:
    #: 默认 False:Mock 按文件指纹给分,不发任何请求。视觉大模型评分器覆盖成 True,
    #: 于是它的每一次调用都会进 `provider_usage_records`。
    #:
    #: 为什么需要这个标记而不是「凡是评分都记一笔」:那样看板上会出现一堆
    #: Mock 的零元调用,把真实开销的占比冲淡。也不能反过来「凡是有 model_name
    #: 就记一笔」—— 那是从结果反推身份,而结果可能缺失(超时的那次尤其)。
    billable: bool = False

    #: 计费主体的名字,进流水的 `provider` 列。
    #:
    #: 默认用评分器自己的名字。视觉评分器覆盖成**从 base_url 推出来的厂商名** ——
    #: 硬规矩 4:这一列要能追溯到真实来源,不许为了接口形状完整填常量。
    @property
    def usage_provider(self) -> str:
        return self.evaluator_name

    @abstractmethod
    async def evaluate(
        self,
        product_assets: list[str],
        candidate_image: str,
        product_metadata: dict,
        rule_set: dict,
    ) -> dict:
        ...

    def is_configured(self) -> bool:
        """缺 Key / 缺地址时返回 False。只读配置,不发网络请求。"""
        return True

    def resolved_model_name(self, payload: dict) -> str | None:
        """这次评分**实际**由哪个模型完成。

        由评分器自己回答,而不是从模型输出里读:``model_name`` 会进审计记录,
        是回答"三个月前这批图是谁打的分"的唯一依据,让被审计对象自报是没有意义的。

        默认从后端专属的 ``_vision_meta`` 里取 —— 那个键只有我们自己写。
        """
        meta = payload.get("_vision_meta")
        if isinstance(meta, dict) and meta.get("model"):
            return str(meta["model"])[:120]
        return None

    async def test_connection(self) -> dict[str, Any]:
        configured = self.is_configured()
        return {
            "evaluator": self.evaluator_name,
            "configured": configured,
            "reachable": None,
            "message": "已配置" if configured else "未配置",
        }

    async def evaluate_structured(
        self,
        product_assets: list[str],
        candidate_image: str,
        product_metadata: dict,
        rule_set: dict,
        *,
        depth: EvaluationDepth = EvaluationDepth.FULL,
    ) -> EvaluationResult:
        """调用 evaluate 并做严格解析。

        解析失败一律抛 ``EvaluationParseError``,绝不吞掉后返回一个"看起来还行"的默认分 ——
        评分器坏掉时静默给 80 分,会让烂图直接自动通过上网站,这是最坏的失败模式。
        """
        import time

        from app.evaluators.scoring import (
            EvaluationParseError,
            parse_evaluator_payload,
        )

        started = time.monotonic()
        try:
            payload = await self.evaluate(
                product_assets, candidate_image, product_metadata, rule_set
            )
        except EvaluationParseError as exc:
            # 模型输出不是合法 JSON 时,解析就发生在 evaluate() **内部**,
            # 异常从这里穿出去而不是从下面的 parse_evaluator_payload。
            # 评分器已经把元数据挂在异常上了(它是唯一还拿得到响应体的人),
            # 这里只补耗时,不覆盖 —— 覆盖会把刚留下的痕迹又擦掉
            if getattr(exc, "duration_ms", None) is None:
                exc.duration_ms = int((time.monotonic() - started) * 1000)
            raise
        # 请求这一步已经成功了 —— 模型名、响应 ID、token 用量都在 payload 里。
        # 先把它们取出来,解析失败时一并带进异常:那条失败记录的价值
        # 全在这些字段上,丢了就只剩一句"解析不了"
        meta = payload.get("_vision_meta") if isinstance(payload, dict) else None
        meta = meta if isinstance(meta, dict) else None

        try:
            result = parse_evaluator_payload(
                payload,
                evaluator=self.evaluator_name,
                depth=depth,
                weights=rule_set.get("weights") if isinstance(rule_set, dict) else None,
                model_name=(
                    self.resolved_model_name(payload) if isinstance(payload, dict) else None
                ),
            )
        except EvaluationParseError as exc:
            if getattr(exc, "vision_meta", None) is None:
                exc.vision_meta = meta
            if getattr(exc, "duration_ms", None) is None:
                exc.duration_ms = int((time.monotonic() - started) * 1000)
            raise
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result
