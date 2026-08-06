"""图片探测:只用 Pillow 读取真实尺寸与格式,不信任客户端声明的 MIME。"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from app.core.errors import ErrorCode, ValidationError

#: Pillow 格式名 -> (MIME, 规范扩展名)
FORMAT_MAP: dict[str, tuple[str, str]] = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}

ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
#: 解压炸弹防护:超过该像素数直接拒绝
MAX_PIXELS = 80_000_000


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    mime_type: str
    extension: str
    format: str
    has_alpha: bool


def probe_image(data: bytes) -> ImageInfo:
    """读取图片元信息;无法解析或格式不允许则抛 ValidationError。"""
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            width, height = img.size
            mode = img.mode
            # 调色板图的透明信息在 info["transparency"] 里,必须在 verify() 前取
            info_dict = dict(img.info)
            # verify() 会消费文件对象,放在取完属性之后
            img.verify()
    except UnidentifiedImageError as exc:
        raise ValidationError(
            "文件不是可识别的图片", code=ErrorCode.IMAGE_UNREADABLE
        ) from exc
    except Image.DecompressionBombError as exc:
        raise ValidationError(
            "图片像素数过大,已拒绝", code=ErrorCode.FILE_TOO_LARGE
        ) from exc
    except Exception as exc:  # noqa: BLE001 - Pillow 会抛多种底层异常
        raise ValidationError(
            "图片已损坏或无法读取", code=ErrorCode.IMAGE_UNREADABLE
        ) from exc

    if fmt not in FORMAT_MAP:
        raise ValidationError(
            f"不支持的图片格式:{fmt or '未知'};仅接受 JPEG / PNG / WebP",
            code=ErrorCode.UNSUPPORTED_MEDIA_TYPE,
        )
    if width * height > MAX_PIXELS:
        raise ValidationError("图片像素数过大,已拒绝", code=ErrorCode.FILE_TOO_LARGE)

    mime, ext = FORMAT_MAP[fmt]
    return ImageInfo(
        width=width,
        height=height,
        mime_type=mime,
        extension=ext,
        format=fmt,
        has_alpha=mode in ("RGBA", "LA", "PA") or "transparency" in info_dict,
    )
