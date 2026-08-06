"""配置项的来源判定:某个键到底是不是环境变量给的。

设置页要回答两个问题,两个都只有\"来源\"能回答:

    这一项现在的值是谁给的?      —— 显示 环境变量 / 后台 / 默认值
    这一项后台能不能改?          —— SETTINGS_ENV_LOCK 打开时,环境变量给了就不能改

判定范围与 pydantic-settings 一致:进程环境变量 + 项目根的 .env 文件。
只用标准库,任何层都能安全导入,也能在纯测试里直接跑。
"""
from __future__ import annotations

import os
from pathlib import Path

from app.core.dotenv import parse_dotenv

#: backend/ 的上一级,即项目根。刻意不从 config.py 取,避免为了一个路径拖进 pydantic。
PROJECT_ROOT = Path(__file__).resolve().parents[3]

_cache: frozenset[str] | None = None


def _dotenv_keys(root: Path) -> set[str]:
    path = root / ".env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    # 只认\"写了值\"的行:KEY= 空值等于没配,不该让后台变只读
    return {k.upper() for k, v in parse_dotenv(text).items() if str(v).strip()}


def env_provided_keys(root: Path | None = None) -> frozenset[str]:
    """环境变量(含 .env)里显式给了非空值的键名集合,全大写。"""
    global _cache
    if root is None and _cache is not None:
        return _cache

    base = root or PROJECT_ROOT
    keys = {k.upper() for k, v in os.environ.items() if str(v).strip()}
    keys |= _dotenv_keys(base)
    result = frozenset(keys)
    if root is None:
        _cache = result
    return result


def is_env_provided(name: str, root: Path | None = None) -> bool:
    return name.strip().upper() in env_provided_keys(root)


def reset_cache() -> None:
    """测试用。生产里环境变量在进程生命周期内不变,不需要调用。"""
    global _cache
    _cache = None
