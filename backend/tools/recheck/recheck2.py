#!/usr/bin/env python3
"""第二轮：给属性赋值解出接收者类型，把「按名字撞出来的写」和真写分开。

三个判定层，从强到弱：
  resolved   接收者在同一函数里被绑定到 `Model(...)` / `session.get(Model,…)`
  ctor/rel   构造点关键字、关系赋值、.values()、常量 setattr
  unresolved 接收者解不出来 —— 沿用原审计「所有同名列都算写」的宽口径
"""
from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path("/home/claude/work/swimwear-imagegen-a45-batch14-24/backend")
sys.path.insert(0, str(BACKEND))
from tools.audit_column_writers import LEDGER, MODELS, APP, BASE_MANAGED, collect_models  # noqa: E402


def flatten(node):
    if isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            yield from flatten(e)
    elif isinstance(node, ast.Starred):
        yield from flatten(node.value)
    else:
        yield node


class FuncScope(ast.NodeVisitor):
    """在一个函数体内，把局部名字绑到模型类名上。"""

    def __init__(self, models):
        self.models = models
        self.binding: dict[str, str] = {}

    def _model_of_call(self, node: ast.expr) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        fn = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
        if fn in self.models:
            return fn
        # session.get(Model, pk) / session.scalar(select(Model)…) 之类
        if fn in {"get", "scalar", "scalars", "first", "one", "one_or_none"}:
            for arg in ast.walk(node):
                if isinstance(arg, ast.Name) and arg.id in self.models:
                    return arg.id
        return None

    def visit_Assign(self, node):
        m = self._model_of_call(node.value)
        if m:
            for t in node.targets:
                for x in flatten(t):
                    if isinstance(x, ast.Name):
                        self.binding[x.id] = m
        self.generic_visit(node)

    def visit_For(self, node):
        # for row in rows / for row in session.scalars(select(Model)):
        m = self._model_of_call(node.iter)
        if m is None:
            for sub in ast.walk(node.iter):
                if isinstance(sub, ast.Name) and sub.id in self.models:
                    m = sub.id
                    break
        if m:
            for x in flatten(node.target):
                if isinstance(x, ast.Name):
                    self.binding[x.id] = m
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_AnnAssign(self, node):
        if node.value is not None:
            m = self._model_of_call(node.value)
            if m and isinstance(node.target, ast.Name):
                self.binding[node.target.id] = m
        # 显式注解 row: GenerationCandidate = ...
        ann = node.annotation
        name = getattr(ann, "id", None) or getattr(ann, "attr", None)
        if name in self.models and isinstance(node.target, ast.Name):
            self.binding[node.target.id] = name
        self.generic_visit(node)


def scan(models):
    by_col = defaultdict(list)
    for m in models.values():
        for c in m.columns:
            by_col[c].append(m.name)

    ev = defaultdict(list)          # key -> [(strength, kind, rel, lineno, recv)]
    unresolved_sites = []

    for path in sorted(APP.rglob("*.py")):
        if MODELS in path.parents:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = str(path.relative_to(BACKEND))

        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        scopes = {}
        for f in funcs:
            s = FuncScope(models)
            # 参数注解： def _finalize(job: BatchJob, ...)
            a = f.args
            for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
                ann = arg.annotation
                if ann is None:
                    continue
                for sub in ast.walk(ann):
                    nm = getattr(sub, "id", None) or getattr(sub, "attr", None)
                    if nm in models:
                        s.binding[arg.arg] = nm
                        break
            for st in f.body:
                s.visit(st)
            for child in ast.walk(f):
                scopes.setdefault(id(child), s)
        module_scope = FuncScope(models)
        for st in tree.body:
            module_scope.visit(st)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                m = models.get(fn)
                if m:
                    for kw in node.keywords:
                        if kw.arg in m.columns:
                            ev[f"{fn}.{kw.arg}"].append(("strong", "ctor", rel, node.lineno, fn))
                        elif kw.arg in m.relationships and m.relationships[kw.arg]:
                            ev[f"{fn}.{m.relationships[kw.arg]}"].append(
                                ("strong", "rel", rel, node.lineno, fn))
                if fn == "values":
                    for kw in node.keywords:
                        for o in by_col.get(kw.arg or "", []):
                            ev[f"{o}.{kw.arg}"].append(("strong", "values", rel, node.lineno, "<core>"))
                if getattr(node.func, "id", "") == "setattr" and len(node.args) >= 2:
                    t = node.args[1]
                    if isinstance(t, ast.Constant) and isinstance(t.value, str):
                        for o in by_col.get(t.value, []):
                            ev[f"{o}.{t.value}"].append(("strong", "setattr", rel, node.lineno, "-"))

            targets = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for top in targets:
                nested = isinstance(top, (ast.Tuple, ast.List, ast.Starred))
                for t in flatten(top):
                    if not isinstance(t, ast.Attribute):
                        continue
                    owners = by_col.get(t.attr, [])
                    if not owners:
                        continue
                    scope = scopes.get(id(node), module_scope)
                    recv = t.value
                    kind = "attr-unpack" if nested else "attr"
                    if isinstance(recv, ast.Name) and recv.id in scope.binding:
                        owner = scope.binding[recv.id]
                        if t.attr in models[owner].columns:
                            ev[f"{owner}.{t.attr}"].append(
                                ("resolved", kind, rel, t.lineno, recv.id))
                        continue
                    try:
                        rtxt = ast.unparse(recv)
                    except Exception:
                        rtxt = "<expr>"
                    unresolved_sites.append((rel, t.lineno, rtxt, t.attr))
                    for o in owners:
                        ev[f"{o}.{t.attr}"].append(("weak", kind, rel, t.lineno, rtxt))
    return ev, unresolved_sites, by_col


