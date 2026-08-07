"""商品与素材 REST 接口(需求第十六章阶段 2 部分)。"""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import (
    current_actor,
    db_session,
    read_within_limit,
    require_operator,
)
from app.core.config import settings
from app.core.enums import AssetType, GarmentType, ProductStatus
from app.core.errors import ErrorCode, ValidationError
from app.schemas.asset import AssetOut, AssetUploadResult
from app.schemas.common import Page
from app.schemas.product import ImportResultOut, ProductCreate, ProductOut, ProductUpdate
from app.services import asset_service, product_service
from app.services.product_import import parse_csv, parse_json_rows
from app.services.storage import asset_url, build_storage

# 整个路由挂 require_operator(读接口也要)。为什么挂在路由器上见 `deps.require_operator`
router = APIRouter(
    prefix="/products",
    tags=["products"],
    dependencies=[Depends(require_operator)],
)


#: 上传体先读进内存再校验,因此必须限制单次读取量
MAX_IMPORT_BYTES = 5 * 1024 * 1024


def get_storage():
    return build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )


def _to_out(product, asset_count: int = 0) -> ProductOut:
    out = ProductOut.model_validate(product)
    out.asset_count = asset_count
    return out


def _asset_out(asset, storage) -> AssetOut:
    out = AssetOut.model_validate(asset)
    # 上传的原始素材是私有的 —— 走 asset_url 拿短期签名地址,不是永久公开地址
    out.url = asset_url(storage, asset.storage_path)
    return out


@router.post(
    "",
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator)],
)
def create_product(
    payload: ProductCreate,
    request: Request,
    session: Session = Depends(db_session),
) -> ProductOut:
    product = product_service.create_product(
        session, payload.model_dump(), actor=current_actor(request)
    )
    session.commit()
    return _to_out(product)


@router.get("", response_model=Page[ProductOut])
def list_products(
    session: Session = Depends(db_session),
    search: str | None = Query(None, description="按 SKU / SPU / 名称模糊匹配"),
    status_filter: ProductStatus | None = Query(None, alias="status"),
    category: str | None = Query(None),
    garment_type: GarmentType | None = Query(None),
    sort: str | None = Query(
        None, description=f"排序字段,可用:{', '.join(product_service.SORTABLE)}"
    ),
    order: str | None = Query(None, description="asc / desc(也认 antd 的 ascend / descend)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Page[ProductOut]:
    rows, total = product_service.list_products(
        session,
        search=search,
        status=status_filter.value if status_filter else None,
        category=category,
        garment_type=garment_type.value if garment_type else None,
        sort=sort,
        order=order,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    counts = product_service.asset_counts(session, [r.id for r in rows])
    return Page[ProductOut](
        items=[_to_out(r, counts.get(r.id, 0)) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/import", response_model=ImportResultOut, dependencies=[Depends(require_operator)])
async def import_products(
    request: Request,
    session: Session = Depends(db_session),
    file: UploadFile | None = File(None, description="CSV 文件,UTF-8 编码"),
) -> ImportResultOut:
    """批量导入。支持 multipart CSV 上传,或直接 POST JSON 数组。

    重复 SKU 记为 skipped 而非失败,重复执行安全。
    """
    if file is not None:
        raw = await file.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            raise ValidationError("导入文件超过 5 MB", code=ErrorCode.FILE_TOO_LARGE)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("导入文件必须是 UTF-8 编码的 CSV") from exc
        parsed = parse_csv(text)
    else:
        # JSON 路径以前直接 await request.body(),没有任何上限:
        # 一个几百 MB 的请求体会在解析之前就把进程内存吃掉。
        # 与 CSV 走同一个上限,先看 Content-Length,再按实际读到的字节数复核 ——
        # Content-Length 可以撒谎,分块传输甚至可以不给。
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_IMPORT_BYTES:
            raise ValidationError("导入内容超过 5 MB", code=ErrorCode.FILE_TOO_LARGE)

        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_IMPORT_BYTES:
                raise ValidationError("导入内容超过 5 MB", code=ErrorCode.FILE_TOO_LARGE)
            chunks.append(chunk)
        body = b"".join(chunks)

        if not body:
            raise ValidationError("请上传 CSV 文件或提交 JSON 数组")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValidationError("请求体不是合法 JSON") from exc
        if not isinstance(payload, list):
            raise ValidationError("JSON 导入必须是数组")
        parsed = parse_json_rows(payload)

    result = product_service.import_products(session, parsed, actor=current_actor(request))
    # 逐行保存点在 service 里,整批的事务边界在这里。错误行已经在它自己的
    # 保存点上回滚过了,这一次提交落的是**正确行** —— 「错误行不影响正确行」
    # 靠的就是这两级的分工
    session.commit()
    return ImportResultOut(**result)


@router.get("/{product_id}", response_model=ProductOut)
def get_product(product_id: UUID, session: Session = Depends(db_session)) -> ProductOut:
    product = product_service.get_product(session, product_id)
    counts = product_service.asset_counts(session, [product.id])
    return _to_out(product, counts.get(product.id, 0))


@router.patch("/{product_id}", response_model=ProductOut, dependencies=[Depends(require_operator)])
def update_product(
    product_id: UUID,
    payload: ProductUpdate,
    request: Request,
    session: Session = Depends(db_session),
) -> ProductOut:
    product = product_service.update_product(
        session,
        product_id,
        payload.model_dump(exclude_unset=True),
        actor=current_actor(request),
    )
    session.commit()
    return _to_out(product)


@router.post(
    "/{product_id}/assets",
    response_model=AssetUploadResult,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator)],
)
async def upload_asset(
    product_id: UUID,
    request: Request,
    session: Session = Depends(db_session),
    file: UploadFile = File(...),
    asset_type: AssetType = Form(...),
    #: 这张图是哪个颜色的样品(§4.8)。**表单可选** —— 不给就是通用图,
    #: 与本参数存在之前的行为逐字相同。
    #:
    #: 校验落在服务层而不是这里:一个不属于本商品所在 SPU 的颜色 id 是
    #: **语义**错误,不是格式错误,而 §4.3 那条「跨 SPU 同名颜色是常态」
    #: 意味着光看 UUID 分不出它属于谁
    color_variant_id: UUID | None = Form(None),
    storage=Depends(get_storage),
) -> AssetUploadResult:
    product = product_service.get_product(session, product_id)
    if color_variant_id is not None:
        product_service.assert_colour_belongs_to(session, product, color_variant_id)

    limit = settings.max_upload_bytes
    data = await read_within_limit(file, limit)

    asset, deduplicated = asset_service.upload_asset(
        session,
        product=product,
        data=data,
        filename=file.filename or "upload",
        asset_type=asset_type,
        storage=storage,
        actor=current_actor(request),
        color_variant_id=color_variant_id,
    )
    # 字节已经落到存储层了,这一次提交决定的是「库里认不认这张图」。
    # 提交失败时存储里会留一个没有素材行的孤儿文件 —— 与提交成功后
    # 才写文件相比,这个方向是刻意选的:孤儿文件由清理任务收,
    # 而反过来(库里有行、文件不在)会让详情页 404 且无从补救
    session.commit()
    return AssetUploadResult(asset=_asset_out(asset, storage), deduplicated=deduplicated)


@router.get("/{product_id}/assets", response_model=list[AssetOut])
def list_assets(
    product_id: UUID,
    session: Session = Depends(db_session),
    storage=Depends(get_storage),
) -> list[AssetOut]:
    product_service.get_product(session, product_id)  # 不存在则 404
    return [_asset_out(a, storage) for a in asset_service.list_assets(session, product_id)]
