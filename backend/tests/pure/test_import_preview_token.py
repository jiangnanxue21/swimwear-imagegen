from __future__ import annotations

from app.workbench import import_token


def test_preview_token_binds_actor_file_and_database_verdict():
    key = b"test-key"
    token = import_token.issue(
        key,
        actor="alice",
        content_digest="file-a",
        preview_digest="plan-a",
        now=100,
    )
    assert import_token.verify(
        token,
        key,
        actor="alice",
        content_digest="file-a",
        preview_digest="plan-a",
        now=101,
    ).valid
    assert not import_token.verify(
        token,
        key,
        actor="alice",
        content_digest="file-b",
        preview_digest="plan-a",
        now=101,
    ).valid
    assert not import_token.verify(
        token,
        key,
        actor="alice",
        content_digest="file-a",
        preview_digest="plan-b",
        now=101,
    ).valid
    assert not import_token.verify(
        token,
        key,
        actor="bob",
        content_digest="file-a",
        preview_digest="plan-a",
        now=101,
    ).valid


def test_preview_token_expires_and_rejects_tampering():
    key = b"test-key"
    token = import_token.issue(
        key,
        actor="alice",
        content_digest="file-a",
        preview_digest="plan-a",
        now=100,
    )
    assert not import_token.verify(
        token,
        key,
        actor="alice",
        content_digest="file-a",
        preview_digest="plan-a",
        now=100 + import_token.TOKEN_TTL_SECONDS + 1,
    ).valid
    assert not import_token.verify(
        token[:-1] + ("A" if token[-1] != "A" else "B"),
        key,
        actor="alice",
        content_digest="file-a",
        preview_digest="plan-a",
        now=101,
    ).valid
