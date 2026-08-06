"""成品图导出接口(需求第十五、十六章)。

    GET /api/exports/products/{id}          单个商品,JSON
    GET /api/exports/products/{id}?format=csv
    GET /api/exports/products?format=csv    批量,支持与商品列表相同的筛选

需求第十六章只点名了单商品导出;批量那条是顺手加的 ——
上架从来不是一个 SKU 一个 SKU 做的,只给单个导出等于逼运营写脚本。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import db_session, require_operator
from app.core.config import settings
from app.core.enums import GarmentType, ProductStatus
from app.core.http_headers import download_headers
from app.services import export_service, product_service
from app.services.storage import build_storage

# 整个路由挂 require_operator(读接口也要)。为什么挂在路由器上见 `deps.require_operator`
router = APIRouter(tags=["export"], dependencies=[Depends(require_operator)])

#: 一次最多导出多少商品。没有上限的导出接口迟早会被人拿来拖全库
MAX_EXPORT_ROWS = 1000


def _storage():
    return build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )


def _csv_response(csv_text: str, filename: str) -> Response:
    # utf-8-sig:让 Excel 双击打开不乱码
    #
    # 文件名走 `download_headers`:SKU 可以是中文(schema 上只限长度不限字符集),
    # 而 HTTP 头按 latin-1 编码 —— 直接拼进去会抛 UnicodeEncodeError,
    # 表现是整个导出接口 500,且错误信息完全不指向文件名
    return Response(
        content=csv_text.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers=download_headers(filename),
    )


@router.get("/exports/products/{product_id}")
def export_product(
    product_id: UUID,
    session: Session = Depends(db_session),
    export_format: str = Query("json", alias="format", pattern="^(json|csv)$"),
):
    payload = export_service.export_product(session, product_id, _storage())
    if export_format == "csv":
        csv_text = export_service.to_csv([payload])
        return _csv_response(csv_text, f"{payload['product']['sku']}-images.csv")
    return payload


@router.get("/exports/products")
def export_products(
    session: Session = Depends(db_session),
    export_format: str = Query("json", alias="format", pattern="^(json|csv)$"),
    search: str | None = Query(None),
    status_filter: ProductStatus | None = Query(None, alias="status"),
    category: str | None = Query(None),
    garment_type: GarmentType | None = Query(None),
    only_complete: bool = Query(False, description="只导出五个用途齐全的商品"),
    offset: int = Query(0, ge=0, description="从第几条开始,配合 total 翻页"),
    limit: int = Query(200, ge=1, le=MAX_EXPORT_ROWS),
):
    """批量导出。

    ## `only_complete` 必须在分页**之前**生效(BLOCK-17)

    原来的顺序是「取前 limit 件 → 组装 → 丢掉不完整的」。后果是:
    库里 1000 件商品、其中 200 件图齐了,导出时取前 200 件(按创建时间倒序)
    发现只有 30 件完整,于是这个接口返回 30 条 —— 而另外 170 件**合法可导出**
    的商品一条都没出现,且返回体里没有任何迹象说明它们被截掉了。
    运营看到的是"能导的就这些"。

    `total` 同理:原来是 `len(payloads)`,也就是"这一页过滤后剩几条",
    而不是符合条件的总数。任何按 total 翻页的调用方都会漏数据。

    现在的顺序是「筛(含完整性)→ 数总数 → 分页 → 组装」,完整性判据下推到
    数据库(`output_service.products_with_all_purposes`)。

    ## CSV 仍然只导当前页

    不因为格式是 CSV 就绕开分页上限:那正是"没有上限的导出接口迟早被拿来
    拖全库"这条注释要挡的事。要全量的话按 total 翻页。
    """
    complete_filter = export_service.complete_product_filter() if only_complete else None
    rows, total = product_service.list_products(
        session,
        search=search,
        status=status_filter.value if status_filter else None,
        category=category,
        garment_type=garment_type.value if garment_type else None,
        id_in=complete_filter,
        offset=offset,
        limit=limit,
    )
    payloads = export_service.export_products(session, rows, _storage())

    if export_format == "csv":
        return _csv_response(export_service.to_csv(payloads), "product-images.csv")
    return {
        "items": payloads,
        "total": total,
        "offset": offset,
        "limit": limit,
        # 明确告诉调用方"还有没有下一页"。让前端自己拿 offset+len 和 total
        # 比大小的话,每个调用方都要写一遍同一个比较,而写错的表现是少导数据
        "has_more": offset + len(payloads) < total,
    }
