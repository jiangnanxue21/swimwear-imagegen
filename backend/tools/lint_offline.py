#!/usr/bin/env python3
"""**ruff 装不上时的替身。它不是门禁，`make lint` 才是。**

先把这句话写死在最前面，因为这个脚本最大的风险不是漏报，是有人拿它跑绿之后
以为 lint 过了。它只复现 ruff 规则集里的**两条**：`F401`（import 了没用）和
`UP017`（`datetime.timezone.utc` 应写成 `datetime.UTC`）。
ruff 在这个仓库里开的是 `E, F, I, B, UP` 五组，剩下的这里一条都不管。

## 它为什么存在

A27 的交接末尾写着「lint 是逐条静态核对的，不是实跑的」。A29 接手时装上了
ruff，跑出 23 条，全部是 A27 那轮「时间收敛到 `core/clock.utc_now()`」的残留：
调用改掉了，`from datetime import UTC` 的 import 没删。

代价写在那句话里：人眼静态核对能发现「新加的 import 插错了位置」，
发现不了「旧 import 变成了死的」—— 后者要的不是读，是把每个绑定名在
整个文件里数一遍出现次数。那正是机器该干的活。

这个交付链上反复出现同一种环境：容器里没有网络，装不上 ruff。
与其下一轮又靠眼睛核对一遍，不如把机器能判定的那两条固化下来。

## 和 ruff 的差异（知道就好，别指望它对齐）

- 认 `# noqa` 与 `# noqa: F401`，但不认 `per-file-ignores`；
- `__init__.py` 整个跳过（那里的 import 通常就是再导出）；
- `from __future__ import annotations` 不报（ruff 也不报）；
- 名字「用没用」按 AST 里的 `Name` 节点数，字符串注解会被再 parse 一次。
  这比 ruff 宽松：ruff 还看 `__all__` 的动态构造之类，这里不看。

也就是说：**它报的基本都是真的，它不报不代表 ruff 也不报。**

## 用法

    python3 tools/lint_offline.py          # 全仓
    python3 tools/lint_offline.py app      # 只看某个目录

有发现时退出码 1。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"

_SKIPPED_DIRS: frozenset[str] = frozenset(
    {"migrations", "__pycache__", ".venv", "build", "dist", ".git"}
)

_NOQA_BARE = re.compile(r"#\s*noqa(?!\s*:)", re.I)
_NOQA_CODE = re.compile(r"#\s*noqa\s*:[^\n]*", re.I)


class _Bindings(ast.NodeVisitor):
    """收集每个 import 引入的绑定名，以及全文件用到的名字。"""

    def __init__(self) -> None:
        #: (报告行, 绑定名, 原文摘要, 语句起始行, 语句结束行)
        self.imports: list[tuple[int, str, str, int, int]] = []
        self.used: set[str] = set()

    def _span(self, node: ast.stmt) -> tuple[int, int]:
        return node.lineno, node.end_lineno or node.lineno

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.imports.append((node.lineno, bound, f"import {alias.name}", *self._span(node)))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name
            module = "." * node.level + (node.module or "")
            self.imports.append(
                (node.lineno, bound, f"from {module} import {alias.name}", *self._span(node))
            )

    def visit_Name(self, node: ast.Name) -> None:
        self.used.add(node.id)

    def visit_Constant(self, node: ast.Constant) -> None:
        # 字符串注解里的名字也算用到了。`from __future__ import annotations`
        # 之下的注解不是字符串常量(AST 里仍是表达式),那条路径由 visit_Name 覆盖
        if not isinstance(node.value, str):
            return
        try:
            inner = ast.parse(node.value, mode="eval")
        except SyntaxError:
            return
        for sub in ast.walk(inner):
            if isinstance(sub, ast.Name):
                self.used.add(sub.id)


def _suppressed(lines: list[str], start: int, end: int) -> bool:
    """整条 import 语句的任意一行上有 noqa,就算压住了。

    多行的 `from x import (\\n  a,\\n  b,\\n)` 里,noqa 通常写在第一行 ——
    只看诊断所在那一行会漏掉它,于是报一堆假阳性。
    """
    span = "\n".join(lines[start - 1 : end])
    if _NOQA_BARE.search(span):
        return True
    return any("F401" in m.group(0).upper() for m in _NOQA_CODE.finditer(span))


def _check_f401(path: Path, src: str, tree: ast.Module) -> list[str]:
    lines = src.splitlines()
    collector = _Bindings()
    collector.visit(tree)

    seen: set[tuple[int, str]] = set()
    out: list[str] = []
    for lineno, bound, text, start, end in collector.imports:
        if bound in collector.used:
            continue
        if bound == "annotations" and text.startswith("from __future__"):
            continue
        if (lineno, bound) in seen:
            continue
        seen.add((lineno, bound))
        if _suppressed(lines, start, end):
            continue
        out.append(f"{path}:{lineno}: F401 `{bound}` 导入了但没用到    [{text}]")
    return out


def _check_up017(path: Path, tree: ast.Module) -> list[str]:
    """`timezone.utc` / `datetime.timezone.utc` -> `UTC`。"""
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != "utc":
            continue
        base = node.value
        name = (
            base.id
            if isinstance(base, ast.Name)
            else base.attr
            if isinstance(base, ast.Attribute)
            else None
        )
        if name == "timezone":
            out.append(f"{path}:{node.lineno}: UP017 用 `datetime.UTC` 代替 `timezone.utc`")
    return out


def check(roots: list[Path]) -> tuple[list[str], int]:
    findings: list[str] = []
    scanned = 0
    for root in roots:
        files = sorted(root.rglob("*.py")) if root.is_dir() else [root]
        for path in files:
            if _SKIPPED_DIRS & set(path.parts):
                continue
            try:
                rel = path.relative_to(BACKEND)
            except ValueError:
                rel = path
            src = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(src)
            except SyntaxError as exc:
                findings.append(f"{rel}:{exc.lineno}: 语法错误 —— {exc.msg}")
                continue
            scanned += 1
            # __init__.py 里的 import 多半是再导出,ruff 对它另有默认
            if path.name != "__init__.py":
                findings.extend(_check_f401(rel, src, tree))
            findings.extend(_check_up017(rel, tree))
    return findings, scanned


def main(argv: list[str]) -> int:
    targets = [BACKEND / a for a in argv] or [BACKEND / "app", BACKEND / "tests"]
    findings, scanned = check(targets)

    if not findings:
        print(f"  {GREEN}OK{RESET}   {scanned} 个文件，F401 / UP017 无发现")
        print("       这只是 ruff 的两条规则。真正的门禁是 `make lint`。")
        return 0

    print(f"  {RED}FAIL{RESET} {len(findings)} 条\n")
    for line in sorted(findings):
        print(f"    {line}")
    print("\n  提醒：这只覆盖 F401 / UP017。跑得通 ruff 的机器上请以 `make lint` 为准。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
