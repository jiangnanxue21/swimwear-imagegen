"""评分落库与轮次决策(需求第十、十二、十三章)。

职责边界:
- 纯规则(打分权重、分档、修复策略、轮次决策)在 `app/evaluators/` 里,是纯函数;
- 这里只负责"读素材 -> 调评分器 -> 写库 -> 拿决策",不重复实现任何规则。

这样"这张图为什么是 C 档"永远能在一个不带数据库的单元测试里复现。
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    CandidateStatus,
    EvaluationDepth,
    EvaluationOutcome,
    Grade,
)
from app.core.errors import (
    EvaluationScoringFailed,
    ManualReviewRequired,
    ReferenceImagesMissing,
)
from app.core.logging import get_logger
from app.evaluators.base import EvaluationResult, ImageQualityEvaluator
from app.evaluators.decision import (
    CandidateVerdict,
    RoundDecision,
    RoundOutcome,
    decide_round,
)
from app.evaluators.ranking import RankedCandidate, rank_candidates
from app.evaluators.registry import get_active_evaluator
from app.evaluators.rules import grade_candidate
from app.evaluators.scoring import EvaluationParseError
from app.evaluators.vision import (
    AUDIENCE_KEY,
    ENABLED_CODES_KEY,
    PROMPT_KEY,
    PROMPT_VERSION_KEY,
)
from app.evaluators.vision import DEPTH_KEY as VISION_DEPTH_KEY
from app.models.evaluation import (
    CandidateEvaluation,
    EvaluationAttempt,
    EvaluationProblem,
)
from app.models.generation import GenerationCandidate, GenerationTask
from app.models.product import Product
from app.models.product_asset import ProductAsset
from app.prompts.versioning import version_text
from app.providers.errors import ProviderError
from app.services import generation_service as gs
from app.services.rule_set_service import ResolvedRuleSet, load_active_rule_set

logger = get_logger(__name__)


def product_metadata(product: Product, session: Session | None = None) -> dict[str, Any]:
    """交给评分器的商品属性。只给判断需要的字段,不倾倒整行数据。

    **M4 起改走属性层(`attributes/service.py`)。** `products` 上那 8 个外观列
    现在是只读投影,事实源在 `product_attribute_values` —— 两者会在
    识别写入与人工确认之间短暂不一致,直接读投影列拿到的可能是旧值。

    只采信 `CONFIRMED` 的值;没有确认值时回落到投影列(存量商品还没跑过识别)。
    拿一个还没人看过的模型猜测去判断"生成的还是不是这件商品",
    等于用一个不确定的输入去验证另一个不确定的输出。

    `session` 可选是为了兼容还没改过来的调用点(以及不带库的单测)——
    不传时行为和 M4 之前完全一致。
    """
    if session is not None:
        from app.attributes import service as attr_service

        return attr_service.metadata_for_evaluation(session, product)

    return {
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "garment_type": product.garment_type,
        "primary_color": product.primary_color,
        "secondary_colors": list(product.secondary_colors or []),
        "pattern_type": product.pattern_type,
        "material": product.material,
        "strap_type": product.strap_type,
        "neckline_type": product.neckline_type,
        "back_style": product.back_style,
        "complexity": product.complexity,
    }


def _reference_asset_paths(session: Session, task: GenerationTask) -> list[str]:
    """商品原图的存储路径。评分要拿它和候选图做比对。

    传给评分器的是 **storage_path**,不是绝对路径:
    存储是内容寻址的(路径里带文件哈希),因此"同样的图 = 同样的路径",
    评分器据此做的确定性指纹天然成立,而且这一层不必知道文件落在本地还是对象存储。
    """
    ids = [UUID(a) for a in (task.input_asset_ids or [])]
    assets = (
        list(session.scalars(select(ProductAsset).where(ProductAsset.id.in_(ids))))
        if ids
        else []
    )
    # 只用商品图做参照,模特参考图和背景图不是"商品长什么样"的依据
    garment_types = {"GARMENT_FRONT", "GARMENT_BACK", "GARMENT_DETAIL", "GARMENT_CUTOUT"}
    paths = [a.storage_path for a in assets if a.asset_type in garment_types and a.storage_path]
    if paths:
        return paths

    # 没有合格的商品参考图就**不评**。
    #
    # 以前这里会退回该任务的全部素材,于是模特参考图、背景图都可能成为
    # 「商品一致性」的比对基准。那不是降级,是评错对象:评分器会认认真真地
    # 拿模特照片当商品去比,给出一个看着正常、实际毫无意义的 garment_identity,
    # 而这个维度权重最高,足以把一张根本不对的图送上 A 档。
    raise ReferenceImagesMissing(
        "这个任务没有可用的商品参考图(正面图/背面图/细节图/抠图),"
        "无法判断生成结果是不是同一件商品;已转人工审核",
        detail={
            "task_id": str(task.id),
            "asset_count": len(assets),
            "asset_types": sorted({a.asset_type for a in assets}),
        },
    )


def _read_bytes(storage, storage_path: str) -> bytes | None:
    """取字节。走 ObjectStorage 抽象,本地与对象存储通吃。"""
    if not storage_path:
        return None
    try:
        return storage.read(storage_path)
    except Exception:  # noqa: BLE001
        return None



def _rule_pack_context(product) -> dict:
    """本商品规则包里与评分有关的两项(§12.5 / §14.4)。

    受众和启用码一起下发,评分器就不必自己连库 —— 它跑在 worker 里,
    而且要能被无库测试。规则包读不出来(受众未确认、spec 缺失)时返回空 dict:
    评分器那边取不到就退回受众前时代的行为(全集码 + 女装提示词),
    这与提示词读失败的降级思路一致 —— 一个可降级的输入不该让整批评分停摆。
    """
    from app.channels import generic

    audience = getattr(product, "audience", None) if product else None
    ctx: dict = {}
    if audience:
        ctx[AUDIENCE_KEY] = str(audience)
    try:
        spec = generic.field_spec(category_id=generic.category_id_for(product))
    except Exception:  # noqa: BLE001
        return ctx
    if spec.enabled_hard_fail_codes:
        ctx[ENABLED_CODES_KEY] = list(spec.enabled_hard_fail_codes)
    return ctx

def _prescreen(
    candidates: list[GenerationCandidate],
    candidate_paths: dict[str, str],
    reference_paths: list[str],
    rule_set: ResolvedRuleSet,
    storage,
) -> dict[str, RankedCandidate]:
    """轮内预排序(需求第十三章)。任何一步出问题都退回"全部完整评分"。

    预排序是**省钱优化**,不是正确性依赖。它坏掉时应该让系统变贵,而不是变错。
    """
    enabled = rule_set.thresholds.prescreen_enabled
    if not enabled:
        return {}

    payload: list[tuple[str, bytes]] = []
    for candidate in candidates:
        data = _read_bytes(storage, candidate_paths.get(str(candidate.id), ""))
        if data is None:
            return {}
        payload.append((str(candidate.id), data))

    references = [b for b in (_read_bytes(storage, p) for p in reference_paths) if b]

    try:
        ranked = rank_candidates(
            payload,
            references,
            enabled=True,
            full_evaluation_count=rule_set.thresholds.full_evaluation_count,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "prescreen failed, falling back to full evaluation for all candidates",
            extra={"extra_fields": {"event": "eval.prescreen_failed"}},
        )
        return {}
    return {item.key: item for item in ranked}


def _is_transient(exc: ProviderError) -> bool:
    """这个错误等一会儿再试有没有意义。

    直接复用 Provider 层已经定义好的重试策略,不在这里再维护一张错误码表 ——
    两张表迟早会不一致,而不一致的那一天没有任何征兆。
    """
    policy = getattr(exc, "policy", None)
    return bool(policy and policy.retriable and not policy.requires_human)


def _billing(evaluator: ImageQualityEvaluator) -> str | None:
    """这个评分器的调用要不要记账;要的话记在谁头上。

    两件事都问评分器自己(`billable` / `usage_provider`),不在这里维护一张
    「哪些评分器要花钱」的表 —— 那张表会和评分器注册表分叉,而分叉的表现是
    新接的付费评分器**静默不计费**,看板上一切正常。
    """
    return evaluator.usage_provider if getattr(evaluator, "billable", False) else None


def _record_attempt(
    session: Session,
    task: GenerationTask,
    candidate: GenerationCandidate | None,
    *,
    outcome: EvaluationOutcome,
    evaluator: str,
    depth: EvaluationDepth,
    prompt_version: object = None,
    model_name: str | None = None,
    duration_ms: int | None = None,
    evaluation_id=None,
    error_code: str | None = None,
    error_message: str | None = None,
    raw: dict[str, Any] | None = None,
    billable_provider: str | None = None,
    diagnostic: bool = False,
    round_number: int | None = None,
) -> EvaluationAttempt:
    """给每一次评分请求留一条痕,**成功与失败一视同仁**。

    失败的那些尤其重要:响应 ID、模型名、token 数、当时的提示词版本,
    是事后找厂商查一次异常调用的全部依据,而它们只在这一刻拿得到。

    错误说明只放我们自己生成的文案并截断 —— 第三方异常原文可能带着
    URL 里的凭据,这张表是要长期留存的。

    ## `billable_provider` 与花费台账

    传了它就同时写一条 `provider_usage_records`。在 a25 之前评分调用**完全不进
    流水表**:`record_usage` 的三个调用点全在图片生成路径上,而一轮 4 张候选
    × 最多 3 轮意味着视觉模型的调用次数是生成调用的**数倍**。
    于是 `/spend` 页即便接好了线,漏掉的也是开销里更大的那一半。

    只在**每次请求**的那三个调用点传:整轮汇总那两条不对应任何一次 API 调用,
    给它们记账会让调用数凭空翻倍。
    """
    meta = raw.get("_vision_meta") if isinstance(raw, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}

    row = EvaluationAttempt(
        task_id=task.id,
        candidate_id=candidate.id if candidate is not None else None,
        evaluation_id=evaluation_id,
        round_number=round_number if round_number is not None else task.current_round,
        outcome=outcome.value,
        evaluator=evaluator or "",
        model_name=(model_name or meta.get("model") or None),
        depth=depth.value,
        response_id=(str(meta["response_id"])[:128] if meta.get("response_id") else None),
        http_status=meta.get("http_status") if isinstance(meta.get("http_status"), int) else None,
        finish_reason=(str(meta["finish_reason"])[:48] if meta.get("finish_reason") else None),
        prompt_version=version_text(prompt_version),
        prompt_tokens=_int_or_none(usage.get("prompt_tokens") or usage.get("input_tokens")),
        completion_tokens=_int_or_none(
            usage.get("completion_tokens") or usage.get("output_tokens")
        ),
        total_tokens=_int_or_none(usage.get("total_tokens")),
        duration_ms=duration_ms if isinstance(duration_ms, int) else meta.get("duration_ms"),
        error_code=(error_code[:48] if error_code else None),
        error_message=(error_message[:2000] if error_message else None),
        meta={
            "api_style": meta.get("api_style"),
            "response_format": meta.get("response_format"),
            "images": meta.get("images"),
            "diagnostic": diagnostic,
        },
    )
    session.add(row)
    session.flush()

    if billable_provider:
        # 一次评分请求 = 一个计费单位。**失败的也算** —— 厂商在收到请求那一刻
        # 就开始计费,超时和解析失败都不退钱,而那正是最容易被漏掉的一类开销。
        gs.record_usage(
            session,
            provider=billable_provider,
            task_id=task.id,
            attempt_id=None,
            operation="vision_score",
            succeeded=outcome is EvaluationOutcome.SUCCEEDED,
            candidate_count=1 if candidate is not None else 0,
            duration_ms=row.duration_ms,
            error_code=(error_code[:48] if error_code else None),
            billable_units=1,
        )

    return row


#: 诊断路径的墙钟上限(秒)。
#:
#: 自动评分跑在 worker 里,慢一点只是慢;诊断不是 —— 它是一个**同步 HTTP
#: 请求处理函数里的真实模型调用**(`api/reviews.py` 里 `asyncio.run` 那一段),
#: 人在页面上等着。按默认配置 `VISION_MODEL_TIMEOUT_SECONDS=90` +
#: `VISION_MODEL_MAX_RETRIES=2` 算,最坏是 3 次往返各 90 秒再加退避,约 272 秒;
#: 而这中间任何一个反代或客户端先超时,连接就断了 —— 用户看到的是 502,
#: 而那几次调用**已经计费**,台账里也不会有记录(响应还没回来,异常分支还没跑到)。
#:
#: 240 秒的来历:它必须小于链路上最短的那个耐心值。常见反代默认 60/300,
#: 浏览器 fetch 无默认上限但运营的耐心有,所以取一个「比 300 明显小、
#: 又能容下一次 high detail 的完整评分」的值。改这个数之前先确认反代配置。
DIAGNOSTIC_WALL_CLOCK_BUDGET_SECONDS = 240.0


def _bounded_for_diagnostics(evaluator: ImageQualityEvaluator) -> ImageQualityEvaluator:
    """返回一个墙钟封顶的评分器副本,专供诊断路径用。

    做两件事:超时封顶到 `DIAGNOSTIC_WALL_CLOCK_BUDGET_SECONDS`,重试归零。
    重试归零是有意的 —— 诊断的目的是**看清这一次调用发生了什么**,自动重试
    只会把第一次的真实错误换成第三次的错误,还多计两次费。真要重试,人点第二次。

    **为什么是副本而不是就地改**:调用方(`api/reviews.py`)自己持有这个
    evaluator 实例并用它判过 `billable`,而传输层是每次调用现读 `self.config`
    的。就地改会让这次诊断的封顶永久留在那个实例上,表现是「某次诊断之后,
    这条路径上的超时莫名其妙变短了」—— 一个没人会想到去查的耦合。

    没有 `config` 的评分器(Mock、测试替身)原样返回:它们不发网络请求,
    没有可封顶的东西,而强行构造副本只会在替身上炸出 AttributeError。
    """
    config = getattr(evaluator, "config", None)
    if config is None:
        return evaluator

    try:
        current = float(getattr(config, "timeout_seconds", DIAGNOSTIC_WALL_CLOCK_BUDGET_SECONDS))
    except (TypeError, ValueError):
        current = DIAGNOSTIC_WALL_CLOCK_BUDGET_SECONDS
    try:
        bounded = dataclasses.replace(
            config,
            timeout_seconds=min(current, DIAGNOSTIC_WALL_CLOCK_BUDGET_SECONDS),
            max_retries=0,
        )
    except (TypeError, ValueError):
        # config 不是 dataclass,或没有这两个字段 —— 同样是「没有可封顶的东西」
        return evaluator

    clone = copy.copy(evaluator)
    clone.config = bounded
    # 传输层自己存了一份 config 引用,且是**调用时现读**的(见
    # `llm/transport.MultimodalClient.send`)。只改 clone.config 而不改它,
    # 封顶就完全不生效 —— 而且是静默不生效。
    transport = getattr(clone, "_transport", None)
    if transport is not None:
        transport = copy.copy(transport)
        transport.config = bounded
        clone._transport = transport
    return clone


def diagnose_candidate(
    session: Session,
    task: GenerationTask,
    candidate: GenerationCandidate,
    *,
    evaluator: ImageQualityEvaluator | None = None,
    actor: str = "system",
) -> tuple[EvaluationResult | None, EvaluationAttempt]:
    """人工测试一张候选图，不覆盖正式评分或候选图状态。

    真实评分器仍可能计费，所以成功与失败都进入原有调用与费用台账；
    `meta.diagnostic` 用来和自动评分区分。

    三条出路(解析失败 / Provider 出错 / 成功)**各自留档**,不在函数末尾
    合并写一次 —— 前两条是 early return,合并点根本到不了,而"失败的测试
    没进历史"正是这张表最该记住的那一类。
    """

    if candidate.task_id != task.id:
        raise ValueError("候选图不属于指定生成任务")

    rule_set = load_active_rule_set(session)
    # 封顶在这里、而不是在 api 层:任何调用 diagnose_candidate 的入口都跑在
    # 同步请求里,漏接一个入口就等于这条防线不存在
    evaluator = _bounded_for_diagnostics(evaluator or get_active_evaluator())
    product = session.get(Product, task.product_id)
    metadata = product_metadata(product, session) if product else {}
    prompt_context = {
        **_prompt_context(
            session, getattr(product, "audience", None) if product else None
        ),
        **_rule_pack_context(product),
    }
    depth = EvaluationDepth.FULL
    payload = rule_set.payload(
        mock_evaluator={
            **rule_set.evaluator_options.get("mock_evaluator", {}),
            **(task.provider_params or {}).get("mock_evaluator", {}),
            "round_number": candidate.round_number,
            "quick_only": False,
        },
        **{VISION_DEPTH_KEY: depth.value},
        **prompt_context,
    )

    try:
        result = asyncio.run(
            evaluator.evaluate_structured(
                _reference_asset_paths(session, task),
                candidate.storage_path or "",
                metadata,
                payload,
                depth=depth,
            )
        )
    except EvaluationParseError as exc:
        attempt = _record_attempt(
            session,
            task,
            candidate,
            outcome=EvaluationOutcome.PARSE_FAILED,
            evaluator=evaluator.evaluator_name,
            depth=depth,
            prompt_version=prompt_context.get(PROMPT_VERSION_KEY),
            error_code=str(exc.code),
            error_message=exc.message,
            duration_ms=exc.duration_ms,
            raw={"_vision_meta": exc.vision_meta} if exc.vision_meta else None,
            billable_provider=_billing(evaluator),
            diagnostic=True,
            round_number=candidate.round_number,
        )
        _archive_diagnostic(session, candidate, attempt, evaluator, product, actor)
        return None, attempt
    except ProviderError as exc:
        attempt = _record_attempt(
            session,
            task,
            candidate,
            outcome=EvaluationOutcome.PROVIDER_ERROR,
            evaluator=evaluator.evaluator_name,
            depth=depth,
            prompt_version=prompt_context.get(PROMPT_VERSION_KEY),
            error_code=str(exc.code),
            error_message=exc.message,
            duration_ms=getattr(exc, "duration_ms", None),
            raw=(
                {"_vision_meta": exc.vision_meta}
                if getattr(exc, "vision_meta", None)
                else None
            ),
            billable_provider=_billing(evaluator),
            diagnostic=True,
            round_number=candidate.round_number,
        )
        _archive_diagnostic(session, candidate, attempt, evaluator, product, actor)
        return None, attempt

    decision = grade_candidate(result, rule_set.thresholds)
    result.grade = decision.grade
    result.recommended_action = decision.action
    result.grade_reasons = list(decision.reasons)
    attempt = _record_attempt(
        session,
        task,
        candidate,
        outcome=EvaluationOutcome.SUCCEEDED,
        evaluator=result.evaluator,
        depth=result.depth,
        prompt_version=prompt_context.get(PROMPT_VERSION_KEY),
        model_name=result.model_name,
        duration_ms=result.duration_ms,
        raw=result.raw,
        billable_provider=_billing(evaluator),
        diagnostic=True,
        round_number=candidate.round_number,
    )
    _archive_diagnostic(session, candidate, attempt, evaluator, product, actor)
    return result, attempt


def _archive_diagnostic(
    session: Session,
    candidate: GenerationCandidate,
    attempt: EvaluationAttempt,
    evaluator: ImageQualityEvaluator,
    product: Product | None,
    actor: str,
) -> None:
    """把这次评分测试写进 `ai_test_runs`(BE-310)。

    数值一律**从 `attempt` 上读回**,不从各分支的局部变量再取一遍:两份
    台账说的是同一次调用,分头取值迟早会在某个分支上分叉,而分叉了没有
    任何东西会红。`_record_attempt` 已经 flush 过,所以这里读得到 id。

    `prompt_key` 按受众现推 —— 键的推导只有 `vision_schema.prompt_key_for`
    一处(见 `_prompt_context` 里那段注释),这里调它而不是抄一份判断。
    """
    from app.workflows.ai_test_record import record_for_evaluation
    from app.services import ai_test_archive

    try:
        from app.core.audience import coerce as _coerce_audience
        from app.evaluators.vision_schema import prompt_key_for

        prompt_key = prompt_key_for(
            _coerce_audience(getattr(product, "audience", None) if product else None)
        )
    except Exception:  # noqa: BLE001 —— 推不出键不该让留档整条消失
        prompt_key = None

    ai_test_archive.archive(
        session,
        record_for_evaluation(
            candidate_id=candidate.id,
            attempt_id=attempt.id,
            success=attempt.outcome == EvaluationOutcome.SUCCEEDED.value,
            billable=bool(getattr(evaluator, "billable", False)),
            actor=actor,
            prompt_key=prompt_key,
            # 序号进序号那一列。评分链路没有内容派生版本,`prompt_version` 留空 ——
            # 见 `ai_test_record._int_version` 上面那段(a63 订正的口径)
            template_version=attempt.prompt_version,
            evaluator=attempt.evaluator,
            model_name=attempt.model_name,
            depth=attempt.depth,
            error_code=attempt.error_code,
            error_message=attempt.error_message,
            duration_ms=attempt.duration_ms,
            total_tokens=attempt.total_tokens,
        ),
    )


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _persist(
    session: Session,
    task: GenerationTask,
    candidate: GenerationCandidate,
    result: EvaluationResult,
    rule_set: ResolvedRuleSet,
    ranked: RankedCandidate | None,
) -> CandidateEvaluation:
    row = CandidateEvaluation(
        task_id=task.id,
        candidate_id=candidate.id,
        rule_set_id=rule_set.id,
        round_number=candidate.round_number,
        evaluator=result.evaluator,
        model_name=result.model_name,
        depth=result.depth.value,
        hard_fail=result.hard_fail,
        hard_fail_codes=list(result.hard_fail_codes),
        scores=dict(result.scores),
        overall_score=result.overall_score,
        model_reported_overall=result.model_reported_overall,
        grade=(result.grade or Grade.D).value,
        recommended_action=(
            result.recommended_action.value if result.recommended_action else "REJECT"
        ),
        grade_reasons=list(result.grade_reasons),
        uncertain_items=list(result.uncertain_items),
        rank_index=ranked.rank if ranked else None,
        similarity_score=ranked.similarity if ranked else None,
        realism_score=ranked.realism if ranked else None,
        duration_ms=result.duration_ms,
        raw_response=result.raw or {},
    )
    session.add(row)
    session.flush()

    for problem in result.problems:
        session.add(
            EvaluationProblem(
                evaluation_id=row.id,
                code=problem.code,
                severity=problem.severity.value,
                dimension=problem.dimension,
                message=problem.message,
                hard_fail=problem.hard_fail,
            )
        )
    session.flush()
    return row


def _prompt_context(session: Session, audience: object = None) -> dict:
    """当前生效的系统提示词与版本号。**按受众取键**(PRD v2 §12.5)。

    这里的 try/except 曾经吞掉**所有**异常并退回内置默认提示词。听起来像是
    合理的降级,实际上不是:数据库超时、连接断开、出现两条 active 记录,
    全都会让这一批商品**在运营不知情的情况下换一套评分口径**,然后自动通过。
    症状是「上周通过率突然变了」,而库里、界面上都没有任何东西能解释它。

    现在只有一种情况允许用内置默认值:**库是好的,确实没有 active 版本**。
    那是一个正常状态(新部署从没在页面上保存过提示词),`get_active_content`
    会明确地返回 version=None。

    除此之外的任何异常都是「我们不知道现在该用哪套标准」。生产环境下宁可
    转人工审核 —— 候选图已经生成好了,人看图比机器用错标准自动放行强。
    开发机上仍然退回默认值并留一条 warning,不然本机没库就没法跑通流程。
    """
    from app.core.config import settings

    try:
        # 受众决定用哪一份提示词。选错键 = 用另一个受众的标准评这批图,
        # 而两侧测试都不会红(§0.2 第二条)—— 所以键的推导只有一处:
        # `vision_schema.prompt_key_for`
        from app.core.audience import coerce as _coerce_audience
        from app.evaluators.vision_schema import prompt_key_for
        from app.services import prompt_service

        key = prompt_key_for(_coerce_audience(audience))
        content, version = prompt_service.get_active_content(session, key)
        return {PROMPT_KEY: content, PROMPT_VERSION_KEY: version}

    except Exception as exc:  # noqa: BLE001
        if settings.is_production:
            logger.error(
                "cannot load the active prompt; refusing to score with a different rubric",
                extra={
                    "extra_fields": {
                        "event": "eval.prompt_missing",
                        "error": type(exc).__name__,
                    }
                },
            )
            raise ManualReviewRequired(
                "读取生效中的评分提示词失败,无法确认本次该用哪套评分标准;"
                "已转人工审核。请检查数据库连接与提示词版本(是否出现多条启用中的记录)",
                detail={"error": type(exc).__name__},
            ) from exc
        logger.warning(
            "cannot load the active prompt, falling back to the built-in default "
            "(non-production only)",
            extra={"extra_fields": {"event": "eval.prompt_fallback", "error": type(exc).__name__}},
        )
        return {}


def evaluate_round(
    session: Session,
    task: GenerationTask,
    candidates: list[GenerationCandidate],
    *,
    storage,
    evaluator: ImageQualityEvaluator | None = None,
    provider_exhausted: bool = False,
    heartbeat: Callable[[], None] | None = None,
) -> RoundDecision:
    """评完一轮候选图并给出任务去向。

    ## `heartbeat`:每张图之前跑一次(A45-batch12-5 / NEW-03)

    编排层传进来的回调,做三件它做不了、这里又必须发生的事:提交上一张的
    结果、推进任务的 `updated_at`、给阶段租约续期。

    为什么非要在**循环里面**:一张图最多 3 × 90 = 270 秒(单次超时 × 内部
    重试),8 张就是 2160 秒。这一整段在库里是"没有任何动静",而回收器的
    卡死阈值是 1800 秒 —— 一个正在正常花钱评分的任务会被判成卡死,然后
    运营点重试,再评一遍。放在循环外面(哪怕是循环前后各一次)修不了这个,
    因为间隔本来就出在两张图之间。

    顺带修掉的是账目:上一版整轮共用一个事务,第 5 张崩掉时前 4 张**已经
    调用过、已经计费**的视觉请求会随 rollback 一起消失。逐张提交之后,
    花掉的钱一定留在账上 —— 而这正是 `_record_attempt` 那句"失败的也算"
    想保证的事情。

    回调抛异常时**不拦**:它唯一会抛的是"租约已经被接管",而那时候唯一正确的
    动作就是立刻停手 —— 继续评下去只会写出一份注定要被覆盖、却已经花了钱的结果。
    """
    rule_set = load_active_rule_set(session)
    evaluator = evaluator or get_active_evaluator()
    product = session.get(Product, task.product_id)
    # 传 session:走属性表的事实源,而不是 products 上的只读投影(§5.4)
    metadata = product_metadata(product, session) if product else {}

    candidate_paths = {str(c.id): c.storage_path or "" for c in candidates}
    reference_paths = _reference_asset_paths(session, task)
    ranking = _prescreen(candidates, candidate_paths, reference_paths, rule_set, storage)

    # 循环外查一次:一轮里所有候选图必须用同一版提示词,
    # 否则同一轮的分数之间不可比,而排序正是拿它们比出来的。
    # 受众取自商品(§23.5:六处用到受众的地方都从 product.audience 读)
    prompt_context = {
        **_prompt_context(
            session, getattr(product, "audience", None) if product else None
        ),
        # 受众 + 本规则包启用的硬错误码。与提示词同一条通道下发,
        # 一轮里所有候选图共用同一份 —— 否则同轮分数之间不可比
        **_rule_pack_context(product),
    }

    verdicts: list[CandidateVerdict] = []
    #: 没评成的候选图 (candidate_id, error_code, message)。
    #: 和"评成了但分低"严格分开 —— 两者该触发的动作完全不同。
    scorer_failures: list[tuple[str, str, str]] = []
    #: 失败里有没有"等一会儿可能就好"的类型(限流、超时)
    transient_failure = False

    for candidate in candidates:
        # 心跳在**这一张开始之前**打:它提交的是上一张的结果,续的是这一张
        # 要用掉的那段租约。放在结尾的话,最后一张评完到函数返回之间就没有
        # 保护了,而那一段正要写"这一轮的结论"。
        if heartbeat is not None:
            heartbeat()
        key = str(candidate.id)
        ranked = ranking.get(key)
        depth = ranked.depth if ranked else EvaluationDepth.FULL
        payload = rule_set.payload(
            mock_evaluator={
                **rule_set.evaluator_options.get("mock_evaluator", {}),
                **(task.provider_params or {}).get("mock_evaluator", {}),
                "round_number": candidate.round_number,
                "quick_only": depth is EvaluationDepth.QUICK,
            },
            # 评分深度对真实评分器是必要信息(维度集合、图片 detail、提示词都靠它),
            # 但 evaluate() 的签名是所有评分器共用的公开接口,不该为一个参数改动它。
            # 走 rule_set 传是最小改动:不认识这个键的评分器原样忽略。
            **{VISION_DEPTH_KEY: depth.value},
            # 运营在设置页编辑过的提示词。在这里取好一起传下去,
            # 评分器就不必自己连库 —— 它跑在 worker 里,而且要能被无库测试
            **prompt_context,
        )

        try:
            result = asyncio.run(
                evaluator.evaluate_structured(
                    reference_paths,
                    candidate_paths.get(key, ""),
                    metadata,
                    payload,
                    depth=depth,
                )
            )
        except EvaluationParseError as exc:
            # 评分器返回了没法解析的东西。**这不是"图片是 D 档"。**
            #
            # 以前这里会伪装成一条 D 档结论塞进 verdicts,于是整轮都解析失败时,
            # 决策器看到的是"四张图全是 D",判断当然是重生一轮 —— 再花一次
            # 生成费用,去修一个根本不在图片上的问题,直到轮次耗尽。
            #
            # 现在把它算作"这张没评成":候选图状态不动(它没有被判过),
            # 只记一条失败留痕,并让它退出本轮排序。整轮都没评成时下面会转人工。
            logger.warning(
                "evaluation payload unparseable",
                extra={
                    "extra_fields": {
                        "event": "eval.payload_unparseable",
                        "candidate_id": key,
                        "reason": exc.message,
                    }
                },
            )
            _record_attempt(
                session, task, candidate,
                outcome=EvaluationOutcome.PARSE_FAILED,
                evaluator=evaluator.evaluator_name,
                depth=depth,
                prompt_version=prompt_context.get(PROMPT_VERSION_KEY),
                error_code=str(exc.code),
                error_message=exc.message,
                # 请求成功、只是解析失败 —— 模型名、响应 ID、token 用量、
                # finish reason 和耗时全都拿得到,一并落库。这条记录
                # 存在的意义就是让人事后能拿着响应 ID 去找厂商对账
                duration_ms=exc.duration_ms,
                raw={"_vision_meta": exc.vision_meta} if exc.vision_meta else None,
                billable_provider=_billing(evaluator),
            )
            scorer_failures.append((key, str(exc.code), exc.message))
            continue
        except ProviderError as exc:
            # 限流、超时、鉴权、额度、内容安全拒绝……评分器调不通。
            # 同样不是图片的问题,处理方式和解析失败一致。
            logger.warning(
                "evaluator call failed",
                extra={
                    "extra_fields": {
                        "event": "eval.call_failed",
                        "candidate_id": key,
                        "code": str(exc.code),
                    }
                },
            )
            _record_attempt(
                session, task, candidate,
                outcome=EvaluationOutcome.PROVIDER_ERROR,
                evaluator=evaluator.evaluator_name,
                depth=depth,
                prompt_version=prompt_context.get(PROMPT_VERSION_KEY),
                error_code=str(exc.code),
                error_message=exc.message,
                # 和上面的解析失败分支对齐。截断、内容安全拒绝这类
                # ProviderError 是**已经计费的成功 HTTP 调用** —— 评分器在
                # `_extract_or_fail` 那一层已经把响应 ID、模型名、token 用量、
                # finish reason 挂到异常上了。以前这个分支不取,于是生产评分
                # 路径上最该对账的一类失败,`evaluation_attempts` 里反而是空的
                # (而同一份数据在诊断路径上是落库的,两条路径就此分叉)。
                #
                # 用 getattr 而不是直接取属性:限流、超时这些是传输层抛的,
                # 请求根本没成功,身上没有这两个字段,取不到就该是 None。
                duration_ms=getattr(exc, "duration_ms", None),
                raw=(
                    {"_vision_meta": getattr(exc, "vision_meta", None)}
                    if getattr(exc, "vision_meta", None)
                    else None
                ),
                billable_provider=_billing(evaluator),
            )
            scorer_failures.append((key, str(exc.code), exc.message))
            transient_failure = transient_failure or _is_transient(exc)
            continue

        decision = grade_candidate(result, rule_set.thresholds)
        result.grade = decision.grade
        result.recommended_action = decision.action
        result.grade_reasons = list(decision.reasons)

        row = _persist(session, task, candidate, result, rule_set, ranked)
        _record_attempt(
            session, task, candidate,
            outcome=EvaluationOutcome.SUCCEEDED,
            evaluator=result.evaluator,
            depth=result.depth,
            prompt_version=prompt_context.get(PROMPT_VERSION_KEY),
            model_name=result.model_name,
            duration_ms=result.duration_ms,
            evaluation_id=row.id,
            raw=result.raw,
            billable_provider=_billing(evaluator),
        )

        candidate.overall_score = result.overall_score
        candidate.grade = decision.grade.value
        candidate.status = CandidateStatus.SCORED.value
        session.flush()

        verdicts.append(
            CandidateVerdict(
                key=key,
                grade=decision.grade,
                overall_score=result.overall_score,
                depth=result.depth,
                hard_fail=result.hard_fail,
                hard_fail_codes=tuple(result.hard_fail_codes),
                problem_codes=tuple(p.code for p in result.problems),
            )
        )

    if scorer_failures and not verdicts:
        # 整轮一张都没评成。绝不能走 decide_round —— 它看到空的 verdicts 会
        # 判"本轮没有可评分的候选图"并安排重生,那正是我们要避免的:
        # 图是好是坏还不知道,重生只会再拿到一批同样评不了的图。
        codes = sorted({code for _, code, _ in scorer_failures})
        _record_attempt(
            session, task, None,
            outcome=(
                EvaluationOutcome.ROUND_RETRY_SCHEDULED
                if transient_failure
                else EvaluationOutcome.PROVIDER_ERROR
            ),
            evaluator=evaluator.evaluator_name,
            depth=EvaluationDepth.FULL,
            prompt_version=prompt_context.get(PROMPT_VERSION_KEY),
            error_code=codes[0] if codes else None,
            error_message=f"整轮 {len(scorer_failures)} 张候选图全部评分失败",
        )
        logger.error(
            "the whole round failed to score",
            extra={
                "extra_fields": {"event": "eval.round_failed",
                    "task_id": str(task.id),
                    "round": task.current_round,
                    "candidates": len(scorer_failures),
                    "codes": codes,
                    "transient": transient_failure,
                }
            },
        )
        raise EvaluationScoringFailed(
            f"本轮 {len(scorer_failures)} 张候选图全部评分失败({'、'.join(codes) or '未知原因'})。"
            "候选图已经生成并入库,不会重新生成",
            detail={
                "task_id": str(task.id),
                "round": task.current_round,
                "codes": codes,
                "failed": len(scorer_failures),
            },
            retry_scoring=transient_failure,
        )

    if scorer_failures:
        # 部分失败。评成的那些仍然参与排序 —— 它们都是真判过的结论,
        # 不掺任何伪造的 D 档。但**能不能据此做终局判断是另一回事**:
        # 见下面 decide_round 之后那段守卫。
        logger.warning(
            "some candidates could not be scored this round",
            extra={
                "extra_fields": {"event": "eval.partial_failure",
                    "task_id": str(task.id),
                    "failed": len(scorer_failures),
                    "scored": len(verdicts),
                }
            },
        )

    decision = decide_round(
        verdicts,
        round_number=task.current_round,
        max_rounds=task.max_rounds,
        thresholds=rule_set.thresholds,
        task_key=str(task.id),
        provider_exhausted=provider_exhausted,
    )

    if scorer_failures and decision.outcome is not RoundOutcome.AUTO_APPROVED:
        # 部分候选没评成,而评成的那些里**没有**一张够 A 档。
        #
        # 这时候 decide_round 的结论是基于一份残缺的名单做出来的:四张图里
        # 一张评出 B、三张超时没评,它看到的就是"本轮最好只有 B",于是安排
        # 重生 —— 再花一次生成的钱,去修一个可能根本不存在的问题,因为那三张
        # 没评的里面完全可能有 A 档。轮次耗尽时更糟:直接判定淘汰,而真正
        # 该被看到的那几张图从头到尾没人看过。
        #
        # 已经拿到 A 档是唯一的例外:那是一个**肯定**的结论,再多评几张也
        # 不会让它变差,可以照常通过。其余情况一律不做终局判断 ——
        # 要么重排一次评分(短暂故障),要么交给人看图,两条路都不花生成的钱。
        codes = sorted({code for _, code, _ in scorer_failures})
        _record_attempt(
            session, task, None,
            outcome=(
                EvaluationOutcome.ROUND_RETRY_SCHEDULED
                if transient_failure
                else EvaluationOutcome.PROVIDER_ERROR
            ),
            evaluator=evaluator.evaluator_name,
            depth=EvaluationDepth.FULL,
            prompt_version=prompt_context.get(PROMPT_VERSION_KEY),
            error_code=codes[0] if codes else None,
            error_message=(
                f"{len(scorer_failures)} 张候选图未评分,已评的 {len(verdicts)} 张里没有 A 档,"
                "不据此做重生或淘汰判定"
            ),
        )
        logger.warning(
            "refusing to decide the round on an incomplete scoreboard",
            extra={
                "extra_fields": {"event": "eval.incomplete_scoreboard",
                    "task_id": str(task.id),
                    "round": task.current_round,
                    "failed": len(scorer_failures),
                    "scored": len(verdicts),
                    "codes": codes,
                    "transient": transient_failure,
                    "would_have_been": decision.outcome.value,
                }
            },
        )
        raise EvaluationScoringFailed(
            f"本轮有 {len(scorer_failures)} 张候选图没能评分"
            f"({'、'.join(codes) or '未知原因'}),已评出的 {len(verdicts)} 张里没有达到 A 档的。"
            "没评过的那几张里可能就有合格的,因此不会据此重新生成或淘汰;"
            "候选图已经入库,请重排评分或人工查看",
            detail={
                "task_id": str(task.id),
                "round": task.current_round,
                "codes": codes,
                "failed": len(scorer_failures),
                "scored": len(verdicts),
            },
            retry_scoring=transient_failure,
        )

    # 落实候选图的最终状态:选中的一张 SELECTED,其余 REJECTED
    by_id = {str(c.id): c for c in candidates}
    for key in decision.rejected_keys:
        row = by_id.get(key)
        if row is not None and row.status != CandidateStatus.DOWNLOAD_FAILED.value:
            row.status = CandidateStatus.REJECTED.value
    if decision.selected_key and decision.selected_key in by_id:
        by_id[decision.selected_key].status = CandidateStatus.SELECTED.value
    session.flush()

    logger.info(
        "round evaluated",
        extra={
            "extra_fields": {"event": "gen.round_evaluated",
                "task_id": str(task.id),
                "round": task.current_round,
                "outcome": decision.outcome.value,
                "candidates": len(verdicts),
            }
        },
    )
    return decision


def list_evaluations(session: Session, task_id: UUID) -> list[CandidateEvaluation]:
    return list(
        session.scalars(
            select(CandidateEvaluation)
            .where(CandidateEvaluation.task_id == task_id)
            .order_by(CandidateEvaluation.round_number, CandidateEvaluation.created_at)
        )
    )


def list_evaluation_attempts(session: Session, task_id: UUID) -> list[EvaluationAttempt]:
    """按发生顺序返回成功与失败的评分调用，供任务详情排障。"""

    return list(
        session.scalars(
            select(EvaluationAttempt)
            .where(EvaluationAttempt.task_id == task_id)
            .order_by(EvaluationAttempt.created_at, EvaluationAttempt.id)
        )
    )


def evaluations_by_candidate(
    session: Session, task_id: UUID
) -> dict[UUID, CandidateEvaluation]:
    """每张候选图取最新一条评分(可能先快速检查再补完整评分)。"""
    latest: dict[UUID, CandidateEvaluation] = {}
    for row in list_evaluations(session, task_id):
        latest[row.candidate_id] = row
    return latest
