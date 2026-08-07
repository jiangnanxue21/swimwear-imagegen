#!/usr/bin/env python3
"""A45-batch14-22 变异验证:素材颜色归属写入路径 + 新结构样例数据。

用法:python3 tools/mutate_batch14_22.py

## 这一批变异在防什么

本批修的洞形状是**"链断在中间"** —— `media_assets.color_variant_id` 落库了、
两处判定读着,而没有任何写入路径。这类洞的共同特征:每一层单看都是对的,
合起来什么也没发生,而且**测试全绿**。

所以 L 组(链路)是重心:逐层把参数掐掉一个,看守卫认不认得出是哪一层断的。
掐掉任意一层的运行时表现完全相同(那一列恒为 NULL),而这恰恰是它当初
躲过整整五批的原因。

    L 组  写入链。四层各掐一次 + 只收不传 + 收下不落库
    G 组  归属的两道闸:重复上传不许改归属、跨 SPU 的 id 不许写进去
    H 组  hint 与外键合流。让模型的猜测决定已确认事实什么时候过期
    S 组  样例数据。三颜色九 SKU、单色对照、通用图、投影列
    V 组  样例验收脚本自己。它是那条验收在仓库里唯一的机械落点

## S/V 两组为什么值得单独有

阶段 1 那条验收「可构造三颜色九 SKU 的 SPU」在本批之前**只写在文档里**。
把它变成会红的检查之后,那条检查自己就成了要被守住的东西 —— 与上一批给
还款日门禁配元守卫是同一件事。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

API = "app/api/products.py"
ASSET = "app/services/asset_service.py"
MEDIA = "app/media/service.py"
PRODUCT = "app/services/product_service.py"
SEED = "app/scripts/seed_sample_data.py"
VERIFY = "tools/verify_sample_data.py"
MANIFEST = "../sample-data/spus.json"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ------------------------------------------------ L 组:写入链断在某一层
    (
        "L1",
        "接口层不收颜色(界面选了颜色,后端当没看见 —— 表现同完全没接)",
        API,
        "    color_variant_id: UUID | None = Form(None),\n",
        "",
    ),
    (
        "L2",
        "接口层收了不往下传(每层单看都对,合起来什么也没发生)",
        API,
        "        actor=current_actor(request),\n        color_variant_id=color_variant_id,",
        "        actor=current_actor(request),",
    ),
    (
        "L3",
        "asset_service 收了不传给影子写(链断在迁移层,最不容易想到的一环)",
        ASSET,
        "    media_service.shadow_from_product_asset(\n"
        "        session, product, asset, color_variant_id=color_variant_id\n"
        "    )",
        "    media_service.shadow_from_product_asset(session, product, asset)",
    ),
    (
        "L4",
        "影子写收了不传给 ingest(最里面那一环,列恒为 NULL)",
        MEDIA,
        "        role=role,\n        color_variant_id=color_variant_id,",
        "        role=role,",
    ),
    (
        "L5",
        "ingest 收下归属却不写进行(参数在、列还是空的)",
        MEDIA,
        "        role_confidence=role_confidence,\n        color_variant_id=color_variant_id,\n        variant_hint=variant_hint,",
        "        role_confidence=role_confidence,\n        variant_hint=variant_hint,",
    ),
    # ------------------------------------------------ G 组:两道闸
    (
        "G1",
        "重复上传会覆盖已有归属(一次重传让三批事实同时过期,两件事界面上无关联)",
        MEDIA,
        "    if asset.color_variant_id is not None:\n        return\n    asset.color_variant_id = color_variant_id",
        "    asset.color_variant_id = color_variant_id",
    ),
    (
        "G2",
        "去重命中那一路不补归属(先传成通用图再补颜色的动线走不通)",
        MEDIA,
        "        _fill_missing_colour(existing, color_variant_id=color_variant_id)\n",
        "",
    ),
    (
        "G3",
        "跨 SPU 校验不比 SPU(图挂到别的款,让一个没人动过的款的事实过期)",
        PRODUCT,
        "    if variant is None or variant.spu_id != product.spu_id:",
        "    if variant is None:",
    ),
    (
        "G4",
        "没有 spu_id 的商品也放行(允许挂到查不出关系的颜色上)",
        PRODUCT,
        "    if product.spu_id is None:\n        raise ValidationError(",
        "    if False:\n        raise ValidationError(",
    ),
    (
        "G5",
        "校验排在写入之后(拦下来时字节已经落存储了)",
        API,
        "    product = product_service.get_product(session, product_id)\n"
        "    if color_variant_id is not None:\n"
        "        product_service.assert_colour_belongs_to(session, product, color_variant_id)",
        "    product = product_service.get_product(session, product_id)",
    ),
    # ------------------------------------------------ H 组:hint 与外键合流
    (
        "H1",
        "归属列从 variant_hint 取值(模型的猜测决定已确认事实何时过期)",
        MEDIA,
        "        color_variant_id=color_variant_id,\n        variant_hint=variant_hint,",
        "        color_variant_id=variant_hint,\n        variant_hint=variant_hint,",
    ),
    # ------------------------------------------------ S 组:样例数据
    (
        "S1",
        "三色样本砍成两色(阶段 1 验收在样例上失去对象)",
        MANIFEST,
        '        { "variant_code": "NVY", "working_name": "藏青", "supplier_color_code": "C-027" }\n',
        "",
    ),
    (
        "S2",
        "尺码模板换成均码(三颜色但只有三 SKU,九那个数字没了)",
        MANIFEST,
        '      "size_template": "ALPHA_3",\n      "color_variants": [\n        { "variant_code": "BLK", "working_name": "经典黑"',
        '      "size_template": "ONE_SIZE",\n      "color_variants": [\n        { "variant_code": "BLK", "working_name": "经典黑"',
    ),
    (
        "S3",
        # 第一版这条改的是 spu_code,而对照组的判据是**颜色个数**——改名之后
        # 那个 SPU 仍然只有一个颜色,守卫照样绿。一条不做它自称做的事的变异,
        # 比没有变异更糟:它让计数变成一句谎话
        "删掉单色对照组(AC-21 少一半 —— 颜色维被关掉会显示成「全都不过期」)",
        MANIFEST,
        '        { "variant_code": "WHT", "working_name": "米白", "supplier_color_code": "C-003" }\n',
        '        { "variant_code": "WHT", "working_name": "米白", "supplier_color_code": "C-003" },\n'
        '        { "variant_code": "IVY", "working_name": "象牙", "supplier_color_code": "C-004" }\n',
    ),
    (
        "S4",
        "男装样本改成女装(「受众决定必填集」在演示里看不出来)",
        MANIFEST,
        '      "internal_name": "男士平角泳裤(受众对照组)",\n      "audience": "MEN",',
        '      "internal_name": "男士平角泳裤(受众对照组)",\n      "audience": "WOMEN",',
    ),
    (
        "S5",
        "样例里填 display_name(绕过属性服务那个唯一写入点)",
        MANIFEST,
        '{ "variant_code": "WHT", "working_name": "米白", "supplier_color_code": "C-003" }',
        '{ "variant_code": "WHT", "working_name": "米白", "display_name": "象牙白", "supplier_color_code": "C-003" }',
    ),
    (
        "S6",
        "通用图那一档去掉(「补通用图不该 stale 颜色事实」在样例上验不到)",
        SEED,
        "    shared_host = _first_sku(None)",
        "    shared_host = None",
    ),
    (
        "S7",
        "播种自己展开 SKU(样例能建出接口建不出来的东西)",
        SEED,
        "        spu = spu_service.create_spu(",
        "        spu = _hand_rolled_create_spu(",
    ),
    (
        "S8",
        "素材逐个 SKU 挂(同一张图在库里三份,证据数量凭空乘尺码数)",
        SEED,
        "        product = _first_sku(variant.id)",
        "        product = None",
    ),
    # ------------------------------------------------ V 组:样例验收脚本自己
    (
        "V1",
        "三颜色九 SKU 那条检查没挂进 CHECKS(写好了从来不会被执行)",
        VERIFY,
        '    ("存在三颜色九 SKU 的 SPU", check_a_three_colour_nine_sku_spu_exists),\n',
        "",
    ),
    (
        "V2",
        "缺 pydantic 时整条跳过而不是只跳字段那一层(schema 全错也显示绿)",
        VERIFY,
        "        expanded = _expand_all(sku_matrix)\n        return (",
        "        expanded = 0\n        return (",
    ),
    (
        "V3",
        "按颜色的图缺了也不报(样例图漏一半,AC-21 演示到一半发现没图)",
        VERIFY,
        '    ("每个颜色的样例图齐全", check_every_colour_has_its_sample_images),\n',
        "",
    ),
    (
        "V4",
        "各颜色图内容相同也放过(AC-21 在样例上得出一个假的结论)",
        VERIFY,
        '    ("各颜色的图内容互不相同", check_colour_images_are_distinct_across_colours),\n',
        "",
    ),
]

SUITE_FILTER = "test_a45_batch14_22_colour_attribution.py"


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
            code, output = run_suite(work, SUITE_FILTER)
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
