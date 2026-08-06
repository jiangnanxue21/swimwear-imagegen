"""本地存储:哈希去重 + 原始文件永不覆盖 + 路径穿越防护。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.core.errors import ValidationError
from app.core.hashing import hash_bytes
from app.services.storage import LocalObjectStorage, build_storage
from tests.pure._helpers import expect_raises, jpeg_bytes


def _storage(tmp: str) -> LocalObjectStorage:
    return LocalObjectStorage(tmp, "http://localhost:8000")


def test_saves_file_under_hash_derived_path():
    with tempfile.TemporaryDirectory() as tmp:
        data = jpeg_bytes()
        h = hash_bytes(data)
        stored = _storage(tmp).save(data, file_hash=h, extension=".jpg")
        assert stored.storage_path == f"{h[0:2]}/{h[2:4]}/{h}.jpg"
        assert (Path(tmp) / stored.storage_path).read_bytes() == data
        assert stored.deduplicated is False


def test_same_content_is_stored_only_once():
    with tempfile.TemporaryDirectory() as tmp:
        s = _storage(tmp)
        data = jpeg_bytes()
        h = hash_bytes(data)
        first = s.save(data, file_hash=h, extension=".jpg")
        second = s.save(data, file_hash=h, extension=".jpg")
        assert first.storage_path == second.storage_path
        assert second.deduplicated is True
        files = [p for p in Path(tmp).rglob("*") if p.is_file()]
        assert len(files) == 1, "相同哈希不得重复落盘"


def test_existing_original_is_never_overwritten():
    """即便传入不同内容,只要哈希路径已存在就不覆盖 —— 原始文件不可变。"""
    with tempfile.TemporaryDirectory() as tmp:
        s = _storage(tmp)
        original = jpeg_bytes(800, 800, color=(10, 20, 30))
        h = hash_bytes(original)
        s.save(original, file_hash=h, extension=".jpg")
        s.save(b"tampered-content", file_hash=h, extension=".jpg")
        assert (Path(tmp) / f"{h[0:2]}/{h[2:4]}/{h}.jpg").read_bytes() == original


def test_no_partial_files_left_behind():
    with tempfile.TemporaryDirectory() as tmp:
        s = _storage(tmp)
        data = jpeg_bytes()
        s.save(data, file_hash=hash_bytes(data), extension=".jpg")
        assert not list(Path(tmp).rglob("*.part"))


def test_prefix_isolates_namespaces():
    with tempfile.TemporaryDirectory() as tmp:
        s = _storage(tmp)
        data = jpeg_bytes()
        h = hash_bytes(data)
        a = s.save(data, file_hash=h, extension=".jpg", prefix="uploads")
        b = s.save(data, file_hash=h, extension=".jpg", prefix="outputs")
        assert a.storage_path.startswith("uploads/") and b.storage_path.startswith("outputs/")


def test_read_and_exists_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        s = _storage(tmp)
        data = jpeg_bytes()
        stored = s.save(data, file_hash=hash_bytes(data), extension=".jpg")
        assert s.exists(stored.storage_path)
        assert s.read(stored.storage_path) == data


def test_public_url_is_built_from_base():
    with tempfile.TemporaryDirectory() as tmp:
        s = LocalObjectStorage(tmp, "http://localhost:8000/")
        assert s.public_url("ab/cd/x.jpg") == "http://localhost:8000/files/ab/cd/x.jpg"


def test_traversal_path_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        expect_raises(ValidationError, _storage(tmp).read, "../../etc/passwd")


def test_build_storage_selects_backend():
    with tempfile.TemporaryDirectory() as tmp:
        assert isinstance(build_storage("local", tmp, ""), LocalObjectStorage)
        expect_raises(ValueError, build_storage, "ftp", tmp, "")


# ---------- S3 / MinIO(阶段 6)----------

def test_s3_requires_a_bucket():
    from app.services.storage import S3ObjectStorage

    expect_raises(ValueError, S3ObjectStorage, bucket="")


def test_s3_uses_the_same_sharded_key_as_local():
    """两种后端路径推导必须一致,否则迁移时数据库里的 storage_path 全得改。"""
    from app.core.hashing import shard_path
    from app.services.storage import S3ObjectStorage

    storage = S3ObjectStorage(bucket="imagegen")
    digest = "a" * 64
    assert storage._key(digest, ".jpg", "outputs") == f"outputs/{shard_path(digest, '.jpg')}"
    assert storage._key(digest, ".jpg", "") == shard_path(digest, ".jpg")


def test_s3_public_url_prefers_the_cdn_domain():
    from app.services.storage import S3ObjectStorage

    cdn = S3ObjectStorage(bucket="b", public_base_url="https://cdn.example.com/")
    assert cdn.public_url("outputs/ab/cd/x.jpg") == "https://cdn.example.com/outputs/ab/cd/x.jpg"


def test_s3_public_url_falls_back_to_the_endpoint():
    from app.services.storage import S3ObjectStorage

    minio = S3ObjectStorage(bucket="imagegen", endpoint_url="http://minio:9000")
    assert minio.public_url("ab/cd/x.jpg") == "http://minio:9000/imagegen/ab/cd/x.jpg"


def test_s3_client_is_lazy_so_boto3_is_optional():
    """本地开发不该被迫装 boto3 —— 只有真正用到 client 时才导入。"""
    from app.services.storage import S3ObjectStorage

    storage = S3ObjectStorage(bucket="b")
    assert storage._client is None


def test_s3_content_type_is_derived_from_extension():
    from app.services.storage import _content_type

    assert _content_type(".jpg") == "image/jpeg"
    assert _content_type("png") == "image/png"
    assert _content_type(".bin") == "application/octet-stream"