def main():
    models = collect_models()
    ev, unresolved, by_col = scan(models)

    strong = {k for k, sites in ev.items() if any(s[0] in ("strong", "resolved") for s in sites)}
    weak_only = {k for k, sites in ev.items() if k not in strong}

    print("=" * 78)
    print("一、台账条目校验：条目指向的列还在不在？")
    print("=" * 78)
    ghosts = []
    for key in sorted(LEDGER):
        mname, col = key.rsplit(".", 1)
        m = models.get(mname)
        if m is None:
            ghosts.append((key, "模型不存在"))
        elif col not in m.columns:
            ghosts.append((key, f"{mname} 没有 {col} 这一列"))
    for key, why in ghosts:
        print(f"  ✗ {key}   —— {why}")
    print(f"  台账 {len(LEDGER)} 条，其中 {len(ghosts)} 条指向不存在的列")

    print()
    print("=" * 78)
    print("二、台账假阳性：条目说「没人写」，实际有强证据写入")
    print("=" * 78)
    fp = [k for k in LEDGER if k in strong]
    for k in sorted(fp):
        for s in ev[k]:
            if s[0] in ("strong", "resolved"):
                print(f"  ✗ {k}   [{s[0]}/{s[1]}] {s[2]}:{s[3]}  recv={s[4]}")
    if not fp:
        print("  （无）")

    print()
    print("=" * 78)
    print("三、漏报的孤儿列：原审计判「有人写」，但全部证据都是弱证据")
    print("   （弱 = 同名属性，接收者解不出来或已解出不是这个模型）")
    print("=" * 78)
    rows = []
    for m in sorted(models.values(), key=lambda x: x.name):
        if m.unknowable:
            continue
        for col in sorted(m.columns):
            if col in BASE_MANAGED:
                continue
            key = f"{m.name}.{col}"
            if key in LEDGER or key in strong:
                continue
            if key in weak_only:
                rows.append((key, ev[key], col in m.defaulted))
    for key, sites, has_default in rows:
        tag = "恒等默认值" if has_default else "恒为 NULL"
        print(f"  ✗ {key}  ({tag})")
        for s in sites[:3]:
            print(f"        [{s[1]}] {s[2]}:{s[3]}  recv={s[4]}")
    print(f"\n  合计 {len(rows)} 列")

    print()
    print("=" * 78)
    print("四、解不出接收者的属性赋值站点（宽口径仍然生效的地方）")
    print("=" * 78)
    agg = defaultdict(int)
    for rel, ln, rtxt, attr in unresolved:
        agg[attr] += 1
    print(f"  共 {len(unresolved)} 处，覆盖 {len(agg)} 个列名")
    for attr, n in sorted(agg.items(), key=lambda x: -x[1])[:12]:
        print(f"     {attr:28s} {n:3d} 处   撞到模型: {', '.join(by_col[attr])[:70]}")


if __name__ == "__main__":
    main()
