"""生成任务 API 集成测试。需要真实 PostgreSQL。

Celery 用 eager 模式同步执行,便于在一次请求里看到完整流水线结果。
"""
from __future__ import annotations

import io

from PIL import Image

from tests.conftest import requires_db

pytestmark = requires_db

SPU_CODE = "SPU-GEN"
PRODUCT = {
    "spu": SPU_CODE,
    "sku": "SKU-GEN-1",
    "name": "生成测试泳衣",
    "garment_type": "BIKINI_SET",
}


def _image(w=800, h=1200) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 90, 120)).save(buf, format="JPEG")
    return buf.getvalue()




def _ensure_spu(client) -> None:
    """先建 SPU 档,再往下挂 SKU。**完整理由见 12-4 那份同名 helper。**

    一句话版本:A45-batch14-26 起 `create_product` 会解析 `spu` 字符串,
    查不到就 422(「SPU xx 不存在:请先用 POST /spus 建档」)。
    batch15 修了 12-4 / 12-5 两份 fixture,但同一笔账在本文件、
    `test_api_products.py`、`test_api_reviews.py` 里原样躺着 —— 那次只跑了
    12-4 / 12-5,**11 这个数字是样本量,不是问题的边界**。本文件占 16 条。

    每条用例都要建:`conftest.session` 跑在 SAVEPOINT 上,teardown 回滚外层
    事务,用例之间不留数据。重复调用返回 409(`create_spu` 的 `_code_taken`
    快速路径),那也是"已经有了",一并放行。

    受众选 `WOMEN`:`create_product` 会把 SPU 的受众抄进商品行,而 `PRODUCT`
    的品类是 `BIKINI_SET` —— 它只在 WOMEN 的词表里(`core/garments`),
    选 MEN/UNISEX 会换一个 422 出来(C-03 受众 × 品类闸),而且报文里
    不会提 SPU,查起来更绕。
    """
    resp = client.post(
        "/api/spus",
        json={
            "spu_code": SPU_CODE,
            "internal_name": "生成测试 SPU",
            "audience": "WOMEN",
            "color_variants": [{"variant_code": "GEN", "working_name": "回归色"}],
            "size_template": "ONE_SIZE",
        },
    )
    assert resp.status_code in (201, 409), resp.text


def _product_with_asset(client, sku="SKU-GEN-1") -> str:
    _ensure_spu(client)
    resp = client.post("/api/products", json={**PRODUCT, "sku": sku})
    # 先断状态码再取 id:原来 `.json()["id"]` 在建品失败时抛 KeyError('id'),
    # 后端那句「请先用 POST /spus 建档」一个字都到不了报告里
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    client.post(
        f"/api/products/{pid}/assets",
        files={"file": ("front.jpg", _image(), "image/jpeg")},
        data={"asset_type": "GARMENT_FRONT"},
    )
    client.post(
        f"/api/products/{pid}/assets",
        files={"file": ("model.jpg", _image(801, 1200), "image/jpeg")},
        data={"asset_type": "MODEL_REFERENCE"},
    )
    return pid


def _force_grade(grade: str) -> dict:
    """把 Mock 评分器钉在指定档位,让用例只验证自己关心的那一件事。"""
    return {"mock_evaluator": {"outcome": grade}}


def _create_task(client, pid, **overrides):
    payload = {
        "product_id": pid,
        "mode": "virtual_try_on",
        "provider": "mock",
        "candidate_count": 4,
        **overrides,
    }
    return client.post("/api/generation-tasks", json=payload)


# ---------- 创建 ----------

def test_create_task_returns_immediately_with_201(client):
    pid = _product_with_asset(client)
    r = _create_task(client, pid)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["deduplicated"] is False
    assert body["task"]["provider"] == "mock"


def test_duplicate_idempotency_key_does_not_create_second_task(client):
    """需求第十八章关键用例。"""
    pid = _product_with_asset(client, "SKU-IDEM")
    first = _create_task(client, pid).json()
    second = _create_task(client, pid).json()
    assert second["deduplicated"] is True
    assert second["task"]["id"] == first["task"]["id"]
    assert client.get("/api/generation-tasks", params={"product_id": pid}).json()["total"] == 1


def test_explicit_idempotency_key_is_honoured(client):
    pid = _product_with_asset(client, "SKU-IDEM2")
    a = _create_task(client, pid, idempotency_key="order-9").json()
    b = _create_task(client, pid, idempotency_key="order-9", candidate_count=2).json()
    assert b["deduplicated"] is True
    assert b["task"]["id"] == a["task"]["id"]


def test_task_without_assets_is_rejected(client):
    # 这条不走 `_product_with_asset`(它要的正是"没有素材"),但建档这一步
    # 一样要先有 SPU —— 漏掉这里就只剩这一条还是 422
    _ensure_spu(client)
    resp = client.post("/api/products", json={**PRODUCT, "sku": "SKU-NOASSET"})
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    r = _create_task(client, pid)
    assert r.status_code == 422
    assert "素材" in r.json()["error"]["message"]


