#!/usr/bin/env python3
"""终轮：每一列的写入证据分四类，算出「写入判定只靠同名撞出来」的集合。

  strong    构造点 kwarg / 关系赋值 / .values() / 字面量 setattr
  correct   属性赋值，接收者解析为**这个**模型
  wrong     属性赋值，接收者解析为**别的**类
  unknown   属性赋值，接收者解不出来（沿用原审计宽口径）
"""
from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path("/home/claude/work/swimwear-imagegen-a45-batch14-24/backend")
sys.path.insert(0, str(BACKEND))
from tools.audit_column_writers import LEDGER, MODELS, APP, BASE_MANAGED, collect_models  # noqa: E402

sys.path.insert(0, "/home/claude/work")
from recheck3 import flatten, all_classes, Scope  # noqa: E402


def main():
    models = collect_models()
    known = all_classes()
    by_col = defaultdict(list)
    for m in models.values():
        for c in m.columns:
            by_col[c].append(m.name)

    ev = defaultdict(lambda: defaultdict(list))  # key -> cls -> [(rel, ln, recv)]

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
            if isinstance(node, ast.Call):
                fn = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                m = models.get(fn)
                if m:
                    for kw in node.keywords:
                        if kw.arg in m.columns:
                            ev[f"{fn}.{kw.arg}"]["strong"].append((rel, node.lineno, "ctor"))
                        elif kw.arg in m.relationships and m.relationships[kw.arg]:
                            ev[f"{fn}.{m.relationships[kw.arg]}"]["strong"].append(
                                (rel, node.lineno, "rel"))
                if fn == "values":
                    for kw in node.keywords:
                        for o in by_col.get(kw.arg or "", []):
                            ev[f"{o}.{kw.arg}"]["strong"].append((rel, node.lineno, "values"))
                if getattr(node.func, "id", "") == "setattr" and len(node.args) >= 2:
                    t = node.args[1]
                    if isinstance(t, ast.Constant) and isinstance(t.value, str):
                        for o in by_col.get(t.value, []):
                            ev[f"{o}.{t.value}"]["strong"].append((rel, node.lineno, "setattr"))

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
                    actual = None
                    if sc is not None and isinstance(t.value, ast.Name):
                        actual = sc.b.get(t.value.id)
                    try:
                        rtxt = ast.unparse(t.value)
                    except Exception:
                        rtxt = "<expr>"
                    for o in owners:
                        if actual is None:
                            ev[f"{o}.{t.attr}"]["unknown"].append((rel, t.lineno, rtxt))
                        elif actual == o:
                            ev[f"{o}.{t.attr}"]["correct"].append((rel, t.lineno, rtxt))
                        else:
                            ev[f"{o}.{t.attr}"]["wrong"].append((rel, t.lineno, f"{rtxt}:{actual}"))

    # 分类
    only_wrong, only_unknown, ghost, ledger_fp = [], [], [], []
    for key in sorted(LEDGER):
        mn, col = key.rsplit(".", 1)
        m = models.get(mn)
        if m is None or col not in m.columns:
            ghost.append(key)
        elif ev[key]["strong"] or ev[key]["correct"]:
            ledger_fp.append(key)

    for m in sorted(models.values(), key=lambda x: x.name):
        if m.unknowable:
            continue
        for col in sorted(m.columns):
            if col in BASE_MANAGED:
                continue
            key = f"{m.name}.{col}"
            if key in LEDGER:
                continue
            e = ev[key]
            if e["strong"] or e["correct"]:
                continue
            if not (e["wrong"] or e["unknown"]):
                continue  # 原审计也会报，不是新发现
            rec = (key, col in m.defaulted, e)
            if e["wrong"] and not e["unknown"]:
                only_wrong.append(rec)
            else:
                only_unknown.append(rec)

    print("=" * 78)
    print("【一】台账幽灵条目：指向的列已不存在")
    print("=" * 78)
    for k in ghost:
        print(f"  {k}")
    print(f"  → {len(ghost)} 条\n")

    print("=" * 78)
    print("【二】台账假阳性：条目说没人写，实际有强/确证写入")
    print("=" * 78)
    for k in ledger_fp:
        e = ev[k]
        for rel, ln, tag in (e["strong"] + e["correct"])[:3]:
            print(f"  {k}\n      {rel}:{ln}  ({tag})")
    print(f"  → {len(ledger_fp)} 条\n")

    print("=" * 78)
    print("【三】硬漏报：写入判定 100% 建立在「接收者已证明不是这个模型」上")
    print("=" * 78)
    for key, dflt, e in only_wrong:
        tag = "恒等默认值" if dflt else "恒为 NULL"
        print(f"  {key}  ({tag})   误判来源 {len(e['wrong'])} 处")
        for rel, ln, r in e["wrong"][:2]:
            print(f"      {rel}:{ln}   {r}")
    print(f"  → {len(only_wrong)} 列\n")

    print("=" * 78)
    print("【四】软漏报：无任何确证写入，只有解不出接收者的同名赋值")
    print("=" * 78)
    for key, dflt, e in only_unknown:
        tag = "恒等默认值" if dflt else "恒为 NULL"
        print(f"  {key}  ({tag})   未解析 {len(e['unknown'])} 处 / 已证伪 {len(e['wrong'])} 处")
    print(f"  → {len(only_unknown)} 列\n")

    Path("/home/claude/work/findings.json").write_text(json.dumps({
        "ghost": ghost,
        "ledger_false_positive": ledger_fp,
        "hard_missed": [k for k, _, _ in only_wrong],
        "soft_missed": [k for k, _, _ in only_unknown],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
