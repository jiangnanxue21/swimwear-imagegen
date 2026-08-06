"""日志脱敏。对应需求第十九章:日志不得记录密钥。"""
from __future__ import annotations

from app.core.logging import redact
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401


def test_redacts_api_key_variants():
    out = redact({"FASHN_API_KEY": "sk-live-123", "fal_api_key": "abc", "apiKey": "z"})
    assert set(out.values()) == {"***"}


def test_redacts_password_token_and_authorization():
    out = redact({"password": "p", "access_token": "t", "Authorization": "Bearer x", "secret": "s"})
    assert set(out.values()) == {"***"}


def test_keeps_non_secret_fields():
    out = redact({"provider": "mock", "candidate_count": 4})
    assert out == {"provider": "mock", "candidate_count": 4}


def test_redacts_nested_structures():
    out = redact({"provider": {"name": "fashn", "api_key": "sk-1"}, "items": [{"token": "t"}]})
    assert out["provider"]["api_key"] == "***"
    assert out["provider"]["name"] == "fashn"
    assert out["items"][0]["token"] == "***"
