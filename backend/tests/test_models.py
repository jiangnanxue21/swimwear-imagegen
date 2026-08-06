"""ORM 模型测试。需要真实 PostgreSQL。"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.enums import AssetType, ProductStatus
from app.models.product import Product
from app.models.product_asset import ProductAsset
from tests.conftest import requires_db

pytestmark = requires_db


def _product(sku: str = "SKU-1") -> Product:
    return Product(spu="SPU-1", sku=sku, name="测试泳衣")


def test_product_gets_uuid_and_timestamps(session):
    p = _product()
    session.add(p)
    session.flush()
    assert p.id is not None
    assert p.status == ProductStatus.DRAFT.value
    session.refresh(p)
    assert p.created_at is not None


def test_sku_is_unique(session):
    session.add(_product("SKU-DUP"))
    session.flush()
    session.add(Product(spu="SPU-2", sku="SKU-DUP", name="另一个"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_secondary_colors_roundtrip_as_jsonb(session):
    p = _product("SKU-COLOR")
    p.secondary_colors = ["white", "gold"]
    session.add(p)
    session.flush()
    session.refresh(p)
    assert p.secondary_colors == ["white", "gold"]


def _asset(product_id, file_hash="a" * 64) -> ProductAsset:
    return ProductAsset(
        product_id=product_id,
        asset_type=AssetType.GARMENT_FRONT.value,
        file_hash=file_hash,
        mime_type="image/jpeg",
        file_size=1024,
        width=800,
        height=1200,
        storage_path=f"uploads/aa/aa/{file_hash}.jpg",
        original_filename="front.jpg",
    )


def test_same_hash_cannot_be_attached_twice_to_one_product(session):
    p = _product("SKU-ASSET")
    session.add(p)
    session.flush()
    session.add(_asset(p.id))
    session.flush()
    session.add(_asset(p.id))
    with pytest.raises(IntegrityError):
        session.flush()


def test_same_hash_can_belong_to_different_products(session):
    """同一素材可复用于不同商品 —— 去重键是 (product_id, sha256) 而非 sha256。

    断言两行真的都落库了。原来这里只有一句"flush 不应报错"的注释:
    autoflush 已经发生过时 `flush()` 是空操作,于是这条用例会静默通过 ——
    而它守的是一个**不该报错**的场景,没有异常可以指望。
    """
    a, b = _product("SKU-A"), _product("SKU-B")
    session.add_all([a, b])
    session.flush()
    session.add_all([_asset(a.id), _asset(b.id)])
    session.flush()

    owners = {
        row.product_id
        for row in session.query(ProductAsset).filter(
            ProductAsset.product_id.in_([a.id, b.id])
        )
    }
    assert owners == {a.id, b.id}


def test_deleting_product_cascades_to_assets(session):
    p = _product("SKU-CASCADE")
    session.add(p)
    session.flush()
    session.add(_asset(p.id))
    session.flush()
    session.delete(p)
    session.flush()
    assert session.query(ProductAsset).filter_by(product_id=p.id).count() == 0
