#!/usr/bin/env python3
"""A45-batch14-7 变异验证:`evidence_class` 单点派生。

用法:python3 tools/mutate_batch14_7.py

## 这一批变异在防什么

`evidence_class` 是 PRD 风险表 D3 那一条的对策,而 D3 的形状是
「**一张 AI 图被误标成 PRODUCT_EVIDENCE 就穿透了整个 §5.1 白名单**」。
白名单穿透的直接后果是每张 AI 候选图产生一次真实的付费调用。

所以这一批的重心不是「派生函数有没有覆盖」,是「**穿透那条路被堵死了没有**」。
D 组四条(D1–D4)全部指向同一件事:让某个 AI 输入落到 `PRODUCT_EVIDENCE`。
其中 D2 是最要命的一条 —— 它不动派生规则,只把**校验器**打瘸,
用来验证守卫没有全靠校验器转述结论(batch14 阶段 3 那次「32/32 全红
好得可疑」正是这个形状:守卫在骗人)。

## 为什么变异先写

反过来做就是 batch13-3 / M11 的来路:先有守卫再补变异时,人会不自觉地
按守卫已经断言的东西去造变异,于是变异全红而覆盖是假的。

## 机制

复制整棵树到临时目录再改,不碰工作树 —— 与 `mutate_batch14_4.py` 同。
`text.count(old) != 1` 拒绝不唯一的锚点。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

RULES = "app/media/evidence_rules.py"
#: A45-batch14-19:§5.1 白名单搬进取数入口之后,P2/P3 打的是这个文件
MEDIA = "app/media/service.py"
ENUMS = "app/core/enums.py"
TESTS = "tests/pure/test_a45_batch14_7_evidence_class.py"
EXTRACT = "app/attributes/service.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ---------------------------------------------- D 组:让 AI 图穿透白名单
    (
        "D1",
        "溯源判定只认 task_id,漏掉 candidate_id(PRD 的 CHECK 原文就漏了这个)",
        RULES,
        "    if generation_task_id is not None or generation_candidate_id is not None:",
        "    if generation_task_id is not None:",
    ),
    (
        "D2",
        "CHECK 孪生恒返回 False(守卫若只会转述它的结论,这条就不会响)",
        RULES,
        '    if _text(evidence_class) != EvidenceClass.PRODUCT_EVIDENCE.value:\n        return False',
        '    if _text(evidence_class) == EvidenceClass.PRODUCT_EVIDENCE.value:\n        return False',
    ),
    (
        "D3",
        "AI 来源直接落成商品证据(D3 风险的字面复现)",
        RULES,
        "    if src == MediaSource.AI_GENERATED.value:\n        return EvidenceClass.GENERATED_RESULT",
        "    if src == MediaSource.AI_GENERATED.value:\n        return EvidenceClass.PRODUCT_EVIDENCE",
    ),
    (
        "D4",
        "CHECK 孪生漏掉 candidate_id 那一路",
        RULES,
        "        generation_task_id is not None\n        or generation_candidate_id is not None\n        or _text(source) == MediaSource.AI_GENERATED.value",
        "        generation_task_id is not None\n        or _text(source) == MediaSource.AI_GENERATED.value",
    ),
    # ---------------------------------------------- R 组:模特参考图那条死分支
    (
        "R1",
        "参考素材只看 role(即 PRD 字面写法 —— 今天这是一条死分支)",
        RULES,
        "        _text(role) in REFERENCE_ONLY_MARKERS\n        or _text(legacy_asset_type) in REFERENCE_ONLY_MARKERS",
        "        _text(role) in REFERENCE_ONLY_MARKERS",
    ),
    (
        "R2",
        "把 MODEL_FRONT 也算成参考素材(每张正经模特图都被降级)",
        RULES,
        "    {AssetType.MODEL_REFERENCE.value, AssetType.BACKGROUND_REFERENCE.value}",
        '    {AssetType.MODEL_REFERENCE.value, AssetType.BACKGROUND_REFERENCE.value, "MODEL_FRONT"}',
    ),
    (
        "R3",
        "参考素材那条判定整条删掉(人工上传的参考图变成商品证据)",
        RULES,
        "    if _is_reference_material(role, legacy_asset_type):\n        return EvidenceClass.REFERENCE_ONLY\n\n    if src == MediaSource.PLATFORM_SYNC.value:",
        "    if src == MediaSource.PLATFORM_SYNC.value:",
    ),
    (
        "R4",
        "只降模特参考图,背景参考图照旧当证据(PRD 规则表的字面范围)",
        RULES,
        "    {AssetType.MODEL_REFERENCE.value, AssetType.BACKGROUND_REFERENCE.value}",
        "    {AssetType.MODEL_REFERENCE.value}",
    ),
    # ---------------------------------------------- T 组:外链「可信」那一处
    (
        "T1",
        "外链默认可信(谁贴条链接谁就产出一条商品证据)",
        RULES,
        "    imported_url_trusted: bool = False,\n) -> EvidenceClass:",
        "    imported_url_trusted: bool = True,\n) -> EvidenceClass:",
    ),
    (
        "T2",
        "不再要求显式声明可信",
        RULES,
        "    if src == MediaSource.IMPORTED_URL.value and imported_url_trusted:",
        "    if src == MediaSource.IMPORTED_URL.value:",
    ),
    (
        "T3",
        "把 IMPORTED_URL 直接塞进证据来源集合",
        RULES,
        "    {MediaSource.MANUAL_UPLOAD.value, MediaSource.SUPPLIER_FEED.value}",
        "    {\n        MediaSource.MANUAL_UPLOAD.value,\n        MediaSource.SUPPLIER_FEED.value,\n        MediaSource.IMPORTED_URL.value,\n    }",
    ),
    # ---------------------------------------------- F 组:兜底方向反了
    (
        "F1",
        "认不出来的 source 兜底成商品证据(fail open)",
        RULES,
        "    return EvidenceClass.REFERENCE_ONLY\n\n\ndef violates_ai_check(",
        "    return EvidenceClass.PRODUCT_EVIDENCE\n\n\ndef violates_ai_check(",
    ),
    # ---------------------------------------------- W 组:白名单判定那一半
    (
        "W1",
        "白名单不再看 READY(隔离/删除的图也进识别)",
        RULES,
        "    return status_is_ready and _text(evidence_class) == EvidenceClass.PRODUCT_EVIDENCE.value",
        "    return _text(evidence_class) == EvidenceClass.PRODUCT_EVIDENCE.value",
    ),
    (
        "W2",
        "白名单认错等级(放 AI 产物进识别输入)",
        RULES,
        "    return status_is_ready and _text(evidence_class) == EvidenceClass.PRODUCT_EVIDENCE.value",
        "    return status_is_ready and _text(evidence_class) != EvidenceClass.REFERENCE_ONLY.value",
    ),
    # ---------------------------------------------- E 组:枚举本身
    # P1「过滤结果算出来了但没赋回 assets」**已退役(A45-batch14-19)**。
    #
    # 它模拟的失效方式在新形状下不存在了。旧形状里 `assets` 先被绑成
    # `usable_assets()` 的未过滤结果,所以删掉 `assets = evidence` 之后
    # 代码照跑,**用的是未过滤的那一份** —— 安静、致命。
    #
    # 现在 `assets` 在 `run_extraction` 里只有这一个绑定点(未过滤取数已经
    # 不在这个函数里了),删掉它是一个 NameError —— 第一次调用就炸,
    # 不可能悄悄上线。缺陷从"静默错"变成了"响亮崩",而这正是本批想要的。
    #
    # 留着它只会让 24/24 变成一句谎话:变异跑出来是 GREEN,原因不是守卫漏了,
    # 是**被建模的那个缺陷已经不可能发生**。改由
    # `test_a45_batch14_19_evidence_query.py` 的
    # `..._has_exactly_one_binding_for_its_asset_list` 从正面钉住绑定点唯一。
    (
        "P2",
        # A45-batch14-19:白名单从 run_extraction 搬进取数入口,锚点跟着搬。
        # 变异本身一个字没改 —— 它打的是"条件被取反"这条性质,而性质没搬家
        "白名单条件取反(留下的全是不该进的)",
        MEDIA,
        "        if evidence_rules.asset_is_extraction_input(\n"
        "            asset, imported_url_trusted=imported_url_trusted\n"
        "        )",
        "        if not evidence_rules.asset_is_extraction_input(\n"
        "            asset, imported_url_trusted=imported_url_trusted\n"
        "        )",
    ),
    (
        "P3",
        "整段白名单删掉(退回 batch14-7 之前的状态)",
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
        "P4",
        "溯源列写死 None(列落库之后这里继续瞎,不报错不留痕)",
        RULES,
        '            generation_task_id=getattr(asset, "generation_task_id", None),\n            generation_candidate_id=getattr(asset, "generation_candidate_id", None),',
        "            generation_task_id=None,\n            generation_candidate_id=None,",
    ),
    (
        "P5",
        "适配器不看状态(隔离/删除的图也进识别)",
        RULES,
        '        status_is_ready=_text(getattr(asset, "status", None)) == "READY",',
        "        status_is_ready=True,",
    ),
    (
        "P6",
        "全被过滤掉时复用「一张图都没有」那条报错(误导运营去传更多图)",
        EXTRACT,
        '                f"该商品的 {total} 条可用素材全部不能作为识别证据"\n                "(AI 生成图、渠道回抓图与参考图不进识别输入,见 §5.1)",',
        '                "该商品没有可用于识别的素材(需要至少一条 READY 且未隔离的素材)",',
    ),
    (
        "P7",
        "适配器默认外链可信(新调用点不再 fail closed)",
        RULES,
        "def asset_is_extraction_input(asset: Any, *, imported_url_trusted: bool = False) -> bool:",
        "def asset_is_extraction_input(asset: Any, *, imported_url_trusted: bool = True) -> bool:",
    ),
    (
        "X1",
        "把输入空间删剩一个来源(穷举守卫塌成平凡通过)",
        TESTS,
        "ALL_SOURCES = (\n    *[s.value for s in MediaSource],\n    None,\n    \"\",\n    \"SOME_SOURCE_ADDED_LATER\",\n)",
        'ALL_SOURCES = (MediaSource.MANUAL_UPLOAD.value,)',
    ),
    (
        "X2",
        "AI 子集过滤条件写反(循环体一次都不执行)",
        TESTS,
        "        ai = task is not None or cand is not None or source == \"AI_GENERATED\"\n        if not ai:\n            continue\n        result = _derive(*combo)",
        "        ai = task is not None or cand is not None or source == \"AI_GENERATED\"\n        if ai:\n            continue\n        result = _derive(*combo)",
    ),
    (
        "E1",
        "证据等级少一个取值(CHANNEL_DERIVATIVE 并进 REFERENCE_ONLY)",
        ENUMS,
        '    CHANNEL_DERIVATIVE = "CHANNEL_DERIVATIVE"',
        "",
    ),
]

SUITE_FILTER = "test_a45_batch14_7_evidence_class.py"


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
