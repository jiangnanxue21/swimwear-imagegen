"""素材库接口(M1,§13)。

    GET   /api/media                     列表,按来源/角色/状态/商品筛选
    GET   /api/media/{id}                单条详情
    POST  /api/media/{id}/role           人工指定角色
    POST  /api/media/{id}/quarantine     隔离
    POST  /api/media/{id}/release        放行
    GET   /api/media/migration/status    回填覆盖率 + 对账统计(迁移期用)

注意路径:代发私有素材字节的那个接口在 `api/media_files.py`,挂在
`/api/media-files`。两者分开是因为**鉴权方式不同** —— 这一组走正常的
操作口令,那一组走 URL 签名(`<img>` 标签带不了自定义请求头)。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import current_actor, db_session, require_operator
from app.core.config import settings
from app.core.enums import MediaRole, MediaSource, MediaStatus
from app.media import service as media_service
from app.schemas.common import Page
from app.schemas.media import (
    MediaAssetOut,
    MediaQuarantineIn,
    MediaRoleIn,
)
from app.services.storage import asset_url, build_storage

# 整个路由挂 require_operator:素材库能看到未发布商品的全部原图,
# 和商品列表是同一个敏感级别
router = APIRouter(
    prefix="/media",
    tags=["media"],
    dependencies=[Depends(require_operator)],
)


def get_storage():
    return build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )


def _out(asset, storage) -> MediaAssetOut:
    result = MediaAssetOut.model_validate(asset)
    # 素材落在私有前缀(uploads/ candidates/)下,拿到的是短期签名地址。
    # 直接用 public_url 会给出一个永久匿名可读的地址 —— 上一轮评审第 2 条
    result.url = asset_url(storage, asset.storage_path)
    return result


@router.get("", response_model=Page[MediaAssetOut])
def list_media(
    session: Session = Depends(db_session),
    storage=Depends(get_storage),
    product_id: UUID | None = Query(None),
    spu: str | None = Query(None),
    source: MediaSource | None = Query(None),
    role: MediaRole | None = Query(None),
    status_filter: MediaStatus | None = Query(None, alias="status"),
    sort: str | None = Query(
        None, description=f"排序字段,可用:{', '.join(media_service.SORTABLE)}"
    ),
    order: str | None = Query(None, description="asc / desc(也认 antd 的 ascend / descend)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> Page[MediaAssetOut]:
    rows, total = media_service.list_assets(
        session,
        product_id=product_id,
        spu=spu,
        source=source.value if source else None,
        role=role.value if role else None,
        status=status_filter.value if status_filter else None,
        sort=sort,
        order=order,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[MediaAssetOut](
        items=[_out(r, storage) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/migration/status")
def migration_status(
    session: Session = Depends(db_session),
    days: int = Query(7, ge=1, le=90),
) -> dict:
    """迁移期的两个判据。

    `coverage` 回答「回填跑完了没有」,`mismatches` 回答「这一周干净不干净」。
    后者是迁移 B 的出口条件 —— 只打日志不落表的话,到了该切换的那天
    没有任何人能拿出证据说明这一周确实没有不一致。
    """
    return {
        "coverage": media_service.coverage(session),
        "mismatches": media_service.mismatch_summary(session, days=days),
        "window_days": days,
    }


@router.get("/{media_id}", response_model=MediaAssetOut)
def get_media(
    media_id: UUID,
    session: Session = Depends(db_session),
    storage=Depends(get_storage),
) -> MediaAssetOut:
    return _out(media_service.get_asset(session, media_id), storage)


@router.post("/{media_id}/role", response_model=MediaAssetOut)
def set_role(
    media_id: UUID,
    payload: MediaRoleIn,
    request: Request,
    session: Session = Depends(db_session),
    storage=Depends(get_storage),
) -> MediaAssetOut:
    """人工指定角色。**永远覆盖模型和规则的判断。**"""
    asset = media_service.set_role(
        session, media_id, role=payload.role, actor=current_actor(request)
    )
    session.commit()
    return _out(asset, storage)


@router.post("/{media_id}/quarantine", response_model=MediaAssetOut)
def quarantine(
    media_id: UUID,
    payload: MediaQuarantineIn,
    request: Request,
    session: Session = Depends(db_session),
    storage=Depends(get_storage),
) -> MediaAssetOut:
    asset = media_service.quarantine(
        session, media_id, reason=payload.reason, actor=current_actor(request)
    )
    session.commit()
    return _out(asset, storage)


@router.post("/{media_id}/release", response_model=MediaAssetOut)
def release(
    media_id: UUID,
    request: Request,
    session: Session = Depends(db_session),
    storage=Depends(get_storage),
) -> MediaAssetOut:
    asset = media_service.release(session, media_id, actor=current_actor(request))
    session.commit()
    return _out(asset, storage)
