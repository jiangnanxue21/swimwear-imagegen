"""扫描:哪些 DB 用例会撞上 `POST /api/products` 的新契约(SPU 必须先存在)。

A45-batch14-26 之后 `create_product` 解析 `spu` 字符串,查不到就 422。
batch15 修了 12-4 / 12-5 两份 fixture,这个脚本回答「还有谁没跟上」。

判据:
  - 文件里出现 `POST /api/products`(直接或经模块内 helper)
  - 且该文件**没有**任何 `POST /api/spus`

只读 AST,不需要任何第三方依赖。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent / "tests"


def _calls_in(node: ast.AST) -> list[tuple[str, str]]:
    """返回 (method, path) —— client.post("/api/xxx") 这种形状。"""
    out: list[tuple[str, str]] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        if not isinstance(fn, ast.Attribute) or fn.attr not in {"post", "get", "patch", "put", "delete"}:
            continue
        if not sub.args:
            continue
        arg = sub.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.append((fn.attr, arg.value))
    return out


def _names_called(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            out.add(sub.func.id)
    return out


def scan(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def reaches_products_post(fn: ast.AST, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        for method, p in _calls_in(fn):
            if method == "post" and p.rstrip("/") == "/api/products":
                return True
        for name in _names_called(fn):
            if name in funcs and name not in seen:
                if reaches_products_post(funcs[name], seen | {name}):
                    return True
        return False

    def reaches_spu_post(fn: ast.AST, seen: set[str] | None = None) -> bool:
        seen = seen or set()
        for method, p in _calls_in(fn):
            if method == "post" and p.rstrip("/") == "/api/spus":
                return True
        for name in _names_called(fn):
            if name in funcs and name not in seen:
                if reaches_spu_post(funcs[name], seen | {name}):
                    return True
        return False

    tests = {n: f for n, f in funcs.items() if n.startswith("test_")}
    hits = {n: (reaches_products_post(f), reaches_spu_post(f)) for n, f in tests.items()}
    return {
        "total_tests": len(tests),
        "posts_products": {n for n, (p, _) in hits.items() if p},
        "at_risk": {n for n, (p, s) in hits.items() if p and not s},
    }


def main() -> int:
    rows = []
    for path in sorted(TESTS.glob("test_*.py")):
        info = scan(path)
        if info["posts_products"]:
            rows.append((path.name, info))

    bad_total = 0
    print("文件                                              用例  建商品  仍缺 SPU 前置")
    print("-" * 86)
    for name, info in rows:
        n_bad = len(info["at_risk"])
        bad_total += n_bad
        flag = "  <-- 会 422" if n_bad else ""
        print(f"{name:<50}{info['total_tests']:>4}{len(info['posts_products']):>7}{n_bad:>11}{flag}")
    print("-" * 86)
    print(f"合计仍会因新契约失败的用例:{bad_total}\n")

    for name, info in rows:
        if info["at_risk"]:
            print(f"[{name}]")
            for t in sorted(info["at_risk"]):
                print(f"    {t}")
            print()
    return 1 if bad_total else 0


if __name__ == "__main__":
    sys.exit(main())
