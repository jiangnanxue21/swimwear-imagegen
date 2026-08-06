"""落库异常文本的脱敏(BLOCK-15)。

这一批断言钉的是同一件事:**签名 URL 不能进数据库**。
候选图下载失败的原文经 `GET /api/generation-tasks/{id}` 原样回到浏览器,
而候选图地址在有效期内是免鉴权可读的。
"""
from __future__ import annotations

from app.core.errors import ErrorCode, ValidationError
from app.core.redaction import describe_exception, safe_error_message, scrub
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401

#: 真实形状:httpx 对一个 403 的签名地址抛出来的原文
SIGNED_URL_ERROR = (
    "Client error '403 Forbidden' for url "
    "'https://fal.media/files/rabbit/x.png"
    "?X-Amz-Credential=AKIAIOSFODNN7EXAMPLE"
    "&X-Amz-Signature=9f3c1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d'"
)


def test_scrub_removes_signed_url_entirely():
    out = scrub(SIGNED_URL_ERROR)
    assert "fal.media" not in out
    assert "X-Amz-Signature" not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "https://" not in out


def test_scrub_keeps_the_status_code():
    """403 和 404 决定运营下一步做什么,不能一起抹掉。"""
    assert "403" in scrub(SIGNED_URL_ERROR)


def test_scrub_removes_broker_credentials():
    out = scrub("ConnectionError: Error connecting to redis://:hunter2@10.0.0.5:6379/0")
    assert "hunter2" not in out
    assert "10.0.0.5" not in out


def test_scrub_removes_inline_key_values():
    out = scrub("auth failed: api_key=sk-proj-abc123def456ghi789jkl012mno345pqr678xyz")
    assert "sk-proj-abc123def456" not in out
    assert "api_key" in out  # 键名留着,取值不留


def test_scrub_keeps_short_identifiers():
    """错误码和短标识不该被打码,否则脱敏本身会毁掉可排查性。"""
    out = scrub("NO_CANDIDATE: provider returned 0 images")
    assert "NO_CANDIDATE" in out


def test_describe_exception_keeps_type_and_status():
    class Resp:
        status_code = 404

    class Boom(Exception):
        response = Resp()

    assert describe_exception(Boom()) == "Boom(404)"


def test_describe_exception_drops_foreign_message():
    """第三方异常只留结构 —— 原文形状随库版本变,今天安全不代表明天安全。"""
    out = describe_exception(RuntimeError(SIGNED_URL_ERROR))
    assert "fal.media" not in out
    assert out.startswith("RuntimeError")


def test_safe_error_message_keeps_our_own_wording():
    """自家 AppError 的中文文案直接决定运营做什么,保留。"""
    exc = ValidationError("候选图超过下载上限", code=ErrorCode.INPUT_INVALID)
    assert safe_error_message(exc) == "候选图超过下载上限"


def test_safe_error_message_scrubs_our_own_wording_too():
    """自家异常也可能把第三方原文拼进 message。"""
    exc = ValidationError(f"拒绝下载:{SIGNED_URL_ERROR}", code=ErrorCode.INPUT_INVALID)
    out = safe_error_message(exc)
    assert "拒绝下载" in out
    assert "X-Amz-Signature" not in out


def test_output_is_length_bounded():
    assert len(scrub("x" * 5000)) <= 500
