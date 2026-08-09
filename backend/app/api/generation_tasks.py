"""生成任务 REST 接口(需求第十六章)。

创建接口必须立即返回,绝不等待生成完成 —— 落库后把任务丢给 Celery 就走。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import current_actor, db_session, require_operator
from app.core.config import settings
from app.core.enums import DispatchReason, TaskStatus
from app.core.logging import get_logger
from app.schemas.common import Page
from app.schemas.generation import (
    AttemptOut,
    CandidateOut,
    TaskCreate,
    TaskCreateResult,
    TaskDetailOut,
    TaskOut,
)
from app.services import dispatch_service
from app.services import generation_service as gs
from app.services.storage import asset_url, build_storage
from app.workflows import state_machine as sm

# 整个路由挂 require_operator(读接口也要)。为什么挂在路由器上见 `deps.require_operator`
router = APIRouter(
    prefix="/generation-tasks",
    tags=["generation"],
    dependencies=[Depends(require_operator)],
)
logger = get_logger(__name__)


def _storage():
    return build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )


def _task_out(task) -> TaskOut:
    out = TaskOut.model_validate(task)
    out.can_cancel = sm.can_cancel(task.status)
    out.can_retry = sm.can_retry(task.status)
    out.can_delete = gs.can_delete_unstarted(task)
    return out


def _candidate_out(candidate, storage) -> CandidateOut:
    out = CandidateOut.model_validate(candidate)
    # 候选图还没过审,更不该公开 —— asset_url 按前缀给签名地址
    out.url = asset_url(storage, candidate.storage_path)
    return out


@router.post(
    "",
    response_model=TaskCreateResult,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator)],
)
def create_task(
    payload: TaskCreate,
    request: Request,
    session: Session = Depends(db_session),
) -> TaskCreateResult:
    task, deduplicated = gs.create_task(
        session,
        product_id=payload.product_id,
        mode=payload.mode.value,
        provider=payload.provider,
        routing_mode=payload.routing_mode.value,
        input_asset_ids=payload.input_asset_ids or None,
        model_template_id=payload.model_template_id,
        candidate_count=payload.candidate_count,
        max_rounds=payload.max_rounds,
        output_width=payload.output_width,
        output_height=payload.output_height,
        base_seed=payload.base_seed,
        prompt=payload.prompt,
        negative_prompt=payload.negative_prompt,
        prompt_version=payload.prompt_version,
        provider_params=payload.provider_params,
        idempotency_key=payload.idempotency_key,
        actor=current_actor(request),
    )

    if not deduplicated:
        # 派发意图和任务写在**同一个事务**里(Outbox)。
        # 以前是「先 commit 再 delay」,中间那道缝里进程挂掉,任务就永远停在
        # CREATED,界面显示"排队中",队列里却没有任何消息。
        dispatch_service.enqueue(session, task.id, reason=DispatchReason.CREATE.value)
        session.commit()
        # 提交之后才允许真正投递:反过来的话 worker 可能读到一个还不存在的任务。
        _dispatch(task.id, session)

    return TaskCreateResult(task=_task_out(task), deduplicated=deduplicated)


def _dispatch(task_id: UUID, session: Session) -> None:
    """快路径投递。失败不抛 —— 意图还在 Outbox 里,relay 会兜底。

    这一步不是"派发",只是"现在就派发,别等 relay 下一轮"。
    正确性由 Outbox 保证,它只负责延迟。
    """
    dispatch_service.deliver_pending(session, task_id)


@router.get("", response_model=Page[TaskOut])
def list_tasks(
    session: Session = Depends(db_session),
    product_id: UUID | None = Query(None),
    status_filter: TaskStatus | None = Query(None, alias="status"),
    provider: str | None = Query(None),
    sort: str | None = Query(None, description=f"排序字段,可用:{', '.join(gs.TASK_SORTABLE)}"),
    order: str | None = Query(None, description="asc / desc(也认 antd 的 ascend / descend)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Page[TaskOut]:
    rows, total = gs.list_tasks(
        session,
        product_id=product_id,
        status=status_filter.value if status_filter else None,
        provider=provider,
        sort=sort,
        order=order,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[TaskOut](
        items=[_task_out(r) for r in rows], total=total, page=page, page_size=page_size
    )


@router.get("/{task_id}", response_model=TaskDetailOut)
def get_task(task_id: UUID, session: Session = Depends(db_session)) -> TaskDetailOut:
    task = gs.get_task(session, task_id)
    storage = _storage()
    detail = TaskDetailOut.model_validate(task)
    detail.can_cancel = sm.can_cancel(task.status)
    detail.can_retry = sm.can_retry(task.status)
    detail.attempts = [AttemptOut.model_validate(a) for a in gs.list_attempts(session, task_id)]
    detail.candidates = [
        _candidate_out(c, storage) for c in gs.list_candidates(session, task_id)
    ]
    dispatch = dispatch_service.latest_for_task(session, task_id)
    if dispatch is not None:
        detail.dispatch_status = dispatch.status
        detail.dispatch_attempts = dispatch.attempts
        detail.dispatch_error = dispatch.last_error
    return detail


@router.post("/{task_id}/cancel", response_model=TaskOut, dependencies=[Depends(require_operator)])
def cancel_task(
    task_id: UUID, request: Request, session: Session = Depends(db_session)
) -> TaskOut:
    task = gs.cancel_task(session, task_id, actor=current_actor(request))
    session.commit()
    return _task_out(task)


@router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_operator)],
)
def delete_task(
    task_id: UUID, request: Request, session: Session = Depends(db_session)
) -> Response:
    gs.delete_unstarted_task(session, task_id, actor=current_actor(request))
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{task_id}/retry", response_model=TaskOut, dependencies=[Depends(require_operator)])
def retry_task(
    task_id: UUID,
    request: Request,
    session: Session = Depends(db_session),
    force: bool = Query(
        False,
        description="强制重试。仅用于「提交结果未知」的任务,"
        "确认 Provider 后台没有产生结果后再用,否则可能重复计费",
    ),
    reconciliation_note: str | None = Query(
        None,
        max_length=500,
        description="人工对账结论。force=true 时必填,会写入审计",
    ),
) -> TaskOut:
    task = gs.retry_task(
        session,
        task_id,
        actor=current_actor(request),
        force=force,
        reconciliation_note=reconciliation_note,
    )
    dispatch_service.enqueue(session, task.id, reason=DispatchReason.RETRY.value)
    session.commit()
    _dispatch(task.id, session)
    return _task_out(task)
