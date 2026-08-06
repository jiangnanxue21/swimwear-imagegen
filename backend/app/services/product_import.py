"""商品批量导入解析。纯逻辑,不碰数据库,便于单独测试。

行为约定:
- 逐行校验,单行出错不影响其余行 —— 返回 (合法行, 错误明细),由调用方决定是否落库;
- 枚举字段大小写不敏感,非法值不静默改写,而是明确报错;
- 辅助颜色支持 "red|blue" 或 "red,blue"(CSV 里逗号已被分隔符占用,推荐用竖线)。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import (
    Audience,
    BackStyle,
    GarmentType,
    NecklineType,
    PatternType,
    ProductComplexity,
    StrapType,
)
from app.core.field_limits import (
    MAX_SECONDARY_COLOR_LENGTH,
    MAX_SECONDARY_COLORS,
    limit_for,
    too_long,
)

REQUIRED_COLUMNS = ("spu", "sku", "name")

#: 列名 -> 枚举类
ENUM_COLUMNS: dict[str, type] = {
    # §9.1:导入模板**必须有受众列**。允许留空(留空进"待确认受众"),
    # 但**不允许按文件名或类目猜一个默认值填进去** —— 走的正是这里的
    # "值为空则跳过"分支,而不是某处 `or "WOMEN"`。
    # 放进 ENUM_COLUMNS 的收益:写错的取值当场报行号,而不是入库后
    # 在模特筛选那一步表现成"候选列表是空的"
    "audience": Audience,
    "garment_type": GarmentType,
    "pattern_type": PatternType,
    "strap_type": StrapType,
    "neckline_type": NecklineType,
    "back_style": BackStyle,
    "complexity": ProductComplexity,
}

TEXT_COLUMNS = ("category", "primary_color", "material", "notes")

ALL_COLUMNS = (*REQUIRED_COLUMNS, *TEXT_COLUMNS, *ENUM_COLUMNS, "secondary_colors")


@dataclass
class RowError:
    row_number: int  # 1 基,含表头偏移,便于用户在 Excel 里定位
    field: str
    message: str


@dataclass
class ImportResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _split_colors(raw: str) -> list[str]:
    if not raw:
        return []
    separator = "|" if "|" in raw else ","
    return [c.strip() for c in raw.split(separator) if c.strip()]


def parse_row(raw: dict[str, Any], row_number: int) -> tuple[dict[str, Any] | None, list[RowError]]:
    errors: list[RowError] = []
    data: dict[str, Any] = {}

    normalized = {
        (k or "").strip().lower(): (v.strip() if isinstance(v, str) else v)
        for k, v in raw.items()
    }

    # 超长报错而不是截断。截断的后果是「导入成功」但 SKU 少了半截 ——
    # 那条记录从此对不上任何东西,而且没有人会发现。
    for col in REQUIRED_COLUMNS:
        value = str(normalized.get(col) or "")
        if not value:
            errors.append(RowError(row_number, col, f"{col} 不能为空"))
        elif too_long(col, value):
            errors.append(
                RowError(row_number, col, f"{col} 超过 {limit_for(col)} 个字符")
            )
        else:
            data[col] = value

    for col in TEXT_COLUMNS:
        value = normalized.get(col)
        if not value:
            continue
        text = str(value)
        if too_long(col, text):
            errors.append(
                RowError(row_number, col, f"{col} 超过 {limit_for(col)} 个字符")
            )
        else:
            data[col] = text

    for col, enum_cls in ENUM_COLUMNS.items():
        value = normalized.get(col)
        if not value:
            continue
        candidate = str(value).strip().upper()
        valid = {e.value for e in enum_cls}
        if candidate not in valid:
            errors.append(
                RowError(
                    row_number,
                    col,
                    f"取值 {value!r} 不合法,可选:{', '.join(sorted(valid))}",
                )
            )
        else:
            data[col] = candidate

    colors = _split_colors(str(normalized.get("secondary_colors") or ""))
    if len(colors) > MAX_SECONDARY_COLORS:
        errors.append(
            RowError(
                row_number,
                "secondary_colors",
                f"辅助颜色最多 {MAX_SECONDARY_COLORS} 个,收到 {len(colors)} 个",
            )
        )
    elif any(len(c) > MAX_SECONDARY_COLOR_LENGTH for c in colors):
        errors.append(
            RowError(
                row_number,
                "secondary_colors",
                f"单个辅助颜色不能超过 {MAX_SECONDARY_COLOR_LENGTH} 个字符",
            )
        )
    else:
        data["secondary_colors"] = colors

    if errors:
        return None, errors
    return data, []


def _spu_audience_error(
    row: dict[str, Any], row_number: int, seen: dict[str, tuple[str | None, int]]
) -> RowError | None:
    """同一 SPU 的各行受众必须一致(§3.4 规则 2)。

    「有男款也有女款」是**两个 SPU**,不是一个 SPU 的两种受众 ——
    受众挂在 SPU 上,不参与颜色款继承。

    不做这个检查的后果不是"数据有点乱":`products` 是 SKU 级表,而草稿是
    SPU 级的。同 SPU 两行受众不同时,草稿的规则包取决于**查到哪一行** ——
    运营从红色 SKU 进去看到男装草稿,从蓝色 SKU 进去看到女装草稿,
    两边写的是同一行草稿。这个不确定性最终会在导出闸口被 §17 第 3 条拦下,
    但那时错误信息指向的是草稿,而根因在三天前的那次导入。

    **留空也是一种取值**:一行填 WOMEN、另一行留空的 SPU 同样不一致 ——
    它会让同 SPU 的一部分 SKU 进"待确认受众"、另一部分不进。
    全部留空则没问题(整个 SPU 待确认)。
    """
    spu = row.get("spu")
    if not spu:
        return None
    audience = row.get("audience")
    if spu not in seen:
        seen[spu] = (audience, row_number)
        return None
    first_audience, first_row = seen[spu]
    if audience == first_audience:
        return None

    def _label(value: str | None) -> str:
        return value if value else "留空(待确认)"

    return RowError(
        row_number,
        "audience",
        f"SPU {spu} 的受众与第 {first_row} 行不一致"
        f"({_label(first_audience)} vs {_label(audience)});"
        "受众挂在 SPU 上,同一 SPU 的所有行必须填同一个值 ——"
        "如果确实既有男款也有女款,请拆成两个 SPU",
    )


def parse_csv(content: str) -> ImportResult:
    result = ImportResult()
    # 去掉 Excel 导出常见的 BOM
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))

    if reader.fieldnames is None:
        result.errors.append(RowError(0, "file", "文件为空或缺少表头"))
        return result

    header = {(h or "").strip().lower() for h in reader.fieldnames}
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        result.errors.append(
            RowError(1, "header", f"缺少必需列:{', '.join(missing)}")
        )
        return result

    seen_sku: dict[str, int] = {}
    seen_spu_audience: dict[str, tuple[str | None, int]] = {}
    for index, raw in enumerate(reader, start=2):  # 第 1 行是表头
        if not any((v or "").strip() for v in raw.values() if isinstance(v, str)):
            continue  # 跳过空行
        row, errors = parse_row(raw, index)
        if errors:
            result.errors.extend(errors)
            continue
        assert row is not None
        sku = row["sku"]
        if sku in seen_sku:
            result.errors.append(
                RowError(index, "sku", f"SKU {sku} 与第 {seen_sku[sku]} 行重复")
            )
            continue
        seen_sku[sku] = index
        conflict = _spu_audience_error(row, index, seen_spu_audience)
        if conflict is not None:
            result.errors.append(conflict)
            continue
        result.rows.append(row)

    return result


def parse_json_rows(rows: list[dict[str, Any]]) -> ImportResult:
    """接受已解析的 JSON 数组,复用同一套校验。"""
    result = ImportResult()
    seen_sku: dict[str, int] = {}
    seen_spu_audience: dict[str, tuple[str | None, int]] = {}
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            result.errors.append(RowError(index, "row", "每个元素必须是对象"))
            continue
        row, errors = parse_row(raw, index)
        if errors:
            result.errors.extend(errors)
            continue
        assert row is not None
        if row["sku"] in seen_sku:
            result.errors.append(
                RowError(index, "sku", f"SKU {row['sku']} 与第 {seen_sku[row['sku']]} 条重复")
            )
            continue
        seen_sku[row["sku"]] = index
        conflict = _spu_audience_error(row, index, seen_spu_audience)
        if conflict is not None:
            result.errors.append(conflict)
            continue
        result.rows.append(row)
    return result
