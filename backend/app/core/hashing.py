"""文件哈希与去重路径推导。仅标准库。

去重规则(需求第八章):相同 sha256 的文件不得重复存储。
存储路径由哈希推导,天然幂等,同一文件多次上传落到同一路径。
"""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import BinaryIO

CHUNK_SIZE = 1024 * 1024


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_stream(stream: BinaryIO) -> str:
    """流式哈希,避免大文件一次性读入内存。不负责 seek 复位。"""
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def shard_path(file_hash: str, extension: str) -> str:
    """由哈希推导两级分片相对路径,避免单目录文件过多。

    >>> shard_path("abcdef1234", ".jpg")
    'ab/cd/abcdef1234.jpg'
    """
    if len(file_hash) < 4:
        raise ValueError("file_hash too short")
    ext = extension.lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    return str(PurePosixPath(file_hash[0:2], file_hash[2:4], f"{file_hash}{ext}"))
