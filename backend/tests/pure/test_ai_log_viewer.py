"""AI 日志查看器对超长结构化事件的基本契约。"""
from __future__ import annotations

import contextlib
import io

from tools.watch_ai_logs import _print_event


def test_viewer_prints_payload_sections_without_images_or_secrets():
    row = {
        "ts": "2026-08-13T21:51:36+0800",
        "level": "INFO",
        "logger": "app.llm.transport",
        "message": "llm http response received",
        "request_id": "req-1",
        "llm_call_id": "call-1",
        "http_status": 200,
        "response_body": {
            "id": "resp-1",
            "output": [{"text": "评分完成"}],
        },
    }
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        _print_event(row)

    text = output.getvalue()
    assert "llm http response received" in text
    assert "request_id=req-1" in text
    assert "脱敏响应体" in text
    assert "评分完成" in text


def test_viewer_ignores_unrelated_health_requests():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        _print_event(
            {
                "logger": "app.main",
                "message": "http request completed",
                "path": "/api/health",
            }
        )
    assert output.getvalue() == ""
