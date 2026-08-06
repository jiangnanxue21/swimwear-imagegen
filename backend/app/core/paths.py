"""存储路径安全工具。仅标准库。

对应需求第十九章:防止路径穿越、原始上传文件不得覆盖。
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from app.core.errors import ErrorCode, ValidationError

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_FILENAME_LENGTH = 120


def sanitize_filename(name: str) -> str:
    """把用户提供的文件名压成安全的展示名。

    仅用于记录 original_filename 与生成后缀,绝不用于拼接落盘路径
    (落盘路径一律由 sha256 推导)。
    """
    if not name:
        return "unnamed"
    name = unicodedata.normalize("NFKD", name)
    name = name.replace("\\", "/").split("/")[-1]
    name = _UNSAFE.sub("_", name).strip("._-")
    if not name:
        return "unnamed"
    return name[:MAX_FILENAME_LENGTH]


def safe_extension(name: str, *, allowed: frozenset[str], default: str = "") -> str:
    """提取并校验扩展名,不在白名单内返回 default。"""
    ext = os.path.splitext(name or "")[1].lower()
    return ext if ext in allowed else default


def resolve_within(base_dir: Path, relative: str) -> Path:
    """把相对路径安全地解析到 base_dir 内部,越界即报错。"""
    base = Path(base_dir).resolve()
    if os.path.isabs(relative) or relative.startswith("\\"):
        raise ValidationError("路径必须是相对路径", code=ErrorCode.PATH_TRAVERSAL)
    candidate = (base / relative).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValidationError("检测到非法路径", code=ErrorCode.PATH_TRAVERSAL)
    return candidate


def is_hidden_path(path: str) -> bool:
    """URL 路径里有没有以点开头的路径段。

    静态文件挂载用它来挡住 ``.settings.key`` / ``.env`` / ``.git`` 这类东西。
    Starlette 的 StaticFiles 只防目录穿越,不防隐藏文件 —— 目录里有什么就发什么,
    而那个目录是给用户素材和生成结果用的公开目录,谁都可能往里放东西。

    刻意做成纯函数放在这里:静态托管的行为不该只能靠起一个 ASGI 服务才测得了。
    反斜杠一并当分隔符处理,免得 ``.\\.settings.key`` 这种写法绕过去。
    """
    if not path:
        return False
    normalized = path.replace("\\", "/")
    return any(part.startswith(".") for part in normalized.split("/") if part)
