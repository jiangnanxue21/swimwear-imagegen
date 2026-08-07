#!/usr/bin/env python3
"""复核 audit_column_writers 的写入判定：漏判(解构赋值)与误判(同名属性)。

跑法： python3 /home/claude/work/recheck_writes.py
"""
from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path("/home/claude/work/swimwear-imagegen-a45-batch14-24/backend")
sys.path.insert(0, str(BACKEND))

from tools.audit_column_writers import LEDGER, MODELS, APP, collect_models  # noqa: E402


def _function_index(tree: ast.Module) -> dict[int, str]:
    index: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            index.setdefault(id(child), node.name)
    return index


def _flatten_targets(node: ast.expr):
    """递归展开赋值目标 —— 原审计只认顶层 ast.Attribute，漏掉解构。"""
    if isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            yield from _flatten_targets(elt)
    elif isinstance(node, ast.Starred):
        yield from _flatten_targets(node.value)
    else:
        yield node


def _receiver(node: ast.Attribute) -> str:
    """`row.width` -> 'row'；`a.b.width` -> 'a.b'；下标/调用 -> '<expr>'"""
    try:
        return ast.unparse(node.value)
    except Exception:
        return "<expr>"


def scan(models):
    by_column: dict[str, list[str]] = defaultdict(list)
    for m in models.values():
        for col in m.columns:
            by_column[col].append(m.name)

    # key -> list of (kind, path, lineno, receiver)
    evidence: dict[str, list[tuple[str, str, int, str]]] = defaultdict(list)
    file_mentions: dict[str, set[str]] = {}
    missed_unpack: list[tuple[str, int, str, str]] = []

    for path in sorted(APP.rglob("*.py")):
        if MODELS in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = str(path.relative_to(BACKEND))
        file_mentions[rel] = {name for name in models if name in text}
        where = _function_index(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                model = models.get(func)
                if model is not None:
                    for kw in node.keywords:
                        if kw.arg in model.columns:
                            evidence[f"{model.name}.{kw.arg}"].append(
                                ("ctor", rel, node.lineno, func)
                            )
                        elif kw.arg in model.relationships and model.relationships[kw.arg]:
                            col = model.relationships[kw.arg]
                            evidence[f"{model.name}.{col}"].append(
                                ("rel", rel, node.lineno, func)
                            )
                if func == "values":
                    for kw in node.keywords:
                        for owner in by_column.get(kw.arg or "", []):
                            evidence[f"{owner}.{kw.arg}"].append(
                                ("values", rel, node.lineno, "<core>")
                            )
                if getattr(node.func, "id", "") == "setattr" and len(node.args) >= 2:
                    tgt = node.args[1]
                    if isinstance(tgt, ast.Constant) and isinstance(tgt.value, str):
                        for owner in by_column.get(tgt.value, []):
                            evidence[f"{owner}.{tgt.value}"].append(
                                ("setattr", rel, node.lineno, ast.unparse(node.args[0])[:30])
                            )

            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for top in targets:
                nested = isinstance(top, (ast.Tuple, ast.List, ast.Starred))
                for tgt in _flatten_targets(top):
                    if not isinstance(tgt, ast.Attribute):
                        continue
                    owners = by_column.get(tgt.attr, [])
                    for owner in owners:
                        evidence[f"{owner}.{tgt.attr}"].append(
                            ("attr-unpack" if nested else "attr", rel, tgt.lineno, _receiver(tgt))
                        )
                    if nested and owners:
                        missed_unpack.append((rel, tgt.lineno, tgt.attr, _receiver(tgt)))

    return evidence, file_mentions, missed_unpack, by_column


def main() -> int:
    models = collect_models()
    evidence, file_mentions, missed_unpack, by_column = scan(models)

    print("=" * 78)
    print("A. 解构赋值盲区：原审计只认顶层 ast.Attribute，`a.x, a.y = ...` 整条漏掉")
    print("=" * 78)
    seen = set()
    for rel, lineno, attr, recv in missed_unpack:
        if (rel, lineno, attr) in seen:
            continue
        seen.add((rel, lineno, attr))
        owners = by_column[attr]
        print(f"  {rel}:{lineno}  {recv}.{attr}   命中列: {', '.join(owners)}")

    print()
    print("=" * 78)
    print("B. 只靠解构赋值成立的写入点 —— 原审计判它们「无人写」")
    print("=" * 78)
    only_unpack = []
    for key, sites in sorted(evidence.items()):
        kinds = {k for k, *_ in sites}
        if kinds == {"attr-unpack"}:
            only_unpack.append((key, sites))
    for key, sites in only_unpack:
        flag = "  ← 在 LEDGER 上（假阳性）" if key in LEDGER else ""
        print(f"  {key}{flag}")
        for kind, rel, lineno, recv in sites:
            print(f"        {rel}:{lineno}  {recv}.{key.split('.')[-1]}")
    if not only_unpack:
        print("  （无）")

    print()
    print("=" * 78)
    print("C. 台账条目 vs 解构赋值：LEDGER 里哪些条目其实有写入点")
    print("=" * 78)
    for key in sorted(LEDGER):
        sites = evidence.get(key, [])
        if not sites:
            continue
        kinds = sorted({k for k, *_ in sites})
        print(f"  {key}   证据类型={kinds}")
        for kind, rel, lineno, recv in sites[:4]:
            print(f"        [{kind}] {rel}:{lineno}  recv={recv}")

    print()
    print("=" * 78)
    print("D. 疑似同名属性误判：某列的全部写入证据都是 attr 赋值，")
    print("   且所在文件从头到尾没有提到那个模型名")
    print("=" * 78)
    suspects = []
    for key, sites in sorted(evidence.items()):
        model_name, col = key.rsplit(".", 1)
        kinds = {k for k, *_ in sites}
        if not kinds <= {"attr", "attr-unpack", "values", "setattr"}:
            continue  # 有构造点/关系写入，属实
        # 所有证据所在文件都不提这个模型
        if all(model_name not in file_mentions.get(rel, set()) for _, rel, _, _ in sites):
            suspects.append((key, sites))
    for key, sites in suspects:
        flag = "  ← 已在 LEDGER" if key in LEDGER else ""
        print(f"  {key}{flag}")
        for kind, rel, lineno, recv in sites[:3]:
            print(f"        [{kind}] {rel}:{lineno}  recv={recv}")
    print(f"\n  合计 {len(suspects)} 列")
    return 0


if __name__ == "__main__":
    sys.exit(main())
