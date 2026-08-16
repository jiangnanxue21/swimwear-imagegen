"""逐图 missing 证据汇总成一次 run 的未解决字段。"""
from __future__ import annotations

from dataclasses import dataclass

from app.attributes.missing_summary import unresolved_missing
from tests.pure._helpers import BACKEND_ROOT


@dataclass
class _Evidence:
    field_name: str
    normalized_value: str | None = None
    missing_reason: str | None = None


def _pairs(rows):
    return [(item.field_name, item.reason) for item in unresolved_missing(rows)]


def test_a_back_view_value_resolves_the_front_views_missing_report():
    rows = [
        _Evidence("back_style", missing_reason="NOT_VISUALLY_DETERMINABLE"),
        _Evidence("back_style", normalized_value="CROSS_BACK"),
    ]
    assert _pairs(rows) == []


def test_resolution_is_independent_of_image_processing_order():
    rows = [
        _Evidence("back_style", normalized_value="CROSS_BACK"),
        _Evidence("back_style", missing_reason="NOT_VISUALLY_DETERMINABLE"),
    ]
    assert _pairs(rows) == []


def test_only_fields_without_any_usable_value_remain():
    rows = [
        _Evidence("back_style", missing_reason="NOT_VISUALLY_DETERMINABLE"),
        _Evidence("back_style", normalized_value="CROSS_BACK"),
        _Evidence("material", missing_reason="AWAITING_SUPPLIER_DATA"),
        _Evidence("material", missing_reason="AWAITING_SUPPLIER_DATA"),
    ]
    assert _pairs(rows) == [("material", "AWAITING_SUPPLIER_DATA")]


def test_an_unusable_or_blank_normalized_value_does_not_hide_the_warning():
    rows = [
        _Evidence("back_style", normalized_value=None),
        _Evidence("back_style", normalized_value="  "),
        _Evidence("back_style", missing_reason="NOT_VISUALLY_DETERMINABLE"),
    ]
    assert _pairs(rows) == [("back_style", "NOT_VISUALLY_DETERMINABLE")]


def test_mapping_rows_are_supported_for_api_and_archived_payloads():
    rows = [
        {
            "field_name": "back_style",
            "normalized_value": None,
            "missing_reason": "NOT_VISUALLY_DETERMINABLE",
        }
    ]
    assert unresolved_missing(rows)[0].as_dict() == {
        "field_name": "back_style",
        "reason": "NOT_VISUALLY_DETERMINABLE",
    }


def test_extraction_detail_endpoint_uses_the_backend_summary_and_waits_for_terminal():
    source = (BACKEND_ROOT / "app" / "api" / "attributes.py").read_text(
        encoding="utf-8"
    )
    assert "missing_summary.unresolved_missing(evidence)" in source
    assert "if detail.can_cancel" in source, (
        "RUNNING 时后续图片还可能解决前一张的 missing,不能提前宣布整批缺失"
    )
