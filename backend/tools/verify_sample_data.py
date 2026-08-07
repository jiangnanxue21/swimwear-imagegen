#!/usr/bin/env python3
"""样例数据自检。

## 它从哪来

原来是 `backend/tests/pure/test_stage2_pipeline.py` 里的四条：

    test_sample_csv_is_valid
    test_sample_csv_skus_are_unique
    test_sample_images_exist_for_every_sku
    test_sample_images_pass_validation
    test_sample_images_have_distinct_hashes

方案 v4.1 第 8.2 节的处理：保留「真正 import→store 流程」和「storage URL 结果」
两条在纯测试里，其余移到样例数据验证脚本。

理由是它们验的是 `sample-data/` 这份**数据**对不对，不是代码对不对。
放在业务测试套件里会有两个坏处：改一行评分逻辑跑一次全套，要顺带读十张图片；
而真出问题时——比如有人换了一批样图——它报出来的位置在「阶段 2 管线」，
和「你换的那批图有两张一样」之间隔着一层。

## 为什么其中三条原来是「不存在就跳过」

样例图是生成物，仓库里可能没有。这在测试里表现为**静默通过**，
而静默通过和真的检查过是两回事。这里改成显式报告：没有图就说「没有图」，
让人自己判断这是不是他要的状态，而不是替他判断。

## 用法

    python3 tools/verify_sample_data.py       # 或 make verify-sample-data
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

PROJECT_ROOT = BACKEND_ROOT.parent
SAMPLE_CSV = PROJECT_ROOT / "sample-data" / "products.csv"
SAMPLE_SPUS = PROJECT_ROOT / "sample-data" / "spus.json"
SAMPLE_IMAGES = PROJECT_ROOT / "sample-data" / "images"

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

#: 需求要求至少这么多个示例商品。少于它 `make seed` 演示不起来
MIN_PRODUCTS = 10
VIEWS = ("front", "back", "detail")


class Failure(Exception):
    pass


def _rows():
    from app.services.product_import import parse_csv

    result = parse_csv(SAMPLE_CSV.read_text(encoding="utf-8"))
    if not result.ok:
        detail = [f"第{e.row_number}行 {e.field}: {e.message}" for e in result.errors]
        raise Failure("样例 CSV 自身不合法，`make seed` 会失败：\n  " + "\n  ".join(detail))
    return result.rows


def check_csv_parses_and_has_enough_rows() -> str:
    rows = _rows()
    if len(rows) < MIN_PRODUCTS:
        raise Failure(f"只有 {len(rows)} 个示例商品，需求要求至少 {MIN_PRODUCTS} 个")
    return f"{len(rows)} 行全部合法"


def check_skus_are_unique() -> str:
    skus = [r["sku"] for r in _rows()]
    dupes = sorted({s for s in skus if skus.count(s) > 1})
    if dupes:
        raise Failure(f"样例 SKU 重复：{dupes}")
    return f"{len(skus)} 个 SKU 互不相同"


def check_every_sku_has_its_images() -> str:
    if not SAMPLE_IMAGES.exists():
        return f"{YELLOW}样例图目录不存在，跳过（图是生成物）{RESET}"
    missing = [
        f"{row['sku']}_{view}.jpg"
        for row in _rows()
        for view in VIEWS
        if not (SAMPLE_IMAGES / f"{row['sku']}_{view}.jpg").exists()
    ]
    if missing:
        raise Failure(f"缺少 {len(missing)} 张样例素材，例如：{missing[:5]}")
    return f"每个 SKU 的 {len(VIEWS)} 个视角都齐"


def check_images_pass_upload_validation() -> str:
    if not SAMPLE_IMAGES.exists():
        return f"{YELLOW}样例图目录不存在，跳过{RESET}"

    from app.core.enums import AssetType
    from app.services.upload_validation import validate_upload

    checked = 0
    for path in sorted(SAMPLE_IMAGES.glob("*.jpg")):
        result = validate_upload(
            path.read_bytes(),
            asset_type=AssetType.GARMENT_FRONT,
            max_bytes=20 * 1024 * 1024,
            min_edge_px=256,
        )
        if not result.check_passed:
            raise Failure(f"{path.name} 过不了上传校验：{result.check_message}")
        checked += 1
    return f"{checked} 张图全部通过上传校验"


def check_images_have_distinct_hashes() -> str:
    """正面/背面/细节必须内容不同，否则会被去重掉，详情页只剩一张。"""
    if not SAMPLE_IMAGES.exists():
        return f"{YELLOW}样例图目录不存在，跳过{RESET}"

    from app.core.hashing import hash_bytes

    collisions: list[str] = []
    for row in _rows():
        seen: dict[str, str] = {}
        for view in VIEWS:
            path = SAMPLE_IMAGES / f"{row['sku']}_{view}.jpg"
            if not path.exists():
                continue
            digest = hash_bytes(path.read_bytes())
            if digest in seen:
                collisions.append(f"{path.name} 与 {seen[digest]} 内容相同")
            seen[digest] = path.name
    if collisions:
        raise Failure(
            "同一个 SKU 的多个视角内容相同，导入后会被去重成一张：\n  "
            + "\n  ".join(collisions)
        )
    return "同一 SKU 的各视角内容互不相同"


# ---------------------------------------------------------------- 新结构(阶段 1)


def _spus() -> list[dict]:
    import json

    if not SAMPLE_SPUS.exists():
        raise Failure(
            f"{SAMPLE_SPUS} 不存在 —— PRD §13 阶段 1 的「测试数据与 Fixture "
            "重置为新结构」没有对象,而「可构造三颜色九 SKU 的 SPU」这条验收"
            "只能靠手工造数据演示"
        )
    return json.loads(SAMPLE_SPUS.read_text(encoding="utf-8"))["spus"]


def _expand_all(sku_matrix) -> int:
    """只跑展开规则那一半(不需要 pydantic)。返回验过的 SPU 个数。"""
    for spec in _spus():
        try:
            sku_matrix.expand(
                spu_code=sku_matrix.normalize_spu_code(spec["spu_code"]),
                variant_codes=[v["variant_code"] for v in spec["color_variants"]],
                size_template=spec["size_template"],
            )
        except Exception as exc:  # noqa: BLE001
            raise Failure(f"{spec['spu_code']} 展开不出 SKU:{exc}") from exc
    return len(_spus())


def check_the_new_structure_sample_is_accepted_by_the_real_service() -> str:
    """样例里每个 SPU 都得过 `SpuCreate` 与展开规则。

    **不在这里重写一遍校验。** 用的是接口那一个 schema 和
    `sku_matrix.expand()` —— 抄一份的下场是样例数据"自检通过"而
    `make seed` 当场失败,而这两件事之间隔着一次 docker compose。
    """
    from app.listings import sku_matrix

    try:
        from app.schemas.spu import SpuCreate
    except ModuleNotFoundError as exc:
        # **如实说跳过,不静默通过。** 这个脚本的整个设计口径就是这一条
        # (模块文档第二节:「没有图就说没有图,让人自己判断」)。
        #
        # 展开规则那一半不依赖 pydantic,所以下面照跑 —— 少验的只是字段
        # 类型那一层,而那一层在真有 pydantic 的机器上(CI、容器)会补上。
        # 整条跳过的话,一个 schema 都过不了的样例文件会在这里显示绿色
        expanded = _expand_all(sku_matrix)
        return (
            f"{YELLOW}缺 {exc.name},字段校验没跑;"
            f"展开规则那一半已验:{expanded} 个 SPU{RESET}"
        )

    for spec in _spus():
        payload = {k: v for k, v in spec.items() if not k.startswith("_") and k != "notes"}
        try:
            parsed = SpuCreate.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            raise Failure(f"{spec.get('spu_code')} 过不了 SpuCreate:{exc}") from exc
        try:
            sku_matrix.expand(
                spu_code=sku_matrix.normalize_spu_code(parsed.spu_code),
                variant_codes=[v.variant_code for v in parsed.color_variants],
                size_template=parsed.size_template,
            )
        except Exception as exc:  # noqa: BLE001
            raise Failure(f"{parsed.spu_code} 展开不出 SKU:{exc}") from exc
    return f"{len(_spus())} 个 SPU 都过得了建档服务"


def check_a_three_colour_nine_sku_spu_exists() -> str:
    """**PRD §13 阶段 1 验收那一条,在样例数据上必须有对象。**

    「可构造三颜色九 SKU 的 SPU」——「可构造」和「构造出来了」是两件事。
    后端的展开规则早就算得出 3 × 3 = 9(纯层有穷举守卫),而在这条检查
    之前,样例数据里**一个多颜色 SPU 都没有**:10 个 SPU 各 1 个 SKU。

    于是这条验收在演示时没有对象,而真库用例只能自己造夹具 ——
    自己造的夹具证明的是"代码能建",不是"这个包交出去之后演示得起来"。
    """
    from app.listings import sku_matrix

    found = []
    for spec in _spus():
        rows = sku_matrix.expand(
            spu_code=sku_matrix.normalize_spu_code(spec["spu_code"]),
            variant_codes=[v["variant_code"] for v in spec["color_variants"]],
            size_template=spec["size_template"],
        )
        if len(spec["color_variants"]) == 3 and len(rows) == 9:
            found.append(spec["spu_code"])
    if not found:
        raise Failure(
            "样例数据里没有三颜色九 SKU 的 SPU —— PRD §13 阶段 1 那条验收"
            "在这个包里演示不出来。加一个三色 × ALPHA_3 的 SPU 到 spus.json"
        )
    return f"{found[0]} 是三颜色九 SKU"


def check_the_sample_covers_both_audiences_and_a_single_colour_control() -> str:
    """样例要盖住两个受众,并留一个单色对照组。

    两条都不是凑数:

        受众    §18.3 的必填属性集按受众条件化(女装问领型、男装问腰头)。
                只有 WOMEN 的话「受众决定必填集」在演示里永远看不出来
        单色    AC-21 要两组才验得出来 —— 多色那件证明「A 色变了 B 色不变」,
                单色这件证明共享作用域本身还在动。只有多色样本时,一个把
                颜色维整个关掉的缺陷会表现成「全都不过期」,而那很像正常
    """
    specs = _spus()
    audiences = {s["audience"] for s in specs}
    if len(audiences) < 2:
        raise Failure(f"样例只覆盖了受众 {sorted(audiences)} —— 至少要两个")
    if not any(len(s["color_variants"]) == 1 for s in specs):
        raise Failure("样例里没有单色 SPU —— AC-21 缺了对照组")
    return f"受众 {sorted(audiences)},含单色对照组"


def check_every_colour_has_its_sample_images() -> str:
    """新结构的素材**按颜色挂**,每个颜色三视角齐,每个 SPU 一张通用图。

    通用图那一张不是装饰:它让「补一张通用图**不该** stale 掉颜色事实」
    (§5.3 模块文档第二条)在样例上验得到。少了它,颜色维整个被关掉的
    缺陷会表现成「全都不过期」。
    """
    if not SAMPLE_IMAGES.exists():
        return f"{YELLOW}样例图目录不存在，跳过（图是生成物）{RESET}"
    import json

    data = json.loads(SAMPLE_SPUS.read_text(encoding="utf-8"))
    views = data["sample_images"]["views"]
    shared_views = data["sample_images"]["shared_views"]
    missing = []
    for spec in data["spus"]:
        code = spec["spu_code"]
        for variant in spec["color_variants"]:
            for view in views:
                name = f"{code}-{variant['variant_code']}_{view}.jpg"
                if not (SAMPLE_IMAGES / name).exists():
                    missing.append(name)
        for view in shared_views:
            name = f"{code}__{view}.jpg"
            if not (SAMPLE_IMAGES / name).exists():
                missing.append(name)
    if missing:
        raise Failure(
            f"缺少 {len(missing)} 张按颜色挂的样例图,例如:{missing[:4]}。"
            "跑 `python sample-data/generate_images.py`"
        )
    return "每个颜色三视角齐,每个 SPU 有通用图"


def check_colour_images_are_distinct_across_colours() -> str:
    """**不同颜色的图内容必须不同。**

    相同的话去重键 `(product_id, sha256)` 挡不住(product_id 不同),
    但 §5.3 的指纹会把它们算成同一个元素 —— 于是"给 A 色补图"和
    "给 B 色补图"算出同一个指纹变化,AC-21 在样例上验出来的是假的。

    这条比老结构那条(同 SKU 各视角不同)更要紧:那条防的是图被去重掉,
    这条防的是**验收结论是假的**。
    """
    if not SAMPLE_IMAGES.exists():
        return f"{YELLOW}样例图目录不存在，跳过{RESET}"
    import json

    from app.core.hashing import hash_bytes

    data = json.loads(SAMPLE_SPUS.read_text(encoding="utf-8"))
    views = data["sample_images"]["views"]
    collisions = []
    for spec in data["spus"]:
        seen: dict[str, str] = {}
        for variant in spec["color_variants"]:
            for view in views:
                path = SAMPLE_IMAGES / f"{spec['spu_code']}-{variant['variant_code']}_{view}.jpg"
                if not path.exists():
                    continue
                digest = hash_bytes(path.read_bytes())
                if digest in seen:
                    collisions.append(f"{path.name} 与 {seen[digest]} 内容相同")
                seen[digest] = path.name
    if collisions:
        raise Failure(
            "同一个 SPU 下不同颜色/视角的图内容相同 —— AC-21 会在样例上"
            "得出一个假的结论:\n  " + "\n  ".join(collisions[:5])
        )
    return "同 SPU 内各颜色各视角内容互不相同"


CHECKS = [
    ("样例 CSV 可解析且数量够", check_csv_parses_and_has_enough_rows),
    ("样例 SKU 唯一", check_skus_are_unique),
    ("每个 SKU 的样例图齐全", check_every_sku_has_its_images),
    ("样例图通过上传校验", check_images_pass_upload_validation),
    ("样例图内容互不相同", check_images_have_distinct_hashes),
    ("新结构样例过得了建档服务", check_the_new_structure_sample_is_accepted_by_the_real_service),
    ("存在三颜色九 SKU 的 SPU", check_a_three_colour_nine_sku_spu_exists),
    ("覆盖两个受众且有单色对照", check_the_sample_covers_both_audiences_and_a_single_colour_control),
    ("每个颜色的样例图齐全", check_every_colour_has_its_sample_images),
    ("各颜色的图内容互不相同", check_colour_images_are_distinct_across_colours),
]


def main() -> int:
    failures: list[tuple[str, str]] = []
    for label, fn in CHECKS:
        try:
            note = fn()
        except Failure as exc:
            failures.append((label, str(exc)))
            print(f"  {RED}FAIL{RESET} {label}")
        except Exception as exc:  # noqa: BLE001
            failures.append((label, f"检查本身抛了异常：{exc!r}"))
            print(f"  {RED}ERROR{RESET} {label}")
        else:
            print(f"  {GREEN}OK{RESET}   {label} — {note}")

    print()
    for label, detail in failures:
        print(f"{RED}{label}{RESET}\n  {detail}\n")

    total = len(CHECKS)
    color = GREEN if not failures else RED
    print(f"{color}{total - len(failures)}/{total} 项通过{RESET}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
