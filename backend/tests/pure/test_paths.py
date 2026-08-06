"""路径安全。对应需求第十九章:防止路径穿越、原始文件不得覆盖。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.core.errors import ErrorCode, ValidationError
from app.core.paths import (
    is_hidden_path,
    resolve_within,
    safe_extension,
    sanitize_filename,
)
from tests.pure._helpers import expect_raises

ALLOWED = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def test_sanitize_strips_directory_components():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe"


def test_sanitize_handles_unicode_and_spaces():
    out = sanitize_filename("泳衣 正面图 v2.jpg")
    assert "/" not in out and "\\" not in out
    assert out.endswith(".jpg")


def test_sanitize_never_returns_empty():
    assert sanitize_filename("") == "unnamed"
    assert sanitize_filename("///") == "unnamed"
    assert sanitize_filename("...") == "unnamed"


def test_sanitize_truncates_long_names():
    assert len(sanitize_filename("a" * 500 + ".jpg")) <= 120


def test_safe_extension_whitelist():
    assert safe_extension("a.JPG", allowed=ALLOWED) == ".jpg"
    assert safe_extension("payload.svg", allowed=ALLOWED) == ""
    assert safe_extension("no-ext", allowed=ALLOWED, default=".bin") == ".bin"


def test_resolve_within_allows_normal_relative_path():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        got = resolve_within(base, "ab/cd/file.jpg")
        assert str(got).startswith(str(base.resolve()))


def test_resolve_within_blocks_parent_escape():
    with tempfile.TemporaryDirectory() as tmp:
        err = expect_raises(ValidationError, resolve_within, Path(tmp), "../../secret.txt")
        assert err.code == ErrorCode.PATH_TRAVERSAL


def test_resolve_within_blocks_absolute_path():
    with tempfile.TemporaryDirectory() as tmp:
        err = expect_raises(ValidationError, resolve_within, Path(tmp), "/etc/passwd")
        assert err.code == ErrorCode.PATH_TRAVERSAL


# ---------------------------------------------------------------- 隐藏文件

def test_hidden_path_detects_dotfiles_at_any_depth():
    for path in (".settings.key", ".env", "sub/.env", "a/b/.git/config", ".git/HEAD"):
        assert is_hidden_path(path), f"没挡住 {path}"


def test_hidden_path_allows_ordinary_assets():
    for path in ("2026/07/abc.png", "outputs/sku-1_main_1200.jpg", "a.b/c.png"):
        assert not is_hidden_path(path), f"误伤了 {path}"


def test_hidden_path_treats_backslash_as_a_separator():
    """别让 Windows 风格的分隔符成为绕过口。"""
    assert is_hidden_path("sub\\.settings.key")
