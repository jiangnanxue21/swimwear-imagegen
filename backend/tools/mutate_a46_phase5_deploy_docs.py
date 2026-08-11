#!/usr/bin/env python3
"""A46-phase5 变异验证(第二份):三把启动键的文档窗口。

用法:python3 tools/mutate_a46_phase5_deploy_docs.py

单独一份,是因为它钉的是**另一个套件**:
`tests/pure/test_a46_phase2_browser_login_seam.py` 里那条
`test_the_startup_blocking_keys_are_documented_somewhere_a_deployer_reads`,
本轮把它的窗口从「只钉 README」扩到了部署的人真的会打开的三份
(README / DEPLOYMENT / MANUAL-ACCEPTANCE),理由在 `docs/DECISIONS.md` §3.70 第三节。

`run_pure_tests.py` 顶部要求变异脚本用**精确文件名**做过滤器,一份脚本因此只能
钉一个套件。合并进 `mutate_a46_phase5.py` 的代价是归因变假 ——
`tools/audit_anchors.py` 会当场拦下,第一版就是这么被拦的。

两条变异分别打在新窗口和旧窗口上:

    D1  新窗口:phase3 那一版守卫看不见 MANUAL-ACCEPTANCE,所以它必须由本轮的扩窗口接住
    D2  旧窗口:README 那一份仍然要在,扩窗口不能把原来钉住的那条弄丢
    D3  (phase6 自审补)401 会话文案改回口令时代的指路牌 —— Vitest 那条在
        单测环境里够不到这一支,只有这条源码级守卫接得住
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

SUITE_FILTER = "test_a46_phase2_browser_login_seam.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "D1",
        "把 ADMIN_PASSWORD 从 UAT 验收手册拿掉(phase3 的旧窗口不会发现)",
        "../docs/MANUAL-ACCEPTANCE.md",
        "ADMIN_PASSWORD=<浏览器登录:管理员密码>\n",
        "",
    ),
    (
        "D2",
        "把 ADMIN_PASSWORD 从 README 拿掉(扩窗口不能把原来钉住的那条弄丢)",
        "../README.md",
        "ADMIN_PASSWORD=换成一个真密码\n",
        "",
    ),
    (
        "D3",
        "401 会话文案改回「到系统设置页核对口令」(单测环境够不到这一支)",
        "../frontend/src/api/client.ts",
        "? ' —— 登录状态已失效,请重新登录'",
        "? ' —— 到「系统设置」页核对口令后重试'",
    ),
]


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
                ignore=shutil.ignore_patterns(
                    ".git", "node_modules", "__pycache__", "dist"
                ),
            )
            work = root / "backend"
            target = (work / rel).resolve()
            text = target.read_text(encoding="utf-8")
            probe = old.replace("\n", "\r\n") if "\r\n" in text else old
            payload = new.replace("\n", "\r\n") if "\r\n" in text else new
            if text.count(probe) != 1:
                print(f"{ident:4} 锚点失准({text.count(probe)} 次命中) —— 无法验证")
                continue
            target.write_text(text.replace(probe, payload), encoding="utf-8")
            code, _ = run_suite(work, SUITE_FILTER)
            verdict = "RED" if code != 0 else "GREEN"
            if code != 0:
                red += 1
            print(f"{ident:4} {description}  {verdict}")

    print(f"\n{red}/{len(MUTATIONS)} 条变异验红")
    return 0 if red == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
