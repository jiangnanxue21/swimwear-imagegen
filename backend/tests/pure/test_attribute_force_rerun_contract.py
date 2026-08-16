"""属性识别显式重跑的跨层契约。

运行时的行锁与部分唯一索引由 requires_db 用例覆盖；这里守住日常离线门禁也能
检查的接缝，避免接口收了 force、服务没读，或前端按钮仍发普通幂等请求。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
FRONTEND = ROOT.parent / "frontend" / "src"


def _tree(relative: str) -> ast.Module:
    return ast.parse((APP / relative).read_text(encoding="utf-8"))


def _function(relative: str, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(_tree(relative))
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_force_rerun_defaults_off_and_is_forwarded_by_both_http_entries():
    schema = _tree("schemas/attribute.py")
    request = next(
        node
        for node in schema.body
        if isinstance(node, ast.ClassDef) and node.name == "ExtractRequest"
    )
    force = next(
        node
        for node in request.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "force_rerun"
    )
    assert isinstance(force.value, ast.Constant) and force.value.value is False

    for endpoint in ("extract_attributes", "create_spu_extraction_run"):
        body = ast.unparse(_function("api/attributes.py", endpoint))
        assert "force_rerun=payload.force_rerun" in body


def test_force_only_supersedes_a_completed_result_and_keeps_inflight_dedupe():
    body = ast.unparse(_function("attributes/service.py", "run_extraction"))
    assert "verdict == run_state.ReuseVerdict.REUSE_RESULT and force_rerun" in body
    assert "existing.idempotency_key = None" in body
    assert "row._request_disposition = run_state.ReuseVerdict.NEW_RUN" in body
    assert "winner._request_disposition = run_state.ReuseVerdict.RETURN_EXISTING" in body


def test_attribute_button_explicitly_requests_a_rerun_and_consumes_disposition():
    api = (FRONTEND / "api" / "attributes.ts").read_text(encoding="utf-8")
    tab = (
        FRONTEND / "components" / "workbench" / "AttributeTab.tsx"
    ).read_text(encoding="utf-8")
    assert "force_rerun: forceRerun" in api
    assert "product?.spu_id,\n      // 这是用户明确点击的付费动作" in tab
    assert "result.request_disposition === 'REUSE_RESULT'" in tab
    assert "result.request_disposition === 'RETURN_EXISTING'" in tab
