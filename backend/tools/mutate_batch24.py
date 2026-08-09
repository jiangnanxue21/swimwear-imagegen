#!/usr/bin/env python3
"""A45-batch24 变异验证:阶段 5 三处接缝的守卫会不会咬。

用法:python3 tools/mutate_batch24.py

## 这一批在防什么

三条缺口的共同形状是**接了一半**:每一条都有真库用例覆盖着接上的那一半,
于是套件全绿而另一半在生产里持续出错(证据链见 `docs/DECISIONS.md` §3.54)。
所以这里的变异不是"把功能改坏",而是**把它改回缺口当时的样子** ——
每一条都对应一个真实发生过、且三位外部评审各自独立复现过的形态。

    F-1    投影搬回 `confirm()`(只有人工确认那条路投影)
           状态门拓宽(SUGGESTED 的猜测也写进正式颜色名)
    F-6    存储回到 SPU 一个槽(颜色轴上来回点每次都付费)
           版本序列不按颜色分(两个颜色共用一条递增序列)
           批量去重键回到 `spu`(三颜色 SPU 只出一稿)
    AC-19  行级图片自己按 sort_order 挑(预览与导出分叉)
           `image_map` 给个默认值(漏传的调用点静默走老路)
           `build_draft` 里映射排在映射字段之后(拿不到映射)

## 两个运行器

    pure  接线形状、粒度一致性、签名不给后门
    db    行为:自动确认真的写了显示名、来回点只花两次钱、两边取到同一张图

## 锚点基准

路径基准 `backend/`,表名 `RUNNER_MUTATIONS`(6 列),与 batch20~23 同形状。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

ATTR = "app/attributes/service.py"
WB = "app/workbench/service.py"
COPY = "app/listings/copy_service.py"
CHANNEL = "app/channels/generic/__init__.py"
BATCH = "app/workbench/batch.py"

PURE_SUITE = "test_a45_batch24_stage5_seams.py"
DB_SUITE = "tests/test_a45_batch24_stage5_seams_db.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成, 运行器)
RUNNER_MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    # ---- F-1:投影的触发边界 ----
    (
        "A1",
        "投影搬回 confirm(自动确认出来的颜色又恒空)",
        ATTR,
        "        _project_colour_name(session, field_name=field_name, owner_id=owner_id, value=value)",
        "        pass",
        "db",
    ),
    (
        "A2",
        "状态门被拓宽(SUGGESTED 的模型猜测也写进正式颜色名)",
        ATTR,
        "    if status is AttributeStatus.CONFIRMED:",
        "    if status is not None:",
        "pure",
    ),
    (
        "A3",
        "apply_evidence 不再经过写入边界(自动确认那条路自己写行)",
        ATTR,
        "def apply_evidence(",
        "def apply_evidence_unused(",
        "pure",
    ),
    # ---- F-6:文案的颜色粒度 ----
    (
        "B1",
        "当前文案不按颜色查(颜色轴上来回点,每次都重新付费)",
        WB,
        "                ListingCopy.color_variant_id == color_variant_id,",
        "",
        "db",
    ),
    (
        "B2",
        "版本序列不按颜色分槽(两个颜色共用一条递增序列)",
        COPY,
        "            ListingCopy.color_variant_id == color_variant_id,\n        )\n    ).scalar_one_or_none()",
        "        )\n    ).scalar_one_or_none()",
        "db",
    ),
    (
        "B3",
        "幂等单元的颜色轴又恒为 None(留位置当成接线)",
        WB,
        "        fact_versions=_confirmed_version_ids(session, product, attr_values),\n        color_variant_id=variants.variant_id_for(product),",
        "        fact_versions=_confirmed_version_ids(session, product, attr_values),\n        color_variant_id=None,",
        "pure",
    ),
    (
        "B4",
        "批量去重回到 SPU 粒度(三颜色 SPU 只出一稿文案)",
        BATCH,
        'BatchAction.GENERATE_COPY: "spu_colour",',
        'BatchAction.GENERATE_COPY: "spu",',
        "pure",
    ),
    (
        "B5",
        "按颜色查时回落到哨兵槽(把 0051 之前那版顶给某个颜色)",
        WB,
        "                ListingCopy.color_variant_id == color_variant_id,\n                ListingCopy.status != CopyStatus.ARCHIVED.value,",
        "                ListingCopy.color_variant_id.in_((color_variant_id, \"\")),\n                ListingCopy.status != CopyStatus.ARCHIVED.value,",
        "db",
    ),
    # ---- AC-19:预览与导出同源 ----
    (
        "C1",
        "行级图片自己按 sort_order 挑(预览显示 A 图、Excel 用 B 图)",
        CHANNEL,
        "            images=_row_image_bucket(\n                draft, image_map, variant_id=sku.variant_id, sku=sku.sku\n            ),",
        "            images=_header_image_bucket(draft),",
        "db",
    ),
    (
        "C2",
        "image_map 给个默认值(漏传的调用点静默走老路)",
        CHANNEL,
        "    spec: ChannelFieldSpec,\n    *,\n    image_map: Mapping[str, Any],",
        "    spec: ChannelFieldSpec,\n    *,\n    image_map: Mapping[str, Any] = {},",
        "pure",
    ),
    (
        "C3",
        "映射算在 map_fields 之后(它拿不到映射,只能自己挑)",
        WB,
        "    mapped = generic.map_fields(data, spec, image_map=color_sku_image_map)",
        "    mapped = generic.map_fields(data, spec, image_map={})",
        "db",
    ),
    (
        "C4",
        "预览重新推断(读当前上游而不是落库那一列)",
        WB,
        'preview["images"] = export_preview.image_preview(row.color_sku_image_map)',
        'preview["images"] = export_preview.image_preview(row.color_sku_image_map or {})\n    _ = upstream_collect',
        "pure",
    ),
]


@contextmanager
def _temporary_directory():
    path = Path(tempfile.gettempdir()) / f"batch24-{uuid4().hex}"
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
    out = (proc.stdout or "") + (proc.stderr or "")
    # **空结果不能读作通过**(A45-batch24)。
    #
    # `requires_db` 在库连不上时**跳过**整份用例,pytest 于是退出 0 ——
    # 而本脚本把 0 读成 GREEN,也就是"这条变异没被守卫咬住"。真实发生过:
    # 容器里的 PostgreSQL 中途挂掉,一轮变异从 13/13 变成 6/13,报告上
    # 是七条守卫失效,实际是七条根本没跑。**失效与没跑必须分开报**,
    # 否则这个脚本会在最需要它的时候给出最让人放心的数字。
    if " no tests ran" in out or re.search(r"\b\d+ skipped\b", out) and " passed" not in out:
        return None, out
    return proc.returncode, out


def main() -> int:
    have_db = bool(os.environ.get("TEST_DATABASE_URL"))
    if not have_db:
        print(
            "没有 TEST_DATABASE_URL —— 需要真库的那些变异**不会被验证**,"
            "本次退出码非零。\n"
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
                _, _, suite = runner.partition(":")
                code, _ = run_pure(root / "backend", suite or PURE_SUITE)
            if code is None:
                print(f"{ident:4} {description}  没跑(真库不可用)—— 不计入")
                skipped += 1
                continue
            verdict = "RED" if code != 0 else "GREEN"
            if code != 0:
                red += 1
            print(f"{ident:4} {description}  {verdict}")

    print(
        f"\n{red}/{len(RUNNER_MUTATIONS)} 条变异验红"
        + (f",{skipped} 条没跑" if skipped else "")
    )
    return 0 if red == len(RUNNER_MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
