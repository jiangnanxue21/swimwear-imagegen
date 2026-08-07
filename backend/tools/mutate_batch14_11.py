#!/usr/bin/env python3
"""A45-batch14-11 变异验证:§11 的两条新场景(确认队列口径 + AI 图伪装拦截)。

用法:python3 tools/mutate_batch14_11.py

## 这一批变异在防什么

两条修的都是「今天不报错、将来才漏」的形状,而且**触发它们的不是代码改动,
是外部事实发生**:

    Q / F 组   真实抽取器接上、而校准分箱是空的那一天
    P / W 组   运营第一次把生成好的图下载下来当样品传回去那一天

Q 组是确认队列的口径本体。`decision.decide_status` 未校准时返回的正是
`CANDIDATE`,所以真实模型接上的第一天**每一个字段都是 CANDIDATE** ——
那时把 CANDIDATE 放回确认队列,判定不会报错、界面会说「有建议值待确认」,
而队列里一个可点的都没有。变异 Q1 就是那一天的原样复现。

F 组是 flow 那一侧。它和 Q 组必须分别有变异:判定分好了桶而 flow 又把两桶
合起来读,与 flow 读对了而桶分错了,是两个独立的坏法,**任何一个单独发生
都足以让 §11 那条规则失效**。

P 组是溯源判定。这一组最值得看的是 P1:今天真正会命中的信号只有
`legacy_kind`(溯源列是阶段 2 的,还没落库)。去掉它之后这个模块一条也拦不住,
而**两侧的测试都不看这件事** —— 那正是 batch14-10 第二节描述的
「靠尚未发生的事没出事」原样重演一遍。

W 组是接线。W1 是本批最贵的一条:把取数范围从 SPU 缩回去重键之后,
「同 SPU 不同 SKU」那一路整条失效 —— 而那正是这条流水线上最常发生的走法,
后果是一张 AI 图变成 `PRODUCT_EVIDENCE` 直接进识别输入,每张一次真实付费调用。

D 组管确定性。它不会让任何断言变假,只会让同一件商品刷新两次显示不同的
字段先后 —— 谁都不会把这件事报成 bug,只会觉得这个界面有点飘。
与 batch14-10 的 D1 / batch14-9 的 H3 是同一个教训。

## 为什么变异先写

反过来做就是 batch13-3 / M11 的来路:守卫写在实现之后,它守的是
「我刚才写了什么」,不是「哪里会坏」。本批的清单是照着上面五段的失效分析
列出来的,守卫按这张清单的形状写。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

QP = "app/attributes/queue_policy.py"
PC = "app/media/provenance_conflict.py"
FLOW = "app/workbench/flow.py"
WSVC = "app/workbench/service.py"
MSVC = "app/media/service.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # --------------------------------------- Q 组:确认队列口径(§11 本体)
    (
        "Q1",
        "CANDIDATE 重新算「可确认」(真实模型接上、校准为空那天的原样复现)",
        QP,
        "    AttributeStatus.CANDIDATE.value: Disposition.EVIDENCE_ONLY,",
        "    AttributeStatus.CANDIDATE.value: Disposition.CONFIRMABLE,",
    ),
    (
        "Q2",
        "CONFIRM_QUEUE 放进 EVIDENCE_ONLY(换个地方把两桶合起来)",
        QP,
        "CONFIRM_QUEUE: frozenset[Disposition] = frozenset(\n"
        "    {Disposition.CONFIRMABLE, Disposition.ADJUDICABLE}\n"
        ")",
        "CONFIRM_QUEUE: frozenset[Disposition] = frozenset(\n"
        "    {Disposition.CONFIRMABLE, Disposition.ADJUDICABLE, Disposition.EVIDENCE_ONLY}\n"
        ")",
    ),
    (
        "Q3",
        "认不出的状态兜底成 DISMISSED(拼错的状态字符串让字段静默消失)",
        QP,
        "    return DISPOSITION_BY_STATUS.get(_text(status), Disposition.EVIDENCE_ONLY)",
        "    return DISPOSITION_BY_STATUS.get(_text(status), Disposition.DISMISSED)",
    ),
    (
        "Q4",
        "REJECTED 重新进确认队列(人工拒绝过的值再问一遍)",
        QP,
        "    AttributeStatus.REJECTED.value: Disposition.DISMISSED,",
        "    AttributeStatus.REJECTED.value: Disposition.CONFIRMABLE,",
    ),
    (
        "Q5",
        "CONFLICT 从归档表里消失(它落进兜底,冲突字段从裁决队列不见了)",
        QP,
        "    AttributeStatus.CONFLICT.value: Disposition.ADJUDICABLE,\n",
        "",
    ),
    (
        "Q6",
        "分桶表把「不采信」指到「已处理」桶(那批字段从阻断清单里静默消失)",
        QP,
        '    Disposition.EVIDENCE_ONLY: "evidence_only",',
        '    Disposition.EVIDENCE_ONLY: "dismissed",',
    ),
    (
        "Q7",
        "confirm_queue 只算 confirmable(冲突字段不再算在队列里)",
        QP,
        "        return self.confirmable | self.adjudicable",
        "        return self.confirmable",
    ),
    # --------------------------------------- F 组:flow 的属性步
    (
        "F1",
        "问题级别重新把 CANDIDATE 并进「有建议值待确认」(本批修的那条回来)",
        FLOW,
        "        if name in facts.suggested:\n"
        "            issues.append(\n"
        "                Issue(\n"
        "                    level=IssueLevel.NEEDS_CONFIRM,\n"
        '                    code="ATTR_NOT_CONFIRMED",',
        "        if name in facts.suggested or name in facts.candidate_only:\n"
        "            issues.append(\n"
        "                Issue(\n"
        "                    level=IssueLevel.NEEDS_CONFIRM,\n"
        '                    code="ATTR_NOT_CONFIRMED",',
    ),
    (
        "F2",
        "步骤状态重新把 CANDIDATE 算成「等人点头」(完成度虚高到 0.6)",
        FLOW,
        # A45-batch14-21 重锚:那一行加进了 `or m in stale_confirmed`
        # (过期字段算「等人点头」,理由在 flow.py 那段注释里)。
        # **锚点必须跟着走** —— 失锚的变异不报错、不变红,只是安静地什么
        # 都没验,而这条 F2 钉的是 §11 修过的那个具体缺陷
        "            if all(m in facts.suggested or m in stale_confirmed for m in missing)",
        "            if all(\n"
        "                m in facts.suggested or m in stale_confirmed or m in facts.candidate_only\n"
        "                for m in missing\n"
        "            )",
    ),
    (
        "F3",
        "「有证据但不采信」降成 NEEDS_CONFIRM(它不阻断了,商品能一路走到导出)",
        FLOW,
        "                    level=IssueLevel.BLOCKING,\n"
        '                    code="ATTR_EVIDENCE_ONLY",',
        "                    level=IssueLevel.NEEDS_CONFIRM,\n"
        '                    code="ATTR_EVIDENCE_ONLY",',
    ),
    (
        "F4",
        "「有证据但不采信」复用 ATTR_NOT_CONFIRMED 码(两条动线在看板上合并)",
        FLOW,
        '                    code="ATTR_EVIDENCE_ONLY",',
        '                    code="ATTR_NOT_CONFIRMED",',
    ),
    (
        "F5",
        "不采信那条的 hint 改成「先跑一次属性识别」(把运营送去花那笔钱)",
        FLOW,
        '                        "模型给过值,但这个(字段 × 模型 × Prompt)还没校准、"\n'
        '                        "或置信度低于下限,系统按 fail-closed 不采信。"\n'
        '                        "再跑一次识别得到的是同一批证据,请人工直接填写"',
        '                        "先跑一次属性识别,或人工直接填写"',
    ),
    (
        "F6",
        "下一步不再区分「可确认」与「要填写」(FILL_ATTRIBUTES 永远走不到)",
        FLOW,
        "        pending = [m for m in missing if m in flow.attribute.suggested]",
        "        pending = list(missing)",
    ),
    (
        "F7",
        "没东西可确认时仍然说「确认属性」(按钮点开是空的)",
        FLOW,
        "            return action(\n"
        "                NextActionCode.FILL_ATTRIBUTES,\n"
        '                "待填写:" + "、".join(missing[:3]) + ("…" if len(missing) > 3 else ""),\n'
        "            )",
        "            return action(\n"
        "                NextActionCode.CONFIRM_ATTRIBUTES,\n"
        '                "待填写:" + "、".join(missing[:3]) + ("…" if len(missing) > 3 else ""),\n'
        "            )",
    ),
    (
        "F8",
        "新动作码归到素材步(前端会把运营跳去素材页)",
        FLOW,
        "    NextActionCode.FILL_ATTRIBUTES: FlowStep.ATTRIBUTE,",
        "    NextActionCode.FILL_ATTRIBUTES: FlowStep.MATERIAL,",
    ),
    # --------------------------------------- P 组:溯源判定(AC-22)
    (
        "P1",
        "溯源判据去掉 legacy_kind(今天唯一真会命中的那一路,拦截整条失效)",
        PC,
        '    return _text(getattr(asset, "legacy_kind", None)) == LEGACY_CANDIDATE',
        "    return False",
    ),
    (
        "P2",
        "溯源判据去掉 source=AI_GENERATED",
        PC,
        '    if _text(getattr(asset, "source", None)) == MediaSource.AI_GENERATED.value:\n'
        "        return True\n",
        "",
    ),
    (
        "P3",
        "溯源判据去掉两个溯源列(阶段 2 落库那天它就瞎了,而且不报错)",
        PC,
        '    if getattr(asset, "generation_task_id", None) is not None:\n'
        "        return True\n"
        '    if getattr(asset, "generation_candidate_id", None) is not None:\n'
        "        return True\n",
        "",
    ),
    (
        "P4",
        "verdict 不问「这次是不是自称原始样品」(每张候选图落盘都隔离自己)",
        PC,
        "    if not claims_product_evidence(\n"
        "        source=source,\n"
        "        role=role,\n"
        "        legacy_asset_type=legacy_asset_type,\n"
        "        imported_url_trusted=imported_url_trusted,\n"
        "    ):\n"
        "        return Verdict.ACCEPT\n",
        "",
    ),
    (
        "P5",
        "claims_product_evidence 取反(只拦 AI 回写,放过人工上传)",
        PC,
        "    return (\n        derive_evidence_class(",
        "    return not (\n        derive_evidence_class(",
    ),
    (
        "P6",
        "may_fill_role 恒 True(退回本批之前的口径:AI 行照样被盖 HUMAN)",
        PC,
        "    return not carries_generation_provenance(asset)",
        "    return True",
    ),
    (
        "P7",
        "verdict 只看第一条同图素材(第二条起带溯源就漏)",
        PC,
        "    for asset in same_digest_in_spu:\n"
        "        if carries_generation_provenance(asset):\n"
        "            return Verdict.SOURCE_CONFLICT\n"
        "    return Verdict.ACCEPT",
        "    for asset in same_digest_in_spu:\n"
        "        return (\n"
        "            Verdict.SOURCE_CONFLICT\n"
        "            if carries_generation_provenance(asset)\n"
        "            else Verdict.ACCEPT\n"
        "        )\n"
        "    return Verdict.ACCEPT",
    ),
    # --------------------------------------- W 组:接线
    (
        "W1",
        "取数范围缩回去重键(同 SPU 不同 SKU 那一路整条失效 —— 本批最贵的一条)",
        MSVC,
        "    where = (\n"
        "        MediaAsset.spu == product.spu\n"
        "        if product.spu\n"
        "        else MediaAsset.product_id == product.id\n"
        "    )",
        "    where = MediaAsset.product_id == product.id",
    ),
    (
        "W2",
        "spu 为空时不退回按商品找(全库无 SPU 的素材被当成同一个 SPU 的兄弟)",
        MSVC,
        "        MediaAsset.spu == product.spu\n"
        "        if product.spu\n"
        "        else MediaAsset.product_id == product.id",
        "        MediaAsset.spu == product.spu\n"
        "        if True\n"
        "        else MediaAsset.product_id == product.id",
    ),
    (
        "W3",
        "冲突时不落隔离,只记一条日志(§11 的「待人工放行」那一半没了)",
        MSVC,
        "        status = MediaStatus.QUARANTINED\n\n    asset = MediaAsset(",
        "\n    asset = MediaAsset(",
    ),
    (
        "W4",
        "隔离了但不写理由(素材页上是一条查不出来路的红行)",
        MSVC,
        "        asset.quarantine_reason = provenance_conflict.QUARANTINE_REASON[:2000]",
        "        pass",
    ),
    (
        "W5",
        "_fill_missing_role 不再问溯源(去重命中那一路的防线没了)",
        MSVC,
        "    if not provenance_conflict.may_fill_role(asset):\n        return\n",
        "",
    ),
    (
        "W6",
        "上传检查的理由重新盖掉溯源冲突那一句",
        MSVC,
        "    if not deduped and status is MediaStatus.QUARANTINED and not media.quarantine_reason:",
        "    if not deduped and status is MediaStatus.QUARANTINED:",
    ),
    (
        "W7",
        "service 把两桶合起来喂给 suggested(混队列的第二个入口)",
        WSVC,
        "        suggested=buckets.confirmable,",
        "        suggested=buckets.confirmable | buckets.evidence_only,",
    ),
    (
        "W8",
        "service 退回直接按状态取桶(枚举新增取值时那个字段静默消失)",
        WSVC,
        "        candidate_only=buckets.evidence_only,",
        "        candidate_only=frozenset(by_status.get(AttributeStatus.CANDIDATE.value, ())),",
    ),
    # --------------------------------------- D 组:确定性
    (
        "D1",
        "待填写的字段按集合迭代序拼(同一件商品刷新两次,提示语里字段先后颠倒)",
        FLOW,
        '                "待填写:" + "、".join(missing[:3]) + ("…" if len(missing) > 3 else ""),',
        '                "待填写:" + "、".join(list(set(missing))[:3]) + ("…" if len(missing) > 3 else ""),',
    ),
    (
        "D2",
        "待确认的字段按集合迭代序拼(同上,换一条动线)",
        FLOW,
        "        pending = [m for m in missing if m in flow.attribute.suggested]",
        "        pending = list(flow.attribute.suggested & set(missing))",
    ),
]

SUITE_FILTER = "test_a45_batch14_11_queue_and_provenance.py"


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
