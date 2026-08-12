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

from app.api import action_gate
from app.api.deps import current_actor, db_session, require_operator
from app.core.errors import NotFoundError
from app.models.product import Product
from app.providers import registry
from app.schemas.generation_plan import (
    EffectivePlanOut,
    GenerationPlanCreate,
    GenerationPlanEffectOut,
    GenerationPlanOut,
)
from app.services import generation_plan_service as svc
from app.workflows import generation_plan as gp

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


@router.get("/effective", response_model=EffectivePlanOut)
def effective_plan(
    product_id: UUID = Query(..., description="按这一行 SKU 的 SPU 与颜色解析"),
    session: Session = Depends(db_session),
) -> EffectivePlanOut:
    """这件商品出图会按哪份方案跑。**只读,不提交。**

    ## 路径必须排在 `/{plan_id}` 前面

    FastAPI 按声明顺序匹配,`/{plan_id}` 会把字面量 `effective` 也吃掉,
    然后在 `UUID("effective")` 上给出一个 422 —— 而错因指向"plan_id 格式不对",
    一条完全查不到真因的报错。今天 `/{plan_id}` 只在 POST 上出现,所以
    顺序反过来也不会撞;写成这个顺序是为了让加 `GET /{plan_id}` 的下一个人
    不必先踩一次。

    作用域推导与 `create_task` **同一条**:颜色从商品行上取,不由调用方传。
    两处各推一次的表现是界面显示 A 颜色的方案、任务按 B 颜色的方案跑。

    ## 出参一次构造完,不逐个属性赋值

    `tools/audit_column_writers.py` 按**属性名**宽认列写入(理由写在
    `generation_plan_service._update_draft` 上)。写成 `out.scope = ...`
    会被算成 `MediaConsent.scope` 的写入点,于是那笔真实欠账当场从台账里
    消失 —— 一条本轮亲手撞上的、不报错只让账目失真的形状。
    """
    product = session.get(Product, product_id)
    if product is None:
        raise NotFoundError("商品不存在")

    spu_id = getattr(product, "spu_id", None)
    variant_id = getattr(product, "color_variant_id", None)
    # 没有 SPU 归属就没有方案可解析。**这不是错误** —— 老建档路径建的行
    # 今天仍是这个形状,而它们照样要能出图(`create_task` 那条"解析不到
    # 方案不拦")。给一个空壳而不是 404,界面才说得出"这件商品没有方案"
    row = (
        svc.resolve_for(session, spu_id=spu_id, color_variant_id=variant_id)
        if spu_id is not None
        else None
    )
    if row is None:
        return EffectivePlanOut(
            product_id=product_id, spu_id=spu_id, color_variant_id=variant_id
        )

    view = svc.to_view(row)
    return EffectivePlanOut(
        product_id=product_id,
        spu_id=spu_id,
        color_variant_id=variant_id,
        plan=GenerationPlanOut.model_validate(row),
        scope="COLOR" if row.color_variant_id is not None else "SPU_DEFAULT",
        candidates_per_round=gp.total_candidates(view.angles),
        required_angles=sorted(gp.required_angles(view.angles)),
        # `usable_providers` 递的是环境事实(装了什么、配了什么 Key)。
        # 不递的话,一份指着 comfyui 或未配 Key 的 FASHN 的方案在这里
        # **一个问题都不报**,而创建任务时它会 CONFIG_INVALID —— 而
        # 这个接口存在的意义正是"提前把问题说出来"
        problems=[
            p.message
            for p in gp.plan_problems(
                view, usable_providers=registry.selectable_names()
            )
        ],
    )


@router.post("", response_model=GenerationPlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(
    payload: GenerationPlanCreate,
    session: Session = Depends(db_session),
    actor: str = Depends(current_actor),
) -> GenerationPlanOut:
    # AC-05(F-2):选方案属于方案步,前置是属性确认。方案按 SPU + 颜色存,
    # 所以按「这个 SPU 下任一行 SKU 可做」判
    action_gate.ensure_allowed_for_spu(
        session, action_gate.NextActionCode.CHOOSE_PLAN, spu_id=payload.spu_id
    )
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
    action_gate.ensure_allowed_for_spu(
        session, action_gate.NextActionCode.CHOOSE_PLAN, spu_id=row.spu_id
    )
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

