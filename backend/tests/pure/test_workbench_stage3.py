"""阶段 3 的三个只读判定:异常聚合(FE-303)、SPU 聚合(FE-306)、CSV 导入(FE-305)。"""
from __future__ import annotations

from app.workbench import exceptions as ex
from app.workbench import import_plan as ip
from app.workbench import spu as sp
from app.workbench.flow import (
    STEP_ORDER,
    FlowResult,
    FlowStep,
    Issue,
    IssueLevel,
    NextAction,
    NextActionCode,
    StepResult,
    StepState,
)
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401

# ==========================================================
# 异常聚合(FE-303)
# ==========================================================


def _issue(
    level: IssueLevel,
    code: str,
    step: FlowStep = FlowStep.MATERIAL,
    ref: str | None = None,
) -> Issue:
    return Issue(level=level, code=code, message=f"msg-{code}", target_step=step, ref=ref)


def _result(*issues: Issue) -> FlowResult:
    return FlowResult(
        steps=tuple(
            StepResult(step, StepState.TODO, "") for step in STEP_ORDER
        ),
        issues=issues,
        completion=0,
        blocking_count=sum(1 for i in issues if i.level is IssueLevel.BLOCKING),
        pending_count=sum(1 for i in issues if i.level is IssueLevel.NEEDS_CONFIRM),
        reminder_count=sum(1 for i in issues if i.level is IssueLevel.REMINDER),
        current_step=FlowStep.MATERIAL,
        next_action=NextAction(
            NextActionCode.UPLOAD_MATERIAL, "补充素材", FlowStep.MATERIAL, ""
        ),
    )


def test_reminders_stay_out_of_the_exception_list():
    """§3.3.2 的口径:提醒不进徽标。异常列表收了它就永远清不空。"""
    entries = ex.entries_from_flow(
        "p1",
        "SKU-1",
        "SPU-1",
        _result(
            _issue(IssueLevel.BLOCKING, "A"),
            _issue(IssueLevel.REMINDER, "B"),
            _issue(IssueLevel.NEEDS_CONFIRM, "C"),
        ),
    )
    assert {e.code for e in entries} == {"A", "C"}


def test_grouping_collapses_the_same_cause_across_products():
    """"120 个问题"变成"7 类问题"是这一页的全部价值。"""
    entries = []
    for index in range(12):
        entries.extend(
            ex.entries_from_flow(
                f"p{index}",
                f"SKU-{index}",
                "SPU-1",
                _result(_issue(IssueLevel.BLOCKING, "MATERIAL_MISSING_ROLE", ref="PRODUCT_BACK")),
            )
        )
    sections = ex.group(entries)
    material = next(s for s in sections if s.step is FlowStep.MATERIAL)
    assert len(material.groups) == 1
    assert material.groups[0].count == 12
    assert material.count == 12


def test_all_five_categories_are_always_present_even_when_empty():
    """空分类也占位。少一个分类,界面上其余几类会跳位。"""
    sections = ex.group([])
    assert [s.step for s in sections] == list(STEP_ORDER)
    assert all(s.count == 0 for s in sections)


def test_groups_are_ordered_by_impact():
    """影响面最大的那一类排最前 —— 运营先处理它。"""
    entries = [
        *[
            ex.ExceptionEntry("p", f"S{i}", "SPU", FlowStep.COPY, IssueLevel.BLOCKING, "MANY", "m")
            for i in range(5)
        ],
        ex.ExceptionEntry("p", "S9", "SPU", FlowStep.COPY, IssueLevel.BLOCKING, "FEW", "m"),
    ]
    copy_section = next(s for s in ex.group(entries) if s.step is FlowStep.COPY)
    assert [g.code for g in copy_section.groups] == ["MANY", "FEW"]


