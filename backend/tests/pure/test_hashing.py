"""文件哈希与分片路径。对应需求第八章:相同哈希不得重复存储。"""
from __future__ import annotations

import io

from app.core.hashing import hash_bytes, hash_stream, shard_path
from tests.pure._helpers import expect_raises, png_bytes  # noqa: F401


def test_same_content_yields_same_hash():
    data = png_bytes()
    assert hash_bytes(data) == hash_bytes(bytes(data))


def test_different_content_yields_different_hash():
    assert hash_bytes(png_bytes(color=(10, 10, 10))) != hash_bytes(png_bytes(color=(240, 240, 240)))


def test_stream_hash_matches_bytes_hash():
    data = png_bytes()
    assert hash_stream(io.BytesIO(data)) == hash_bytes(data)


def test_stream_hash_handles_multi_chunk_files():
    big = b"x" * (3 * 1024 * 1024 + 17)
    assert hash_stream(io.BytesIO(big)) == hash_bytes(big)


def test_shard_path_is_two_level_and_deterministic():
    h = hash_bytes(b"garment-front")
    p1 = shard_path(h, ".jpg")
    p2 = shard_path(h, "jpg")  # 无点前缀也应归一化
    assert p1 == p2
    assert p1 == f"{h[0:2]}/{h[2:4]}/{h}.jpg"


def test_shard_path_rejects_short_hash():
    expect_raises(ValueError, shard_path, "ab", ".jpg")
