#!/usr/bin/env python3
"""A45-batch14-20 的变异验证:守卫是不是真的拦得住它声称拦的东西。

## 这一批的失效方向

本批守的是接线,而接线的坏法**一条都不报错**:

    键算了不写列        双击照样建两个 run,每个打一轮付费调用
    查重排在循环之后    先把钱花完再问「这次是不是重复请求」
    凑一个假的 spu      两个不同 SPU 的同型请求算出同一个键 ——
                        第二个商品会填上第一个商品的属性,接口 200
    终态不写回列        `status` 停在 RUNNING,而 RUNNING 占键 ——
                        这个 SPU 再也识别不了第二次
    指纹不过 views()    每个元素 getattr 到 None,算出一个恒定哈希 ——
                        换了图也不 stale

全部的共同点是**账单之外没有任何征兆**,所以变异集中打这几个方向。

## 边界:验不到什么

三条真库语义在这台机器上一次都执行不了 —— 部分唯一索引的谓词、
`IntegrityError` 那一路的并发裁决、`server_default` 对存量行的效果。
把 `postgresql_where` 删掉、把 `status.in_(...)` 改成 `isnot(None)`
这类变异在这里会 **GREEN**,而它们不是守卫的漏洞,是这台机器没有库。

它们没有列进下面的表:**列一条明知抓不住的变异进去,只会让 N/N 这个
数字变成一句谎话。**规格写在
`tests/test_a45_batch14_20_run_identity_db.py`,那份用例本轮没跑过。

    python3 tools/mutate_batch14_20_run_identity.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

SERVICE = "app/attributes/service.py"
MODEL = "app/models/attribute.py"
MOCK = "app/extractors/mock.py"
MIGRATION = "migrations/versions/0040_extraction_run_identity.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # -------------------------------------------------- 算出来了但没用
    (
        "W1",
        "键算了但建行时不带上(「算出来了但没用」——最常见的接线坏法)",
        SERVICE,
        "        idempotency_key=identity.key,\n",
        "",
    ),
    (
        "W2",
        "终态算了但不写回 status 列(停在 RUNNING,而 RUNNING 占键)",
        SERVICE,
        "    row.status = run_state.terminal_status_for(",
        "    _unused = run_state.terminal_status_for(",
    ),
    (
        "W3",
        "指纹算了但不落列(facts_stale 退回恒 True,全库事实集体过期)",
        SERVICE,
        "        input_fingerprint=identity.input_fingerprint,\n",
        "",
    ),
    (
        "W4",
        "作用域算了但不落列(键里有它、列里没有,别的调用点复算不出同一个键)",
        SERVICE,
        "        requested_scope=identity.scope_token,\n",
        "",
    ),
    # -------------------------------------------------- 顺序
    (
        "O1",
        "查到了占键的行却不 return(日志说复用了,下面照样付一轮钱)",
        SERVICE,
        "            return existing\n",
        "",
    ),
    # -------------------------------------------------- 凑一个假的
    (
        "F1",
        "没有 spu_id 时拿 product.id 顶上(两个不同 SPU 算出同一个键)",
        SERVICE,
        "    spu_id = getattr(product, \"spu_id\", None)\n"
        "    if spu_id is None:\n"
        "        return _RunIdentity(None, scope.token, input_fingerprint, None, "
        "\"PRODUCT_HAS_NO_SPU\")",
        "    spu_id = getattr(product, \"spu_id\", None) or product.id",
    ),
    (
        "F2",
        "增量识别硬塞成 ALL 作用域(requested_scope 说一句不真的话)",
        SERVICE,
        "    if not whole_product:\n"
        "        return _RunIdentity(None, None, input_fingerprint, None, "
        "\"PARTIAL_MEDIA_SELECTION\")",
        "",
    ),
    (
        "F3",
        "抽取器报不出版本时拿 name 凑一个(换了模型还在用旧结果)",
        SERVICE,
        "    declared = getattr(extractor, \"declared_versions\", None)\n"
        "    if declared is None:\n"
        "        return _RunIdentity(\n"
        "            spu_id, scope.token, input_fingerprint, None, "
        "\"EXTRACTOR_DECLARES_NO_VERSIONS\"\n"
        "        )\n"
        "    model_version, prompt_version = declared()",
        "    declared = getattr(extractor, \"declared_versions\", None)\n"
        "    model_version, prompt_version = (\n"
        "        declared() if declared else (getattr(extractor, \"name\", \"?\"), \"1\")\n"
        "    )",
    ),
    (
        "F4",
        "版本改取响应里的(键要付过钱才算得出来,而它要挡的是付钱前那两下)",
        MOCK,
        "        return MOCK_MODEL_NAME, MOCK_PROMPT_VERSION",
        "        return \"\", MOCK_PROMPT_VERSION",
    ),
    # -------------------------------------------------- 判定被重写
    (
        "D1",
        "复用判据改成在服务层自己比状态(和 reuse_verdict 对 PARTIAL_SUCCESS 分歧)",
        SERVICE,
        "        verdict = run_state.reuse_verdict(\n"
        "            existing.status if existing is not None else None\n"
        "        )",
        "        verdict = (\n"
        "            run_state.ReuseVerdict.REUSE_RESULT\n"
        "            if existing is not None and existing.status == \"COMPLETED\"\n"
        "            else run_state.ReuseVerdict.NEW_RUN\n"
        "        )",
    ),
    (
        "D2",
        "查重的状态集合手写一份(与索引谓词漂移,并发双击变成 500)",
        SERVICE,
        "            ProductAttributeExtraction.status.in_(\n"
        "                sorted(s.value for s in run_state.KEY_OCCUPYING_STATUSES)\n"
        "            ),",
        "            ProductAttributeExtraction.status.in_([\"QUEUED\", \"RUNNING\"]),",
    ),
    (
        "D3",
        "补 error_code 的判据退回按计数数(cancel 落地那天两者就不同了)",
        SERVICE,
        "    if row.status == ExtractionRunStatus.FAILED.value and not row.error_code:",
        "    if succeeded == 0 and not row.error_code:",
    ),
    # -------------------------------------------------- 索引孪生
    (
        "T1",
        "ORM 索引谓词改成手打字面量(与 KEY_OCCUPYING_STATUSES 漂移)",
        MODEL,
        "            postgresql_where=text(run_state.unique_index_predicate()),",
        "            postgresql_where=text(\"status IN ('COMPLETED', 'RUNNING')\"),",
    ),
    (
        "T2",
        "迁移里的冻结谓词多带一档(新库按新索引、老库按老索引,答案不同)",
        MIGRATION,
        "_KEY_OCCUPYING_PREDICATE = \"status IN ('COMPLETED', 'QUEUED', 'RUNNING')\"",
        "_KEY_OCCUPYING_PREDICATE = \"status IN ('COMPLETED', 'QUEUED', 'RUNNING', "
        "'PARTIAL_SUCCESS')\"",
    ),
    # -------------------------------------------------- 指纹入参
    (
        "P1",
        "ORM 行直接进指纹,不过 views()(算出一个恒定哈希,换了图也不 stale)",
        SERVICE,
        "    views = scope_fingerprint.views(assets)\n"
        "    prints = scope_fingerprint.fingerprints(views, imported_url_trusted=True)",
        "    prints = scope_fingerprint.fingerprints(assets, imported_url_trusted=True)",
    ),
    (
        "P2",
        "指纹的可信口径和取数不一致(有一批图花了钱却不进指纹)",
        SERVICE,
        "    prints = scope_fingerprint.fingerprints(views, imported_url_trusted=True)",
        "    prints = scope_fingerprint.fingerprints(views)",
    ),
    # -------------------------------------------------- 并发裁决
    (
        "R1",
        "去掉保存点(约束冲突让整个事务 aborted,这次识别再也提交不了)",
        SERVICE,
        "    savepoint = session.begin_nested()\n"
        "    try:\n"
        "        session.add(row)\n"
        "        session.flush()\n"
        "        savepoint.commit()",
        "    try:\n"
        "        session.add(row)\n"
        "        session.flush()",
    ),
    (
        "R2",
        "IntegrityError 无条件吞掉(别的写入缺陷会伪装成一次安静的复用)",
        SERVICE,
        "        if winner is None:\n",
        "        if False:\n",
    ),
    # -------------------------------------------------- 建行档位
    (
        "S1",
        "建行时不写 RUNNING(跑的过程中键没被占住,第二个请求又打一轮调用)",
        SERVICE,
        "        status=ExtractionRunStatus.RUNNING.value,\n",
        "",
    ),
    (
        "S2",
        "派生属性 status 复活(索引谓词读列、接口读属性,两个答案)",
        MODEL,
        "class AttributeEvidence(Base, UUIDPrimaryKeyMixin, TimestampMixin):",
        "def _revived(self):\n"
        "    return \"COMPLETED\"\n"
        "\n"
        "\n"
        "ProductAttributeExtraction.status = property(_revived)\n"
        "\n"
        "\n"
        "class AttributeEvidence(Base, UUIDPrimaryKeyMixin, TimestampMixin):",
    ),
]

SUITE_FILTER = "test_a45_batch14_20_run_identity.py"

#: S2 打的是「派生属性不许复活」,那条守卫在 14-12 的文件里(它和终态宿主
#: 是同一件事的两面)。T2 打的是迁移孪生,同样在 14-12。
#: 拿子串一次跑两个文件的话,颜色由**哪一条守卫先红**决定,而报告只记一个
#: 编号 —— 见 `tools/suite_filter.py` 顶部。
EXTRA_FILTER = {
    "W2": "test_a45_batch14_12_run_state.py",
    "S2": "test_a45_batch14_12_run_state.py",
    "T1": "test_a45_batch14_12_run_state.py",
    "T2": "test_a45_batch14_12_run_state.py",
    "P1": "test_a45_batch14_9_scope_fingerprint.py",
    "P2": "test_a45_batch14_9_scope_fingerprint.py",
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
            text_src = target.read_text(encoding="utf-8")
            if text_src.count(old) != 1:
                results.append(
                    (
                        ident,
                        f"{description} —— 锚点出现 {text_src.count(old)} 次,拒绝变异",
                        False,
                        [],
                        False,
                    )
                )
                continue
            target.write_text(text_src.replace(old, new, 1), encoding="utf-8")
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