def test_a_group_takes_the_worst_level_not_the_first_one():
    """同一码在两件商品上级别不同时,颜色不该由列表顺序决定。"""
    entries = [
        ex.ExceptionEntry(
            "p1", "S1", "SPU", FlowStep.IMAGE_SET, IssueLevel.NEEDS_CONFIRM, "X", "m"
        ),
        ex.ExceptionEntry("p2", "S2", "SPU", FlowStep.IMAGE_SET, IssueLevel.BLOCKING, "X", "m"),
    ]
    section = next(s for s in ex.group(entries) if s.step is FlowStep.IMAGE_SET)
    assert section.groups[0].level is IssueLevel.BLOCKING
    assert section.blocking_count == 2


def test_entries_carry_the_three_coordinates_needed_for_one_click():
    """§6.3.1 第二条:商品 + 步骤 + 具体位置。少一个就不叫一键跳转。"""
    (entry,) = ex.entries_from_flow(
        "p1",
        "SKU-1",
        "SPU-1",
        _result(_issue(IssueLevel.BLOCKING, "DRAFT_FIELD_ERROR", FlowStep.DRAFT, ref="price")),
    )
    assert (entry.product_id, entry.step, entry.ref) == ("p1", FlowStep.DRAFT, "price")


def test_batch_rows_become_entries_with_the_advice_attached():
    """批次失败的 hint 取异常码注册表里的 advice —— 那句话就是"下一步做什么"。"""
    entry = ex.entry_from_batch_row(
        {
            "status": "NEEDS_HUMAN",
            "error_code": "DRAFT_FIELD_INVALID",
            "error_message": "price 缺失",
            "product_id": "p1",
            "sku": "SKU-1",
            "spu": "SPU-1",
            "target_step": "DRAFT",
            "target_ref": "price",
            "batch_id": "b1",
        }
    )
    assert entry is not None
    assert entry.source == ex.SOURCE_BATCH
    assert entry.level is IssueLevel.NEEDS_CONFIRM
    assert entry.ref == "price"
    assert entry.hint  # advice 必须有
    assert entry.batch_id == "b1"


def test_successful_batch_rows_are_not_exceptions():
    for status in ("SUCCEEDED", "SKIPPED", "PENDING", "RUNNING"):
        assert ex.entry_from_batch_row({"status": status}) is None


def test_batch_row_with_a_bogus_step_falls_back_to_the_code_category():
    """target_step 脏了不该让整页 500。"""
    entry = ex.entry_from_batch_row(
        {"status": "FAILED", "error_code": "EXTRACTION_FAILED", "target_step": "NOT_A_STEP"}
    )
    assert entry is not None
    assert entry.step is FlowStep.ATTRIBUTE


def test_serialize_is_json_shaped():
    entries = ex.entries_from_flow(
        "p1", "SKU-1", "SPU-1", _result(_issue(IssueLevel.BLOCKING, "A"))
    )
    payload = ex.serialize(ex.group(entries))
    assert isinstance(payload, list) and len(payload) == len(STEP_ORDER)
    material = payload[0]
    assert material["step"] == FlowStep.MATERIAL.value
    assert material["groups"][0]["entries"][0]["product_id"] == "p1"
    summary = ex.summarize(ex.group(entries))
    assert summary["total"] == 1 and summary["blocking"] == 1


# ==========================================================
# SPU 聚合(FE-306)
# ==========================================================


def _sku(
    sku: str, *, color: str | None = None, size: str | None = None, **attrs: str
) -> sp.SkuRow:
    return sp.SkuRow(
        product_id=f"id-{sku}",
        sku=sku,
        color=color,
        size=size,
        attributes=dict(attrs),
        completion=100,
    )


def test_agreeing_attributes_are_common_not_repeated_per_sku():
    groups = sp.aggregate(
        [
            ("SPU-1", "泳衣 A", _sku("A-S", size="S", material="polyamide")),
            ("SPU-1", "泳衣 A", _sku("A-M", size="M", material="polyamide")),
        ]
    )
    (group,) = groups
    assert [c.field_name for c in group.common] == ["material"]
    assert group.common[0].value == "polyamide"
    assert group.inconsistent == ()


