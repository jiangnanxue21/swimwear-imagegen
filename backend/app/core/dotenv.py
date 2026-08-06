"""极简 .env 解析器,仅标准库。

用途:Makefile 工具链与自检测试读取 .env.example,不替代 pydantic-settings。
"""
from __future__ import annotations

from pathlib import Path


def parse_dotenv(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def load_dotenv_file(path: str | Path) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    return parse_dotenv(p.read_text(encoding="utf-8"))
