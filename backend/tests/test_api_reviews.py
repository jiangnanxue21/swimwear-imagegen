"""人工审核队列 API 集成测试(需求第十二、十四、十六章)。需要真实 PostgreSQL。

这一组用例守的是阶段 4 最容易被写歪的那条边界:

    低分和硬错误候选**先自动淘汰重生**,只有轮次耗尽仍拿不到 A 档,
    任务才作为一个整体进入人工审核 —— 进队的是商品任务,不是每一张废图。

Mock 评分器的 outcome 旋钮让这条路径可以离线复现:
``stuck`` 永远 D,``improving`` 第三轮到 A,``a`` 首轮即通过。
"""
from __future__ import annotations

import io

from PIL import Image

from tests.conftest import requires_db

pytestmark = requires_db

PRODUCT = {
    "spu": "SPU-REV",
    "sku": "SKU-REV-1",
    "name": "审核测试泳衣",
    "garment_type": "BIKINI_SET",
}


def _image(w=800, h=1200) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 90, 120)).save(buf, format="JPEG")
    return buf.getvalue()


def _product_with_asset(client, sku: str) -> str:
    pid = client.post("/api/products", json={**PRODUCT, "sku": sku}).json()["id"]
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


def _run_task(client, sku: str, outcome: str, **overrides) -> dict:
    """跑完一个任务并返回它的详情。outcome 直接钉住 Mock 评分器的档位。"""
    pid = _product_with_asset(client, sku)
    payload = {
        "product_id": pid,
        "mode": "virtual_try_on",
        "provider": "mock",
        "candidate_count": 4,
        "max_rounds": 2,
        "provider_params": {"mock_evaluator": {"outcome": outcome}},
        **overrides,
    }
    task_id = client.post("/api/generation-tasks", json=payload).json()["task"]["id"]
    return client.get(f"/api/generation-tasks/{task_id}").json()


def _blocking_reviews(client, **params) -> list[dict]:
    """只取阻塞项。A 档抽检也会建审核项,但它不挡任务,不该混进断言。"""
    body = client.get("/api/reviews", params=params).json()
    return [r for r in body["items"] if r["blocking"]]


def _first_review(client, task_id: str) -> dict:
    for item in _blocking_reviews(client):
        if item["task_id"] == task_id:
            return item
    raise AssertionError(f"任务 {task_id} 不在审核队列里")


# ---------- 进队条件 ----------

def test_stuck_task_lands_in_review_after_rounds_are_exhausted(client, celery_eager):
    """需求第十二章:耗尽轮次后才转人工,且进队的是任务本身。"""
    detail = _run_task(client, "SKU-REV-STUCK", "stuck")
    assert detail["status"] == "MANUAL_REVIEW"
    assert detail["current_round"] == 2, "两轮都不达标才升级"

    review = _first_review(client, detail["id"])
    assert review["reason"] == "ROUNDS_EXHAUSTED"
    assert review["round_count"] == 2
    assert review["attempt_count"] == 2
    assert review["best_grade"] == "D"
    assert review["product_sku"] == "SKU-REV-STUCK"


def test_low_score_candidates_never_appear_as_separate_review_items(client, celery_eager):
    """8 张低分候选只产生 1 条审核项 —— 审核对象是任务,不是候选图。"""
    detail = _run_task(client, "SKU-REV-ONE", "stuck")
    assert len(detail["candidates"]) == 8, "两轮各 4 张"
    mine = [r for r in _blocking_reviews(client) if r["task_id"] == detail["id"]]
    assert len(mine) == 1


def test_hard_fail_task_reaches_review_and_records_codes(client, celery_eager):
    """需求第十一章:硬错误不自动通过,但也不立刻交人工 —— 先重生,耗尽才进队。"""
    detail = _run_task(client, "SKU-REV-HARD", "hard_fail")
    assert detail["status"] == "MANUAL_REVIEW"
    review = _first_review(client, detail["id"])
    assert review["hard_fail_codes"], "硬错误码必须带到审核页"


def test_improving_task_is_auto_approved_and_never_blocks_a_reviewer(client, celery_eager):
    """自动重生成功的任务不该出现在阻塞队列里。

    **终态是 COMPLETED,不是 AUTO_APPROVED。** 阶段 6 起自动通过之后还要产出
    多尺寸图,出完才算完成;`AUTO_APPROVED` 退化成了链路中间的一站。
    权威口径在 `tests/pure/test_pipeline_simulation.py`:
    `test_approved_task_ends_at_completed_not_auto_approved` 钉住的序列是
    DOWNLOADING -> SCORING -> AUTO_APPROVED -> FORMATTING -> COMPLETED。
    这一整个模块因为缺 PostgreSQL 长期没跑过,断言就停在阶段 6 之前了。
    """
    detail = _run_task(client, "SKU-REV-IMPROVE", "improving", max_rounds=3)
    assert detail["status"] == "COMPLETED"
    assert detail["current_round"] == 3, "前两轮不达标,第三轮才通过"
    assert not [r for r in _blocking_reviews(client) if r["task_id"] == detail["id"]]


