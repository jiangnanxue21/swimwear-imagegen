#!/usr/bin/env python3
"""A45-batch14-23 变异验证:§6.5 两列的写入路径 + 去重键欠账拆账。

用法:python3 tools/mutate_batch14_23.py

## 这一批变异在防什么

本批还的是 14-20 记下的那笔账:`shared_opt_in` / `angle` 落库了、被读、
被判定使用,而**没有任何代码路径写过它们**。这类洞的形状与 14-22 那笔
(`color_variant_id` 无写入路径)完全同型 —— 每一层单看都对,合起来
什么也没发生,测试全绿。

    W 组  写入路径。两个 kwarg 各掐一次 + 归一化函数被绕开
    E 组  入参形状。角度退化成自由字符串、勾选退化成可空
    D 组  去重键那笔逾期欠账。守卫自己被喂真、被绕过

## D 组为什么值得单独有

原 `test_dedupe_key_is_product_scoped` 把「不许全局唯一」(永久不变量)
和「今天是 product 作用域」(一笔欠账)写在同一组断言里,于是它**一直是
绿的**,而 PRD §4.8 要求的新键从来没有人记账。拆开之后欠账那一半会在
还款日到期时被门禁点名 —— D 组钉的是"拆开这件事本身别被合回去"。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

SERVICE = "app/listings/image_set_service.py"
SCHEMA = "app/schemas/image_set.py"
MODEL = "app/models/media_asset.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # -------------------------------------------- W 组:写入路径断在某一处
    (
        "W1",
        "构造点不写 shared_opt_in(每张通用图恒定命中 UNMARKED_SHARED_IMAGE)",
        SERVICE,
        'shared_opt_in=bool(item.get("shared_opt_in", False)),\n',
        "",
    ),
    (
        "W2",
        "构造点不写 angle(配了方案的颜色永远覆盖不了必要角度,报成「缺正面图」)",
        SERVICE,
        'angle=_angle_value(item.get("angle")),\n',
        "",
    ),
    (
        "W3",
        "angle 不过归一化,直接塞原值(空串与 NULL 两种形状同时进库)",
        SERVICE,
        'angle=_angle_value(item.get("angle")),',
        'angle=item.get("angle"),',
    ),
    (
        "W4",
        "归一化把空串当成有效角度(covered_angles 里多出一个空元素)",
        SERVICE,
        "    text = str(raw).strip()\n    return text or None",
        "    return str(raw).strip()",
    ),
    # -------------------------------------------- E 组:入参形状退化
    (
        "E1",
        "入参角度退化成自由字符串(拼错的角度一路走到「图片集批不过」)",
        SCHEMA,
        "    angle: ImageAngle | None = None",
        "    angle: str | None = None",
    ),
    (
        "E2",
        "shared_opt_in 退化成可空(「没勾」与「勾了又取消」走出两个结果)",
        SCHEMA,
        "    shared_opt_in: bool = False\n    #: 这一项覆盖的拍摄角度",
        "    shared_opt_in: bool | None = None\n    #: 这一项覆盖的拍摄角度",
    ),
    (
        "E3",
        "出参角度也收紧成枚举(存量值让详情接口 500,而调用方修不了)",
        SCHEMA,
        "    shared_opt_in: bool = False\n    angle: str | None = None",
        "    shared_opt_in: bool = False\n    angle: ImageAngle | None = None",
    ),
    # -------------------------------------------- D 组:去重键欠账被抹掉
    #
    # **D1 / D2 于 A45-batch14-26 退役 —— 那笔欠账还清了。**
    #
    # 两条验的都是「欠账守卫在有人动去重键时会当场红」。14-26 按 §4.8 落了
    # 新键,欠账守卫按它自己的遗嘱被删掉并换成正向守卫,于是:
    #
    #   D1 的锚点(旧的表级 `UniqueConstraint("product_id","sha256")`)
    #      在目标文件里已经不存在 —— `audit_anchors` 报「锚点过期」,
    #      而一条什么都没验的变异比没有变异更糟(14-22 的 S3 记过这句)。
    #   D2 变异的是"偷偷加 spu_id 而不动去重键"这个中间态,
    #      而那个中间态正是 14-26 走过去的路,今天它是终态的一半。
    #
    # 继任者是 `tools/mutate_batch14_26.py` 的 K 组(K1–K4),它钉的不再是
    # 「别动这个键」,是**新键的覆盖面**:两条局部唯一索引的谓词必须严格
    # 互补,否则中间裂开一批两条都不管的行。
    #
    # 按 14-24 台账那条「自净」规矩删掉,而不是留在这里注释掉:
    # 留着的话它会在下一次清点里以"已有变异覆盖"的身份被数进去。
]

SUITE_FILTER = "test_a45_batch14_23_item_columns_and_dedupe_ledger.py"


def run_suite(cwd: Path, name_filter: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "tools/run_pure_tests.py", name_filter],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    red = 0
    for ident, description, rel, old, new in MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "tree"
            shutil.copytree(PROJECT, work, symlinks=True)
            target = work / "backend" / rel
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                print(f"{ident:4} 锚点失准({text.count(old)} 次命中) —— 无法验证")
                continue
            target.write_text(text.replace(old, new), encoding="utf-8")
            code, _ = run_suite(work / "backend", SUITE_FILTER)
            verdict = "RED" if code != 0 else "GREEN"
            if code != 0:
                red += 1
            print(f"{ident:4} {description}  {verdict}")

    print(f"\n{red}/{len(MUTATIONS)} 条变异验红")
    return 0 if red == len(MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
