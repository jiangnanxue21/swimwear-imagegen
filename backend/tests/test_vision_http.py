"""视觉评分器的网络层。用 httpx.MockTransport,不联网。

这个文件里**只测需要真 httpx 的部分** —— 也就是 ``_send_once``:
发请求、读状态码、解 JSON、把非 JSON 错误页处理掉。

端点拼接、Schema 形状、请求体、图片顺序、响应文本提取、错误归类、重试次数
都在 ``tests/pure/test_vision_evaluator.py`` 里,那批不需要任何三方依赖。
两边刻意不重复:同一件事测两遍,改起来要改两处,久了就会有一处是错的。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.core.enums import EvaluationDepth, HardFailCode
from app.evaluators.base import SCORE_DIMENSIONS
from app.evaluators.scoring import EvaluationParseError
from app.evaluators.vision import (
    API_STYLE_CHAT,
    DEPTH_KEY,
    VisionEvaluatorConfig,
    VisionModelImageQualityEvaluator,
)
from app.providers.errors import (
    ProviderAuthError,
    ProviderContentSafetyError,
    ProviderError,
    ProviderInputError,
    ProviderRateLimitError,
)

SECRET = "sk-must-never-appear-anywhere-987654"


def _png() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (48, 48), (200, 120, 90)).save(buf, format="PNG")
    return buf.getvalue()


class FakeStorage:
    def __init__(self) -> None:
        self.files = {"ref.png": _png(), "cand.png": _png()}

    def read(self, storage_path: str) -> bytes:
        if storage_path not in self.files:
            raise FileNotFoundError(storage_path)
        return self.files[storage_path]

    def public_url(self, storage_path: str) -> str:
        return ""


def _config(**overrides) -> VisionEvaluatorConfig:
    base = {
        "api_key": SECRET,
        "base_url": "https://api.example.com/v1",
        "model": "test-vision-model",
        "max_retries": 2,
        "retry_base_seconds": 0.001,
    }
    base.update(overrides)
    return VisionEvaluatorConfig(**base)


def _evaluator(handler, **overrides) -> VisionModelImageQualityEvaluator:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return VisionModelImageQualityEvaluator(
        _config(**overrides), storage=FakeStorage(), http_client=client
    )


def _model_output(depth: EvaluationDepth = EvaluationDepth.FULL) -> str:
    from app.evaluators.vision_schema import dimensions_for

    return json.dumps(
        {
            "hard_fail": False,
            "hard_fail_codes": [],
            "scores": {name: 82 for name in dimensions_for(depth)},
            "uncertain_items": [],
            "problems": [],
            "model_name": "test-vision-model",
        }
    )


def _ok_response(text: str = "", request: httpx.Request | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_abc",
            "model": "test-vision-model-2026",
            "usage": {"input_tokens": 900, "output_tokens": 120},
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": text or _model_output()}
                    ],
                }
            ],
        },
    )


def _run(coro):
    return asyncio.run(coro)


def _run_eval(evaluator, depth=EvaluationDepth.FULL):
    """跑一次评分并关掉注入的客户端 —— 注入的客户端归调用方管,测试也是调用方。"""
    try:
        return _run(
            evaluator.evaluate(["ref.png"], "cand.png", {"sku": "SW-001"}, {DEPTH_KEY: depth.value})
        )
    finally:
        _run(evaluator._http_client.aclose())


def _run_probe(handler):
    evaluator = _evaluator(handler)
    try:
        return _run(evaluator.test_connection())
    finally:
        _run(evaluator._http_client.aclose())




# ---------------------------------------------------------------- 正常路径


def test_end_to_end_full_evaluation():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _ok_response()

    payload = _run_eval(_evaluator(handler))

    assert seen["url"] == "https://api.example.com/v1/responses"
    assert seen["auth"] == f"Bearer {SECRET}"
    assert set(payload["scores"]) == set(SCORE_DIMENSIONS)
    assert payload["_vision_meta"]["response_id"] == "resp_abc"
    assert payload["_vision_meta"]["usage"]["output_tokens"] == 120


def test_quick_evaluation_sends_the_quick_schema():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok_response(_model_output(EvaluationDepth.QUICK))

    _run_eval(_evaluator(handler), depth=EvaluationDepth.QUICK)
    required = seen["body"]["text"]["format"]["schema"]["properties"]["scores"]["required"]
    assert "face_realism" not in required


def test_chat_completions_round_trip():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "qwen3-vl-plus",
                "choices": [
                    {"finish_reason": "stop", "message": {"content": _model_output()}}
                ],
            },
        )

    payload = _run_eval(_evaluator(handler, api_style=API_STYLE_CHAT))
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    # 模型名从**后端写的** _vision_meta 里读,不从模型输出里读。
    #
    # 这条断言以前写的是 payload["model_name"],而评分器现在会主动
    # `payload.pop("model_name")` —— 这个字段的全部用途是回答"三个月前
    # 这批图是谁打的分",让被审计对象自己填等于没有审计。
    # 取值优先响应体里的 model(厂商实际路由到的那一个,可能和请求里写的不同),
    # 这里 handler 返回的是 qwen3-vl-plus,而配置里写的是 test-vision-model。
    assert payload["_vision_meta"]["model"] == "qwen3-vl-plus"
    assert "model_name" not in payload


def test_parse_failure_still_carries_the_audit_metadata():
    """解析失败时,响应 ID / 模型名 / token 用量必须留得下来。

    HTTP 请求其实**成功了** —— 这些字段全都拿到了,只是响应体不是合法 JSON。
    它们恰恰是事后拿着响应 ID 找厂商查一次异常调用的全部依据,
    而且只在这一刻存在。以前异常一抛就全丢了,`evaluation_attempts` 里
    那条失败记录只剩一句"解析不了",一张专门为留痕而建的表在最需要
    留痕的场景里什么都没留下。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_broken",
                "model": "qwen3-vl-plus",
                "usage": {"input_tokens": 42, "output_tokens": 7},
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "这不是 JSON"}}
                ],
            },
        )

    evaluator = _evaluator(handler, api_style=API_STYLE_CHAT)
    try:
        with pytest.raises(EvaluationParseError) as caught:
            _run(
                evaluator.evaluate_structured(
                    ["ref.png"],
                    "cand.png",
                    {"sku": "SW-001"},
                    {DEPTH_KEY: EvaluationDepth.FULL.value},
                )
            )
    finally:
        # 注入的客户端归调用方管,测试也是调用方
        _run(evaluator._http_client.aclose())

    meta = caught.value.vision_meta
    assert meta is not None, "解析失败的异常没有带上审计元数据"
    assert meta["response_id"] == "resp_broken"
    assert meta["model"] == "qwen3-vl-plus"
    assert meta["usage"]["output_tokens"] == 7
    assert caught.value.duration_ms is not None


