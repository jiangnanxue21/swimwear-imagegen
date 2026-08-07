#!/usr/bin/env python3
"""A45-batch14-21 变异验证:`facts_stale` 派生接线 + 还款日门禁。

用法:python3 tools/mutate_batch14_21.py

## 这一批变异在防什么

14-9 那批变异咬的是**判定**(指纹算得对不对)。本批咬的是**接线**,
而接线的失效方式和判定完全不同:判定错了会算出一个错的答案,接线错了
是**没有人问那个问题**,而套件照样全绿。

所以每一组针对的都是一种"不报错的坏法":

    W 组  写入侧不记指纹 / 记错来源。最坏的一条是拿 run 行的指纹顶数 ——
          共享事实与颜色事实共用同一个答案,D1 从后门原样回来
    Q 组  取数口径分叉。写入按 A 集合、读取按 B 集合,表现是"刚确认完
          就显示过期",而两边都不报错
    R 组  读取侧措辞与完成条件。说错话会把运营送去做一件要花钱且确定
          无效的事;完成条件少一句话会让换掉全部样品的商品显示"已完成"
    S 组  素材侧快照。before/after 取成同一批活对象时 changed_scopes
          恒空 —— §8.1 第 2 行那一格恒定说"什么都没变"
    D 组  还款日门禁自己。**这一组是本批的重心**:一条盯着别人的门禁
          自己失效时,没有任何东西会响

## D 组为什么必须有

本批新加的门禁是用来替**其他**守卫记账的。它自己失效的表现,恰恰是
它要防的那件事:一切照常绿,而欠账在后面躺着。所以这里逐条改坏它,
看纯层那条元守卫会不会红。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

SVC = "app/attributes/service.py"
VAL = "app/attributes/validation.py"
FLOW = "app/workbench/flow.py"
WB = "app/workbench/service.py"
MEDIA = "app/media/service.py"
API = "app/api/attributes.py"
SCHEMA = "app/schemas/attribute.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------------ W 组:写入侧
    (
        "W1",
        "事实的指纹取自 run 行(共享与颜色事实共用一个答案,D1 从后门回来)",
        SVC,
        "                input_fingerprint=fingerprint_for_owner(\n"
        "                    prints, owner_type=owner_type, owner_id=owner_id\n"
        "                ),",
        "                input_fingerprint=extraction.input_fingerprint,",
    ),
    (
        "W2",
        "识别写值不记指纹(每次识别完所有字段立刻显示「样品已变」)",
        SVC,
        "                input_fingerprint=fingerprint_for_owner(\n"
        "                    prints, owner_type=owner_type, owner_id=owner_id\n"
        "                ),",
        "",
    ),
    (
        "W3",
        "人工确认不记指纹(确认→刷新→又待确认,一个不报错的死循环)",
        SVC,
        "        actor=actor,\n"
        "        input_fingerprint=fingerprint_for_owner(\n"
        "            prints, owner_type=owner_type, owner_id=owner_id\n"
        "        ),\n"
        "    )",
        "        actor=actor,\n    )",
    ),
    (
        "W4",
        "set_value 收下参数却不写进行(列恒为空,全库事实恒过期)",
        SVC,
        "        input_fingerprint=input_fingerprint,\n        valid_from=_now(),",
        "        valid_from=_now(),",
    ),
    # ------------------------------------------------ Q 组:取数口径
    (
        "Q1",
        "算指纹时把口径写死成 False(一批图花了钱却不进指纹)",
        SVC,
        "    return scope_fingerprint.fingerprints(\n"
        "        scope_fingerprint.views(assets),\n"
        "        imported_url_trusted=EVIDENCE_IMPORTED_URL_TRUSTED,\n"
        "    )",
        "    return scope_fingerprint.fingerprints(\n"
        "        scope_fingerprint.views(assets),\n"
        "        imported_url_trusted=False,\n"
        "    )",
    ),
    (
        "Q2",
        "工作台自己再算一遍指纹(读取侧与写入侧两套口径)",
        WB,
        "        stale=attr_service.stale_fields(session, product, values),",
        "        stale=frozenset(\n"
        "            name for name, row in values.items()\n"
        "            if row.input_fingerprint is None\n"
        "        ),",
    ),
    (
        "Q3",
        "指纹不经 views()(所有素材长得一样,换了图也不 stale)",
        SVC,
        "        scope_fingerprint.views(assets),",
        "        assets,",
    ),
    # ------------------------------------------------ R 组:读取侧判定与措辞
    (
        "R1",
        "完成条件少掉指纹那一句(换掉全部样品后界面显示「已完成」)",
        FLOW,
        "            if f not in self.confirmed or f in self.stale_confirmed",
        "            if f not in self.confirmed",
    ),
    (
        "R2",
        "过期字段掉进「尚无可用值」(把运营送去付一次确定无效的钱)",
        FLOW,
        "        if name in stale_confirmed:",
        "        if False:",
    ),
    (
        "R3",
        "过期报成阻断(阻断数虚高,而它欠的只是「人再看一眼」)",
        FLOW,
        '                    level=IssueLevel.NEEDS_CONFIRM,\n'
        '                    code="ATTR_FACT_STALE",',
        '                    level=IssueLevel.BLOCKING,\n'
        '                    code="ATTR_FACT_STALE",',
    ),
    (
        "R4",
        "不变式改回读裸 stale(判据落回调用点,没确认过的字段会说假话)",
        FLOW,
        "        return self.stale & self.confirmed",
        "        return self.stale",
    ),
    (
        "R5",
        "CANDIDATE 也算过期(工作台对同一字段同时说两句话)",
        SVC,
        "        if settled_only and (\n"
        "            queue_policy.disposition(row.status) is not queue_policy.Disposition.SETTLED\n"
        "        ):\n            continue",
        "        if False:\n            continue",
    ),
    (
        "R6",
        "CHANNEL 也参与(刊登属性跟着补图集体过期)",
        VAL,
        "    if owner_type is OwnerType.CHANNEL:\n        return NOT_FINGERPRINTED",
        "    if owner_type is OwnerType.CHANNEL:\n        return SHARED_FINGERPRINT_SCOPE",
    ),
    (
        "R7",
        "VARIANT 拿整个带命名空间的 owner_id 去查(颜色事实恒过期)",
        VAL,
        "        _spu, variant_id = split\n"
        "        return FingerprintScope(participates=True, scope=variant_id or None)",
        "        return FingerprintScope(participates=True, scope=owner_id)",
    ),
    (
        "R8",
        "拆不出来的裸变体 id 判成不参与(身份可疑的事实被宣布永远成立)",
        VAL,
        "        if split is None:\n            return SHARED_FINGERPRINT_SCOPE",
        "        if split is None:\n            return NOT_FINGERPRINTED",
    ),
    (
        "R9",
        "「不参与」靠 current is None 推(CHANNEL 与「颜色没图」合成一种)",
        SVC,
        "        if not validation.fingerprint_scope(owner_type, row.owner_id).participates:\n"
        "            continue",
        "        pass",
    ),
    # ------------------------------------------------ S 组:素材侧快照
    (
        "S1",
        "before/after 取成同一批活 ORM 行(changed_scopes 恒空)",
        MEDIA,
        "    return scope_fingerprint.views(\n"
        "        evidence_assets_for(\n"
        "            session, product_id, imported_url_trusted=_IMPORTED_URL_TRUSTED\n"
        "        )\n"
        "    )",
        "    return list(\n"
        "        evidence_assets_for(\n"
        "            session, product_id, imported_url_trusted=_IMPORTED_URL_TRUSTED\n"
        "        )\n"
        "    )",
    ),
    (
        "S2",
        "隔离不记作用域变化(最常被追的那次动作没有答案)",
        MEDIA,
        '            "downgraded_image_sets": len(downgraded),\n'
        "            **_scope_change_payload(before, _evidence_views(session, asset.product_id)),",
        '            "downgraded_image_sets": len(downgraded),',
    ),
    (
        "S3",
        "共享作用域在审计里留成 null(和「什么都没变」混掉)",
        MEDIA,
        '            "SHARED" if scope is scope_fingerprint.SHARED else str(scope)',
        "            str(scope)",
    ),
    (
        "S4",
        "改角色的快照取在赋值之后(拿到的是同一个值)",
        MEDIA,
        "    before = _evidence_views(session, asset.product_id)\n    asset.role = role.value",
        "    asset.role = role.value",
    ),
    # ------------------------------------------------ A 组:接口出参
    (
        "A1",
        "_out 的 stale 给了默认值(第三个端点会忘记传而不报错)",
        API,
        "def _out(row, *, stale: frozenset[str]) -> AttributeValueOut:",
        "def _out(row, *, stale: frozenset[str] = frozenset()) -> AttributeValueOut:",
    ),
    (
        "A2",
        "出参默认成「不过期」(一次忘记赋值 = 全库事实显示正常)",
        SCHEMA,
        "    facts_stale: bool = True",
        "    facts_stale: bool = False",
    ),
    (
        "A3",
        "接口读 legacy 那一桶(颜色事实的作用域退化成 SPU 级)",
        API,
        "    values = attr_service.effective_map(session, product_id, product=product)",
        "    values = attr_service.effective_map(session, product_id)",
    ),
    # ------------------------------------------------ D 组:还款日门禁自己
    (
        "D1",
        "还款日门禁不判到期(退回「守卫钉没接线、不钉什么时候该接」)",
        "tools/verify_delivery.py",
        "            if int(due.group(1)) <= stage:\n"
        '                overdue.append(f"{where}（还款日 阶段 {due.group(1)}）")',
        "            pass",
    ),
    (
        "D2",
        "没声明还款日的欠账被放过(欠账又回到只写在文档里)",
        "tools/verify_delivery.py",
        "            if not due:\n"
        "                undeclared.append(",
        "            if False:\n"
        "                undeclared.append(",
    ),
    (
        "D3",
        "命名式反向检查删掉(删掉标记就能绕过整条门禁)",
        "tools/verify_delivery.py",
        "                if any(hint in node.name for hint in DEBT_GUARD_NAME_HINTS):\n"
        "                    unmarked.append(where)",
        "                pass",
    ),
    (
        "D4",
        "找不到标记时静静通过(标记被删 = 门禁永远绿)",
        "tools/verify_delivery.py",
        "    if not seen_debt:\n        raise Failure(",
        "    if False:\n        raise Failure(",
    ),
    (
        "D5",
        "当前阶段写死在脚本里(和 STATUS 那张表分叉,按没人维护的数字判)",
        "tools/verify_delivery.py",
        "    stage = _declared_stage()\n"
        "    root = PROJECT_ROOT / \"backend\" / \"tests\" / \"pure\"",
        "    stage = 0\n"
        "    root = PROJECT_ROOT / \"backend\" / \"tests\" / \"pure\"",
    ),
    (
        "D6",
        "STATUS 读不到标记时跳过而不是红(门禁自己成了一笔没还款日的欠账)",
        "tools/verify_delivery.py",
        "    if not match:\n        raise Failure(\n"
        '            "docs/STATUS.md 里找不到 `<!-- DELIVERY_STAGE: N -->` 标记。"',
        "    if not match:\n        return 99\n    if False:\n        raise Failure(\n"
        '            "docs/STATUS.md 里找不到 `<!-- DELIVERY_STAGE: N -->` 标记。"',
    ),
    # ------------------------------------------------ C 组:链路守卫自己
    (
        "D7",
        "声明范围放回全文(解释里提一句标记就被判成欠账 —— 假红会诱导删门禁)",
        "tools/verify_delivery.py",
        'if DEBT_REPAID_MARKER in head:',
        'if DEBT_REPAID_MARKER in doc:',
    ),
    (
        "D8",
        "欠账声明的正则改成只认半角冒号(全角写法一条都找不到 = 门禁恒绿)",
        "tools/verify_delivery.py",
        'DEBT_GUARD_MARKER = "记的是欠账"',
        'DEBT_GUARD_MARKER = "記的是欠賬"',
    ),
    (
        "C1",
        "迁移链守卫的覆盖率断言删掉(退回那条看不见 0041 的瞎正则)",
        "tests/pure/test_a45_batch14_15_attribution.py",
        "    assert len(revisions) == len(files), (",
        "    assert True, (",
    ),
]

#: **精确文件名,不是子串。** 子串会连带跑上别的套件,于是一条变异可以因为
#: 别人的守卫而变红,而报告记在自己头上(见 `tools/suite_filter.py` 顶部)。
#:
#: D 组与 C 组改的是门禁与链路守卫,它们的元守卫住在本批那个文件与
#: 14-15 那个文件里,所以按变异分别指定套件。
SUITE_FILTER = "test_a45_batch14_21_facts_stale.py"
#: C 组改的是**另一条守卫自己的断言**,所以它的检测者不可能是那条守卫 ——
#: 跑它自己的套件只会一片绿。检测者是本批那层元守卫,住在 delivery_gate 里。
SUITE_BY_PREFIX = {
    "D": "test_a45_batch14_21_delivery_gate.py",
    "C": "test_a45_batch14_21_delivery_gate.py",
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
        suite = SUITE_BY_PREFIX.get(ident[0], SUITE_FILTER)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            shutil.copytree(
                PROJECT,
                root,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", "dist"),
            )
            work = root / "backend"
            target = (work / rel).resolve()
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                results.append(
                    (ident, f"{description} —— 锚点出现 {text.count(old)} 次,拒绝变异",
                     False, [], False)
                )
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            code, output = run_suite(work, suite)
            failed = [
                line.split("::", 1)[1].strip()
                for line in output.splitlines()
                if "FAILED" in line and "::" in line
            ]
            crashed = any(
                marker in output
                for marker in ("NameError", "ImportError", "SyntaxError", "ModuleNotFoundError")
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
