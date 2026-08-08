#!/usr/bin/env python3
"""A45-batch19(阶段 5 批次 5-1)变异验证:草稿的颜色维上游快照。

用法:python3 tools/mutate_batch19.py

## 这一批在防什么

判定层的失效方式**几乎全部是绿的**,而且方向分两种,两种都得验:

    放行侧   没快照当没变化、颜色集合比交集、schema 不比
             —— 表现是草稿永远不过期,导出的是旧事实
    误伤侧   ACTIVE 口径丢了、inactive 带上版本
             —— 表现是一个还没到样的颜色让草稿永远过期,
                运营重新生成一次它又过期,而提示说的是他还没开始做的颜色

第二种没有任何东西会报错,只会让人不再相信过期提示,然后去关掉它。
所以两个方向各要有会红的守卫,这份脚本验它们**真的咬得住** ——
一条只在今天绿着的守卫,和没有守卫是一回事。

## 锚点基准

路径基准 `backend/`,与 `tools/audit_anchors.py` 的 `MUTATIONS` 形状一致。
本批的守卫全在纯层,不读前端源码,但沙箱照样复制整个项目树:
守卫要读 `docs/STATUS.md`(欠账门禁那条的邻居)与 `migrations/`,
只复制 `backend/` 会让它们在**每一次**变异里都红,把真绿伪装成红
(batch14 那次踩过)。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

LAYER = "app/workbench/upstream_snapshot.py"
MODEL = "app/models/listing_copy.py"
MIGRATION = "migrations/versions/0049_draft_upstream_snapshot.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # -------------------------------------------------- A 组:误伤侧(§6.7 的范围口径)
    (
        "A1",
        "ACTIVE 口径丢了(PLANNED 颜色进快照,它的事实一改草稿就过期)",
        LAYER,
        "    return tuple(sorted((c for c in colors if c.is_active), key=lambda c: c.variant_id))",
        "    return tuple(sorted(colors, key=lambda c: c.variant_id))",
    ),
    (
        "A2",
        "快照不再排序(查询顺序换一次就算出一个不等的快照,草稿凭空过期)",
        LAYER,
        "    return tuple(sorted((c for c in colors if c.is_active), key=lambda c: c.variant_id))",
        "    return tuple(c for c in colors if c.is_active)",
    ),
    (
        "A3",
        "inactive 带上版本(PLANNED 颜色的事实一改,草稿就过期一次)",
        LAYER,
        "        c.variant_id: c.sellable_status\n",
        "        c.variant_id: {'status': c.sellable_status, 'facts': list(c.fact_versions)}\n",
    ),
    # -------------------------------------------------- B 组:放行侧
    (
        "B1",
        "没快照当没变化(0049 之前建的草稿全部静默放行)",
        LAYER,
        "    if not stored:\n        return [\n            StaleChange(\n"
        '                component="upstream_snapshot",\n'
        '                message="这份草稿建于上游快照落列之前,颜色维是否仍然成立无法证明",\n'
        '                action="重新生成一次草稿,新草稿会带上颜色维的版本快照",\n'
        "            )\n        ]\n",
        "    if not stored:\n        return []\n",
    ),
    (
        "B2",
        "schema 不比(形状换版之后每个轴都「没变」,存量草稿全部放行)",
        LAYER,
        '    if str(stored.get("schema") or "") != str(current.get("schema") or ""):',
        "    if False:",
    ),
    (
        "B3",
        "颜色集合比交集(「某个颜色被停用」被漏掉,它的 SKU 还在导出文件里)",
        LAYER,
        "    for variant in sorted(set(old) | set(new)):",
        "    for variant in sorted(set(old) & set(new)):",
    ),
    (
        "B4",
        "没有映射快照当通过(READY 门禁在颜色轴上静默放行)",
        LAYER,
        '        return ["草稿没有颜色→SKU→图片映射快照,无法确认每个 ACTIVE SKU 都拿到了图"]',
        "        return []",
    ),
    (
        "B5",
        "映射里已停用的颜色不再被点名(停售的颜色继续参与导出)",
        LAYER,
        '        problems.append(f"映射里还留着颜色 {label},它已经不是 ACTIVE")',
        "        pass",
    ),
    # -------------------------------------------------- C 组:§6.5 与分工
    (
        "C1",
        "主图混进附图位(同一张图出现两次,而平台那边只说「审核不通过」)",
        LAYER,
        "        (i for i in cov.owned if not i.is_primary), "
        "key=lambda i: (i.sort_order, i.media_asset_id)",
        "        cov.owned, key=lambda i: (i.sort_order, i.media_asset_id)",
    ),
    (
        "C2",
        "文案版本被比第二遍(改一次文案出两条提示,「变了几处」从此不可信)",
        LAYER,
        "    changes.extend(_shared_changes(stored, current))",
        '    if stored.get("copies") != current.get("copies"):\n'
        '        changes.append(StaleChange(component="color_set", message="文案版本变了"))\n'
        "    changes.extend(_shared_changes(stored, current))",
    ),
    (
        "C3",
        "三个轴改成自己采集(两列说的不再是同一件事,对账时才会发现)",
        LAYER,
        '        "spec_version": str(components.get("spec_version") or ""),',
        '        "spec_version": "",',
    ),
    # -------------------------------------------------- D 组:落列的两个决定
    (
        "D1",
        "迁移给了 server_default='{}'(存量草稿看起来「颜色维算过、没变化」)",
        MIGRATION,
        "            sa.Column(name, postgresql.JSONB(), nullable=True),",
        "            sa.Column(name, postgresql.JSONB(), nullable=False, server_default=\"{}\"),",
    ),
    (
        "D2",
        "ORM 侧改成非空(迁移与模型各说各话,真库第一次插入才暴露)",
        MODEL,
        "    upstream_versions: Mapped[dict[str, Any] | None] = "
        "mapped_column(JSONB, nullable=True)",
        "    upstream_versions: Mapped[dict[str, Any]] = mapped_column("
        'JSONB, nullable=False, default=dict, server_default="{}")',
    ),
]

SUITE_FILTER = "test_a45_batch19_draft_upstream_snapshot.py"


@contextmanager
def _temporary_directory():
    """建一个 Windows 受限令牌也能继续写入的临时目录(同 batch17-2 / batch18)。"""
    path = Path(tempfile.gettempdir()) / f"batch19-{uuid4().hex}"
    path.mkdir(parents=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _ignore_copy(_directory: str, names: list[str]) -> set[str]:
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
        if name in exact or name.startswith((".tmp", ".mutation", ".pytest-"))
    }


def run_suite(cwd: Path, name_filter: str) -> tuple[int, str]:
    # 不继承数据库/Redis 等业务环境变量；Windows 子进程只保留启动与临时目录
    # 所需的系统键。显式统一 UTF-8，避免 reader thread 解码失败后 stdout 变 None。
    child_env = {
        key: os.environ[key]
        for key in ("COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR")
        if key in os.environ
    }
    child_env.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
    )
    proc = subprocess.run(
        [sys.executable, "tools/run_pure_tests.py", name_filter],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        env=child_env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    red = 0
    for ident, description, rel, old, new in MUTATIONS:
        with _temporary_directory() as tmp:
            root = tmp / "project"
            shutil.copytree(PROJECT, root, symlinks=True, ignore=_ignore_copy)
            target = (root / "backend" / rel).resolve()
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                print(f"{ident:4} 锚点失准({text.count(old)} 次命中) —— 无法验证")
                continue
            target.write_text(text.replace(old, new), encoding="utf-8")
            code, _ = run_suite(root / "backend", SUITE_FILTER)
            verdict = "RED" if code != 0 else "GREEN"
            if code != 0:
                red += 1
            print(f"{ident:4} {description}  {verdict}")

    print(f"\n{red}/{len(MUTATIONS)} 条变异验红")
    return 0 if red == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
