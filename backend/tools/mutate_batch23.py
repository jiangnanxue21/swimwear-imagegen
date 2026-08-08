#!/usr/bin/env python3
"""A45-batch23(阶段 5 批次 5-4)变异验证:颜色结构化字段的确认流与投影。

用法:python3 tools/mutate_batch23.py

## 这一批在防什么

`ColorVariant.display_name` 恒空躲了几批,而它躲过去的方式是**一个不存在的
字段名**(`standard_color_name`)。接上之后的失效方式分两类:

    不写   触发字段写错、投影被摘掉、赋值那一行被删
           —— 表现是那一列重新变回恒空,而后端 5 处读、前端 7 处显示照旧
    乱写   空值擦掉已有名字、owner_id 算错把同 SPU 每个颜色写成同一个名字、
           同值也写导致 updated_at 每次都动
           —— 第二种最贵:界面上"都有名字了",而每个颜色的名字都是错的

## 两个运行器

    pure  投影口径、触发口径、写入点唯一性、台账已还
    db    那一列真的有值了,而且只有该写的那一行被写

## 锚点基准

路径基准 `backend/`,表名 `RUNNER_MUTATIONS`(6 列),与 batch20/21/22 同形状。
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

LAYER = "app/attributes/colour_projection.py"
SERVICE = "app/attributes/service.py"
LEDGER = "tools/audit_column_writers.py"

PURE_SUITE = "test_a45_batch23_colour_projection.py"
DB_SUITE = "tests/test_a45_batch23_colour_projection_db.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成, 运行器)
RUNNER_MUTATIONS: list[tuple[str, str, str, str, str, str]] = [
    # ------------------------------------------------ A 组:不写(这一列重新恒空)
    (
        "A1",
        "投影调用被摘掉(display_name 重新恒空,而 5 处读、7 处显示照旧)",
        SERVICE,
        "    _project_colour_name(session, field_name=field_name, owner_id=owner_id, value=value)",
        "    pass",
        "db",
    ),
    (
        "A2",
        "触发字段写回那个不存在的名字(投影永远不触发)",
        LAYER,
        'SOURCE_FIELD = "primary_color"',
        'SOURCE_FIELD = "standard_color_name"',
        "pure",
    ),
    (
        "A3",
        "赋值那一行被删掉(算了但不写)",
        SERVICE,
        "        row.display_name = wanted\n        session.flush()",
        "        pass",
        "db",
    ),
    # ------------------------------------------------ B 组:乱写(名字错了而界面正常)
    (
        "B1",
        "空值写成空串(把界面上正在显示的颜色名静默擦掉)",
        LAYER,
        "    text = _text_of(value)\n    if not text:\n        return None\n    return text[:MAX_LENGTH]",
        "    return str(_text_of(value))[:MAX_LENGTH]",
        "pure",
    ),
    (
        "B2",
        "owner_id 换成 SPU 码(同 SPU 每个颜色被写成同一个名字)",
        SERVICE,
        "    _project_colour_name(session, field_name=field_name, owner_id=owner_id, value=value)",
        "    _project_colour_name(session, field_name=field_name, owner_id=product.spu, value=value)",
        "db",
    ),
    # **这里原来有一条 B3,验不出来,已删。** 值得记一句,因为它是被这份
    # 脚本证伪的一个说法:`needs_update` 的 `wanted is None` 分支
    # **从唯一调用点到不了** —— `_project_colour_name` 在更前面已经早返回了。
    # 于是任何只动其中一道的变异都是行为等价的,任何"两道一起动"的写法
    # 也只是把等价换个位置。
    #
    # 空值这条动线真正承重的那一道是 `projected_name` 返回 None,
    # 而 B1 正是它,验红。再补一条只会让"10 条全红"这个数字多一位而已。
    (
        "B4",
        "包装形状认不出来(把 {'value': 'black'} 原样显示给运营)",
        LAYER,
        '    if isinstance(value, Mapping):\n        inner = value.get("value")\n        if isinstance(inner, str):\n            return inner.strip()',
        "    if isinstance(value, Mapping):\n        return str(value)",
        "pure",
    ),
    # ------------------------------------------------ C 组:边界与台账
    (
        "C1",
        "投影里抛错(一列显示副本让事实写入回滚,运营点确认收到 500)",
        SERVICE,
        "    row = session.get(ColorVariant, variant_pk)\n    if row is None:\n        return",
        "    row = session.get(ColorVariant, variant_pk)\n    if row is None:\n        raise ValueError(owner_id)",
        "pure",
    ),
    (
        "C2",
        "投影上限与校验层脱钩(界面上的颜色名与事实值不是同一个字符串)",
        LAYER,
        "MAX_LENGTH = 128",
        "MAX_LENGTH = 64",
        "pure",
    ),
    (
        "C3",
        "台账留着已还的那一条(下一个人以为缺口还开着,重做一遍)",
        LEDGER,
        "    # `ColorVariant.display_name` **曾经在这里**(还款日:阶段 5)。",
        '    "ColorVariant.display_name": "还款日:阶段 5",\n'
        "    # `ColorVariant.display_name` **曾经在这里**(还款日:阶段 5)。",
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