def test_content_array_response_is_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": [{"type": "text", "text": _model_output()}]},
                    }
                ],
            },
        )

    payload = _run_eval(_evaluator(handler, api_style=API_STYLE_CHAT))
    assert payload["scores"]["composition"] == 82


def test_hard_fail_output_survives_the_round_trip():
    body = json.loads(_model_output())
    body.update(
        hard_fail=True,
        hard_fail_codes=[HardFailCode.GARMENT_WRONG.value],
        problems=[
            {
                "code": HardFailCode.GARMENT_WRONG.value,
                "severity": "CRITICAL",
                "message": "连体变成了分体",
                "dimension": "garment_identity",
                "hard_fail": True,
            }
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(json.dumps(body))

    payload = _run_eval(_evaluator(handler))
    assert payload["hard_fail_codes"] == [HardFailCode.GARMENT_WRONG.value]


# ---------------------------------------------------------------- 重试


def test_a_transient_status_is_retried_then_succeeds():
    """只留一个有代表性的瞬时状态码。

    哪些状态码算瞬时,在 tests/pure/test_vision_evaluator.py 的
    test_status_classification_table 里逐个定义。这一层验证的是
    「真的经由 httpx 走完了一次重试」,不是分类表的副本。
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": {"message": "temporary"}})
        return _ok_response()

    _run_eval(_evaluator(handler))
    assert calls["n"] == 2


def test_timeouts_are_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("too slow", request=request)
        return _ok_response()

    _run_eval(_evaluator(handler))
    assert calls["n"] == 2


# 重试次数上限在纯测试里验(test_retry_count_is_bounded_by_configuration),
# 那里不需要起 httpx 就能数清调用次数。


def test_auth_failure_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(ProviderAuthError):
        _run_eval(_evaluator(handler))
    assert calls["n"] == 1


def test_rate_limit_error_type_is_preserved_after_exhausting_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"retry-after": "0"}, json={"error": {"message": "slow down"}}
        )

    with pytest.raises(ProviderRateLimitError):
        _run_eval(_evaluator(handler, max_retries=1))


# ---------------------------------------------------------------- 失败路径


def test_refusal_fails_explicitly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"model": "m", "choices": [{"message": {"refusal": "我不能评价这张图"}}]},
        )

    with pytest.raises(ProviderContentSafetyError):
        _run_eval(_evaluator(handler, api_style=API_STYLE_CHAT))


def test_content_filter_fails_explicitly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"finish_reason": "content_filter", "message": {"content": ""}}],
            },
        )

    with pytest.raises(ProviderContentSafetyError):
        _run_eval(_evaluator(handler, api_style=API_STYLE_CHAT))


def test_truncated_output_fails_with_actionable_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"finish_reason": "length", "message": {"content": '{"scor'}}],
            },
        )

    with pytest.raises(ProviderInputError) as excinfo:
        _run_eval(_evaluator(handler, api_style=API_STYLE_CHAT))
    assert "VISION_MODEL_MAX_OUTPUT_TOKENS" in excinfo.value.message


def test_prose_output_becomes_a_parse_error():
    """散文一律拒收。绝不给一个"看起来还行"的默认分。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response("这张图整体不错,我给 85 分。")

    with pytest.raises(EvaluationParseError):
        _run_eval(_evaluator(handler))


def test_html_error_page_does_not_crash_the_parser():
    """网关返回 HTML 是常事,不该表现成一个含混的 JSONDecodeError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<!DOCTYPE html><html>502 Bad Gateway</html>")

    with pytest.raises(ProviderError) as excinfo:
        _run_eval(_evaluator(handler, max_retries=0))
    assert "HTML" in excinfo.value.message


def test_empty_output_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "m", "output": []})

    with pytest.raises(ProviderInputError):
        _run_eval(_evaluator(handler))


# ---------------------------------------------------------------- 不泄密


def test_no_secret_appears_in_any_error_message():
    for status in (400, 401, 403, 429, 500):

        def handler(request: httpx.Request, status: int = status) -> httpx.Response:
            return httpx.Response(status, json={"error": {"message": "boom"}})

        with pytest.raises(Exception) as excinfo:
            _run_eval(_evaluator(handler, max_retries=0))
        blob = f"{excinfo.value}{getattr(excinfo.value, 'detail', '')}"
        assert SECRET not in blob


def test_raw_metadata_contains_no_base64():
    """raw_response 会进数据库和界面。base64 进去会把两边都撑爆,而且毫无用处。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response()

    payload = _run_eval(_evaluator(handler))
    blob = json.dumps(payload, ensure_ascii=False)
    assert "data:image" not in blob
    assert "base64" not in blob
    assert SECRET not in blob


def test_images_are_sent_inline_as_data_urls_by_default():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok_response()

    _run_eval(_evaluator(handler))
    content = seen["body"]["input"][1]["content"]
    images = [c for c in content if c["type"] == "input_image"]
    assert len(images) == 2
    assert all(i["image_url"].startswith("data:image/png;base64,") for i in images)


# ---------------------------------------------------------------- 连接测试


def test_connection_probe_reports_reachable():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_output_tokens"] == 64  # 最小请求,不产生业务级开销
        return _ok_response('{"ok": true}')

    result = _run_probe(handler)
    assert result["configured"] is True
    assert result["reachable"] is True
    assert result["latency_ms"] is not None
    assert result["api_style"] == "responses"


def test_connection_probe_turns_errors_into_a_result_object():
    """后台页面不能因为自检抛异常而打不开。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    result = _run_probe(handler)
    assert result["reachable"] is False
    assert SECRET not in result["message"]
