"""平台侧操作 service(OPS-REVIEW P1:驳回回流)。

这里是唯一能写 `listing_drafts.platform_status` 与 `platform_rejections` 的地方。

分工与 service.py 相同:
    platform.py           纯函数判定(哪些转移合法、如何定位导出版本)
    platform_service.py   查库、写库、落审计
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    UNRESOLVED_REJECTION_STATUSES,
    AuditAction,
    PlatformStatus,
    PublishAttemptStatus,
    RejectionStatus,
)
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.audit_log import AuditLog
from app.models.listing_copy import ListingDraft
from app.models.platform_rejection import PlatformRejection
from app.models.product import Product
from app.models.publishing import ChannelListing, PublishAttempt
from app.services import audit
from app.workbench import platform as pf

logger = get_logger(__name__)

# ==========================================================
# 读取
# ==========================================================


def open_rejections(session: Session, draft_id: UUID) -> list[PlatformRejection]:
    """该草稿行的**未解决**驳回记录。flow 层通过计数决定是否阻断。

    评审第 7 条:判据是 `UNRESOLVED_REJECTION_STATUSES`,不是 `== OPEN`。
    `FIXED_PENDING_EXPORT`(运营声明改过了、但系统还没看到新导出)同样算
    未解决 —— 少了它,点一次"我改好了"就能让商品从阻断列表里消失,
    而那正是原来勾选框的问题,只是换了个状态名。
    """
    return list(
        session.scalars(
            select(PlatformRejection)
            .where(
                PlatformRejection.draft_id == draft_id,
                PlatformRejection.status.in_(list(UNRESOLVED_REJECTION_STATUSES)),
            )
            .order_by(PlatformRejection.created_at.desc())
        )
    )


def all_rejections(session: Session, draft_id: UUID) -> list[PlatformRejection]:
    """所有驳回记录(含已解决),供导出页展示历史。"""
    return list(
        session.scalars(
            select(PlatformRejection)
            .where(PlatformRejection.draft_id == draft_id)
            .order_by(PlatformRejection.created_at.desc())
        )
    )


def _export_audit_entries(session: Session, draft_id: UUID) -> list[dict[str, Any]]:
    """取导出审计,新在前,带 payload 里的 fingerprint / components。"""
    rows = list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "ListingDraft",
                AuditLog.entity_id == draft_id,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(50)
        )
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        # 解析交给 platform.export_entry —— 它和写方 export_audit_payload 同处一个
        # 文件,两边的键名不可能各自漂移(那次漂移让 audit 定位静默失效过一轮)
        entry = pf.export_entry(
            row.payload, row.created_at.isoformat() if row.created_at else None
        )
        if entry is not None:
            out.append(entry)
    return out


# ==========================================================
# 平台状态更新
# ==========================================================


def update_platform_status(
    session: Session,
    draft: ListingDraft,
    *,
    to_status: str,
    actor: str,
) -> ListingDraft:
    """手工更新平台侧状态。允许的转移由 `platform.manual_targets` 决定。

    REJECTED 不能手动设置:它只能由 `record_rejection` 写入。
    """
    open_count = len(open_rejections(session, draft.id))
    allowed = pf.manual_targets(draft.platform_status, open_rejections=open_count)
    try:
        target = PlatformStatus(to_status)
    except ValueError as exc:
        raise ValidationError(
            f"未知的平台状态 {to_status!r}",
            code=ErrorCode.INPUT_INVALID,
        ) from exc
    if target not in allowed:
        current_label = pf.PLATFORM_STATUS_LABELS.get(
            draft.platform_status, draft.platform_status
        )
        if open_count and target is not PlatformStatus.REJECTED:
            raise ValidationError(
                f"有 {open_count} 条未解决驳回,先处理驳回记录再改状态",
                code=ErrorCode.STATE_CONFLICT,
                http_status=409,
            )
        raise ValidationError(
            f"当前状态「{current_label}」不允许改成"
            f"「{pf.PLATFORM_STATUS_LABELS.get(to_status, to_status)}」",
            code=ErrorCode.STATE_CONFLICT,
            http_status=409,
        )

    now = datetime.now(UTC)
    draft.platform_status = target.value
    draft.platform_status_at = now.replace(tzinfo=None)
    draft.platform_status_by = actor
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="ListingDraft",
        entity_id=draft.id,
        payload={
            "action": "platform_status",
            "spu": draft.spu,
            "to_status": target.value,
        },
    )
    return draft


# ==========================================================
# 记录驳回
# ==========================================================


def record_rejection(
    session: Session,
    draft: ListingDraft,
    product: Product,
    *,
    reason_code: str,
    platform_note: str | None,
    wanted_fingerprint: str | None,
    actor: str,
) -> PlatformRejection:
    """录入一条平台驳回,同时将草稿行的平台状态置 REJECTED。

    驳回原因码用 `platform.RejectionReason` 里的枚举值。
    `wanted_fingerprint` 是运营在界面上选的那次导出;None 表示自动取最近一次。

    写法上故意让这一个函数同时完成两件事(写驳回记录 + 改平台状态),
    保证两者的原子性 —— 把它们拆开,第一件写了第二件漏写,状态台账就不同步了。
    """
    try:
        pf.RejectionReason(reason_code)
    except ValueError as exc:
        allowed = "、".join(r.value for r in pf.RejectionReason)
        raise ValidationError(
            f"未知的驳回原因码 {reason_code!r};可用:{allowed}",
            code=ErrorCode.INPUT_INVALID,
        ) from exc

    entries = _export_audit_entries(session, draft.id)
    located = pf.locate_export(
        wanted_fingerprint=wanted_fingerprint,
        entries=entries,
        current_fingerprint=draft.source_fingerprint,
        current_components=(dict(draft.canonical_snapshot or {}).get("components")),
        current_exported_at=(
            draft.exported_at.isoformat() if draft.exported_at else None
        ),
    )
    if located is None:
        raise ValidationError(
            "找不到对应的导出记录;请确认草稿已导出过,或指定正确的指纹",
            code=ErrorCode.NOT_FOUND,
            http_status=409,
        )

    now = datetime.now(UTC)
    row = PlatformRejection(
        draft_id=draft.id,
        spu=product.spu,
        sku=product.sku,
        reason_code=reason_code,
        platform_note=platform_note or None,
        export_fingerprint=located.fingerprint,
        export_at=datetime.fromisoformat(located.exported_at).replace(tzinfo=None)
        if located.exported_at
        else None,
        component_snapshot=dict(located.components) if located.components else None,
        located_by=located.located_by,
        status=RejectionStatus.OPEN.value,
        recorded_by=actor,
    )
    session.add(row)

    # 平台状态强制置 REJECTED(不走手工转移检查,驳回是外部强制的)
    draft.platform_status = PlatformStatus.REJECTED.value
    draft.platform_status_at = now.replace(tzinfo=None)
    draft.platform_status_by = actor
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="PlatformRejection",
        entity_id=row.id,
        payload={
            "action": "record_rejection",
            "spu": product.spu,
            "sku": product.sku,
            "reason_code": reason_code,
            "located_by": located.located_by,
        },
    )
    return row


def record_api_rejection(
    session: Session,
    draft: ListingDraft,
    product: Product,
    *,
    category: Any,
    platform_code: str | None,
    platform_note: str | None,
    actor: str = "system:poller",
) -> PlatformRejection | None:
    """平台在**轮询**里驳回了一个 API 自动上架的商品(任务 15 的驳回回流)。

    返回 None 表示这条驳回已经在台账里了,本次不重复记。

    ## 为什么不直接复用 `record_rejection()`

    那个函数走 `locate_export()`,找不到导出记录就抛 409 ——「没导出过的商品
    谈不上被平台驳回」。这个前提对**手工上传 Excel** 的流程成立,对 API 自动
    上架不成立:一个从头到尾没人导过 Excel 的商品,照样会被平台驳回。

    照搬过去的后果很具体:驳回恰恰在最该被记下来的时候记不进台账,商品顶着
    "审核中"沉底,而它其实已经被拒了。所以这里换一个定位依据,而不是换一张表 ——
    表、状态机、阻断计数全部照旧,工作台不用改一行就能看见这条驳回。

    ## 被驳回的是哪一版,由提交尝试确定

    草稿指纹进过幂等键(`build_publish_idempotency_key`),报文快照存在
    `PublishAttempt.safe_request_snapshot` 上。两者一起把版本钉死,而且比
    导出审计更准:导出是人手工上传的,中间可能换过文件;提交是系统自己发的。
    `located_by` 如实标成 `publish_attempt`,不冒充 `audit`。

    ## 重复驳回不重复记

    轮询器会反复问平台,而平台会反复说 REJECTED。虽然 `PLATFORM_REJECTED`
    不在 `POLLABLE_STATUSES` 里(转进去之后就不再问了),这里仍然自己挡一次:
    并发的两个 worker 可能同时拿到同一次驳回,而"同一次驳回记了两条"会让
    运营看到两个待办、改一次、剩一个永远关不掉的幽灵。判据是**同一份草稿上
    同原因码的未解决记录**,不是"有没有任何未解决记录" —— 平台一次驳回多条
    原因是常事,那些必须各记一条。
    """
    reason = pf.reason_for_category(category)
    for existing in open_rejections(session, draft.id):
        if existing.reason_code == reason.value:
            logger.info(
                "publish.rejection_already_recorded",
                extra={
                    "extra_fields": {
                        "draft_id": str(draft.id),
                        "reason_code": reason.value,
                        "rejection_id": str(existing.id),
                    }
                },
            )
            return None

    now = datetime.now(UTC)
    row = PlatformRejection(
        draft_id=draft.id,
        spu=product.spu,
        sku=product.sku,
        reason_code=reason.value,
        # 平台原话一字不改。原因码只回答"该去哪个标签页改",
        # 而"为什么被拒"只有这句原话说得清
        platform_note=_api_note(platform_code, platform_note),
        export_fingerprint=draft.source_fingerprint,
        export_at=draft.exported_at,
        component_snapshot=pf.trim_components(
            dict(draft.canonical_snapshot or {}).get("components")
        ),
        located_by=pf.LOCATED_BY_PUBLISH_ATTEMPT,
        status=RejectionStatus.OPEN.value,
        recorded_by=actor,
    )
    session.add(row)

    # 与 record_rejection 一致:驳回是外部强制的,不走手工转移检查
    draft.platform_status = PlatformStatus.REJECTED.value
    draft.platform_status_at = now.replace(tzinfo=None)
    draft.platform_status_by = actor
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="PlatformRejection",
        entity_id=row.id,
        payload={
            "action": "record_api_rejection",
            "spu": product.spu,
            "sku": product.sku,
            "reason_code": reason.value,
            "platform_code": platform_code,
            "located_by": pf.LOCATED_BY_PUBLISH_ATTEMPT,
        },
    )
    return row


def _api_note(platform_code: str | None, platform_note: str | None) -> str | None:
    """平台驳回码与原话拼成一句给人看的备注。

    码和原话都留:原话是给运营看的,码是拿去平台后台搜的 —— 出事时那是
    唯一能和平台客服对上的东西,而它常常不出现在原话里。
    """
    code = (platform_code or "").strip()
    note = (platform_note or "").strip()
    if code and note:
        return f"[{code}] {note}"
    return code or note or None


# ==========================================================
# 解决驳回
# ==========================================================


def _publish_attempt_entries(session: Session, draft_id: UUID) -> list[dict[str, Any]]:
    """A-13:这份草稿名下**成功的**提交尝试留痕,新在前。

    关联口径与 `api/publish._current_draft` 的第一优先级一致:
    `ChannelListing.draft_id` 是提交时钉住的那一版。API 自动发布的商品
    不经过导出,`resolve_gate` 用这份留痕代替"新导出"那半边证据 ——
    指纹那半边仍然落在当前草稿指纹上,所以**没改草稿的重复提交过不了闸**。

    只认 SUCCEEDED:PENDING/IN_FLIGHT 还没有结果,UNKNOWN 不知道平台
    收没收到,FAILED/ABORTED 根本没提交成功 —— 它们都不构成
    "商品已经重新提交给平台"这个事实。
    """
    rows = session.execute(
        select(PublishAttempt.created_at)
        .join(ChannelListing, PublishAttempt.channel_listing_id == ChannelListing.id)
        .where(
            ChannelListing.draft_id == draft_id,
            PublishAttempt.status == PublishAttemptStatus.SUCCEEDED.value,
        )
        .order_by(PublishAttempt.created_at.desc())
        .limit(50)
    ).all()
    return [
        {"at": row[0].isoformat() if row[0] else None}
        for row in rows
    ]


def _gate_for(
    rejection: PlatformRejection,
    draft: ListingDraft,
    entries: list[dict[str, Any]],
    attempt_entries: Sequence[dict[str, Any]] = (),
) -> pf.ResolveGate:
    return pf.resolve_gate(
        rejected_fingerprint=rejection.export_fingerprint,
        rejected_at=(
            rejection.created_at.isoformat() if rejection.created_at else None
        ),
        entries=entries,
        current_fingerprint=draft.source_fingerprint,
        attempt_entries=attempt_entries,
    )


def serialize_resolve_gate(gate: pf.ResolveGate) -> dict[str, Any]:
    """闸门 -> 接口字段。`missing_labels` 就是界面上"还缺哪一步"那行字。"""
    return {
        "satisfied": gate.satisfied,
        "has_new_export": gate.has_new_export,
        "has_new_attempt": gate.has_new_attempt,
        "fingerprint_changed": gate.fingerprint_changed,
        "missing": list(gate.missing),
        "missing_labels": [
            pf.RESOLVE_GATE_LABELS.get(code, code) for code in gate.missing
        ],
        "missing_advice": [
            pf.RESOLVE_GATE_ADVICE.get(code, "") for code in gate.missing
        ],
    }


def resolve_gates(
    session: Session, draft: ListingDraft, rejections: Sequence[PlatformRejection]
) -> dict[str, dict[str, Any]]:
    """一次读审计,算出这批 OPEN 驳回各自的闸门状态。

    A4 的验收要求"页面明确显示还缺少哪一步"。做法是让列表接口把闸门一起带出来,
    而不是等运营点了"解决"再由 409 告诉他 —— 后者是把提醒放在动作之后。

    A6:这条路径挂在 GET 上,全程只读(`_export_audit_entries` 只 select)。
    每条驳回单独查一次审计会变成 N 次查询,所以审计在这里读一次给全部人用。
    """
    entries = _export_audit_entries(session, draft.id)
    # A-13:提交尝试留痕同样读一次给全部驳回用,理由同上(避免 N 次查询)
    attempt_entries = _publish_attempt_entries(session, draft.id)
    out: dict[str, dict[str, Any]] = {}
    for row in rejections:
        if row.status not in UNRESOLVED_REJECTION_STATUSES:
            continue
        out[str(row.id)] = serialize_resolve_gate(
            _gate_for(row, draft, entries, attempt_entries)
        )
    return out


def resolve_rejection(
    session: Session,
    rejection: PlatformRejection,
    draft: ListingDraft,
    *,
    note: str | None,
    confirmed: bool = False,
    actor: str,
) -> PlatformRejection:
    """推进一条驳回记录。**"已解决"不再是一次点击能到达的状态。**

    ## 评审第 7 条改了什么

    原来:闸门没过时,运营勾一个框或写一句备注,记录直接变 RESOLVED,
    最后一条关闭后草稿还自动变回 SUBMITTED。于是台账上的"已解决"和
    "已重新提交"都证明不了商品真的被修过、重导过、重传过 —— 而这三件事
    正是驳回回流唯一要留下的东西。

    现在走 `platform.resolve_target` 那张表(纯函数,可穷举):

        闸门过了(驳回之后有新导出 + 指纹变了)  -> RESOLVED
        闸门没过 + 运营声明修过了                -> FIXED_PENDING_EXPORT
        闸门没过 + 什么都没说                    -> 409

    中间态**仍然阻断**(`open_rejections` 按未解决集合算),商品不会因为
    一句声明就从阻断列表里消失。运营改完重导之后再点一次,这次闸门过了,
    才真的进 RESOLVED。

    ## 平台状态不再自动跳

    全部解决后草稿**停在 REJECTED**,由运营用独立动作 `mark_resubmitted`
    确认"我确实把新文件传上去了"。自动跳 SUBMITTED 是在替运营声明一件
    系统根本看不见的事:平台侧全程手工,系统不知道文件有没有真的传上去。

    P1 的三条不变量原样保留:REJECTED 仍只能由 `record_rejection` 产生、
    有未解决驳回时状态仍锁死、定位把握仍如实分三档。
    """
    # A1:归属校验放在 service 而不是只放在 API —— 这里是唯一能写
    # platform_rejections 的地方,守卫也应该在这里各写一份。
    # 抛在任何写入之前,整个事务不留痕迹。
    if rejection.draft_id != draft.id:
        raise NotFoundError("驳回记录不属于这份草稿")

    if rejection.status == RejectionStatus.RESOLVED.value:
        raise ValidationError(
            "这条驳回记录已经解决过了",
            code=ErrorCode.STATE_CONFLICT,
            http_status=409,
        )

    # 闸门与目标状态都算在任何写入之前 —— 被拦下的那次请求不该在
    # 事务里留下痕迹。
    gate = _gate_for(
        rejection,
        draft,
        _export_audit_entries(session, draft.id),
        _publish_attempt_entries(session, draft.id),
    )
    text = (note or "").strip()
    target = pf.resolve_target(
        current_status=rejection.status,
        gate_satisfied=gate.satisfied,
        declared_fixed=bool(confirmed or text),
    )

    if target == pf.RESOLVE_REFUSED:
        missing = ";".join(
            pf.RESOLVE_GATE_LABELS.get(code, code) for code in gate.missing
        )
        raise ValidationError(
            f"系统没有看到这条驳回被修过的证据({missing})。"
            "如果已经改过,请勾选「已修改」或写一句说明 —— 这条驳回会转成"
            "「已声明修复,等待重新导出」并继续阻断,直到出现新的导出留痕。",
            code=ErrorCode.STATE_CONFLICT,
            http_status=409,
            detail={"resolve_gate": serialize_resolve_gate(gate)},
        )

    now = datetime.now(UTC)

    if target == pf.RESOLVE_TO_FIXED_PENDING:
        # 声明如实记下,但**不算解决**。resolved_at / resolved_by 不写:
        # 这条记录还没被解决,填上它们等于伪造一个时间点
        rejection.status = RejectionStatus.FIXED_PENDING_EXPORT.value
        rejection.resolved_note = text or pf.GATE_CONFIRM_NOTE
        session.flush()
        audit.record(
            session,
            actor=actor,
            action=AuditAction.UPDATE,
            entity_type="PlatformRejection",
            entity_id=rejection.id,
            payload={
                "action": "declare_rejection_fixed",
                "spu": rejection.spu,
                "gate_satisfied": False,
                "gate_missing": list(gate.missing),
            },
        )
        return rejection

    rejection.status = RejectionStatus.RESOLVED.value
    rejection.resolved_at = now.replace(tzinfo=None)
    rejection.resolved_by = actor
    rejection.resolved_note = text or rejection.resolved_note
    session.flush()

    remaining = len(open_rejections(session, draft.id))
    # 平台状态**不动**。它停在 REJECTED,直到运营用 mark_resubmitted
    # 明确说一句"新文件已经传上去了"(见那个函数的说明)
    session.flush()
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="PlatformRejection",
        entity_id=rejection.id,
        payload={
            "action": "resolve_rejection",
            "spu": rejection.spu,
            "remaining_open": remaining,
            # 第 7 条之后,走到这里的每一条都是闸门过了的。这两个字段
            # 留着不是冗余:它们让"这条是靠什么关掉的"在台账上自证,
            # 而不必回头去读当时的代码是哪一版
            "gate_satisfied": True,
            "gate_missing": [],
            "closed_by_confirmation": False,
        },
    )
    return rejection


def mark_resubmitted(
    session: Session, draft: ListingDraft, *, actor: str
) -> ListingDraft:
    """运营确认"驳回改完的新文件已经重新传上平台"(评审第 7 条)。

    这是一个**独立动作**,不是关闭最后一条驳回的副作用。区别不是形式上的:

        关闭驳回     "这个问题我处理完了"       —— 关于内容
        重新上传     "新文件我传到平台了"       —— 关于平台侧,系统看不见

    原来后者由前者自动触发,于是台账上的 SUBMITTED 可能对应着"运营关掉了
    最后一条驳回、然后去吃午饭了"。首期没有平台 API,系统永远不可能自己
    知道文件传没传,所以这句话只能由人说 —— 那就让人明确地说一次。

    前置:没有任何未解决驳回(含 FIXED_PENDING_EXPORT)。
    """
    remaining = open_rejections(session, draft.id)
    if remaining:
        raise ValidationError(
            f"还有 {len(remaining)} 条驳回未解决,先把它们处理完再确认重新上传",
            code=ErrorCode.STATE_CONFLICT,
            http_status=409,
        )
    if draft.platform_status != PlatformStatus.REJECTED.value:
        raise ValidationError(
            f"当前平台状态是"
            f"「{pf.PLATFORM_STATUS_LABELS.get(draft.platform_status, draft.platform_status)}」,"
            "「确认重新上传」只用于驳回修复完成之后",
            code=ErrorCode.STATE_CONFLICT,
            http_status=409,
        )

    now = datetime.now(UTC)
    draft.platform_status = PlatformStatus.SUBMITTED.value
    draft.platform_status_at = now.replace(tzinfo=None)
    draft.platform_status_by = actor
    session.flush()
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="ListingDraft",
        entity_id=draft.id,
        payload={
            "action": "resubmit_after_rejection",
            "spu": draft.spu,
            "to_status": PlatformStatus.SUBMITTED.value,
        },
    )
    return draft


def _get_rejection(session: Session, rejection_id: UUID) -> PlatformRejection:
    row = session.get(PlatformRejection, rejection_id)
    if row is None:
        raise NotFoundError("驳回记录不存在")
    return row


# ==========================================================
# 序列化
# ==========================================================


def serialize_rejection(row: PlatformRejection) -> dict[str, Any]:
    from app.workbench.platform import (
        LOCATED_BY_LABELS,
        REJECTION_REASON_LABELS,
        REJECTION_STATUS_LABELS,
        RejectionReason,
    )

    known = {r.value for r in RejectionReason}
    reason = RejectionReason(row.reason_code) if row.reason_code in known else None
    return {
        "id": str(row.id),
        "draft_id": str(row.draft_id),
        "spu": row.spu,
        "sku": row.sku,
        "reason_code": row.reason_code,
        "reason_label": (
            REJECTION_REASON_LABELS.get(reason, row.reason_code) if reason else row.reason_code
        ),
        "platform_note": row.platform_note,
        "export_fingerprint": row.export_fingerprint,
        "export_at": row.export_at.isoformat() if row.export_at else None,
        "component_snapshot": row.component_snapshot or {},
        "located_by": row.located_by,
        "located_by_label": LOCATED_BY_LABELS.get(row.located_by, row.located_by),
        "status": row.status,
        "status_label": REJECTION_STATUS_LABELS.get(row.status, row.status),
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "resolved_by": row.resolved_by,
        "resolved_note": row.resolved_note,
        "recorded_by": row.recorded_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
