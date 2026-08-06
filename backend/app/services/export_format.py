"""导出格式化(需求第十五章 / 阶段 6)。

刻意从 `export_service` 里拆出来:**行形状是纯逻辑,不该需要一个数据库才能验证**。
和 evaluators 里分档规则的处理方式一致 —— 能纯就纯,纯的部分才写得起密集的测试。

CSV 的行形状选择:一个 SKU 一行,各用途一列。
另一种做法是一个用途一行、SKU 重复五次,那种更"规范",
但运营拿到表格后第一件事永远是做透视表把它转回来。这里直接给出他们要用的形状。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from app.core.enums import OutputPurpose

#: CSV 的用途列。顺序即枚举顺序,新增用途会自动出现在末尾
PURPOSE_COLUMNS: tuple[str, ...] = tuple(p.value for p in OutputPurpose)

CSV_HEADER: tuple[str, ...] = (
    "sku",
    "spu",
    "name",
    "category",
    "garment_type",
    "primary_color",
    "status",
    *(f"{p.lower()}_url" for p in PURPOSE_COLUMNS),
    *(f"{p.lower()}_size" for p in PURPOSE_COLUMNS),
)


def is_complete(images: dict[str, Any]) -> bool:
    """五个用途齐全才算这个 SKU 的图做完了。"""
    return set(images) >= {p.value for p in OutputPurpose}


#: Excel / Sheets 会把以这些字符开头的单元格当公式执行。
#: csv 模块的引号转义拦不住它 —— 那是 CSV 语法层面的转义,和电子表格怎么解释无关。
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def escape_formula(value: str) -> str:
    """给危险首字符前加一个单引号,Excel 打开时按文本处理。

    商品名和 SKU 是运营自己填的,但「自己填的」不等于「可信」:
    导出的 CSV 会被发给外部合作方,一条 =HYPERLINK(...) 或 =cmd|... 就够了。
    """
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def row_for(payload: dict[str, Any]) -> list[str]:
    product = payload["product"]
    images = payload["images"]
    row = [
        product.get("sku", ""),
        product.get("spu", ""),
        product.get("name", ""),
        product.get("category", ""),
        product.get("garment_type", ""),
        product.get("primary_color", ""),
        product.get("status", ""),
    ]
    row = [escape_formula(cell) for cell in row]
    # URL 与尺寸是系统生成的,不经过用户,但一起转义成本为零,少一条要记住的例外
    row += [escape_formula(str(images.get(p, {}).get("url", ""))) for p in PURPOSE_COLUMNS]
    row += [
        f"{images[p]['width']}x{images[p]['height']}" if p in images else ""
        for p in PURPOSE_COLUMNS
    ]
    return row


def to_csv(payloads: list[dict[str, Any]]) -> str:
    """生成 CSV 文本。

    行结束符固定 CRLF,配合接口层的 utf-8-sig,让 Excel 双击打开时不乱码、不错行 ——
    运营拿到这个文件的第一件事就是双击它。
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for payload in payloads:
        writer.writerow(row_for(payload))
    return buffer.getvalue()
