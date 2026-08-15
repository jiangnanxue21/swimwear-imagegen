"""日志脱敏。对应需求第十九章:日志不得记录密钥。"""
from __future__ import annotations

import logging

from app.core.logging import get_logger, redact
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401


def test_redacts_api_key_variants():
    out = redact({"FASHN_API_KEY": "sk-live-123", "fal_api_key": "abc", "apiKey": "z"})
    assert set(out.values()) == {"***"}


def test_redacts_password_token_and_authorization():
    out = redact({"password": "p", "access_token": "t", "Authorization": "Bearer x", "secret": "s"})
    assert set(out.values()) == {"***"}


def test_keeps_non_secret_fields():
    out = redact({
        "provider": "mock",
        "candidate_count": 4,
        "max_output_tokens": 8192,
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    })
    assert out == {
        "provider": "mock",
        "candidate_count": 4,
        "max_output_tokens": 8192,
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    }


def test_redacts_nested_structures():
    out = redact({"provider": {"name": "fashn", "api_key": "sk-1"}, "items": [{"token": "t"}]})
    assert out["provider"]["api_key"] == "***"
    assert out["provider"]["name"] == "fashn"
    assert out["items"][0]["token"] == "***"


def test_logger_adapter_keeps_call_site_structured_fields():
    class Handler(logging.Handler):
        record: logging.LogRecord | None = None

        def emit(self, record: logging.LogRecord) -> None:
            self.record = record

    handler = Handler()
    target = logging.getLogger("test.structured-fields")
    target.addHandler(handler)
    original_level = target.level
    target.setLevel(logging.INFO)
    try:
        get_logger(target.name).info(
            "request sent",
            extra={"extra_fields": {"attempt": 2, "request_body_bytes": 123}},
        )
    finally:
        target.removeHandler(handler)
        target.setLevel(original_level)

    assert handler.record is not None
    assert handler.record.extra_fields == {"attempt": 2, "request_body_bytes": 123}
