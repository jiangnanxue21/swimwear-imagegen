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


def provider_inline_size_message(
    size_bytes: int,
    *,
    provider_name: str,
    provider_limit_bytes: int,
    upload_limit_bytes: int | None = None,
) -> str:
    """构造「素材尺寸超过某 Provider 内联上限」的运营可读报错文案。

    ## 为什么单独有这个函数

    上传闸(`MAX_UPLOAD_SIZE_MB`,默认 20MB)与 Provider 的内联上限
    (例如 `FASHN_MAX_IMAGE_MB`,默认 10MB)是两道**不同且不等**的门:前者松、
    后者严。于是一张 15MB 的商品图能通过上传校验、却在建任务后才在 Provider
    侧失败。原文案只说「素材超过 10 MB」,运营无从判断三件事:这是哪个环节的
    限制?上传时明明放行了、为什么现在不行?怎么修?——这里一次说清,让这类
    失败可自我解释、可自行修复,而不必去翻代码或问人(REVIEW II.8 / DECISIONS)。

    入参一律以**字节**计,不读配置、不碰网络 —— 便于在纯单测里逐条质询文案,
    也让"用哪个上限"这件事由调用方显式决定,而不是藏在这个函数里。
    """
    size_mb = size_bytes / (1024 * 1024)
    # 尺寸给一位小数,避免「10 MB 超过 10 MB」这种把人绕晕的等值显示;
    # 上限恒是整数 MB(配置里 `* 1024 * 1024` 来的),照既有口径给整数。
    size_str = f"{size_mb:.1f} MB"
    limit_str = f"{provider_limit_bytes // (1024 * 1024)} MB"

    parts = [f"素材 {size_str} 超过 {provider_name} 的单图上限 {limit_str},无法提交"]
    if upload_limit_bytes is not None and upload_limit_bytes > provider_limit_bytes:
        upload_str = f"{upload_limit_bytes // (1024 * 1024)} MB"
        parts.append(f"上传闸放行到 {upload_str}、比它宽,所以文件能传上来却过不了这一关")
    # 只给**确定存在**的那一条出路。初版这里还写了「或改用不受此限的渠道」——
    # 而 `backend/app/providers/registry.py` 里注册的换装 Provider 只有 `mock` 与
    # `fashn`(`backend/app/providers/comfyui.py` 等有文件但没进那张表),
    # 运营照着去找会找到一片空白。
    # 一句办不到的建议比一句简略的报错更糟:前者让人去做一件做不到的事。
    # 等真有第二个可用 Provider 了,再由**调用方**把它作为参数传进来。
    parts.append(f"请把该图压到 {limit_str} 以内再重试")
    return "；".join(parts) + "。"


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
