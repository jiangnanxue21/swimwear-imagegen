"""文案编排(M6,§8.2)。

**按 `(channel, site, locale)` 独立生成、独立校验、独立重试。**

多语言一致性靠共享同一份 `ContentPlan.facts`,不是靠一次调用同时出多语言。
失败隔离:`en` 通过、`pt-BR` 未通过时,只重试 `pt-BR`,而 `en` 已经可以导出。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.attributes import service as attr_service
from app.core.clock import utc_now
from app.core.enums import AttributeStatus, AuditAction, CopyStatus
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db import locks
from app.listings import content_plan as plan_rules
from app.listings import copy_rules
from app.listings.contracts import ContentPlanData
from app.models.listing_copy import ContentPlan, ListingCopy
from app.models.product import Product
from app.services import audit

logger = get_logger(__name__)

#: 定向重写的轮次上限(§8.4)。超限转人工。
#:
#: 两轮不是随便定的:第一轮改的是模型没看懂规则,第二轮改的是它看懂了
#: 但做不到。第三轮通常只是在同一个问题上换说法 —— 那时候该让人看了。
MAX_REPAIR_ROUNDS = 2


def build_content_plan(
    session: Session, *, product: Product, actor: str = "system", **kwargs
) -> ContentPlan:
    """组装内容计划。**facts 只取 CONFIRMED 的属性。**

    用 SUGGESTED 的值生成文案,等于把一个还没人看过的模型猜测
    写进面向消费者的商品描述里。
    """
    data = content_plan_data(session, product=product, **kwargs)
    row = ContentPlan(
        spu=data.spu,
        facts=dict(data.facts),
        selling_points=list(data.selling_points),
        forbidden_claims=list(data.forbidden_claims),
        facts_version=data.facts_version,
        attr_snapshot_ids=list(data.attr_snapshot_ids),
    )
    session.add(row)
    session.flush()
    audit.record(
        session, actor=actor, action=AuditAction.CREATE,
        entity_type="ContentPlan", entity_id=row.id,
        payload={"spu": data.spu, "facts": len(data.facts)},
    )
    return row


def content_plan_data(
    session: Session, *, product: Product, **kwargs
) -> ContentPlanData:
    """组装但不落库的事实计划,供独立能力测试复用生产口径。"""
    values = attr_service.effective_map(session, product.id, product=product)
    confirmed = {
        name: row.value_json
        for name, row in values.items()
        if row.status == AttributeStatus.CONFIRMED.value
    }
    version_ids = [
        str(row.id)
        for row in values.values()
        if row.status == AttributeStatus.CONFIRMED.value
    ]
    data = plan_rules.build_plan(
        spu=product.spu,
        confirmed=confirmed,
        attr_version_ids=version_ids,
        **kwargs,
    )
    return data


def _next_version(
    session: Session,
    *,
    spu: str,
    channel: str,
    site: str,
    locale: str,
    color_variant_id: str,
) -> int:
    """下一个版本号。**必须在拿到 advisory lock 之后调用。**

    上一版是"把这个 scope 的所有行查出来取 max + 1",没有任何锁。两个
    并发请求会算出同一个版本号,后提交的那个撞 `uq_listing_copies_version`,
    用户看到的是一个 500 —— 而他做的事情完全正当,只是同事恰好也点了生成。

    版本序列按 (scope, 颜色) 分槽(迁移 0051):红色的第 3 版和蓝色的
    第 3 版是两条互不相干的历史。不带颜色过滤的话两个颜色共用一条递增
    序列 —— 数字仍然唯一,但「这个颜色改到第几版了」再也答不出来,
    且蓝色第一次生成会把红色那一版「挤成历史」的旧病换个形状回来。
    """
    current = session.execute(
        select(func.max(ListingCopy.version)).where(
            ListingCopy.spu == spu,
            ListingCopy.channel == channel,
            ListingCopy.site == site,
            ListingCopy.locale == locale,
            ListingCopy.color_variant_id == color_variant_id,
        )
    ).scalar_one_or_none()
    return (current or 0) + 1


def resolve_rules(channel: str, site: str) -> copy_rules.CopyRules:
    """当前生效的渠道文案规则。**审批时要重新读一遍。**

    现在返回默认规则。这个函数存在的意义是给"规则从哪来"留一个唯一入口 ——
    将来接渠道 spec 时只改这里,而不是在生成、校验、审批三处各查一次,
    然后在某一次改动之后三处读到不同的版本。
    """
    return copy_rules.CopyRules()


def save_copy(
    session: Session,
    *,
    plan: ContentPlan,
    channel: str,
    site: str,
    locale: str,
    copy: dict[str, Any],
    claims: Sequence[dict[str, Any]],
    rules: copy_rules.CopyRules,
    model_name: str | None = None,
    prompt_version: str | None = None,
    repair_rounds: int = 0,
    synonyms: dict[str, str] | None = None,
    idempotency_key: str | None = None,
    color_variant_id: str = "",
    actor: str = "system",
) -> ListingCopy:
    """落一版文案并**立刻校验**。

    校验结果落库(`violations`),状态按有没有硬失败定。不校验就落库的话,
    「这一版为什么没通过」要等到导出时才知道,而那时候上下文已经没了。

    结构不合格的输出也落库(经 `sanitize_*` 折成安全形态),状态是
    `REJECTED`。丢掉的话运营只能看到"生成失败",看不到模型到底吐了什么。

    `idempotency_key`(§4.9 / AC-11):这一版属于哪个文案生成幂等单元。
    由调用方(`workbench/service.generate_copy`)算好传进来,见下面那段注释。
    **`REJECTED` 的行也带着键落库**,而复用口径把失败态排除在外 ——
    两者的分工是:列如实记录"这一版是为哪个单元生成的",
    判定层决定"哪些状态可以拿来复用"。合成一件事的写法(失败就不写键)
    会让"这个单元试过没有"查不出来。

    `color_variant_id`(AC-11 第二句,迁移 0051):这一版属于哪个颜色,
    取值是变体身份串(`variants.variant_id_for()` 的产出)。默认 `""`
    只是迁移前形状的镜像,**生产入口一律显式传** —— batch24 的守卫钉着
    `generate_copy` / `save_copy_manual` 两处都把它接到了商品的变体身份上。
    """
    violations = copy_rules.validate_copy(
        copy,
        claims,
        plan.facts,
        rules,
        forbidden_claims=plan.forbidden_claims,
        synonyms=synonyms or {},
    )
    hard = copy_rules.blocking(violations)

    safe_copy = copy_rules.sanitize_copy(copy)
    safe_claims = copy_rules.sanitize_claims(claims)

    # 锁 -> 算版本号 -> 插入,整段对同一个 (spu, channel, site, locale, 颜色)
    # 串行。颜色进锁键(迁移 0051):版本序列按颜色分槽之后,红蓝两个颜色
    # 各自的插入互不相干,让它们互相排队只是白等
    locks.advisory_xact_lock(
        session, "listing_copy", plan.spu, channel, site, locale, color_variant_id
    )

    row: ListingCopy | None = None
    last_error: IntegrityError | None = None
    for _ in range(locks.MAX_LOCK_RETRIES):
        # 保存点必须建在 session.add 之前。SQLAlchemy 在开启嵌套事务时会先把
        # 待处理对象自动 flush 出去,于是唯一约束异常发生在 SAVEPOINT 建立
        # **之前** —— 外层事务当场进入失败状态,重试作用在一个已经不能再执行
        # 任何语句的会话上。这个坑 prompt_service 踩过一次,不必再踩第二次
        try:
            with session.begin_nested():
                row = ListingCopy(
                    spu=plan.spu,
                    channel=channel,
                    site=site,
                    locale=locale,
                    color_variant_id=color_variant_id,
                    version=_next_version(
                        session,
                        spu=plan.spu,
                        channel=channel,
                        site=site,
                        locale=locale,
                        color_variant_id=color_variant_id,
                    ),
                    content_plan_id=plan.id,
                    title=safe_copy["title"],
                    bullet_points=safe_copy["bullet_points"],
                    description=safe_copy["description"],
                    keywords=safe_copy["keywords"],
                    extra=safe_copy["extra"],
                    claims=safe_claims,
                    status=(CopyStatus.REJECTED if hard else CopyStatus.VALIDATED).value,
                    violations=_serialize(violations),
                    model_name=model_name,
                    prompt_version=prompt_version,
                    repair_rounds=repair_rounds,
                    spec_version=rules.spec_version,
                    # §4.9 / AC-11。**不传就留 NULL,不在这里现算一个** ——
                    # 现算的话这一层要知道"已确认事实版本集"是什么,
                    # 而那是调用方刚刚算过的东西;算第二次就是第二个答案,
                    # 表现是刚生成的那一版立刻判"不命中"、下次点生成又付一次费
                    idempotency_key=idempotency_key,
                )
                session.add(row)
                session.flush()
            break
        except IntegrityError as exc:
            # 走到这里说明有进程绕过了 advisory lock(迁移脚本、手工 SQL)。
            # 重新算一次版本号再试,而不是把 500 扔给用户
            last_error = exc
            row = None
            logger.warning(
                "listing copy version collided, retrying",
                extra={
                    "extra_fields": {
                        "event": "listing.copy_version_collision",
                        "spu": plan.spu,
                        "locale": locale,
                    }
                },
            )
    if row is None:
        raise ValidationError(
            f"文案版本连续 {locks.MAX_LOCK_RETRIES} 次冲突,请稍后重试",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        ) from last_error

    # 审计:这一版由谁、用哪个模型和哪一版提示词生成,规则版本是多少。
    #
    # 上一版 `actor` 是个没被用到的形参(评审第 12 条),于是"这条文案
    # 是谁生成的"在库里没有任何记录 —— 而文案是要对外发布的东西,
    # 出问题时第一个要回答的就是这个。
    #
    # **只记元数据,不记正文**:正文已经在 listing_copies 里了,
    # 复制一份进审计表既没有新信息,又多一处要脱敏的地方
    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="ListingCopy",
        entity_id=row.id,
        payload={
            "spu": plan.spu,
            "channel": channel,
            "site": site,
            "locale": locale,
            "color_variant_id": color_variant_id,
            "version": row.version,
            "status": row.status,
            "model_name": model_name,
            "prompt_version": prompt_version,
            "spec_version": rules.spec_version,
            "repair_rounds": repair_rounds,
            "violation_codes": [v.code.value for v in violations][:10],
            "blocking": len(hard),
        },
    )

    if hard:
        logger.warning(
            "copy rejected by validation",
            extra={
                "extra_fields": {"event": "listing.copy_validation_failed",
                    "spu": plan.spu,
                    "locale": locale,
                    "codes": [v.code.value for v in hard][:5],
                }
            },
        )
    return row


def _serialize(violations: Sequence[copy_rules.CopyViolation]) -> list[dict[str, Any]]:
    return [
        {
            "code": v.code.value,
            "message": v.message,
            "location": v.location,
            "level": "error" if v.is_blocking else "warning",
        }
        for v in violations
    ]


def revalidate(
    session: Session,
    row: ListingCopy,
    *,
    rules: copy_rules.CopyRules,
    synonyms: dict[str, str] | None = None,
) -> list[copy_rules.CopyViolation]:
    """按**当前**规则重跑一遍校验,结果写回这一行。

    审批前必须做一次:文案是在某一版规则下通过的,规则换版之后
    那个结论就不再代表"现在能不能发"。
    """
    plan = session.get(ContentPlan, row.content_plan_id)
    if plan is None:
        raise NotFoundError("文案对应的内容计划不存在,无法重新校验")

    violations = copy_rules.validate_copy(
        {
            "title": row.title,
            "description": row.description,
            "bullet_points": list(row.bullet_points or []),
            "keywords": list(row.keywords or []),
            "extra": dict(row.extra or {}),
        },
        list(row.claims or []),
        plan.facts,
        rules,
        forbidden_claims=plan.forbidden_claims,
        synonyms=synonyms or {},
    )
    row.violations = _serialize(violations)
    row.spec_version = rules.spec_version
    row.status = (
        CopyStatus.REJECTED if copy_rules.blocking(violations) else CopyStatus.VALIDATED
    ).value
    session.flush()
    return violations


def _load_for_spu(session: Session, copy_id: UUID, expect_spu: str | None) -> ListingCopy:
    """取一版文案,并校验它确实属于调用方说的那个 SPU。

    ## 为什么这道守卫在服务层

    A1 修的就是同一类缺陷:`resolve_rejection` 用全局 id 取驳回记录,
    于是拿商品 A 的 id 去请求商品 B 的接口能同时改到两件商品。文案接口
    的形状一模一样 —— `/products/{product_id}/copy/{copy_id}/approve`
    里 `product_id` 只用来确认商品存在,`copy_id` 是全局取的,
    **两个参数从不互相印证**:用 A 的 copy_id 请求 B 的路径,批准的是 A 的文案,
    而审计和界面都会记成 B。

    404 而不是 403:调用方对这条路径下的这个 id 来说,它就是不存在。
    """
    row = session.get(ListingCopy, copy_id)
    if row is None:
        raise NotFoundError("文案不存在")
    if expect_spu is not None and row.spu != expect_spu:
        raise NotFoundError("这一版文案不属于该商品")
    return row


def approve_copy(
    session: Session,
    copy_id: UUID,
    *,
    actor: str,
    expect_spu: str | None = None,
    rules: copy_rules.CopyRules | None = None,
    synonyms: dict[str, str] | None = None,
) -> ListingCopy:
    """批准一版文案。**有硬失败就不许批准。**

    ## 三道闸,缺一不可(评审第 8 条)

    上一版只读历史落库的 `violations`,于是三条路都能走通:

        状态不限   一版 DRAFT(还没校验过,violations 是空的)可以直接批准
        规则不查   规则收紧之后,旧文案带着旧结论原样获批
        不重跑     文案本身没变,但它依据的 facts 可能已经变了

    现在:状态必须是 `VALIDATED`;`spec_version` 和当前规则不一致就在
    审批事务内重跑校验并把新结论落库;最后才看有没有硬失败。

    重跑的结果会覆盖 `violations` 与 `status` —— 批不批得成另说,
    「按今天的规则这一版是什么结论」必须留在库里。
    """
    row = _load_for_spu(session, copy_id, expect_spu)

    if row.status != CopyStatus.VALIDATED.value:
        raise ValidationError(
            f"只有校验通过(VALIDATED)的文案可以批准,当前状态 {row.status}",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    current = rules or resolve_rules(row.channel, row.site)
    revalidated = False
    if row.spec_version != current.spec_version:
        logger.info(
            "copy rules changed since this version was generated, revalidating",
            extra={
                "extra_fields": {"event": "listing.copy_rules_changed",
                    "copy_id": str(row.id),
                    "saved_spec": row.spec_version,
                    "current_spec": current.spec_version,
                }
            },
        )
        revalidate(session, row, rules=current, synonyms=synonyms)
        revalidated = True

    hard = [v for v in (row.violations or []) if v.get("level") == "error"]
    if hard:
        raise ValidationError(
            "这一版文案有未解决的硬失败,不能批准:"
            + ";".join(v.get("code", "") for v in hard[:3]),
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    row.status = CopyStatus.APPROVED.value
    row.approved_by = actor
    row.approved_at = utc_now()
    session.flush()
    audit.record(
        session, actor=actor, action=AuditAction.UPDATE,
        entity_type="ListingCopy", entity_id=row.id,
        payload={
            "action": "approve",
            "locale": row.locale,
            "version": row.version,
            "spec_version": current.spec_version,
            "revalidated": revalidated,
        },
    )
    return row


def reject_copy(
    session: Session,
    copy_id: UUID,
    *,
    reason: str,
    note: str | None,
    actor: str,
    expect_spu: str | None = None,
) -> ListingCopy:
    """快审退回一版文案(A10)。

    ## 与"校验没过"共用一个状态,但不是一回事

    `CopyStatus.REJECTED` 本来就有人用:`save_copy` / `revalidate` 在有硬失败时
    也置这个值。所以退回**必须同时写 `reject_reason`** —— 判定层是靠它区分
    "禁词扫出来的"和"人看完退的"(见 `flow._evaluate_copy`),
    两者的下一步文案完全不同,而只看状态会让规则驳回的那一版
    在界面上被说成"快审退回:未注明原因"。

    ## 退回之后它不会自己走回来

    改文案走 `save_copy`,每次落的是**新版本行**,被退回的那一版原样留在库里;
    `approve_copy` 又要求状态是 VALIDATED。于是这一版既不会被原地改回去,
    也不会被直接批准 —— 除非有人真的改出新版本,它就停在退回状态。
    """
    from app.workbench import reject as reject_rules

    row = _load_for_spu(session, copy_id, expect_spu)

    if row.status == CopyStatus.APPROVED.value:
        raise ValidationError(
            "这一版文案已经批准,不能退回;要改内容请编辑并保存为新版本",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    if row.status == CopyStatus.ARCHIVED.value:
        raise ValidationError(
            "已归档的文案不能退回",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    try:
        checked = reject_rules.validate_reject("copy", reason, note)
    except ValueError as exc:
        raise ValidationError(
            str(exc), code=ErrorCode.INPUT_INVALID, http_status=422
        ) from exc

    row.status = CopyStatus.REJECTED.value
    row.reject_reason = checked.reason.value
    row.reject_note = checked.note
    row.rejected_by = actor
    row.rejected_at = utc_now()
    session.flush()

    audit.record(
        session, actor=actor, action=AuditAction.UPDATE,
        entity_type="ListingCopy", entity_id=row.id,
        payload={
            "action": "reject",
            "locale": row.locale,
            "version": row.version,
            "reason": checked.reason.value,
            "note": checked.note,
        },
    )
    logger.info(
        "listing copy rejected in quick review",
        extra={
            "extra_fields": {"event": "listing.copy_rejected_quick_review",
                "spu": row.spu,
                "locale": row.locale,
                "reason": checked.reason.value,
            }
        },
    )
    return row


def latest_approved(
    session: Session,
    *,
    spu: str,
    channel: str,
    site: str,
    locale: str,
    color_variant_id: str,
) -> ListingCopy | None:
    """发布时用哪一版。**只返回已批准的、且属于这个颜色的。**

    颜色过滤是等值,不做回落:存量行的哨兵 `""` 因此永远不命中 ——
    「这个颜色还没有已批准文案」正是它该给的答案。把 SPU 槽时代的旧文案
    (可能写着另一个颜色的名字)顶给这个颜色,才是要防的那件事
    (0051 文件头的回填一节)。
    """
    rows = session.scalars(
        select(ListingCopy).where(
            ListingCopy.spu == spu,
            ListingCopy.channel == channel,
            ListingCopy.site == site,
            ListingCopy.locale == locale,
            ListingCopy.color_variant_id == color_variant_id,
            ListingCopy.status == CopyStatus.APPROVED.value,
        )
    )

    ordered = sorted(rows, key=lambda r: r.version, reverse=True)
    return ordered[0] if ordered else None


def needs_manual_review(row: ListingCopy) -> bool:
    """重写轮次用尽仍未通过 -> 转人工(§8.4)。"""
    return row.repair_rounds >= MAX_REPAIR_ROUNDS and row.status == CopyStatus.REJECTED.value
