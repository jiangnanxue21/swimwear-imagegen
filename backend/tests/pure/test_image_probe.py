"""图片探测:不信任扩展名,只信 Pillow 解出来的真实格式。"""
from __future__ import annotations

from app.core.errors import ErrorCode, ValidationError
from app.services.image_probe import probe_image
from tests.pure._helpers import expect_raises, jpeg_bytes, png_bytes


def test_reads_png_dimensions_and_mime():
    info = probe_image(png_bytes(640, 960))
    assert (info.width, info.height) == (640, 960)
    assert info.mime_type == "image/png"
    assert info.extension == ".png"


def test_reads_jpeg_dimensions_and_mime():
    info = probe_image(jpeg_bytes(1024, 1536))
    assert (info.width, info.height) == (1024, 1536)
    assert info.mime_type == "image/jpeg"
    assert info.extension == ".jpg"


def test_detects_alpha_channel():
    assert probe_image(png_bytes(mode="RGBA")).has_alpha is True
    assert probe_image(jpeg_bytes()).has_alpha is False


def test_rejects_non_image_bytes():
    err = expect_raises(ValidationError, probe_image, b"#!/bin/sh\nrm -rf /\n")
    assert err.code == ErrorCode.IMAGE_UNREADABLE


def test_rejects_truncated_image():
    data = png_bytes()[: len(png_bytes()) // 3]
    err = expect_raises(ValidationError, probe_image, data)
    assert err.code == ErrorCode.IMAGE_UNREADABLE


def test_rejects_disallowed_format():
    """GIF 能被 Pillow 识别,但不在业务白名单里,应报不支持而非无法读取。"""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("P", (100, 100)).save(buf, format="GIF")
    err = expect_raises(ValidationError, probe_image, buf.getvalue())
    assert err.code == ErrorCode.UNSUPPORTED_MEDIA_TYPE


def test_extension_lie_does_not_change_detected_type():
    """文件名叫 .png 但内容是 JPEG,探测结果必须以内容为准。"""
    info = probe_image(jpeg_bytes())
    assert info.mime_type == "image/jpeg"


def test_detects_palette_transparency():
    """调色板 PNG 的透明信息藏在 info["transparency"],不在 mode 里。

    回归用例:曾经误用模块级 Image.info 导致此分支恒为 False。
    """
    import io

    from PIL import Image

    img = Image.new("P", (300, 300))
    buf = io.BytesIO()
    img.save(buf, format="PNG", transparency=0)
    assert probe_image(buf.getvalue()).has_alpha is True


def test_opaque_palette_png_has_no_alpha():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("P", (300, 300)).save(buf, format="PNG")
    assert probe_image(buf.getvalue()).has_alpha is False
