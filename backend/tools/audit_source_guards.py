#!/usr/bin/env python3
"""守卫的窗口体检:**反向断言不许喂一段来路不明的源码。**

## 它钉的是哪一件事

`tests/pure/` 里有一大批守卫读源码然后断言。其中**反向**的那些
(`assert X not in 某段`)有一个共同的失效方式,而它不表现为红:

    窗口是按字符串定位切出来的,而定位串在文件里不止一处
    窗口是定长的(`[:200]`、`[i : i + 400]`)

被切窄之后,**正向**断言通常还在残段里(所以看起来一切正常),而**反向**
断言被残段喂成了**平凡真** —— 那条守卫在某次无关改动的那一刻悄悄不设防,
并且照样打印 PASS。

A45-batch14-13 一次撞见两条,都是自己人写的:

    entry.index("),")   登记项的注释里有一句 `(归属外键不存在),`,提前命中
    wired[...][:200]    定长窗口。登记项一变长,窗口就切在半路上

第二条尤其值得记:它的反向断言是「`idempotency_key` 不许被登记进
WIRED_MODULES」,而那一批往登记项里加了一行之后,这条断言**再也拦不住
任何东西**,测试却照样绿。

## 判据

只管**反向**断言,不管正向。理由是失效方向不同:

    正向断言碰上窄窗口   断言变假 -> 红 -> 有人会看见
    反向断言碰上窄窗口   断言变真 -> 绿 -> 没有人会看见

所以这份审计不是「字符串切一律禁止」,而是「**喂给反向断言的窗口必须是
封闭的**」。封闭的办法只有一个:`tests.pure._helpers.window()`,它保证
定位串恰好出现一次、终点在起点之后恰好一次、起点之后确有内容 ——
「恰好一次」这条口径和 `tools/audit_anchors.py` 对变异锚点的要求同源:
**一个不唯一的锚点,和一个没有锚点是一回事。**

## 为什么是独立脚本而不是一条纯用例

和 `audit_anchors.py` 同一个理由:它扫的是 `tests/pure/` **自己**。
写成那个目录里的一条用例,就成了「用被审对象审自己」——
而这份审计要能在那批用例本身有问题时照样给出答案。

零三方依赖,只读文件、只解析不执行。退出码非零 = 有窗口需要收口。

    python3 tools/audit_source_guards.py     # 或 make audit-guards
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PURE = BACKEND / "tests" / "pure"

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

#: 这些调用会把「一整个源文件」读进来。窗口的来源都是它们。
READERS = {
    "read_text",
    "_src",
    "_ts",
    "_source",
    "_read",
    "_code_only",
    "_func_source",
    "_body_without_docstring",
}


def _is_window_expr(node: ast.AST) -> str | None:
    """这个表达式是不是「按字符串定位/定长切出来的一段」。返回坏在哪。"""
    if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)):
        return None
    dumped = ast.dump(node.slice)
    if ".index" in dumped or ".find" in dumped or "'index'" in dumped:
        return "按字符串定位切窗口"
    for bound in (node.slice.lower, node.slice.upper):
        if isinstance(bound, ast.Constant) and isinstance(bound.value, int):
            return "定长窗口"
        # `text[i : i + 400]` —— 定长的另一种写法
        if isinstance(bound, ast.BinOp) and isinstance(bound.right, ast.Constant):
            if isinstance(bound.right.value, int):
                return "定长窗口"
    return None


def _reads_source(fn: ast.FunctionDef) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in READERS:
                return True
    return False


def _window_vars(fn: ast.FunctionDef) -> dict[str, str]:
    """函数里哪些局部变量装着一段来路不明的窗口。"""
    found: dict[str, str] = {}
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        why = _is_window_expr(node.value)
        if why:
            found[target.id] = why
        elif isinstance(node.value, ast.Name) and node.value.id in found:
            # `block = block[...]` 之后再 `x = block` —— 传染
            found[target.id] = found[node.value.id]
    return found


def _negative_uses(fn: ast.FunctionDef, windows: dict[str, str]) -> list[tuple[int, str, str]]:
    """哪几处反向断言吃了那些窗口。"""
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ast.NotIn) for op in node.ops):
            continue
        for cmp in node.comparators:
            if isinstance(cmp, ast.Name) and cmp.id in windows:
                out.append((node.lineno, cmp.id, windows[cmp.id]))
    return out


def violations_in(source: str, filename: str = "<test>") -> list[str]:
    """一份源码里,有几处反向断言吃了来路不明的窗口。

    单独成一个函数,是为了让**这份审计自己**能被穷举验证:CLI 那一层
    只负责遍历文件,判定在这里,而判定拿几段合成代码就能钉死。
    脚本自己的判定藏在 `main()` 里的话,验它就要在真实文件树上造违规 ——
    而那正是 `mutate_contract_tests.py` 当年报「18 条全被抓住」却
    一条都没验到的形状(batch14-3 退役了它)。
    """
    out: list[str] = []
    for fn in [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)]:
        if not _reads_source(fn):
            continue
        windows = _window_vars(fn)
        if not windows:
            continue
        for lineno, name, why in _negative_uses(fn, windows):
            out.append(
                f"{filename}:{lineno} {fn.name}() 的 `{name}` 是{why},"
                f"却被一条 `not in` 断言当成了完整的一段"
            )
    return out


def scanned_guards(source: str) -> int:
    """这份源码里有几个「读源码的守卫」。给非空性守卫用 —— 判定塌成
    「什么都不扫」时,`violations_in` 会永远返回空清单而看起来一切正常。"""
    return sum(
        1
        for fn in ast.walk(ast.parse(source))
        if isinstance(fn, ast.FunctionDef) and _reads_source(fn)
    )


def main() -> int:
    problems: list[str] = []
    scanned = 0
    for path in sorted(PURE.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        scanned += scanned_guards(source)
        problems.extend(violations_in(source, path.name))

    if problems:
        print(f"{RED}反向断言吃了来路不明的窗口:{len(problems)} 处{RESET}")
        for line in problems:
            print(f"  {line}")
        print()
        print(
            "改法只有一个:窗口走 `tests.pure._helpers.window()`。它保证定位串\n"
            "恰好出现一次、终点在起点之后恰好一次、起点之后确有内容。\n"
            "被切窄的反向断言不会变红,只会变成一句永远为真的话。"
        )
        return 1

    print(f"{GREEN}{scanned} 个读源码的守卫,反向断言都吃着封闭的窗口{RESET}")
    print(f"{DIM}    这只管反向断言 —— 正向断言切窄了会变红,有人会看见{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
