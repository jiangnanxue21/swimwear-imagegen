"""多模态调用日志必须既可排障，又不能复制密钥或图片。"""
from __future__ import annotations

import asyncio
import logging

from app.llm.images import PreparedImage
from app.llm.redaction import safe_payload_for_log
from app.llm.transport import LLMRequest, MultimodalClient


class _Config:
    api_key = "secret"
    base_url = "https://example.invalid/v1"
    model = "vision-test"
    api_style = "responses"
    timeout_seconds = 90.0
    max_retries = 0
    retry_base_seconds = 0.1


class _Handler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _request() -> LLMRequest:
    image = PreparedImage(
        role="CANDIDATE",
        mime_type="image/png",
        byte_size=3,
        data_url="data:image/png;base64,SEVMTE8=",
    )
    return LLMRequest(
        url="https://example.invalid/v1/responses",
        headers={"Authorization": "Bearer top-secret"},
        body={
            "model": "vision-test",
            "input": [{"image_url": image.data_url}],
            "signed_url": "https://files.invalid/a.png?sig=do-not-log",
            "api_key": "do-not-log",
        },
        images=[image],
    )


def test_payload_log_keeps_shape_but_redacts_images_secrets_and_signed_queries():
    safe = safe_payload_for_log(_request().body)
    blob = repr(safe)
    assert safe["model"] == "vision-test"
    assert safe["input"][0]["image_url"]["redacted"] == "inline_image"
    assert safe["api_key"] == "***"
    assert safe["signed_url"] == "https://files.invalid/a.png"
    for forbidden in ("SEVMTE8=", "do-not-log", "?sig=", "top-secret"):
        assert forbidden not in blob


def test_text_error_body_cannot_echo_an_image_or_bearer_secret_into_logs():
    safe = safe_payload_for_log(
        "upstream echoed data:image/png;base64,SEVMTE8= and Bearer sk-private"
    )
    assert "SEVMTE8=" not in safe
    assert "sk-private" not in safe
    assert "inline_image mime=image/png" in safe


def test_every_attempt_has_started_and_completed_lifecycle_logs():
    import app.llm.transport as transport

    handler = _Handler()
    target = logging.getLogger(transport.__name__)
    target.addHandler(handler)
    original_level = target.level
    target.setLevel(logging.INFO)

    async def send_once(request, client):
        return {"id": "resp-1", "model": "vision-routed", "output": []}, 200

    try:
        asyncio.run(
            MultimodalClient(_Config(), http_client=object()).send(
                _request(), send_once=send_once
            )
        )
    finally:
        target.removeHandler(handler)
        target.setLevel(original_level)

    messages = [record.getMessage() for record in handler.records]
    assert messages == [
        "llm request prepared",
        "llm request attempt started",
        "llm request completed",
    ]
    extras = [getattr(record, "extra_fields", {}) for record in handler.records]
    assert len({row["llm_call_id"] for row in extras}) == 1
    assert extras[0]["request_body_bytes"] > 0
    assert extras[1]["attempt"] == 1
    assert extras[2]["http_status"] == 200
    assert extras[2]["response_id"] == "resp-1"
