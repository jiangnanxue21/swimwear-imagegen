#!/usr/bin/env python3
"""A45-batch14-20 的变异验证:守卫是不是真的拦得住它声称拦的东西。

## 这一批的失效方向

阶段 4 的四类缺陷**没有一类会报错**:

    方案指纹算错     换了方案静默命中旧任务 —— 运营看到"提交成功",拿到旧参数的图
    作用域算宽了     改一次 SPU 默认角度重出八个颜色的全部候选,每张都是真实付费调用
    §6.5 门禁松了    红色 SKU 挂着黑色主图上架,而批准那一步说"校验通过"
    事实一致性漏了   不一致的图照常自动通过,而报告是全绿的

所以变异分五组,每组打一个方向。

## 边界:哪些变异**刻意没有列进来**

    一  迁移 0041 的表达式唯一索引 / CHECK / 外键方向。
        这台机器没有 sqlalchemy,更没有 PostgreSQL —— 把 `COALESCE` 删掉
        在纯层必然 GREEN。真验证在
        `tests/test_a45_batch14_20_stage4_db.py`(已写、一次都没跑过)。
    二  前端的方案面板与颜色绑定入口。无 node_modules,Vitest 跑不了。

**列一条明知抓不住的变异进去,只会让"N/N"这个数字变成一句谎话。**
这两类写在 `docs/MERGE-A45-BATCH14-20-STAGE4-IMAGE-PRODUCTION.md`
「验不到什么」一节。

    python3 tools/mutate_batch14_20_stage4.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

PLAN = "app/workflows/generation_plan.py"
FACTS = "app/evaluators/fact_consistency.py"
RULES = "app/listings/image_set_rules.py"
SCORING = "app/evaluators/scoring.py"
IDEM = "app/workflows/idempotency.py"
MATRIX = "app/workbench/stale_matrix.py"
SCHEMA = "app/evaluators/vision_schema.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ---------------------------------------------- 一、方案指纹与作用域
    (
        "P1",
        "指纹把归属编进去(同参数不同颜色算出两个指纹 -> 什么都没改也判过期)",
        PLAN,
        "        angles=plan.angles,\n        budget_cap=plan.budget_cap,\n    )",
        "        angles=plan.angles,\n        budget_cap=plan.budget_cap,\n    ) + str(plan.color_variant_id)",
    ),
    (
        "P2",
        "预算不进指纹(改了上限重新提交 -> 静默命中被预算拦下的旧任务)",
        PLAN,
        "        length_prefixed(_money(budget_cap)),\n",
        "",
    ),
    (
        "P3",
        "角度只编角度名不编张数(每角度 1 张与 2 张算成同一份方案)",
        PLAN,
        'parts += [length_prefixed(f"{a.angle}={a.count}") for a in angles]',
        'parts += [length_prefixed(a.angle) for a in angles]',
    ),
    (
        "P4",
        "金额不归一(10 与 10.00 算出两个指纹 -> 每次创建任务顺带判自己过期)",
        PLAN,
        'return "" if decimal is None else format(decimal.normalize(), "f")',
        'return "" if value is None else str(value)',
    ),
    (
        "P5",
        "角度改按输入序编码(前端拖一下排序 = 重出一批图)",
        PLAN,
        "    order = {a.value: i for i, a in enumerate(ImageAngle)}\n"
        "    return tuple(\n"
        "        AngleRequirement(angle=a, count=counts[a]) for a in sorted(counts, key=lambda x: order[x])\n"
        "    )",
        "    return tuple(AngleRequirement(angle=a, count=counts[a]) for a in counts)",
    ),
    (
        "P6",
        "作用域解析忽略颜色覆盖(配了覆盖也用 SPU 默认)",
        PLAN,
        "    if wanted is not None:\n"
        "        for plan in active:\n"
        "            if _text_or_none(plan.color_variant_id) == wanted:\n"
        "                return plan\n",
        "",
    ),
    (
        "P7",
        "解析放行 DRAFT(编辑器里改到一半的参数直接拿去花钱)",
        PLAN,
        'active = [p for p in plans if p.status == "ACTIVE"]',
        "active = list(plans)",
    ),
    (
        "P8",
        "失效作用域只比交集(某个颜色的方案被删掉 -> 那个颜色永远不重出图)",
        PLAN,
        "        scope for scope in set(before) | set(after) if before.get(scope) != after.get(scope)",
        "        scope for scope in set(before) & set(after) if before.get(scope) != after.get(scope)",
    ),
    (
        "P9",
        "有覆盖的颜色也跟着 SPU 默认一起过期(九色 SPU 改一次默认 = 重出八色)",
        PLAN,
        "        plan = resolve_plan(plans, key)\n"
        "        if plan is not None:\n"
        "            resolved[key] = fingerprint_of(plan)",
        "        for plan in plans:\n"
        "            if _text_or_none(plan.color_variant_id) is None and plan.status == \"ACTIVE\":\n"
        "                resolved[key] = fingerprint_of(plan)",
    ),
    (
        "P10",
        "预算读不到时放行(fail-open —— 证明不了没超还是去花钱)",
        PLAN,
        "    if used is None:\n        return BudgetVerdict.UNKNOWN",
        "    if used is None:\n        return BudgetVerdict.ALLOWED",
    ),
    (
        "P11",
        "预算只比已花不比本次预估(上限永远在超出之后才生效)",
        PLAN,
        "    return BudgetVerdict.EXCEEDED if used + plan > cap else BudgetVerdict.ALLOWED",
        "    return BudgetVerdict.EXCEEDED if used > cap else BudgetVerdict.ALLOWED",
    ),
    (
        "P12",
        "候选上限抄一个字面量(调大 MAX_CANDIDATE_COUNT 之后方案侧仍按旧值拦)",
        PLAN,
        "MAX_PLAN_CANDIDATES = MAX_CANDIDATE_COUNT",
        "MAX_PLAN_CANDIDATES = 8  # 抄一个恰好相等的字面量",
    ),
    # ---------------------------------------------- 二、幂等键
    (
        "K1",
        "颜色不进幂等键(多色 SPU 的两个颜色共用一个任务)",
        IDEM,
        '        "color_variant_id": str(color_variant_id or ""),\n',
        "",
    ),
    (
        "K2",
        "方案指纹不进幂等键(§6.4 的\"确认方案\"点了等于没点)",
        IDEM,
        '        "plan_fingerprint": str(plan_fingerprint or ""),\n',
        "",
    ),
    (
        "K3",
        "样品指纹不进幂等键(换了样品图重出 -> 命中旧任务)",
        IDEM,
        '        "sample_fingerprint": str(sample_fingerprint or ""),\n',
        "",
    ),
    (
        "K4",
        "截断退回 128 字面量(列扩了而截断没跟上 -> 两份输入一个键)",
        IDEM,
        "MAX_IDEMPOTENCY_KEY_CHARS = 256",
        "MAX_IDEMPOTENCY_KEY_CHARS = 128",
    ),
    # ---------------------------------------------- 三、§6.5 颜色图片规则
    (
        "S1",
        "主图退回回落到通用图(红色 SKU 挂黑色主图 —— BLOCK-02 那个结局)",
        RULES,
        "    for item in image_set.items:\n"
        "        if item.enabled and item.is_primary and item.variant_id == variant_id:\n"
        "            return item\n"
        "    return None",
        "    for want in (variant_id, None):\n"
        "        for item in image_set.items:\n"
        "            if item.enabled and item.is_primary and item.variant_id == want:\n"
        "                return item\n"
        "    return None",
    ),
    (
        "S2",
        "通用图重新算作覆盖(一张通用图让九色 SPU 永远\"覆盖完整\")",
        RULES,
        "        if not cover.has_own_image:",
        "        if not cover.has_own_image and not cover.shared:",
    ),
    (
        "S3",
        "未标记的通用图默认混入(默认 opt-in —— 附图位自动灌满通用图)",
        RULES,
        "    shared_opt_in: bool = False",
        "    shared_opt_in: bool = True",
    ),
    (
        "S4",
        "通用图可以当颜色主图(§6.5 第一条整条失效)",
        RULES,
        "        if item.is_primary:\n"
        "            problems.append(\n"
        "                Violation(\n"
        "                    ImageSetViolation.SHARED_IMAGE_AS_PRIMARY,",
        "        if False:\n"
        "            problems.append(\n"
        "                Violation(\n"
        "                    ImageSetViolation.SHARED_IMAGE_AS_PRIMARY,",
    ),
    (
        "S5",
        "角度验收改成数张数(4 张正面图算作覆盖了正面+背面)",
        RULES,
        "            missing_angles=frozenset(angles.get(variant, frozenset())) - have,",
        "            missing_angles=frozenset()\n"
        "            if len(owned) >= len(angles.get(variant, ()))\n"
        "            else frozenset(angles.get(variant, frozenset())) - have,",
    ),
    (
        "S6",
        "没标注角度的图算作覆盖全部角度(接线完成前角度验收恒绿)",
        RULES,
        "        have = frozenset(i.angle for i in owned if i.angle)",
        "        have = frozenset(angles.get(variant, frozenset())) if owned else frozenset()",
    ),
    (
        "S7",
        "外来颜色标签退回诊断(指向已删商品的标签让覆盖率算出假的绿)",
        RULES,
        "            problems.append(\n"
        "                Violation(\n"
        "                    ImageSetViolation.FOREIGN_VARIANT_IMAGE,",
        "            continue\n"
        "            problems.append(\n"
        "                Violation(\n"
        "                    ImageSetViolation.FOREIGN_VARIANT_IMAGE,",
    ),
    (
        "S8",
        "§6.5 在单色 SPU 上也生效(不传 variant_ids 也查 —— 存量全停产)",
        RULES,
        "    wanted = [str(v) for v in variant_ids if str(v or \"\").strip()]\n"
        "    if not wanted:\n"
        "        return []",
        "    wanted = [str(v) for v in variant_ids if str(v or \"\").strip()]",
    ),
    # ---------------------------------------------- 四、事实一致性
    (
        "F1",
        "事实没确认时默认判不一致(整批图在确认事实之前全部被否)",
        FACTS,
        "        missing = [f for f in check.fact_fields if not _has_value(facts.get(f))]\n"
        "        if missing:",
        "        missing = [f for f in check.fact_fields if not _has_value(facts.get(f))]\n"
        "        if False:",
    ),
    (
        # 打在 `_verdict_of` 而不是 `is_hard_fail`:后者单独改**不会造成危害**
        # ——「判 MISMATCH」这件事被两处独立地要求(赋码时一处、判定时一处),
        # 只改一处的结果是 UNCERTAIN 拿到码但仍不否决。那是好的冗余,
        # 但也意味着那一行没有可实现的单点变异,所以不列。
        "F2",
        "UNCERTAIN 被归一成 MISMATCH(被水花挡住背部的好图被判死)",
        FACTS,
        "    if raw in FactVerdict.__members__:\n        return FactVerdict(raw)",
        "    if raw in FactVerdict.__members__:\n"
        "        return FactVerdict.MATCH if raw == \"MATCH\" else FactVerdict.MISMATCH",
    ),
    (
        "F3",
        "认不出来的 verdict 当成 MATCH(模型拼错一个词 = 放行)",
        FACTS,
        "    return FactVerdict.UNCERTAIN\n\n\ndef _has_value",
        "    return FactVerdict.MATCH\n\n\ndef _has_value",
    ),
    (
        "F4",
        "不一致不产出硬错误码(维度组算了但一票否决接不上)",
        FACTS,
        "        if finding.is_hard_fail and finding.code is not None:",
        "        if False and finding.is_hard_fail and finding.code is not None:",
    ),
    (
        "F5",
        "严重度降成 MAJOR(阈值一调,事实不一致就悄悄放行)",
        FACTS,
        '            "severity": ProblemSeverity.CRITICAL.value,',
        '            "severity": ProblemSeverity.MAJOR.value,',
    ),
    (
        "F6",
        "跳过项报成\"看不清\"(少确认一个字段被显示成模型看不清)",
        FACTS,
        "        if f.skipped_reason:\n"
        '            notes.append(f"事实一致性「{f.label}」未判定:{f.skipped_reason}")\n'
        "        elif f.verdict is FactVerdict.UNCERTAIN:",
        "        if f.verdict is FactVerdict.UNCERTAIN:\n"
        '            notes.append(f"事实一致性「{f.label}」看不清")\n'
        "        elif f.skipped_reason:",
    ),
    (
        "F7",
        "规则包不再决定启用哪几项(女装的肩带项摆进男装泳裤的清单)",
        FACTS,
        "    if not allowed_codes:\n        return FACT_CHECKS\n"
        "    wanted = {str(c).strip().upper() for c in allowed_codes}\n"
        "    return tuple(c for c in FACT_CHECKS if c.code.value in wanted)",
        "    return FACT_CHECKS",
    ),
    (
        "F8",
        "解析层不接事实一致性(维度组在纯层全绿而链路上从不生效)",
        SCORING,
        "    problems = [*problems, *_parse_problems(fc.problem_dicts(findings))]",
        "    problems = list(problems)",
    ),
    (
        "F9",
        "Schema 手抄一份清单(加一项时抄的那份不跟着变)",
        SCHEMA,
        '    check_lines = "\\n".join(f"- {c.key}({c.label})" for c in checks) or "- (本次无)"',
        '    check_lines = "- color(主辅色)\\n- pattern(图案)"',
    ),
    # ---------------------------------------------- 五、失效矩阵
    (
        "M1",
        "方案那一格点一个不按作用域算的机制(改一次默认重出全部颜色)",
        MATRIX,
        'mechanism="app.workflows.generation_plan.image_set_stale_scopes",',
        'mechanism="app.workflows.generation_plan.fingerprint_of",',
    ),
    (
        "M2",
        "替换批准图片集写成 STALE(与\"重新编排 -> 重置为待批准\"混成一件事)",
        MATRIX,
        "        Target.IMAGE_SET,\n        Effect.NEW_VERSION,\n"
        '        mechanism="app.listings.image_set_service.approve",',
        "        Target.IMAGE_SET,\n        Effect.STALE,\n"
        '        mechanism="app.listings.image_set_service.approve",',
    ),
    (
        "M3",
        "方案变化连带把事实判过期(方案决定怎么拍,不决定这件衣服是什么)",
        MATRIX,
        "        ChangeSource.GENERATION_PLAN_CHANGED,\n        Target.FACTS,\n        Effect.NOT_AFFECTED,",
        "        ChangeSource.GENERATION_PLAN_CHANGED,\n        Target.FACTS,\n        Effect.STALE,",
    ),
    # ---------------------------------------------- 打在老守卫上的几条
    (
        "R1",
        "[老守卫] 主图回落 —— test_image_set_rules 那条翻转后的断言还咬不咬得住",
        RULES,
        "    for item in image_set.items:\n"
        "        if item.enabled and item.is_primary and item.variant_id == variant_id:\n"
        "            return item\n"
        "    return None",
        "    for want in (variant_id, None):\n"
        "        for item in image_set.items:\n"
        "            if item.enabled and item.is_primary and item.variant_id == want:\n"
        "                return item\n"
        "    return None",
    ),
    (
        "R2",
        "[老守卫] 通用图重新算覆盖 —— BLOCK-02 那条契约翻转后还咬不咬得住",
        RULES,
        "        owned = tuple(i for i in enabled if i.variant_id == variant)",
        "        owned = tuple(\n"
        "            i for i in enabled if i.variant_id in (variant, None)\n"
        "        )",
    ),
    (
        "R3",
        "[老守卫] 矩阵少一格 —— 格数改成推导式之后还抓不抓得到",
        MATRIX,
        "    _cell(\n"
        "        ChangeSource.APPROVED_IMAGE_SET_REPLACED,\n"
        "        Target.COPY,\n"
        "        Effect.NOT_AFFECTED,\n"
        '        note="文案过期判定不接收图片集输入",\n'
        "    ),\n",
        "",
    ),
    (
        # **不打"把常量改成 128"**:守卫现在读同一个常量,两边一起变是
        # 正确行为(不变式是"截断长度 == 列宽",不是"等于某个数")。
        # 那条由本批的 K4 从**迁移文件**那一侧钉住。这里打的是截断本身没了。
        "R4",
        "[老守卫] 显式键根本不截断(超长键直接写库 -> 列宽溢出)",
        IDEM,
        "        return explicit_key.strip()[:MAX_IDEMPOTENCY_KEY_CHARS]",
        "        return explicit_key.strip()",
    ),
    (
        "R5",
        "[老守卫] 预览端点开始写库 —— 白名单放行的只是\"不提交\"",
        "app/api/generation_plans.py",
        "    scopes = svc.stale_image_scopes(session, spu_id=row.spu_id, before=before, after=after)",
        "    session.add(row)\n"
        "    scopes = svc.stale_image_scopes(session, spu_id=row.spu_id, before=before, after=after)",
    ),
]


SUITE_FILTER = "test_a45_batch14_20_stage4_image_production.py"

#: 打在老守卫上的几条:§6.5 翻转了 `test_image_set_rules` / 
#: `test_blocking_fix_contracts` 里的三条断言,矩阵那三条也换了形状。
#: **那几条必须单独跑**——拿子串一次跑两个文件的话,变异的颜色由
#: "哪一条守卫先红"决定,而报告只会记一个编号(见 `tools/suite_filter.py`)。
EXTRA_FILTER = {
    "R1": "test_image_set_rules.py",
    "R2": "test_blocking_fix_contracts.py",
    "R3": "test_stale_matrix.py",
    "R4": "test_idempotency.py",
    "R5": "test_transaction_boundaries.py",
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
