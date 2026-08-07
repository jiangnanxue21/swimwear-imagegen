#!/usr/bin/env python3
"""生成示例商品素材图。

这些不是真实商品照片,只是带图案的占位图 —— 目的是让阶段 2 的上传、去重、
尺寸探测和详情页展示能在没有真实素材时也跑通。
接入真实 Provider(阶段 5)前请替换为真实商品图。

## 两套命名,因为素材的归属层级不同(阶段 1 / §5.3)

    <sku>_<view>.jpg                    老结构:按 SKU 挂。products.csv 那 10 件
    <spu>-<variant>_<view>.jpg          新结构:**按颜色挂**
    <spu>__<view>.jpg                   新结构:通用图(不属于任何颜色)

按颜色挂是新结构的要点。一个颜色的样品图对该颜色下的每一个尺码都算数,
而 §5.3 的颜色作用域指纹正是按 `color_variant_id` 分组的。

按 SKU 挂的话,同一个颜色的三个尺码会各存一份同样的图 —— 去重键把后两份
挡掉,于是 S 码有图、M/L 没有,表现是「同一个颜色有的尺码缺图」。

通用图那一档存在的意义很具体:它让「补一张通用图**不该** stale 掉颜色事实」
这条(§5.3 模块文档第二条)在样例数据上验得到。少了它,颜色维整个被关掉的
缺陷会表现成「全都不过期」,而那看起来很像正常。
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
OUT = HERE / "images"

W, H = 900, 1200


def _base(color: tuple[int, int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), (245, 245, 244))
    return img, ImageDraw.Draw(img)


def draw_pattern(draw: ImageDraw.ImageDraw, pattern: str, color, secondary) -> None:
    box = (150, 220, 750, 980)
    if pattern == "STRIPE":
        draw.rectangle(box, fill=color)
        for y in range(box[1], box[3], 60):
            draw.rectangle((box[0], y, box[2], y + 28), fill=secondary)
    elif pattern == "FLORAL":
        draw.rectangle(box, fill=color)
        for cx in range(box[0] + 60, box[2] - 40, 120):
            for cy in range(box[1] + 60, box[3] - 40, 120):
                draw.ellipse((cx - 26, cy - 26, cx + 26, cy + 26), fill=secondary)
                draw.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), fill=color)
    elif pattern == "GEOMETRIC":
        draw.rectangle(box, fill=color)
        for cx in range(box[0], box[2], 100):
            for cy in range(box[1], box[3], 100):
                draw.polygon([(cx + 50, cy), (cx + 100, cy + 50), (cx + 50, cy + 100), (cx, cy + 50)], fill=secondary)
    elif pattern == "COLOR_BLOCK":
        mid = (box[1] + box[3]) // 2
        draw.rectangle((box[0], box[1], box[2], mid), fill=color)
        draw.rectangle((box[0], mid, box[2], box[3]), fill=secondary)
    elif pattern == "ANIMAL":
        draw.rectangle(box, fill=color)
        import random

        rnd = random.Random(7)
        for _ in range(120):
            x = rnd.randint(box[0], box[2] - 40)
            y = rnd.randint(box[1], box[3] - 30)
            draw.ellipse((x, y, x + rnd.randint(16, 38), y + rnd.randint(12, 28)), fill=secondary)
    else:  # SOLID / TIE_DYE 等
        draw.rectangle(box, fill=color)


COLORS = {
    "black": (28, 28, 30), "white": (250, 250, 248), "blue": (32, 86, 148),
    "navy": (23, 42, 79), "red": (176, 46, 52), "coral": (238, 122, 96),
    "green": (46, 110, 88), "gold": (198, 162, 92), "pink": (226, 143, 160),
    "purple": (110, 78, 140), "beige": (214, 196, 172), "teal": (27, 75, 90),
}


def rgb(name: str, fallback=(120, 120, 120)):
    return COLORS.get((name or "").lower(), fallback)


def render(sku: str, view: str, primary: str, secondary: str, pattern: str, label: str) -> Path:
    img, draw = _base(rgb(primary))
    draw_pattern(draw, pattern, rgb(primary), rgb(secondary, (240, 240, 238)))
    # 背面图做一点区分,避免和正面图哈希相同被去重掉
    if view == "back":
        draw.rectangle((300, 300, 600, 420), fill=(245, 245, 244))
    if view == "detail":
        img = img.crop((200, 300, 700, 800)).resize((700, 700))
        draw = ImageDraw.Draw(img)
    draw.text((40, 40), f"{sku}  ·  {label}", fill=(60, 60, 62))
    draw.text((40, 64), f"{view.upper()}  |  示例素材 非真实商品图", fill=(140, 140, 142))

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{sku}_{view}.jpg"
    img.save(path, format="JPEG", quality=90)
    return path


#: 颜色 working_name -> 调色板里的键。样例里的颜色名是中文,而 `COLORS`
#: 的键是英文 —— 缺一条映射的表现不是报错,是那个颜色回落到灰色占位,
#: 于是三个颜色的图**看起来一样但哈希不同**,肉眼查不出来,而它恰恰会让
#: 「这张图是哪个颜色的」在演示时失去意义
VARIANT_PALETTE = {
    "BLK": ("black", "gold"),
    "COR": ("coral", "white"),
    "NVY": ("navy", "white"),
    "WHT": ("white", "beige"),
    "GRY": ("beige", "black"),
}


def render_new_structure() -> int:
    """按新结构出图:每个颜色一组,外加每个 SPU 一张通用图。

    复用上面那个 `render()` —— **不写第二个渲染器**。写第二个的代价不是
    重复,是两套占位图会漂移(尺寸、质量、水印文案),而上传校验的
    `min_edge_px` 只在其中一套上被验过。
    """
    manifest = HERE / "spus.json"
    if not manifest.exists():
        print(f"缺少 {manifest},跳过新结构样例图", file=sys.stderr)
        return 0

    data = json.loads(manifest.read_text(encoding="utf-8"))
    views = tuple(data["sample_images"]["views"])
    shared_views = tuple(data["sample_images"]["shared_views"])

    count = 0
    for spu in data["spus"]:
        code = spu["spu_code"]
        for variant in spu["color_variants"]:
            primary, secondary = VARIANT_PALETTE.get(
                variant["variant_code"], ("teal", "white")
            )
            for view in views:
                render(
                    f"{code}-{variant['variant_code']}", view, primary, secondary,
                    "SOLID", f"{spu['internal_name']} · {variant['working_name']}",
                )
                count += 1
        # 通用图:文件名里变体段为空(双下划线)。它只进共享作用域
        for view in shared_views:
            render(f"{code}_", view, "teal", "beige", "GEOMETRIC",
                   f"{spu['internal_name']} · 通用")
            count += 1
    return count


def main() -> int:
    csv_path = HERE / "products.csv"
    if not csv_path.exists():
        print(f"缺少 {csv_path}", file=sys.stderr)
        return 1

    count = 0
    with csv_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            secondary = (row.get("secondary_colors") or "").split("|")[0]
            for view in ("front", "back", "detail"):
                render(
                    row["sku"], view, row.get("primary_color", ""), secondary,
                    row.get("pattern_type", "SOLID"), row["name"],
                )
                count += 1
    new_count = render_new_structure()
    print(f"生成 {count} 张老结构素材 + {new_count} 张新结构素材 -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
