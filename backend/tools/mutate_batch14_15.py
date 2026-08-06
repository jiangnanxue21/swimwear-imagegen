#!/usr/bin/env python3
"""A45-batch14-15 变异验证:素材归属外键落库,四批欠账收其二。

用法:python3 tools/mutate_batch14_15.py

## 这一批变异在防什么

本批加的是一**列**加一条**接线**,而两者的失效方式都不响:

    C 组   列本身。没了它,颜色作用域退回全空 —— 每个颜色的完整度
           静静地变成「合格」,而缺图的那个颜色一处都不显示
    W 组   接线。最要紧的是 W2:分桶改读 `variant_hint`。
           两列长得像,而 hint 是**模型猜出来的**;拿它决定「这件商品
           能不能往下走」正是 14-9 / 14-10 / 14-13 三批各自拒绝过一次的事。
           改过去之后本机一个断言都不会动 —— 除非有守卫专门钉它

过滤器挑的是 `test_a45_batch14_1` 前缀:本批改的是**别人那两批的守卫**
(14-9 的欠账、14-10 的欠账),所以要连着跑。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent



MODEL = "app/models/media_asset.py"
SVC = "app/workbench/service.py"
FLOW = "app/workbench/flow.py"
FP = "app/attributes/scope_fingerprint.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "C1",
        "归属外键改回不存在(颜色作用域退回全空,缺图的颜色一处都不显示)",
        MODEL,
        "    color_variant_id: Mapped[UUID | None] = mapped_column(",
        "    color_variant_id_removed: Mapped[UUID | None] = mapped_column(",
    ),
    (
        "C2",
        "归属外键给一个默认值(存量素材凭空成为某个颜色的证据)",
        MODEL,
        '        ForeignKey("color_variants.id", ondelete="SET NULL"),\n        nullable=True,',
        '        ForeignKey("color_variants.id", ondelete="SET NULL"),\n        nullable=False,',
    ),
    (
        "W1",
        "不按颜色分桶(判定验得再干净也白验 ——「算好了没人调」那一类)",
        SVC,
        "        variant_gate_roles={",
        "        variant_gate_roles_unused={",
    ),
    (
        "W3",
        "新字段给宽松默认(没传就退回 gate_roles —— 每个颜色都拿到全 SPU 的角色)",
        FLOW,
        "    variant_gate_roles: dict[str, frozenset[str]] = field(default_factory=dict)",
        "    variant_gate_roles: dict[str, frozenset[str]] = field(\n"
        "        default_factory=lambda: {\"__all__\": frozenset({\"PRODUCT_FRONT\"})}\n"
        "    )",
    ),
    (
        "A1",
        "指纹适配器改读 variant_hint(模型的猜测决定人工确认过的事实什么时候过期)",
        FP,
        '        self.color_variant_id = getattr(asset, "color_variant_id", None)',
        '        self.color_variant_id = getattr(asset, "variant_hint", None)',
    ),
    (
        "A2",
        "指纹适配器认不出内容哈希(列名对不上,`content_hash` 恒为 None —— 换了文件指纹不变)",
        FP,
        '        self.content_hash = getattr(asset, "sha256", None)',
        '        self.content_hash = getattr(asset, "content_hash", None)',
    ),
]

SUITE_FILTER = "test_a45_batch14_15_attribution.py"


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
