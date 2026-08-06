"""CSV 导入最小版(阶段 3,FE-305)。**零数据库依赖纯函数。**

验收结果是一句话:**错误行不影响正确行**。落到实现上是三件事:

    固定模板    列名写死在这里,并能下载一份带表头和示例行的空模板
    导入预览    落库**之前**告出"47 行会新增、2 行已存在跳过、1 行有错"
    错误文件    把出错的行连同原始内容一起吐回来,改完直接重新上传

第三件是最容易被做成"弹一个红字提示"的。那样运营手上有一份 50 行的
表和一句"第 37 行有错",他要自己去数到第 37 行 —— 而 Excel 的行号
和 CSV 的行号在有多行单元格时还不一样。所以错误文件必须**回带原始行**。

## 为什么不复用 `/api/products/import`

那条路径已经能导入了(`services/product_import.py` 的解析 + 落库),
本模块**不重写校验**,只在它外面补预览与错误回带:`parse_csv` 仍然是
唯一的校验实现处。重写一份的后果是"预览说合法、落库说非法"。

行号口径也跟着它:表头是第 1 行,数据从第 2 行起,和 Excel 左侧的行号对齐。
"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from app.services.export_format import escape_formula
from app.services.product_import import (
    ALL_COLUMNS,
    ENUM_COLUMNS,
    REQUIRED_COLUMNS,
    ImportResult,
    RowError,
    parse_csv,
)

#: 模板列顺序。必填列在前,其余按业务熟悉度排。
#:
#: 直接从 `product_import.ALL_COLUMNS` 派生而不是另写一份:那边加一列
#: 而这边忘了,模板就少一列,而运营会以为那个字段不能填。
TEMPLATE_COLUMNS: tuple[str, ...] = (
    *REQUIRED_COLUMNS,
    *(c for c in ALL_COLUMNS if c not in REQUIRED_COLUMNS),
)

COLUMN_LABELS: Mapping[str, str] = {
    "spu": "SPU(款号,必填)",
    "sku": "SKU(唯一,必填)",
    "name": "商品名(必填)",
    # §9.1:导入模板必须有受众列,允许留空。标签写清"留空的去向",
    # 否则运营会以为不填就是默认女装 —— 那正是这条要防的事
    "audience": "受众(WOMEN/MEN/UNISEX;留空则进「待确认受众」)",
    "category": "类目",
    "primary_color": "主色",
    "material": "面料",
    "notes": "备注",
    "garment_type": "款式类型",
    "pattern_type": "图案类型",
    "strap_type": "肩带类型",
    "neckline_type": "领口类型",
    "back_style": "背部款式",
    "complexity": "复杂度",
    "secondary_colors": "辅助颜色(多个用竖线分隔)",
}

#: 示例行。给一行真的能导进去的数据,比在文档里写一段说明有用得多。
SAMPLE_ROW: Mapping[str, str] = {
    "spu": "SW-2026-001",
    "sku": "SW-2026-001-BLK-M",
    "name": "高腰三角比基尼",
    # 示例行给一个**显式**受众,不留空:样例的作用是示范正确用法,
    # 而"留空也行"已经写在列标签里了
    "audience": "WOMEN",
    "category": "swimwear",
    "primary_color": "black",
    "material": "polyamide",
    "secondary_colors": "white|gold",
}


class RowPlan:
    """一行落库后会发生什么。字符串常量而非枚举:它只出现在接口出参里。"""

    CREATE = "CREATE"
    SKIP_EXISTING = "SKIP_EXISTING"
    ERROR = "ERROR"


ROW_PLAN_LABELS: Mapping[str, str] = {
    RowPlan.CREATE: "新增",
    RowPlan.SKIP_EXISTING: "已存在,跳过",
    RowPlan.ERROR: "有错,不导入",
}


@dataclass(frozen=True)
class PreviewRow:
    row_number: int
    plan: str
    sku: str = ""
    spu: str = ""
    name: str = ""
    #: 有错时的全部问题(同一行可能多列出错)
    problems: tuple[RowError, ...] = ()


@dataclass
class ImportPreview:
    rows: list[PreviewRow] = field(default_factory=list)
    #: 表头级问题(缺必需列、空文件)。它一出现,整份文件都进不去
    header_problems: list[RowError] = field(default_factory=list)
    #: 原始行内容,按行号索引。错误文件靠它回带
    raw_by_row: dict[int, dict[str, str]] = field(default_factory=dict)
    #: 文件里出现过、但模板不认识的列。不报错,只提示 —— 多一列不影响导入
    unknown_columns: list[str] = field(default_factory=list)

    @property
    def will_create(self) -> int:
        return sum(1 for r in self.rows if r.plan == RowPlan.CREATE)

    @property
    def will_skip(self) -> int:
        return sum(1 for r in self.rows if r.plan == RowPlan.SKIP_EXISTING)

    @property
    def error_rows(self) -> int:
        return sum(1 for r in self.rows if r.plan == RowPlan.ERROR)

    @property
    def blocked(self) -> bool:
        """整份文件都进不去的情形。只有表头级问题会这样。"""
        return bool(self.header_problems)

    def counts(self) -> dict[str, int]:
        return {
            "total": len(self.rows),
            "will_create": self.will_create,
            "will_skip": self.will_skip,
            "error_rows": self.error_rows,
        }


def template_csv() -> str:
    """空模板:第一行机器列名,第二行一条能直接导入的示例数据。

    表头**只放机器列名**。加一行中文说明会让 `parse_csv` 把它当成数据行,
    然后因为枚举非法报错 —— 一份下载下来就导不进去的模板比没有模板更糟。
    中文列名与枚举取值由接口的 `columns` 字段单独返回(`column_help`),
    界面显示成一张对照表放在上传框旁边。
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(list(TEMPLATE_COLUMNS))
    writer.writerow(
        [escape_formula(SAMPLE_ROW.get(column, "")) for column in TEMPLATE_COLUMNS]
    )
    return buffer.getvalue()


