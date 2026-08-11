#!/usr/bin/env python3
"""A46-phase5 变异验证:文档审核那批守卫,逐条验红。

用法:python3 tools/mutate_a46_phase5.py

## 这一批守的东西为什么必须变异验

本轮新增的守卫**全部是文档类**,而文档类守卫有一个特有的失效方式:
判据写得太宽,于是它对任何文本都绿。`assert not problems` 里的 `problems`
永远是空列表 —— 这种守卫和不存在是一回事,但它在报告里占一行 PASS,
让人以为这件事已经被盯住了。这正是本轮修的四类缺陷里最贵的那一类
(「宣告一件没发生的事」)的**同构体**,只不过发生在守卫自己身上。

所以下面每一条都把被守的那件事真的改坏,看守卫认不认得出。

    C 组  副本与说法       AGENTS.md 分叉、菜单收敛的说法翻回去(正反两个方向)
    G 组  幽灵守卫         注释改回引用一个不存在的文件
    N 组  写死的数         文档地图、过程文档、示例条数、硬错误条数

## N 组里那两条方向相反,是刻意的

「不许写死」(示例条数)和「写死了就必须是真的」(sample-data/README 的图片数)
是两条不同的规矩,分别对应两种读者。变异也因此分两个方向:前者塞一个数进去
要变红,后者把真数改错要变红。

## 为什么部署入口那一组不在这份脚本里

第一版这里写的是 `SUITE_FILTER = "a46"`,想用一个子串同时覆盖 phase2 与
phase5 两个套件。`tools/audit_anchors.py` 当场拦下:子串挑中两个套件,
一条变异可能被**别的**套件里的守卫抓住,而报告记在这份脚本头上 —— 归因是假的。
这正是 `run_pure_tests.py` 顶部那条告诫说的事,而本轮的主题恰恰是「说的和做的
要对上」,在自己的变异脚本上放过它就太难看了。

所以拆成两份,各自钉一个套件:部署入口那一组(它该由 phase2 那条被扩过窗口的
守卫接住)在 `tools/mutate_a46_phase5_deploy_docs.py`。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

SUITE_FILTER = "test_a46_phase5_doc_truth.py"

APP = "../frontend/src/App.tsx"
README = "../README.md"
STATUS = "../docs/STATUS.md"
HEALTH = "app/api/health.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ---------------------------------------------- C 组:副本与说法
    (
        "C1",
        "AGENTS.md 与 CLAUDE.md 分叉一行(旧世界里没有任何东西会发现)",
        "../AGENTS.md",
        "# CLAUDE.md — 仓库总纲",
        "# CLAUDE.md — 仓库总纲(有人只改了这一份)",
    ),
    (
        "C2",
        "把「只有管理员看得见」放回 App.tsx(说法翻回废弃设计,代码没动)",
        APP,
        " * 「系统管理」在最下面,而且**只有管理员看得见**(`adminOnly: true`)。",
        " * 「系统管理」在最下面,而且只有管理员看得见 —— 这句话现在是对的,\n"
        " * 但下面那段说明仍写着「那一版没有落地」,两边就此对不上。",
    ),
    (
        "C3",
        "反方向:把菜单过滤撤回去,而四个文件的说法还写着「只有管理员看得见」",
        "../frontend/src/components/AppLayout.tsx",
        "  const visible = groups.filter((g) => !g.adminOnly || isAdmin)",
        "  const visible = groups",
    ),
    # ---------------------------------------------- G 组:幽灵守卫
    (
        "G1",
        "health.py 改回引用一个不存在的守卫文件(现在时,无过去式标记)",
        HEALTH,
        "`test_a46_phase2_browser_login_seam.py`",
        "`test_browser_login_frontend.py`",
    ),
    # ---------------------------------------------- N 组:写死的数
    (
        "N1",
        "文档地图那句话与表格行数分叉(19 -> 13,正是本轮修前的状态)",
        STATUS,
        "一共 19 份。",
        "一共 13 份。",
    ),
    (
        "N2",
        "地图外过程文档的条数写回旧值(9 -> 29)",
        STATUS,
        "过程结论(眼下 9 份)",
        "过程结论(眼下 29 份)",
    ),
    (
        "N3",
        "README 把示例数据条数又写死一次(实际 51,写 30)",
        README,
        "导入示例数据(幂等,可重复执行。",
        "导入示例数据(10 个商品 + 30 张素材,幂等,可重复执行。",
    ),
    (
        "N4",
        "反方向:sample-data/README 里那个数改成假的(51 -> 30)",
        "../sample-data/README.md",
        "51 张 900×1200 JPEG",
        "30 张 900×1200 JPEG",
    ),
    (
        "N5",
        "README 把硬错误代码条数冻回 15(HardFailCode 实际 21 条)",
        README,
        "**硬错误只淘汰候选,不终结任务。** 硬错误代码",
        "**硬错误只淘汰候选,不终结任务。** 15 个硬错误代码,",
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
