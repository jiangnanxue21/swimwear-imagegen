#!/usr/bin/env python3
"""A45-batch22(阶段 5 批次 5-5)变异验证:导出预览的颜色→SKU→图片。

用法:python3 tools/mutate_batch22.py

## 这一批在防什么

AC-19 的后半句是「预览与导出读**同一份已存映射**,不在预览时重新推断」。
它的失效方式几乎全部是绿的,而且**最危险的那一种看起来像是改进**:

    重新推断   预览显示"当前该用哪些图",导出写的是草稿存下来的那些。
               预览更"准"了,而两者不再是一回事 —— 运营在预览里看到红色
               有主图、导出出来那一行是空的,两边都不报错
    跳过缺图   那一行从预览里消失,运营读成"这个尺码不在这次导出里"
    合并状态   UNPROVEN 与 INCOMPATIBLE 合成一句"没有图片信息",
               而后者重新生成一百次也不会变好
    不接线     算好了没人看(§3.43)

## 两个运行器

    pure  形状、只读性(反向断言)、接线
    db    上游真的变过之后预览仍然显示存下来的那一张

第二个非真库不可:两边同源时怎么写都对,只有**分叉之后**才检验得出来。

## 锚点基准

路径基准 `backend/`,表名 `RUNNER_MUTATIONS`(6 列),与 batch20/21 同一张形状。
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

LAYER = "app/listings/export_preview.py"
SERVICE = "app/workbench/service.py"

PURE_SUITE = "test_a45_batch22_export_preview.py"
DB_SUITE = "tests/test_a45_batch22_export_preview_db.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成, 运行器)
RUNNER_MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    # ------------------------------------------------ A 组:重新推断(最危险)
    (
        "A1",
        "预览多收一个能重新推断的入参(预览更准了,而和导出不再是一回事)",
        LAYER,
        "def image_preview(mapping: Mapping[str, Any] | None) -> dict[str, Any]:",
        "def image_preview(\n"
        "    mapping: Mapping[str, Any] | None, *, image_set: Any = None\n"
        ") -> dict[str, Any]:",
        "pure",
    ),
    (
        "A2",
        "接线改成读现算的映射,不读落库那一列(预览永远自洽,等于没有预览)",
        SERVICE,
        "    preview[\"images\"] = export_preview.image_preview(row.color_sku_image_map)",
        "    preview[\"images\"] = export_preview.image_preview({\"schema\": \"1\", \"colors\": {}})",
        "db",
    ),
    (
        "A3",
        "接线整个摘掉(算好了没人看,§3.43 那一类)",
        SERVICE,
        "    preview[\"images\"] = export_preview.image_preview(row.color_sku_image_map)",
        "    pass",
        "pure",
    ),
    # ------------------------------------------------ B 组:缺图与状态
    (
        "B1",
        "缺主图的行被跳过(运营读成「这个尺码不在这次导出里」)",
        LAYER,
        "        rows = [\n            _row(sku, dict(cell or {}))",
        "        rows = [\n            _row(sku, dict(cell or {}))\n            if (cell or {}).get(\"primary\") else None",
        "pure",
    ),
    (
        "B2",
        "缺图判成 OK(READY 门禁之外再没有人说得出是哪一行缺)",
        LAYER,
        "        \"status\": ROW_OK if primary else ROW_MISSING,",
        "        \"status\": ROW_OK,",
        "pure",
    ),
    (
        "B3",
        "UNPROVEN 与 INCOMPATIBLE 合成一档(重新生成一百次也不会变好的那一种被掩盖)",
        LAYER,
        "            \"status\": MAP_INCOMPATIBLE,",
        "            \"status\": MAP_UNPROVEN,",
        "pure",
    ),
    (
        "B4",
        "存量 NULL 铺成空表格(被读成「这个 SPU 没有图」而不是「没算过」)",
        LAYER,
        "    if mapping is None:\n        return {\n            \"status\": MAP_UNPROVEN,",
        "    if mapping is None:\n        return {\n            \"status\": MAP_READY,",
        "db",
    ),
    # ------------------------------------------------ C 组:形状
    (
        "C1",
        "行序不排序(同一份草稿刷新两次行序不同,运营以为数据变了)",
        LAYER,
        "            for sku, cell in sorted(\n                (str(k), v) for k, v in (payload.get(\"skus\") or {}).items()\n            )",
        "            for sku, cell in ((str(k), v) for k, v in (payload.get(\"skus\") or {}).items())",
        "pure",
    ),
    (
        "C2",
        "颜色没有码时显示成空(每一个未命名颜色在界面上都是空白)",
        LAYER,
        "                \"variant_code\": payload.get(\"variant_code\") or f\"{variant_id[:8]}…\",",
        "                \"variant_code\": payload.get(\"variant_code\") or \"\",",
        "pure",
    ),
    (
        "C3",
        "字段预览被图片那一段挤掉(三个消费者同时空掉)",
        SERVICE,
        "    preview[\"images\"] = export_preview.image_preview(row.color_sku_image_map)\n    return preview",
        "    return {\"images\": export_preview.image_preview(row.color_sku_image_map)}",
        "db",
    ),
]

@contextmanager
def _temporary_directory():
    path = Path(tempfile.gettempdir()) / f"batch21-{uuid4().hex}"
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
