"""并发写入路径的静态守卫(零三方依赖)。

这些断言检查的是「服务代码有没有拿锁」,不是「并发下真的不冲突」——
后者需要真库,在 tests/ 下的集成测试里。两者互补:真库测试能证明当前
实现正确,这里能在有人把锁删掉时立刻红。

来自已删除的 test_code_review_round3.py。
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT

APP_DIR = BACKEND_ROOT / "app"

# ---------------------------------------------------------------- 源码静态检查工具

def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _fn(tree, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _calls(node) -> set[str]:
    """这段代码里出现的调用名,归一成 `模块.函数` 或 `函数`。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                names.add(f"{func.value.id}.{func.attr}")
            names.add(func.attr)
    return names


def test_copy_version_is_computed_under_a_lock():
    """评审第 6 条:版本号是读-改-写,必须串行。"""
    fn = _fn(_tree(APP_DIR / "listings" / "copy_service.py"), "save_copy")
    calls = _calls(fn)
    assert "locks.advisory_xact_lock" in calls, "save_copy 没有拿 advisory lock"
    assert "session.begin_nested" in calls, "唯一冲突必须在保存点里捕获"


def test_image_set_version_is_computed_under_a_lock():
    fn = _fn(_tree(APP_DIR / "listings" / "image_set_service.py"), "create_set")
    calls = _calls(fn)
    assert "locks.advisory_xact_lock" in calls
    assert "session.begin_nested" in calls


def test_image_set_approval_is_serialized():
    """评审第 7 条:两个并发审批都提交成功 = 两个 APPROVED,且不报错。"""
    fn = _fn(_tree(APP_DIR / "listings" / "image_set_service.py"), "approve")
    assert "locks.advisory_xact_lock" in _calls(fn), "approve 没有对 scope 加锁"


def test_advisory_keys_distinguish_none_from_empty_string():
    """`(spu, None, None)` 是通用集,`(spu, "", "")` 是被填了空串的渠道字段。

    两者在库里是两条不同的记录,锁上必须也是两把不同的锁 ——
    否则通用集的操作会莫名其妙地被另一条记录挡住。
    """
    import hashlib

    def key(namespace, *parts):
        payload = ":".join([namespace, *(repr(p) for p in parts)])
        return int.from_bytes(
            hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big", signed=True
        )

    assert key("s", "SW-1", None, None) != key("s", "SW-1", "", "")