def test_disagreeing_common_attribute_is_reported_with_who_holds_what():
    """**这是这一页真正的价值。** 4 个 SKU 识别出两种面料时必须点名。"""
    groups = sp.aggregate(
        [
            ("SPU-1", "A", _sku("A-S", material="polyamide")),
            ("SPU-1", "A", _sku("A-M", material="polyamide")),
            ("SPU-1", "A", _sku("A-L", material="elastane")),
        ]
    )
    (group,) = groups
    assert group.common == ()
    (bad,) = group.inconsistent
    assert bad.field_name == "material"
    assert bad.value is None, "不一致时不该猜一个值出来"
    assert bad.values["polyamide"] == ("A-M", "A-S")
    assert bad.values["elastane"] == ("A-L",)


def test_variant_dimensions_never_count_as_inconsistent():
    """同一 SPU 下颜色尺码本来就该不同,报成不一致会让整页发红。"""
    groups = sp.aggregate(
        [
            ("SPU-1", "A", _sku("A-BLK-S", color="black", size="S", primary_color="black")),
            ("SPU-1", "A", _sku("A-WHT-M", color="white", size="M", primary_color="white")),
        ]
    )
    (group,) = groups
    assert group.inconsistent == ()
    assert group.variants == {"颜色": ("black", "white"), "尺码": ("M", "S")}


def test_a_missing_value_is_not_a_third_opinion():
    """"还没确认"和"确认成了另一个值"是两件事。混在一起,刚导入的商品全是红的。"""
    groups = sp.aggregate(
        [
            ("SPU-1", "A", _sku("A-S", material="polyamide")),
            ("SPU-1", "A", _sku("A-M")),  # 还没确认
        ]
    )
    (group,) = groups
    assert [c.field_name for c in group.common] == ["material"]
    assert group.inconsistent == ()


def test_spu_completion_is_the_minimum_not_the_average():
    """上架文件是整个 SPU 一起导的:三件做完一件没做,这个 SPU 还是导不了。"""
    rows = [
        ("SPU-1", "A", sp.SkuRow(product_id="1", sku="A-S", completion=100)),
        ("SPU-1", "A", sp.SkuRow(product_id="2", sku="A-M", completion=100)),
        ("SPU-1", "A", sp.SkuRow(product_id="3", sku="A-L", completion=40)),
    ]
    (group,) = sp.aggregate(rows)
    assert group.completion == 40


def test_blocking_counts_add_up_across_skus():
    rows = [
        ("SPU-1", "A", sp.SkuRow(product_id="1", sku="A-S", blocking_count=2)),
        ("SPU-1", "A", sp.SkuRow(product_id="2", sku="A-M", blocking_count=1)),
    ]
    (group,) = sp.aggregate(rows)
    assert group.blocking_count == 3


def test_explicit_field_list_excludes_variant_dimensions():
    """属性字典锁定后按字典比对,但变体维度永远不参与。"""
    groups = sp.aggregate(
        [
            ("SPU-1", "A", _sku("A-S", primary_color="black", material="x")),
            ("SPU-1", "A", _sku("A-M", primary_color="white", material="x")),
        ],
        common_fields=["primary_color", "material", "neckline_type"],
    )
    (group,) = groups
    names = {c.field_name for c in (*group.common, *group.inconsistent)}
    assert "primary_color" not in names
    assert names == {"material"}  # neckline_type 无人确认过,不显示空行


def test_groups_and_skus_are_sorted_for_a_stable_page():
    groups = sp.aggregate(
        [
            ("SPU-2", "B", _sku("B-1")),
            ("SPU-1", "A", _sku("A-2")),
            ("SPU-1", "A", _sku("A-1")),
        ]
    )
    assert [g.spu for g in groups] == ["SPU-1", "SPU-2"]
    assert [s.sku for s in groups[0].skus] == ["A-1", "A-2"]


