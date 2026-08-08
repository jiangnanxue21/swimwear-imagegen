#!/usr/bin/env python3
"""A45-batch21(阶段 5 批次 5-3)变异验证:文案生成的幂等单元。

用法:python3 tools/mutate_batch21.py

## 这一批在防什么

幂等的失效方式分两个方向,而**两个方向都是绿的**:

    松掉   键按尺码分裂、判定被绕过、键不落库
           —— 表现是 S/M/L 三行又各花一次钱。台账上多出来的调用没人对得上
    紧过头 失败态被复用、事实变了还复用、force 被吃掉
           —— 表现是"点生成没反应",而运营会以为是页面卡了

第二个方向尤其要验:只钉「复用生效」的话,最省事的实现是"有就返回",
而那样文案会永远停在第一次生成的那一版。

## 两个运行器

    pure  键的成分与判定口径、接线顺序、迁移形状
    db    真的少落了两个版本、少建了两个内容计划

db 那一半需要 `TEST_DATABASE_URL` 与 `ALLOW_DESTRUCTIVE_TEST_DB=1`,
没配就明确跳过并返回非零(同 batch20)。

## 锚点基准

路径基准 `backend/`,表名 `RUNNER_MUTATIONS`(6 列),形状注册在
`tools/audit_anchors.py` 的 `SHAPES` 里 —— 与 batch20 同一张形状。
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

LAYER = "app/workflows/copy_idempotency.py"
SERVICE = "app/workbench/service.py"
COPY_SERVICE = "app/listings/copy_service.py"
MIGRATION = "migrations/versions/0050_copy_idempotency_key.py"

PURE_SUITE = "test_a45_batch21_copy_idempotency.py"
DB_SUITE = "tests/test_a45_batch21_copy_idempotency_db.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成, 运行器)
RUNNER_MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    # ------------------------------------------------ A 组:松掉(钱又花出去了)
    (
        "A1",
        "幂等判定被绕过(每次都生成,S/M/L 三行又各花一次钱)",
        SERVICE,
        "    if not copy_idempotency.spends_money(verdict):",
        "    if False:",
        "db",
    ),
    (
        "A2",
        "键不落库(下一次必然不命中,幂等永远为空转)",
        SERVICE,
        "        idempotency_key=unit.key(),\n        actor=actor,\n    )",
        "        actor=actor,\n    )",
        "db",
    ),
    (
        "A3",
        "SKU 进了单元(幂等按尺码分裂,回到三次付费)",
        LAYER,
        "    #: 颜色维文案启用时才有值。None = SPU 级文案(今天的唯一形态)\n"
        "    color_variant_id: str | None = None",
        "    #: 颜色维文案启用时才有值。None = SPU 级文案(今天的唯一形态)\n"
        "    color_variant_id: str | None = None\n"
        '    sku: str = ""',
        "pure",
    ),
    (
        "A4",
        "事实集合按列表算(换个 ORDER BY 就算出新键,凭空重新付费)",
        LAYER,
        '        facts = ",".join(sorted({str(v) for v in self.fact_versions if str(v).strip()}))',
        '        facts = ",".join(str(v) for v in self.fact_versions)',
        "pure",
    ),
    (
        "A5",
        "内容计划建在判定之前(复用路径攒垃圾行,账面上省不下来)",
        SERVICE,
        "    unit = _copy_unit(session, product)",
        "    _early_plan = _content_plan_or_raise(session, product, actor=actor)\n"
        "    unit = _copy_unit(session, product)",
        "db",
    ),
    (
        "A6",
        "并发不按单元串行(两个并发请求各调一次生成器)",
        SERVICE,
        '    if not locks.try_advisory_xact_lock(session, "listing_copy_unit", unit.key()):',
        '    if not locks.try_advisory_xact_lock(session, "listing_copy_product", str(product.id)):',
        "pure",
    ),
    # ------------------------------------------------ B 组:紧过头(点了没反应)
    (
        "B1",
        "失败态也复用(这个 SPU 的文案永远修不好)",
        LAYER,
        'REUSABLE_STATUSES: frozenset[str] = frozenset({"DRAFT", "VALIDATED", "APPROVED"})',
        'REUSABLE_STATUSES: frozenset[str] = frozenset(\n'
        '    {"DRAFT", "VALIDATED", "APPROVED", "REJECTED"}\n'
        ")",
        "db",
    ),
    (
        "B2",
        "事实变了还复用(确认了新属性,拿回来的还是旧文案)",
        LAYER,
        "    if not existing_key or existing_key != wanted_key:\n        return CopyUnitVerdict.GENERATE",
        "    if not existing_key:\n        return CopyUnitVerdict.GENERATE",
        "db",
    ),
    (
        "B3",
        "force 被复用吃掉(单字段重生变成「点了没反应」)",
        LAYER,
        "    if force:\n        return CopyUnitVerdict.FORCED",
        "    if False:\n        return CopyUnitVerdict.FORCED",
        "db",
    ),
    (
        "B4",
        "存量 NULL 判成复用(拿一版按早就变过的事实生成的文案顶上)",
        LAYER,
        "    if not existing_key or existing_key != wanted_key:",
        "    if existing_key and existing_key != wanted_key:",
        "pure",
    ),
    (
        "B5",
        "spends_money 按「不等于 GENERATE」判(新增取值默认算成花钱的)",
        LAYER,
        "    return verdict is not CopyUnitVerdict.REUSE",
        "    return verdict is CopyUnitVerdict.GENERATE",
        "pure",
    ),
    # ------------------------------------------------ C 组:落库与迁移
    (
        "C1",
        "ListingCopy 构造点丢掉键(列在、判定读着、没有写入路径)",
        COPY_SERVICE,
        "                    idempotency_key=idempotency_key,",
        "                    spec_version=rules.spec_version,",
        "pure",
    ),
    (
        "C2",
        "键上加唯一索引(REJECTED 那一版永久占住它,文案再也生成不出来)",
        MIGRATION,
        '    op.create_index(_INDEX, _TABLE, ["spu", "channel", "site", "locale", _COLUMN])',
        '    op.create_index(\n'
        '        _INDEX, _TABLE, ["spu", "channel", "site", "locale", _COLUMN], unique=True\n'
        "    )",
        "pure",
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