def column_help() -> list[dict[str, object]]:
    """列说明对照表。界面把它显示在上传框旁边,替代模板文件里的中文表头。"""
    return [
        {
            "name": column,
            "label": COLUMN_LABELS.get(column, column),
            "required": column in REQUIRED_COLUMNS,
            "enum": sorted(m.value for m in ENUM_COLUMNS[column])
            if column in ENUM_COLUMNS
            else None,
        }
        for column in TEMPLATE_COLUMNS
    ]


def read_raw_rows(content: str) -> dict[int, dict[str, str]]:
    """原样读一遍,只为错误文件能回带原始内容。

    行号与 `parse_csv` 对齐(表头 1,数据从 2 起)。空行跳过 —— 那边也跳过,
    两处不一致会让错误文件里出现一行空白且行号错位。
    """
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    if reader.fieldnames is None:
        return {}
    out: dict[int, dict[str, str]] = {}
    for index, raw in enumerate(reader, start=2):
        values = {
            (k or "").strip(): ("" if v is None else str(v))
            for k, v in raw.items()
            if k is not None
        }
        if not any(v.strip() for v in values.values()):
            continue
        out[index] = values
    return out


def _header_of(content: str) -> list[str]:
    reader = csv.reader(io.StringIO(content.lstrip("\ufeff")))
    for row in reader:
        return [(c or "").strip() for c in row]
    return []