def test_unconfigured_provider_blocks_task_creation(client):
    """需求第七章:Provider 未配置时自动阻止任务提交。"""
    pid = _product_with_asset(client, "SKU-UNCONF")
    r = _create_task(client, pid, provider="fashn")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PROVIDER_NOT_CONFIGURED"


def test_unknown_product_returns_404(client):
    r = _create_task(client, "00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# ---------- 流水线 ----------

def test_pipeline_produces_four_candidates(client, celery_eager):
    """一轮出 4 张候选图并全部落到自有存储。

    阶段 4 起流水线不再停在 SCORING,因此这里把评分器钉在 A 档,
    让任务在第一轮就自动通过 —— 本用例要验的是"出图与落盘",不是分档。

    阶段 6 起 AUTO_APPROVED 也不再是终点:通过之后还要产出多尺寸图,
    出完才算 COMPLETED。行为由
    `tests/pure/test_pipeline_simulation.py::test_approved_task_ends_at_completed_not_auto_approved`
    钉住(那条用真实评分器、真实决策引擎、真实渲染跑过)。
    """
    pid = _product_with_asset(client, "SKU-RUN")
    task_id = _create_task(client, pid, provider_params=_force_grade("a")).json()["task"]["id"]
    detail = client.get(f"/api/generation-tasks/{task_id}").json()

    assert detail["status"] == "COMPLETED"
    assert len(detail["candidates"]) == 4
    assert all(c["url"] for c in detail["candidates"])
    assert all(c["overall_score"] is not None for c in detail["candidates"])
    statuses = [c["status"] for c in detail["candidates"]]
    assert statuses.count("SELECTED") == 1, "只应选中一张"
    assert statuses.count("REJECTED") == 3


def test_each_provider_call_records_one_attempt(client, celery_eager):
    pid = _product_with_asset(client, "SKU-ATTEMPT")
    task_id = _create_task(
        client, pid, provider_params=_force_grade("a")
    ).json()["task"]["id"]
    detail = client.get(f"/api/generation-tasks/{task_id}").json()
    assert len(detail["attempts"]) == 1
    assert detail["attempts"][0]["candidate_count"] == 4
    assert detail["attempts"][0]["seed"] is not None


def test_provider_failure_is_persisted_with_error_code(client, celery_eager):
    """需求第七章:Provider 调用失败时保存错误信息。"""
    pid = _product_with_asset(client, "SKU-FAIL")
    task_id = _create_task(
        client, pid, provider_params={"mock_outcome": "fail"}
    ).json()["task"]["id"]
    detail = client.get(f"/api/generation-tasks/{task_id}").json()
    assert detail["status"] == "FAILED"
    assert detail["error_code"] == "GENERATION_FAILED"
    assert detail["attempts"][0]["error_code"] == "GENERATION_FAILED"


def test_empty_result_exhausts_rounds_then_escalates(client, celery_eager):
    """Provider 一张图都不返回时,走的是和"没有 A 档候选"完全相同的轮次规则:
    还有轮次就重生,轮次用尽才转人工。不给它开特殊通道。
    """
    pid = _product_with_asset(client, "SKU-EMPTY")
    task_id = _create_task(
        client, pid, provider_params={"mock_outcome": "empty"}, max_rounds=2
    ).json()["task"]["id"]

    detail = client.get(f"/api/generation-tasks/{task_id}").json()
    assert detail["status"] == "MANUAL_REVIEW"
    assert detail["current_round"] == 2, "两轮都空,才升级"
    assert len(detail["candidates"]) == 0


# ---------- 取消与重试 ----------

def test_failed_task_can_be_retried(client, celery_eager):
    pid = _product_with_asset(client, "SKU-RETRY")
    task_id = _create_task(
        client, pid, provider_params={"mock_outcome": "fail"}
    ).json()["task"]["id"]
    assert client.get(f"/api/generation-tasks/{task_id}").json()["can_retry"] is True
    assert client.post(f"/api/generation-tasks/{task_id}/retry").status_code == 200


def test_completed_task_cannot_be_cancelled(client, celery_eager):
    """阶段 6 起任务会一路跑到 COMPLETED —— 终态,取消必须被拒。"""
    pid = _product_with_asset(client, "SKU-CANCEL")
    task_id = _create_task(client, pid, provider_params=_force_grade("a")).json()["task"]["id"]
    assert client.get(f"/api/generation-tasks/{task_id}").json()["status"] == "COMPLETED"
    assert client.post(f"/api/generation-tasks/{task_id}/cancel").status_code == 409


def test_running_task_can_still_be_cancelled(client, celery_eager):
    """还没跑完的任务仍然可以取消 —— 上面那条不是"取消功能没了"。"""
    pid = _product_with_asset(client, "SKU-CANCEL2")
    task_id = _create_task(
        client, pid, provider_params={"mock_evaluator": {"outcome": "stuck"}}, max_rounds=1
    ).json()["task"]["id"]
    # 轮次耗尽 -> MANUAL_REVIEW,非终态,可取消
    assert client.get(f"/api/generation-tasks/{task_id}").json()["status"] == "MANUAL_REVIEW"
    assert client.post(f"/api/generation-tasks/{task_id}/cancel").status_code == 200


def test_cancelled_executed_task_can_be_deleted_and_recreated(client, celery_eager):
    """已取消任务删除后不再可见，同参数可以创建一条全新的任务。"""
    pid = _product_with_asset(client, "SKU-DELETE-CANCELLED")
    params = {
        "provider_params": {"mock_evaluator": {"outcome": "stuck"}},
        "max_rounds": 1,
    }
    first = _create_task(client, pid, **params).json()["task"]
    task_id = first["id"]
    assert first["current_round"] == 1

    cancelled = client.post(f"/api/generation-tasks/{task_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["can_delete"] is True

    deleted = client.delete(f"/api/generation-tasks/{task_id}")
    assert deleted.status_code == 204, deleted.text
    assert client.get(f"/api/generation-tasks/{task_id}").status_code == 404
    listed = client.get("/api/generation-tasks", params={"product_id": pid}).json()
    assert all(row["id"] != task_id for row in listed["items"])

    recreated = _create_task(client, pid, **params).json()
    assert recreated["deduplicated"] is False
    assert recreated["task"]["id"] != task_id


def test_completed_task_cannot_be_retried(client, celery_eager):
    pid = _product_with_asset(client, "SKU-CANCEL3")
    task_id = _create_task(client, pid, provider_params=_force_grade("a")).json()["task"]["id"]
    assert client.post(f"/api/generation-tasks/{task_id}/retry").status_code == 409


def test_completed_task_has_website_ready_outputs(client, celery_eager):
    """阶段 6:通过的任务必须能直接导出网站要用的五个尺寸(需求第十五章)。"""
    pid = _product_with_asset(client, "SKU-OUTPUT")
    _create_task(client, pid, provider_params=_force_grade("a"))

    body = client.get(f"/api/exports/products/{pid}").json()
    assert body["complete"] is True, "五个用途必须齐全"
    assert set(body["images"]) == {"ORIGINAL", "DETAIL", "THUMBNAIL", "MOBILE", "SQUARE"}
    for image in body["images"].values():
        assert image["url"] and image["width"] > 0 and image["height"] > 0

    square = body["images"]["SQUARE"]
    assert square["width"] == square["height"]


def test_export_csv_has_one_row_per_sku(client, celery_eager):
    pid = _product_with_asset(client, "SKU-CSV")
    _create_task(client, pid, provider_params=_force_grade("a"))

    response = client.get(f"/api/exports/products/{pid}", params={"format": "csv"})
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) == 2, "表头 + 一行"
    assert lines[0].lstrip("\ufeff").startswith("sku,")


def test_dashboard_summary_reflects_finished_work(client, celery_eager):
    pid = _product_with_asset(client, "SKU-DASH")
    _create_task(client, pid, provider_params=_force_grade("a"))

    body = client.get("/api/dashboard/summary").json()
    assert body["products"]["total"] >= 1
    assert body["tasks"]["total"] >= 1
    assert body["outputs"]["total"] >= 5
    assert body["grades"]["A"] >= 1
    assert "recent_failures" in body


# ---------- Provider 与模板 ----------

def test_provider_list_never_exposes_keys(client):
    body = client.get("/api/providers").text.lower()
    assert "api_key" not in body and "secret" not in body


def test_unconfigured_provider_does_not_break_application_startup(client):
    """需求第十八章关键用例:没配任何 Key,应用照常工作。"""
    r = client.get("/api/providers")
    assert r.status_code == 200
    by_name = {p["name"]: p for p in r.json()}
    assert by_name["mock"]["configured"] is True
    assert by_name["fashn"]["configured"] is False
    assert client.get("/api/health").status_code == 200


def test_provider_connection_test_works_for_all(admin_client):
    """连接测试会拿真 Key 去打厂商接口,所以现在要口令。"""
    for name in ("mock", "fashn", "fal", "comfyui"):
        r = admin_client.post(f"/api/providers/{name}/test")
        assert r.status_code == 200, name
        assert "message" in r.json()


def test_model_template_upload_and_listing(client):
    r = client.post(
        "/api/model-templates",
        files={"file": ("model.jpg", _image(), "image/jpeg")},
        # `audience` 在 batch13 的身份规范化里变成必填(模特候选集要按受众收窄,
        # 前端表单也标了 required)。这条用例属于"写了没跑过"的那批,一直没跟上
        data={
            "name": "亚洲长发模特",
            "pose": "STANDING_FRONT",
            "tags": '["亚洲","长发"]',
            "audience": "WOMEN",
        },
    )
    assert r.status_code == 201, r.text
    template_id = r.json()["id"]
    assert r.json()["tags"] == ["亚洲", "长发"]

    client.post(f"/api/model-templates/{template_id}/disable")
    assert client.get("/api/model-templates", params={"enabled_only": True}).json() == []
