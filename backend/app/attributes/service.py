"""属性识别编排与写入(M3,§6)。

两件事,严格分开:

    run_extraction()   逐图调模型 -> 落证据。**不产生任何结论**
    decide()           证据 -> 决定采信什么。纯判定,不调模型

## 为什么是逐图

一次调用只看一张图,而且**不告诉它已有的值**(§6.1)。v1 让模型
"先独立判断,再与 known 比对" —— 模型看到 `{"primary_color": "NAVY"}`
之后就会把图判成 NAVY,所谓交叉验证只是复读我们喂进去的答案。

逐图另有两个好处:新增一张素材时只识别新图(增量);某张图解析失败
不会毁掉整次识别。

## M3 的边界

合并规则(来源优先级、多数投票、AI 分歧)在 M4 的 `extractors/merge.py`。
本阶段的 `decide()` 只做单证据判定,而且因为校准表是空的,
`system_confidence` 恒为 None —— 于是所有值都落 `CANDIDATE`。

**这不是没做完,这是 fail closed 生效的样子。** 没有校准数据时,
阈值是一个没有意义的数字,任何自动确认都是在假装我们知道准确率。
"""
from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.attributes import confidence as conf
from app.attributes import queue_policy, run_state, scope_fingerprint, validation
from app.attributes.decision import decide_status
from app.attributes.registry import REGISTRY, extractable_fields
from app.attributes.validation import (
    AttributeOwnerError,
    AttributeValueError,
    owner_for,
    validate_attribute_value,
)
from app.core import audience as audience_rules
from app.core.clock import utc_now
from app.core.enums import (
    AttributeSource,
    AttributeStatus,
    AuditAction,
    ExtractionRunStatus,
    OwnerType,
)
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.extractors import call_budget
from app.extractors.evidence_plan import plan_evidence
from app.extractors.schema import (
    EXTRACTION_SCHEMA_VERSION,
    ExtractionParseError,
    enum_hint_for,
    normalize_value,
)
from app.media import service as media_service
from app.models.attribute import (
    AttributeCalibration,
    AttributeEvidence,
    ProductAttributeExtraction,
    ProductAttributeValue,
)
from app.models.product import Product
from app.services import audit

logger = get_logger(__name__)

#: 一张都没成功时落的错误码。**是个常量不是字面量**:它同时出现在
#: 落库这一处和读它的地方,而两处各打一遍字的下场是其中一处改了名字,
#: 另一处静静地再也匹配不到 —— 与 `field_limits` 那条规矩同源。
EXTRACTION_ALL_FAILED = "EXTRACTION_ALL_IMAGES_FAILED"


def _now():
    return utc_now()


# ------------------------------------------------------------------ 识别


