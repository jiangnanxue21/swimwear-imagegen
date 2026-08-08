#!/usr/bin/env python3
"""A45-batch17-2 变异验证:发布投递的落库归属校验。

用法:python3 tools/mutate_batch17_2.py

## 这一批在防什么

本批关掉的缺口是「迟到的调用把较新的结论盖掉」。它的两种失效方式方向相反,
而**只有一种会被人发现**:

    校验被摘掉      缺口回来了 —— 库里看起来一切正常,只在出事那次露馅
    校验判错方向    每一次结果都落不了库 —— 这个会被立刻发现

所以变异集里两种都要有。M 组是前者(守卫必须报),N 组是后者
(守卫不必报,但它是本次修法选择令牌而不是时间戳的理由,列在这里备查)。

## 为什么不验"迟到覆写"本身

那要两个真连接,在 `tests/test_publish_lease_concurrency_db.py` 里,
需要 PostgreSQL。这份脚本验的是**离线守卫的射程**:真库那组 skip 掉的时候,
挡在缺口前面的就只剩这几条。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

SERVICE = "app/services/publish_service.py"
MODEL = "app/models/publishing.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------------ M 组:校验被摘掉
    (
        "M1",
        "把归属校验整条摘掉(恢复 P2-1 的缺口:迟到的调用无条件覆写)",
        SERVICE,
        "        if not _still_holds(session, call):\n"
        "            _record_stale_outcome(session, call, outcome, listing=listing)\n"
        "            session.commit()\n"
        "            return \"stale\"\n",
        "",
    ),
    (
        "M2",
        "校验还在,但结果不再决定去留(算出来没人读)",
        SERVICE,
        "        if not _still_holds(session, call):",
        "        _still_holds(session, call)\n        if False:",
    ),
    (
        "M3",
        "判据退回 `lease_until`(评审给的\"最小修法\",在本仓会让正常路径也落不了库)",
        SERVICE,
        "                PublishOutbox.lease_token == call.lease_token,\n"
        "            )\n"
        "            .with_for_update()",
        "                PublishOutbox.lease_until == call.lease_until,\n"
        "            )\n"
        "            .with_for_update()",
    ),
    (
        "M4",
        "确认路径忘了领令牌(它的结论从此永远落不了库,而界面纹丝不动)",
        SERVICE,
        "            lease_token=row.lease_token,\n",
        "",
    ),
    (
        "M5",
        "落 DONE 时不吊销令牌(还拿着旧令牌的执行体仍然写得进来)",
        SERVICE,
        "        row.lease_token = None\n"
        "        row.next_attempt_at = None\n"
        "        row.last_error = None\n",
        "        row.next_attempt_at = None\n"
        "        row.last_error = None\n",
    ),
    (
        "M6",
        "人工重投不吊销令牌(还在飞的确认回来后盖掉运营刚做的决定)",
        SERVICE,
        "    # 与批次回收器 `reap_expired_leases` 吊销令牌是同一个动作\n"
        "    row.lease_token = None\n",
        "",
    ),
    (
        "M7",
        "stale 结论只打日志不进审计(这次调用拿到了什么,从此没有落脚点)",
        SERVICE,
        "    audit.record(\n"
        "        session,\n"
        "        actor=\"system\",\n"
        "        action=AuditAction.UPDATE,\n"
        "        entity_type=\"PublishOutbox\",\n"
        "        entity_id=call.outbox_id,",
        "    _ = (\n"
        "        session,\n"
        "        \"system\",\n"
        "        AuditAction.UPDATE,\n"
        "        \"PublishOutbox\",\n"
        "        call.outbox_id,",
    ),
    (
        "M8",
        "续租判据与落库判据分叉(两处口径必须只有一份)",
        SERVICE,
        "                PublishOutbox.status == PublishOutboxStatus.LEASED.value,\n"
        "                PublishOutbox.lease_token == call.lease_token,\n"
        "            )\n"
        "            .values(lease_until=moment",
        "                PublishOutbox.status == PublishOutboxStatus.LEASED.value,\n"
        "                PublishOutbox.lease_until == call.lease_until,\n"
        "            )\n"
        "            .values(lease_until=moment",
    ),
    (
        "M9",
        "模型上那一列被删掉(ORM 建的测试库有、alembic 建的生产库没有)",
        MODEL,
        "    lease_token: Mapped[UUID | None] = mapped_column(\n"
        "        PGUUID(as_uuid=True), nullable=True\n"
        "    )\n",
        "",
    ),
    (
        "M10",
        "续租不再显式拒绝空令牌(SQLAlchemy 会把比较编译成 IS NULL)",
        SERVICE,
        "    if call.lease_token is None:\n"
        "        return False\n"
        "    moment = now or _now()\n",
        "    moment = now or _now()\n",
    ),
    (
        "M11",
        "落库校验不再显式拒绝空令牌(存量无主行会被 IS NULL 命中)",
        SERVICE,
        "    if call.lease_token is None:\n"
        "        return False\n"
        "    return (\n",
        "    return (\n",
    ),
]

SUITE_FILTER = "test_a45_batch17_2_publish_fencing.py"


@contextmanager
def _temporary_directory():
    """建一个 Windows 受限令牌也能继续写入的临时目录。

    Python 3.12 在 Windows 上会把 ``TemporaryDirectory`` 建成 0o700；
    沙箱的受限令牌随后不能在其中创建 ``project``。普通 ``mkdir`` 使用
    继承 ACL，Linux 上语义不变，退出时仍由本函数清理。
    """
    path = Path(tempfile.gettempdir()) / f"batch17-2-{uuid4().hex}"
    path.mkdir(parents=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _ignore_copy(_directory: str, names: list[str]) -> set[str]:
    """排除运行产物与本脚本自己的临时树。

    变异只需要源码；复制 `.venv`、pytest/import-linter 缓存或历史 `.tmp*`
    不但浪费时间，Windows 上还会撞到仅创建进程可读的 0o700 临时目录。
    """
    exact = {
        ".git",
        ".venv",
        ".pytest_cache",
        ".ruff_cache",
        ".import_linter_cache",
        "node_modules",
        "__pycache__",
        "dist",
    }
    return {
        name
        for name in names
        if name in exact or name.startswith((".tmp", ".mutation"))
    }


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
        with _temporary_directory() as tmp:
            root = tmp / "project"
            shutil.copytree(
                PROJECT,
                root,
                symlinks=True,
                ignore=_ignore_copy,
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
