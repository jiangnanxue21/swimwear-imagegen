"""商品 API 测试。需要真实 PostgreSQL 与 FastAPI TestClient。"""
from __future__ import annotations

import io

from PIL import Image

from tests.conftest import requires_db

pytestmark = requires_db

SPU_CODE = "SPU-100"
PAYLOAD = {
    "spu": SPU_CODE,
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




def _ensure_spu(client) -> None:
    """先建 SPU 档,再往下挂 SKU。**完整理由见 12-4 那份同名 helper。**

    一句话版本:A45-batch14-26 起 `create_product` 会解析 `spu` 字符串,
    查不到就 422。batch15 修了 12-4 / 12-5,本文件(8 条)、
    `test_api_generation.py`(16 条)、`test_api_reviews.py`(15 条)
    是同一笔账的剩余部分。

    每条用例都要建(SAVEPOINT 回滚);重复调用返回 409,一并放行。
    受众选 `WOMEN` —— `PAYLOAD` 的品类 `BIKINI_SET` 只在 WOMEN 词表里。

    ## 建档会多出一行商品,而这里有一条用例在数数

    `ONE_SIZE` × 单颜色展开一行 `SPU-100-100-OS`,`name` 是
    「<internal_name> 100 OS」。`test_list_supports_search_and_pagination`
    搜「连体」断言 `total == 3` —— 列表的 search 打在 sku / spu / name 三列上
    (`list_products`),所以 `internal_name` **不能带「连体」**。
    这不是巧合能保证的事,写在这里:改这个名字前先看那条用例。
    """
    resp = client.post(
        "/api/spus",
        json={
            "spu_code": SPU_CODE,
            "internal_name": "商品接口测试 SPU",
            "audience": "WOMEN",
            "color_variants": [{"variant_code": "100", "working_name": "回归色"}],
            "size_template": "ONE_SIZE",
        },
    )
    assert resp.status_code in (201, 409), resp.text


def _product(client, **overrides) -> str:
    """建一个商品并返回 id。**建档失败时点名是哪一步。**

    原来各处写的是 `client.post(...).json()["id"]`:建品一旦失败,抛的是
    `KeyError('id')`,既不点名是哪一步、也看不到后端给的那句话。
    这批用例这次全红了一轮,报文就在响应体里,而堆栈里一个字都没有。
    """
    _ensure_spu(client)
    resp = client.post("/api/products", json={**PAYLOAD, **overrides})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_create_and_get_product(client):
    _ensure_spu(client)
    r = client.post("/api/products", json=PAYLOAD)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    got = client.get(f"/api/products/{pid}")
    assert got.status_code == 200
    assert got.json()["sku"] == "SKU-100"
    assert got.json()["status"] == "DRAFT"


def test_duplicate_sku_returns_409(client):
    _ensure_spu(client)
    assert client.post("/api/products", json=PAYLOAD).status_code == 201
    r = client.post("/api/products", json=PAYLOAD)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "DUPLICATE_RESOURCE"


def test_invalid_enum_returns_422(client):
    """非法枚举被拒。**断言必须钉住\"因为枚举\",不能只看 422。**

    A45-batch14-26 之后 SPU 不存在也返回 422,于是这条用例在 SPU 缺席时
    **照样通过,但已经不再检验它声称检验的东西** —— 它变成了一条假绿:
    真把枚举校验删掉,它还是绿的。

    两道 422 分得开,靠的是响应体形状而不是措辞:
    枚举走 Pydantic -> `RequestValidationError` 处理器,**一定带 `fields`**;
    SPU 不存在走 `AppError.to_payload`,`fields` 为空时**整个键不出现**。
    所以判 `fields` 里点到了 `garment_type` 就是判\"因为枚举\"。
    """
    _ensure_spu(client)
    bad = {**PAYLOAD, "sku": "SKU-BAD", "garment_type": "BANANA"}
    r = client.post("/api/products", json=bad)
    assert r.status_code == 422, r.text
    fields = r.json()["error"].get("fields", [])
    assert any("garment_type" in f["loc"] for f in fields), r.text


def test_missing_product_returns_404(client):
    r = client.get("/api/products/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_patch_only_updates_given_fields(client):
    pid = _product(client)
    r = client.patch(f"/api/products/{pid}", json={"material": "再生涤纶"})
    assert r.status_code == 200
    assert r.json()["material"] == "再生涤纶"
    assert r.json()["name"] == PAYLOAD["name"]  # 未传的字段不变


def test_list_supports_search_and_pagination(client):
    for i in range(3):
        _product(client, sku=f"SKU-L{i}", name=f"连体泳衣{i}")
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
    pid = _product(client, sku="SKU-UP")
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
    pid = _product(client, sku="SKU-BADFILE")
    r = client.post(
        f"/api/products/{pid}/assets",
        files={"file": ("x.jpg", b"not an image", "image/jpeg")},
        data={"asset_type": "GARMENT_FRONT"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "IMAGE_UNREADABLE"


def test_uploading_front_image_advances_status_to_ready(client):
    pid = _product(client, sku="SKU-READY")
    client.post(
        f"/api/products/{pid}/assets",
        files={"file": ("f.jpg", _image(), "image/jpeg")},
        data={"asset_type": "GARMENT_FRONT"},
    )
    assert client.get(f"/api/products/{pid}").json()["status"] == "READY"


def test_health_endpoints(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert "checks" in client.get("/api/health/ready").json()