def test_spu_serialize_keeps_common_and_variant_apart():
    """FE-306 的验收结果:公共属性与变体属性不混淆 —— 出参里也不能混。"""
    groups = sp.aggregate(
        [
            ("SPU-1", "A", _sku("A-S", color="black", size="S", material="x")),
            ("SPU-1", "A", _sku("A-M", color="black", size="M", material="y")),
        ]
    )
    (payload,) = sp.serialize(groups)
    assert set(payload) >= {"common", "inconsistent", "variants", "skus"}
    assert payload["variants"]["尺码"] == ["M", "S"]
    assert payload["inconsistent"][0]["field_name"] == "material"
    assert payload["common"] == []


# ==========================================================
# CSV 导入(FE-305)
# ==========================================================


def _csv(*rows: str) -> str:
    header = ",".join(ip.TEMPLATE_COLUMNS)
    return "\r\n".join([header, *rows]) + "\r\n"


def _row(spu: str, sku: str, name: str, **extra: str) -> str:
    values = {"spu": spu, "sku": sku, "name": name, **extra}
    return ",".join(values.get(column, "") for column in ip.TEMPLATE_COLUMNS)


def test_template_has_the_machine_column_names_on_the_first_line():
    """模板下载下来必须能直接导进去 —— 中文表头会被当成数据行然后报错。"""
    first_line = ip.template_csv().splitlines()[0]
    assert first_line.split(",") == list(ip.TEMPLATE_COLUMNS)
    for column in ("spu", "sku", "name"):
        assert column in first_line


def test_template_columns_stay_in_sync_with_the_parser():
    """模板少一列,运营会以为那个字段不能填。"""
    from app.services.product_import import ALL_COLUMNS, REQUIRED_COLUMNS

    assert set(ip.TEMPLATE_COLUMNS) == set(ALL_COLUMNS)
    assert ip.TEMPLATE_COLUMNS[: len(REQUIRED_COLUMNS)] == REQUIRED_COLUMNS


def test_every_template_column_has_a_chinese_label():
    missing = [c for c in ip.TEMPLATE_COLUMNS if c not in ip.COLUMN_LABELS]
    assert not missing, f"这些列在界面上会显示成英文: {missing}"


def test_column_help_exposes_enum_choices():
    """枚举取值不给出来,运营只能靠猜,然后收到一份错误文件。"""
    help_rows = {row["name"]: row for row in ip.column_help()}
    assert help_rows["garment_type"]["enum"], "款式类型没给可选值"
    assert help_rows["name"]["required"] is True
    assert help_rows["notes"]["required"] is False


def test_the_sample_row_in_the_template_actually_parses():
    """示例行本身必须是合法输入,否则模板一导入就报错。"""
    from app.services.product_import import parse_csv

    result = parse_csv(ip.template_csv())
    assert result.ok, [(e.row_number, e.field, e.message) for e in result.errors]
    assert result.rows[0]["sku"] == ip.SAMPLE_ROW["sku"]


def test_preview_splits_create_skip_and_error():
    content = _csv(
        _row("SPU-1", "SKU-NEW", "新款"),
        _row("SPU-1", "SKU-OLD", "已有的"),
        _row("SPU-2", "SKU-BAD", "枚举错的", garment_type="NOT_A_TYPE"),
    )
    view = ip.preview(content, existing_skus={"SKU-OLD"})
    counts = view.counts()
    assert counts == {"total": 3, "will_create": 1, "will_skip": 1, "error_rows": 1}
    assert not view.blocked


def test_error_rows_do_not_block_the_good_ones():
    """FE-305 的验收结果。预览就要把这件事说清楚,而不是等落库之后。"""
    content = _csv(
        _row("SPU-1", "SKU-1", "好的"),
        _row("SPU-1", "", "缺 SKU"),
        _row("SPU-1", "SKU-3", "也好的"),
    )
    view = ip.preview(content, existing_skus=set())
    assert view.will_create == 2
    assert view.error_rows == 1


def test_row_numbers_line_up_with_the_spreadsheet():
    """表头是第 1 行。行号错位的错误文件等于没有错误文件。"""
    content = _csv(_row("SPU-1", "SKU-1", "第二行"), _row("SPU-1", "", "第三行缺 SKU"))
    view = ip.preview(content, existing_skus=set())
    by_number = {r.row_number: r for r in view.rows}
    assert by_number[2].sku == "SKU-1"
    assert by_number[3].plan == ip.RowPlan.ERROR


