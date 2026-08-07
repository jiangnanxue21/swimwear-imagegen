#!/usr/bin/env python3
"""第三轮：只报「能证明接收者不是这个模型」的硬误判。

做法：把全仓所有 class（不只是模型）都收进符号表。接收者解析到一个
**已知的非模型类** 时，那次属性赋值就不该算作对该模型列的写入。
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


def all_classes() -> set[str]:
    names = set()
    for p in APP.rglob("*.py"):
        try:
            t = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for n in ast.walk(t):
            if isinstance(n, ast.ClassDef):
                names.add(n.name)
    return names


class Scope:
    """name -> 类名（模型或非模型都记）"""

    def __init__(self, known: set[str]):
        self.known = known
        self.b: dict[str, str] = {}

    def _cls_of(self, v):
        if isinstance(v, ast.Call):
            fn = getattr(v.func, "id", "") or getattr(v.func, "attr", "")
            if fn in self.known:
                return fn
        return None

    def load(self, fn: ast.AST, enclosing_class: str | None = None):
        if enclosing_class:
            self.b["self"] = enclosing_class
        a = getattr(fn, "args", None)
        if a is not None:
            for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
                if arg.annotation is None:
                    continue
                for sub in ast.walk(arg.annotation):
                    nm = getattr(sub, "id", None) or getattr(sub, "attr", None)
                    if nm in self.known:
                        self.b[arg.arg] = nm
                        break
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                c = self._cls_of(n.value)
                if c:
                    for t in n.targets:
                        for x in flatten(t):
                            if isinstance(x, ast.Name):
                                self.b[x.id] = c
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                ann = n.annotation
                nm = getattr(ann, "id", None) or getattr(ann, "attr", None)
                if nm in self.known:
                    self.b[n.target.id] = nm
                elif n.value is not None:
                    c = self._cls_of(n.value)
                    if c:
                        self.b[n.target.id] = c


def main():
    models = collect_models()
    known = all_classes()
    model_names = set(models)
    by_col = defaultdict(list)
    for m in models.values():
        for c in m.columns:
            by_col[c].append(m.name)

    hard_false = []   # 接收者解出来是非模型类 / 别的模型
    for path in sorted(APP.rglob("*.py")):
        if MODELS in path.parents:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = str(path.relative_to(BACKEND))

        scopes = {}
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for fn in [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                s = Scope(known)
                s.load(fn, enclosing_class=cls.name)
                for ch in ast.walk(fn):
                    scopes.setdefault(id(ch), s)
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            s = Scope(known)
            s.load(fn)
            for ch in ast.walk(fn):
                scopes.setdefault(id(ch), s)

        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for top in targets:
                for t in flatten(top):
                    if not isinstance(t, ast.Attribute):
                        continue
                    owners = by_col.get(t.attr, [])
                    if not owners:
                        continue
                    sc = scopes.get(id(node))
                    if sc is None or not isinstance(t.value, ast.Name):
                        continue
                    actual = sc.b.get(t.value.id)
                    if actual is None:
                        continue
                    for o in owners:
                        if o == actual:
                            continue
                        # 接收者已解出，且不是这个模型 —— 这次赋值不该算 o 的写入
                        hard_false.append((f"{o}.{t.attr}", rel, t.lineno,
                                           t.value.id, actual,
                                           actual in model_names))

    # 哪些列 **只** 靠这类硬误判成立？用原审计的口径反推
    from tools.audit_column_writers import collect_writes
    written = collect_writes(collect_models())

    per_key = defaultdict(list)
    for key, rel, ln, recv, actual, is_model in hard_false:
        per_key[key].append((rel, ln, recv, actual, is_model))

    print("=" * 78)
    print("硬误判：接收者已解析出真实类型，且不是这个模型")
    print("=" * 78)
    print(f"  共 {len(hard_false)} 处赋值被错记，涉及 {len(per_key)} 个「模型.列」\n")

    # 只列那些「在原审计里被判为已写」且我们能证明证据有问题的
    interesting = []
    for key in sorted(per_key):
        if key not in written:
            continue
        mname, col = key.rsplit(".", 1)
        m = models.get(mname)
        if m is None or m.unknowable or col in BASE_MANAGED:
            continue
        interesting.append(key)

    for key in interesting:
        sites = per_key[key]
        kinds = {("模型" if im else "非模型类") for *_, im in sites}
        print(f"  {key}")
        for rel, ln, recv, actual, is_model in sites[:3]:
            tag = "模型" if is_model else "非模型"
            print(f"      {rel}:{ln}   {recv}.{key.split('.')[-1]}   → {recv} 实为 {actual} ({tag})")
    print(f"\n  其中在原审计里被判「有人写」的：{len(interesting)} 个")


if __name__ == "__main__":
    main()
