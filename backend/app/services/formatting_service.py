"""网站图片输出(需求第十五章)。

把一张通过审核的候选图,渲染成网站各处直接可用的多个尺寸。

分成两层,原因是**尺寸决策必须能在没有 Pillow、没有数据库的情况下验证**:

    plan_outputs()    纯函数,只算尺寸 —— 输入宽高,输出每个用途该做多大
    render_outputs()  拿着计划真的去缩放编码,依赖 Pillow

一条贯穿始终的规则:**绝不放大**。候选图是 768×1024 时,详情页规格写的 1200×1600
不会被强行拉上去 —— 拉上去只会得到一张糊的假高清,不如老实输出原始尺寸并记录下来。
运营看到详情页图只有 768 宽,才知道该去调高生成分辨率,而不是被一张假 1200 蒙混过去。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.enums import FitMode, OutputPurpose


@dataclass(frozen=True)
class OutputSpec:
    """一个用途的输出规格。"""

    purpose: OutputPurpose
    #: 目标框。FitMode.NONE 时忽略
    width: int
    height: int
    fit: FitMode
    #: jpeg 质量;png 忽略
    quality: int = 88
    image_format: str = "JPEG"
    #: PAD 模式补边颜色。白色是电商图的默认底色
    background: tuple[int, int, int] = (255, 255, 255)


#: 默认规格表(需求第十五章的五个版本)。
#: 数字不是随手写的:详情页按 2x 视网膜的 600 逻辑宽给,缩略图按列表卡片 200 逻辑宽给,
#: 移动端按主流 375pt 屏 2x 给,正方形按社媒与网格位常用的 1:1 给。
DEFAULT_SPECS: tuple[OutputSpec, ...] = (
    OutputSpec(OutputPurpose.ORIGINAL, 0, 0, FitMode.NONE, quality=95),
    OutputSpec(OutputPurpose.DETAIL, 1200, 1600, FitMode.CONTAIN, quality=90),
    OutputSpec(OutputPurpose.THUMBNAIL, 400, 533, FitMode.CONTAIN, quality=82),
    OutputSpec(OutputPurpose.MOBILE, 750, 1000, FitMode.CONTAIN, quality=85),
    OutputSpec(OutputPurpose.SQUARE, 1000, 1000, FitMode.PAD, quality=88),
)


@dataclass
class PlannedOutput:
    """一个用途最终要输出的尺寸。"""

    purpose: OutputPurpose
    width: int
    height: int
    fit: FitMode
    quality: int
    image_format: str
    background: tuple[int, int, int]
    #: 内容实际被缩放到的尺寸。PAD 时它比画布小,画布其余部分是补边
    content_width: int
    content_height: int
    #: 为什么是这个尺寸 —— 直接存进 OutputAsset,排查时不用再反推
    note: str = ""

    @property
    def upscaled(self) -> bool:
        return False  # 恒为假:plan_outputs 从不放大,留此属性是为了让意图可读

    def to_dict(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose.value,
            "width": self.width,
            "height": self.height,
            "fit": self.fit.value,
            "content_width": self.content_width,
            "content_height": self.content_height,
            "note": self.note,
        }


def _contain_size(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """等比缩到框内。绝不放大,因此缩放比上限是 1。"""
    if src_w <= 0 or src_h <= 0:
        return 0, 0
    ratio = min(box_w / src_w, box_h / src_h, 1.0)
    return max(1, round(src_w * ratio)), max(1, round(src_h * ratio))


def plan_outputs(
    source_width: int,
    source_height: int,
    specs: tuple[OutputSpec, ...] = DEFAULT_SPECS,
) -> list[PlannedOutput]:
    """算出每个用途的最终尺寸。纯函数,不碰图片数据。"""
    if source_width <= 0 or source_height <= 0:
        raise ValueError("源图尺寸不合法")

    planned: list[PlannedOutput] = []
    for spec in specs:
        if spec.fit is FitMode.NONE:
            width = height = None
            content_w, content_h = source_width, source_height
            width, height = source_width, source_height
            note = "原始尺寸,仅重新编码"

        elif spec.fit is FitMode.CONTAIN:
            content_w, content_h = _contain_size(
                source_width, source_height, spec.width, spec.height
            )
            width, height = content_w, content_h
            note = (
                f"等比缩到 {spec.width}×{spec.height} 框内"
                if (content_w, content_h) != (source_width, source_height)
                else f"源图已小于 {spec.width}×{spec.height},保持原尺寸不放大"
            )

        else:  # PAD:画布尺寸固定,内容缩进去再补边
            content_w, content_h = _contain_size(
                source_width, source_height, spec.width, spec.height
            )
            width, height = spec.width, spec.height
            note = f"内容缩到 {content_w}×{content_h} 后补边到 {spec.width}×{spec.height}"

        planned.append(
            PlannedOutput(
                purpose=spec.purpose,
                width=width,
                height=height,
                fit=spec.fit,
                quality=spec.quality,
                image_format=spec.image_format,
                background=spec.background,
                content_width=content_w,
                content_height=content_h,
                note=note,
            )
        )
    return planned


@dataclass
class RenderedOutput:
    """渲染完成、等待落盘的一张图。"""

    plan: PlannedOutput
    data: bytes
    width: int
    height: int
    image_format: str
    extension: str
    metadata: dict[str, Any] = field(default_factory=dict)


def render_outputs(source: bytes, plan: list[PlannedOutput]) -> list[RenderedOutput]:
    """按计划渲染。这里才需要 Pillow。

    统一转 RGB:候选图可能带 alpha(透明底),而 JPEG 不支持 alpha,
    直接保存会抛异常。转的时候用规格里的背景色合成,不是简单丢掉 alpha 通道 ——
    丢通道会让半透明边缘变成脏黑边。
    """
    import io

    from PIL import Image

    rendered: list[RenderedOutput] = []
    with Image.open(io.BytesIO(source)) as original:
        original.load()

        for item in plan:
            image = original

            if item.fit is not FitMode.NONE and (
                image.width != item.content_width or image.height != item.content_height
            ):
                image = image.resize(
                    (item.content_width, item.content_height), Image.LANCZOS
                )

            image = _flatten(image, item.background)

            if item.fit is FitMode.PAD:
                canvas = Image.new("RGB", (item.width, item.height), item.background)
                offset = ((item.width - image.width) // 2, (item.height - image.height) // 2)
                canvas.paste(image, offset)
                image = canvas

            buffer = io.BytesIO()
            save_kwargs: dict[str, Any] = {"format": item.image_format}
            if item.image_format.upper() == "JPEG":
                save_kwargs.update(quality=item.quality, optimize=True, progressive=True)
            image.save(buffer, **save_kwargs)

            rendered.append(
                RenderedOutput(
                    plan=item,
                    data=buffer.getvalue(),
                    width=image.width,
                    height=image.height,
                    image_format=item.image_format.upper(),
                    extension=".jpg" if item.image_format.upper() == "JPEG" else ".png",
                    metadata={"note": item.note, "fit": item.fit.value},
                )
            )
    return rendered


def _flatten(image, background: tuple[int, int, int]):
    """把带 alpha 的图合成到背景色上。"""
    from PIL import Image

    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, background)
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    if image.mode != "RGB":
        return image.convert("RGB")
    return image