def test_first_round_pass_creates_no_blocking_review(client, celery_eager):
    # 终态同上:COMPLETED。AUTO_APPROVED 是中间站,不是终点
    detail = _run_task(client, "SKU-REV-A", "a")
    assert detail["status"] == "COMPLETED"
    assert detail["current_round"] == 1
    assert not [r for r in _blocking_reviews(client) if r["task_id"] == detail["id"]]


# ---------- 审核详情 ----------

def test_review_detail_carries_everything_a_reviewer_needs(client, celery_eager):
    """需求第十四章:原图、各轮最佳候选、评分、硬错误、Provider、生成次数。"""
    detail = _run_task(client, "SKU-REV-DETAIL", "stuck")
    review_id = _first_review(client, detail["id"])["id"]
    body = client.get(f"/api/reviews/{review_id}").json()

    assert len(body["product_assets"]) == 2, "商品原图要能对照"
    assert len(body["round_best_candidates"]) == 2, "每轮一张最佳"
    assert len(body["all_candidates"]) == 8
    assert body["provider"] == "mock"
    assert body["max_rounds"] == 2

    best = body["round_best_candidates"][0]
    assert best["url"], "候选图必须给出可访问 URL"
    assert best["evaluation"] is not None
    assert best["evaluation"]["scores"], "评分必须是结构化维度分,不是自由文本"
    assert best["evaluation"]["grade"] in {"A", "B", "C", "D"}


