#!/usr/bin/env python3
"""A45-batch14-12 变异验证:识别 run 的终态、取消与幂等键(§4.6 / §9.2)。

用法:python3 tools/mutate_batch14_11.py

## 这一批变异在防什么

本批的判定分成**已接线**与**未接线**两半,两半的失效方式完全不同,
所以变异也分两类。

T / A / W 组打的是**今天就在跑**的那一半(终态派生 + 谁算数 + 接线)。
它们的共同形状是:改完之后本机一切照旧、接口照样 200,只是运营看到的那句话
从「跑了一半」变成「识别完成」。这一半此前根本没有后端判定 —— 三档全在
前端算,而前端算漏了一种,所以 T1 是本批的**病灶复现**:它把前端那个 bug
原样搬进后端。

R / K / S 组打的是**还没接线**的那一半(§9.2 幂等)。它们今天一条真实数据
都影响不了 —— 五个列还不存在。但判定要在接线之前验完:真库用例池里已经
压着 60 条没跑过的了,再往里加一条等于把它推给下一台机器。而这一半的
失效方式尤其安静,K 组每一条的后果都是**同一件事付两次钱**,
而账单不会告诉你哪一笔是重复的。

K9 / S5 是两条「碰撞」变异,专门为这个仓库反复栽过的那个坑写:
**构造了一对不同的输入,不等于构造了一对会碰撞的输入**(batch14-9 的 H1)。
S5 尤其值得看 —— 它把作用域成员判定改回在 token 里找子串,而那正是
「文件里出现过这串字 ≠ 这件事成立」换到数据编码上的样子。

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
MODEL = "app/models/attribute.py"
SVC = "app/attributes/service.py"
API = "app/api/attributes.py"
SCHEMA = "app/schemas/attribute.py"
TAB = "../frontend/src/components/workbench/AttributeTab.tsx"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------- T 组:终态判定(已接线)
    (
        "T1",
        "没跑完但一张没失败 -> COMPLETED(把前端那个 bug 原样搬进后端)",
        RS,
        "        return S.PARTIAL_SUCCESS if succeeded > 0 else S.FAILED",
        "        pass",
    ),
    (
        "T2",
        "零张图的 run 宣布成功(一个没有输入的 run 带着「成功」进事实)",
        RS,
        "    if image_count == 0:\n        return S.FAILED",
        "    if image_count == 0:\n        return S.COMPLETED",
    ),
    (
        "T3",
        "全部失败判 PARTIAL_SUCCESS(0 条证据的 run 被当成算数的)",
        RS,
        "    if succeeded == 0:\n        return S.FAILED\n    if failed == 0:",
        "    if succeeded == 0:\n        return S.PARTIAL_SUCCESS\n    if failed == 0:",
    ),
    (
        "T4",
        "有失败也判 COMPLETED(部分失败被当成完整,缺的图没人回来补)",
        RS,
        "    if failed == 0:\n        return S.COMPLETED\n    return S.PARTIAL_SUCCESS",
        "    return S.COMPLETED",
    ),
    (
        "T5",
        "计数自相矛盾时不再兜底(处理数比总数还多的 run 一路走到 COMPLETED)",
        RS,
        "    if succeeded < 0 or failed < 0 or image_count < 0 or processed > image_count:",
        "    if False:",
    ),
    (
        "T6",
        "喊得太晚也算取消(把已经买到手、已经付过钱的结果扔掉)",
        RS,
        "    if cancel_requested and processed < image_count:",
        "    if cancel_requested:",
    ),
    # ------------------------------------------- A 组:哪几档算数
    (
        "A1",
        "CANCELLED 进合并集合(被人叫停的那次识别照样改写事实)",
        RS,
        "AUTHORITATIVE_STATUSES: frozenset[ExtractionRunStatus] = frozenset(\n"
        "    {S.COMPLETED, S.PARTIAL_SUCCESS}\n"
        ")",
        "AUTHORITATIVE_STATUSES: frozenset[ExtractionRunStatus] = frozenset(\n"
        "    {S.COMPLETED, S.PARTIAL_SUCCESS, S.CANCELLED}\n"
        ")",
    ),
    (
        "A2",
        "PARTIAL_SUCCESS 移出合并集合(成功那几张的证据一条都不用,钱白付)",
        RS,
        "AUTHORITATIVE_STATUSES: frozenset[ExtractionRunStatus] = frozenset(\n"
        "    {S.COMPLETED, S.PARTIAL_SUCCESS}\n"
        ")",
        "AUTHORITATIVE_STATUSES: frozenset[ExtractionRunStatus] = frozenset(\n"
        "    {S.COMPLETED}\n"
        ")",
    ),
    (
        "A3",
        "没有状态时放行(状态列还没落地的存量行一律当成算数的)",
        RS,
        "    if status is None:\n        return False",
        "    if status is None:\n        return True",
    ),
    # ------------------------------------------- R 组:复用判定与占键
    (
        "R1",
        "PARTIAL_SUCCESS 判复用(失败那几张永远不再重试,「按颜色重试」落空)",
        RS,
        "    if status is S.COMPLETED:\n        return ReuseVerdict.REUSE_RESULT",
        "    if status in (S.COMPLETED, S.PARTIAL_SUCCESS):\n"
        "        return ReuseVerdict.REUSE_RESULT",
    ),
    (
        "R2",
        "RUNNING 判新建(双击当场产生第二个 run,同一批图付两次钱)",
        RS,
        "    if status in (S.QUEUED, S.RUNNING):\n        return ReuseVerdict.RETURN_EXISTING",
        "    if status in (S.QUEUED,):\n        return ReuseVerdict.RETURN_EXISTING",
    ),
    (
        "R3",
        "占键名单改成手写全集(一次失败之后同样的输入再也建不出第二个 run)",
        RS,
        "KEY_OCCUPYING_STATUSES: frozenset[ExtractionRunStatus] = frozenset(\n"
        "    s for s in ExtractionRunStatus if reuse_verdict(s) != ReuseVerdict.NEW_RUN\n"
        ")",
        "KEY_OCCUPYING_STATUSES: frozenset[ExtractionRunStatus] = frozenset(\n"
        "    ExtractionRunStatus\n"
        ")",
    ),
    (
        "R4",
        "唯一索引退回全表唯一(谓词没了,失败的行照样占着键)",
        RS,
        '    values = ", ".join(f"\'{s.value}\'" for s in sorted(KEY_OCCUPYING_STATUSES))\n'
        '    return f"{column} IN ({values})"',
        '    return "TRUE"',
    ),
    (
        "R5",
        "终态集合漏掉 PARTIAL_SUCCESS(它变成中间态,重试会写回同一行)",
        RS,
        "TERMINAL_STATUSES: frozenset[ExtractionRunStatus] = frozenset(\n"
        "    {S.COMPLETED, S.PARTIAL_SUCCESS, S.FAILED, S.CANCELLED}\n"
        ")",
        "TERMINAL_STATUSES: frozenset[ExtractionRunStatus] = frozenset(\n"
        "    {S.COMPLETED, S.FAILED, S.CANCELLED}\n"
        ")",
    ),
    (
        "R6",
        "终态还能被取消(一个跑完并已合并的 run 被改成不算数,钱不会回来)",
        RS,
        "    S.COMPLETED: frozenset(),",
        "    S.COMPLETED: frozenset({S.CANCELLED}),",
    ),
    # ------------------------------------------- K 组:幂等键本体
    (
        "K1",
        "字段清单不排序(集合迭代序接管 —— 同一个请求在两个 worker 上两个键)",
        RS,
        "    fields = sorted({f for f in (_text_or_none(x) for x in requested_fields) if f})",
        "    fields = list({f for f in (_text_or_none(x) for x in requested_fields) if f})",
    ),
    (
        "K2",
        "字段清单不去重(同一个字段传两遍就换一个键,重复请求变成第二次付费)",
        RS,
        "    fields = sorted({f for f in (_text_or_none(x) for x in requested_fields) if f})",
        "    fields = sorted([f for f in (_text_or_none(x) for x in requested_fields) if f])",
    ),
    (
        "K3",
        "空 model_version 放行(键少一维,换了模型还在复用旧结果)",
        RS,
        '    if not model:\n'
        '        raise ValueError(\n'
        '            "幂等键必须带 model_version,而且要取配置里的那个 —— "\n'
        '            "取响应里的话,键只有在付过钱之后才算得出来(见模块文档第三节)"\n'
        '        )',
        "    model = model or \"\"",
    ),
    (
        "K4",
        "空 prompt_version 放行(改了提示词的那一次会命中旧 run)",
        RS,
        '    if not prompt:\n        raise ValueError("幂等键必须带 prompt_version")',
        '    prompt = prompt or ""',
    ),
    (
        "K5",
        "字段清单为空时放行(一个什么都不问的 run 也能拿到键)",
        RS,
        '    if not fields:\n'
        '        raise ValueError("幂等键必须带至少一个字段:一个什么都不问的 run 没有意义")',
        "    fields = fields or []",
    ),
    (
        "K6",
        "键不带作用域(共享和某个颜色算出同一个键 —— 拿 A 色结果当共享事实)",
        RS,
        "        length_prefixed(spu),\n        scope.token,",
        "        length_prefixed(spu),",
    ),
    (
        "K7",
        "键不带指纹(补一张图之后照样命中旧 run,新图一条证据都不会有)",
        RS,
        "    for name, digest in _scope_fingerprints(scope, fingerprints):\n"
        "        parts.append(length_prefixed(name))\n"
        "        parts.append(length_prefixed(digest))",
        "    _scope_fingerprints(scope, fingerprints)",
    ),
    (
        "K8",
        "拼接不用长度前缀(可构造碰撞 —— 两个不同的输入拿到同一个键)",
        RS,
        "    for name in fields:\n        parts.append(length_prefixed(name))",
        '    parts.append("|".join(fields))',
    ),
    (
        "K9",
        "键版本不进键(改了算法之后新旧键互相比较,全库 run 一次性认不出来)",
        RS,
        "        length_prefixed(KEY_VERSION),\n",
        "",
    ),
    # ------------------------------------------- S 组:作用域
    (
        "S1",
        "颜色 id 不排序(集合迭代序接管,同一个请求两个键)",
        RS,
        "    wanted = tuple(sorted(set(ids)))",
        "    wanted = tuple(set(ids))",
    ),
    (
        "S2",
        "ALL 归一成「当前全部颜色」(加了新颜色之后再点识别会命中旧 run 直接复用)",
        RS,
        "    if all_variants:\n"
        "        return Scope(kind=SCOPE_ALL, token=length_prefixed(SCOPE_ALL))",
        "    if all_variants and not ids:\n"
        "        return Scope(kind=SCOPE_SHARED, token=length_prefixed(SCOPE_SHARED))",
    ),
    (
        "S3",
        "颜色作用域也带上共享指纹(补一张通用图 stale 掉每个颜色 —— D1 从后门回来)",
        RS,
        "    else:\n        wanted = list(scope.variant_ids)",
        "    else:\n        wanted = [SHARED, *scope.variant_ids]",
    ),
    (
        "S4",
        "作用域成员判定改回 token 子串(id 长成 `x1:a` 就能偷到颜色 a 的指纹)",
        RS,
        "    else:\n        wanted = list(scope.variant_ids)",
        "    else:\n"
        "        wanted = [\n"
        "            k\n"
        "            for k in sorted(fingerprints, key=lambda k: \"\" if k is SHARED else str(k))\n"
        "            if k is not SHARED and length_prefixed(k) in scope.token\n"
        "        ]",
    ),
    (
        "S5",
        "指纹缺了就跳过(键少一维,而少的那一维正是这次要识别的颜色)",
        RS,
        "        if not value:",
        "        if False:",
    ),
    # ------------------------------------------- W 组:接线
    (
        "W1",
        "写入方自己写三元,不调判定(第二份判定,和纯层那份各说各话)",
        SVC,
        "    row.status = run_state.terminal_status_for(",
        '    row.status = "FAILED" if succeeded == 0 else (\n'
        '        "COMPLETED" if not failed else "PARTIAL_SUCCESS"\n'
        "    )\n"
        "    _unused = run_state.terminal_status_for(",
    ),
    (
        "W2",
        "合并判据退回 `succeeded_count > 0`(判定验得再干净也白验)",
        API,
        "    if run_state.run_is_authoritative(extraction.status):",
        "    if (extraction.succeeded_count or 0) > 0:",
    ),
    (
        "W3",
        "全部失败不写 error_code(失败的 run 在库里和成功的长得一模一样)",
        SVC,
        "    if row.status == ExtractionRunStatus.FAILED.value and not row.error_code:\n"
        "        row.error_code = EXTRACTION_ALL_FAILED",
        "    pass",
    ),
    (
        "W4",
        "出参默认值改成 COMPLETED(漏字段时悄悄放行 —— 硬规则 4 第二次事故的形状)",
        SCHEMA,
        '    status: str = "FAILED"',
        '    status: str = "COMPLETED"',
    ),
    (
        "W5",
        "前端改回自己算三档(硬规则 4 原样复发,而且照旧漏掉「没跑完」那一种)",
        TAB,
        "      const notice = RUN_STATUS_NOTICE[result.status] ?? RUN_STATUS_NOTICE.FAILED",
        "      const notice =\n"
        "        (result.succeeded_count ?? 0) === 0\n"
        "          ? RUN_STATUS_NOTICE.FAILED\n"
        "          : (result.failed_count ?? 0) > 0\n"
        "            ? RUN_STATUS_NOTICE.PARTIAL_SUCCESS\n"
        "            : RUN_STATUS_NOTICE.COMPLETED",
    ),
]

SUITE_FILTER = "test_a45_batch14_12_run_state.py"


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
