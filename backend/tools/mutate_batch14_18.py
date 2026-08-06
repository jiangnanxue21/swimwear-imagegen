#!/usr/bin/env python3
"""A45-batch14-18 的变异验证:每一条守卫是不是真的拦得住它声称拦的东西。

## 为什么这一批格外需要变异

本批守的是**钱**,而钱这一类的错误有一个共同形状:**错了不报错**。
少记一半的额度不会让任何测试变红、不会让任何接口报 500,只会让预算横幅
一路绿着而账单是它的好几倍。这种缺陷唯一的发现方式是有人月底去对账 ——
也就是"几个月后才发现"。

所以这里逐条问的是:把修好的那行改回去(或改成一个看起来同样合理的写法),
守卫会不会红。不会红的守卫,等于本批什么都没验。

## 一条口径

`SUITE_FILTER` 必须是**精确文件名**,不是子串。子串会连带跑上别的套件,
于是一条变异可以因为**别人的**守卫而变红,而报告记在自己头上 ——
见 `tools/suite_filter.py` 顶部。

## 用法

    python3 tools/mutate_batch14_18.py

不进 CI:它要复制几十份工作树。锚点会不会过期由 `tools/audit_anchors.py`
每次门禁都查一遍(零子进程,不到一秒)。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

BASE = "app/providers/base.py"
FASHN = "app/providers/fashn.py"
MODEL = "app/models/generation.py"
LEDGER = "app/services/generation_service.py"
TASKS = "app/tasks/generation_tasks.py"
MIGRATION = "migrations/versions/0039_usage_units_source.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # -------------------------------------------------- 谁的数说了算
    (
        "B1",
        "推算值优先于厂商自述(接了线、解析了响应头,而台账数字一个没变)",
        BASE,
        "    if usage is not None and usage.units is not None:\n"
        "        return max(int(usage.units), 0), UnitsSource.PROVIDER.value\n"
        "    return max(int(inferred), 0), UnitsSource.INFERRED.value",
        "    return max(int(inferred), 0), UnitsSource.INFERRED.value",
    ),
    (
        "B2",
        "退回推算时仍标 provider(一张全是估算的表冒充和账单同源)",
        BASE,
        "    return max(int(inferred), 0), UnitsSource.INFERRED.value",
        "    return max(int(inferred), 0), UnitsSource.PROVIDER.value",
    ),
    (
        "B3",
        "把厂商报的 0 当成「没报」(一次真的免费的调用被记了一笔钱)",
        BASE,
        "    if usage is not None and usage.units is not None:",
        "    if usage is not None and usage.units:",
    ),
    (
        "B4",
        "不再夹负数(负额度一路走到 cost_for(),收尾动作自己炸在写流水的路上)",
        BASE,
        "        return max(int(usage.units), 0), UnitsSource.PROVIDER.value",
        "        return int(usage.units), UnitsSource.PROVIDER.value",
    ),
    (
        "B5",
        "基类默认改成「厂商报了候选数」(新 Provider 忘了覆写就冒充账单同源)",
        BASE,
        "        return ProviderUsage()\n\n    async def cancel",
        "        return ProviderUsage(units=len(candidates))\n\n    async def cancel",
    ),
    # -------------------------------------------------- FASHN:去重
    (
        "F1",
        "按张数求和而不是按 prediction 去重(num_images=4 那次记 4 倍的账)",
        FASHN,
        "            per_prediction[str(prediction_id)] = used",
        "            per_prediction[f'{prediction_id}#{len(per_prediction)}'] = used",
    ),
    (
        "F2",
        "去重去过头,只取第一条(product-to-model 那种四次提交只记 1/4 的钱)",
        FASHN,
        "        return ProviderUsage(\n            units=sum(per_prediction.values()),",
        "        return ProviderUsage(\n            units=next(iter(per_prediction.values())),",
    ),
    (
        "F3",
        "缺额度的那条当 0 跳过(求和结果偏小,而它看起来和真数一模一样)",
        FASHN,
        "            if not prediction_id or not isinstance(used, int):",
        "            if not prediction_id:\n                continue\n            if not isinstance(used, int):",
    ),
    (
        "F4",
        "一张候选都没有时报 0(把「没读到」冒充成厂商说的「没收钱」)",
        FASHN,
        '            return ProviderUsage(detail={"reason": "no_candidates"})',
        '            return ProviderUsage(units=0, detail={"reason": "no_candidates"})',
    ),
    (
        "F5",
        "响应头名字改错一个字母(解析恒为 None,台账安静退回本批之前的样子)",
        FASHN,
        'CREDITS_HEADER = "x-fashn-credits-used"',
        'CREDITS_HEADER = "x-fashn-credits-use"',
    ),
    # -------------------------------------------------- 接线
    (
        "W1",
        "接线绕过判定,直接塞推算值(解析响应头的代码还在,只是又一次没人读)",
        TASKS,
        "    usage = provider.usage_from_candidates(candidates)\n"
        "    billable_units, units_source = settle_billable_units(\n"
        "        usage, inferred=max(outcome.provider_count, 1)\n"
        "    )",
        "    billable_units = max(outcome.provider_count, 1)\n"
        "    units_source = UnitsSource.INFERRED.value\n"
        "    usage = provider.usage_from_candidates(candidates)",
    ),
    (
        "W2",
        "流水只传金额不传来源(数对了、来源标错,比两个都错更难查)",
        TASKS,
        "        billable_units=billable_units,\n        units_source=units_source,",
        "        billable_units=billable_units,",
    ),
    (
        "W3",
        "按落库成功的张数记账(三张里坏一张记两张的账,而钱按三张收)——"
        "12-4 那条守卫本批一般化过,这里验它还咬得住",
        TASKS,
        "    usage = provider.usage_from_candidates(candidates)\n"
        "    billable_units, units_source = settle_billable_units(\n"
        "        usage, inferred=max(outcome.provider_count, 1)\n"
        "    )",
        "    usage = provider.usage_from_candidates(candidates)\n"
        "    billable_units, units_source = settle_billable_units(\n"
        "        usage, inferred=max(len(stored), 1)\n"
        "    )",
    ),
    # -------------------------------------------------- 台账
    (
        "L1",
        "record_usage 的来源默认改成 provider(四类没接线的调用全部自称权威)",
        LEDGER,
        "    units_source: str = UnitsSource.INFERRED.value,",
        "    units_source: str = UnitsSource.PROVIDER.value,",
    ),
    (
        "L2",
        "更新路径不改来源(恢复那一遍拿到真数,而来源留在 inferred)",
        LEDGER,
        "        record.units_source = units_source\n",
        "",
    ),
    # -------------------------------------------------- 列
    (
        "M1",
        "ORM 丢掉 CHECK(create_all 建的表和迁移建的表不一样,autogenerate 会提议 DROP)",
        MODEL,
        '            name="ck_provider_usage_records_units_source",',
        '            name="ck_usage_units_source",',
    ),
    (
        "M2",
        "迁移的存量默认改成 provider(替过去的账做一个今天才定下来的判断)",
        MIGRATION,
        '_INFERRED = "inferred"',
        '_INFERRED = "provider"',
    ),
    (
        "M3",
        "迁移重新挂到更早的节点(照样是单 head,而两条线的列会互相盖掉)",
        MIGRATION,
        'down_revision: Union[str, None] = "0038"',
        'down_revision: Union[str, None] = "0037"',
    ),
    (
        "M4",
        "迁移改成按条件挑行回填(存量来源变成一个猜出来的判断)",
        MIGRATION,
        "    op.create_check_constraint(",
        '    op.execute("UPDATE provider_usage_records SET units_source = \'provider\' '
        "WHERE billable_units > 1\")\n"
        "    op.create_check_constraint(",
    ),
]

SUITE_FILTER = "test_a45_batch14_18_provider_usage.py"

#: W3 打的是 12-4 那条被本批一般化过的守卫,它不在上面那个文件里。
#: 单独给它一个过滤器 —— 拿子串一次跑两个文件的话,W3 的颜色会由
#: **哪一条守卫先红**决定,而报告只会记一个编号。
EXTRA_FILTER = {"W3": "test_a45_batch12_4_fixes.py"}


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
            # 判据和 14-17 同源:套件正常跑完(打印了统计行)且点得出是哪条
            # 用例红的,就说明它没有炸在导入期。只按异常名字扫全段输出的话,
            # 由守卫在用例内部触发的 AttributeError 会被误报成「不算数」。
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
