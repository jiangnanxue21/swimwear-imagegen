"""成品图导出(需求第十五、十六章 / 阶段 6)。

网站要的是"这个 SKU 各用途的图 URL",导出就是把它变成能直接喂给
上架流程或 CMS 的两种格式:JSON 给程序,CSV 给人和表格。

行形状与 CSV 生成在 `export_format` 里,那部分是纯函数、有独立测试;
这里只负责从数据库把数据取出来。
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.product import Product
from app.services import output_service
from app.services.export_format import (  # noqa: F401  (对外保持同一入口)
    CSV_HEADER,
    PURPOSE_COLUMNS,
    is_complete,
    to_csv,
)
from app.services.storage import ObjectStorage


def product_payload(session: Session, product: Product, storage: ObjectStorage) -> dict[str, Any]:
    """单个商品的导出结构(JSON 形态)。"""
    assets = output_service.list_outputs(session, product.id, enabled_only=True)
    return _payload_from_outputs(product, assets, storage)


def _payload_from_outputs(
    product: Product, assets: list[Any], storage: ObjectStorage
) -> dict[str, Any]:
    """已经取好成品图之后的组装。单件与批量共用同一份形状。

    拆出来是为了让批量路径能一次取完全部输出再分桶,而不必为了省查询
    就在批量那边复制一遍字段列表 —— 复制件会漂移,而漂移的表现是
    "单个导出的 CSV 和批量导出的 CSV 列不一样"。
    """
    images = output_service.public_urls(assets, storage)

    return {
        "product": {
            "id": str(product.id),
            "spu": product.spu,
            "sku": product.sku,
            "name": product.name,
            "category": product.category,
            "garment_type": product.garment_type,
            "primary_color": product.primary_color,
            "pattern_type": product.pattern_type,
            "status": product.status,
        },
        "images": images,
        "image_count": len(images),
        "complete": is_complete(images),
    }


def export_product(session: Session, product_id: UUID, storage: ObjectStorage) -> dict[str, Any]:
    from app.services.product_service import get_product

    return product_payload(session, get_product(session, product_id), storage)


def export_products(
    session: Session, products: list[Product], storage: ObjectStorage
) -> list[dict[str, Any]]:
    """批量导出。**一次查询取回全部商品的成品图**,不逐件查(报告 6.14)。

    原来这里是 `[product_payload(session, p, storage) for p in products]`,
    每件商品一条 `list_outputs` 查询 —— 导 200 件就是 201 条。真实数据量下
    这个接口会慢到被网关掐断,而掐断的表现是"导出偶尔失败",不指向任何原因。

    排序与"同一用途出现多条时谁赢"仍然交给 `output_service`,这里只负责
    把结果按商品分桶 —— 否则"谁赢"就有了第二份实现。
    """
    if not products:
        return []

    by_product = output_service.outputs_for_products(
        session, [p.id for p in products], enabled_only=True
    )
    return [
        _payload_from_outputs(p, by_product.get(p.id, []), storage) for p in products
    ]


def complete_product_filter():
    """"五个用途齐全"的筛选条件,给 `only_complete` 用。

    返回一条**子查询**交给 `list_products`,而不是先把 id 全捞出来:
    判据与 `export_format.is_complete` 同源(都是 `OutputPurpose` 的全集),
    但整个筛选留在数据库里,规模不影响它。
    """
    return output_service.complete_products_query()
