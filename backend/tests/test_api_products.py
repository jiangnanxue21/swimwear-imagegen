"""商品 API 测试。需要真实 PostgreSQL 与 FastAPI TestClient。"""
from __future__ import annotations

import io

from PIL import Image

from tests.conftest import requires_db

pytestmark = requires_db

PAYLOAD = {
    "spu": "SPU-100",
    "sku": "SKU-100",
    "name": "高腰比基尼",
    "primary_color": "black",
    "secondary_colors": ["gold"],
    "garment_type": "BIKINI_SET",
    "pattern_type": "SOLID",
}


def _image(width=800, height=1200, fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 90, 120)).save(buf, format=fmt)
    return buf.getvalue()


def test_create_and_get_product(client):
    r = client.post("/api/products", json=PAYLOAD)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    got = client.get(f"/api/products/{pid}")
    assert got.status_code == 200
    assert got.json()["sku"] == "SKU-100"
    assert got.json()["status"] == "DRAFT"


def test_duplicate_sku_returns_409(client):
    client.post("/api/products", json=PAYLOAD)
    r = client.post("/api/products", json=PAYLOAD)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_RESOURCE"


def test_invalid_enum_returns_422(client):
    bad = {**PAYLOAD, "sku": "SKU-BAD", "garment_type": "BANANA"}
    assert client.post("/api/products", json=bad).status_code == 422


def test_missing_product_returns_404(client):
    r = client.get("/api/products/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_patch_only_updates_given_fields(client):
    pid = client.post("/api/products", json=PAYLOAD).json()["id"]
    r = client.patch(f"/api/products/{pid}", json={"material": "再生涤纶"})
    assert r.status_code == 200
    assert r.json()["material"] == "再生涤纶"
    assert r.json()["name"] == PAYLOAD["name"]  # 未传的字段不变


def test_list_supports_search_and_pagination(client):
    for i in range(3):
        client.post("/api/products", json={**PAYLOAD, "sku": f"SKU-L{i}", "name": f"连体泳衣{i}"})
    r = client.get("/api/products", params={"search": "连体", "page_size": 2})
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_csv_import_creates_products(client):
    csv_text = (
        "spu,sku,name,garment_type\n"
        "SPU-I1,SKU-I1,导入商品1,ONE_PIECE\n"
        "SPU-I2,SKU-I2,导入商品2,TANKINI\n"
    )
    r = client.post(
        "/api/products/import",
        files={"file": ("p.csv", csv_text.encode("utf-8"), "text/csv")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2


def test_reimport_is_idempotent(client):
    csv_text = "spu,sku,name\nSPU-R,SKU-R,重复导入\n"
    files = {"file": ("p.csv", csv_text.encode("utf-8"), "text/csv")}
    assert client.post("/api/products/import", files=files).json()["created"] == 1
    second = client.post(
        "/api/products/import",
        files={"file": ("p.csv", csv_text.encode("utf-8"), "text/csv")},
    ).json()
    assert second["created"] == 0
    assert second["skipped_existing"] == 1


def test_asset_upload_and_dedup(client):
    pid = client.post("/api/products", json={**PAYLOAD, "sku": "SKU-UP"}).json()["id"]
    img = _image()
    files = {"file": ("front.jpg", img, "image/jpeg")}
    r = client.post(
        f"/api/products/{pid}/assets", files=files, data={"asset_type": "GARMENT_FRONT"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["deduplicated"] is False
    assert r.json()["asset"]["width"] == 800

    again = client.post(
        f"/api/products/{pid}/assets",
        files={"file": ("front-copy.jpg", img, "image/jpeg")},
        data={"asset_type": "GARMENT_FRONT"},
    )
    assert again.json()["deduplicated"] is True
    assert len(client.get(f"/api/products/{pid}/assets").json()) == 1


def test_upload_rejects_non_image(client):
    pid = client.post("/api/products", json={**PAYLOAD, "sku": "SKU-BADFILE"}).json()["id"]
    r = client.post(
        f"/api/products/{pid}/assets",
        files={"file": ("x.jpg", b"not an image", "image/jpeg")},
        data={"asset_type": "GARMENT_FRONT"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "IMAGE_UNREADABLE"


def test_uploading_front_image_advances_status_to_ready(client):
    pid = client.post("/api/products", json={**PAYLOAD, "sku": "SKU-READY"}).json()["id"]
    client.post(
        f"/api/products/{pid}/assets",
        files={"file": ("f.jpg", _image(), "image/jpeg")},
        data={"asset_type": "GARMENT_FRONT"},
    )
    assert client.get(f"/api/products/{pid}").json()["status"] == "READY"


def test_health_endpoints(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert "checks" in client.get("/api/health/ready").json()
