"""上传素材的基础检查(需求第四章「素材基础检查」、第十九章上传限制)。

检查项:
1. 文件大小上限;
2. 真实图片格式(由 Pillow 判定,不看扩展名);
3. 最短边像素下限 —— 太小的图生成出来在网站上没法用;
4. 透明底素材必须真的有 alpha 通道。

设计取舍:格式/大小/损坏属于**硬性拒绝**,直接抛错;
尺寸偏小、缺 alpha 属于**可入库但标记未通过**,让运营在详情页看到并决定是否补图,
不阻断整条录入流程。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import ALPHA_REQUIRED_ASSET_TYPES, AssetType
from app.core.errors import ErrorCode, ValidationError
from app.services.image_probe import ImageInfo, probe_image


@dataclass(frozen=True)
class ValidatedUpload:
    data: bytes
    info: ImageInfo
    check_passed: bool
    check_message: str | None


def validate_upload(
    data: bytes,
    *,
    asset_type: AssetType,
    max_bytes: int,
    min_edge_px: int,
) -> ValidatedUpload:
    if not data:
        raise ValidationError("上传内容为空", code=ErrorCode.INPUT_INVALID)
    if len(data) > max_bytes:
        raise ValidationError(
            f"文件超过上限 {max_bytes // (1024 * 1024)} MB",
            code=ErrorCode.FILE_TOO_LARGE,
            detail={"size": len(data)},
        )

    info = probe_image(data)

    warnings: list[str] = []
    if min(info.width, info.height) < min_edge_px:
        warnings.append(
            f"最短边 {min(info.width, info.height)}px 低于建议值 {min_edge_px}px,生成图可能不够清晰"
        )
    if asset_type in ALPHA_REQUIRED_ASSET_TYPES and not info.has_alpha:
        warnings.append("透明底素材缺少 alpha 通道,请确认是否传错文件")

    return ValidatedUpload(
        data=data,
        info=info,
        check_passed=not warnings,
        check_message="；".join(warnings) if warnings else None,
    )
