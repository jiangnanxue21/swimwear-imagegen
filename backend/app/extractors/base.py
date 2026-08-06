"""抽取器协议(M0 契约,§6.1)。

**一次调用只看一张图,而且不告诉它已有的值。**

v1 让模型「先独立判断,再与 known 比对」。提示词可以那样写,但模型看到
``{"primary_color": "NAVY"}`` 之后就会把图判成 NAVY —— 所谓交叉验证
只是复读我们喂进去的答案。比对必须由后端纯函数做,模型只提供证据。

逐图调用另有两个好处:新增一张素材时只识别新图(增量);
某张图解析失败不会毁掉整次识别。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class FieldObservation:
    """模型对某个字段说了什么。**不是结论。**"""

    name: str
    value: Any
    confidence: float | None = None
    evidence: str | None = None      # 理由,仅供人看,不参与计算


@dataclass(frozen=True)
class FieldMissing:
    """模型对某个字段说"没有值",并说明为什么(PRD v3.1 §4.5)。

    `reason` 的取值是 `core.enums.MissingReason` 的字符串形式。
    这里存字符串而不是枚举实例,是为了让本模块保持零依赖可离线测试 ——
    枚举校验在 `schema.parse_extraction_payload()` 与落库前各做一次。
    """

    name: str
    reason: str


@dataclass(frozen=True)
class ImageExtractionResult:
    """一张图的识别结果。

    `unreadable_fields` 是必需的,不是可选的:没有它,「看不清」
    只能表现为一个低置信度的猜测值,而那和「有把握但就是这个值」
    在数值上是同一个东西。

    ## A45-batch14:`missing` 与 `unreadable_fields` 的关系

    `missing` 是 `unreadable_fields` 的带原因版(§4.5):同样表示"没有值",
    但说清了是"这张图看不清"还是"这个角度判断不了"。两条通道都保留 ——
    Mock 与旧调用方继续用 `unreadable_fields`,落库层把它按
    `INSUFFICIENT_EVIDENCE` 归并进同一条写入路径(见 `run_extraction`),
    **不存在第二种"无值证据"的落库形状**。
    """

    fields: tuple[FieldObservation, ...] = ()
    unreadable_fields: tuple[str, ...] = ()
    missing: tuple[FieldMissing, ...] = ()
    model_name: str | None = None
    prompt_version: str | None = None
    duration_ms: int | None = None
    #: 脱敏后的响应元数据(响应 id、用量、finish_reason)。
    #: 全文按 §12.3 进对象存储,库里只留摘要
    meta: Mapping[str, Any] = field(default_factory=dict)


class ProductAttributeExtractor(Protocol):
    """识别器。M3 给出 mock 与 vision 两个实现。"""

    name: str

    def extract(
        self,
        *,
        image: Any,                             # M2 之后是 llm.images.PreparedImage
        target_fields: tuple[str, ...],
        enum_defs: Mapping[str, tuple[str, ...]],
        options: dict[str, Any],
    ) -> ImageExtractionResult:
        """识别单张图。

        **不接受 `known` 参数。** 这是一条契约级的约束,不是实现细节:
        签名里没有这个参数,就没人能在某次「临时调试」时把已有值传进去。
        """
        ...

    def declared_versions(self) -> tuple[str, str]:
        """`(model_version, prompt_version)`,**在第一次调用之前就要答得出来**。

        A45-batch14-20。§9.2 的幂等键含这两维,而键要挡的是「双击」和
        「网络重发」——两件事都发生在**付钱之前**。`ImageExtractionResult`
        上那两个同名字段是**响应回来才知道**的,照它们建键的实现会
        编译得过、测试也绿,只是每一次双击都买两次。

        ## 没实现这个方法的抽取器不会被硬编一个默认值

        `run_extraction` 用 `getattr` 取,取不到就**不建键**(退回本批之前的
        行为),而不是拿 `name` 或空串凑一个。凑出来的键不报错,只是把两个
        不同模型的同型请求判成同一次 —— 换了模型还在用旧结果。
        这与 batch14-18 的 `usage_from_candidates()` 是同一条口径:
        **基类默认不冒充权威。**

        实现方要保证的是「同一份配置下,这两个值在调用前后一致」。
        答不上来(比如版本只能从响应里读)就不要实现这个方法。
        """
        ...
