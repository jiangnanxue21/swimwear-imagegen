#!/usr/bin/env python3
"""A45-batch14-26 变异验证:三笔决定类欠账 + 老建档路径切 SPU。

用法:python3 tools/mutate_batch14_26.py

## 这一批变异在防什么

本批还的三笔账都是 14-23 逐条点名过的**决定**类,而不是缺代码类。
决定落地之后有一个共同的失效方式:**决定被悄悄撤回,而代码看上去还在**。

    C 组  evidence_class 的判定点。规则被冻进 SQL、回填绕开派生函数
    K 组  §4.8 去重键。谓词不再互补 → 中间裂开一批两条都不管的行
    A 组  素材归属。外键漏写、从字符串码反查
    P 组  老建档路径。查不到 SPU 时回落成 None、长出第二条建档路径

## K 组为什么是本批的重心

§4.8 那个键落成了两条局部唯一索引,它成立的**全部前提**是两个谓词互补。
退化回去最省事的路径不是删掉一条(太显眼),是把某一条的谓词**改窄**——
那之后仍然有两条唯一索引、`spu_id` 仍在、PRD 那个键仍在,而中间裂开的
那批行彻底失去去重,**且没有任何地方会报**。K3 / K4 钉的正是这个。

## P1 是本批唯一一条"守卫钉拒绝路径"的变异

只钉"写了 spu_id"的守卫会被 `spu_id = row.id if row else None` 喂真 ——
那一行确实写了这一列,而 §4.2 那条缝原封不动。这与 14-22 的 S6/S8
(前缀断言被另一处正确的代码喂真)是同一型。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

MODEL = "app/models/media_asset.py"
MEDIA_SVC = "app/media/service.py"
PROD_SVC = "app/services/product_service.py"
BACKFILL = "app/scripts/backfill_evidence_class.py"
MIG_0045 = "migrations/versions/0045_media_asset_evidence_class.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------ C 组:evidence_class 的判定点被复制
    (
        "C1",
        "把四条规则冻成 SQL CASE 写进迁移(第二个判定点,规则演进后它不跟)",
        MIG_0045,
        "    # **刻意不回填。** 见模块文档「为什么不在这里回填」。",
        "    op.execute(\"UPDATE media_assets SET evidence_class = "
        "CASE WHEN source = 'AI_GENERATED' THEN 'GENERATED_RESULT' "
        "WHEN source = 'PLATFORM_SYNC' THEN 'CHANNEL_DERIVATIVE' "
        "ELSE 'REFERENCE_ONLY' END\")",
    ),
    (
        "C2",
        "回填脚本不再 import 派生函数(改自己抄一份规则)",
        BACKFILL,
        "from app.media.evidence_rules import derive_evidence_class, violates_ai_check",
        "from app.media.evidence_rules import violates_ai_check",
    ),
    (
        "C3",
        "模型与迁移两处 CHECK 写得不一致(新库与存量库拦住不同的组合)",
        MIG_0045,
        '"   OR generation_candidate_id IS NOT NULL)"',
        '"   )"',
    ),
    # ------------------------------------ K 组:§4.8 去重键的覆盖面
    (
        "K1",
        "去重键退回旧的表级 UNIQUE(PRD §4.8 那条要求被撤回)",
        MODEL,
        'Index(\n            "uq_media_assets_spu_colour_sha256",',
        'UniqueConstraint("product_id", "sha256", name="uq_x"),\n        Index(\n            "uq_media_assets_spu_colour_sha256_disabled",',
    ),
    (
        "K2",
        "spu_id 列被删(§4.8 的键落不下来,0043 是 0044 的前提)",
        MODEL,
        "    spu_id: Mapped[UUID | None] = mapped_column(",
        "    spu_id_removed: Mapped[UUID | None] = mapped_column(",
    ),
    (
        "K3",
        "兜底键的谓词被改窄(中间裂开一批两条都不管的行,且不会报)",
        MODEL,
        'postgresql_where=text("spu_id IS NULL"),',
        'postgresql_where=text("spu_id IS NULL AND legacy_kind IS NULL"),',
    ),
    (
        "K4",
        "§4.8 那条的谓词被改窄(同上,方向相反)",
        MODEL,
        'postgresql_where=text("spu_id IS NOT NULL"),',
        'postgresql_where=text("spu_id IS NOT NULL AND status <> \'DELETED\'"),',
    ),
    # ------------------------------------ A 组:素材归属的写入路径
    (
        "A1",
        "构造点漏写 spu_id(素材明明有 SPU,却落进兜底去重键)",
        MEDIA_SVC,
        "        spu_id=product.spu_id,\n",
        "",
    ),
    (
        "A2",
        "构造点漏写 evidence_class(列在、白名单读着、全仓恒 NULL)",
        MEDIA_SVC,
        "        evidence_class=evidence_class.value,\n",
        "",
    ),
    (
        "A3",
        "从反规范化的 spu 字符串码反查 SPU 行补外键(改名后反查出错的 SPU)",
        MEDIA_SVC,
        "        spu_id=product.spu_id,",
        "        spu_id=session.scalar(select(Spu).where(Spu.spu_code == product.spu)).id,",
    ),
    # ------------------------------------ P 组:老建档路径
    (
        "P1",
        "查不到 SPU 时回落成 None(这一列写了,而 §4.2 那条缝原封不动)",
        PROD_SVC,
        "    spu_row = session.scalar(select(Spu).where(Spu.spu_code == spu_code))\n"
        "    if spu_row is None:",
        "    spu_row = session.scalar(select(Spu).where(Spu.spu_code == spu_code))\n"
        "    if False:",
    ),
    (
        "P2",
        "受众信入参而不是从 SPU 抄(规则包键与模特/尺码表各按一个受众走)",
        PROD_SVC,
        '    data["audience"] = spu_row.audience',
        "    pass",
    ),
    (
        "P3",
        "老路径长出第二条建档路径(调 create_spu,从一个商品展开一整套 SKU)",
        PROD_SVC,
        "    product.spu_id = spu_row.id",
        "    spu_service.create_spu(session, {}, actor=actor)\n    product.spu_id = spu_row.id",
    ),
]

#: 本批守卫 + 被翻成正向守卫的那一份 + 单写入点那一条。
#: **三份一起跑**:C 组的判据一半在 14-7 那份里(单写入点),
#: 只跑本批那份的话 C2 会 GREEN —— 而那不是"守卫漏了",是"跑窄了"。
SUITE_FILTER = "test_a45_batch14_26_decisions.py"
EXTRA_SUITES = [
    "test_a45_batch14_23_item_columns_and_dedupe_ledger.py",
    "test_a45_batch14_7_evidence_class.py",
]


def run_suite(cwd: Path, name_filter: str) -> int:
    proc = subprocess.run(
        [sys.executable, "tools/run_pure_tests.py", name_filter],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return proc.returncode


def main() -> int:
    red = 0
    for ident, description, rel, old, new in MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "tree"
            shutil.copytree(PROJECT, work, symlinks=True)
            target = work / "backend" / rel
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                print(f"{ident:4} 锚点失准({text.count(old)} 次命中) —— 无法验证")
                continue
            target.write_text(text.replace(old, new), encoding="utf-8")

            code = run_suite(work / "backend", SUITE_FILTER)
            for extra in EXTRA_SUITES:
                if code == 0:
                    code = run_suite(work / "backend", extra)

            verdict = "RED" if code != 0 else "GREEN"
            if code != 0:
                red += 1
            print(f"{ident:4} {description}  {verdict}")

    print(f"\n{red}/{len(MUTATIONS)} 条变异验红")
    return 0 if red == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
