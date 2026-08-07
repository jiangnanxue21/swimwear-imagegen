#!/usr/bin/env python3
"""A45-batch14-24 变异验证:列写入点审计自己。

用法:python3 tools/mutate_batch14_24.py

## 这一批变异在防什么

本批交付的是一份**审计**。审计的失效方式和功能代码不同:它不会报错,
它会安静地少查一点,而报告仍然是绿的。第一版当场演示过这件事 ——
一处动态 `setattr` 让 40 个模型全部划进"判不了",输出「0 列答得出」
**而退出码是 0**。

    A 组  审计的射程。把模型/写入点识别掐掉一类,看守卫认不认得出
    L 组  台账。条目失效检测、原因、还款日
    R 组  注册。审计从 verify_delivery / CI 上摘下来

## R 组为什么值得有

一份单独存在的审计脚本会在第二个月变成没人跑的脚本 —— 这个仓库对此的
处理一律是"挂到门禁上,再给门禁加一条盯它有没有被挂"。R 组钉的正是
那条"盯它有没有被挂"。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

AUDIT = "tools/audit_column_writers.py"
VERIFY = "tools/verify_delivery.py"
CI = "../.github/workflows/ci.yml"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------------ A 组:射程塌陷
    (
        "A1",
        "动态 setattr 重新按「整个仓库判不了」处理(审计输出 0 列而退出码 0)",
        AUDIT,
        'dynamic.add((path, where.get(id(node), "<module>")))',
        'pass\n'
        '                        for _m in models.values():\n'
        '                            _m.unknowable = "dynamic setattr"',
    ),
    (
        "A2",
        "关系赋值不再算写外键(ColorVariant.spu_id 变成假红,消法是删门禁)",
        AUDIT,
        "                        elif kw.arg in model.relationships and model.relationships[kw.arg]:\n"
        "                            written.add(f\"{model.name}.{model.relationships[kw.arg]}\")",
        "",
    ),
    (
        "A3",
        "构造点关键字不再算写入(全表变红,红得太多等于没报)",
        AUDIT,
        '                        if kw.arg in model.columns:\n'
        '                            written.add(f"{model.name}.{kw.arg}")',
        "                        if False:\n                            pass",
    ),
    # ------------------------------------------------ L 组:台账失去自净
    (
        "L1",
        "台账条目失效不再报(某列有了写入点,条目留着当挡箭牌)",
        AUDIT,
        "                if key in ledger:\n                    out.stale.append(key)",
        "                if False:\n                    pass",
    ),
    (
        "L2",
        "台账整条清空(所有已知欠账消失,审计仍绿)",
        AUDIT,
        'LEDGER: dict[str, str] = {',
        'LEDGER: dict[str, str] = {} or {',
    ),
    # ------------------------------------------------ R 组:摘下注册
    (
        "R1",
        "从 verify_delivery 的 CHECKS 上摘掉",
        VERIFY,
        '    ("每一列都答得出谁写它", check_every_column_can_say_who_writes_it),\n',
        "",
    ),
    (
        "R2",
        "从 CI 上摘掉",
        CI,
        "      - name: 列写入点审计\n        run: cd backend && python3 tools/audit_column_writers.py\n",
        "",
    ),
]

SUITE_FILTER = "test_a45_batch14_24_column_writer_audit.py"


def run_suite(cwd: Path, name_filter: str) -> tuple[int, str]:
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
    red = 0
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
                print(f"{ident:4} 锚点失准({text.count(old)} 次命中) —— 无法验证")
                continue
            target.write_text(text.replace(old, new), encoding="utf-8")
            code, _ = run_suite(work, SUITE_FILTER)
            verdict = "RED" if code != 0 else "GREEN"
            if code != 0:
                red += 1
            print(f"{ident:4} {description}  {verdict}")

    print(f"\n{red}/{len(MUTATIONS)} 条变异验红")
    return 0 if red == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
