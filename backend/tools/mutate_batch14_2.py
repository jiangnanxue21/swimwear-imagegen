#!/usr/bin/env python3
"""A45-batch14-2 变异验证:把每条守卫对应的决定改回去,确认它真的变红。

用法:python3 tools/mutate_batch14_2.py

机制与 `tools/mutate_batch14.py` 相同(复制工作树 -> 改源码 -> 跑本批守卫),
理由也相同,不再重复。这里只记与上一份不同的地方:

## 本批**必须**验的是前端那三条

batch13-3 的 M11 第一版是 GREEN:守卫的正则没锚在 JSX 的开括号上,
`{false && identityShadowed.length > 0 && (` 照样匹配 —— 渲染死了、守卫还绿。
本批又往前端加了一个渲染块,于是同一个陷阱原样摆在这里。
N9 / N10 / N11 就是照着那个形状造的:**把渲染死代码化、把早退改成恒真、
把分组的过滤条件拿掉**,三种都能让界面上一个字都不显示。
写守卫的时候先把这三条变异想清楚,再决定断言长什么样 ——
反过来做(先写断言再补变异)就是 M11 的来路。

## 锚点会过期,这是常态

F3 重构 `call_budget` 时一次让 `mutate_batch14.py` 的 M33/M34 同时失锚。
改被变异的源码时,连着更新变异脚本 —— 失锚的变异不报错、不变红,
只是安静地什么都没验。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

FRONTEND_TAB = "../frontend/src/components/workbench/AttributeTab.tsx"
FRONTEND_API = "../frontend/src/api/attributes.ts"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ---------------------------------------------------------- F1 一图一票
    (
        "N1",
        "去掉去重(退回边读边落,一张图能投多票)",
        "app/extractors/evidence_plan.py",
        "        if all(item.value == items[0].value for item in items[1:]):",
        "        if True:",
    ),
    (
        "N2",
        "同名同值也计入编造(污染 §6.3 的指标)",
        "app/extractors/evidence_plan.py",
        "            evidence.append(items[0])\n            continue\n        fabricated_names.extend",
        "            evidence.append(items[0])\n        fabricated_names.extend",
    ),
    (
        "N3",
        "同名异值时挑第一个值(替模型做决定)",
        "app/extractors/evidence_plan.py",
        "        evidence.append(\n            PlannedEvidence(\n                field_name=name, missing_reason=REASON_INSUFFICIENT_EVIDENCE\n            )\n        )",
        "        evidence.append(items[0])",
    ),
    (
        "N4",
        "去重丢掉出现顺序(按字典序输出)",
        "app/extractors/evidence_plan.py",
        "    for name in order:",
        "    for name in sorted(by_name):",
    ),
    (
        "N5",
        "规则 4 被新去重挤掉(说看不清时仍然落值)",
        "app/extractors/evidence_plan.py",
        "        if name in no_value:\n            # 规则 4",
        "        if False:\n            # 规则 4",
    ),
    # ---------------------------------------------------------- F3 生效上限
    (
        "N6",
        "提示语报入参而不是生效上限(判定 12、提示 0)",
        "app/extractors/call_budget.py",
        "    limit = effective_ceiling(ceiling)",
        "    limit = ceiling",
    ),
    (
        "N7",
        "生效上限把 0 当成不限",
        "app/extractors/call_budget.py",
        "    return ceiling if ceiling > 0 else DEFAULT_MAX_IMAGES_PER_RUN",
        "    return ceiling",
    ),
    # ---------------------------------------------------------- F4 全树不变式
    (
        "N8",
        "已知那条关系去掉 passive_deletes(不变式必须自己发现它)",
        "app/models/spu.py",
        '        passive_deletes="all",\n',
        "",
    ),
    (
        "N9",
        'passive_deletes 改成 True(不够:已加载子行仍被置 NULL)',
        "app/models/spu.py",
        '        passive_deletes="all",',
        "        passive_deletes=True,",
    ),
    # ---------------------------------------------------------- F2 无值上屏
    (
        "N10",
        "渲染块死代码化(batch13-3 / M11 的原样重演)",
        FRONTEND_TAB,
        # 锚点跟着挂载点从单行改成多行(组件多收了一个 `labelOf`)。
        # 变异**意图一字未改**:把渲染块死代码化,验的仍然是守卫认不认得出
        # `{false && ...}`。改锚点而不是删变异 —— 失锚的变异不报错、不变红,
        # 只是安静地什么都没验,那正是 `audit_anchors.py` 存在的理由
        "      <MissingEvidenceNotice",
        "      {false && <MissingEvidenceNotice",
    ),
    (
        "N11",
        "组件早退改成恒真(定义还在,一个字都不显示)",
        FRONTEND_TAB,
        "  if (!byReason.length) return null",
        "  if (true) return null",
    ),
    (
        "N12",
        "分组不再按 missing_reason 过滤(带值证据被当成'没有值'报出来)",
        FRONTEND_TAB,
        "      if (!item.missing_reason) continue",
        "      if (false) continue",
    ),
    (
        "N13",
        "不再拉逐图证据(属性行上没有这个信号,组件永远收到空数组)",
        FRONTEND_TAB,
        "    queryFn: () => attributesApi.extraction(lastExtractionId as string),",
        "    queryFn: async () => ({ evidence: [] }),",
    ),
    (
        "N14",
        "三个原因合并成一句'没识别出来'",
        FRONTEND_API,
        "  NOT_VISUALLY_DETERMINABLE: {\n    label: '这个角度判断不了',\n    action: '补一张对应角度的图(背面 / 细节 / 吊牌)',",
        "  NOT_VISUALLY_DETERMINABLE: {\n    label: '这个角度判断不了',\n    action: '换一张更清楚的图再识别',",
    ),
    (
        "N15",
        "前端类型里删掉 missing_reason",
        FRONTEND_API,
        "  missing_reason: string | null",
        "  // missing_reason: string | null",
    ),
    (
        "N16",
        "逐图证据的失败分支拆掉(拉不到时静默渲染成'什么都没有')",
        FRONTEND_TAB,
        "      {lastExtraction.isError && (",
        "      {false && (",
    ),
]

SUITE_FILTER = "test_a45_batch14_2_fixes.py"


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
                # **不唯一就拒改。**改了也不知道改的是哪一处 ——
                # batch13-3 的 M2 就是被这条拦下的(注释里也含那串字)
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
