"""图片集接口(M5,§13)。

    GET   /api/image-sets?spu=            某个 SPU 的全部版本
    POST  /api/image-sets                 新建一版(DRAFT)
    GET   /api/image-sets/{id}            详情 + 校验结果
    POST  /api/image-sets/{id}/derive     从这一版派生新版(改已批准集的唯一方式)
    POST  /api/image-sets/{id}/approve    批准(校验不过就拒绝)
    POST  /api/image-sets/{id}/reject     快审退回(A10,受控原因 + 说明)
    POST  /api/image-sets/{id}/rollback   回滚到这一版
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import current_actor, db_session, require_operator
from app.listings import image_set_service
from app.schemas.image_set import (
    ImageSetCreate,
    ImageSetDetailOut,
    ImageSetItemOut,
    ImageSetItemsIn,
    ImageSetOut,
    VariantCoverageOut,
    ViolationOut,
)
from app.workbench.reject import MAX_NOTE_LENGTH

router = APIRouter(
    prefix="/image-sets", tags=["image-sets"], dependencies=[Depends(require_operator)]
)


def _detail(session: Session, row) -> ImageSetDetailOut:
    detail = ImageSetDetailOut.model_validate(row)
    detail.items = [
        ImageSetItemOut.model_validate(i)
        for i in image_set_service.list_items(session, row.id)
    ]
    problems = image_set_service.validate(session, row.id)
    detail.violations = [
        ViolationOut(
            code=p.code.value,
            message=p.message,
            variant_id=p.variant_id,
            media_asset_id=p.media_asset_id,
        )
        for p in problems
    ]
    detail.publishable = not problems and row.status == "APPROVED"
    detail.variant_coverage = VariantCoverageOut(
        **image_set_service.variant_coverage(session, row)
    )
    return detail


@router.get("", response_model=list[ImageSetOut])
def list_sets(
    spu: str = Query(..., min_length=1),
    session: Session = Depends(db_session),
) -> list[ImageSetOut]:
    return [
        ImageSetOut.model_validate(r) for r in image_set_service.list_sets(session, spu)
    ]


@router.post("", response_model=ImageSetDetailOut)
def create_set(
    payload: ImageSetCreate,
    request: Request,
    session: Session = Depends(db_session),
) -> ImageSetDetailOut:
    row = image_set_service.create_set(
        session,
        spu=payload.spu,
        items=[i.model_dump() for i in payload.items],
        channel=payload.channel,
        site=payload.site,
        actor=current_actor(request),
    )
    session.commit()
    return _detail(session, row)


@router.get("/{image_set_id}", response_model=ImageSetDetailOut)
def get_set(
    image_set_id: UUID, session: Session = Depends(db_session)
) -> ImageSetDetailOut:
    return _detail(session, image_set_service.get_set(session, image_set_id))


@router.post("/{image_set_id}/derive", response_model=ImageSetDetailOut)
def derive(
    image_set_id: UUID,
    payload: ImageSetItemsIn,
    request: Request,
    session: Session = Depends(db_session),
) -> ImageSetDetailOut:
    """从这一版派生新版。**改已批准图片集的唯一方式。**

    原地改的话,「平台驳回的是哪一版图」就没法回答 —— 而驳回回流
    第一件要做的事正是定位到具体那一版的具体那一项。
    """
    row = image_set_service.derive(
        session,
        image_set_id,
        items=[i.model_dump() for i in payload.items],
        actor=current_actor(request),
    )
    session.commit()
    return _detail(session, row)


@router.post("/{image_set_id}/approve", response_model=ImageSetDetailOut)
def approve(
    image_set_id: UUID, request: Request, session: Session = Depends(db_session)
) -> ImageSetDetailOut:
    row = image_set_service.approve(
        session, image_set_id, actor=current_actor(request)
    )
    session.commit()
    return _detail(session, row)


class ImageSetRejectIn(BaseModel):
    """快审退回的入参(A10)。

    `reason` 在这里只做长度校验,**取值合法性由服务层判**
    (`workbench.reject.validate_reject`)。写成 Literal 枚举的话,
    "哪些原因适用于图片集"就有了两份实现,而那张表还要按对象区分 ——
    加一个原因要改两处,漏一处的表现是接口 422 但界面上明明列着它。
    """

    reason: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)


@router.post("/{image_set_id}/reject", response_model=ImageSetDetailOut)
def reject(
    image_set_id: UUID,
    payload: ImageSetRejectIn,
    request: Request,
    session: Session = Depends(db_session),
) -> ImageSetDetailOut:
    """退回一版图片集。

    退回不是"不批准":不批准什么都不改,那一版仍然停在待批准、下一步仍然是
    "等待批准",于是它立刻回到快审队列排在原处。退回会改变判定结果,
    并由原因推出唯一下一步(`workbench/reject.py` 的映射表)。
    """
    row = image_set_service.reject(
        session,
        image_set_id,
        reason=payload.reason,
        note=payload.note,
        actor=current_actor(request),
    )
    session.commit()
    return _detail(session, row)


@router.post("/{image_set_id}/rollback", response_model=ImageSetDetailOut)
def rollback(
    image_set_id: UUID, request: Request, session: Session = Depends(db_session)
) -> ImageSetDetailOut:
    """回滚到这一版。

    不是"建一个新版本把内容改回去" —— 那样版本历史里会出现两条内容
    相同的记录,而"我们回滚过"这件事没有任何地方记着。
    """
    row = image_set_service.rollback_to(
        session, image_set_id, actor=current_actor(request)
    )
    session.commit()
    return _detail(session, row)
