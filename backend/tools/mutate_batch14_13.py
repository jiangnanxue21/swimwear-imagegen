#!/usr/bin/env python3
"""A45-batch14-13 变异验证:§11 第一行,按作用域的失败归集。

用法:python3 tools/mutate_batch14_13.py

## 这一批变异在防什么

§11 那一行要的三件事里,只有第三件没落地:「失败颜色明确列出,可单独重试该
颜色作用域」。而它的每一种坏法都**不会报错**,只会让运营在下一步多付一笔钱
或者少看见一个空颜色:

    重试范围算宽了      为已经答上来的颜色再买一次(A 组、R 组)
    重试范围算窄了/没算  B 色一条证据都没有,而没有任何地方说出来(F 组)
    作用域划错了        与指纹模块口径分叉,「这次识别覆盖了哪些图」有两个答案(S 组)

S 组最要紧的一条是 **S5**:把 `color_variant_id` 换成 `variant_hint`。
表上今天只有 hint,所以那是**最省事的接线方式**,而它让模型猜出来的值
决定重试范围 —— 14-9 与 14-10 两次拒绝接线的同一条理由,这里是第三次。
换过去之后本机一个断言都不会动(两列今天都取不到值),要等归属外键落库
那天才开始漏,那时代码和现在逐字相同。

W 组管接线。这一批的接线**今天一定给出空清单**(归属外键不存在),
所以 W 组每一条改完之后本机行为也不变 —— 它们守的是那一列落库那天的行为。

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

RS = "app/attributes/run_state.py"
SVC = "app/attributes/service.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------- F 组:哪个颜色算全军覆没
    (
        "F1",
        "判据放宽成「有失败就算」(5 张里失败 1 张的颜色也进重试清单,为 4 张已有的证据再付一次)",
        RS,
        "    return outcome.total > 0 and outcome.succeeded == 0",
        "    return outcome.total > 0 and outcome.failed > 0",
    ),
    (
        "F2",
        "判据收紧成「必须整批都失败」的写法漏掉 total(零图的颜色被算成失败,重试一个没有图的作用域)",
        RS,
        "    return outcome.total > 0 and outcome.succeeded == 0",
        "    return outcome.succeeded == 0",
    ),
    (
        "F3",
        "失败清单不排序(同一次 run 两次读出来的重试范围顺序不同,键跟着换)",
        RS,
        "    colours = sorted({r.scope for r in rows if r.scope})",
        "    colours = list({r.scope for r in rows if r.scope})",
    ),
    (
        "F4",
        "共享作用域也进失败清单(重试范围变成一个既不是全部、也不是某个颜色的东西)",
        RS,
        "    scopes = [shared]\n    failed: list[str] = []",
        "    scopes = [shared]\n"
        "    failed: list[str] = [] if shared.succeeded else [str(SHARED)]",
    ),
    # ------------------------------------------- R 组:重试范围
    (
        "R1",
        "没有失败颜色时也给一个重试范围(空重试:再跑一遍,再付一次,什么都没变)",
        RS,
        "    retry = canonical_scope(variant_ids=failed) if failed else None",
        "    retry = canonical_scope(variant_ids=failed)",
    ),
    (
        "R2",
        "重试范围改成 ALL(成功的那些颜色跟着再买一次)",
        RS,
        "    retry = canonical_scope(variant_ids=failed) if failed else None",
        "    retry = canonical_scope(all_variants=True) if failed else None",
    ),
    (
        "R3",
        "重试范围退回共享(整批重跑 —— §11 说的是「单独重试该颜色作用域」)",
        RS,
        "    retry = canonical_scope(variant_ids=failed) if failed else None",
        "    retry = canonical_scope() if failed else None",
    ),
    # ------------------------------------------- S 组:作用域怎么划
    (
        "S1",
        "共享作用域只算未归属的图(§6.3「共享字段跨全部证据合并」当场破)",
        RS,
        "    shared = _tally(rows, SHARED)",
        "    shared = _tally([r for r in rows if not r.scope], SHARED)",
    ),
    (
        "S2",
        "通用图进每个颜色子集(一张通用图失败,每个颜色都被算成有失败 —— D1 的形状)",
        RS,
        "        outcome = _tally([r for r in rows if r.scope == colour], colour)",
        "        outcome = _tally(\n"
        "            [r for r in rows if r.scope == colour or not r.scope], colour\n"
        "        )",
    ),
    (
        "S3",
        "整体状态另起一套算法(和 models/attribute.py 那个派生属性各说各话)",
        RS,
        "    return RunOutcome(\n        status=shared.status,",
        "    return RunOutcome(\n"
        "        status=(\n"
        "            ExtractionRunStatus.COMPLETED\n"
        "            if not failed\n"
        "            else ExtractionRunStatus.PARTIAL_SUCCESS\n"
        "        ),",
    ),
    (
        "S4",
        "作用域取值不做归一(空串与空格被当成一个真实的颜色 id)",
        RS,
        '    return _text_or_none(getattr(asset, "color_variant_id", None))',
        '    return getattr(asset, "color_variant_id", None)',
    ),
    (
        "S5",
        "改读 variant_hint(模型猜出来的值决定重试范围,而重试范围决定付多少钱)",
        RS,
        '    return _text_or_none(getattr(asset, "color_variant_id", None))',
        '    return _text_or_none(getattr(asset, "variant_hint", None))',
    ),
    (
        "S6",
        "属性访问代替 getattr(归属外键落库之前当场 AttributeError)",
        RS,
        '    return _text_or_none(getattr(asset, "color_variant_id", None))',
        "    return _text_or_none(asset.color_variant_id)",
    ),
    # ------------------------------------------- W 组:接线
    (
        "W1",
        "失败的图不进成绩单(整批只剩成功的,任何颜色都不会被算成全军覆没)",
        SVC,
        "            image_outcome = run_state.image_result(asset, succeeded=False)\n"
        "            outcomes.append(image_outcome)\n",
        "            image_outcome = run_state.image_result(asset, succeeded=False)\n",
    ),
    (
        "W2",
        "成功的图不进成绩单(每个颜色都被算成全军覆没,重试范围是全部)",
        SVC,
        "        image_outcome = run_state.image_result(asset, succeeded=True)\n"
        "        outcomes.append(image_outcome)\n",
        "        image_outcome = run_state.image_result(asset, succeeded=True)\n",
    ),
    (
        "W3",
        "归集算了但没人读(判定验得再干净也白验 —— 「算好了没人读」那一类)",
        SVC,
        "    row.failed_scopes = list(outcome.failed_scopes)\n",
        "    row.failed_scopes = []\n",
    ),
    (
        "W4",
        "日志不报重试范围(读日志的人得自己拼一次,拼宽一档就是再付一次钱)",
        SVC,
        '                    "retry_scope": outcome.retry_scope.token,\n',
        "",
    ),
    (
        "W5",
        "成绩单按成败建而不是按图建(失败那条丢掉 asset,作用域信息当场没了)",
        SVC,
        "            image_outcome = run_state.image_result(asset, succeeded=False)",
        "            image_outcome = run_state.ImageResult(scope=None, succeeded=False)",
    ),
]

SUITE_FILTER = "test_a45_batch14_13_scope_outcome.py"


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
            code, output = run_suite(work)
            failed = [
                line.split("::", 1)[1].strip()
                for line in output.splitlines()
                if "FAILED" in line and "::" in line
            ]
            # 「导入期就炸了」的检测:**不能**只在整段输出里找异常名字。
            #
            # S6 那条变异(把 getattr 换成属性访问)的**正确**失败方式就是
            # 一条 AttributeError —— 它由守卫在用例内部触发,是一次真红。
            # 原来那版按子串扫全段输出,把它误报成「不算数」。
            #
            # 判据改成结构性的:套件正常跑完(打印了统计行)且点得出是哪条
            # 用例红的,就说明它没有炸在导入期。
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
