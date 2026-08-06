#!/usr/bin/env python3
"""每一条 `from app.X import Y` 指向的东西，在仓库里真的存在吗？

## 它为什么存在

A29 在 `tests/test_poll_and_delist_db.py` 里发现了这一行：

    from app.models.listing_export import ListingExport   # 全仓没有这个模块

它躺在 `test_a_rejection_recorded_by_polling_needs_no_excel_export` 的函数体里，
而数据库用例在没有 PostgreSQL 的机器上默认 skip —— 于是这条 import
**从写下来那天起没有被求值过一次**，本机永远绿，CI 上是 error。
那条用例的 docstring 自己写着「整个任务 15 里最容易被漏掉的一条」。

这类缺陷只有两种发现方式：真跑一次，或者静态把 import 解析一遍。
真跑一次依赖一整套三方包和一个数据库；解析一遍只要 `python3`。
两者不互相替代 —— 但当前者跑不了的时候（离线容器、没装依赖的评审机），
后者是唯一还能说话的那个，而它恰好能判定这一类。

## 它检查什么、不检查什么

**检查**：`app.*` 的模块存不存在；`from app.X import Y` 里的 `Y` 在 `X` 的
顶层名字表里找不找得到（或者本身是个子模块）。

**不检查**：三方包、相对 import、运行期动态 import、以及「名字在但类型不对」。
不做真的 import（那需要装依赖，也就失去了这个脚本存在的理由）。

延迟 import（函数体内那种）**照查**。它们正是最容易漏的一类：模块顶层的
import 一 collect 就会炸，函数体内的要等那一行真的被执行。

## 用法

    python3 tools/verify_imports.py       # 或 make verify-imports

零三方依赖，只读文件，秒级。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"

#: 不扫的目录。`migrations/` 里的脚本按版本冻结，改它们要动迁移链，
#: 不该被这条门禁牵着走
_SKIPPED_DIRS: frozenset[str] = frozenset(
    {"migrations", "__pycache__", ".venv", "build", "dist", ".git"}
)


def _module_file(dotted: str) -> Path | None:
    """点号模块名 -> 磁盘上的文件。包和模块都认。"""
    rel = Path(*dotted.split("."))
    for candidate in (BACKEND / rel.with_suffix(".py"), BACKEND / rel / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


#: 模块 -> 顶层名字表。`None` 表示模块不存在。缓存是必要的：
#: `app.core.enums` 这种会被上百个文件引用，每次重新 parse 一遍太蠢
_NAMES: dict[str, frozenset[str] | None] = {}

#: 出现在名字表里表示「这个模块有 `from x import *`」，于是它的名字表不可知，
#: 只能全部放行。用一个不可能是合法标识符的串，避免和真名字撞上
_HAS_STAR = "*"


def _collect(node: ast.AST, into: set[str]) -> None:
    """把一个语句里定义出来的顶层名字收进 `into`。"""
    if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        into.add(node.name)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                into.add(target.id)
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        into.add(node.target.id)
    elif isinstance(node, ast.Import):
        for alias in node.names:
            into.add(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            into.add(_HAS_STAR if alias.name == "*" else (alias.asname or alias.name))


def _toplevel_names(dotted: str) -> frozenset[str] | None:
    if dotted in _NAMES:
        return _NAMES[dotted]

    path = _module_file(dotted)
    if path is None:
        _NAMES[dotted] = None
        return None

    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    for stmt in tree.body:
        _collect(stmt, names)
        # `if TYPE_CHECKING:` 和 `try/except ImportError:` 里的定义同样是顶层名字。
        # 漏掉它们会让这个脚本报一堆假阳性,而一个会误报的门禁最后一定会被关掉
        if isinstance(stmt, ast.If | ast.Try):
            for sub in ast.walk(stmt):
                _collect(sub, names)

    frozen = frozenset(names)
    _NAMES[dotted] = frozen
    return frozen


def _sources() -> list[Path]:
    return sorted(
        p
        for p in BACKEND.rglob("*.py")
        if not (_SKIPPED_DIRS & set(p.relative_to(BACKEND).parts))
    )


def check() -> list[str]:
    """返回问题列表，空列表表示全部解析得通。"""
    problems: list[str] = []

    for path in _sources():
        where = path.relative_to(BACKEND)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            problems.append(f"{where}:{exc.lineno}: 语法错误 —— {exc.msg}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if (name == "app" or name.startswith("app.")) and not _module_file(name):
                        problems.append(f"{where}:{node.lineno}: 模块不存在 —— {name}")
                continue

            if not isinstance(node, ast.ImportFrom):
                continue

            module = node.module or ""
            # 相对 import 不查:解析它要重建包上下文,而全仓的 app 内部引用
            # 一律写绝对路径(.importlinter 的契约也是按绝对路径写的)
            if node.level or not (module == "app" or module.startswith("app.")):
                continue

            names = _toplevel_names(module)
            if names is None:
                problems.append(f"{where}:{node.lineno}: 模块不存在 —— {module}")
                continue
            if _HAS_STAR in names:
                continue

            for alias in node.names:
                if alias.name == "*" or alias.name in names:
                    continue
                # `from app.pkg import submodule` —— 名字不在 __init__ 里,
                # 但它是个真实存在的子模块
                if _module_file(f"{module}.{alias.name}") is not None:
                    continue
                problems.append(
                    f"{where}:{node.lineno}: {module} 里没有 `{alias.name}`"
                )

    return problems


def main() -> int:
    problems = check()
    scanned = len(_sources())

    if not problems:
        print(f"  {GREEN}OK{RESET}   {scanned} 个文件，app.* 的 import 全部解析得通")
        return 0

    print(f"  {RED}FAIL{RESET} {len(problems)} 条 import 指向了不存在的东西\n")
    for line in problems:
        print(f"    {line}")
    print(
        "\n这类 import 如果躺在延迟导入或默认 skip 的用例里，本机会一直绿，"
        "\n而 CI 或生产环境上它是 ModuleNotFoundError / ImportError。"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