def run_extraction(
    session: Session,
    *,
    product: Product,
    extractor,
    fields: tuple[str, ...] | None = None,
    only_media_ids: list[UUID] | None = None,
    only_variant_ids: list[UUID] | None = None,
) -> ProductAttributeExtraction:
    """逐图识别,落证据。**不写任何属性值。**

    `only_media_ids` 用于增量:新增一张素材时只识别新图。不传则识别
    该商品全部可用素材。

    `only_variant_ids` 用于**按颜色重试**(§11 第一行):上一次 run 的
    `RunOutcome.retry_scope` 已经算出了该重跑哪几个颜色,这个入参是它
    回来的那一跳。不传则不按颜色筛。

    两者同传时取**交集**。取并集的话「只重跑这几张图」会被一个颜色作用域
    悄悄扩大成整个颜色,而那是一次更贵的调用。
    """
    # 受众过滤在这里发生(PRD v2 §18.3):男装商品的识别目标里没有领型,
    # 女装的里没有腰头。受众未确认时退回全集 —— 该商品会先被工作台排去
    # "待确认受众",不在识别层替它猜
    audience = audience_rules.coerce(product.audience)
    targets = tuple(fields or extractable_fields(audience))
    # ---- §5.1 证据白名单:取数入口(A45-batch14-19)----
    #
    # 这一行原来是 `media_service.usable_assets(session, product.id)` 加下面
    # 一段 Python 过滤。判定从 14-7 起就是对的,**取数不是收口的**:任何一个
    # 新写的调用点只要照着 `usable_assets()` 抄一行,就能把 AI 图喂进付费
    # 抽取器,而没有任何地方会红。PRD 阶段 2 交付原文「白名单查询助手成为
    # 唯一取数入口」要消灭的正是这个形状。
    #
    # 过滤没有消失,它搬进了 `evidence_assets_for()` —— 判定仍然是
    # `evidence_rules.asset_is_extraction_input` 那一个,那边只是在它前面
    # 加了一道**被判定蕴含**的 SQL 粗筛(理由见那个函数的文档)。
    #
    # 这里显式传 `imported_url_trusted=True` 是为了**维持现状**,不是在判定
    # 外链可信:本仓还没有可信机制(见 evidence_rules 文档第二条),而今天
    # `IMPORTED_URL` 没有任何写入点,所以这个值改变不了任何一条真实数据的去留。
    # 接可信机制时改这里,不要改默认值 —— 默认值留给新调用点,新调用点该 fail closed。
    evidence = media_service.evidence_assets_for(
        session, product.id, imported_url_trusted=True
    )
    if only_media_ids:
        wanted = set(only_media_ids)
        evidence = [a for a in evidence if a.id in wanted]
    # An explicitly empty scope must stay empty. Treating [] like None would turn a
    # narrowly requested retry into a full-SPU extraction (and potentially a paid
    # provider call).
    if only_variant_ids is not None:
        # 作用域的划法**借 `run_state.scope_of`,不在这里重写一遍**。
        #
        # 就地写 `a.color_variant_id in wanted` 今天给出同样的结果,坏处在
        # 将来:那个函数明写着「不读 `variant_hint`」,而重试范围决定下一次
        # 付多少钱。在这里另写一份的话,下一个人顺手加上 hint 兜底
        # (「归属为空就看看模型猜的」)不会有任何地方报错 —— 而那正是
        # 14-9 / 14-10 / `scope_of` 三次拒绝过的同一件事。
        scopes = {str(v) for v in only_variant_ids}
        evidence = [a for a in evidence if run_state.scope_of(a) in scopes]
        if not evidence:
            # **和「一张证据都没有」分开报。** 合成一句的话,运营看到
            # 「没有可用素材」会去传更多图,而真正的原因是他勾的那几个颜色
            # 今天一张归属图都没有 —— 下一步是去按颜色传图,或者干脆
            # 重跑整件商品,两者都不是"再传几张通用图"
            raise ValidationError(
                "所选颜色下没有可用于识别的素材 —— "
                "按颜色重试只跑归属到该颜色的图,通用图不进任何颜色作用域",
                code=ErrorCode.INPUT_INVALID,
            )
    if not evidence:
        # **两条错误分开报。** 素材库里明明摆着十几张图,却告诉运营
        # 「没有素材」,会让人去传更多图 —— 而传进来的如果还是 AI 图,
        # 一张也进不来。所以只在证据为空时才多查一次总数,把话说准。
        #
        # 用计数而不是 `usable_assets()`:识别路径上只要出现过一个装着
        # 未过滤素材行的变量,下一个人就可能顺手拿它去喂抽取器 ——
        # 而那一步不会有任何地方报错。拿不到行,就不会有人用错行。
        total = media_service.usable_asset_count(session, product.id)
        if total:
            raise ValidationError(
                f"该商品的 {total} 条可用素材全部不能作为识别证据"
                "(AI 生成图、渠道回抓图与参考图不进识别输入,见 §5.1)",
                code=ErrorCode.INPUT_INVALID,
            )
        raise ValidationError(
            "该商品没有可用于识别的素材(需要至少一条 READY 且未隔离的素材)",
            code=ErrorCode.INPUT_INVALID,
        )
    assets = evidence

    # ---- 付费抽取器的单次上限(A45-batch14)----
    #
    # 判定在 `extractors/call_budget.py`(纯函数,边界能穷举)。这里只负责
    # 读配置、翻译成接口错误。检查必须排在逐图循环**之前** —— 排在循环里的话,
    # 拒绝发生时钱已经花出去一部分,而运营看到的是一次"失败"的识别,
    # 失败的识别不会让人想到去查账单。
    billable = bool(getattr(extractor, "billable", False))
    if billable:
        from app.providers._config import provider_int

        ceiling = provider_int(
            "EXTRACTOR_MAX_IMAGES_PER_RUN", call_budget.DEFAULT_MAX_IMAGES_PER_RUN
        )
        if call_budget.over_call_budget(
            image_count=len(assets), ceiling=ceiling, billable=billable
        ):
            raise ValidationError(
                call_budget.over_budget_message(
                    image_count=len(assets), ceiling=ceiling
                ),
                code=ErrorCode.INPUT_INVALID,
            )

    # ---- §9.2 幂等 + §5.3 指纹(A45-batch14-20)----
    #
    # 判定在 `run_state` 与 `scope_fingerprint`,两边都零依赖、都被穷举验过;
    # 这里只做三件事:凑入参、问一次库、把结果写进那几列。
    #
    # **顺序:排在上限检查之后、建行之前。** 排在上限之前也可以 —— 复用一次
    # 调用都不产生,上限本来就拦不到它 —— 两种排法的行为其实等价:一次超限的
    # 请求根本没建出行,所以库里不可能有一个超限的键去命中。等价时不动既有代码。
    #
    # 但**必须**排在逐图循环之前:排在后面等于先把钱花完再问「这次是不是重复
    # 请求」,而幂等键要挡的正是双击和网络重发。
    identity = _run_identity(
        product=product,
        extractor=extractor,
        assets=assets,
        targets=targets,
        whole_product=not only_media_ids,
    )
    if identity.key:
        existing = _run_occupying_key(session, identity.key)
        verdict = run_state.reuse_verdict(
            existing.status if existing is not None else None
        )
        if existing is not None and verdict != run_state.ReuseVerdict.NEW_RUN:
            # 两档都是「不再付一次钱」,但落点不同,所以日志分得开:
            # RETURN_EXISTING 是那一次还在跑,REUSE_RESULT 是那一次跑完了。
            # 合成一句的话,「双击」和「换了模型却还在用旧结果」这两种
            # 完全不同的怀疑,在排查时读到的是同一行字
            logger.info(
                "extraction request reused an existing run",
                extra={
                    "extra_fields": {
                        "product_id": str(product.id),
                        "extraction_id": str(existing.id),
                        "verdict": verdict,
                        "idempotency_key": identity.key,
                    }
                },
            )
            return existing
    elif identity.no_key_reason:
        # **如实说为什么没有键。**不说的话,「双击产生了两个 run」这件事
        # 在排查时只剩一个猜:是键算错了,还是根本没算?两者的修法完全不同
        logger.info(
            "extraction run has no idempotency key",
            extra={
                "extra_fields": {
                    "product_id": str(product.id),
                    "reason": identity.no_key_reason,
                }
            },
        )

    started = time.monotonic()
    row = ProductAttributeExtraction(
        product_id=product.id,
        extractor=getattr(extractor, "name", "unknown"),
        target_fields=list(targets),
        image_count=len(assets),
        schema_version=EXTRACTION_SCHEMA_VERSION,
        # 建行即 RUNNING。这一档**占键**(`KEY_OCCUPYING_STATUSES`),
        # 所以第二个并发请求要么在下面撞唯一索引、要么查到这一行然后复用 ——
        # 两条路都不会再打一次付费调用。同步执行下没有 QUEUED 那一档
        status=ExtractionRunStatus.RUNNING.value,
        spu_id=identity.spu_id,
        requested_scope=identity.scope_token,
        input_fingerprint=identity.input_fingerprint,
        idempotency_key=identity.key,
    )
    # 先查后插只能挡住「明显重复」,挡不住并发 —— 与 `create_product` 同一个
    # 形状,也是 §9.2 写「数据库裁决」的原因。保存点是必需的:约束冲突会让
    # 整个事务进入 aborted 状态,不回滚就没法继续往下跑这次识别
    savepoint = session.begin_nested()
    try:
        session.add(row)
        session.flush()
        savepoint.commit()
    except IntegrityError:
        savepoint.rollback()
        winner = _run_occupying_key(session, identity.key) if identity.key else None
        if winner is None:
            # 不是幂等键撞的(或者撞了但那一行已经不占键了)。**不吞** ——
            # 把别的约束冲突翻译成「复用了一次识别」会让一个真实的写入缺陷
            # 表现成一次安静的成功
            raise
        logger.info(
            "extraction run lost the idempotency race and reused the winner",
            extra={
                "extra_fields": {
                    "product_id": str(product.id),
                    "extraction_id": str(winner.id),
                    "idempotency_key": identity.key,
                }
            },
        )
        return winner

    enum_defs = {name: enum_hint_for(name) for name in targets}
    # 受众传给抽取器,让 Schema/提示词里的**枚举取值**也按受众收窄。
    # 键名是抽取器模块的契约(`extractors.vision.AUDIENCE_KEY`);
    # 这里写字面量会在两边改名时静默脱钩,所以 import 它
    from app.extractors.vision import AUDIENCE_KEY

    options: dict[str, Any] = {AUDIENCE_KEY: audience} if audience else {}
    succeeded = failed = fabricated = 0
    #: 逐图成绩,按作用域归集用(§11 第一行)。**不是两个标量能替代的东西**:
    #: 「3 张图散着失败」与「B 色的 3 张全失败」在计数里一模一样,
    #: 而前者每个颜色都还有证据、后者 B 色一条都没有
    outcomes: list[run_state.ImageResult] = []
    for asset in assets:
        call_started = time.monotonic()
        try:
            result = extractor.extract(
                image=asset,
                target_fields=targets,
                enum_defs=enum_defs,
                options=dict(options),
            )
        except Exception as exc:  # noqa: BLE001
            # 一张图失败不毁掉整次识别 —— 这正是逐图调用的好处之一。
            #
            # 默认只记类型:provider / 传输错误的消息里嵌着请求地址,
            # 而预签名地址的查询串就是凭据(`EXTRACTOR_MODEL_SEND_PUBLIC_URLS`)。
            #
            # **解析失败是另一回事**,它必须多记一点。`ExtractionParseError`
            # 是我们自己的解析器对模型输出报的错:消息是我们写的字符串,
            # `detail` 里是坏在哪 + 已抹查询串的原文摘要。只记一个类型名的话,
            # 模型开始返回坏 JSON 时(降级档位没有 Schema 兜着,今天就会发生)
            # 运营手上只有"那张图 failed",唯一想得到的动作是再点一次识别,
            # 而每一次都在花钱 —— 与 §4.5 三个原因指向三个动作同一个病灶,
            # 只是换到了日志这条通道上
            failed += 1
            outcomes.append(run_state.image_result(asset, succeeded=False))
            fields: dict[str, Any] = {
                "media_asset_id": str(asset.id),
                "error": type(exc).__name__,
            }
            if isinstance(exc, ExtractionParseError):
                fields["parse_error"] = str(exc)
                fields.update(exc.detail)
            logger.warning(
                "extraction failed for one image",
                extra={"extra_fields": fields},
            )
            if billable:
                _record_extraction_usage(
                    session,
                    extractor=extractor,
                    succeeded=False,
                    duration_ms=int((time.monotonic() - call_started) * 1000),
                    error_code=type(exc).__name__,
                )
            continue

        succeeded += 1
        outcomes.append(run_state.image_result(asset, succeeded=True))
        if billable:
            # 失败的也记(上面那条分支):厂商在收到请求那一刻就开始计费,
            # 超时和解析失败都不退钱 —— 与评分流水同一条口径
            _record_extraction_usage(
                session,
                extractor=extractor,
                succeeded=True,
                duration_ms=result.duration_ms
                or int((time.monotonic() - call_started) * 1000),
                error_code=None,
            )
        row.model_name = row.model_name or result.model_name
        row.prompt_version = row.prompt_version or result.prompt_version

        # ---- 写什么由纯判定决定(§6.3 / §4.5)----
        #
        # 目标过滤、无值证据、两条通道归并全在 `extractors/evidence_plan.py`,
        # 那里零依赖所以能在 `tests/pure/` 被穷举。这里只做物化 ——
        # 两件事分开的理由与 `workflows/publish_view.py` 顶部那条相同,
        # 而 batch13-2 的 F1 是"判定和落库混在一起时,决定只落一半
        # 不会被任何守卫看见"的实例。
        plan = plan_evidence(result, targets)
        fabricated += plan.fabricated
        for name in plan.fabricated_names:
            logger.warning(
                "model returned a field outside the requested targets",
                extra={"extra_fields": {"field": name}},
            )
        for item in plan.evidence:
            session.add(
                AttributeEvidence(
                    extraction_id=row.id,
                    media_asset_id=asset.id,
                    field_name=item.field_name,
                    # 无值证据的"模型原始输出"就是这个 reason —— 原样存,
                    # 不发明第二种空值表示(`raw_value_json` 是 NOT NULL 列)
                    raw_value_json=(
                        {"missing_reason": item.missing_reason}
                        if item.is_missing
                        else item.value
                    ),
                    normalized_value=(
                        None
                        if item.is_missing
                        else normalize_value(item.field_name, item.value)
                    ),
                    model_confidence=item.confidence,
                    evidence_text=item.evidence_text,
                    missing_reason=item.missing_reason,
                    model_name=result.model_name or "",
                    prompt_version=result.prompt_version or "",
                )
            )

    row.succeeded_count = succeeded
    row.failed_count = failed
    row.fabricated_field_count = fabricated
    row.duration_ms = int((time.monotonic() - started) * 1000)

    # ---- 终态落列(A45-batch14-20 / §4.6)----
    #
    # 判定一个字没动,仍然是 `run_state.terminal_status_for`;换的是**调用点**:
    # 14-11 到 14-19 期间它挂在 `models/attribute.py` 的派生属性上,列一落库
    # 就必须挪到这里 —— 唯一索引的谓词 `status IN (...)` 由数据库读**列**求值,
    # 而属性只在 Python 里存在,两个答案漂移时没有任何地方会报。
    #
    # `cancel_requested=False` 是**如实描述现状**不是占位:同步执行下没有
    # 取消通道(§4.6 的 cancel 要 Celery,那一半仍然欠着)。有了取消列之后
    # 它读那一列,而在那之前任何非 False 的值都是编出来的。
    #
    # 不做 `can_transition(RUNNING, verdict)` 检查:`terminal_status_for` 的
    # 值域恰好是 `TRANSITIONS[RUNNING]`(纯守卫钉着这条),所以那个检查今天
    # 恒真;而它一旦不恒真,在这里抛异常会连着整次识别的证据一起回滚 ——
    # 钱已经花了,回滚掉的是唯一能证明花在哪的记录。
    row.status = run_state.terminal_status_for(
        image_count=row.image_count or 0,
        succeeded=succeeded,
        failed=failed,
        cancel_requested=False,
    ).value

    # ---- 终态派生(A45-batch14-11 / §4.6)----
    #
    # 在这之前,**全部失败的 run 和全部成功的 run 在库里长得一模一样**:
    # `error_code` 是 NULL、接口返回 200,差别只有两个计数。于是
    # 「查一下最近有哪些识别是失败的」这个问题在 SQL 里问不出来,
    # 而按计数反推等于把判定又抄一份(前端已经抄过一份,还抄错了)。
    #
    # `row.status` 是派生属性,不落列;`error_code` 是真列,所以这里补写。
    # 不写 `error_message`:那一列存的是**某一次调用**的原话,而
    # 「一张都没成功」是整次 run 的结论,逐图的原因已经逐条进了日志。
    if row.status == ExtractionRunStatus.FAILED.value and not row.error_code:
        row.error_code = EXTRACTION_ALL_FAILED
    session.flush()

    # ---- §11 第一行:哪个颜色全军覆没(A45-batch14-13)----
    #
    # 归集在 `run_state.outcome_by_scope`(零依赖、输入空间被穷举验过),
    # 这里只把逐图成绩递过去。**今天它一定给出空清单** —— `scope_of` 读的是
    # 阶段 2 的归属外键 `color_variant_id`,那一列还不存在,于是每张图都落在
    # 共享作用域里,一个颜色子集都分不出来。
    #
    # 那为什么现在就接:接的是**形状**。这一列落库那天,不接线的表现是
    # 「判定写好了没人调」,而最省事的补法是就近抓一个 `variant_hint` 来分组 ——
    # 那正是 §4.8 明令不许的(模型猜的值决定重试范围,而重试范围决定付多少钱)。
    #
    # 落点只有日志,**这是本批的欠账不是设计**:§11 要的「失败颜色明确列出」
    # 要一列才成立,理由与规格见
    # `test_listing_the_failed_scopes_needs_a_column_and_here_is_which_one`。
    outcome = run_state.outcome_by_scope(outcomes)
    if outcome.failed_scopes:
        logger.warning(
            "every image of one or more colour scopes failed",
            extra={
                "extra_fields": {
                    "extraction_id": str(row.id),
                    "failed_scopes": list(outcome.failed_scopes),
                    # 重试范围**跟着一起报**:少了它,读日志的人得自己拼一次,
                    # 而拼宽一档(比如整批重试)就是为已经答上来的颜色再付一次钱
                    "retry_scope": outcome.retry_scope.token,
                }
            },
        )

    logger.info(
        "extraction finished",
        extra={
            "extra_fields": {
                "product_id": str(product.id),
                "extraction_id": str(row.id),
                "images": len(assets),
                "succeeded": succeeded,
                "failed": failed,
            }
        },
    )
    return row


