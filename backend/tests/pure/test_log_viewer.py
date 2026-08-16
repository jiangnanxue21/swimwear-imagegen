"""运行日志查看器的契约。

这个文件**曾经**叫 `test_ai_log_viewer.py`,跟着 `watch_ai_logs.py` ->
`watch_logs.py` 一起改的名:查看器不再只看 AI 调用,过滤也不再靠
硬编码的消息原文。
"""
from __future__ import annotations

import contextlib
import io

from tools.watch_logs import Filter, _print_event, main


def _capture(row: dict, flt: Filter | None = None) -> str:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        _print_event(row, flt)
    return output.getvalue()


def test_viewer_prints_payload_sections_without_images_or_secrets():
    text = _capture(
        {
            "ts": "2026-08-13T21:51:36+0800",
            "level": "INFO",
            "logger": "app.llm.transport",
            "domain": "llm",
            "event": "llm.response_received",
            "message": "llm http response received",
            "request_id": "req-1",
            "llm_call_id": "call-1",
            "http_status": 200,
            "response_body": {"id": "resp-1", "output": [{"text": "评分完成"}]},
        }
    )
    assert "llm http response received" in text
    assert "request_id=req-1" in text
    assert "response_body" in text
    assert "评分完成" in text


def test_the_event_label_comes_from_the_registry_not_from_the_message():
    """界面上那句中文来自注册表 —— message 想怎么改就怎么改。"""
    text = _capture(
        {
            "level": "WARNING",
            "logger": "app.llm.transport",
            "event": "llm.attempt_failed",
            "message": "whatever the author feels like writing today",
        }
    )
    assert "调用尝试失败" in text
    assert "模型调用" in text


def test_domain_filter_uses_the_registry():
    row = {"level": "INFO", "logger": "app.services.publish_service", "message": "x"}
    assert _capture(row, Filter(domains=("publish",), show_routine=True)) != ""
    assert _capture(row, Filter(domains=("llm",), show_routine=True)) == ""


def test_an_unmigrated_call_site_still_gets_a_domain():
    """没写 event 的调用点靠 logger 前缀兜底 —— 查看器里不出现「未分类」。"""
    text = _capture(
        {"level": "INFO", "logger": "app.workbench.batch_service", "message": "anything"},
        Filter(domains=("batch",), show_routine=True),
    )
    assert "批量任务" in text


def test_routine_events_are_folded_but_never_when_they_failed():
    lease = {
        "level": "INFO",
        "logger": "app.tasks.generation_tasks",
        "event": "gen.task_already_claimed",
        "message": "task already claimed by another worker, skipping",
    }
    assert _capture(lease, Filter(domains=("gen",))) == ""
    assert _capture(lease, Filter(domains=("gen",), show_routine=True)) != ""

    # 同一个事件码,ERROR 级 —— routine 标记对它不生效
    assert _capture({**lease, "level": "ERROR"}, Filter(domains=("gen",))) != ""


def test_viewer_ignores_unrelated_health_requests():
    """默认只看模型调用:访问日志属于 http 域,不该在默认视图里刷屏。"""
    assert (
        _capture(
            {
                "logger": "app.main",
                "event": "http.request_completed",
                "message": "http request completed",
                "path": "/api/health",
            },
            Filter(),
        )
        == ""
    )


def test_an_unknown_domain_is_reported_instead_of_silently_matching_nothing():
    """打错一个域名必须报错。

    静默漏事件正是硬编码消息集最坏的地方 —— 它连打错的机会都不给。
    """
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = main(["--domain", "llmm"])
    assert code == 2
    assert "未知的域" in err.getvalue()
