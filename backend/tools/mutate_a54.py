#!/usr/bin/env python3
"""a54 变异验证:运行日志控制台那批**修复**的守卫,逐条验红。

用法:python3 tools/mutate_a54.py

## 为什么这一批非验不可

a54 修的十件事有一个共同形状:**每一件都不会报错、不会变红,只会让控制台
在某个具体时刻少说一句真话**。worker 的日志一条都不进环形,页面看起来像
"这段时间没跑任务";看日志的动作把要查的证据顶出窗口,而 `held/cap` 显示
5000/5000;级别筛 WARNING 把 ERROR 挡掉,而列表看起来只是"这段时间很干净"。

也就是说,守住它们的那些断言如果写宽了,**没有任何人会发现** —— 它们会在
报告里安安静静占一行 PASS。所以每一条都把被守的那件事真的改回去,看守卫
认不认得出。

    W  进程        worker 不装 JSON 日志
    S  自指        环形不挡控制台自己的访问日志
    L  语义        级别回到精确匹配 / CRITICAL 可以被折叠
    T  留痕        字节预算砍掉的字段不列进 truncated
    I  身份        `"-"` 当成一个合法的 call id
    B  记账        旁挂库写失败不记数
    D  诚实        环形关掉之后照样画一张空列表
    P  开销        回到"先成形再丢掉"
    G  计数        域计数吃 domain 筛选

## 锚点里不许出现被守卫读的那个数

`tools/mutate_a46_phase5.py` 的 N 组栽在这上面:锚点写死了「一共 19 份」,
文档一加就失锚,而失锚的变异**什么都没验**。这份脚本的锚点一律锚在代码
形状上,不锚在会随内容漂移的字面量上。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

SUITE_FILTER = "test_a53_log_console.py"

CELERY = "app/tasks/celery_app.py"
EVENTS = "app/core/log_events.py"
RING = "app/core/log_ring.py"
STORE = "app/llm/payload_store.py"
API = "app/api/ops_logs.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ---------------------------------------------- W 组:哪个进程在写日志
    (
        "W1",
        "worker 子进程不再装 JSON 日志(环形里一条 tasks 域的日志都没有)",
        CELERY,
        "@worker_process_init.connect\ndef _install_json_logging_in_worker",
        "@beat_init.connect\ndef _install_json_logging_in_worker",
    ),
    # ---------------------------------------------- S 组:自指流量
    (
        "S1",
        "环形不再挡控制台自己的访问日志(看日志的动作冲刷诊断窗口)",
        RING,
        '        return not str(fields.get("path") or "").startswith(self.prefix)',
        "        return True",
    ),
    (
        "S2",
        "反方向:连自己的 5xx 一起挡掉(这一页坏了反而看不见)",
        RING,
        "        if isinstance(status, int) and status >= 400:\n            return True",
        "        if isinstance(status, int) and status >= 600:\n            return True",
    ),
    # ---------------------------------------------- L 组:筛选与折叠的语义
    (
        "L1",
        "级别回到精确匹配(选 WARNING 就看不见 ERROR)",
        EVENTS,
        "    return level_rank(level) >= level_rank(minimum)",
        "    return level_rank(level) == level_rank(minimum)",
    ),
    (
        "L2",
        "CRITICAL 又可以被折进计数条",
        EVENTS,
        'NEVER_FOLD_FROM = LEVEL_ORDER["ERROR"]',
        'NEVER_FOLD_FROM = LEVEL_ORDER["CRITICAL"]',
    ),
    # ---------------------------------------------- T 组:截断显形
    (
        "T1",
        "字节预算砍掉的字段不再列进 truncated(界面以为看到的是全文)",
        STORE,
        "        cut_here.append(label(ranked[0][0]))",
        "        label(ranked[0][0])",
    ),
    # ---------------------------------------------- I 组:call id
    (
        "I1",
        '`"-"` 又被当成一个合法的 call id(连接自检写出 ops:llm:-)',
        STORE,
        "    return bool(call_id) and call_id != NO_CALL_ID",
        "    return bool(call_id)",
    ),
    # ---------------------------------------------- B 组:记账
    (
        "B1",
        "旁挂库写失败又变回静默(404 时分不清过期还是没写过)",
        STORE,
        "        note_failure(exc)\n        STATS.drop(exc)\n        return",
        "        return",
    ),
    # ---------------------------------------------- D 组:关掉之后说什么
    (
        "D1",
        "环形关掉之后照样去读 Redis,读到空就画一张空列表",
        API,
        "    if not settings.OPS_LOG_RING_ENABLED:",
        "    if not settings.REDIS_URL.startswith(chr(0)):",
    ),
    # ---------------------------------------------- P 组:先筛还是先成形
    (
        "P1",
        "回到「先成形再丢掉」(全窗 5000 条每 3 秒 dump 一遍)",
        API,
        "        if not _matches(",
        "        if not _matches_removed(",
    ),
    # ---------------------------------------------- G 组:域计数
    (
        "G1",
        "域计数又吃了 domain 筛选(点进一个域,其余十四格全是 0)",
        API,
        "            domain=None,",
        "            domain=domain,",
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
                ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", "dist"),
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