@dataclass(frozen=True)
class _RunIdentity:
    """一次 run 的身份四件套(§4.6 的四个列)+ 「为什么没有键」。

    `no_key_reason` 不是日志装饰。没有它的话,「双击产生了两个 run」这件事
    在排查时只剩一个猜:键算错了,还是根本没算?两者的修法完全不同 ——
    前者要去看编码,后者要去看这个商品有没有 `spu_id`。
    """

    spu_id: Any | None
    scope_token: str | None
    input_fingerprint: str | None
    key: str | None
    no_key_reason: str | None


def _run_identity(
    *, product: Product, extractor, assets: list, targets: tuple[str, ...],
    whole_product: bool,
) -> _RunIdentity:
    """凑齐 §9.2 幂等键与 §5.3 指纹的入参。**判定不在这里。**

    ## 指纹要走 `AssetView`

    §5.3 的元素清单用 `asset_id` / `content_hash`,而 `MediaAsset` 上的列名是
    `id` / `sha256`。翻译由 `scope_fingerprint.views()` 做 —— 直接把 ORM 行
    递进去的话每个元素都是一串 None,而那**不报错**:算出来的是一个所有素材
    都长得一样的指纹,于是换了图也不 stale。

    `imported_url_trusted=True` 与上面取数那一行一致。不一致的后果是指纹
    按一个比取数更窄的集合算 —— 那批图真的进了识别,却不参与「输入变没变」。

    ## 三种建不出键的情况,一律留空而不是编一个

        只识别指定素材    `canonical_scope` 只有共享 / 指定颜色 / 全部三种形状,
                          「任意素材子集」不是其中之一。硬塞成 ALL 的话,
                          `requested_scope` 这一列会说一句不真的话,而它是
                          键的一部分 —— 别的调用点照那句话复算会得到另一个键
        商品没有 spu_id   老建档路径(阶段 1 的剩余项)还不写这一列。拿
                          `product_id` 凑一个假的 SPU 作用域是**最省事也最坏**
                          的补法:两个不同 SPU 的同型请求会算出同一个键
        抽取器报不出版本  见 `extractors/base.declared_versions` —— 取响应里的
                          版本等于付过钱才算得出键,而键要挡的是付钱前那两下

    留空的代价是这几条路径退回本批之前的行为(双击付两次钱),**不是**错误地
    拦住请求。方向是刻意的:少挡一次的代价是一次重复付费,挡错一次的代价是
    一个再也识别不了的商品。
    """
    views = scope_fingerprint.views(assets)
    prints = scope_fingerprint.fingerprints(views, imported_url_trusted=True)
    # 这次 run 吃进去的那批素材的整体指纹。颜色维在 `prints` 里也算好了,
    # 供下面建键用;落列的是共享那一维 —— 它覆盖全部用到的素材
    input_fingerprint = prints.get(scope_fingerprint.SHARED)

    if not whole_product:
        return _RunIdentity(None, None, input_fingerprint, None, "PARTIAL_MEDIA_SELECTION")

    scope = run_state.canonical_scope(all_variants=True)
    spu_id = getattr(product, "spu_id", None)
    if spu_id is None:
        return _RunIdentity(None, scope.token, input_fingerprint, None, "PRODUCT_HAS_NO_SPU")

    declared = getattr(extractor, "declared_versions", None)
    if declared is None:
        return _RunIdentity(
            spu_id, scope.token, input_fingerprint, None, "EXTRACTOR_DECLARES_NO_VERSIONS"
        )
    model_version, prompt_version = declared()

    try:
        key = run_state.idempotency_key(
            spu_id=spu_id,
            scope=scope,
            fingerprints=prints,
            model_version=model_version,
            prompt_version=prompt_version,
            requested_fields=targets,
        )
    except ValueError as exc:
        # 判定对缺维的入参抛 ValueError(空模型版本、没有指纹的作用域……)。
        # **翻译成「没有键」而不是让它冒出去**:识别本身是能跑的,
        # 少的只是幂等这一层保护,而把一次能跑的识别变成 500 更贵
        return _RunIdentity(
            spu_id, scope.token, input_fingerprint, None, f"KEY_REFUSED:{exc}"[:120]
        )
    return _RunIdentity(spu_id, scope.token, input_fingerprint, key, None)


