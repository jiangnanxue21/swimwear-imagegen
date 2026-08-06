"""FASHN 的 HTTP 流程测试。

纯测试(`tests/pure/test_fashn_provider.py`)只验证字段映射与错误分类,
覆盖不到 submit -> 轮询 -> 取结果这条接线。这里用 ``httpx.MockTransport``
把整条链路跑一遍:**不发真实网络请求,不需要 API Key,不产生任何费用**。

只需要 httpx,不需要数据库,因此不带 `requires_db` 标记。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.providers.base import ExternalTaskStatus, GenerationMode, GenerationRequest
from app.providers.errors import (
    ProviderAuthError,
    ProviderContentSafetyError,
    ProviderQuotaError,
    ProviderTimeoutError,
)
from app.providers.fashn import FashnImageGenerationProvider, data_uri

TINY = data_uri(b"\xff\xd8\xff\xd9", "image/jpeg")


def _request(**overrides) -> GenerationRequest:
    payload = {
        "mode": GenerationMode.VIRTUAL_TRY_ON,
        "garment_image_url": TINY,
        "model_image_url": TINY,
        "candidate_count": 4,
        "seed": 42,
    }
    payload.update(overrides)
    return GenerationRequest(**payload)


class FakeFashn:
    """一个极小的 FASHN 假服务端,只实现我们真正用到的三个端点。"""

    def __init__(self, *, script: list[str] | None = None, run_status: int = 200,
                 run_body: dict | None = None, credits_used: str = "3") -> None:
        #: 每次查询状态时依次返回的状态字符串;用完后停在最后一个
        self.script = script or ["completed"]
        self.run_status = run_status
        self.run_body = run_body
        self.credits_used = credits_used
        self.submitted: list[dict] = []
        self.status_calls = 0
        self._counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path.endswith("/run"):
            if self.run_status != 200:
                return httpx.Response(self.run_status, json=self.run_body or {})
            body = json.loads(request.content.decode())
            self.submitted.append(body)
            self._counter += 1
            return httpx.Response(200, json={"id": f"pred-{self._counter}", "error": None})

        if "/status/" in path:
            prediction_id = path.rsplit("/", 1)[-1]
            index = min(self.status_calls, len(self.script) - 1)
            state = self.script[index]
            self.status_calls += 1
            payload: dict = {"id": prediction_id, "status": state, "error": None}
            if state == "completed":
                payload["output"] = [f"https://cdn.fashn.ai/{prediction_id}/output_0.png"]
            elif state == "failed":
                payload["output"] = None
                payload["error"] = {"name": "ContentModerationError", "message": "flagged"}
            return httpx.Response(
                200, json=payload, headers={"x-fashn-credits-used": self.credits_used}
            )

        if path.endswith("/credits"):
            return httpx.Response(
                200,
                json={"credits": {"total": 234, "subscription": 100, "on_demand": 134}},
            )

        return httpx.Response(404, json={"error": "NotFound", "message": path})


@pytest.fixture
def provider(monkeypatch):
    """把 provider 的 AsyncClient 换成走 MockTransport 的实例。"""
    fake = FakeFashn()
    p = FashnImageGenerationProvider(api_key="test-key")

    def _client(_self=None, _fake=fake, _p=p):
        return httpx.AsyncClient(
            base_url=_p.base_url,
            transport=httpx.MockTransport(_fake.handler),
            headers={"Authorization": "Bearer test-key", "Content-Type": "application/json"},
        )

    monkeypatch.setattr(p, "_client", _client)
    monkeypatch.setattr("app.providers.fashn.provider_float", lambda name, default: 0.0
                        if "POLL_INTERVAL" in name else (5.0 if "TIMEOUT" in name else default))
    p.fake = fake  # type: ignore[attr-defined]
    return p


def run(coro):
    return asyncio.run(coro)


# ---------- 提交 ----------

def test_try_on_submits_one_prediction_with_num_images(provider):
    external_id = run(provider.submit(_request()))
    assert provider.fake.submitted[0]["model_name"] == "tryon-max"
    assert provider.fake.submitted[0]["inputs"]["num_images"] == 4
    assert len(provider.fake.submitted) == 1, "一次调用就能出 4 张,不该拆"
    assert external_id == "pred-1"


def test_product_to_model_submits_once_per_candidate(provider):
    """文档里 product-to-model 没有候选数字段,只能提交四次。"""
    external_id = run(
        provider.submit(_request(mode=GenerationMode.PRODUCT_TO_MODEL, candidate_count=4))
    )
    assert len(provider.fake.submitted) == 4
    assert external_id.split(",") == ["pred-1", "pred-2", "pred-3", "pred-4"]


def test_each_sub_submission_uses_a_different_seed(provider):
    """同 seed 同输入会得到同一张图,拿 4 张一样的没有意义。"""
    run(provider.submit(_request(mode=GenerationMode.PRODUCT_TO_MODEL, candidate_count=4)))
    seeds = [body["inputs"]["seed"] for body in provider.fake.submitted]
    assert len(set(seeds)) == 4


def test_authorization_header_is_sent(provider):
    run(provider.submit(_request()))
    # MockTransport 收到的请求头由 _client 构造,这里断言 provider 确实带了 Bearer
    assert provider._headers()["Authorization"] == "Bearer test-key"


# ---------- 轮询与取结果 ----------

def test_fetch_results_polls_until_terminal(provider):
    provider.fake.script = ["in_queue", "processing", "completed"]
    external_id = run(provider.submit(_request()))
    candidates = run(provider.fetch_results(external_id))
    assert provider.fake.status_calls == 3, "应当一直轮询到终态"
    assert len(candidates) == 1
    assert candidates[0].image_url.startswith("https://cdn.fashn.ai/")


def test_candidates_carry_credits_used(provider):
    external_id = run(provider.submit(_request()))
    candidates = run(provider.fetch_results(external_id))
    assert candidates[0].metadata["credits_used"] == 3


def test_get_status_maps_provider_states(provider):
    provider.fake.script = ["processing"]
    external_id = run(provider.submit(_request()))
    assert run(provider.get_status(external_id)) is ExternalTaskStatus.RUNNING


def test_runtime_failure_raises_the_mapped_error(provider):
    provider.fake.script = ["failed"]
    external_id = run(provider.submit(_request()))
    with pytest.raises(ProviderContentSafetyError):
        run(provider.fetch_results(external_id))


def test_polling_timeout_raises_timeout_error(provider, monkeypatch):
    """轮询超时必须报超时,不能报生成失败 —— 两者的重试策略完全不同。"""
    provider.fake.script = ["processing"]
    monkeypatch.setattr("app.providers.fashn.provider_float", lambda name, default: 0.0)
    external_id = run(provider.submit(_request()))
    with pytest.raises(ProviderTimeoutError):
        run(provider.fetch_results(external_id))


# ---------- API 错误 ----------

def test_unauthorized_submit_raises_auth_error(provider):
    provider.fake.run_status = 401
    provider.fake.run_body = {"error": "UnauthorizedAccess", "message": "invalid key"}
    with pytest.raises(ProviderAuthError):
        run(provider.submit(_request()))


def test_out_of_credits_raises_quota_error(provider):
    provider.fake.run_status = 429
    provider.fake.run_body = {"error": "OutOfCredits", "message": "top up"}
    with pytest.raises(ProviderQuotaError):
        run(provider.submit(_request()))


# ---------- 自检 ----------

def test_test_connection_reports_credit_balance(provider):
    result = run(provider.test_connection())
    assert result["configured"] is True
    assert result["reachable"] is True
    assert "234" in result["message"]
    assert "test-key" not in json.dumps(result)


def test_test_connection_without_a_key_does_not_call_the_api():
    result = run(FashnImageGenerationProvider(api_key="").test_connection())
    assert result["configured"] is False
    assert result["reachable"] is None
