"""生成方案的服务层(PRD v3.1 §4.7 / §6.4)。

**判定一律不在这里。**指纹怎么算、哪个颜色当前生效的是哪一份、预算过不过,
全在 `app/workflows/generation_plan.py`(零依赖、可穷举)。这一层只做四件事:

    取数     把库里的行翻成 `PlanView`
    校验     调既有的授权闸与受众筛选,不自己写一套
    落库     写规范化之后的角度与算出来的指纹
    留痕     audit

## 唯一写入点

`plan_fingerprint` 列带 `info={"projection": True}`,与 `products` 上那八个
投影列同一套规矩:**只有这里能写它**。路由层或脚本自己拼一个的表现是
——创建任务时用 A、判过期时用 B,于是每次创建任务都会顺带把自己的
图片集判成过期。一个只会多花钱、不会报错的洞。

## 启用一份方案 = 归档上一份,不是原地改

`uq_generation_plans_scope` 带 `WHERE status <> 'ARCHIVED'`,所以同一层
最多一份非归档的方案。`activate()` 先归档旧的再启用新的,两步在**同一个
事务**里 —— 分开提交的话中间那一刻两份都是 ACTIVE,而唯一索引会把第二步
挡下来,留下一个没有生效方案的 SPU。

原地改的另一个问题在 §8.1:图片集过期判定要拿"这批图当初按哪份方案生成"
和"现在生效的是哪份"比,原地改会把前者覆盖掉。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import AuditAction, GenerationPlanStatus
from app.core.errors import DuplicateError, ErrorCode, NotFoundError, ValidationError
from app.models.generation_plan import GenerationPlan
from app.models.spu import ColorVariant, Spu
from app.services import audit, model_template_service
from app.services.model_license import assert_usable
from app.workflows import generation_plan as gp


def to_view(row: GenerationPlan) -> gp.PlanView:
    """库行 -> 判定入参。**翻译只有这一处。**

    与 `scope_fingerprint.AssetView`、`media/evidence_rules` 那两处同一条
    理由:判定可穷举,而喂给判定的东西如果在每个调用点各翻一遍,
    守卫就只能靠 AST 看"调用出现了没有"。
    """
    return gp.PlanView(
        plan_id=str(row.id),
        color_variant_id=str(row.color_variant_id) if row.color_variant_id else None,
        model_template_id=str(row.model_template_id) if row.model_template_id else None,
        provider=row.provider or "",
        scene=row.scene or "",
        pose=row.pose or "",
        angles=gp.normalize_angles(row.angles_json),
        budget_cap=row.budget_cap,
        status=row.status,
    )


def list_plans(session: Session, spu_id: UUID) -> list[GenerationPlan]:
    """一个 SPU 的全部方案(含归档)。

    含归档是刻意的:界面要能回答"这批图是按哪份方案出的",而那份多半
    已经被归档了。过滤放在展示层,不放在取数 —— 取数少给一类,
    界面就永远没法把那类显示出来。
    """
    return list(
        session.scalars(
            select(GenerationPlan)
            .where(GenerationPlan.spu_id == spu_id)
            .order_by(GenerationPlan.color_variant_id, GenerationPlan.created_at)
        )
    )


def get_plan(session: Session, plan_id: UUID) -> GenerationPlan:
    row = session.get(GenerationPlan, plan_id)
    if row is None:
        raise NotFoundError("生成方案不存在")
    return row


def resolve_for(
    session: Session, *, spu_id: UUID, color_variant_id: UUID | None
) -> GenerationPlan | None:
    """某个颜色**当前生效**的方案。颜色覆盖优先,回落 SPU 默认。

    判定在 `gp.resolve_plan`,这里只负责取数 —— 写成一句 SQL
    (`ORDER BY color_variant_id NULLS LAST LIMIT 1`)会让"颜色覆盖优先"
    这条规则活在 SQL 里,而 SQL 在这台机器上一次都执行不了。
    """
    rows = list_plans(session, spu_id)
    wanted = str(color_variant_id) if color_variant_id else None
    chosen = gp.resolve_plan([to_view(r) for r in rows], wanted)
    if chosen is None:
        return None
    return next(r for r in rows if str(r.id) == chosen.plan_id)


def _assert_scope(session: Session, *, spu_id: UUID, color_variant_id: UUID | None) -> None:
    if session.get(Spu, spu_id) is None:
        raise NotFoundError("SPU 不存在")
    if color_variant_id is None:
        return
    variant = session.get(ColorVariant, color_variant_id)
    if variant is None:
        raise NotFoundError("颜色变体不存在")
    if variant.spu_id != spu_id:
        # 挡的是"给 A 款的颜色配了 B 款的方案"。外键各自成立,所以数据库
        # 一声不吭 —— 而那份方案永远解析不到,表现是"配了方案却没生效"
        raise ValidationError(
            "颜色变体不属于这个 SPU", code=ErrorCode.INPUT_INVALID
        )


def _assert_plan_usable(view: gp.PlanView, *, raw_angle_count: int) -> None:
    problems = gp.plan_problems(view, raw_angle_count=raw_angle_count)
    if problems:
        raise ValidationError(
            ";".join(p.message for p in problems), code=ErrorCode.INPUT_INVALID
        )


def save_plan(
    session: Session,
    *,
    spu_id: UUID,
    color_variant_id: UUID | None = None,
    model_template_id: UUID | None = None,
    provider: str = "",
    scene: str = "",
    pose: str = "",
    angles: Any = None,
    budget_cap: Decimal | None = None,
    note: str | None = None,
    actor: str = "system",
) -> GenerationPlan:
    """落一份 DRAFT 方案。**不直接生效**(见模块文档最后一段)。"""
    _assert_scope(session, spu_id=spu_id, color_variant_id=color_variant_id)

    if model_template_id is not None:
        # §6.4:「ModelTemplate 可用(现有授权闸 + 受众筛选 §10.5 + 生成前阻断)」。
        # 走既有的 assert_usable,不在这里重写一遍判断 —— 方案不是绕过
        # 授权的第二条路,那正是 §6.4 保留 v3.0 那条约束的原因。
        #
        # **在这里就挡,而不是等创建任务那一刻**:方案是运营会反复看的对象,
        # 一份挂着已吊销模特的方案摆在界面上,每个人都会以为它能用
        assert_usable(model_template_service.get_template(session, model_template_id))

    normalized = gp.normalize_angles(angles)
    raw_count = len(angles) if isinstance(angles, (list, tuple, dict)) else 0

    row = GenerationPlan(
        spu_id=spu_id,
        color_variant_id=color_variant_id,
        model_template_id=model_template_id,
        provider=(provider or "").strip(),
        scene=(scene or "").strip(),
        pose=(pose or "").strip(),
        angles_json=gp.serialize_angles(normalized),
        budget_cap=budget_cap,
        note=note,
        status=GenerationPlanStatus.DRAFT.value,
        created_by=actor,
    )
    view = to_view(row)
    _assert_plan_usable(view, raw_angle_count=raw_count)
    # **唯一写入点。**见模块文档第二段
    row.plan_fingerprint = gp.fingerprint_of(view)

    session.add(row)
    session.flush()
    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="GenerationPlan",
        entity_id=row.id,
        payload={
            "spu_id": str(spu_id),
            "color_variant_id": str(color_variant_id) if color_variant_id else None,
            "plan_fingerprint": row.plan_fingerprint,
            "angles": row.angles_json,
        },
    )
    return row


def activate(session: Session, plan_id: UUID, *, actor: str = "system") -> GenerationPlan:
    """启用一份方案:归档同层的旧方案,再把这一份置 ACTIVE。

    两步必须在同一个事务里,理由见模块文档最后一段。
    """
    row = get_plan(session, plan_id)
    if row.status == GenerationPlanStatus.ARCHIVED.value:
        raise DuplicateError("已归档的方案不能重新启用,请复制一份新的")

    previous = session.scalars(
        select(GenerationPlan).where(
            GenerationPlan.spu_id == row.spu_id,
            GenerationPlan.status == GenerationPlanStatus.ACTIVE.value,
        )
    ).all()
    for old in previous:
        if old.id == row.id:
            continue
        if old.color_variant_id == row.color_variant_id:
            old.status = GenerationPlanStatus.ARCHIVED.value

    row.status = GenerationPlanStatus.ACTIVE.value
    row.row_version += 1
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="GenerationPlan",
        entity_id=row.id,
        payload={
            "status": row.status,
            "plan_fingerprint": row.plan_fingerprint,
            # 谁被顶下去了要留痕:§8.1「更换生成方案」那一行的触发时机
            # 就是这一刻,而"哪些颜色因此要重出图"要能追溯
            "archived": [str(o.id) for o in previous if o.id != row.id],
        },
    )
    return row


def stale_image_scopes(
    session: Session,
    *,
    spu_id: UUID,
    before: list[gp.PlanView],
    after: list[gp.PlanView] | None = None,
) -> frozenset[str]:
    """改完方案之后,哪些颜色的图片集过期了(§8.1 第 7 行)。

    入参是 `PlanView` 而不是库行,**这一点是必须的**:`activate()` 会原地
    改 ORM 行的 status,取完库行再改的话,调用方手里的 `before` 会跟着变,
    于是"变了哪些作用域"永远算出空集 —— 一个不报错、只让图片集该过期
    而没过期的洞。`PlanView` 是冻结的 dataclass,拿不到这种反噬。

    颜色清单**由库里的 ACTIVE 颜色给出**,不由调用方传:传清单的写法有个
    具体的坏处 —— 清单漏了一个颜色时,那个颜色的图片集永远不会被判过期,
    而"漏了一个"不会有任何征兆(与 `scope_fingerprint.fingerprints` 同源)。
    """
    variants = [
        str(v.id)
        for v in session.scalars(
            select(ColorVariant).where(ColorVariant.spu_id == spu_id)
        )
    ]
    current = after if after is not None else [to_view(r) for r in list_plans(session, spu_id)]
    return gp.image_set_stale_scopes(
        gp.effective_fingerprints(before, variants),
        gp.effective_fingerprints(current, variants),
    )


def required_angles_for(session: Session, spu_id: UUID) -> dict[str, list[str]]:
    """每个颜色当前生效方案要求的角度。喂给 §6.5 的图片集验收。

    没有生效方案的颜色**不出现在结果里**,而不是给一个空集合:
    空集合会被读成"这个颜色不要求任何角度",于是没配方案等于免检 ——
    而 §6.4 的前置校验本来就要求先有方案才能创建任务。
    """
    plans = [to_view(r) for r in list_plans(session, spu_id)]
    out: dict[str, list[str]] = {}
    for variant in session.scalars(
        select(ColorVariant).where(ColorVariant.spu_id == spu_id)
    ):
        chosen = gp.resolve_plan(plans, str(variant.id))
        if chosen is not None and chosen.angles:
            out[str(variant.id)] = sorted(gp.required_angles(chosen.angles))
    return out
