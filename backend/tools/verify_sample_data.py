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


CHECKS = [
    ("样例 CSV 可解析且数量够", check_csv_parses_and_has_enough_rows),
    ("样例 SKU 唯一", check_skus_are_unique),
    ("每个 SKU 的样例图齐全", check_every_sku_has_its_images),
    ("样例图通过上传校验", check_images_pass_upload_validation),
    ("样例图内容互不相同", check_images_have_distinct_hashes),
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
