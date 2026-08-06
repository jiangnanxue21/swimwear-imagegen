#!/usr/bin/env python3
"""A45-batch14-19 的变异验证:守卫是不是真的拦得住它声称拦的东西。

## 这一批的失效方向

本批守的是「AI 图别进识别输入」和「本该识别的图别被挡在库里」。两个方向的
错误都**不报错**:

    多放一张   每张一次真实付费调用,要等有人查账单
    多挡一张   识别成功,只是少了几个字段 —— 会被当成"这张图上看不出来"

后者尤其安静,所以变异里专门有几条打粗筛的方向。

## 边界

粗筛的 WHERE 子句在这台机器上一次都没执行过(没有 sqlalchemy)。下面这些
变异验的是**结构守卫咬不咬得住**,不是 SQL 对不对 —— 后者只有
`tests/test_a45_batch14_19_evidence_query_db.py` 在真库上能回答。
所以有几条 SQL 语义变异(把 `is_(None)` 改成 `isnot(None)`)在这里必然
GREEN,它们没有列进来:**列一条明知抓不住的变异进去,只会让 19/19 这个
数字变成一句谎话。**那几条写在 MERGE 文档「验不到什么」一节。

    python3 tools/mutate_batch14_19.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

MEDIA = "app/media/service.py"
ATTRS = "app/attributes/service.py"
RULES = "app/media/evidence_rules.py"
FINGERPRINT = "app/attributes/scope_fingerprint.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # -------------------------------------------------- 入口自己不过滤了
    (
        "E1",
        "取数入口不再过滤,把判定留给调用方(退回本批之前那个形状)",
        MEDIA,
        "    return [\n"
        "        asset\n"
        "        for asset in rows\n"
        "        if evidence_rules.asset_is_extraction_input(\n"
        "            asset, imported_url_trusted=imported_url_trusted\n"
        "        )\n"
        "    ]",
        "    return rows",
    ),
    (
        "E2",
        "过滤算了但返回粗筛结果(「算出来了但没用」——最常见的接线坏法)",
        MEDIA,
        "    rows = list(session.scalars(stmt.order_by(MediaAsset.created_at)))",
        "    rows = list(session.scalars(stmt.order_by(MediaAsset.created_at)))\n"
        "    return rows",
    ),
    (
        "E3",
        "过滤条件取反(识别只跑在 AI 图上——比不修还严重,14-7 变异 P2 的形状)",
        MEDIA,
        "        if evidence_rules.asset_is_extraction_input(\n"
        "            asset, imported_url_trusted=imported_url_trusted\n"
        "        )",
        "        if not evidence_rules.asset_is_extraction_input(\n"
        "            asset, imported_url_trusted=imported_url_trusted\n"
        "        )",
    ),
    (
        "E4",
        "过滤条件和别的条件拼在一起(白名单变成半个白名单)",
        MEDIA,
        "        if evidence_rules.asset_is_extraction_input(\n"
        "            asset, imported_url_trusted=imported_url_trusted\n"
        "        )\n    ]",
        "        if evidence_rules.asset_is_extraction_input(\n"
        "            asset, imported_url_trusted=imported_url_trusted\n"
        "        ) or asset.role is not None\n    ]",
    ),
    # -------------------------------------------------- 粗筛用了没证明过的列
    (
        "C1",
        "粗筛多加一条没被判定蕴含的条件(本该识别的图静静消失,没有地方会报)",
        MEDIA,
        "        MediaAsset.source.not_in(",
        "        MediaAsset.role.is_not(None),\n        MediaAsset.source.not_in(",
    ),
    (
        "C2",
        "粗筛不再排除 AI 图(结果还对,但每次识别都读整个素材库再丢掉)",
        MEDIA,
        "        MediaAsset.source.not_in(\n"
        "            (MediaSource.AI_GENERATED.value, MediaSource.PLATFORM_SYNC.value)\n"
        "        ),",
        "",
    ),
    (
        "C3",
        "粗筛丢掉两条溯源列(14-16 落的库白落了)",
        MEDIA,
        "        MediaAsset.generation_task_id.is_(None),\n"
        "        MediaAsset.generation_candidate_id.is_(None),",
        "",
    ),
    (
        "C4",
        "把 order_by 之外的列声明表清空(守卫失去参照物)",
        MEDIA,
        '_COARSE_FILTER_COLUMNS = frozenset(\n'
        '    {"status", "generation_task_id", "generation_candidate_id", "source"}\n'
        ")",
        "_COARSE_FILTER_COLUMNS = frozenset()",
    ),
    # -------------------------------------------------- 识别路径又碰未过滤取数
    (
        "P1",
        "识别路径退回 usable_assets(唯一取数入口这条整个失效)",
        ATTRS,
        "    evidence = media_service.evidence_assets_for(\n"
        "        session, product.id, imported_url_trusted=True\n"
        "    )",
        "    evidence = media_service.usable_assets(session, product.id)",
    ),
    (
        "P2",
        "识别路径同时留一个装未过滤行的变量(下一个人拿哪个全看运气)",
        ATTRS,
        "        total = media_service.usable_asset_count(session, product.id)",
        "        total = len(media_service.usable_assets(session, product.id))",
    ),
    (
        "P4",
        "喂给抽取器的清单多一个绑定点(14-7 的 P1 退役后,这个洞由它守着)",
        ATTRS,
        "    assets = evidence",
        "    assets = evidence\n    assets = list(assets) or []",
    ),
    (
        "P3",
        "未过滤取数悄悄多一个调用点(账没销就先用上了)",
        ATTRS,
        "def build_canonical(session: Session, product: Product):",
        "def _sneaky_new_caller(session, product):\n"
        "    return media_service.usable_assets(session, product.id)\n"
        "\n"
        "\n"
        "def build_canonical(session: Session, product: Product):",
    ),
    # -------------------------------------------------- 作用域词汇漂移
    (
        "S1",
        "共享作用域在取数这边改成空串(指纹和识别从此看的不是同一批图)",
        MEDIA,
        "SHARED_SCOPE: None = None",
        'SHARED_SCOPE: str = ""',
    ),
    (
        "S2",
        "共享作用域在指纹那边改成空串(同上,反方向)",
        FINGERPRINT,
        "SHARED: None = None",
        'SHARED: str = ""',
    ),
    # -------------------------------------------------- 溯源列
    (
        "R1",
        "适配器不再读溯源列(被重新上传成 MANUAL_UPLOAD 的 AI 图重新变干净)",
        RULES,
        '            generation_task_id=getattr(asset, "generation_task_id", None),\n'
        '            generation_candidate_id=getattr(asset, "generation_candidate_id", None),',
        "            generation_task_id=None,\n            generation_candidate_id=None,",
    ),
]

SUITE_FILTER = "test_a45_batch14_19_evidence_query.py"

#: R1 打的是 14-7 那份守卫(适配器的前向兼容那条),不在本批文件里。
#: 拿子串一次跑两个文件的话,它的颜色由**哪一条守卫先红**决定,
#: 而报告只会记一个编号 —— 见 `tools/suite_filter.py` 顶部。
EXTRA_FILTER = {"R1": "test_a45_batch14_7_evidence_class.py"}


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
    results = []
    for ident, description, rel, old, new in MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            shutil.copytree(
                PROJECT,
                root,
                symlinks=True,
                ignore=shutil.ignore_patterns(
                    ".git", "node_modules", "__pycache__", "dist"
                ),
            )
            work = root / "backend"
            target = (work / rel).resolve()
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                results.append(
                    (
                        ident,
                        f"{description} —— 锚点出现 {text.count(old)} 次,拒绝变异",
                        False,
                        [],
                        False,
                    )
                )
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            code, output = run_suite(work, EXTRA_FILTER.get(ident, SUITE_FILTER))
            failed = [
                line.split("::", 1)[1].strip()
                for line in output.splitlines()
                if "FAILED" in line and "::" in line
            ]
            ran = "passed" in output and any(
                "FAILED" in line and "::" in line for line in output.splitlines()
            )
            crashed = not ran and any(
                marker in output
                for marker in (
                    "NameError",
                    "ImportError",
                    "SyntaxError",
                    "ModuleNotFoundError",
                    "AttributeError",
                )
            )
            results.append((ident, description, code != 0, failed, crashed))

    ok = True
    for ident, description, went_red, failed, crashed in results:
        mark = "RED " if went_red else "GREEN"
        note = ""
        if crashed:
            note = "  <- 导入期就炸了,这条变异不算数"
            ok = False
        elif went_red and failed:
            note = "  " + ", ".join(failed[:2])
        print(f"{ident:<4} {description}  {mark}{note}")
        ok = ok and went_red
    reds = sum(1 for r in results if r[2] and not r[4])
    print()
    print(f"{reds}/{len(results)} 条变异验红")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
