"""多尺寸输出测试(需求第十五章 / 阶段 6)。

尺寸决策是纯函数,渲染只需要 Pillow —— 两者都能在没有数据库的环境里跑。
"""
from __future__ import annotations

import io

from app.core.enums import FitMode, OutputPurpose
from app.services.formatting_service import (
    DEFAULT_SPECS,
    OutputSpec,
    plan_outputs,
    render_outputs,
)
from tests.pure._helpers import expect_raises, jpeg_bytes, png_bytes


def _by_purpose(plans):
    return {p.purpose: p for p in plans}


# ---------- 规格表 ----------

def test_default_specs_cover_every_purpose_required_by_the_spec():
    """需求第十五章点名五个版本,一个都不能少。"""
    assert {s.purpose for s in DEFAULT_SPECS} == set(OutputPurpose)


def test_purposes_are_unique():
    purposes = [s.purpose for s in DEFAULT_SPECS]
    assert len(set(purposes)) == len(purposes)


# ---------- 尺寸计算 ----------

def test_original_keeps_the_source_size():
    plans = _by_purpose(plan_outputs(1536, 2048))
    original = plans[OutputPurpose.ORIGINAL]
    assert (original.width, original.height) == (1536, 2048)
    assert original.fit is FitMode.NONE


def test_contain_preserves_aspect_ratio():
    plans = _by_purpose(plan_outputs(1536, 2048))
    detail = plans[OutputPurpose.DETAIL]
    assert (detail.width, detail.height) == (1200, 1600)
    assert abs(detail.width / detail.height - 1536 / 2048) < 0.01


def test_contain_fits_inside_the_box_for_wide_sources():
    """宽图受宽度限制,高度必须小于框高。"""
    plans = _by_purpose(plan_outputs(2000, 1000))
    detail = plans[OutputPurpose.DETAIL]
    assert detail.width <= 1200 and detail.height <= 1600
    assert detail.width == 1200, "宽图应该顶到框宽"


def test_small_source_is_never_upscaled():
    """768×1024 的候选图不该被拉成 1200×1600 —— 那是一张糊的假高清。"""
    plans = _by_purpose(plan_outputs(768, 1024))
    detail = plans[OutputPurpose.DETAIL]
    assert (detail.width, detail.height) == (768, 1024)
    assert "不放大" in detail.note


def test_thumbnail_is_always_smaller_than_detail():
    plans = _by_purpose(plan_outputs(3000, 4000))
    assert plans[OutputPurpose.THUMBNAIL].width < plans[OutputPurpose.DETAIL].width


def test_square_canvas_is_exactly_square():
    plans = _by_purpose(plan_outputs(768, 1024))
    square = plans[OutputPurpose.SQUARE]
    assert square.width == square.height == 1000
    assert square.fit is FitMode.PAD


def test_square_pads_instead_of_cropping():
    """裁剪会切掉商品。电商图宁可留白,也不能把泳衣裁掉一角。"""
    plans = _by_purpose(plan_outputs(768, 1024))
    square = plans[OutputPurpose.SQUARE]
    assert square.content_width <= square.width
    assert square.content_height <= square.height
    # 竖图放进正方形:高度顶满,宽度留白
    assert square.content_height == 1000
    assert square.content_width < 1000


def test_square_does_not_upscale_a_tiny_source():
    plans = _by_purpose(plan_outputs(300, 400))
    square = plans[OutputPurpose.SQUARE]
    assert (square.width, square.height) == (1000, 1000), "画布尺寸固定"
    assert (square.content_width, square.content_height) == (300, 400), "内容不放大"


def test_every_plan_carries_a_human_readable_note():
    for plan in plan_outputs(1536, 2048):
        assert plan.note, f"{plan.purpose} 缺少尺寸说明"


def test_invalid_source_size_is_rejected():
    expect_raises(ValueError, plan_outputs, 0, 100)
    expect_raises(ValueError, plan_outputs, 100, -1)


def test_custom_spec_table_is_honoured():
    specs = (OutputSpec(OutputPurpose.THUMBNAIL, 200, 200, FitMode.CONTAIN),)
    plans = plan_outputs(1000, 1000, specs)
    assert len(plans) == 1
    assert (plans[0].width, plans[0].height) == (200, 200)


# ---------- 渲染 ----------

def _size_of(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return img.size


def test_render_produces_one_file_per_purpose():
    source = jpeg_bytes(1536, 2048)
    rendered = render_outputs(source, plan_outputs(1536, 2048))
    assert len(rendered) == len(DEFAULT_SPECS)
    assert {r.plan.purpose for r in rendered} == set(OutputPurpose)


def test_rendered_sizes_match_the_plan():
    source = jpeg_bytes(1536, 2048)
    for item in render_outputs(source, plan_outputs(1536, 2048)):
        assert _size_of(item.data) == (item.plan.width, item.plan.height), item.plan.purpose
        assert (item.width, item.height) == _size_of(item.data)


def test_square_output_is_actually_square_on_disk():
    source = jpeg_bytes(768, 1024)
    rendered = {r.plan.purpose: r for r in render_outputs(source, plan_outputs(768, 1024))}
    assert _size_of(rendered[OutputPurpose.SQUARE].data) == (1000, 1000)


def test_thumbnail_is_much_smaller_in_bytes_than_original():
    source = jpeg_bytes(2000, 2667)
    rendered = {r.plan.purpose: r for r in render_outputs(source, plan_outputs(2000, 2667))}
    assert len(rendered[OutputPurpose.THUMBNAIL].data) < len(rendered[OutputPurpose.ORIGINAL].data)


def test_transparent_source_is_flattened_not_rejected():
    """带透明底的候选图直接存 JPEG 会抛异常,必须先合成到背景色。"""
    source = png_bytes(800, 1000, mode="RGBA")
    rendered = render_outputs(source, plan_outputs(800, 1000))
    assert len(rendered) == len(DEFAULT_SPECS)
    for item in rendered:
        assert item.data, f"{item.plan.purpose} 渲染为空"


def test_rendered_files_carry_the_right_extension():
    source = jpeg_bytes(900, 1200)
    for item in render_outputs(source, plan_outputs(900, 1200)):
        assert item.extension == ".jpg"
        assert item.image_format == "JPEG"


def test_rendering_is_deterministic():
    """同一张源图渲染两次必须字节相同,否则去重和缓存都会失效。"""
    source = jpeg_bytes(1200, 1600)
    first = render_outputs(source, plan_outputs(1200, 1600))
    second = render_outputs(source, plan_outputs(1200, 1600))
    assert [r.data for r in first] == [r.data for r in second]


def test_render_does_not_mutate_the_source_bytes():
    source = jpeg_bytes(1000, 1000)
    snapshot = bytes(source)
    render_outputs(source, plan_outputs(1000, 1000))
    assert source == snapshot
