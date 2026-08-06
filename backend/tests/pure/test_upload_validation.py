"""上传基础检查:硬性拒绝 vs 入库但标记未通过。"""
from __future__ import annotations

from app.core.enums import AssetType
from app.core.errors import ErrorCode, ValidationError
from app.services.upload_validation import validate_upload
from tests.pure._helpers import expect_raises, jpeg_bytes, png_bytes

MAX = 20 * 1024 * 1024
MIN_EDGE = 256


def _validate(data: bytes, asset_type=AssetType.GARMENT_FRONT, max_bytes=MAX, min_edge=MIN_EDGE):
    return validate_upload(data, asset_type=asset_type, max_bytes=max_bytes, min_edge_px=min_edge)


def test_accepts_normal_garment_image():
    result = _validate(jpeg_bytes(1200, 1600))
    assert result.check_passed is True
    assert result.check_message is None
    assert result.info.width == 1200


def test_rejects_empty_upload():
    err = expect_raises(ValidationError, _validate, b"")
    assert err.code == ErrorCode.INPUT_INVALID


def test_rejects_oversized_file():
    err = expect_raises(ValidationError, _validate, jpeg_bytes(800, 800), max_bytes=1024)
    assert err.code == ErrorCode.FILE_TOO_LARGE


def test_rejects_corrupted_file_before_size_checks():
    err = expect_raises(ValidationError, _validate, b"not-an-image-at-all")
    assert err.code == ErrorCode.IMAGE_UNREADABLE


def test_small_image_is_stored_but_flagged():
    """尺寸偏小不阻断录入,只标记,让运营决定是否补图。"""
    result = _validate(jpeg_bytes(120, 160))
    assert result.check_passed is False
    assert "低于建议值" in (result.check_message or "")


def test_cutout_without_alpha_is_flagged():
    result = _validate(jpeg_bytes(800, 1000), asset_type=AssetType.GARMENT_CUTOUT)
    assert result.check_passed is False
    assert "alpha" in (result.check_message or "")


def test_cutout_with_alpha_passes():
    result = _validate(png_bytes(800, 1000, mode="RGBA"), asset_type=AssetType.GARMENT_CUTOUT)
    assert result.check_passed is True


def test_non_cutout_without_alpha_is_fine():
    assert _validate(jpeg_bytes(800, 1000), asset_type=AssetType.MODEL_REFERENCE).check_passed


def test_multiple_warnings_are_joined():
    result = _validate(png_bytes(100, 100), asset_type=AssetType.GARMENT_CUTOUT)
    assert result.check_passed is False
    assert "；" in (result.check_message or "")
