"""运营工作台接口(任务书阶段 1+2)。

    GET  /api/workbench/products                    商品列表 + 流程状态聚合(FE-101~104/106)
    GET  /api/workbench/products/{id}/flow          单商品详情(FE-105 顶部进度区的数据源)
    POST /api/workbench/products/{id}/copy/generate 生成文案 / 单字段重生(FE-221/222)
    POST /api/workbench/products/{id}/copy/save     人工编辑落新版本(FE-222)
    POST /api/workbench/products/{id}/copy/{cid}/approve  批准文案(FE-225)
    POST /api/workbench/products/{id}/copy/{cid}/reject   快审退回文案(A10)
    GET  /api/workbench/reject-reasons              退回原因清单(A10,中文只有后端一份)
    POST /api/workbench/products/{id}/draft         生成/重建草稿(FE-231,手填字段一并提交)
    GET  /api/workbench/products/{id}/draft/stale-reason  过期详情(FE-234 / BE-205)
    POST /api/workbench/products/{id}/export        导出上架文件(FE-235,默认 xlsx)
    GET  /api/workbench/products/{id}/exports       导出历史(旧导出记录仍可追溯,§5.2 步骤 13)

列表页与详情页的完成度、阻断数来自同一个 `workbench.service.collect` ——
「任何情况下不得出现两个数字」(§3.4)是这条调用路径保证的,不是约定保证的。
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import current_actor, db_session, require_operator
from app.core import audience as audience_rules
from app.core.clock import iso_utc
from app.core.config import settings
from app.core.enums import AUDIENCE_LABELS, MediaStatus
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.http_headers import download_headers
from app.core.search import ESCAPE_CHAR, like_pattern
from app.core.sorting import normalize_sort, sort_in_memory
from app.models.audit_log import AuditLog
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.services.storage import asset_url, build_storage
from app.workbench import flow as flow_rules
from app.workbench import reject as reject_rules
from app.workbench import service as wb


def from_app_error(exc):
    """把应用层 NotFoundError 转成 FastAPI HTTPException——保持抛出即中止的语义。

    这个函数原来卡在 import 块中间,把后面六行 import 变成了 E402。
    位置是手滑,不是设计:它不需要在任何 import 之前。
    """
    from fastapi import HTTPException

    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    raise exc


router = APIRouter(
    prefix="/workbench", tags=["workbench"], dependencies=[Depends(require_operator)]
)


def _storage():
    return build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )


def _product(session: Session, product_id: UUID) -> Product:
    row = session.get(Product, product_id)
    if row is None:
        raise NotFoundError("商品不存在")
    return row


def _thumbnails(
    session: Session, products: list[Product], storage
) -> dict[UUID, str | None]:
    """每件商品一张列表缩略图(FE-101):优先正面图,其次任意可用图。"""
    ids = [p.id for p in products]
    if not ids:
        return {}
    rows = list(
        session.scalars(
            select(MediaAsset).where(
                MediaAsset.product_id.in_(ids),
                MediaAsset.status == MediaStatus.READY.value,
            )
        )
    )
    best: dict[UUID, MediaAsset] = {}
    for asset in rows:
        current = best.get(asset.product_id)
        prefer = asset.role == "PRODUCT_FRONT"
        if current is None or (prefer and current.role != "PRODUCT_FRONT"):
            best[asset.product_id] = asset
    return {
        pid: asset_url(storage, a.storage_path) for pid, a in best.items()
    } | {p.id: None for p in products if p.id not in best}


def _product_out(product: Product) -> dict[str, Any]:
    audience = audience_rules.coerce(product.audience)
    return {
        "id": str(product.id),
        "spu": product.spu,
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        # ---- 受众(PRD v2 §13.2:审阅页左栏常驻显示) ----
        #
        # 三个字段一起给,前端一个都不许自己算:
        #   audience         机器读的取值,None = 待确认
        #   audience_label   界面文字(§21.3:文字标签,不用性别符号或
        #                    颜色编码 —— 符号跨站点含义不稳定)
        #   review_focus     本受众的重点检查项(§19)。**从规则包读出后
        #                    下发,前端不许自己维护一份受众到检查项的映射**;
        #                    维护两份的后果是规则包改了检查项,界面还在提示
        #                    旧的那几项,而且不会报错
        "audience": audience.value if audience else None,
        "audience_label": AUDIENCE_LABELS[audience] if audience else "待确认",
        "review_focus": list(wb.review_focus_for(product)),
        # §9.4:受众与品类**同屏确认,不拆两步** —— 它们通常一起对或一起错,
        # 拆开会让运营连点两次"正确",第二次就不看了。所以品类跟着受众一起下发
        "garment_type": product.garment_type,
        "size": product.size,
        "status": product.status,
        "updated_at": iso_utc(product.updated_at),
    }


#: 工作台列表允许排序的字段 -> 取值函数。
#:
#: 键函数而不是列名,因为一半的维度不在 products 表里:`completion` / `blocking_count`
#: / `pending_count` 是 flow_rules 每次现算的判定结果。这也是它们最该能排的原因 ——
#: 「今天从哪件商品下手」这个问题的答案,就藏在阻断数和完成度的排序里。
_LIST_SORT_KEYS = {
    "updated_at": lambda ctx: ctx.product.updated_at,
    "sku": lambda ctx: ctx.product.sku,
    "spu": lambda ctx: ctx.product.spu,
    "name": lambda ctx: ctx.product.name,
    "status": lambda ctx: ctx.product.status,
    "completion": lambda ctx: ctx.result.completion,
    "blocking_count": lambda ctx: ctx.result.blocking_count,
    "pending_count": lambda ctx: ctx.result.pending_count,
    "current_step": lambda ctx: ctx.result.current_step.value,
}


@router.get("/products")
def list_products(
    search: str | None = Query(default=None, description="SPU/SKU/名称模糊搜索"),
    step: str | None = Query(default=None, description="按流程步骤筛选(FE-106)"),
    state: str | None = Query(default=None, description="配合 step:该步骤处于某状态"),
    only_blocked: bool = Query(default=False, description="只看有阻断的"),
    next_action: str | None = Query(default=None, description="按唯一下一步筛选"),
    audience: str | None = Query(
        default=None,
        description=(
            "按商品受众筛选:WOMEN / MEN / UNISEX / UNCONFIRMED(待确认)。"
            "**批量导出闸口的配套动作** —— 批次内规则包不一致时要求"
            "「按受众分成两个批次分别导出」,没有这个参数那句话就没有行动路径"
        ),
    ),
    sort: str | None = Query(
        default=None, description=f"排序字段,可用:{', '.join(_LIST_SORT_KEYS)}"
    ),
    order: str | None = Query(
        default=None, description="asc / desc(也认 antd 的 ascend / descend)"
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(db_session),
    storage=Depends(_storage),
) -> dict[str, Any]:
    """商品列表 + 五子状态 + 阻断数 + 唯一下一步。

    筛选在判定结果上做(`flow_rules.matches_filters`),不翻译成 SQL ——
    分页因此在内存里,首期单商品闭环量级完全撑得住,换来的是筛选结果
    与界面显示状态永远一致(理由见 flow.py)。
    """
    stmt = select(Product).order_by(Product.updated_at.desc())
    if search:
        # 通配符转义走 core/search.py 那一份,四个列表接口共用
        like = like_pattern(search)
        stmt = stmt.where(
            or_(
                Product.spu.ilike(like, escape=ESCAPE_CHAR),
                Product.sku.ilike(like, escape=ESCAPE_CHAR),
                Product.name.ilike(like, escape=ESCAPE_CHAR),
            )
        )
    products = list(session.scalars(stmt))

    # 评审第 19 条:列表是只读的。原来这里 collect 之后主动 commit,
    # 把草稿状态改成 STALE —— 刷新一次页面、浏览器预取一次,业务状态就变了。
    # 判定照常做(界面上仍然显示"已过期"),只是不落库
    contexts = [wb.collect(session, p, dry_run=True) for p in products]
    # **rollback 挪到函数末尾了**,不在这里。理由见下面 `return` 之前那一段:
    # 它会 expire 会话里的每一个 ORM 对象,而这一段后面还要逐行读 `ctx.product`

    # 受众取值不合法要当场 400。`matches_filters` 抛 ValueError 而不是
    # 静默返回全部,理由写在那个函数里;这里负责把它翻成运营看得懂的话
    try:
        matched = [
            ctx
            for ctx in contexts
            if flow_rules.matches_filters(
                ctx.result,
                step=step,
                state=state,
                only_blocked=only_blocked,
                next_action=next_action,
                audience=audience,
                flow=ctx.flow,
            )
        ]
    except ValueError as exc:
        # **不在这里 rollback。** `collect(dry_run=True)` 没有写,而
        # `test_read_only_endpoints_roll_back_after_the_last_orm_read` 要求
        # 本函数**只有一次**只读兜底回滚且紧邻 return —— 多补一次"保险"
        # 等于把 expire 提前,后面每一处 ORM 读又变回逐行往返。
        # 这条路径直接抛,会话由依赖注入的 teardown 收尾
        raise ValidationError(str(exc), code=ErrorCode.INPUT_INVALID) from None
    summary = flow_rules.summarize(ctx.result for ctx in matched)

    # 排序在**全量 matched 上**做,然后才切页 —— 这一点是这次改动的全部意义所在。
    # 上一版界面上没有排序,理由写在 WorkbenchListPage.tsx:在前端挂 antd 的
    # 客户端 sorter,排的是当前这 20 行,而运营以为排的是 500 行。那不是修好了,
    # 是把「没有排序」换成「有排序但结果是错的」。
    #
    # 这一页的排序不需要走 SQL:它的筛选本来就在判定结果上做(见上面的注释),
    # 全量已经在内存里了。而且「完成度」「阻断数」这两个最该排的维度根本不是列,
    # 是 flow_rules 现算出来的 —— 想用 SQL 排就得先把判定逻辑翻译成 SQL,
    # 那正是 flow.py 明确拒绝的那件事(筛选结果与界面显示状态必须永远一致)。
    sort_field, direction = normalize_sort(
        sort, order, allowed=_LIST_SORT_KEYS, default_sort="updated_at", default_order="desc"
    )
    matched = sort_in_memory(
        matched,
        keys=_LIST_SORT_KEYS,
        sort=sort_field,
        order=direction,
        # 兜底键用 SKU 而不是 id:它唯一(uq_products_sku),而且顺序对人有意义 ——
        # 按阻断数排的时候,数目相同的那几十件按 SKU 排列,运营翻页时找得回位置。
        tiebreak=lambda ctx: ctx.product.sku,
    )

    start = (page - 1) * page_size
    page_items = matched[start : start + page_size]
    thumbs = _thumbnails(session, [ctx.product for ctx in page_items], storage)

    payload = {
        "summary": summary,
        "total": len(matched),
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "product": _product_out(ctx.product),
                "thumbnail_url": thumbs.get(ctx.product.id),
                "flow": wb.serialize_flow(ctx.result),
            }
            for ctx in page_items
        ],
    }
    # 只读接口的兜底回滚。**必须在最后一次 ORM 读之后**,不能提前。
    #
    # `Session.rollback()` 会把会话里**所有**对象置为 expired(SQLAlchemy 文档:
    # "All objects not expunged are fully expired")。`expire_on_commit=False`
    # 只管 commit,管不到它。而 expired 对象的下一次属性访问会**隐式发一条 SELECT**。
    #
    # 原来这一句在 `collect()` 之后,于是它下面每一处 `ctx.product.<列>` 都是
    # 一次往返 —— 最贵的是 `sort_in_memory` 的 tiebreak(`ctx.product.sku`),
    # 它跑在**全量 matched** 上而不是当前页:500 个 SKU = 500 条 SELECT,
    # 每次打开工作台都发生一遍。
    #
    # 这类退化 `tests/pure/test_workbench_query_budget.py` 结构上看不见:
    # 它数的是源码里的 `session.*` 调用点,而这里一个都没有,查询是属性访问
    # 触发的。那个文件自己写着"它不是实测,是上界" —— 这一条正落在上界之外。
    #
    # 挪到这里之后保护只多不少:`_thumbnails` 万一将来写了什么,也一并回滚。
    session.rollback()
    return payload


def _copy_out(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row.id),
        "locale": row.locale,
        "version": row.version,
        "status": row.status,
        "title": row.title,
        "bullet_points": list(row.bullet_points or []),
        "description": row.description,
        "keywords": list(row.keywords or []),
        "claims": list(row.claims or []),
        "violations": list(row.violations or []),
        "model_name": row.model_name,
        "approved_by": row.approved_by,
        "approved_at": iso_utc(row.approved_at),
        # A10:状态是 REJECTED 时,界面要能说清是规则驳回还是人退回的。
        # 判据是这一项有没有值 —— 两者共用一个状态值(见 models/listing_copy.py)
        "reject_reason": row.reject_reason,
        "reject_note": row.reject_note,
        "rejected_at": iso_utc(row.rejected_at),
    }


def _draft_out(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": str(row.id),
        "status": row.status,
        "manual_payload": dict(row.manual_payload or {}),
        "validation_errors": list(row.validation_errors or []),
        "validation_warnings": list(row.validation_warnings or []),
        "spec_version": row.spec_version,
        "mapping_version": row.mapping_version,
        "fingerprint": row.source_fingerprint,
        "exported_at": iso_utc(row.exported_at),
        "exported_by": row.exported_by,
        "export_count": row.export_count or 0,
        "updated_at": iso_utc(row.updated_at),
    }


@router.get("/products/{product_id}/flow")
def product_flow(
    product_id: UUID,
    session: Session = Depends(db_session),
    storage=Depends(_storage),
) -> dict[str, Any]:
    """单商品详情:判定结论 + 各标签页要展开的对象。"""
    product = _product(session, product_id)
    ctx = wb.collect(session, product, dry_run=True)

    draft_block: dict[str, Any] | None = _draft_out(ctx.draft)
    if ctx.draft is not None:
        draft_block = {
            **(draft_block or {}),
            "preview": wb.draft_preview(session, product, ctx.draft),
            "stale": wb.stale_reason(session, product, ctx.draft, dry_run=True),
            # 受众闸口的警告档(§17 兼容缝)。problems 会抛异常挡住导出,
            # warnings 只能靠这里下发 —— 之前它算出来就被丢掉了
            "audience_warnings": wb.audience_gate_warnings(product, ctx.draft),
        }
    payload = {
        "product": _product_out(product),
        "thumbnail_url": _thumbnails(session, [product], storage).get(product.id),
        "flow": wb.serialize_flow(ctx.result),
        "image_set": (
            {
                "id": str(ctx.image_set.id),
                "version": ctx.image_set.version,
                "status": ctx.image_set.status,
            }
            if ctx.image_set
            else None
        ),
        "copy": _copy_out(ctx.copy),
        "draft": draft_block,
    }
    # 评审第 19 条:详情页是只读的。**位置和列表页同一个理由** ——
    # rollback 会 expire 全部 ORM 对象,放在出参组装之前的话,下面每一个
    # `product.` / `ctx.image_set.` / `ctx.copy.` 都各自再打一次库
    session.rollback()
    return payload


# ---------------------------------------------------------------- 文案


class CopyGenerateIn(BaseModel):
    """FE-221 整体生成 / FE-222 单字段重生。"""

    only_fields: list[str] | None = Field(
        default=None, description="只重生这些字段;空 = 整篇生成"
    )


class CopySaveIn(BaseModel):
    """FE-222 人工编辑。只传要改的字段。"""

    title: str | None = None
    bullet_points: list[str] | None = None
    description: str | None = None
    keywords: list[str] | None = None


@router.post("/products/{product_id}/copy/generate")
def generate_copy(
    product_id: UUID,
    payload: CopyGenerateIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    product = _product(session, product_id)
    row = wb.generate_copy(
        session,
        product,
        only_fields=payload.only_fields or None,
        actor=current_actor(request),
    )
    session.commit()
    return {"copy": _copy_out(row)}


@router.post("/products/{product_id}/copy/save")
def save_copy(
    product_id: UUID,
    payload: CopySaveIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    product = _product(session, product_id)
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    row = wb.save_copy_manual(
        session, product, fields=fields, actor=current_actor(request)
    )
    session.commit()
    return {"copy": _copy_out(row)}


@router.post("/products/{product_id}/copy/{copy_id}/approve")
def approve_copy(
    product_id: UUID,
    copy_id: UUID,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    product = _product(session, product_id)
    from app.listings import copy_service

    # `expect_spu` 是 A1 那条缺陷的同形补丁:两个路径参数以前从不互相印证,
    # 拿 A 的 copy_id 请求 B 的路径批准的是 A 的文案。守卫在服务层
    # (`_load_for_spu`),这里只负责把商品的 SPU 递过去
    row = copy_service.approve_copy(
        session, copy_id, actor=current_actor(request), expect_spu=product.spu
    )
    session.commit()
    return {"copy": _copy_out(row)}


class CopyRejectIn(BaseModel):
    """快审退回文案(A10)。取值合法性由服务层判,理由同 `ImageSetRejectIn`。"""

    reason: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=reject_rules.MAX_NOTE_LENGTH)


@router.post("/products/{product_id}/copy/{copy_id}/reject")
def reject_copy(
    product_id: UUID,
    copy_id: UUID,
    payload: CopyRejectIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    product = _product(session, product_id)
    from app.listings import copy_service

    row = copy_service.reject_copy(
        session,
        copy_id,
        reason=payload.reason,
        note=payload.note,
        actor=current_actor(request),
        expect_spu=product.spu,
    )
    session.commit()
    return {"copy": _copy_out(row)}


@router.get("/reject-reasons")
def reject_reasons(
    target: str = Query("image_set", pattern="^(image_set|copy)$"),
) -> dict[str, Any]:
    """退回原因清单(A10)。

    ## 为什么这是一个接口而不是前端的一张常量表

    这九个原因有三样东西必须和后端一致:中文、哪些适用于这一步、
    以及**退回之后的下一步**。前端抄一份的后果不是显示错字,
    是运营选了「商品结构改变」,界面说"接下来去重排图片",
    而系统实际把他送去改属性 —— 两个说法都出自本系统,他会信界面那个。

    §3.2.1 的纪律是唯一下一步由后端算、前端只翻译。这条同样适用于
    "选这个原因会发生什么"。
    """
    return {"target": target, "reasons": reject_rules.describe(target)}


# ---------------------------------------------------------------- 草稿与导出


class DraftBuildIn(BaseModel):
    """FE-231 生成草稿。手填字段(品牌/价格/币种/库存)一并提交。"""

    manual: dict[str, Any] | None = None


@router.post("/products/{product_id}/draft")
def build_draft(
    product_id: UUID,
    payload: DraftBuildIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    product = _product(session, product_id)
    row = wb.build_draft(
        session, product, manual=payload.manual, actor=current_actor(request)
    )
    preview = wb.draft_preview(session, product, row)
    session.commit()
    return {"draft": {**(_draft_out(row) or {}), "preview": preview}}


@router.get("/products/{product_id}/draft/stale-reason")
def draft_stale_reason(
    product_id: UUID, session: Session = Depends(db_session)
) -> dict[str, Any]:
    """BE-205:哪个上游变了、变了哪些字段、该做什么(§4.5.1)。"""
    product = _product(session, product_id)
    from app.workbench.service import _current_draft

    row = _current_draft(session, product.spu)
    if row is None:
        raise NotFoundError("还没有生成上架草稿")
    result = wb.stale_reason(session, product, row, dry_run=True)
    # 评审第 19 条:「为什么过期」是一个查询。回答它不该改状态
    session.rollback()
    return result


@router.post("/products/{product_id}/export")
def export_draft(
    product_id: UUID,
    request: Request,
    fmt: str = Query(default="xlsx", pattern="^(xlsx|csv)$"),
    session: Session = Depends(db_session),
) -> Response:
    """导出上架文件。过期与校验不过一律 409,不产出文件。"""
    product = _product(session, product_id)
    payload, filename, mime = wb.export_draft(
        session, product, fmt=fmt, actor=current_actor(request)
    )
    session.commit()
    return Response(
        content=payload,
        media_type=mime,
        headers=download_headers(filename),
    )


@router.get("/products/{product_id}/exports")
def export_history(
    product_id: UUID, session: Session = Depends(db_session)
) -> dict[str, Any]:
    """导出历史(§5.2 步骤 13:旧导出记录仍可追溯)。数据源是审计日志。"""
    product = _product(session, product_id)
    from app.workbench.service import _current_draft

    row = _current_draft(session, product.spu)
    if row is None:
        return {"items": []}
    entries = list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "ListingDraft",
                AuditLog.entity_id == row.id,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(50)
        )
    )
    return {
        "items": [
            {
                "actor": e.actor,
                "action": (e.payload or {}).get("action") or e.action,
                "at": iso_utc(e.created_at),
                "payload": e.payload or {},
            }
            for e in entries
            if (e.payload or {}).get("action") in ("export", "build")
        ]
    }


# ---------------------------------------------------------------- 平台侧闭环(P1)


class PlatformStatusIn(BaseModel):
    """手工更新平台侧状态(NONE/SUBMITTED/LIVE,不含 REJECTED)。"""

    status: str = Field(description="目标状态;REJECTED 只能由记录驳回接口写入")


class RecordRejectionIn(BaseModel):
    """录入一条平台驳回。"""

    reason_code: str = Field(description="驳回原因码,见 platform.RejectionReason")
    platform_note: str | None = Field(default=None, description="平台原话照贴")
    export_fingerprint: str | None = Field(
        default=None, description="指定哪次导出;不传 = 最近一次"
    )


class ResolveRejectionIn(BaseModel):
    """关闭一条驳回。A4:闸门没过时,`confirmed` 与 `note` 至少要有一个。"""

    note: str | None = Field(default=None, description="解决备注;闸门没过时落进 resolved_note")
    confirmed: bool = Field(
        default=False,
        description="运营勾选「已修改并重新上传」。闸门没过且没填说明时,靠它放行",
    )


@router.post("/products/{product_id}/platform-status")
def set_platform_status(
    product_id: UUID,
    payload: PlatformStatusIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """手工更新平台侧状态。只允许合法转移,REJECTED 不开手工通道。"""
    from app.workbench import platform_service as ps
    from app.workbench.service import _current_draft

    product = _product(session, product_id)
    row = _current_draft(session, product.spu)
    if row is None:
        raise from_app_error(NotFoundError("还没有生成上架草稿"))
    updated = ps.update_platform_status(
        session, row, to_status=payload.status, actor=current_actor(request)
    )
    session.commit()
    return {"platform_status": updated.platform_status}


@router.post("/products/{product_id}/rejections")
def record_rejection(
    product_id: UUID,
    payload: RecordRejectionIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """录入平台驳回。同时把平台状态置 REJECTED、驳回件进阻断列表。"""
    from app.workbench import platform_service as ps
    from app.workbench.service import _current_draft

    product = _product(session, product_id)
    row = _current_draft(session, product.spu)
    if row is None:
        raise from_app_error(NotFoundError("还没有生成上架草稿,无法记录驳回"))
    rejection = ps.record_rejection(
        session,
        row,
        product,
        reason_code=payload.reason_code,
        platform_note=payload.platform_note,
        wanted_fingerprint=payload.export_fingerprint,
        actor=current_actor(request),
    )
    session.commit()
    return {"rejection": ps.serialize_rejection(rejection)}


@router.post("/products/{product_id}/platform-resubmitted")
def mark_resubmitted(
    product_id: UUID,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """确认"驳回改完的新文件已经重新传上平台"(评审第 7 条)。

    独立动作,不再是"关闭最后一条驳回"的副作用。系统看不见平台侧发生了
    什么,所以这句话只能由人明确地说一次 —— 那就给它一个自己的按钮和
    一条自己的审计记录。
    """
    from app.workbench import platform_service as ps
    from app.workbench.service import _current_draft

    product = _product(session, product_id)
    row = _current_draft(session, product.spu)
    if row is None:
        raise from_app_error(NotFoundError("找不到关联的草稿行"))
    updated = ps.mark_resubmitted(session, row, actor=current_actor(request))
    session.commit()
    return {"platform_status": updated.platform_status}


@router.get("/products/{product_id}/rejections")
def list_rejections(
    product_id: UUID,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """该商品的全部驳回记录(含已解决)。导出页「平台驳回」面板用它。"""
    from app.workbench import platform_service as ps
    from app.workbench.service import _current_draft

    product = _product(session, product_id)
    row = _current_draft(session, product.spu)
    if row is None:
        return {"items": [], "open_count": 0}
    items = ps.all_rejections(session, row.id)
    open_count = sum(1 for i in items if i.status == "OPEN")
    # A4:每条 OPEN 驳回带上"还缺哪一步"。只读,不写(A6)
    gates = ps.resolve_gates(session, row, items)
    serialized = []
    for r in items:
        data = ps.serialize_rejection(r)
        data["resolve_gate"] = gates.get(str(r.id))
        serialized.append(data)
    return {
        "items": serialized,
        "open_count": open_count,
        "platform_status": getattr(row, "platform_status", "NONE"),
    }


@router.post("/products/{product_id}/rejections/{rejection_id}/resolve")
def resolve_rejection(
    product_id: UUID,
    rejection_id: UUID,
    payload: ResolveRejectionIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """把一条驳回标记为已解决。全部解决后平台状态自动退回 SUBMITTED。

    A1:`rejection_id` 必须属于**这个商品当前草稿**。用商品 A 的 rejection_id
    请求商品 B 的这个接口曾经能同时改到两件商品(关掉 A 的驳回、按 B 的草稿
    重算平台状态),留下 A「REJECTED 但无 OPEN 驳回」的不一致态。
    因此查询条件用 rejection_id + draft_id 联合,不匹配一律 404 且不产生写入。
    """
    from sqlalchemy import select as sa_select

    from app.models.platform_rejection import PlatformRejection
    from app.workbench import platform_service as ps
    from app.workbench.service import _current_draft

    product = _product(session, product_id)
    row = _current_draft(session, product.spu)
    if row is None:
        raise from_app_error(NotFoundError("找不到关联的草稿行"))
    rejection = session.scalars(
        sa_select(PlatformRejection).where(
            PlatformRejection.id == rejection_id,
            PlatformRejection.draft_id == row.id,
        )
    ).first()
    if rejection is None:
        # 不区分"不存在"与"不属于这个商品":两者对调用方都是同一个结论,
        # 分开说等于告诉外部这个 ID 在别处存在
        raise from_app_error(NotFoundError("驳回记录不存在,或不属于这个商品的当前草稿"))
    resolved = ps.resolve_rejection(
        session,
        rejection,
        row,
        note=payload.note,
        confirmed=payload.confirmed,
        actor=current_actor(request),
    )
    session.commit()
    return {"rejection": ps.serialize_rejection(resolved)}
