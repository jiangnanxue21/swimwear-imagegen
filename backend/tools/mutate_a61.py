#!/usr/bin/env python3
"""a61 变异验证:前端行为测试通路与调色板检查。

用法:python3 tools/mutate_a61.py

## 本批的核心是"通路看起来在,而实际没有对象"

一条门禁最贵的坏法不是红,是**绿着什么都没跑**。M3~M5 造的正是这个:
删光用例、把判定搬回 .tsx、把目录直接交给 --test —— 三种都能让
`make fe-test-pure` 输出一段看起来正常的东西。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

RUNNER = "../frontend/tools/run-pure-tests.mjs"
SYNTAX = "../frontend/tools/syntax-check.mjs"
PURE = "../frontend/src/utils/textDiff.ts"
TSX = "../frontend/src/components/TextDiff.tsx"
TEST = "../frontend/tests/pure/textDiff.test.ts"
MAKEFILE = "../Makefile"
CI = "../.github/workflows/ci.yml"
STATUS = "../docs/STATUS.md"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M1",
        "运行器不进 check-offline(只在有人记得手敲时跑)",
        MAKEFILE,
        # 同 `mutate_batch14_14` G1:a64 把依赖拆成一项一行,锚点跟着改成一整行。
        # 原文锚在"清单的前三项"上 —— 那是一段会变的东西
        "\n\tfe-test-pure \\",
        "",
    ),
    (
        "M2",
        "CI 不跑它(本机绿而 CI 从来没验过)",
        CI,
        "      - name: 前端纯逻辑测试(零依赖)\n        run: cd frontend && node tools/run-pure-tests.mjs\n",
        "",
    ),
    (
        "M3",
        "找不到用例时静静通过(什么都没跑和全都过了长得一样)",
        RUNNER,
        "if (files.length === 0) {",
        "if (false) {",
    ),
    (
        "M4",
        "判定搬回 .tsx(通路还在,而它的对象没了)",
        PURE,
        "export function diffLines(",
        "function diffLinesUnused(",
    ),
    (
        "M5",
        "把目录直接交给 --test(node 当 CJS 模块 require,报 Cannot find module)",
        RUNNER,
        # a68 在 `--test` 前面插了探测出来的 runtime 参数(A67-ISSUE-003),
        # 锚点跟着搬到 spawn 那一行的尾巴上。变异本身没变:把文件列表换成目录
        "...nodeArgs, ...files]",
        "...nodeArgs, 'tests/pure']",
    ),
    (
        "M6",
        "运行器引第三方依赖(退回成 fe-check 的一部分,而那跑不了)",
        RUNNER,
        "import { spawnSync } from 'node:child_process'",
        "import { spawnSync } from 'node:child_process'\nimport _x from 'vitest'",
    ),
    (
        "M7",
        "调色板解析失败时静静跳过(这一格自己变成一句空话)",
        SYNTAX,
        "    console.log('  BROKEN src/theme.ts 解析不出 lightTokens,调色板检查失去了判据')\n    badTokens += 1",
        "    console.log('  BROKEN src/theme.ts 解析不出 lightTokens,调色板检查失去了判据')",
    ),
    (
        "M8",
        "离线体检不再扫 brandVars(颜色写错回到完全静默)",
        SYNTAX,
        "for (const m of text.matchAll(/brandVars\\.([a-zA-Z]+)/g)) {",
        "for (const m of text.matchAll(/__never__\\.([a-zA-Z]+)/g)) {",
    ),
    (
        "M9",
        "用例文件的 import 省掉扩展名(Node ESM 不补,MODULE_NOT_FOUND)",
        TEST,
        "from '../../src/utils/textDiff.ts'",
        "from '../../src/utils/textDiff'",
    ),
    (
        "M10",
        "渲染层自己留一份判定(两份会分叉)",
        TSX,
        "import { type Op, diffLines } from '../utils/textDiff'",
        "import { type Op } from '../utils/textDiff'\nfunction lcsTable() { return [] }\nconst diffLines = () => []",
    ),
    (
        "M11",
        "STATUS 把射程那句话删掉(下一个人会以为 fe-check 已经不欠了)",
        STATUS,
        "**别把 `fe-test-pure` 的绿读成 `fe-check` 的绿**",
        "这条通路已经覆盖了前端",
    ),
    (
        "M12",
        "运行器的输出不再说明射程",
        RUNNER,
        "'\\x1b[2m    只覆盖判定层 —— 剥类型不做 JSX 变换,组件渲染仍然只有 Vitest 验得了\\x1b[0m',",
        "'',",
    ),
]

SUITE_FILTER = "test_a61_frontend_pure_tests.py"


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
                ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", "dist"),
            )
            work = root / "backend"
            target = (work / rel).resolve()
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                results.append(
                    (ident, f"{description} —— 锚点出现 {text.count(old)} 次,拒绝变异",
                     False, [], False)
                )
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            code, output = run_suite(work)
            failed = [
                line.split("::", 1)[1].strip()
                for line in output.splitlines()
                if "FAILED" in line and "::" in line
            ]
            crashed = any(
                marker in output
                for marker in ("NameError", "ImportError", "SyntaxError", "ModuleNotFoundError")
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
