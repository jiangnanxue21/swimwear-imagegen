#!/usr/bin/env python3
"""A45-batch14-3 变异验证:把每条守卫对应的决定改回去,确认它真的变红。

用法:python3 tools/mutate_batch14_3.py

机制与 `tools/mutate_batch14_2.py` 相同(复制工作树 -> 改源码 -> 跑本批守卫)。
这里只记与前两份不同的地方:

## 本批有一半变异改的是**工具自己**

F6 的守卫盯的是 `tools/audit_anchors.py`。变异一份审计工具和变异一段业务
判定是同一件事 —— 一份扫不出问题的审计比一条恒绿的守卫更危险,
因为它给出的是"全都对得上"这种结论,读的人会据此不再检查。

`P*` 那几条专挑审计里"少查一件事就会静默放过"的位置:把 `count != 1`
放宽成 `not in`、把读不出来的行当成跳过、把空树当成通过。
每一条都不会让审计报错 —— 它只会**更绿**。

## 锚点会过期,这是常态

改被变异的源码时连着更新这份脚本。跑 `make audit-anchors` 能在
一秒内告诉你哪一条失锚了 —— 那正是这一批加的东西。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

AUDIT = "tools/audit_anchors.py"
SCHEMA = "app/extractors/schema.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------------------- F5 解析失败的诊断
    (
        "P1",
        "按 & 切而不是按 ? 切(第一个参数留下,而签名通常就是第一个)",
        SCHEMA,
        '    head, sep, _ = match.group(0).partition("?")',
        '    head, sep, _ = match.group(0).partition("&")',
    ),
    (
        "P2",
        "不抹查询串(预签名签名进日志)",
        SCHEMA,
        "    redacted = _URL_IN_TEXT.sub(_strip_query, text)",
        "    redacted = text",
    ),
    (
        "P3",
        "把整个地址抹掉(查不出是哪张图)",
        SCHEMA,
        '    return head + ("?<已抹去>" if sep else "")',
        '    return "<已抹去>"',
    ),
    (
        "P4",
        "截断了不说自己截了",
        SCHEMA,
        '    return redacted[:limit] + f"…(截断,共 {len(text)} 字符)"',
        "    return redacted[:limit]",
    ),
    (
        "P5",
        "根本不截断(一条日志撑爆采集管道)",
        SCHEMA,
        "    if len(redacted) <= limit:\n        return redacted",
        "    if True:\n        return redacted",
    ),
    (
        "P6",
        "解析失败不挂原文摘要(服务层只剩一个类型名)",
        "app/extractors/vision.py",
        '                "raw_excerpt": redact_for_log(text),',
        "",
    ),
    (
        "P7",
        "成功路径也被包装层改写",
        "app/extractors/vision.py",
        "        try:\n            return parse_extraction_payload(text)",
        "        try:\n            parse_extraction_payload(text)\n            return None",
    ),
    (
        "P8",
        "服务层不分异常类型(退回只记类型名)",
        "app/attributes/service.py",
        "            if isinstance(exc, ExtractionParseError):",
        "            if False:",
    ),
    (
        "P9",
        "服务层把所有异常的消息都记进日志(provider 地址里的凭据泄进日志)",
        "app/attributes/service.py",
        '                "error": type(exc).__name__,',
        '                "error": str(exc),',
    ),
    # ------------------------------------------------------- F6 审计工具自己
    (
        "P10",
        "锚点只查零次、不查两次(退回另外两份老脚本的口径)",
        AUDIT,
        "    count = text.count(anchor.old)\n    if count == 1:",
        "    count = text.count(anchor.old)\n    if count >= 1:",
    ),
    (
        "P11",
        "读不出来的行当成跳过(没被审计的行报成通过)",
        AUDIT,
        "                except Unreadable as exc:\n"
        "                    problems.append(f\"{where}:{exc}\")\n"
        "                    continue",
        "                except Unreadable:\n                    continue",
    ),
    (
        "P12",
        "一份变异脚本都没有时报通过(审计一棵不存在的树)",
        AUDIT,
        '        print(f"{RED}一份变异脚本都没找到{RESET} —— 审计对象为空,这不是通过")\n        return 1',
        '        return 0',
    ),
    (
        "P13",
        "未知形状的表当成没问题",
        AUDIT,
        "    if not seen_tables:",
        "    if False:",
    ),
    (
        "P14",
        "不查派发目标还在不在(F7 那条永远发现不了)",
        AUDIT,
        "        if defined is not None and anchor.dispatch not in defined:",
        "        if False:",
    ),
    (
        "P15",
        "不查 SUITE_FILTER 匹配得到用例",
        AUDIT,
        "    suite_problem = check_suite_filter(script, tree, consts)",
        "    suite_problem = None",
    ),
    (
        "P16",
        "一行只报第一个问题(修完派发名字才发现锚点也过期了)",
        AUDIT,
        "            for verdict in verdicts:",
        "            for verdict in verdicts[:1]:",
    ),
    (
        "P17",
        "审计退回 import 被审脚本(顶层变异循环当场改写工作树)",
        AUDIT,
        "import ast\nimport re",
        "import ast\nimport importlib\nimport re",
    ),
]

SUITE_FILTER = "test_a45_batch14_3_fixes.py"


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
                # 不唯一就拒改,且元组必须和下面那条同宽 ——
                # 失锚时脚本自己 ValueError 是 batch14-2 修过的洞
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