def test_review_detail_of_unknown_id_returns_404(client):
    r = client.get("/api/reviews/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


# ---------- 决策 ----------

def test_approve_moves_task_and_product_forward(client, celery_eager):
    detail = _run_task(client, "SKU-REV-APPROVE", "stuck")
    review = _first_review(client, detail["id"])
    body = client.get(f"/api/reviews/{review['id']}").json()
    chosen = body["round_best_candidates"][-1]["id"]

    r = client.post(
        f"/api/reviews/{review['id']}/approve",
        json={"note": "光影可接受,先上架", "tags": ["可用于列表页"], "candidate_id": chosen},
        headers={"X-Actor": "reviewer-1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "APPROVED"
    assert r.json()["decided_by"] == "reviewer-1"
    assert r.json()["manual_tags"] == ["可用于列表页"]

    task = client.get(f"/api/generation-tasks/{detail['id']}").json()
    # **这里原来断言 `MANUALLY_APPROVED`,那是阶段 6 之前的期望(A42 订正)。**
    #
    # `state_machine` 里 `MANUALLY_APPROVED` 的出边是 `{FORMATTING, FAILED}` ——
    # 它明写着「人已经动过了,下一步是机器去 FORMATTING」,不是终态。
    # 阶段 6 把人工通过接进了和自动通过同一条出图路径
    # (`review_service._format_after_approval`,"不给人工开小灶"),
    # 于是 approve 之后任务会一路走到 COMPLETED。
    #
    # 断言停在 `MANUALLY_APPROVED` 等于要求出图**不要**发生,而那恰好是
    # 阶段 6 修掉的缺陷:人点了通过、审核关闭、却一张成品图都没有。
    assert task["status"] == "COMPLETED", (
        "人工通过之后必须继续走完出图。停在 MANUALLY_APPROVED 意味着"
        "审核关了却没有成品图,而那正是阶段 6 修掉的东西"
    )
    outputs = client.get(f"/api/exports/products/{task['product_id']}").json()
    assert outputs["image_count"] > 0, "走完 FORMATTING 就必须真的有成品图"
    selected = [c for c in task["candidates"] if c["status"] == "SELECTED"]
    assert [c["id"] for c in selected] == [chosen], "人工选定的那张才是采用图"


def test_approve_rejects_candidate_from_another_task(client, celery_eager):
    mine = _run_task(client, "SKU-REV-X1", "stuck")
    other = _run_task(client, "SKU-REV-X2", "stuck")
    review = _first_review(client, mine["id"])
    foreign = other["candidates"][0]["id"]

    r = client.post(
        f"/api/reviews/{review['id']}/approve", json={"candidate_id": foreign}
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INPUT_INVALID"


def test_reject_returns_product_to_ready_not_completed(client, celery_eager):
    """驳回后这个 SKU 还没有能用的图,应该回到\"可再生成\",不是\"已完成\"。"""
    detail = _run_task(client, "SKU-REV-REJECT", "stuck")
    review = _first_review(client, detail["id"])

    r = client.post(f"/api/reviews/{review['id']}/reject", json={"note": "商品结构错误"})
    assert r.status_code == 200
    assert r.json()["status"] == "REJECTED"

    task = client.get(f"/api/generation-tasks/{detail['id']}").json()
    assert task["status"] == "MANUALLY_REJECTED"
    assert client.get(f"/api/products/{task['product_id']}").json()["status"] == "READY"


def test_decided_review_cannot_be_decided_twice(client, celery_eager):
    detail = _run_task(client, "SKU-REV-TWICE", "stuck")
    review = _first_review(client, detail["id"])
    assert client.post(f"/api/reviews/{review['id']}/reject", json={}).status_code == 200

    again = client.post(f"/api/reviews/{review['id']}/approve", json={})
    assert again.status_code == 409, "重复决策必须挡住"


# ---------- 重新生成 ----------

def test_regenerate_adds_rounds_so_the_task_does_not_bounce_back(client, celery_eager):
    """不加轮次就重排队,任务会立刻再次判定耗尽 —— 这里守住那个空转陷阱。"""
    detail = _run_task(client, "SKU-REV-REGEN", "stuck")
    review = _first_review(client, detail["id"])

    r = client.post(
        f"/api/reviews/{review['id']}/regenerate",
        json={"extra_rounds": 2, "note": "换个模特再试"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "REGENERATION_REQUESTED"

    task = client.get(f"/api/generation-tasks/{detail['id']}").json()
    assert task["max_rounds"] == 4, "2 轮上限 + 追加 2 轮"


def test_regenerate_refuses_to_switch_to_an_unconfigured_provider(client, celery_eager):
    """需求第七章:未配置的 Provider 不能被选中,审核页也不例外。"""
    detail = _run_task(client, "SKU-REV-SWITCH", "stuck")
    review = _first_review(client, detail["id"])

    r = client.post(
        f"/api/reviews/{review['id']}/regenerate", json={"provider": "fashn"}
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PROVIDER_NOT_CONFIGURED"


# ---------- 列表与只读端点 ----------

def test_review_list_filters_by_status_and_reason(client, celery_eager):
    detail = _run_task(client, "SKU-REV-FILTER", "stuck")
    task_id = detail["id"]

    pending = client.get("/api/reviews", params={"status": "PENDING"}).json()
    assert any(r["task_id"] == task_id for r in pending["items"])

    wrong_reason = client.get(
        "/api/reviews", params={"status": "PENDING", "reason": "SPOT_CHECK"}
    ).json()
    assert not any(r["task_id"] == task_id for r in wrong_reason["items"])

    review = _first_review(client, task_id)
    client.post(f"/api/reviews/{review['id']}/reject", json={})
    rejected = client.get("/api/reviews", params={"status": "REJECTED"}).json()
    assert any(r["task_id"] == task_id for r in rejected["items"])


def test_task_evaluations_endpoint_returns_structured_scores(client, celery_eager):
    """需求第十章:评分结果必须是结构化数据,不接受不可解析的自由文本。"""
    detail = _run_task(client, "SKU-REV-EVAL", "stuck")
    rows = client.get(f"/api/generation-tasks/{detail['id']}/evaluations").json()
    assert len(rows) == 8

    row = rows[0]
    assert isinstance(row["scores"], dict) and row["scores"]
    assert 0 <= row["overall_score"] <= 100
    assert row["grade"] in {"A", "B", "C", "D"}
    assert isinstance(row["hard_fail_codes"], list)
    assert isinstance(row["problems"], list)


def test_evaluation_overall_score_is_computed_by_the_backend(client, celery_eager):
    """总分由后端按权重算,不采信评分器自报的那个数。"""
    detail = _run_task(client, "SKU-REV-WEIGHT", "b")
    rows = client.get(f"/api/generation-tasks/{detail['id']}/evaluations").json()
    full = [r for r in rows if r["depth"] == "FULL"]
    assert full, "每轮至少有一张做完整评分"
    assert all(r["overall_score"] is not None for r in full)
    assert any(r["model_reported_overall"] is not None for r in full), (
        "评分器自报总分要留档,用于监控打分漂移"
    )


def test_rule_sets_and_evaluators_are_readable_without_keys(client):
    rules = client.get("/api/rule-sets")
    assert rules.status_code == 200

    evaluators = client.get("/api/evaluators")
    assert evaluators.status_code == 200
    by_name = {e["name"]: e for e in evaluators.json()}
    assert by_name["mock"]["configured"] is True

    body = evaluators.text.lower()
    assert "api_key" not in body and "secret" not in body
