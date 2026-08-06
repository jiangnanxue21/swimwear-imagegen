#!/usr/bin/env python3
"""A45-batch14-16 变异验证:溯源列落库,候选落盘写入(§4.8)。

用法:python3 tools/mutate_batch14_16.py

## 这一批变异在防什么

两列 + 一处写入。**每一条坏法的后果都是同一件事**:一张 AI 生成的图
少一个溯源信号,于是它在 §5.1 白名单里畅通、在 AC-22 拦截前隐形 ——
而它进识别输入的表现是**一次真实的付费调用**,账单上看不出哪一笔是它。

W3 值得单看:删掉过渡记号 `legacy_kind`。真列落库之后它看起来是冗余的,
而**存量的 AI 影子行只有它**(本批刻意不回填)。删掉的表现是历史上
所有 AI 影子行一次性失去溯源,而新行照常拦得住 —— 所以本机跑什么都正常。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent




MODEL = "app/models/media_asset.py"
SVC = "app/media/service.py"
MIG = "migrations/versions/0038_media_asset_provenance.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "C1",
        "溯源列改回不存在(三个判定读到的永远是 None —— 回到本批之前)",
        MODEL,
        "    generation_candidate_id: Mapped[UUID | None] = mapped_column(",
        "    generation_candidate_id_removed: Mapped[UUID | None] = mapped_column(",
    ),
    (
        "C2",
        "溯源列改成 NOT NULL(人工上传的样品没有溯源可填,整条上传链路不可用)",
        MODEL,
        '        ForeignKey("generation_tasks.id", ondelete="SET NULL"),\n        nullable=True,',
        '        ForeignKey("generation_tasks.id", ondelete="SET NULL"),\n        nullable=False,',
    ),
    (
        "C3",
        "溯源列改成级联删除(清理一次生成任务,连坐删掉素材文件)",
        MODEL,
        '        ForeignKey("generation_candidates.id", ondelete="SET NULL"),',
        '        ForeignKey("generation_candidates.id", ondelete="CASCADE"),',
    ),
    (
        "W1",
        "候选落盘不写 task id(少一个溯源信号,而账单上看不出是哪一笔)",
        SVC,
        "        generation_task_id=candidate.task_id,\n",
        "",
    ),
    (
        "W2",
        "候选落盘不写 candidate id(PRD 的 CHECK 原文漏的正是这一条)",
        SVC,
        "        generation_candidate_id=candidate.id,\n",
        "",
    ),
    (
        "W3",
        "删掉过渡记号(存量 AI 影子行一次性失去溯源,而新行照常拦得住)",
        SVC,
        "        legacy_kind=LEGACY_CANDIDATE,\n        legacy_id=candidate.id,\n",
        "",
    ),
    (
        "W4",
        "task id 取自别处(溯源指向另一次生成 —— 比没有溯源更难查)",
        SVC,
        "        generation_task_id=candidate.task_id,",
        "        generation_task_id=candidate.attempt_id,",
    ),
    (
        "W5",
        "ingest 收了溯源却不写进行里(「算好了没人读」那一类)",
        SVC,
        "        generation_task_id=generation_task_id,\n"
        "        generation_candidate_id=generation_candidate_id,\n",
        "",
    ),
    (
        "M1",
        "迁移顺手回填(信任 legacy_id 指得到候选行 —— 人工样品会被标成 AI 生成)",
        MIG,
        "def upgrade() -> None:\n    for column, target in _COLUMNS:",
        "def upgrade() -> None:\n"
        "    op.execute(\"UPDATE media_assets SET generation_candidate_id = legacy_id\")\n"
        "    for column, target in _COLUMNS:",
    ),
]

SUITE_FILTER = "test_a45_batch14_16_provenance.py"


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
