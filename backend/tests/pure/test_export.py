"""成品图导出测试(需求第十五章 / 阶段 6)。

CSV 生成是纯函数,不碰数据库,因此可以在这里完整验证行形状与转义。
"""
from __future__ import annotations

import csv
import io

from app.core.enums import OutputPurpose
from app.services.export_format import CSV_HEADER, PURPOSE_COLUMNS, is_complete, to_csv


def _payload(sku: str = "SW-001-BLK-S", purposes=None, **product_overrides) -> dict:
    purposes = PURPOSE_COLUMNS if purposes is None else purposes
    product = {
        "id": "00000000-0000-0000-0000-000000000001",
        "spu": "SPU-001",
        "sku": sku,
        "name": "黑色三角比基尼",
        "category": "swimwear",
        "garment_type": "BIKINI_SET",
        "primary_color": "BLACK",
        "pattern_type": "SOLID",
        "status": "COMPLETED",
    }
    product.update(product_overrides)
    images = {
        p: {
            "url": f"https://cdn.example.com/{sku}/{p.lower()}.jpg",
            "width": 1200,
            "height": 1600,
            "format": "JPEG",
            "file_size": 240_000,
            "hash": "a" * 64,
        }
        for p in purposes
    }
    return {
        "product": product,
        "images": images,
        "image_count": len(images),
        "complete": is_complete(images),
    }


def _parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


# ---------- 表头 ----------

def test_export_header_contract():
    """HEADER 常量的三条约定一次断清:SKU 位置、用途完整性、列顺序。"""
    # 运营打开表格第一眼要看到 SKU
    assert CSV_HEADER[0] == "sku"

    for purpose in OutputPurpose:
        assert f"{purpose.value.lower()}_url" in CSV_HEADER
        assert f"{purpose.value.lower()}_size" in CSV_HEADER

    assert list(PURPOSE_COLUMNS) == [p.value for p in OutputPurpose]


# ---------- 行形状 ----------

def test_one_row_per_sku_not_per_image():
    """一个 SKU 一行、各用途一列 —— 否则运营拿到表第一件事就是做透视表转回来。"""
    rows = _parse(to_csv([_payload("A"), _payload("B")]))
    assert len(rows) == 3, "表头 + 两行"
    assert [r[0] for r in rows[1:]] == ["A", "B"]


def test_row_length_matches_header():
    rows = _parse(to_csv([_payload()]))
    assert len(rows[1]) == len(CSV_HEADER)


def test_missing_purpose_leaves_an_empty_cell():
    """只出了两个用途的商品,其余列必须留空而不是错位。"""
    rows = _parse(to_csv([_payload(purposes=("ORIGINAL", "THUMBNAIL"))]))
    header, row = rows[0], rows[1]
    cells = dict(zip(header, row, strict=True))
    assert cells["original_url"]
    assert cells["thumbnail_url"]
    assert cells["detail_url"] == ""
    assert cells["square_url"] == ""


def test_size_column_is_width_x_height():
    rows = _parse(to_csv([_payload()]))
    cells = dict(zip(rows[0], rows[1], strict=True))
    assert cells["detail_size"] == "1200x1600"


def test_empty_export_still_emits_the_header():
    """空导出给一个只有表头的文件,比给一个 0 字节文件好排查。"""
    rows = _parse(to_csv([]))
    assert rows == [list(CSV_HEADER)]


# ---------- 转义 ----------

def test_commas_in_product_name_are_quoted():
    text = to_csv([_payload(name="比基尼,黑色,S 码")])
    rows = _parse(text)
    cells = dict(zip(rows[0], rows[1], strict=True))
    assert cells["name"] == "比基尼,黑色,S 码"


def test_quotes_and_newlines_do_not_break_the_row_count():
    text = to_csv([_payload(name='他说"很好"\n换行')])
    assert len(_parse(text)) == 2


def test_line_terminator_is_crlf_for_excel():
    text = to_csv([_payload()])
    assert "\r\n" in text
    assert not text.replace("\r\n", "").count("\n"), "不应残留裸 \\n"


def test_complete_flag_requires_every_purpose():
    assert _payload()["complete"] is True
    assert _payload(purposes=("ORIGINAL",))["complete"] is False
