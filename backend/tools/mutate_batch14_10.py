#!/usr/bin/env python3
"""A45-batch14-10 变异验证:§6.2 样品完整度门禁的单点派生。

用法:python3 tools/mutate_batch14_10.py

## 这一批变异在防什么

这道门禁**今天是对的,但它对得没有道理**:它不问角色是谁标的,也不问那张图
是不是本商品的证据,而它至今没出事,靠的是 M3 的角色识别还没接。
所以这一批的失效方式有一个共同形状:**改完之后本机一切照旧,
要等某条外部事实发生(M3 落地、有人补一次角色)才开始漏。**

R 组是白名单本体:放进 `MODEL` 或改成排除法,判定今天一个字都不会变 ——
因为全仓 `RULE` / `MODEL` 一个写入点都没有。它们要到 M3 落地那天才开始漏,
而那时**代码和现在逐字相同**,没有任何测试会变红。这正是这一批最该被
穷举守卫钉住的部分。

E 组是另一半条件。只筛角色来源挡不住 AI 图(`_fill_missing_role` 会在去重
命中时给一条 AI 影子行写上 `role_source=HUMAN`),只筛来源挡不住模型猜的
角色。两个条件缺一不可,所以两边各有变异。

G 组管满足组 —— 本批**放宽**的那一处。退回单角色的表现是:只有透明底
商品图的商品被永久判「缺少正面图」,而运营解除它的唯一办法是把平铺图
改标成正面图,于是素材库的角色标注被改坏,下游每一处按角色取图的地方跟着错。

S 组管双作用域。颜色那一半今天没有调用点,但判定要在接线之前验完 ——
真库用例池里已经压着 60 条没跑过的了,再往里加一条等于把它推给下一台机器。

C 组管接线与联动那几道接缝。这一组最值得看的是 C2:给新字段一个宽松默认值
(「没传就用 usable_roles」),漏填的表现是**悄悄放行** —— 硬规则 4
第二次事故的形状正是如此。

D 组管确定性。它们不会让任何断言变假,只会让同一件商品刷新两次显示不同的
提示语顺序 —— 谁都不会把这件事报成 bug,只会觉得这个界面有点飘。

## 为什么变异先写

反过来做就是 batch13-3 / M11 的来路:守卫写在实现之后,它守的是
「我刚才写了什么」,不是「哪里会坏」。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

SC = "app/media/sample_completeness.py"
FLOW = "app/workbench/flow.py"
SVC = "app/workbench/service.py"
BATCH = "app/workbench/batch.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------- R 组:角色来源白名单(§6.2 本体)
    (
        "R1",
        "白名单放进 MODEL(模型猜的角色直接满足门禁 —— M3 落地那天的原样复现)",
        SC,
        'CONFIRMED_ROLE_SOURCES: frozenset[str] = frozenset({"HUMAN", "CONFIRMED"})',
        'CONFIRMED_ROLE_SOURCES: frozenset[str] = frozenset({"HUMAN", "CONFIRMED", "MODEL"})',
    ),
    (
        "R2",
        "白名单放进 RULE(按文件名推出来的角色也算人定的)",
        SC,
        'CONFIRMED_ROLE_SOURCES: frozenset[str] = frozenset({"HUMAN", "CONFIRMED"})',
        'CONFIRMED_ROLE_SOURCES: frozenset[str] = frozenset({"HUMAN", "CONFIRMED", "RULE"})',
    ),
    (
        "R3",
        "白名单改成排除法(将来新增的任何来源取值默认合格)",
        SC,
        "    return _text_or_none(role_source) in CONFIRMED_ROLE_SOURCES",
        '    return _text_or_none(role_source) not in {"MODEL"}',
    ),
    (
        "R4",
        "role_source 整条不参与判定(退回本批之前的口径)",
        SC,
        '    if not role_source_is_confirmed(getattr(asset, "role_source", None)):\n'
        "        return False\n"
        '    return _text_or_none(getattr(asset, "role", None)) is not None',
        '    return _text_or_none(getattr(asset, "role", None)) is not None',
    ),
    (
        "R5",
        "空 role_source 视作已确认(存量行一次性全部合格)",
        SC,
        "    return _text_or_none(role_source) in CONFIRMED_ROLE_SOURCES",
        "    return role_source is None or _text_or_none(role_source) in CONFIRMED_ROLE_SOURCES",
    ),
    # ------------------------------------------- E 组:证据那一半(§5.1 的复用)
    (
        "E1",
        "门禁不过证据白名单(AI 图满足样品完整度 —— 第二节那条开口)",
        SC,
        "    if not asset_is_extraction_input(asset, imported_url_trusted=imported_url_trusted):\n"
        "        return False\n",
        "",
    ),
    (
        "E2",
        "证据判定取反(门禁只认 AI 图与被隔离的图)",
        SC,
        "    if not asset_is_extraction_input(asset, imported_url_trusted=imported_url_trusted):",
        "    if asset_is_extraction_input(asset, imported_url_trusted=imported_url_trusted):",
    ),
    (
        "E3",
        "可确认角色不过证据白名单(叫运营去确认一张 AI 图的角色,确认完还是过不去)",
        SC,
        "        if not asset_is_extraction_input(\n"
        "            asset, imported_url_trusted=imported_url_trusted\n"
        "        ):\n"
        "            continue\n",
        "",
    ),
    # ------------------------------------------- G 组:满足组(本批放宽的那一处)
    (
        "G1",
        "满足组退回单角色(只有透明底商品图的商品被永久判「缺少正面图」)",
        SC,
        'REQUIRED_ROLE_GROUPS: tuple[tuple[str, ...], ...] = (\n    ("PRODUCT_FRONT", "FLAT_LAY"),\n)',
        'REQUIRED_ROLE_GROUPS: tuple[tuple[str, ...], ...] = (\n    ("PRODUCT_FRONT",),\n)',
    ),
    (
        "G2",
        "组内改成「全部满足」(正面图与平铺图都要有,比原来更严)",
        SC,
        "if not (set(group) & have)",
        "if not (set(group) <= have)",
    ),
    (
        "G3",
        "组标签只说第一个角色(判定说「或平铺图」、提示说「缺少正面图」)",
        SC,
        '    return "或".join(role_label(role) for role in group)',
        "    return role_label(next(iter(group)))",
    ),
    (
        "G4",
        "缺组判定取反(有正面图反而报缺,没有反而放行)",
        SC,
        "    return tuple(group for group in REQUIRED_ROLE_GROUPS if not (set(group) & have))",
        "    return tuple(group for group in REQUIRED_ROLE_GROUPS if (set(group) & have))",
    ),
    # ------------------------------------------- S 组:双作用域
    (
        "S1",
        "颜色作用域不按颜色筛(每个颜色都拿到全 SPU 素材 —— D1 从后门回来)",
        SC,
        "        (a for a in assets if _in_variant(a, variant_id)),",
        "        assets,",
    ),
    (
        "S2",
        "未归属素材进每一个颜色子集(一张通用图证明了所有颜色都有正面照)",
        SC,
        '    return _text_or_none(getattr(asset, "color_variant_id", None)) == variant_id',
        '    return _text_or_none(getattr(asset, "color_variant_id", None)) in (variant_id, None)',
    ),
    (
        "S3",
        "SPU 作用域也按颜色筛(归属到颜色的图不再算进 SPU 门禁)",
        SC,
        "def gate_roles(\n"
        "    assets: Iterable[Any], *, imported_url_trusted: bool = False\n"
        ") -> frozenset[str]:",
        "def gate_roles(\n"
        "    assets: Iterable[Any], *, imported_url_trusted: bool = False\n"
        ") -> frozenset[str]:\n"
        "    assets = [a for a in assets if _in_variant(a, None)]",
    ),
    (
        "S4",
        "接线时分桶改读 variant_hint(A45-batch14-15 接线之后才存在的坏法)",
        SVC,
        "                    str(a.color_variant_id)\n"
        "                    for a in usable\n"
        '                    if getattr(a, "color_variant_id", None)',
        "                    str(a.variant_hint)\n"
        "                    for a in usable\n"
        '                    if getattr(a, "variant_hint", None)',
    ),
    # ------------------------------------------- C 组:接线与联动
    (
        "C1",
        "`_material_facts` 不接门禁,gate_roles 退回 usable_roles(判定验得再干净也白验)",
        SVC,
        "        gate_roles=sample_completeness.gate_roles(usable),",
        "        gate_roles=usable_roles,",
    ),
    (
        "C2",
        "新字段给宽松默认(漏填悄悄放行 —— 硬规则 4 第二次事故的形状)",
        FLOW,
        "    for group in gate.missing_role_groups(facts.gate_roles):",
        "    for group in gate.missing_role_groups(facts.gate_roles or facts.usable_roles):",
    ),
    (
        "C3",
        "待确认那条 issue 降级成 REMINDER(唯一能解开阻断的事在看板上一处都不显示)",
        FLOW,
        "                level=IssueLevel.NEEDS_CONFIRM,\n"
        '                code="MATERIAL_ROLE_UNCONFIRMED",',
        "                level=IssueLevel.REMINDER,\n"
        '                code="MATERIAL_ROLE_UNCONFIRMED",',
    ),
    (
        "C4",
        "并进 NO_USABLE_MATERIAL(建议是「补图或放行隔离素材」,两个动作都是错的)",
        BATCH,
        "    NextActionCode.CONFIRM_ASSET_ROLE: BatchErrorCode.ASSET_ROLE_UNCONFIRMED,",
        "    NextActionCode.CONFIRM_ASSET_ROLE: BatchErrorCode.NO_USABLE_MATERIAL,",
    ),
    (
        "C5",
        "下一步理由从 ref 重拼(判定说「或平铺图」、按钮说「缺少正面图」—— 坏循环回来)",
        FLOW,
        "        missing = [\n"
        '            i.message for i in material.issues if i.code == "MATERIAL_MISSING_REQUIRED_ROLE"\n'
        "        ]",
        "        missing = [\n"
        '            f"缺少{_role_label(i.ref)}"\n'
        "            for i in material.issues\n"
        '            if i.code == "MATERIAL_MISSING_REQUIRED_ROLE"\n'
        "        ]",
    ),
    (
        "C6",
        "有可确认角色时仍然推荐补图(点一下就能解开的事,运营被送去重新拍一张)",
        FLOW,
        "        if confirm is not None:\n"
        "            return action(NextActionCode.CONFIRM_ASSET_ROLE, confirm.message)\n",
        "",
    ),
    # ------------------------------------------- D 组:确定性
    (
        "D1",
        "可确认角色不排序(同一件商品刷新两次,提示语里的角色先后颠倒)",
        SC,
        "    return tuple(sorted(found))",
        "    return tuple(found)",
    ),
    (
        "D2",
        "组标签按集合迭代(同一句提示语两次刷新顺序不同,`ref` 跟着换一个)",
        SC,
        '    return "或".join(role_label(role) for role in group)',
        '    return "或".join(role_label(role) for role in set(group))',
    ),
]

SUITE_FILTER = "test_a45_batch14_10_sample_completeness.py"


def run_suite(cwd: Path, name_filter: str = SUITE_FILTER) -> tuple[int, str]:
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
            code, output = run_suite(work)
            failed = [
                line.split("::", 1)[1].strip()
                for line in output.splitlines()
                if "FAILED" in line and "::" in line
            ]
            crashed = any(
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
