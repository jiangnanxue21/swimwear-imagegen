#!/usr/bin/env python3
"""A45-batch14-14 变异验证:守卫的窗口必须是封闭的。

用法:python3 tools/mutate_batch14_14.py

## 这一批变异在防什么

本批做的是一条**门禁**,而门禁的失效方式只有一种形状:**它照样退 0**。
所以 A 组逐条打掉审计的判定(认不出定长窗口、认不出定位切片、
把「读源码」的判据打空),每一条改完之后 `make audit-guards` 都是绿的 ——
而全树那 10 处开着的窗口一处都没被发现。

H 组打三个取窗口的工具。它们的失效同样不响:少一条唯一性检查,
窗口还是切得出来,只是切的是哪一段全看运气 —— 而反向断言会静默变真。

G 组打**注册**。硬规则 3 说加一条门禁要改三个地方,少一处的表现各不相同
(本地跑不到 / CI 跑不到 / CI 删掉了没人发现),但都通向同一个结局:
门禁只写在文档里。

## 为什么变异先写

反过来做就是 batch13-3 / M11 的来路:守卫写在实现之后,它守的是
「我刚才写了什么」,不是「哪里会坏」。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent


AUD = "tools/audit_source_guards.py"
HLP = "tests/pure/_helpers.py"
MK = "../Makefile"
CI = "../.github/workflows/ci.yml"
VD = "tools/verify_delivery.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------- A 组:审计判定
    (
        "A1",
        "认不出定长窗口(`[:200]` 那一类重新畅通 —— 14-12 那条假绿会原样回来)",
        AUD,
        "        if isinstance(bound, ast.Constant) and isinstance(bound.value, int):\n"
        '            return "定长窗口"',
        "        if False:\n"
        '            return "定长窗口"',
    ),
    (
        "A2",
        "认不出 `text[i : i + 400]` 这种定长写法(batch8 那条原样回来)",
        AUD,
        "        if isinstance(bound, ast.BinOp) and isinstance(bound.right, ast.Constant):\n"
        "            if isinstance(bound.right.value, int):\n"
        '                return "定长窗口"',
        "        if False:\n"
        "            if isinstance(bound.right, ast.Constant):\n"
        '                return "定长窗口"',
    ),
    (
        "A3",
        "认不出按字符串定位的切片(14-13 撞见的那两条都不再被发现)",
        AUD,
        '    if ".index" in dumped or ".find" in dumped or "\'index\'" in dumped:\n'
        '        return "按字符串定位切窗口"',
        "    if False:\n"
        '        return "按字符串定位切窗口"',
    ),
    (
        "A4",
        "把「读源码」的判据打空(审计一个函数都不看,永远报 0 处)",
        AUD,
        "            if name in READERS:\n                return True",
        "            if name in ():\n                return True",
    ),
    (
        "A5",
        "连正向断言一起判红(门禁开始误伤 87 处正当用法,下一步就是加白名单)",
        AUD,
        "        if not any(isinstance(op, ast.NotIn) for op in node.ops):\n            continue",
        "        if not node.ops:\n            continue",
    ),
    (
        "A6",
        "窗口变量的传染不再跟踪(`b = a[...]` 之后 `c = b` 就脱身了)",
        AUD,
        "        elif isinstance(node.value, ast.Name) and node.value.id in found:",
        "        elif False:",
    ),
    # ------------------------------------------- H 组:三个取窗口的工具
    (
        "H1",
        "window 不查起点唯一性(注释里的那一处照样能抢先命中)",
        HLP,
        "    first = text.count(start)\n    if first != 1:",
        "    first = text.count(start)\n    if first < 1:",
    ),
    (
        "H2",
        "window 不查终点唯一性(提前截断重新畅通 —— 14-13 那条 `),` 的病根)",
        HLP,
        "        if count != 1:",
        "        if count < 1:",
    ),
    (
        "H3",
        "window 不查起点之后有没有内容(空窗口让任何 not in 平凡为真)",
        HLP,
        "    if not chunk[len(start) :].strip():",
        "    if False:",
    ),
    (
        "H4",
        "braced_block 退回「第一个右括号」(嵌套一层就切窄,反向断言静默变真)",
        HLP,
        "    depth = 0\n"
        "    for i in range(begin, len(text)):\n"
        '        if text[i] == "{":\n'
        "            depth += 1\n"
        '        elif text[i] == "}":\n'
        "            depth -= 1\n"
        "            if depth == 0:\n"
        "                return text[begin : i + 1]",
        "    for i in range(begin, len(text)):\n"
        '        if text[i] == "}":\n'
        "            return text[begin : i + 1]",
    ),
    (
        "H5",
        "only_line 不查唯一性(两行同前缀时挑第一行 —— 挑到哪一行全看运气)",
        HLP,
        "    if len(hits) != 1:",
        "    if len(hits) < 1:",
    ),
    (
        "H6",
        "only_line 改成子串匹配(注释那行重新算命中 —— 正是它要解决的那件事)",
        HLP,
        "    hits = [ln for ln in text.splitlines() if ln.lstrip().startswith(prefix)]",
        "    hits = [ln for ln in text.splitlines() if prefix in ln]",
    ),
    # ------------------------------------------- G 组:注册(硬规则 3)
    (
        "G1",
        "Makefile 的 check-offline 不再跑它(本地跑门禁的人看不到这一条)",
        MK,
        # 锚点是**一整行**。a64 把 check-offline 的依赖拆成了一项一行,
        # 于是"某一项"有了一个不随清单增长而变的表示。
        #
        # 这条锚点断过两次,病因一样:锚在**当时的清单**上。第一版编进了前六项
        # (a61 插 `fe-test-pure` 时断);a61 的修法是"再往右多带一项",而多带的
        # 那一项也是清单成员(a64 插 `audit-dataclass-fields` 时又断)。
        # **锚在会变的清单上,修法不能是换一段同样会变的清单。**
        "\n\taudit-guards \\",
        "",
    ),
    (
        "G2",
        "CI 里那条命令删掉(分支保护挂的是 all-green,而它不会再跑这一条)",
        CI,
        "        run: cd backend && python3 tools/audit_source_guards.py\n",
        "",
    ),
    (
        "G3",
        "门禁表里不登记(CI 哪天把这一步删掉,不会有任何东西发现)",
        VD,
        '        "audit_source_guards.py": "守卫窗口体检（反向断言不许吃切窄的源码）",\n',
        "",
    ),
]

SUITE_FILTER = "test_a45_batch14_14_source_windows.py"


def run_suite(cwd: Path, name_filter: str = SUITE_FILTER) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "tools/run_pure_tests.py", name_filter],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=1800,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin", "HOME": str(cwd)},
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    results = []
    for ident, description, rel, old, new in MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            shutil.copytree(
                PROJECT,
                root,
                symlinks=True,
                ignore=shutil.ignore_patterns(
                    ".git", "node_modules", "__pycache__", "dist"
                ),
            )
            work = root / "backend"
            target = (work / rel).resolve()
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                results.append(
                    (
                        ident,
                        f"{description} —— 锚点出现 {text.count(old)} 次,拒绝变异",
                        False,
                        [],
                        False,
                    )
                )
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            code, output = run_suite(work)
            failed = [
                line.split("::", 1)[1].strip()
                for line in output.splitlines()
                if "FAILED" in line and "::" in line
            ]
            # 「导入期就炸了」的检测:**不能**只在整段输出里找异常名字。
            #
            # S6 那条变异(把 getattr 换成属性访问)的**正确**失败方式就是
            # 一条 AttributeError —— 它由守卫在用例内部触发,是一次真红。
            # 原来那版按子串扫全段输出,把它误报成「不算数」。
            #
            # 判据改成结构性的:套件正常跑完(打印了统计行)且点得出是哪条
            # 用例红的,就说明它没有炸在导入期。
            ran = "passed" in output and any(
                "FAILED" in line and "::" in line for line in output.splitlines()
            )
            crashed = not ran and any(
                marker in output
                for marker in (
                    "NameError",
                    "ImportError",
                    "SyntaxError",
                    "ModuleNotFoundError",
                    "AttributeError",
                )
            )
            results.append((ident, description, code != 0, failed, crashed))

    ok = True
    for ident, description, went_red, failed, crashed in results:
        mark = "RED " if went_red else "GREEN"
        note = ""
        if crashed:
            note = "  <- 导入期就炸了,这条变异不算数"
            ok = False
        elif went_red and failed:
            note = "  " + ", ".join(failed[:2])
        print(f"{ident:<4} {description}  {mark}{note}")
        ok = ok and went_red
    reds = sum(1 for r in results if r[2] and not r[4])
    print()
    print(f"{reds}/{len(results)} 条变异验红")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
