#!/usr/bin/env python3
"""a60 变异验证:S4 的乐观锁、回滚理由与真 diff。

用法:python3 tools/mutate_a60.py

## 本批要验的核心是"少一个动作"

AC-35 的全部内容是**三个动作都要**。而"漏掉其中一个"这种缺陷不会有任何症状:
保存有锁、切版本没有,在单人使用时完全看不出区别,直到两个运营同时在这一页上。
M1~M3 逐个把锁拆掉,验守卫认不认得出少的是哪一个。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

SVC = "app/services/prompt_service.py"
API = "app/api/prompts.py"
PAGE = "../frontend/src/pages/PromptsPage.tsx"
DIFF = "../frontend/src/components/TextDiff.tsx"
# a61 把判定搬进了这个 .ts(剥类型能跑,.tsx 不能),M13 跟着搬
DIFF_PURE = "../frontend/src/utils/textDiff.ts"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M1",
        "保存不再检查版本(A 保存 v5,B 停在 v4 点保存 -> 直接落 v6)",
        SVC,
        "    _lock_key(session, key)\n    _assert_expected_version(session, key, expected_version)\n\n    last_error",
        "    _lock_key(session, key)\n\n    last_error",
    ),
    (
        "M2",
        "切版本不再检查(A 切 v3、B 同时切 v5,后到的静默赢)",
        SVC,
        "    _lock_key(session, key)\n    _assert_expected_version(session, key, expected_version)\n\n    row = session.execute(",
        "    _lock_key(session, key)\n\n    row = session.execute(",
    ),
    (
        "M3",
        "恢复默认不再检查(三个动作里破坏性最强的那个没有锁)",
        SVC,
        "    _lock_key(session, key)\n    _assert_expected_version(session, key, expected_version)\n    _deactivate_all(session, key)",
        "    _lock_key(session, key)\n    _deactivate_all(session, key)",
    ),
    (
        "M4",
        "检查挪到拿锁之前(检查完照样能被插进来,只是让人以为有防线)",
        SVC,
        "    _lock_key(session, key)\n    _assert_expected_version(session, key, expected_version)\n\n    row = session.execute(",
        "    _assert_expected_version(session, key, expected_version)\n    _lock_key(session, key)\n\n    row = session.execute(",
    ),
    (
        "M5",
        "内置默认用 0 表示(「回到默认」和「第 0 版」长得一样)",
        SVC,
        "    actual = row.version if row is not None else None",
        "    actual = row.version if row is not None else 0",
    ),
    (
        "M6",
        "409 只回一句话(运营判断不了该放弃还是该合并)",
        API,
        '                "expected_version": exc.expected,\n',
        "",
    ),
    (
        "M7",
        "保存端点接错异常类型(服务层抛的那个没人接 -> 运营看到 500)",
        API,
        # 直接删掉 except 会留下一个没有处理器的 try —— 那是语法错,
        # 变异脚本会判成"导入期就炸了,这条不算数"。换类型才是等价的坏法:
        # 语法完好、看起来接住了,而真正会抛的那个从这里漏出去
        "        row = prompt_service.save_version(\n            session,\n            key,\n            body.content,\n            note=body.note,\n            updated_by=actor,\n            expected_version=body.expected_version,\n        )\n    except prompt_service.PromptConflict as exc:",
        "        row = prompt_service.save_version(\n            session,\n            key,\n            body.content,\n            note=body.note,\n            updated_by=actor,\n            expected_version=body.expected_version,\n        )\n    except LookupError as exc:",
    ),
    (
        "M8",
        "前端不传看到的版本(锁做好了而没人用)",
        PAGE,
        "    mutationFn: () => promptsApi.save(promptKey, content, note, seenVersion),",
        "    mutationFn: () => promptsApi.save(promptKey, content, note),",
    ),
    (
        "M9",
        "冲突提示长出重试按钮(重试正是这里最不该做的事 —— 它会覆盖对方)",
        PAGE,
        '            error={conflict}\n            kind="write"',
        '            error={conflict}\n            onRetry={() => save.mutate()}\n            kind="write"',
    ),
    (
        "M10",
        "回滚理由又写回版本行(顺手抹掉 ListingImageSet.note 那笔欠账)",
        SVC,
        "    _deactivate_all(session, key)\n    row.is_active = True\n    session.flush()",
        "    _deactivate_all(session, key)\n    row.is_active = True\n    row.note = note\n    session.flush()",
    ),
    (
        "M11",
        "事件码改回三元(注册表侧变红,读起来像登记多了)",
        SVC,
        '    fields = {"key": key, "version": version, "actor": updated_by}\n    if note and note.strip():',
        '    fields = {"key": key, "version": version, "actor": updated_by}\n    if False:',
    ),
    (
        "M12",
        "回滚理由不进审计(日志滚掉之后再也查不到谁退回去的)",
        API,
        '            "note": body.note,\n        },\n    )\n    session.commit()\n    return prompt_service.describe(session, key)',
        "        },\n    )\n    session.commit()\n    return prompt_service.describe(session, key)",
    ),
    (
        "M13",
        "diff 退回并排显示(状态名叫 diffOpen 而里面没有对比)",
        DIFF_PURE,
        "function lcsTable(a: string[], b: string[]): number[][] {",
        "function notLcs(a: string[], b: string[]): number[][] {",
    ),
    (
        "M14",
        "diff 比的是已保存版而不是编辑中的内容(答非所问)",
        PAGE,
        "          right={content}",
        "          right={query.data?.content ?? ''}",
    ),
    (
        "M15",
        "diff 用调色板里没有的 token(颜色错了不会有任何征兆)",
        DIFF,
        "del: { bg: brandVars.dangerBg, mark: '\u2212' },",
        "del: { bg: brandVars.dangerSoft, mark: '\u2212' },",
    ),
]

SUITE_FILTER = "test_a60_prompt_concurrency.py"


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
