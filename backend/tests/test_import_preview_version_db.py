from __future__ import annotations

from tests.conftest import requires_db

pytestmark = requires_db


def _create_spu(client, code: str) -> None:
    response = client.post(
        "/api/spus",
        json={
            "spu_code": code,
            "internal_name": "AC17 导入",
            "audience": "WOMEN",
            "size_template": "ONE_SIZE",
            "color_variants": [{"variant_code": "BASE"}],
        },
    )
    assert response.status_code == 201, response.text


def _preview(client, csv_text: str) -> dict:
    response = client.post(
        "/api/workbench/import/preview",
        files={"file": ("import.csv", csv_text.encode(), "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_commit_requires_the_exact_previewed_file_and_database_state(client):
    _create_spu(client, "SPU-AC17")
    csv_text = (
        "spu,variant_code,sku,name\n"
        "SPU-AC17,BASE,SKU-AC17-NEW,预览后提交\n"
    )
    preview = _preview(client, csv_text)
    assert preview["counts"]["will_create"] == 1
    token = preview["preview_token"]

    changed = client.post(
        "/api/workbench/import/commit",
        files={"file": ("import.csv", csv_text.replace("NEW", "OTHER").encode(), "text/csv")},
        data={"preview_token": token},
    )
    assert changed.status_code == 409

    committed = client.post(
        "/api/workbench/import/commit",
        files={"file": ("import.csv", csv_text.encode(), "text/csv")},
        data={"preview_token": token},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["created"] == 1


def test_preview_becomes_stale_when_another_request_creates_the_sku(client):
    _create_spu(client, "SPU-AC17-RACE")
    csv_text = (
        "spu,variant_code,sku,name\n"
        "SPU-AC17-RACE,BASE,SKU-AC17-RACE,并发创建\n"
    )
    token = _preview(client, csv_text)["preview_token"]
    created = client.post(
        "/api/products",
        json={"spu": "SPU-AC17-RACE", "sku": "SKU-AC17-RACE", "name": "抢先创建"},
    )
    assert created.status_code == 201, created.text

    stale = client.post(
        "/api/workbench/import/commit",
        files={"file": ("import.csv", csv_text.encode(), "text/csv")},
        data={"preview_token": token},
    )
    assert stale.status_code == 409


def test_preview_reports_missing_spu_before_commit(client):
    csv_text = "spu,variant_code,sku,name\nSPU-NOT-THERE,BASE,SKU-NO-SPU,缺少款号\n"

    preview = _preview(client, csv_text)

    assert preview["counts"] == {
        "total": 1,
        "will_create": 0,
        "will_skip": 0,
        "error_rows": 1,
    }
    assert preview["rows"][0]["plan"] == "ERROR"
    assert preview["rows"][0]["problems"][0]["field"] == "spu"
