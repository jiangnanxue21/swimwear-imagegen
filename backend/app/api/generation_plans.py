"""生成方案接口(PRD v3.1 §12.4 / §6.4)。

四个端点,对应向导第四步的四次交互:

    GET    /generation-plans?spu_id=      这个款配过哪些方案(含归档)
    POST   /generation-plans              落一份 DRAFT
    POST   /generation-plans/{id}/preview 启用它会让哪些颜色的图过期(§7.5)
    POST   /generation-plans/{id}/activate 启用

**判定不在这一层。**这里只做取数、调服务、持有事务 —— 与
`api/publish.py` 顶部那条同一个理由:判定留在零依赖模块里才能被穷举,
搬进接口函数之后,覆盖"某个组合下该不该拒"就要起一个 FastAPI 加一个库。

## 为什么"预览"是 POST 而不是 GET

它不改状态,但它要**先算一遍启用之后的效果**,入参是整份方案。
写成 GET 就要把方案塞进 query string,而 `angles` 是个数组。
`api/exports.py` 的预览端点当初也走过这一步。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import current_actor, db_session, require_operator
from app.schemas.generation_plan import (
    GenerationPlanCreate,
    GenerationPlanEffectOut,
    GenerationPlanOut,
)
from app.services import generation_plan_service as svc

router = APIRouter(
    prefix="/generation-plans",
    tags=["generation-plans"],
    dependencies=[Depends(require_operator)],
)


@router.get("", response_model=list[GenerationPlanOut])
def list_plans(
    spu_id: UUID = Query(...),
    session: Session = Depends(db_session),
) -> list[GenerationPlanOut]:
    return [GenerationPlanOut.model_validate(r) for r in svc.list_plans(session, spu_id)]


@router.post("", response_model=GenerationPlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(
    payload: GenerationPlanCreate,
    session: Session = Depends(db_session),
    actor: str = Depends(current_actor),
) -> GenerationPlanOut:
    row = svc.save_plan(
        session,
        spu_id=payload.spu_id,
        color_variant_id=payload.color_variant_id,
        model_template_id=payload.model_template_id,
        provider=payload.provider,
        scene=payload.scene,
        pose=payload.pose,
        angles=[a.model_dump() for a in payload.angles],
        budget_cap=payload.budget_cap,
        note=payload.note,
        actor=actor,
    )
    # 写端点自己提交(任务 19 后半:请求的事务边界归接口所有)。
    # 漏了这一行 = 接口返回 201 而什么都没存下来
    session.commit()
    return GenerationPlanOut.model_validate(row)


@router.post("/{plan_id}/preview", response_model=GenerationPlanEffectOut)
def preview_activation(
    plan_id: UUID,
    session: Session = Depends(db_session),
) -> GenerationPlanEffectOut:
    """启用它之前先看代价(§7.5)。**只读,不提交。**"""
    row = svc.get_plan(session, plan_id)
    before = [svc.to_view(p) for p in svc.list_plans(session, row.spu_id)]
    after = [p for p in before if not _same_scope_active(p, row)] + [_as_active(row)]
    scopes = svc.stale_image_scopes(session, spu_id=row.spu_id, before=before, after=after)
    return GenerationPlanEffectOut(
        plan=GenerationPlanOut.model_validate(row),
        stale_color_variant_ids=[UUID(s) for s in sorted(scopes)],
    )


@router.post("/{plan_id}/activate", response_model=GenerationPlanEffectOut)
def activate_plan(
    plan_id: UUID,
    session: Session = Depends(db_session),
    actor: str = Depends(current_actor),
) -> GenerationPlanEffectOut:
    row = svc.get_plan(session, plan_id)
    # 快照必须在改之前取,而且必须是 `PlanView`(冻结的)——
    # 拿库行当快照的话,`activate()` 原地改 status 会把快照一起改掉,
    # 于是"变了哪些作用域"永远算出空集
    snapshot = [svc.to_view(p) for p in svc.list_plans(session, row.spu_id)]
    updated = svc.activate(session, plan_id, actor=actor)
    scopes = svc.stale_image_scopes(session, spu_id=updated.spu_id, before=snapshot)
    session.commit()
    return GenerationPlanEffectOut(
        plan=GenerationPlanOut.model_validate(updated),
        stale_color_variant_ids=[UUID(s) for s in sorted(scopes)],
    )


def _same_scope_active(candidate, target) -> bool:
    """`candidate` 是 `PlanView`(颜色是字符串),`target` 是库行(UUID)。"""
    target_scope = str(target.color_variant_id) if target.color_variant_id else None
    return candidate.color_variant_id == target_scope and candidate.status == "ACTIVE"


def _as_active(row):
    """把这一份当成已启用来算效果。**不写库。**"""
    from app.workflows import generation_plan as gp

    view = svc.to_view(row)
    return gp.PlanView(
        plan_id=view.plan_id,
        color_variant_id=view.color_variant_id,
        model_template_id=view.model_template_id,
        provider=view.provider,
        scene=view.scene,
        pose=view.pose,
        angles=view.angles,
        budget_cap=view.budget_cap,
        status="ACTIVE",
    )

