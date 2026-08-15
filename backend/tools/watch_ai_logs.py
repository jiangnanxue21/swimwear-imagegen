r"""实时查看本地 AI 请求日志。

用法（仓库根目录）：

    backend\.venv\Scripts\python.exe backend\tools\watch_ai_logs.py

这个查看器只读日志文件，不启动、不停止 API。Windows 下 API 重定向文件通常使用
系统代码页，因此先按本机首选编码读取；单行 JSON 解析失败时只报告位置，不把整条
2MB 请求重复刷到屏幕上。
"""
from __future__ import annotations

import json
import locale
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = BACKEND_ROOT / ".api-stdout.log"
AI_MESSAGES = {
    "image compacted for multimodal request budget",
    "vision request fitted to endpoint body budget",
    "llm request prepared",
    "llm request attempt started",
    "llm http response received",
    "llm request completed",
    "llm request attempt failed",
    "llm request retrying",
    "vision evaluation completed",
}


def _encoding() -> str:
    return locale.getpreferredencoding(False) or "utf-8"


def _compact(value: Any, *, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + f"…({len(text)} chars)"


def _print_payload(label: str, payload: Any) -> None:
    print(f"\n  {label}:")
    pretty = json.dumps(payload, ensure_ascii=False, indent=2)
    for line in pretty.splitlines():
        print(f"    {line}")


def _print_event(row: dict[str, Any]) -> None:
    message = str(row.get("message") or "")
    path = str(row.get("path") or "")
    logger = str(row.get("logger") or "")
    if (
        message not in AI_MESSAGES
        and not logger.startswith(("app.llm", "app.evaluators"))
        and "evaluation-test" not in path
    ):
        return

    print("\n" + "=" * 96)
    print(
        f"[{row.get('ts', '-')}] {message}\n"
        f"request_id={row.get('request_id', '-')}  "
        f"llm_call_id={row.get('llm_call_id', '-')}"
    )

    hidden = {
        "ts", "level", "logger", "message", "request_id", "llm_call_id",
        "request_body", "response_body",
    }
    fields = {key: value for key, value in row.items() if key not in hidden and value is not None}
    for key, value in fields.items():
        print(f"  {key}: {_compact(value)}")

    if "request_body" in row:
        _print_payload("脱敏请求体", row["request_body"])
    if "response_body" in row:
        _print_payload("脱敏响应体", row["response_body"])
    sys.stdout.flush()


def _follow(path: Path) -> None:
    encoding = _encoding()
    print(f"AI 实时日志：{path}")
    print(f"读取编码：{encoding}；Ctrl+C 只停止查看，不停止 API。")
    print("等待新的 AI 请求……")
    sys.stdout.flush()

    with path.open("r", encoding=encoding, errors="replace") as stream:
        stream.seek(0, 2)
        while True:
            line = stream.readline()
            if not line:
                time.sleep(0.2)
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"\n[查看器] 跳过无法解析的日志行：JSON 第 {exc.pos} 字符处错误")
                continue
            if isinstance(row, dict):
                _print_event(row)


def main() -> int:
    path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_LOG
    if not path.exists():
        print(f"日志文件不存在：{path}", file=sys.stderr)
        return 2
    try:
        _follow(path)
    except KeyboardInterrupt:
        print("\n已停止查看；API 未停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