def _run_occupying_key(session: Session, key: str) -> ProductAttributeExtraction | None:
    """当前**占着**这个键的那一行(§9.2)。

    状态集合直接来自 `run_state.KEY_OCCUPYING_STATUSES` —— 与
    `uq_attr_extractions_idempotency_key` 的谓词同一个来源。手写一份的话
    两者会漂移,而漂移的表现极隐蔽:库拦下了插入(键被占),这个查询却
    说没人占,于是 `IntegrityError` 那一路找不到赢家,一次正常的并发
    双击变成 500。

    `ORDER BY created_at DESC` 是为了在**理论上不该出现**的多行情况下也有
    确定答案 —— 唯一索引保证最多一行,但索引是在真库上才生效的东西,
    而这个函数在测试夹具与未来的分库场景里都得有一个稳定的选择。
    """
    stmt = (
        select(ProductAttributeExtraction)
        .where(
            ProductAttributeExtraction.idempotency_key == key,
            ProductAttributeExtraction.status.in_(
                sorted(s.value for s in run_state.KEY_OCCUPYING_STATUSES)
            ),
        )
        .order_by(ProductAttributeExtraction.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _record_extraction_usage(
    session: Session,
    *,
    extractor,
    succeeded: bool,
    duration_ms: int | None,
    error_code: str | None,
) -> None:
    """一次付费识别调用 = 一条流水。**成功与失败一视同仁。**

    没有这一条,真实抽取器上线那天起,`/spend` 页就缺了识别这一整块开销 ——
    和 a25 之前评分调用完全不进流水表是同一个洞,这次在接后端的同一批堵上,
    不留"先跑起来再补记账"的窗口。

    `task_id` / `attempt_id` 为 None:识别不挂生成任务。`billing_key` 不传:
    每一次识别调用都是**真的又调了一次**(逐图、无恢复路径),不存在
    "同一笔消费记两行"的窗口 —— 与轮询流水同一条口径,不与 submit 混。
    记在谁名下问抽取器自己(`usage_provider`),推导规则在
    `llm/endpoint_trust.provider_label_from_base_url`。

    延迟导入:`generation_service` 在模块级引 providers 注册表,
    属性层不需要在 import 期背上它。
    """
    from app.services.generation_service import record_usage

    record_usage(
        session,
        provider=str(
            getattr(extractor, "usage_provider", "")
            or getattr(extractor, "name", "unknown")
        ),
        task_id=None,
        attempt_id=None,
        operation="attribute_extract",
        succeeded=succeeded,
        candidate_count=0,
        duration_ms=duration_ms,
        error_code=(error_code[:48] if error_code else None),
        billable_units=1,
    )


def list_evidence(session: Session, extraction_id: UUID) -> list[AttributeEvidence]:
    return list(
        session.scalars(
            select(AttributeEvidence)
            .where(AttributeEvidence.extraction_id == extraction_id)
            .order_by(AttributeEvidence.field_name, AttributeEvidence.created_at)
        )
    )


# ------------------------------------------------------------------ 判定


def _calibration_bins(
    session: Session, *, field_name: str, model_name: str, prompt_version: str
) -> list[conf.CalibrationBin]:
    rows = session.scalars(
        select(AttributeCalibration).where(
            AttributeCalibration.field_name == field_name,
            AttributeCalibration.model_name == model_name,
            AttributeCalibration.prompt_version == prompt_version,
        )
    )
    return [
        conf.CalibrationBin(
            bin_lower=float(r.bin_lower),
            bin_upper=float(r.bin_upper),
            observed_accuracy=float(r.observed_accuracy),
            sample_count=r.sample_count,
            calibration_version=r.calibration_version,
        )
        for r in rows
    ]


def current_value(
    session: Session, *, owner_type: OwnerType, owner_id: str, field_name: str
) -> ProductAttributeValue | None:
    return session.scalar(
        select(ProductAttributeValue).where(
            ProductAttributeValue.owner_type == owner_type.value,
            ProductAttributeValue.owner_id == owner_id,
            ProductAttributeValue.field_name == field_name,
            ProductAttributeValue.is_current.is_(True),
        )
    )


#: 指纹与取数口径的**唯一一份**声明(§5.1 / §5.3)。
#:
#: `run_extraction` 那一行原来是就地写的字面量,而事实侧一接线就变成了
#: 四个调用点各写一遍 —— 而这个值一旦有两个答案,后果不是报错:指纹会按
#: 一个比取数更窄(或更宽)的集合算,于是**那批图真的进了识别、真的花了钱,
#: 却不参与"输入变没变"的判断**,补一张图不会让事实过期,AC-21 静静地不成立。
#:
#: 取 True 是为了**维持现状**,不是在判定外链可信:本仓还没有可信机制
#: (见 `media/evidence_rules` 文档第二条),而今天 `IMPORTED_URL` 没有任何
#: 写入点,所以这个值改变不了任何一条真实数据的去留。接可信机制时改这里,
#: **不要改 `evidence_assets_for` 的默认值** —— 默认值留给新调用点,
#: 新调用点该 fail closed。
EVIDENCE_IMPORTED_URL_TRUSTED = True


def scope_fingerprints_for(
    session: Session, product: Product
) -> dict[str | None, str]:
    """一件商品当前各作用域的样品指纹。**事实侧的唯一取数入口。**

    返回 `{None: 共享指纹, 颜色id: 颜色指纹, ...}`,形状与
    `scope_fingerprint.fingerprints()` 相同(它就是这里的判定)。

    ## 为什么要有这个函数,而不是三处各抄一行

    事实侧的指纹有三个消费者:`apply_evidence`(识别写值)、`confirm`
    (人工确认)、`workbench._attribute_facts`(读取时判过期)。三处各写
    一行 `evidence_assets_for(...)` + `views()` + `fingerprints()` 的话,
    这个仓库已经付过一次学费的形状会原样回来 —— §5.1 那一条写的是
    「白名单查询助手成为**唯一取数入口**」,而它防的从来不是今天的代码,
    是明天那个照着抄一行、少传一个关键字的调用点。

    这里的漂移尤其安静:**写入侧按 A 集合算指纹、读取侧按 B 集合算**,
    两边都不报错,表现是一条刚刚确认完的事实立刻显示"已过期",
    或者反过来,一条真的该过期的事实永远显示正常。

    ## 经 `views()`,不把 ORM 行直接递进去

    §5.3 的元素清单用 `asset_id` / `content_hash`,而 `MediaAsset` 上的列名
    是 `id` / `sha256`。少了这层翻译,`_element()` 的每一项都 getattr 到
    None,算出来的是**一个所有素材都长得一样的指纹** —— 不报错、不抛异常,
    只是换了图也不 stale。那是本模块最安静的坏法。
    """
    # SPU 事实必须看整个 SPU 的样品集合；只按当前 SKU 查会让同款另一颜色
    # 刚确认的共享事实立即显示过期。没有 spu_id 的历史行仍按商品隔离。
    assets = media_service.evidence_assets_for(
        session,
        product.id if product.spu_id is None else None,
        spu_id=product.spu_id,
        imported_url_trusted=EVIDENCE_IMPORTED_URL_TRUSTED,
    )
    return scope_fingerprint.fingerprints(
        scope_fingerprint.views(assets),
        imported_url_trusted=EVIDENCE_IMPORTED_URL_TRUSTED,
    )


def fingerprint_for_owner(
    prints: Mapping[str | None, str], *, owner_type: OwnerType, owner_id: str
) -> str | None:
    """这条事实该存/该比的那一个指纹。**作用域判定不在这里。**

    判定在 `validation.fingerprint_scope()`(纯层,能穷举 `OwnerType` 的每一个
    成员)。这里只负责把它的答案翻译成一次查表。

    两种情况返回 None,含义不同但**下游一致**:

        不参与(CHANNEL)      写入时不存指纹,读取时不判过期
        参与但查不到那个键    该颜色今天没有任何证据素材。`facts_stale`
                              拿它当"对不上",判过期 —— 而那正是对的:
                              一个颜色的最后一张证据图被删掉时,
                              它的事实最该被重新看一眼

    第二种是 `fingerprints()` 那条注释里说的"没有素材的颜色不出现在结果里",
    在事实侧的落点。**不要在这里给它兜一个空指纹**:兜出来的值会让一条
    没有任何证据支撑的事实带着"指纹没变"的证明进导出。
    """
    scope = validation.fingerprint_scope(owner_type, owner_id)
    if not scope.participates:
        return None
    return prints.get(scope.scope)


def stale_fields(
    session: Session,
    product: Product,
    values: Mapping[str, ProductAttributeValue],
    *,
    settled_only: bool = True,
) -> frozenset[str]:
    """哪些事实的输入指纹对不上了(§5.3 / AC-21)。**`facts_stale` 的唯一调用点。**

    ## 这是 AC-21 的落点

    「给颜色 A 补传样品后:共享事实与 A 色事实 stale,**B 色事实不受影响**」。
    这里不重新证明它 —— 全称命题由 `scope_fingerprint` 那边穷举验过。
    这个函数只做三件事:取一次当前指纹、按 owner 找到该比的那一个、比。

    ## `settled_only`:默认只看已确认的那些

    过期这件事只对**已经立起来的事实**有意义。一个 CANDIDATE 值本来就等着
    人处理,把它也标成"过期"会让工作台对同一个字段同时说两句话 ——
    「有建议值待确认」和「已确认的事实过期了」,而后者是假的:
    那条事实从来没有立起来过,谈不上过不过期。

    档位判定**不在这里手写状态集合**,走 `queue_policy.disposition()` ——
    那张表是穷举的、由守卫钉着,而手写一份 `{"CONFIRMED"}` 的下场是
    `AttributeStatus` 新增一档时这里静静地漏掉它。

    传 `settled_only=False` 表示全都看,给巡检和接口出参用:它们问的是
    "库里这一行的指纹对不对得上",不是"工作台该不该催人"。

    ## 参不参与要单独问一次,不能靠 `current is None` 推

    `fingerprint_for_owner()` 对**两种**情况都返回 None:不参与(CHANNEL),
    以及参与但那个作用域今天没有证据素材。后者必须判过期(一个颜色的最后
    一张证据图被删掉,它的事实最该被重新看一眼),前者必须不判 ——
    只看返回值的话两者分不开,而合并的表现是刊登属性跟着补图集体过期。

    ## 为什么返回字段名集合而不是逐行布尔

    调用方(工作台判定、接口出参)问的都是"哪些字段",按行返回会让每个
    调用方自己再折一次 —— 那是两处判定,而两处判定会漂移。
    """
    if not values:
        return frozenset()
    prints = scope_fingerprints_for(session, product)
    stale: set[str] = set()
    for name, row in values.items():
        if settled_only and (
            queue_policy.disposition(row.status) is not queue_policy.Disposition.SETTLED
        ):
            continue
        owner_type = OwnerType(row.owner_type)
        if not validation.fingerprint_scope(owner_type, row.owner_id).participates:
            continue
        current = fingerprint_for_owner(
            prints, owner_type=owner_type, owner_id=row.owner_id
        )
        if scope_fingerprint.facts_stale(
            stored_fingerprint=row.input_fingerprint, current_fingerprint=current
        ):
            stale.add(name)
    return frozenset(stale)


def set_value(
    session: Session,
    *,
    owner_type: OwnerType,
    owner_id: str,
    field_name: str,
    value: Any,
    source: AttributeSource,
    status: AttributeStatus,
    actor: str = "system",
    model_confidence: float | None = None,
    system_confidence: float | None = None,
    breakdown: dict[str, Any] | None = None,
    evidence_ids: list[str] | None = None,
    input_fingerprint: str | None = None,
) -> ProductAttributeValue:
    """写一个属性值。**这是唯一允许写 `product_attribute_values` 的入口。**

    旧值置 `is_current=False` 并记 `superseded_at`,不删除 —— 平台驳回后
    要能回答「上次提交时这个字段是什么值」。

    投影到 `products` 的兼容列在**同一个事务**里做(§5.4)。分两次写的话,
    两边会在崩溃时永久不一致,而投影列还有旧查询在读。

    ## `input_fingerprint`:写在这里,因为这里是唯一写入点

    §4.5 要求每条事实记下"它是按哪批素材立起来的"。调用方算好了传进来,
    因为**该比哪个作用域取决于 owner**,而 owner 正是这个函数的入参
    (判定在 `validation.fingerprint_scope()`,取数在 `scope_fingerprints_for()`)。

    默认 None = 不知道。它和"算出来是空"不是一回事,但下游一致:
    `facts_stale(stored=None)` 恒为 True,判过期 —— 算不出来就算过期
    (`scope_fingerprint` 模块文档第四条)。默认值朝**不放行**那一侧倒,
    是因为反方向的代价是一条来路不明的事实带着"没变"的证明进导出。
    """
    # **校验落在这里,不落在 API 层(A43 / BLOCK-03)。**
    #
    # 这个函数自称"唯一允许写 product_attribute_values 的入口",那么类型契约
    # 就该长在它身上。放到 API 层只拦得住人工那一条路,而识别合并
    # (`apply_evidence`)走的是同一个函数 —— 模型输出恰恰是最不该被信任的那一路。
    #
    # 原来这里只有 `get_field(field_name)`:它证明字段名注册过,没有证明值
    # 符合那个字段的定义。人工确认值写成 MANUAL + CONFIRMED 之后不被任何自动
    # 流程覆盖,于是一个错误值一旦写进去,反而是系统里保护级别最高的数据。
    value = validate_attribute_value(field_name, value)

    previous = current_value(
        session, owner_type=owner_type, owner_id=owner_id, field_name=field_name
    )
    if previous is not None:
        if previous.source == AttributeSource.MANUAL.value and source != AttributeSource.MANUAL:
            # 人工值永不被自动覆盖(§6.2 规则 4)。
            # 抛错而不是静默跳过:调用方以为写成功了才是更糟的结果
            raise ValidationError(
                f"{field_name} 已有人工确认的值,不能被 {source.value} 覆盖;"
                "请走冲突处理",
                code=ErrorCode.INPUT_INVALID,
                http_status=409,
            )
        previous.is_current = False
        previous.superseded_at = _now()

    row = ProductAttributeValue(
        owner_type=owner_type.value,
        owner_id=owner_id,
        field_name=field_name,
        value_json=value,
        normalized_value=normalize_value(field_name, value),
        source=source.value,
        status=status.value,
        model_confidence=model_confidence,
        system_confidence=system_confidence,
        confidence_breakdown=breakdown,
        evidence_ids=evidence_ids or [],
        input_fingerprint=input_fingerprint,
        valid_from=_now(),
        is_current=True,
        confirmed_by=actor if status is AttributeStatus.CONFIRMED else None,
        confirmed_at=_now() if status is AttributeStatus.CONFIRMED else None,
    )
    session.add(row)
    session.flush()

    _project_to_legacy_column(session, owner_id=owner_id, field_name=field_name, value=value)

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="ProductAttributeValue",
        entity_id=row.id,
        payload={
            "field": field_name,
            "status": status.value,
            "source": source.value,
            # 记**有没有**指纹,不记指纹本身:64 位哈希对读审计的人是噪音,
            # 而"这条事实立起来的时候算不算得出输入"恰恰是事后排查
            #「它为什么一确认就显示过期」唯一有用的那一位
            "fingerprinted": input_fingerprint is not None,
        },
    )
    return row


def _project_to_legacy_column(
    session: Session, *, owner_id: str, field_name: str, value: Any
) -> None:
    """把值投影到 `products` 的兼容列(§5.4)。**单向,同事务。**

    投影列是只读兼容层:业务代码不得直接写它们,读也应当逐步改走
    `get_effective_value()`。这个函数是唯一的写入点。

    M3 只投影 PRODUCT/SPU 层的字段。VARIANT/SKU 层等 M4 的
    `CanonicalProduct` 组装好之后再说 —— 现在把颜色投影到
    `products.primary_color` 会在一个 SPU 多颜色时写错。
    """
    spec = REGISTRY.get(field_name)
    if spec is None or not spec.legacy_column:
        return
    if spec.owner_type not in (OwnerType.SPU, OwnerType.PRODUCT):
        return
    try:
        product = session.get(Product, UUID(owner_id))
    except (ValueError, AttributeError):
        return
    if product is None:
        return
    setattr(product, spec.legacy_column, value if value is not None else "")


def apply_evidence(
    session: Session,
    *,
    product: Product,
    extraction: ProductAttributeExtraction,
    actor: str = "system",
) -> list[ProductAttributeValue]:
    """把一次识别的证据合并成属性值(M4)。

    合并规则本身是纯函数(`extractors/merge.py`):来源优先级、加权投票、
    冲突判定、AI 分歧。这里只负责把库里的证据喂进去、把结论写回来。

    权重用的是 `system_confidence` 而**不是模型自报的 confidence** ——
    未校准时前者是 None(权重 0),于是整组证据的权重和为零,
    合并结论是 `INSUFFICIENT`,不会硬凑出一个值来。
    """
    from app.extractors.merge import EvidenceItem, MergeOutcome, merge_all

    evidence = list_evidence(session, extraction.id)
    existing_map = effective_map(session, product.id, product=product)
    # §4.5:每条事实记下它自己作用域的指纹。**算一次,逐字段查表。**
    #
    # 不能拿 `extraction.input_fingerprint`:那是**这次 run 吃进去的整批
    # 素材**的共享指纹,而这里要写的是共享事实与各颜色事实各自的那一个。
    # 拿 run 行的顶数,给颜色 A 补一张图会 stale 掉 B 色事实 —— D1 那个
    # 问题从后门原样回来,而且不报错。那正是欠账守卫当初点名的捷径。
    #
    # 也不能逐字段各取一次数:那是 N 次 `evidence_assets_for`,而且**两次
    # 取数之间素材可能变**,于是同一次合并写出来的几条事实分属不同的
    # 输入快照 —— 一个查不出来的不一致
    prints = scope_fingerprints_for(session, product)

    # 逐条证据算 system_confidence。每张图的质量与来源不同,
    # 所以权重必须一条一条算,不能按字段算一次
    items: list[EvidenceItem] = []
    #: 按 (字段, 归一化值) 存因子分解。
    #:
    #: **不能按字段存。** 合并可能选中一个和"第一条证据"不同的值,
    #: 那样界面上展示的因子分解就来自一条被淘汰的证据 —— 而运营正是
    #: 靠它判断"该补一张图还是重拍",依据错了整个判断就错了。
    #: 同值多条证据时保留置信度最高的那条。
    breakdowns: dict[tuple[str, str], Any] = {}
    for ev in evidence:
        if ev.normalized_value is None:
            # 归一失败的证据不参与合并:模型自造的枚举值不能流到下游
            continue
        asset = media_service.get_asset(session, ev.media_asset_id)
        same_field = [
            e for e in evidence
            if e.field_name == ev.field_name and e.normalized_value is not None
        ]
        agreeing = sum(1 for e in same_field if e.normalized_value == ev.normalized_value)
        breakdown = conf.compute(
            field_name=ev.field_name,
            model_confidence=float(ev.model_confidence) if ev.model_confidence else None,
            bins=_calibration_bins(
                session,
                field_name=ev.field_name,
                model_name=ev.model_name,
                prompt_version=ev.prompt_version,
            ),
            total_images=len(same_field),
            agreeing_images=agreeing,
            quality=asset.quality_json,
            media_source=asset.source,
        )
        key = (ev.field_name, ev.normalized_value)
        previous = breakdowns.get(key)
        if previous is None or (breakdown.value or 0) > (previous.value or 0):
            breakdowns[key] = breakdown
        items.append(
            EvidenceItem(
                field_name=ev.field_name,
                normalized_value=ev.normalized_value,
                # 未校准 -> None -> 权重 0 -> 合并判 INSUFFICIENT。
                # 这是 fail closed 在合并层的落点
                weight=breakdown.value or 0.0,
                source=AttributeSource.EXTRACTED.value,
                media_source=asset.source,
                media_asset_id=str(asset.id),
                evidence_id=str(ev.id),
            )
        )

    merged = merge_all(
        items,
        existing={
            name: (row.normalized_value, row.source) for name, row in existing_map.items()
        },
    )

    written: list[ProductAttributeValue] = []
    for field_name, result in merged.items():
        if result.outcome in (
            MergeOutcome.NO_EVIDENCE,
            MergeOutcome.INSUFFICIENT,
        ):
            # 不写值。证据已经落库了,人可以在识别详情页看到
            logger.info(
                "merge produced no usable value",
                extra={
                    "extra_fields": {
                        "field": field_name,
                        "outcome": result.outcome.value,
                        "reasons": list(result.reasons)[:2],
                    }
                },
            )
            continue
        if result.outcome is MergeOutcome.CONFLICTS_WITH_MANUAL:
            # **不写**。写一条 CONFLICT 会把人工值挤下 is_current,
            # 而"人工值永不被覆盖"要防的正是这个
            logger.warning(
                "merge conflicts with a manual value; keeping the manual one",
                extra={"extra_fields": {"field": field_name}},
            )
            continue

        # 取**胜出值**的因子分解。合并结论和证据不是一一对应的
        breakdown = breakdowns.get((field_name, result.value))
        system_confidence = breakdown.value if breakdown else None
        existing = existing_map.get(field_name)
        status = (
            AttributeStatus.CONFLICT
            if result.outcome is MergeOutcome.CONFLICT
            else decide_status(
                field_name=field_name,
                system_confidence=system_confidence,
                existing=existing,
            )
        )
        # 识别写入同样按注册表分层(A43 / BLOCK-03)。颜色写进变体层,
        # 品类材质写进 SPU 层 —— 和人工确认落在同一个地方,
        # 否则两条路径写出来的值互相看不见
        try:
            owner_type, owner_id = owner_for(field_name, **owner_coordinates(product))
            validate_attribute_value(field_name, result.value)
        except AttributeOwnerError:
            # **坐标算不出来不许被吞。**
            #
            # 下面那条 `logger.warning + continue` 对「值不合契约」是对的,
            # 对这一档是灾难性的:owner 算不出来意味着这件商品的整个
            # VARIANT 层静默消失,而界面显示「识别完成」加一个空的主色 ——
            # 运营没有任何理由怀疑它。§3.22 与 `identity_of()` 里那颗雷
            # 警告的「识别侧静默丢颜色字段」,那个"静默"就在这个 except 上。
            #
            # 抛出去而不是 continue:整次识别当场失败并说出原因。代价是
            # 一批没回填的存量商品跑不了识别 —— **那是对的**,它们本来就
            # 写不出读得回来的颜色属性,跑完只会留下一批查不到的行。
            raise
        except AttributeValueError as exc:
            # **一个字段不合契约,不该让整次识别失败。**
            #
            # 人工那条路必须 422(提交者就在屏幕前,他能改);机器这条路
            # 不行 —— 模型偶尔吐出一个不在枚举里的值是常态,让它炸掉整次识别
            # 意味着另外 7 个正常字段也写不进去,而运营看到的是"识别失败",
            # 完全指不到真正的原因。
            #
            # 跳过 + 记一条 warning:证据已经落库了,人在识别详情页看得到,
            # 只是这个值没有被采信
            logger.warning(
                "merged value rejected by the field contract",
                extra={
                    "extra_fields": {
                        "product_id": str(product.id),
                        "field": field_name,
                        "reason": exc.reason,
                    }
                },
            )
            continue
        written.append(
            set_value(
                session,
                owner_type=owner_type,
                owner_id=owner_id,
                field_name=field_name,
                value=result.value,
                source=AttributeSource.EXTRACTED,
                status=status,
                actor=actor,
                system_confidence=system_confidence,
                breakdown={
                    **(
                        {
                            "calibrated": breakdown.calibrated,
                            "agreement_factor": breakdown.agreement_factor,
                            "quality_factor": breakdown.quality_factor,
                            "visibility_prior": breakdown.visibility_prior,
                            "source_factor": breakdown.source_factor,
                        }
                        if breakdown
                        else {}
                    ),
                    "merge_outcome": result.outcome.value,
                    "ai_divergence": result.ai_divergence,
                    "reasons": list(result.reasons),
                    "rejected_values": list(result.rejected_values),
                },
                evidence_ids=list(result.evidence_ids),
                input_fingerprint=fingerprint_for_owner(
                    prints, owner_type=owner_type, owner_id=owner_id
                ),
            )
        )
        if result.ai_divergence:
            # 生成图改变了商品外观 —— 这是生成链路的质量信号,
            # 不是属性识别的问题。进 Dashboard(§6.2 规则 5)
            logger.warning(
                "AI divergence detected",
                extra={
                    "extra_fields": {
                        "product_id": str(product.id),
                        "field": field_name,
                        "value": result.value,
                    }
                },
            )
    return written


def confirm(
    session: Session,
    *,
    product: Product,
    field_name: str,
    value: Any,
    actor: str,
) -> ProductAttributeValue:
    """人工确认一个值。写入 `MANUAL`,**从此不被任何自动流程覆盖**。

    ## owner 由注册表决定(A43 / BLOCK-03)

    原来这里硬写 `owner_type=OwnerType.SPU, owner_id=str(product.id)`。
    两个字段都不对:`primary_color` / `secondary_colors` 在注册表里声明的是
    `VARIANT`,而 `product.id` 是一个 **SKU 行**的主键,不是 SPU 的标识。
    合起来的实际语义是"每个 Product UUID 一桶属性",分层只存在于契约里。

    现在三个坐标交给 `owner_for()`,由字段自己决定落在哪一层。

    ## 人工确认同样记指纹(§4.5),而且这一条最要紧

    §6.3 的完成条件原文是「必填事实 `CONFIRMED` **且其 `input_fingerprint`
    匹配当前作用域指纹**」。人工确认写的正是 `CONFIRMED`,不记指纹的话
    这条路写出来的事实**永远满足不了完成条件** —— 存量指纹恒为空,
    `facts_stale(stored=None)` 恒为 True,于是运营点一次确认、刷新之后
    那个字段又回到待确认,点多少次都一样。

    这是一个**没有任何地方会报错**的死循环,所以它不许靠"记得传"来避免:
    取数与判定都在这个模块里(`scope_fingerprints_for` /
    `fingerprint_for_owner`),调用点只有这一处和 `apply_evidence`,
    两处都由 `test_a45_batch14_21_facts_stale.py` 用 AST 钉着。
    """
    if not actor or actor == "anonymous":
        raise ValidationError(
            "人工确认必须有已验证的操作者", code=ErrorCode.AUTH_FAILED, http_status=401
        )
    owner_type, owner_id = owner_for(field_name, **owner_coordinates(product))
    prints = scope_fingerprints_for(session, product)
    return set_value(
        session,
        owner_type=owner_type,
        owner_id=owner_id,
        field_name=field_name,
        value=value,
        source=AttributeSource.MANUAL,
        status=AttributeStatus.CONFIRMED,
        actor=actor,
        input_fingerprint=fingerprint_for_owner(
            prints, owner_type=owner_type, owner_id=owner_id
        ),
    )


def owner_coordinates(product: Product) -> dict[str, str]:
    """一件商品的三个 owner 坐标。**变体口径只有一份定义。**

    `variant_id_for()` 住在 `listings/variants.py`,那里同时被图片集的变体
    覆盖校验消费。在这边重写一遍的表现会是"导出按颜色铺了 3 行,
    而属性只写了 1 个变体" —— 两边都不报错,只有平台会。

    ## A44:这个坐标现在是稳定的

    A43 时 `variant_id_for()` 还是 `primary_color or sku`,于是运营改一次
    颜色文案,这里算出来的 `variant_id` 就变了 —— 已确认的颜色属性
    留在旧 owner_id 上,读不到,界面表现是"改了个颜色名,属性不见了",
    而属性侧**没有**图片侧那种 `unknown_tags` 诊断。

    现在它读 `products.variant_key`(0027),创建时分配一次,此后不再推导。
    改名只改名字,不改归属。A43 窗口期里已经被改名甩掉的值追不回来,
    由 `orphaned_variant_owners()` 点名列出,人工决定怎么处理。

    延迟导入:`app.listings` 已经引了 `app.attributes`(见 copy_service),
    模块级反向 import 会成环。
    """
    from app.listings.variants import variant_id_for

    return {
        "spu": product.spu,
        "variant_id": variant_id_for(product),
        "sku": product.sku,
    }


def effective_map(
    session: Session, product_id: UUID, *, product: Product | None = None
) -> dict[str, ProductAttributeValue]:
    """该商品当前的全部属性值,**跨三层合并**(A43 / BLOCK-03)。

    ## 为什么要合并而不是只读一层

    A43 之前所有值都写在 `(SPU, product.id)` 上,读也只读这一处。
    分层落地之后,同一件商品的属性散在三个 owner 上:

        SPU      品类、图案、材质…      owner_id = product.spu
        VARIANT  颜色                   owner_id = variant_id_for(product)
        SKU      (今天没有字段)         owner_id = product.sku
        legacy   A43 之前写的全部值      owner_type=SPU, owner_id=product.id

    最后一行是**存量**。直接改口径会让所有已确认的属性在界面上凭空消失,
    那比不分层更糟。所以这里保留一条回落链:新写入的值优先,读不到时
    回落到旧位置。等属性表迁移过一遍之后可以删掉最后一段 ——
    删的时候要连同 `docs/DECISIONS.md` 里那条一起。

    `product` 省略时退化成只读 legacy 位置:调用方只有一个 UUID 时
    (审计、巡检)给不出 spu/variant 坐标,而那些路径读的本来就是旧数据。
    """
    #: 优先级从低到高 —— 后写进 dict 的覆盖先写进去的
    lookups: list[tuple[str, str]] = [(OwnerType.SPU.value, str(product_id))]
    if product is not None:
        coords = owner_coordinates(product)
        lookups.extend(
            [
                (OwnerType.SKU.value, coords["sku"]),
                (OwnerType.SPU.value, coords["spu"]),
                # VARIANT 的 owner_id 就是 `color_variants.id`(阶段 1)。
                #
                # 这里原来套的是 `<len>:<spu>/<variant_id>` 命名空间。那个
                # hack 的存在理由是"变体 id 取值是颜色名、唯一索引里没有 SPU"
                # —— 换成 UUID 之后理由消失了,跨 SPU 同名在结构上撞不上。
                #
                # **读写必须同源**:写在 `owner_for()`,读在这里。分开写两遍的
                # 表现是"确认成功了,刷新之后值没了",而两边都不报错。
                # 迁移 0046 把存量行从命名空间形式改写成了 UUID,所以这里
                # 不做兼容读 —— 兼容读会让一个没跑迁移的库看起来一切正常,
                # 然后在第一次确认时静静地分裂成两份数据
                (OwnerType.VARIANT.value, coords["variant_id"]),
            ]
        )

    merged: dict[str, ProductAttributeValue] = {}
    for owner_type, owner_id in lookups:
        if not owner_id:
            continue
        rows = session.scalars(
            select(ProductAttributeValue).where(
                ProductAttributeValue.owner_type == owner_type,
                ProductAttributeValue.owner_id == owner_id,
                ProductAttributeValue.is_current.is_(True),
            )
        )
        for row in rows:
            spec = REGISTRY.get(row.field_name)
            # 一个字段只认它在注册表里声明的那一层。少了这一条,
            # 存量的 `(SPU, product.id)` 里那份 primary_color 会盖掉
            # 新写入的变体级值 —— 回落链会变成覆盖链
            if spec is not None and owner_type != OwnerType.SPU.value:
                if spec.owner_type.value != owner_type:
                    continue
            merged[row.field_name] = row
    return merged


def variant_attr_map(
    session: Session, spu: str, variant_ids: Sequence[str]
) -> dict[str, dict[str, ProductAttributeValue]]:
    """一次查完多个变体的颜色层属性(A43 / BLOCK-03)。

    **一条 SQL,不是一个循环。** 多颜色 SPU 的草稿要给每一个变体都装上
    颜色层事实,而"每个变体调一次 `build_canonical`"会把一次库读
    嵌进两层循环里 —— `tests/pure/test_workbench_query_budget.py` 那条棘轮
    正是为这种写法立的,它在 A43 拦下过一次。

    ## spu 是必填的(A44)

    查询要用带命名空间的 owner_id(见 `validation.variant_owner_id`),
    而命名空间要 SPU。原来这个参数不存在,于是它查的是裸变体 id ——
    也就是**跨 SPU 共用**的那一份,同名颜色的两个 SPU 会互相读到对方的值。

    进出参数都用**裸**变体 id:命名空间是存储细节,调用方(草稿组装)
    手上只有 `sku.variant_id`,让它自己拼一遍就等于把这个细节复制出去。

    返回 `{variant_id: {field_name: row}}`。查不到的变体不出现在结果里,
    调用方按空字典处理:一个还没确认过颜色的变体是合法状态,不是错误。
    """
    wanted = [v for v in dict.fromkeys(variant_ids) if v]
    if not wanted or not (spu or "").strip():
        return {}
    # owner_id 就是变体 UUID 本身(阶段 1,命名空间 hack 已退役)。
    # 这个 dict 从"带命名空间 -> 裸 id"退化成恒等映射,保留它是因为
    # 下面按 `namespaced[row.owner_id]` 回折 —— 改成直接用 owner_id
    # 会让"查不到的变体不出现在结果里"这条性质失去一个显式的落点
    namespaced = {v: v for v in wanted}
    rows = session.scalars(
        select(ProductAttributeValue).where(
            ProductAttributeValue.owner_type == OwnerType.VARIANT.value,
            ProductAttributeValue.owner_id.in_(list(namespaced)),
            ProductAttributeValue.is_current.is_(True),
        )
    )
    out: dict[str, dict[str, ProductAttributeValue]] = {}
    for row in rows:
        spec = REGISTRY.get(row.field_name)
        # 注册表里不属于变体层的字段即使被写在这里也不认 —— 读取侧
        # 只承认一个字段声明的那一层
        if spec is not None and spec.owner_type is not OwnerType.VARIANT:
            continue
        bare = namespaced.get(row.owner_id)
        if bare is None:
            continue
        out.setdefault(bare, {})[row.field_name] = row
    return out


def orphaned_variant_owners(
    session: Session, spu: str
) -> dict[str, list[str]]:
    """这个 SPU 名下\"挂空\"的变体属性行。**只读诊断,不动数据**(A44)。

    A44 之前变体归属挂在 `primary_color or sku` 上,运营改一次颜色名,
    已确认的颜色属性就留在旧 owner_id 上读不出来 —— 而属性侧没有图片侧
    那种 `unknown_tags` 能让人看见(DECISIONS §3.17)。

    A44 之后不会再产生新的这种行,但**窗口期里已经产生的追不回来**:
    库里只剩一个字符串,没有任何地方记着它当初属于哪一件商品。
    所以这里只做一件事 —— 把它们列出来,让人工决定是重新确认一次还是删掉。

        namespaced_orphans   带本 SPU 命名空间、但变体已经不存在了
        legacy_bare          A44 之前写下的裸 owner_id(拆不出 SPU)。
                             **它们可能属于任何一个 SPU** —— 这正是
                             `variant_owner_id()` 要消灭的那个跨 SPU 串档,
                             所以这里不猜归属,只报告存在

    不提供 `--apply`:重指归属要靠人判断\"这个黑色是哪个 SPU 的黑色\",
    而本轮拿不到真库,一个能改数据的脚本不该在这种状态下交出去。
    """
    from app.listings import variants as variant_service

    # 活变体的 owner_id 现在就是它们的 UUID(阶段 1)
    live = set(variant_service.required_variant_ids(session, spu))
    prefix = validation.legacy_variant_owner_prefix(spu)
    rows = session.scalars(
        select(ProductAttributeValue.owner_id).where(
            ProductAttributeValue.owner_type == OwnerType.VARIANT.value,
            ProductAttributeValue.is_current.is_(True),
        )
    )
    namespaced_orphans: set[str] = set()
    legacy_bare: set[str] = set()
    for owner_id in rows:
        parsed = validation.split_variant_owner_id(owner_id)
        if parsed is None:
            legacy_bare.add(owner_id)
            continue
        if owner_id.startswith(prefix) and owner_id not in live:
            namespaced_orphans.add(owner_id)
    return {
        "namespaced_orphans": sorted(namespaced_orphans),
        "legacy_bare": sorted(legacy_bare),
    }


def attr_values_of(rows: Mapping[str, ProductAttributeValue]) -> dict:
    """把一批 ORM 行折成契约里的 `AttrValue`。给组装方用,免得它去碰 `_attr_value`。"""
    return {name: _attr_value(row) for name, row in rows.items()}


def get_extraction(session: Session, extraction_id: UUID) -> ProductAttributeExtraction:
    row = session.get(ProductAttributeExtraction, extraction_id)
    if row is None:
        raise NotFoundError("识别记录不存在")
    return row


# ------------------------------------------------------------------ 对外读取
#
# **所有业务读属性都该走这两个函数**,包括生成链路(§5.4)。
# 直接读 `products` 的 8 个外观列是兼容路径,M4 之后逐步下线 ——
# 那些列是只读投影,事实源在 product_attribute_values。


def get_effective_value(
    session: Session,
    *,
    owner_type: OwnerType,
    owner_id: str,
    field: str,
    fallback_to_spu: bool = True,
) -> ProductAttributeValue | None:
    """取一个字段的当前采信值。

    ``fallback_to_spu``:VARIANT/SKU 层没有专属值时回落到 SPU 层。
    品类、材质挂在 SPU 上,颜色挂在变体上 —— 查颜色时不该因为
    SPU 层没有它就返回空。
    """
    row = current_value(
        session, owner_type=owner_type, owner_id=owner_id, field_name=field
    )
    if row is not None or not fallback_to_spu or owner_type is OwnerType.SPU:
        return row
    return current_value(
        session, owner_type=OwnerType.SPU, owner_id=owner_id, field_name=field
    )


def _attr_value(row: ProductAttributeValue):
    from app.attributes.contracts import AttrValue
    from app.core.enums import AttributeSource as _Src
    from app.core.enums import AttributeStatus as _St

    # 这里原来有一行 `breakdown = row.confidence_breakdown or {}`,算出来之后
    # **没有传给 AttrValue** —— 而 AttrValue 上确实有 `breakdown` 字段。
    # 也就是说置信度分解从来没有到达过下游。
    #
    # Phase 0 只删掉这行死代码,不顺手把它接上:接上会改变属性层对外的语义,
    # 而那属于 Phase 1「证据和置信度」的范围,应该连同 schema 和前端展示一起做。
    # 已记入交接文档的遗留清单。
    return AttrValue(
        field_name=row.field_name,
        value=row.value_json,
        source=_Src(row.source),
        status=_St(row.status),
        owner_type=OwnerType(row.owner_type),
        owner_id=row.owner_id,
        normalized_value=row.normalized_value,
        model_confidence=float(row.model_confidence) if row.model_confidence else None,
        system_confidence=float(row.system_confidence) if row.system_confidence else None,
        evidence_ids=tuple(row.evidence_ids or []),
        calibration_version=row.calibration_version,
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
        version_id=str(row.id),
    )


def build_canonical(session: Session, product: Product):
    """组装 `CanonicalProduct`(§5.5)。

    这是识别层与渠道层之间**唯一**的商品契约。渠道适配器只接受它,
    不得直接查数据库 —— 那条限制是"映射逻辑必须可穷举测试"的前提。

    M4 的版本只有 SPU 层。VARIANT/SKU 层要等 `products` 加 `size` /
    `size_group` 列(迁移 0015),在那之前硬造一个变体结构只会让
    下游按一个将来必然要改的形状写代码。
    """
    from app.attributes.contracts import CanonicalProduct, CanonicalVariant
    from app.core.enums import MediaRole, MediaSource
    from app.media.contracts import MediaRef

    values = effective_map(session, product.id, product=product)
    media = tuple(
        MediaRef(
            media_asset_id=str(a.id),
            role=MediaRole(a.role) if a.role else None,
            source=MediaSource(a.source),
            storage_path=a.storage_path,
            width=a.width,
            height=a.height,
            variant_hint=a.variant_hint,
        )
        for a in media_service.usable_assets(session, product.id)
    )
    # 按注册表把值分回它该待的那一层(A43 / BLOCK-03)。
    #
    # 原来这里把**全部**值塞进 `spu_attrs`,于是 `CanonicalVariant.attrs`
    # 这个契约虽然存在、渠道映射也会读它,却永远是空的 —— 属性分层名义上
    # 分成了 SPU / VARIANT / SKU,实际运行仍是"一桶"。
    spu_attrs: dict = {}
    variant_attrs: dict = {}
    for name, row in values.items():
        spec = REGISTRY.get(name)
        if spec is not None and spec.owner_type is OwnerType.VARIANT:
            variant_attrs[name] = _attr_value(row)
        else:
            spu_attrs[name] = _attr_value(row)

    from app.listings.variants import variant_id_for

    variants = (
        (CanonicalVariant(variant_id=variant_id_for(product), attrs=variant_attrs),)
        if variant_attrs
        else ()
    )
    return CanonicalProduct(
        spu=product.spu,
        category_path=(product.category,) if product.category else (),
        spu_attrs=spu_attrs,
        variants=variants,
        media=media,
    )


def metadata_for_evaluation(session: Session, product: Product) -> dict[str, Any]:
    """交给评分器的商品属性(§5.4 的读取统一)。

    以前直接读 `products` 的 8 个外观列。那些列现在是**只读投影**,
    事实源在属性表 —— 而两者会在识别写入与人工确认之间短暂不一致,
    投影巡检修好之前评分器读到的就是旧值。

    只取 `CONFIRMED` 的值:评分器拿一个还没人看过的模型猜测去判断
    "生成的还是不是这件商品",等于用一个不确定的输入去验证另一个。
    没有确认值时回落到投影列 —— 存量商品的属性还没跑过识别。
    """
    values = effective_map(session, product.id, product=product)
    result: dict[str, Any] = {
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "complexity": product.complexity,
    }
    for field_name, spec in REGISTRY.items():
        if not spec.legacy_column:
            continue
        row = values.get(field_name)
        if row is not None and row.status == AttributeStatus.CONFIRMED.value:
            result[field_name] = row.value_json
        else:
            result[field_name] = getattr(product, spec.legacy_column, None)
    result.setdefault("secondary_colors", list(product.secondary_colors or []))
    return result


# ------------------------------------------------------------------ 投影巡检


def projection_mismatches(
    session: Session, *, limit: int = 500
) -> tuple[list[dict[str, Any]], int]:
    """比对事实源与 `products` 投影列(§5.4 第二条防线)。

    返回 `(不一致清单, 真正检查了几件)`,**不自动修复** —— 和素材层的
    双读对账同一个理由:这一步的目的是发现,自动改数据会掩盖
    "为什么会不一致"这个问题本身。修复走 `repair_projections()`,
    那是一个显式动作。

    第二个返回值是接口层的分母(报告 6.6):库里商品不足 `limit` 时,
    "检查了 limit 件"是一句假话,而巡检报告的价值全在分母可信。
    """
    from app.models.product import Product as _P

    found: list[dict[str, Any]] = []
    products = list(session.scalars(select(_P).limit(limit)))
    for product in products:
        values = effective_map(session, product.id, product=product)
        for field_name, spec in REGISTRY.items():
            if not spec.legacy_column:
                continue
            row = values.get(field_name)
            if row is None:
                continue
            projected = getattr(product, spec.legacy_column, None)
            if _same(row.value_json, projected):
                continue
            found.append(
                {
                    "product_id": str(product.id),
                    "field": field_name,
                    "column": spec.legacy_column,
                    "fact": row.value_json,
                    "projection": projected,
                }
            )
    return found, len(products)


def _same(fact: Any, projected: Any) -> bool:
    """空字符串与 None 视作同一件事 —— 投影列多数 NOT NULL DEFAULT ''。"""
    if isinstance(fact, list) or isinstance(projected, list):
        return list(fact or []) == list(projected or [])
    return (str(fact) if fact is not None else "") == (
        str(projected) if projected is not None else ""
    )


def repair_projections(session: Session, *, limit: int = 500) -> int:
    """按事实源修复投影列。返回修复条数。

    **以事实源为准**,不是反过来 —— 投影列没有历史、没有来源、没有置信度,
    拿它去覆盖属性表等于把一次人工确认的记录抹掉。
    """
    from app.models.product import Product as _P

    fixed = 0
    mismatches, _checked = projection_mismatches(session, limit=limit)
    for item in mismatches:
        product = session.get(_P, UUID(item["product_id"]))
        if product is None:
            continue
        setattr(product, item["column"], item["fact"] if item["fact"] is not None else "")
        fixed += 1
    if fixed:
        session.flush()
        logger.warning(
            "repaired attribute projections",
            extra={"extra_fields": {"count": fixed}},
        )
    return fixed


# ------------------------------------------------------------------ 指标


def confirmation_metrics(session: Session, *, days: int = 30) -> dict[str, Any]:
    """自动确认率与人工推翻率(M4 出口条件)。

    **推翻率 > 2% 就要收紧阈值**(§6.5)。没有这个数的话,阈值定得对不对
    没有任何反馈 —— 而阈值是唯一决定"模型的猜测会不会直接上架"的东西。

    推翻的判据:一条 `CONFIRMED + EXTRACTED` 的值后来被一条 `MANUAL` 值取代,
    且取代它的值不同。只看"有没有被人工改过"会把补充录入也算进去。
    """
    from datetime import timedelta

    from sqlalchemy import func

    cutoff = utc_now() - timedelta(days=max(1, days))

    def _count(*where) -> int:
        stmt = select(func.count()).select_from(ProductAttributeValue)
        for clause in where:
            stmt = stmt.where(clause)
        return session.scalar(stmt) or 0

    auto_confirmed = _count(
        ProductAttributeValue.created_at >= cutoff,
        ProductAttributeValue.source == AttributeSource.EXTRACTED.value,
        ProductAttributeValue.status == AttributeStatus.CONFIRMED.value,
    )
    extracted_total = _count(
        ProductAttributeValue.created_at >= cutoff,
        ProductAttributeValue.source == AttributeSource.EXTRACTED.value,
    )

    # 被人工推翻的:同一 (owner, field) 上,一条已失效的自动确认值
    # 后面跟着一条值不同的人工值
    overturned = 0
    superseded = session.scalars(
        select(ProductAttributeValue).where(
            ProductAttributeValue.created_at >= cutoff,
            ProductAttributeValue.is_current.is_(False),
            ProductAttributeValue.source == AttributeSource.EXTRACTED.value,
            ProductAttributeValue.status == AttributeStatus.CONFIRMED.value,
        )
    )
    for old in superseded:
        replacement = current_value(
            session,
            owner_type=OwnerType(old.owner_type),
            owner_id=old.owner_id,
            field_name=old.field_name,
        )
        if (
            replacement is not None
            and replacement.source == AttributeSource.MANUAL.value
            and replacement.normalized_value != old.normalized_value
        ):
            overturned += 1

    rate = round(overturned / auto_confirmed, 4) if auto_confirmed else 0.0
    return {
        "window_days": days,
        "extracted_values": extracted_total,
        "auto_confirmed": auto_confirmed,
        "auto_confirm_rate": round(auto_confirmed / extracted_total, 4)
        if extracted_total
        else 0.0,
        "overturned": overturned,
        "overturn_rate": rate,
        # 超过 2% 就该收紧阈值 —— 把判据写进返回值,不让读的人自己记
        "overturn_alert": rate > 0.02,
    }
