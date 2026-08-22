"""阶段 P0 的清单,做成一条命令(PRD v3.1.1 §13「阶段 P0:基础设施门禁」)。

    make p0-gate

## 为什么需要它

PRD §14.3 写着「开始真实人工测试前必须满足:阶段 P0 全部关闭」。而在此之前
「关没关闭」的唯一依据是散在 `docs/STATUS.md` 各版本块里的几句话 ——
它们过期不会有任何东西报错,这个仓库为这类数字立过规矩(STATUS 第五节)。

这个脚本把那份清单变成可执行的:六项逐条跑,跑得动的真跑,跑不动的**点名
说出缺什么**,并且让退出码非零。它刻意**不**汇报得比实际做到的多。

## 它不是一条 CI 门禁,所以没有进 `check_ci_runs_every_gate()` 的表

硬规则 3 要求「加一条门禁 = 改三个地方」,那条规则针对的是**缺席不会被发现**
的检查。这个脚本不是那一类:

  - 它调用的每一项本身都已经在 CI 里(ruff / pytest / alembic / 前端四条 /
    docker build),漏掉哪一项 CI 会自己红;
  - 它跨越 CI 的 job 边界(前端依赖在 frontend job、库在 backend job、
    镜像在 images job),放进任何一个 job 都会因为**别的 job 才有的前提**
    而永远报「未验证」——一条恒红的门禁会在两周内被人加 `|| true`。

它的读者是**准备冻结人工测试版本的那个人**,回答的是「现在能不能开始人工
测试」。这句话写在这里,免得下一轮有人当成漏接线去「修好」。

## 未验证 ≠ 失败

两者分开报,因为处置方式不同:失败要改代码,未验证要换一台有前提的机器。
但**退出码一视同仁** —— 「没跑过」不能算通过,这正是 P0 存在的理由
(v3.0 把这些后置到 §14.3,等于让每个阶段都建在未验证的地基上)。
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
FRONTEND = ROOT / "frontend"

PASS, FAIL, UNVERIFIED = "通过", "失败", "未验证"


# ---------------------------------------------------------------- 前提探测


def _module(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _tcp(url: str, default_port: int) -> str | None:
    """连得上返回 None,连不上返回原因。**只探端口,不建连接池。**"""
    parsed = urlparse(url)
    host, port = parsed.hostname or "localhost", parsed.port or default_port
    try:
        with socket.create_connection((host, port), timeout=2):
            return None
    except OSError as exc:
        return f"{host}:{port} 连不上({exc.__class__.__name__})"


def _postgres() -> str | None:
    url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "postgresql://localhost:5432/imagegen"
    )
    if not _module("sqlalchemy"):
        return "缺 sqlalchemy"
    if not _module("pytest"):
        return "缺 pytest"
    return _tcp(url, 5432)


def _redis() -> str | None:
    url = os.environ.get("CELERY_BROKER_URL") or os.environ.get(
        "REDIS_URL", "redis://localhost:6379/0"
    )
    return _tcp(url, 6379)


def _node_modules() -> str | None:
    if not shutil.which("node"):
        return "缺 node"
    if not (FRONTEND / "node_modules").is_dir():
        return "frontend/node_modules 不存在(需要联网跑 npm ci)"
    return None


def _docker() -> str | None:
    if not shutil.which("docker"):
        return "缺 docker"
    proc = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, timeout=30
    )
    return None if proc.returncode == 0 else "docker daemon 不可用"


# ---------------------------------------------------------------- 清单


@dataclass
class Item:
    """P0 的一条。`blocked_by` 返回非 None 时这一条报未验证,不跑命令。

    ## 命令可以各带各的前提(A45-batch14-6)

    `commands` 的元素可以是 `(label, cmd, cwd)`,也可以是
    `(label, cmd, cwd, needs)` —— `needs()` 返回非 None 就**只跳这一条**,
    别的照跑。

    加这一层是因为 P0-3 原来的表现有误导性:它的 `blocked_by` 是
    `_node_modules() or _docker()`,于是一台装了 node_modules、没有 docker
    的机器上,四条 npm 检查**明明跑得了也跑得过**,却和两条 docker build
    一起被报成"未验证",输出里六条全是「有前提时跑」。

    读的人由此得到的结论是"P0-3 一条都没验过",而真相是"只差两条 docker"。
    这两句话对"还要做什么"的指导完全不同 —— 而全有或全无的报告
    永远只会给出前一句。剩余项说不准,和守卫不咬人是同一类损失。
    """

    number: str
    title: str
    commands: list[tuple]
    blocked_by: object = None
    #: 离线也能确认的旁证。**不构成通过** —— 只用来区分「写了没跑」与「没写」
    evidence: list[tuple[str, object]] = field(default_factory=list)


def _db_cases_exist() -> tuple[bool, str]:
    """P0 第一条那 13 条真库用例(12-4 六条 + 12-5 七条)在不在树上。"""
    import ast

    total = 0
    missing = []
    for name in (
        "test_a45_batch12_4_recovery_db.py",
        "test_a45_batch12_5_lease_and_billing_db.py",
    ):
        path = BACKEND / "tests" / name
        if not path.exists():
            missing.append(name)
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        total += sum(
            1
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
        )
    if missing:
        return False, "缺文件:" + ", ".join(missing)
    return total == 13, f"{total} 条已写(应为 13),**均未执行**"


def _single_head() -> tuple[bool, str]:
    """迁移链单一 head,并报出 head 编号。

    **这里刻意不写 head 的编号。** 原文是「P0 第二条要跑到 0034」,而文件树
    早已到 0045 —— 它过期不会有任何东西报错,正是本文件头引用的那条规矩
    (STATUS 第五节:写死的数字会在下一次增删时过期,而过期了没有东西报错)
    在自己身上失效了一次。`AC-VERIFICATION.md` §P0-2 已把它记成「过期叙述」。

    判定本来就是**动态算的**(下面从迁移文件里推 head),从来不比对某个常量,
    所以修法是删掉那个数字而不是把它改成 0045 —— 改成 0045 只是把下一次
    过期推迟到下一个迁移。
    """
    import re

    versions = BACKEND / "migrations" / "versions"
    revs, downs = set(), set()
    for path in versions.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)", text, re.M)
        down = re.search(
            r"^down_revision(?::\s*[^=]+)?\s*=\s*[\"']([^\"']+)", text, re.M
        )
        if rev:
            revs.add(rev.group(1))
        if down:
            downs.add(down.group(1))
    heads = revs - downs
    return len(heads) == 1, f"head = {', '.join(sorted(heads)) or '(空)'}"


def _lease_cases_exist() -> tuple[bool, str]:
    """P0 第六条:两侧租约的真库用例。

    生成阶段租约在 batch12-5 那份里;**批次条目租约**在
    `test_batch_lease_concurrency_db.py` —— PRD §0.4 把后者记成「仍欠」,
    与本仓库的事实不符,详见 `HANDOVER.md`。这里如实报数。
    """
    import ast

    path = BACKEND / "tests" / "test_batch_lease_concurrency_db.py"
    if not path.exists():
        return False, "缺 test_batch_lease_concurrency_db.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    n = sum(
        1
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )
    return n > 0, f"批次条目租约双 session 用例 {n} 条(A42 跑绿过,本轮未重跑)"


def _billed_drill_exists() -> tuple[bool, str]:
    import ast

    path = BACKEND / "tests" / "test_a45_batch12_7_billed_unknown_db.py"
    if not path.exists():
        return False, "缺 test_a45_batch12_7_billed_unknown_db.py(P0 第五条要的演练)"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    n = sum(
        1
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    )
    return n > 0, f"{n} 条已写,**未执行**"


def build_items() -> list[Item]:
    py = sys.executable
    return [
        Item(
            "P0-1",
            "真库 pytest 全量跑通(含 12-3 / 12-4 / 12-5 三批)",
            [("pytest", [py, "-m", "pytest", "-q"], BACKEND)],
            blocked_by=_postgres,
            evidence=[("12-4 + 12-5 用例清单", _db_cases_exist)],
        ),
        Item(
            "P0-2",
            "Alembic 升降级在真 PostgreSQL 验证(0001 → head → 回退)",
            [
                ("alembic upgrade head", ["alembic", "upgrade", "head"], BACKEND),
                ("alembic downgrade base", ["alembic", "downgrade", "base"], BACKEND),
                ("alembic upgrade head", ["alembic", "upgrade", "head"], BACKEND),
            ],
            blocked_by=lambda: (
                "缺 alembic" if not shutil.which("alembic") else _postgres()
            ),
            evidence=[("迁移链", _single_head)],
        ),
        Item(
            "P0-3",
            "前端 typecheck / lint / Vitest / build + Docker build",
            [
                ("npm run typecheck", ["npm", "run", "typecheck"], FRONTEND),
                ("npm run lint", ["npm", "run", "lint"], FRONTEND),
                ("npm run test", ["npm", "run", "test"], FRONTEND),
                ("npm run build", ["npm", "run", "build"], FRONTEND),
                (
                    "docker build backend",
                    ["docker", "build", "-t", "swimwear-imagegen-backend:p0", "backend"],
                    ROOT,
                    _docker,
                ),
                (
                    "docker build frontend",
                    ["docker", "build", "-t", "swimwear-imagegen-frontend:p0", "frontend"],
                    ROOT,
                    _docker,
                ),
                # **`docker build` 绿不等于镜像能用。**
                #
                # 评审 B-01 那个缺陷构建是会成功的:`pip install -e` 在 app/ 还
                # 不存在时扫到空包集,不报错,只是装了个什么都没有的包。开发态
                # 有 bind mount 盖着看不出来,而生产态 `!override` 掉了那个挂载。
                #
                # 少了这一条,P0-3 会在「镜像里根本 import 不了 app」的情况下
                # 报通过 —— 而 PRD §14.3 的人工测试准入正是以这份清单为准。
                # 一条绿着的门禁说着一件不一定成立的事,是本仓记过最多次的失效。
                #
                # 同一件事在 Makefile(`images-smoke`)与 ci.yml(images job)
                # 各有一份。三处都要,因为它们失效的方式不同。
                (
                    "docker run backend import",
                    [
                        "docker", "run", "--rm", "swimwear-imagegen-backend:p0",
                        "python", "-c", "import app, app.main",
                    ],
                    ROOT,
                    _docker,
                ),
            ],
            # docker 从 `blocked_by` 挪到了那两条命令各自的前提上:
            # 没有 node_modules 时四条 npm 一条都跑不了,那才是整条卡住;
            # 只缺 docker 时四条 npm 照跑,剩余项就精确到"两条 docker build"
            blocked_by=_node_modules,
        ),
        Item(
            "P0-4",
            "关闭 R-04(坏候选图重试没有出路)与 R-05(CREATED 下的非法边)",
            [
                (
                    "纯逻辑守卫 batch12_7",
                    [py, "tools/run_pure_tests.py", "batch12_7"],
                    BACKEND,
                ),
                ("app.* import 解析", [py, "tools/verify_imports.py"], BACKEND),
            ],
        ),
        Item(
            "P0-5",
            "重复扣费残窗:BILLED_RESULT_UNKNOWN 闸 + 解除通道真库演练",
            [
                (
                    "pytest 演练",
                    [
                        py,
                        "-m",
                        "pytest",
                        "-q",
                        "tests/test_a45_batch12_7_billed_unknown_db.py",
                        "tests/test_batch_receipt_lifecycle_db.py",
                    ],
                    BACKEND,
                )
            ],
            blocked_by=_postgres,
            evidence=[("演练用例", _billed_drill_exists)],
        ),
        Item(
            "P0-6",
            "租约 fencing 与「租约过期但 worker 还活着」",
            [
                (
                    "pytest 租约",
                    [
                        py,
                        "-m",
                        "pytest",
                        "-q",
                        "tests/test_batch_lease_concurrency_db.py",
                        "tests/test_a45_batch12_5_lease_and_billing_db.py",
                    ],
                    BACKEND,
                )
            ],
            blocked_by=lambda: _postgres() or _redis(),
            evidence=[("双 session 用例", _lease_cases_exist)],
        ),
    ]


# ---------------------------------------------------------------- 执行


def _run(cmd: list[str], cwd: Path, verbose: bool) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=not verbose, text=True, timeout=3600
        )
    except FileNotFoundError:
        return False, f"命令不存在:{cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, "超时"
    if proc.returncode == 0:
        return True, ""
    tail = "" if verbose else "\n".join((proc.stdout or "").splitlines()[-15:])
    return False, tail or f"退出码 {proc.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段 P0 清单(逐条跑,跑不动的点名)")
    parser.add_argument("--verbose", action="store_true", help="把子命令输出直接透出")
    parser.add_argument("--only", help="只跑编号前缀匹配的项,例如 P0-4")
    args = parser.parse_args()

    results: list[tuple[Item, str, str]] = []
    for item in build_items():
        if args.only and not item.number.startswith(args.only):
            continue
        print(f"\n\033[1m{item.number}\033[0m  {item.title}")

        for label, probe in item.evidence:
            ok, note = probe()
            mark = "·" if ok else "!"
            print(f"    {mark} {label}:{note}")

        blocked = item.blocked_by() if item.blocked_by else None
        if blocked:
            print(f"    \033[33m{UNVERIFIED}\033[0m —— {blocked}")
            for command in item.commands:
                print(f"        有前提时跑:{command[0]}")
            results.append((item, UNVERIFIED, str(blocked)))
            continue

        detail, status = "", PASS
        skipped: list[str] = []
        for command in item.commands:
            label, cmd, cwd = command[0], command[1], command[2]
            needs = command[3] if len(command) > 3 else None
            reason = needs() if needs else None
            if reason:
                # **只跳这一条。** 跳过不是通过 —— 它把整条降级成未验证,
                # 但别的命令照跑,好让剩余项说得准是哪几条
                print(f"    \033[33m–\033[0m {label} —— {reason}")
                skipped.append(label)
                continue
            ok, tail = _run(cmd, cwd, args.verbose)
            print(f"    {'✓' if ok else '✗'} {label}")
            if not ok:
                status, detail = FAIL, f"{label}:{tail}"
                break
        if status == PASS and skipped:
            status = UNVERIFIED
            detail = f"还差 {len(skipped)} 条:{'、'.join(skipped)}"
        colour = {PASS: "32", FAIL: "31", UNVERIFIED: "33"}[status]
        print(f"    \033[{colour}m{status}\033[0m" + (f" —— {detail}" if detail else ""))
        results.append((item, status, detail))

    print("\n" + "=" * 62)
    for item, status, detail in results:
        colour = {PASS: "32", FAIL: "31", UNVERIFIED: "33"}[status]
        suffix = f"  \033[2m({detail})\033[0m" if status == UNVERIFIED and detail else ""
        print(f"  \033[{colour}m{status}\033[0m  {item.number}  {item.title}{suffix}")

    unverified = [r for r in results if r[1] == UNVERIFIED]
    failed = [r for r in results if r[1] == FAIL]
    print("=" * 62)
    if not unverified and not failed:
        print("\n\033[32m阶段 P0 全部关闭。\033[0m §14.3 的准入条件之一已满足。")
        return 0
    if failed:
        print(f"\n\033[31m{len(failed)} 项失败\033[0m —— 要改代码。")
    if unverified:
        print(
            f"\n\033[33m{len(unverified)} 项未验证\033[0m —— 不是失败,是这台机器没有前提。"
            "\n换一台有 PostgreSQL / Redis / node_modules / docker 的机器再跑一次。"
        )
    print("\n**阶段 P0 未关闭,不得冻结正式人工测试版本**(PRD §14.3)。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
