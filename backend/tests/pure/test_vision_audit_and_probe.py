"""解析失败的留痕、以及连接探针的客户端生命周期。

这两条对应 tests/test_vision_http.py 里的
``test_parse_failure_still_carries_the_audit_metadata`` 和
``test_connection_probe_reports_reachable`` —— 那两个要真 httpx。
这里用假的一次往返把 HTTP 那一层整个替掉,断言的是同一个行为,
所以没有 httpx 的机器上也能跑。
"""

from __future__ import annotations

import asyncio

from app.core.enums import EvaluationDepth
from app.evaluators.scoring import EvaluationParseError
from app.evaluators.vision import (
    DEPTH_KEY,
    VisionEvaluatorConfig,
    VisionModelImageQualityEvaluator,
)
from app.providers.errors import ProviderInputError
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401  (装 sys.path)


def _config(**overrides) -> VisionEvaluatorConfig:
    base = {
        "api_key": "sk-not-a-real-key",
        "base_url": "https://api.example.com/v1",
        "model": "test-vision-model",
        "max_retries": 0,
        "retry_base_seconds": 0.001,
    }
    base.update(overrides)
    return VisionEvaluatorConfig(**base)


def _run(coro):
    return asyncio.run(coro)


class _FakeImage:
    def describe(self) -> dict:
        return {"role": "PRODUCT_FRONT", "byte_size": 12}


class _FakeRequest:
    images = (_FakeImage(),)
    body = {}

    def safe_body_summary(self) -> dict:
        return {}


def test_parse_failure_still_carries_the_audit_metadata():
    """响应体不是合法 JSON 时,响应 ID / 模型名 / token 用量必须留得下来。

    HTTP 请求其实成功了 —— 这些字段全都拿到了。它们是事后拿着响应 ID
    找厂商查一次异常调用的全部依据,而且只在这一刻存在。
    """
    broken = {
        "id": "resp_broken",
        "model": "qwen3-vl-plus",
        "usage": {"input_tokens": 42, "output_tokens": 7},
        "choices": [{"finish_reason": "stop", "message": {"content": "这不是 JSON"}}],
    }

    evaluator = VisionModelImageQualityEvaluator(_config(api_style="chat_completions"))

    async def fake_send(request):
        return broken, 200

    evaluator._send_with_retries = fake_send  # type: ignore[assignment]

    async def fake_prepare(product_assets, candidate_image):
        return [], None

    evaluator._prepare_images = fake_prepare  # type: ignore[assignment]
    evaluator._adapter.build = lambda **kwargs: _FakeRequest()  # type: ignore[assignment]

    caught: EvaluationParseError | None = None
    try:
        _run(
            evaluator.evaluate_structured(
                ["ref.png"],
                "cand.png",
                {"sku": "SW-001"},
                {DEPTH_KEY: EvaluationDepth.FULL.value},
            )
        )
    except EvaluationParseError as exc:
        caught = exc

    assert caught is not None, "非法 JSON 居然没有抛 EvaluationParseError"

    meta = caught.vision_meta
    assert meta is not None, "解析失败的异常没有带上审计元数据"
    assert meta["response_id"] == "resp_broken"
    # 厂商实际路由到的模型,不是配置里写的 test-vision-model
    assert meta["model"] == "qwen3-vl-plus"
    assert meta["usage"]["output_tokens"] == 7
    assert meta["http_status"] == 200
    assert caught.duration_ms is not None


def test_truncation_still_carries_usage_and_is_not_automatically_retried():
    truncated = {
        "id": "resp_truncated",
        "model": "qwen3.8-max",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "usage": {"input_tokens": 900, "output_tokens": 1800},
        "output": [],
    }
    evaluator = VisionModelImageQualityEvaluator(_config(max_output_tokens=1800))
    calls = 0

    async def fake_send(request):
        nonlocal calls
        calls += 1
        return truncated, 200

    evaluator._send_with_retries = fake_send  # type: ignore[assignment]

    async def fake_prepare(product_assets, candidate_image):
        return [], None

    evaluator._prepare_images = fake_prepare  # type: ignore[assignment]
    evaluator._adapter.build = lambda **kwargs: _FakeRequest()  # type: ignore[assignment]

    caught = None
    try:
        _run(evaluator.evaluate(["ref.png"], "cand.png", {}, {}))
    except ProviderInputError as exc:
        caught = exc

    assert caught is not None
    assert calls == 1, "截断后自动发了第二次付费请求"
    assert caught.detail["automatic_retry"] is False
    assert caught.vision_meta["response_id"] == "resp_truncated"
    assert caught.vision_meta["usage"]["output_tokens"] == 1800


def test_connection_probe_reports_reachable():
    """探针必须走传输层的客户端生命周期,而不是评分器上并不存在的 _client()。

    以前那个 AttributeError 会被兜底的 except 转成"连接测试失败",
    于是一个完全可达的服务被报成不可达。
    """
    ok = {
        "id": "resp_probe",
        "model": "test-vision-model-2026",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": '{"ok": true}'}]}
        ],
    }

    evaluator = VisionModelImageQualityEvaluator(
        _config(),
        http_client=object(),  # 注入的客户端不会被真的用到
    )
    seen: dict = {}

    async def fake_send_once(request, client):
        seen["client"] = client
        seen["max_output_tokens"] = request.body.get("max_output_tokens")
        return ok, 200

    evaluator._send_once = fake_send_once  # type: ignore[assignment]

    result = _run(evaluator.test_connection())

    assert result["configured"] is True, result["message"]
    assert result["reachable"] is True, result["message"]
    assert result["latency_ms"] is not None
    assert result["api_style"] == "responses"
    assert result["model"] == "test-vision-model-2026"
    # 注入的客户端确实被透传下去了 —— 探针不该自己再建一个连接池
    assert seen["client"] is evaluator._http_client
    # 最小请求,不产生业务级推理开销
    assert seen["max_output_tokens"] == 64


def test_probe_reports_unreachable_when_the_round_trip_really_fails():
    """真的打不通时仍然要报不可达 —— 上面那条不能是"永远返回 True"。"""
    from app.providers.errors import ProviderAuthError

    evaluator = VisionModelImageQualityEvaluator(_config(), http_client=object())

    async def fake_send_once(request, client):
        raise ProviderAuthError("鉴权失败", provider="vision")

    evaluator._send_once = fake_send_once  # type: ignore[assignment]

    result = _run(evaluator.test_connection())
    assert result["reachable"] is False
    assert "鉴权失败" in result["message"]