def preview(content: str, existing_skus: Iterable[str]) -> ImportPreview:
    """落库前的预览。**不写库,也不判断谁先谁后。**

    `existing_skus` 由调用方从库里查(只查这份文件里出现的 SKU)。
    已存在的记为跳过而不是错误 —— 增量补充是批量导入的常态,
    重复执行必须安全(`product_service.import_products` 也是这个口径)。
    """
    result: ImportResult = parse_csv(content)
    raw_by_row = read_raw_rows(content)
    existing = {s for s in existing_skus}

    view = ImportPreview(raw_by_row=raw_by_row)

    known = set(TEMPLATE_COLUMNS)
    view.unknown_columns = [
        c for c in _header_of(content) if c and c.lower() not in known
    ]

    # 表头级问题:`parse_csv` 用行号 0/1 表示"文件为空"和"缺必需列",
    # 两者都导致它一行数据都不返回。这时不该显示"0 行会新增",
    # 而要明确告诉运营整份文件被拒了。
    header_problems = [e for e in result.errors if e.field in ("file", "header")]
    if header_problems:
        view.header_problems = list(header_problems)
        return view

    by_row: dict[int, list[RowError]] = {}
    for problem in result.errors:
        by_row.setdefault(problem.row_number, []).append(problem)

    rows: list[PreviewRow] = []
    for row in result.rows:
        row_number = _row_number_of(raw_by_row, row["sku"])
        rows.append(
            PreviewRow(
                row_number=row_number,
                plan=RowPlan.SKIP_EXISTING
                if row["sku"] in existing
                else RowPlan.CREATE,
                sku=row["sku"],
                spu=row.get("spu", ""),
                name=row.get("name", ""),
            )
        )
    for row_number, problems in by_row.items():
        raw = raw_by_row.get(row_number, {})
        rows.append(
            PreviewRow(
                row_number=row_number,
                plan=RowPlan.ERROR,
                sku=raw.get("sku", ""),
                spu=raw.get("spu", ""),
                name=raw.get("name", ""),
                problems=tuple(problems),
            )
        )

    rows.sort(key=lambda r: r.row_number)
    view.rows = rows
    return view


def _row_number_of(raw_by_row: Mapping[int, Mapping[str, str]], sku: str) -> int:
    """合法行的行号。`parse_csv` 的返回值里没有行号,只能按 SKU 回查。

    SKU 在文件内唯一(重复的已被 `parse_csv` 判成错误行),所以这个回查是确定的。
    查不到时返回 0 而不是抛错:行号只用于显示,不该因为它让整次预览失败。
    """
    for row_number, raw in raw_by_row.items():
        if (raw.get("sku") or "").strip() == sku:
            return row_number
    return 0


def error_csv(view: ImportPreview) -> str:
    """错误行文件。**原始列 + 两列诊断**,改完可直接重新上传。

    诊断列放在最后,因为 `parse_csv` 只认它认识的列,多出来的
    `_错误列` / `_错误说明` 会被忽略 —— 于是这份文件本身就是一份
    可以直接重传的输入,运营不需要先删掉两列。
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")

    if view.header_problems:
        writer.writerow(["_行号", "_错误列", "_错误说明"])
        for problem in view.header_problems:
            writer.writerow(
                [
                    problem.row_number,
                    escape_formula(problem.field),
                    escape_formula(problem.message),
                ]
            )
        return buffer.getvalue()

    error_rows = [r for r in view.rows if r.plan == RowPlan.ERROR]
    writer.writerow([*TEMPLATE_COLUMNS, "_行号", "_错误列", "_错误说明"])
    for row in error_rows:
        raw = view.raw_by_row.get(row.row_number, {})
        lowered = {(k or "").lower(): v for k, v in raw.items()}
        writer.writerow(
            [
                *(escape_formula(lowered.get(c, "")) for c in TEMPLATE_COLUMNS),
                row.row_number,
                escape_formula(";".join(p.field for p in row.problems)),
                escape_formula(";".join(p.message for p in row.problems)),
            ]
        )
    return buffer.getvalue()


def serialize(view: ImportPreview) -> dict[str, object]:
    """接口出参。`error_csv` 一并返回,前端直接存成文件,不用再传一次内容。"""
    return {
        "blocked": view.blocked,
        "counts": view.counts(),
        "unknown_columns": view.unknown_columns,
        "header_problems": [
            {"row_number": p.row_number, "field": p.field, "message": p.message}
            for p in view.header_problems
        ],
        "rows": [
            {
                "row_number": r.row_number,
                "plan": r.plan,
                "plan_label": ROW_PLAN_LABELS[r.plan],
                "sku": r.sku,
                "spu": r.spu,
                "name": r.name,
                "problems": [
                    {"field": p.field, "message": p.message} for p in r.problems
                ],
            }
            for r in view.rows
        ],
        "error_csv": error_csv(view) if (view.error_rows or view.blocked) else "",
    }


def sample_content(rows: Sequence[Mapping[str, str]]) -> str:
    """按模板列拼一份 CSV。测试与 `sample-data` 生成脚本用。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(list(TEMPLATE_COLUMNS))
    for row in rows:
        writer.writerow([row.get(c, "") for c in TEMPLATE_COLUMNS])
    return buffer.getvalue()