def test_error_csv_echoes_the_original_row_so_it_can_be_re_uploaded():
    """只给"第 37 行有错"等于让运营自己去数到第 37 行。"""
    content = _csv(_row("SPU-9", "SKU-9", "有问题的", garment_type="NOPE"))
    view = ip.preview(content, existing_skus=set())
    text = ip.error_csv(view)
    lines = text.strip().splitlines()
    assert lines[0].split(",")[: len(ip.TEMPLATE_COLUMNS)] == list(ip.TEMPLATE_COLUMNS)
    assert "SKU-9" in lines[1], "原始行没有被回带"
    assert "garment_type" in lines[1]
    # 诊断列在最后,于是这份文件可以直接重传(解析器忽略多出来的列)
    assert lines[0].endswith("_行号,_错误列,_错误说明")


def test_error_csv_can_be_fed_back_into_the_parser():
    """"改完直接重新上传"必须是真的 —— 多出来的诊断列不能让解析失败。"""
    from app.services.product_import import parse_csv

    content = _csv(_row("SPU-9", "SKU-9", "名字", garment_type="NOPE"))
    text = ip.error_csv(ip.preview(content, existing_skus=set()))
    fixed = text.replace("NOPE", "ONE_PIECE")
    result = parse_csv(fixed)
    assert result.ok, [(e.field, e.message) for e in result.errors]
    assert result.rows[0]["sku"] == "SKU-9"


def test_a_bad_header_blocks_the_whole_file():
    """缺必需列时不该显示"0 行会新增",要明确说整份被拒了。"""
    view = ip.preview("foo,bar\r\n1,2\r\n", existing_skus=set())
    assert view.blocked
    assert view.header_problems
    assert view.rows == []
    assert "_错误说明" in ip.error_csv(view)


def test_empty_file_is_a_header_problem_not_a_crash():
    view = ip.preview("", existing_skus=set())
    assert view.blocked


def test_unknown_columns_are_flagged_but_not_fatal():
    """多一列不影响导入,但要说一声 —— 常见于运营在模板上加了自己的备注列。"""
    content = "spu,sku,name,我的备注\r\nSPU-1,SKU-1,名字,随便写\r\n"
    view = ip.preview(content, existing_skus=set())
    assert not view.blocked
    assert view.will_create == 1
    assert "我的备注" in view.unknown_columns


def test_duplicate_sku_inside_one_file_is_an_error_row():
    content = _csv(_row("SPU-1", "SKU-X", "第一次"), _row("SPU-1", "SKU-X", "第二次"))
    view = ip.preview(content, existing_skus=set())
    assert view.will_create == 1
    assert view.error_rows == 1


def test_serialize_carries_the_error_csv_for_download():
    """错误文件随预览一起返回,前端不用再传一次内容。"""
    content = _csv(_row("SPU-1", "", "缺 SKU"))
    payload = ip.serialize(ip.preview(content, existing_skus=set()))
    assert payload["counts"]["error_rows"] == 1
    assert payload["error_csv"], "错误文件没有一起返回"
    assert payload["rows"][0]["plan_label"] == ip.ROW_PLAN_LABELS[ip.RowPlan.ERROR]


def test_serialize_omits_the_error_csv_when_everything_is_clean():
    payload = ip.serialize(
        ip.preview(_csv(_row("SPU-1", "SKU-1", "好的")), existing_skus=set())
    )
    assert payload["error_csv"] == ""


def test_formula_injection_is_escaped_in_generated_files():
    """这些文件会被发给外部,一条 =HYPERLINK(...) 就够了。"""
    content = _csv(_row("SPU-1", "", "=cmd|'/c calc'!A1"))
    text = ip.error_csv(ip.preview(content, existing_skus=set()))
    assert "'=cmd" in text
