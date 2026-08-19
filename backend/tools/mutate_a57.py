#!/usr/bin/env python3
"""a57 变异验证:把每条守卫对应的决定改回去,确认它真的变红。

用法:python3 tools/mutate_a57.py

机制与 `tools/mutate_batch14_2.py` 相同(复制工作树 -> 改源码 -> 跑本批守卫),
理由不再重复。这里只记与前几份不同的地方:

## 本批**必须**验的是保留期那三条

`purgeable()` 的两道闸有一个共同的失效方式:**放宽之后测试仍然大面积绿**。
删多了不会报错 —— 表变小了而已,而且要几个月后有人问「那一版跑过什么」
才看得出来。所以 M4/M5/M6 分别拿掉"引用还活着"、把顺序换过来、把不变量
降级成可调项,三种都能让一条正在生效的证据被清掉。

## 锚点会过期,这是常态

改被变异的源码时,连着更新这份脚本 —— 失锚的变异不报错、不变红,只是安静
地什么都没验。a57 的第一件事就是修 `mutate_batch14_2.py` 的 N12 失锚,
起因正是它。锚点由 `tools/audit_anchors.py` 每次现查。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

RECORD = "app/workflows/ai_test_record.py"
ARCHIVE = "app/services/ai_test_archive.py"
EVAL_SVC = "app/services/evaluation_service.py"
WB_SVC = "app/workbench/service.py"
VERSIONING = "app/prompts/versioning.py"
MIGRATION = "migrations/versions/0055_prompt_version_string.py"
MODEL = "app/models/evaluation.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ---------------------------------------------------------- 成形
    (
        "M1",
        "评分也落全文(开始抄 EvaluationAttempt 的第二份,两份会分叉)",
        RECORD,
        'payload_out=_redacted({"attempt_id": str(attempt_id)} if attempt_id else None),',
        'payload_out=_redacted({"attempt_id": str(attempt_id), "scores": {}} if attempt_id else None),',
    ),
    (
        "M2",
        "文案输出不过脱敏(模型原样吐回来的东西直接进库)",
        RECORD,
        """        payload_out=_redacted(
            {
                "copy": result.get("copy"),""",
        """        payload_out=dict(
            {
                "copy": result.get("copy"),""",
    ),
    (
        "M3",
        "bool 不再排除(success=True 变成 token 数 1)",
        RECORD,
        "    return value if isinstance(value, int) and not isinstance(value, bool) else None",
        "    return value if isinstance(value, int) else None",
    ),
    # ---------------------------------------------------------- 保留期不变量
    (
        "M4",
        "拿掉「引用还活着」那道闸(当前生效版本的唯一证据被清掉)",
        RECORD,
        "        if _still_referenced(row, active):\n            continue",
        "        if False:\n            continue",
    ),
    (
        "M5",
        "保留期为 0 时不变量跟着失效(把不可协商的那半降级成可调项)",
        RECORD,
        "        if _still_referenced(row, active):",
        "        if retention_days and _still_referenced(row, active):",
    ),
    (
        "M6",
        "负的保留期不再拒绝(会删掉刚写下的行)",
        RECORD,
        '    if retention_days < 0:\n        raise ValueError(',
        '    if False:\n        raise ValueError(',
    ),
    # ---------------------------------------------------------- 接线
    (
        "M7",
        "解析失败那条出路不留档(失败的测试从历史里消失)",
        EVAL_SVC,
        "        _archive_diagnostic(session, candidate, attempt, evaluator, product, actor)\n        return None, attempt\n    except ProviderError as exc:",
        "        return None, attempt\n    except ProviderError as exc:",
    ),
    (
        "M8",
        "文案失败分支不留档(「跑了但没成」和「没跑过」变得一样)",
        WB_SVC,
        "        _archive_copy_test(session, product, failed, actor=actor)\n",
        "",
    ),
    (
        "M9",
        "留档失败重新抛出(钱花了、结果也没了,而丢的本来只是台账)",
        ARCHIVE,
        "        )\n        return None\n    logger.info(",
        "        )\n        raise\n    logger.info(",
    ),
    (
        "M10",
        "端点退回默认 actor(留档答不出「谁跑的」)",
        "app/api/workbench.py",
        "        generator_name=payload.generator,\n        actor=current_actor(request),",
        "        generator_name=payload.generator,",
    ),
    # ---------------------------------------------------------- BE-307
    (
        "M11",
        "迁移退回那张不存在的表(在任何真库上当场失败)",
        MIGRATION,
        '_TABLE = "evaluation_attempts"',
        '_TABLE = "evaluations"',
    ),
    (
        "M12",
        "ORM 不跟着改型(测试库与生产库结构不一致)",
        MODEL,
        "    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)",
        "    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)",
    ),
    (
        "M13",
        "版本文本退回静默丢弃器(接上内容哈希后落库版本集体变空)",
        VERSIONING,
        "    text = str(value).strip()\n    return text[:_COLUMN_CHARS] if text else None",
        "    return value if isinstance(value, int) else None",
    ),
    (
        "M14",
        "成形层漏一列(那一列永远 NULL,而没有任何东西会红)",
        RECORD,
        '            "total_tokens": self.total_tokens,\n',
        "",
    ),
]

SUITE_FILTER = "test_a57_ai_test_archive.py"


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
                # 不唯一就拒改:改了也不知道改的是哪一处
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
