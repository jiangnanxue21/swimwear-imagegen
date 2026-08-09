"""导入预览凭证：提交必须对应同一文件、同一数据库判定和同一操作者。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

TOKEN_VERSION = "v1"
TOKEN_TTL_SECONDS = 15 * 60


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue(
    key: bytes,
    *,
    actor: str,
    content_digest: str,
    preview_digest: str,
    now: int | None = None,
) -> str:
    payload = {
        "actor": actor,
        "content": content_digest,
        "preview": preview_digest,
        "exp": int(now if now is not None else time.time()) + TOKEN_TTL_SECONDS,
    }
    encoded = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    signed = f"{TOKEN_VERSION}.{encoded}"
    signature = _b64(hmac.new(key, signed.encode(), hashlib.sha256).digest())
    return f"{signed}.{signature}"


@dataclass(frozen=True)
class Verification:
    valid: bool
    reason: str = ""


def verify(
    token: str,
    key: bytes,
    *,
    actor: str,
    content_digest: str,
    preview_digest: str,
    now: int | None = None,
) -> Verification:
    try:
        version, encoded, supplied = token.split(".", 2)
        signed = f"{version}.{encoded}"
        expected = _b64(hmac.new(key, signed.encode(), hashlib.sha256).digest())
        if version != TOKEN_VERSION or not hmac.compare_digest(expected, supplied):
            return Verification(False, "签名无效")
        payload = json.loads(_unb64(encoded))
    except (ValueError, TypeError, json.JSONDecodeError):
        return Verification(False, "格式无效")
    current = int(now if now is not None else time.time())
    if int(payload.get("exp", 0)) < current:
        return Verification(False, "预览已过期")
    if payload.get("actor") != actor:
        return Verification(False, "预览不属于当前操作者")
    if payload.get("content") != content_digest:
        return Verification(False, "文件已变化")
    if payload.get("preview") != preview_digest:
        return Verification(False, "数据库状态已变化")
    return Verification(True)
