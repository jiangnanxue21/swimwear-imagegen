"""商品批量导入:逐行校验,单行失败不影响其它行。"""
from __future__ import annotations

from app.services.product_import import parse_csv, parse_json_rows, parse_row
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401

#: CSV 表头。拼出来而不是写成一整行 —— 那一行 148 个字符,而它在字符串字面量
#: 内部,连一条豁免注释都放不进去:放进去就成了被测数据的一部分。
#: (这句话本身也不能写出那条指令的字面形式,否则 ruff 会把注释当成指令来解析
#:  并报「无效指令」—— 一条被自己的说明文字触发的规则,和 eslint.config.js 里
#:  记的那个教训是同一件事。)
#: 逗号拼接的结果与原来那一行**逐字节相同**,被测数据没有变。
_HEADER = ",".join(
    [
        "spu", "variant_code", "sku", "name", "category", "primary_color", "secondary_colors",
        "garment_type", "pattern_type", "strap_type", "neckline_type",
        "back_style", "material", "complexity",
    ]
)

GOOD_CSV = f"""{_HEADER}
SPU-001,BLK,SKU-001-S,基础三角比基尼,swimwear,black,white|gold,BIKINI_SET,SOLID,HALTER,V_NECK,TIE_BACK,涤纶/氨纶,SIMPLE
SPU-002,BLU,SKU-002-M,印花连体泳衣,swimwear,blue,,ONE_PIECE,FLORAL,SPAGHETTI,SCOOP,OPEN_BACK,锦纶,MEDIUM
"""


def test_parses_valid_csv():
    result = parse_csv(GOOD_CSV)
    assert result.ok, result.errors
    assert len(result.rows) == 2
    assert result.rows[0]["sku"] == "SKU-001-S"
    assert result.rows[0]["secondary_colors"] == ["white", "gold"]
    assert result.rows[1]["secondary_colors"] == []


def test_handles_bom_and_blank_lines():
    result = parse_csv("\ufeff" + GOOD_CSV + "\n,,,,,,,,,,,,\n")
    assert result.ok, result.errors
    assert len(result.rows) == 2


def test_missing_required_column_fails_fast():
    result = parse_csv("spu,name\nSPU-1,x\n")
    assert not result.ok
    assert "sku" in result.errors[0].message


def test_empty_required_value_reports_row_number():
    result = parse_csv("spu,variant_code,sku,name\nSPU-1,BASE,,泳衣\n")
    assert not result.ok
    assert result.errors[0].row_number == 2
    assert result.errors[0].field == "sku"


def test_invalid_enum_is_reported_not_silently_replaced():
    csv_text = "spu,variant_code,sku,name,garment_type\nSPU-1,BASE,SKU-1,泳衣,BANANA\n"
    result = parse_csv(csv_text)
    assert not result.ok
    assert result.errors[0].field == "garment_type"
    assert "BANANA" in result.errors[0].message
    assert result.rows == []


def test_enum_is_case_insensitive():
    result = parse_csv("spu,variant_code,sku,name,garment_type\nSPU-1,BASE,SKU-1,泳衣,one_piece\n")
    assert result.ok, result.errors
    assert result.rows[0]["garment_type"] == "ONE_PIECE"


def test_bad_row_does_not_block_good_rows():
    csv_text = (
        "spu,variant_code,sku,name,complexity\n"
        "SPU-1,BASE,SKU-1,好行,SIMPLE\n"
        "SPU-2,BASE,SKU-2,坏行,VERY_HARD\n"
        "SPU-3,BASE,SKU-3,另一好行,MEDIUM\n"
    )
    result = parse_csv(csv_text)
    assert len(result.rows) == 2
    assert len(result.errors) == 1
    assert result.errors[0].row_number == 3


def test_duplicate_sku_within_file_is_rejected():
    csv_text = "spu,variant_code,sku,name\nSPU-1,BASE,SKU-DUP,A\nSPU-2,BASE,SKU-DUP,B\n"
    result = parse_csv(csv_text)
    assert len(result.rows) == 1
    assert "重复" in result.errors[0].message


def test_empty_file_reports_header_error():
    result = parse_csv("")
    assert not result.ok
    assert "表头" in result.errors[0].message


def test_comma_separated_secondary_colors_also_work():
    row, errors = parse_row(
        {"spu": "S", "variant_code": "BASE", "sku": "K", "name": "N", "secondary_colors": "red, blue"}, 1
    )
    assert not errors
    assert row["secondary_colors"] == ["red", "blue"]


def test_json_rows_share_the_same_validation():
    result = parse_json_rows([
        {"spu": "SPU-1", "variant_code": "BASE", "sku": "SKU-1", "name": "泳衣", "pattern_type": "stripe"},
        {"spu": "SPU-2", "variant_code": "BASE", "sku": "SKU-2", "name": "泳衣2", "pattern_type": "NOPE"},
    ])
    assert len(result.rows) == 1
    assert result.rows[0]["pattern_type"] == "STRIPE"
    assert result.errors[0].field == "pattern_type"


def test_json_rejects_non_object_element():
    result = parse_json_rows(["just-a-string"])
    assert not result.ok
    assert result.errors[0].field == "row"
