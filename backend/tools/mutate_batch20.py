#!/usr/bin/env python3
"""A45-batch20(阶段 5 批次 5-2)变异验证:草稿颜色维的**接线**。

用法:python3 tools/mutate_batch20.py

## 这一批在防什么,以及为什么它和 batch19 防的不是一回事

batch19 验的是判定层算得对不对。**本批验的是有没有人调它** ——
§3.43 点过名的那一类:纯层全绿、门禁全绿,而功能整条不通。

接线的失效方式几乎全部是**绿的**,而且绝大多数偏向放行:

    写入侧漏掉    两列恒为 NULL,而 NULL 的语义是「没算过」——
                  于是每一份新草稿都被判成不可证明。这一种会吵,不危险
    读取侧漏掉    快照一直落库、没有任何人比较它。新增一个 ACTIVE 颜色
                  之后草稿说自己没过期,导出文件里没有那个颜色的 SKU 行,
                  **而没有任何东西会报错**。这一种最贵
    门禁侧漏掉    `ready_problems` 换成只调一半(只查 SKU 或只查图片集),
                  少的那一半静默失效
    取数侧走偏    `sellable_status` 读错、身份口径回落到颜色名、
                  样品指纹自己拼一遍 —— 表现全部是"提示说的颜色不对"

## 两个运行器

守卫分两处,所以变异也要分两处跑:

    pure   `tests/pure/test_a45_batch19_draft_upstream_snapshot.py` 的接线守卫
           (5-1 那两条欠账守卫翻转来的)+ 判定层自己的形状
    db     `tests/test_a45_batch20_draft_color_axis_db.py` —— 真库,
           因为「`build_draft` 有没有把违规拼进那个列表」纯层看不见

db 那一半需要 `TEST_DATABASE_URL` 与 `ALLOW_DESTRUCTIVE_TEST_DB=1`。
**没配就明确跳过并计入失败**,不静默当成通过 —— 一个"没跑但报绿"的
变异脚本比没有脚本更糟,那正是 §3.31 那条在说的事。

## 锚点基准

路径基准 `backend/`,与 `tools/audit_anchors.py` 的 `MUTATIONS` 形状一致。
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

SERVICE = "app/workbench/service.py"
COLLECT = "app/workbench/upstream_collect.py"
LAYER = "app/workbench/upstream_snapshot.py"

PURE_SUITE = "test_a45_batch19_draft_upstream_snapshot.py"
DB_SUITE = "tests/test_a45_batch20_draft_color_axis_db.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成, 运行器)
#:
#: 表名是 `RUNNER_MUTATIONS` 而不是 `MUTATIONS`:多了「运行器」这一列,
#: 而 `tools/audit_anchors.py` 的 `SHAPES` 按表名认宽度。沿用旧名会让
#: 审计按 5 列读、脚本按 6 列解包,两边对同一张表的宽度理解不同。
RUNNER_MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    # ---------------------------------------------- A 组:读取侧(最贵的那一种)
    (
        "A1",
        "refresh_draft 不再算颜色轴(新增 ACTIVE 颜色之后草稿说自己没过期)",
        SERVICE,
        "    color_changes = _color_axis_changes(session, product, row, current, image_set_row)",
        "    color_changes = []",
        "db",
    ),
    (
        "A2",
        "颜色轴算了但不并进结论(提示里一条颜色维的话都没有)",
        SERVICE,
        "    if not is_stale and not color_changes:\n        return False, []",
        "    if not is_stale:\n        return False, []",
        "db",
    ),
    (
        "A3",
        "存量草稿的 NULL 被当成空快照(0049 之前的草稿全部静默放行)",
        LAYER,
        "    if not stored:\n        return [\n            StaleChange(",
        "    if not stored:\n        return []\n    if False:\n        return [\n            StaleChange(",
        "db",
    ),
    # ---------------------------------------------- B 组:写入侧
    (
        "B1",
        "upstream_versions 不再落库(每份新草稿都被判成颜色维没算过)",
        SERVICE,
        "    row.upstream_versions = upstream_snapshot.build_upstream_versions(",
        "    _unused_upstream = upstream_snapshot.build_upstream_versions(",
        "pure",
    ),
    (
        "B2",
        "color_sku_image_map 不再落库(映射快照恒为 NULL)",
        SERVICE,
        "    row.color_sku_image_map = color_sku_image_map",
        "    pass  # row.color_sku_image_map = color_sku_image_map",
        "pure",
    ),
    # ---------------------------------------------- C 组:门禁侧(合取被拆成单侧)
    (
        "C1",
        "READY 只查 SKU 映射,不查 §6.5 图片集规则(缺图的颜色顺利放行)",
        LAYER,
        "    return [\n        *(f\"{problem.code.value}: {problem.message}\" for problem in violations),\n        *map_problems(mapping, colors=kept),\n    ]",
        "    return [*map_problems(mapping, colors=kept)]",
        "db",
    ),
    (
        "C2",
        "READY 只查图片集规则,不查 SKU 映射(新加的尺码永远不在映射里也放行)",
        LAYER,
        "    return [\n        *(f\"{problem.code.value}: {problem.message}\" for problem in violations),\n        *map_problems(mapping, colors=kept),\n    ]",
        "    return [f\"{problem.code.value}: {problem.message}\" for problem in violations]",
        "db",
    ),
    (
        "C3",
        "颜色维问题降级成 warning(草稿照样 READY,平台那边才发现)",
        SERVICE,
        '            level="error",\n        )\n        for problem in upstream_snapshot.ready_problems(',
        '            level="warning",\n        )\n        for problem in upstream_snapshot.ready_problems(',
        "db",
    ),
    (
        "C4",
        "门禁读现算的映射而不是落库的那一份(它永远自洽,等于没有门禁)",
        SERVICE,
        "        *_color_ready_violations(color_sku_image_map, inputs),",
        "        *_color_ready_violations({'schema': '1', 'colors': {}}, inputs),",
        "db",
    ),
    # ---------------------------------------------- D 组:取数侧
    (
        "D1",
        "sellable_status 没读库,恒当成 ACTIVE(PLANNED 颜色让草稿永远过期)",
        COLLECT,
        "            sellable_status=row.sellable_status,",
        '            sellable_status="ACTIVE",',
        "db",
    ),
    (
        "D2",
        "身份回落到颜色名(与图片标签、属性 owner_id 分叉,每个颜色都报缺 SKU)",
        COLLECT,
        "            variant_id=str(row.id),",
        "            variant_id=row.variant_code,",
        "db",
    ),
    (
        "D3",
        "SKU 全集只取第一个(同颜色其余尺码永远不在映射里)",
        COLLECT,
        "    return {variant: tuple(sorted(codes)) for variant, codes in out.items()}",
        "    return {variant: tuple(sorted(codes))[:1] for variant, codes in out.items()}",
        "db",
    ),
    (
        "D4",
        "从 spu 字符串码反查 SPU 行(§3.39 禁掉的形状,静默指向另一个款)",
        COLLECT,
        "    if product.spu_id is None:",
        "    from app.models.spu import Spu\n"
        "    product.spu_id = (\n"
        "        session.scalar(select(Spu).where(Spu.spu_code == product.spu)) or product\n"
        "    ).id\n"
        "    if product.spu_id is None:",
        "pure:test_a45_batch14_26_decisions.py",
    ),
    # ---------------------------------------------- E 组:分工(每个轴只有一个人报)
    (
        "E1",
        "图片集轴被搬回颜色维比较(重批一次图片集出 N+1 条重复提示)",
        LAYER,
        '    # `image_set` 落在快照里但**不在这里比**(模块文档第四项)。\n'
        "    # 图片集按 SPU 批准,`diff_components` 已经报过那一次换版了。\n"
        "    return out",
        '    if before.get("image_set") != after.get("image_set"):\n'
        "        out.append(\n"
        "            StaleChange(\n"
        '                component="color_image_set",\n'
        '                message=f"颜色 {label} 的图片集换了版本",\n'
        "                fields=(variant,),\n"
        "            )\n"
        "        )\n"
        "    return out",
        "pure",
    ),
    (
        "E2",
        "图片集干脆不落进快照(AC-19 要的「可解释的上游版本引用」没了)",
        COLLECT,
        "            image_set_id=None if image_set_row is None else str(image_set_row.id),",
        "            image_set_id=None,",
        "db",
    ),
]

SUITE_FILTER = PURE_SUITE


@contextmanager
def _temporary_directory():
    """建一个 Windows 受限令牌也能继续写入的临时目录(同 batch17-2 / batch18 / 19)。"""
    path = Path(tempfile.gettempdir()) / f"batch20-{uuid4().hex}"
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


def _base_env() -> dict[str, str]:
    child_env = {
        key: os.environ[key]
        for key in ("COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR", "HOME")
        if key in os.environ
    }
    child_env.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONIOENCODING="utf-8",
        PYTHONUTF8="1",
    )
    return child_env


def run_pure(cwd: Path, suite: str = PURE_SUITE) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "tools/run_pure_tests.py", suite],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        env=_base_env(),
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_db(cwd: Path) -> tuple[int, str]:
    env = _base_env()
    env["TEST_DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    env["ALLOW_DESTRUCTIVE_TEST_DB"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", DB_SUITE],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    have_db = bool(os.environ.get("TEST_DATABASE_URL"))
    if not have_db:
        print(
            "没有 TEST_DATABASE_URL —— 需要真库的那些变异**不会被验证**,"
            "本次退出码非零。\n"
            "    export TEST_DATABASE_URL=postgresql+psycopg://.../xxx_test\n"
            "    export ALLOW_DESTRUCTIVE_TEST_DB=1\n"
        )

    red = 0
    skipped = 0
    for ident, description, rel, old, new, runner in RUNNER_MUTATIONS:
        if runner.startswith("db") and not have_db:
            print(f"{ident:4} {description}  SKIP(无真库)")
            skipped += 1
            continue
        with _temporary_directory() as tmp:
            root = tmp / "project"
            shutil.copytree(PROJECT, root, symlinks=True, ignore=_ignore_copy)
            target = (root / "backend" / rel).resolve()
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                print(f"{ident:4} 锚点失准({text.count(old)} 次命中) —— 无法验证")
                continue
            target.write_text(text.replace(old, new), encoding="utf-8")
            if runner == "db":
                code, _ = run_db(root / "backend")
            else:
                # `pure:<文件名>` 把变异指向**拥有那条规矩的**套件。
                # D4 就是这样的一条:「不许从 `spu` 码反查 SPU 行」的守卫
                # 长在 `test_a45_batch14_26_decisions.py` 上(§3.39 是它的账),
                # 在本批的套件里再抄一份守卫,等于给同一条规矩造第二个判定点。
                _, _, suite = runner.partition(":")
                code, _ = run_pure(root / "backend", suite or PURE_SUITE)
            verdict = "RED" if code != 0 else "GREEN"
            if code != 0:
                red += 1
            print(f"{ident:4} {description}  {verdict}")

    print(f"\n{red}/{len(RUNNER_MUTATIONS)} 条变异验红" + (f",{skipped} 条没跑" if skipped else ""))
    return 0 if red == len(RUNNER_MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
