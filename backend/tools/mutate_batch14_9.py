#!/usr/bin/env python3
"""A45-batch14-9 变异验证:双作用域指纹与 `facts_stale` 派生。

用法:python3 tools/mutate_batch14_9.py

## 这一批变异在防什么

修的是 PRD 风险表 D1:**指纹作用域过宽**。它的失效方式和 D3 相反 ——
D3 是「该拦的没拦住」(AI 图进了识别),D1 是「不该动的动了」
(补一张 A 色图 stale 掉 B 色事实)。两种都不报错,都只是让人做无谓返工。

所以 S 组(作用域)是重心:每一条都让某个作用域**多**受影响一点,
而多受影响的表现是运营反复重新确认同一批事实,查不出为什么。

G 组管通用素材:让它进每个颜色子集,D1 就从后门原样回来了。
H 组管编码与哈希:碰撞的后果是**该过期的事实没有过期**。
C 组管跨模块那道接缝:指纹不复用 §5.1 的证据判定时,
生成一轮图会 stale 掉全部已确认事实。

## 为什么变异先写

反过来做就是 batch13-3 / M11 的来路。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

FP = "app/attributes/scope_fingerprint.py"
MATRIX = "app/workbench/stale_matrix.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ---------------------------------------------- S 组:作用域过宽(D1 本体)
    (
        "S1",
        "颜色子集不再按颜色筛(每个颜色都拿到全 SPU 素材 —— D1 的字面复现)",
        FP,
        "        for a in _evidence(assets, imported_url_trusted=imported_url_trusted)\n"
        "        if _text_or_none(getattr(a, \"color_variant_id\", None)) == variant_id",
        "        for a in _evidence(assets, imported_url_trusted=imported_url_trusted)",
    ),
    (
        "S2",
        "fingerprints 里的颜色子集也不筛",
        FP,
        "            for a in kept\n            if _text_or_none(getattr(a, \"color_variant_id\", None)) == variant\n        )",
        "            for a in kept\n        )",
    ),
    (
        "S3",
        "changed_scopes 比交集而不是并集(删光某色的图不算变化)",
        FP,
        "        scope for scope in set(old) | set(new) if old.get(scope) != new.get(scope)",
        "        scope for scope in set(old) & set(new) if old.get(scope) != new.get(scope)",
    ),
    (
        "S4",
        "指纹元素漏掉颜色归属(改归属对共享事实完全隐形)",
        FP,
        '        for name in ("asset_id", "content_hash", "source", "role", "color_variant_id", "status")',
        '        for name in ("asset_id", "content_hash", "source", "role", "status")',
    ),
    (
        "S5",
        "指纹元素漏掉状态(素材被隔离时事实不过期)",
        FP,
        '        for name in ("asset_id", "content_hash", "source", "role", "color_variant_id", "status")',
        '        for name in ("asset_id", "content_hash", "source", "role", "color_variant_id")',
    ),
    (
        "S6",
        "共享作用域改用字符串键(会被同名 variant_id 覆盖)",
        FP,
        "SHARED: None = None",
        'SHARED: str = "__shared__"',
    ),
    # ---------------------------------------------- G 组:通用素材
    (
        "G1",
        "通用素材进每一个颜色子集(补一张通用图 stale 掉全部颜色)",
        FP,
        '        if _text_or_none(getattr(a, "color_variant_id", None)) == variant_id\n    )',
        '        if _text_or_none(getattr(a, "color_variant_id", None)) in (variant_id, None)\n    )',
    ),
    (
        "G2",
        "空串归属不再归一成 NULL(同一张未归属素材两个指纹)",
        FP,
        "    text = str(value).strip()\n    return text or None",
        "    return str(value)",
    ),
    # ---------------------------------------------- H 组:编码与哈希
    (
        "H1",
        "去掉长度前缀(字段边界可平移,能构造碰撞)",
        FP,
        '    return f"{len(text)}:{text}"',
        '    return f"{text}|"',
    ),
    (
        "H2",
        "指纹版本不进哈希(改算法不会改指纹,新旧混比)",
        FP,
        '    payload = _field(FINGERPRINT_VERSION) + "".join(sorted(elements))',
        '    payload = "".join(sorted(elements))',
    ),
    (
        "H3",
        "不排序就哈希(同一组素材换个顺序得到不同指纹 —— 无谓 stale)",
        FP,
        '_field(FINGERPRINT_VERSION) + "".join(sorted(elements))',
        '_field(FINGERPRINT_VERSION) + "".join(elements)',
    ),
    # ---------------------------------------------- C 组:与 batch14-8 的接缝
    (
        "C1",
        "指纹不再过证据白名单(生成一轮图 stale 掉全部已确认事实)",
        FP,
        "    return [\n        a\n        for a in assets\n        if asset_is_extraction_input(a, imported_url_trusted=imported_url_trusted)\n    ]",
        "    return list(assets)",
    ),
    # ---------------------------------------------- F 组:facts_stale 判定
    (
        "F1",
        "没存过指纹时按「不过期」算(存量事实带着无法证明的有效性进导出)",
        FP,
        "    if stored is None:\n        return True",
        "    if stored is None:\n        return False",
    ),
    (
        "F2",
        "比较反向(对得上反而算过期)",
        FP,
        "    return stored != current",
        "    return stored == current",
    ),
    # ---------------------------------------------- M 组:失效矩阵那一列
    (
        "M1",
        "FACTS 列那一格降成「不过期」(矩阵重新对事实失明)",
        MATRIX,
        "        ChangeSource.ASSET_REPLACED_OR_QUARANTINED,\n        Target.FACTS,\n        Effect.STALE,",
        "        ChangeSource.ASSET_REPLACED_OR_QUARANTINED,\n        Target.FACTS,\n        Effect.NOT_AFFECTED,",
    ),
    (
        "M2",
        "「改已确认事实」那一格并进 STALE(新版本和过期混为一谈)",
        MATRIX,
        "        ChangeSource.CONFIRMED_ATTRIBUTE_MODIFIED,\n        Target.FACTS,\n        Effect.NEW_VERSION,",
        "        ChangeSource.CONFIRMED_ATTRIBUTE_MODIFIED,\n        Target.FACTS,\n        Effect.STALE,",
    ),
]

SUITE_FILTER = "test_a45_batch14_9_scope_fingerprint.py"


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
