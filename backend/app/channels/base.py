"""渠道抽象(M0 契约,§9.2)。

只有协议与纯数据,没有任何实现。SHEIN 适配器在 M7 之后。

## 为什么 `build_request` 要从 `submit` 里切出来

v1 的适配器只有 `submit()`。那样干跑就只能是「submit 里加一个 if」,
而 if 的两条分支迟早会长出差异 —— 干跑通过、真实提交报错,
是这类设计最典型的失败方式。

切开之后干跑与真实提交走的是**同一段构造逻辑**,干跑产出的报文
可以直接做快照测试;`submit()` 只剩下「把这个报文发出去」。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from app.core.enums import PublishOperation
from app.listings.contracts import (
    FieldViolation,
    ListingDraftData,
    MappedListing,
)

#: spec 里 `type:` 允许的取值。**这份集合是加载期与校验期共用的唯一来源** ——
#: 两边各写一份字符串的下场是 spec 里写了个 `type: float`,加载期放行、
#: 校验期不认识,于是那一列的类型约束静默失效。
FIELD_TYPES: frozenset[str] = frozenset({"string", "integer", "number"})

#: 会参与数值校验的类型。`minimum` / `maximum` / `decimal_places`
#: 只在这两种下有意义。
NUMERIC_FIELD_TYPES: frozenset[str] = frozenset({"integer", "number"})


@dataclass(frozen=True)
class FieldSpec:
    """渠道要求的一个字段。"""

    key: str
    required: bool = False
    max_length: int | None = None
    allowed_values: tuple[str, ...] = ()
    #: 该字段从哪来。受限 DSL,见 `source_expr.py`
    source: str | None = None
    transforms: tuple[Mapping[str, Any], ...] = ()
    #: 平台列头原文。Excel 按列头定位,不按列序号 —— 平台加一列
    #: 就会让所有按序号写的代码错位,而且错得很安静
    column_header: str | None = None
    note: str | None = None
    #: 值类型,取自 `FIELD_TYPES`。spec 里的 YAML 键是 `type`,
    #: 这里叫 `value_type` 是为了不和内置的 `type` 撞名 ——
    #: `FieldSpec.type` 在 dataclass 上还另有含义,重名会让读代码的人
    #: 分不清拿到的是字段声明还是字段的 Python 类型。
    #:
    #: None = 不做类型校验。**这是默认值**:绝大多数平台字段是自由文本,
    #: 给它们强加一个类型只会制造假阳性
    value_type: str | None = None
    #: 数值下界 / 上界(闭区间)。用 `Decimal` 而不是 `float`:
    #: 价格是这条链路上唯一会真的向消费者收款的数字,而
    #: `0.1 + 0.2 > 0.3` 在 float 下为真 —— 一个边界价永远说不清
    #: 到底该不该过,是这类校验最难查的故障
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    #: 小数位上限。平台的价格列几乎都有这条,而超位的表现是
    #: 整份文件被拒收,错误信息通常只说「格式错误」
    decimal_places: int | None = None

    @property
    def is_numeric(self) -> bool:
        return self.value_type in NUMERIC_FIELD_TYPES

    @property
    def is_todo(self) -> bool:
        """还没从官方文档确认下来的字段。

        生产环境下 spec 里存在 TODO 一律拒绝启动(`CHANNEL_SPEC_INCOMPLETE`)。
        硬规矩 1:第三方字段不凭记忆写代码。
        """
        return self.key.startswith("TODO") or self.source == "TODO"


@dataclass(frozen=True)
class ChannelFieldSpec:
    """某个 `(渠道, 类目, 站点)` 的完整字段规格。

    PRD v2 §18.2 起,规格同时是**规则包**的载体,新增三项:

        audience                  受众声明(§18.2 第 1 项)。None = 受众前
                                  时代的旧 spec(如 legacy 的 swimwear.yaml)
        enabled_hard_fail_codes   本包启用的硬错误码清单(§14.4:枚举是全集,
                                  规则包决定本次用哪些 —— 女装三条码在男装包
                                  里不启用,反之亦然)
        review_checks             本受众的重点检查区域(§14/§19)。审阅页的
                                  `review_focus` 从这里下发,**前端不许自己
                                  维护一份受众到检查项的映射**
        review_check_labels       上面那些键的中文名。和键放同一个文件,
                                  是为了让"加一个检查项忘了给中文名"在**同一屏**
                                  上看得见 —— 分到 Python 里的话,漏掉的表现是
                                  审阅页上冒出一个 `waistband_type`,而没有任何
                                  东西会叫一声(`spec_loader` 的加载期校验会)
    """

    channel: str
    category_id: str
    site: str
    spec_version: str
    header_fields: tuple[FieldSpec, ...] = ()
    row_fields: tuple[FieldSpec, ...] = ()
    manual_fields: tuple[str, ...] = ()
    consts: Mapping[str, Any] = field(default_factory=dict)
    audience: str | None = None
    enabled_hard_fail_codes: tuple[str, ...] = ()
    review_checks: tuple[str, ...] = ()
    review_check_labels: Mapping[str, str] = field(default_factory=dict)

    def review_focus(self) -> tuple[str, ...]:
        """重点检查区域的**中文名**,按 spec 里的顺序。

        查不到中文名时回落成键本身:少一条翻译的表现应该是界面上出现一个
        英文词(看得见、查得出),而不是那一项凭空消失。
        """
        return tuple(self.review_check_labels.get(c, c) for c in self.review_checks)

    def todos(self) -> tuple[FieldSpec, ...]:
        return tuple(f for f in (*self.header_fields, *self.row_fields) if f.is_todo)


@dataclass(frozen=True)
class TemplateRef:
    """官方 Excel 模板的引用。

    `checksum` 落库(`listing_drafts.template_checksum`):平台悄悄换了模板
    是这类集成最常见的故障,而表现是导出的文件被后台拒收,错误信息通常
    毫无指向性。指纹对不上时直接抛 `CHANNEL_TEMPLATE_MISMATCH`。
    """

    path: str
    template_version: str
    checksum: str


@dataclass(frozen=True)
class ExcelArtifact:
    """一次 Excel 导出的产物。

    校验报告是**独立文件**,不往官方工作簿里加 sheet(意见 #13)——
    平台的解析器可能按 sheet 序号读,多一个 sheet 就可能整份被拒。
    """

    workbook_path: str
    validation_json_path: str | None = None
    errors_csv_path: str | None = None


@dataclass(frozen=True)
class PreparedRequest:
    """构造好但**还没发出去**的平台请求。

    干跑产出的就是它(脱敏后)。`body` 不含任何签名与密钥 ——
    签名在 `submit()` 里现算,不进快照、不进日志、不进归档。
    """

    method: str
    path: str
    query: Mapping[str, Any] = field(default_factory=dict)
    body: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    idempotency_key: str | None = None


@dataclass(frozen=True)
class PublishOperationRef:
    """一次发布动作的身份。

    `operation` 与 `channel_listing_id` 都进幂等键(意见 #17):
    v1 的键不含操作类型,创建和更新会算出同一个键。
    """

    operation: PublishOperation
    channel_listing_id: str | None = None
    external_spu_id: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    accepted: bool
    external_spu_id: str | None = None
    platform_status: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class ChannelListingRef:
    """本地稳定的刊登身份(§11.3)。"""

    channel_listing_id: str
    channel: str
    shop_id: str
    spu: str
    external_spu_id: str | None = None


@dataclass(frozen=True)
class ChannelContext:
    """一次真实调用需要的运行期上下文。

    **凭证不在这里放明文。** 这个对象会进日志上下文,只放引用;
    真正的 secret 由 `settings_service` 在调用点解密,用完即弃。
    """

    channel: str
    shop_id: str
    site: str
    application_id: str
    data_region: str | None = None
    dry_run: bool = True          # 默认干跑。要发真请求必须显式关掉
    #: 渠道自己的运行期开关。加这个口子而不是给每个渠道往上加字段 ——
    #: Simulator 要用它接收 `scenario`(走哪一种模拟行为),而真实渠道
    #: 将来可能要 sandbox 标志、API 版本之类。**不放凭证**:
    #: 这个对象会进日志上下文,规矩见类文档
    options: Mapping[str, Any] = field(default_factory=dict)


class ChannelAdapter(Protocol):
    """一个渠道的完整能力。

    前四个方法是**纯函数**:不查库、不发网络、不读文件(spec 由调用方传入)。
    这不是风格偏好 —— 字段映射和校验是最需要穷举测试的部分,
    而只要它们能查库,就没人会写那个穷举测试。
    """

    name: str

    def field_spec(self, *, category_id: str, site: str) -> ChannelFieldSpec: ...

    def map_fields(
        self, draft: ListingDraftData, *, image_map: Mapping[str, Any]
    ) -> MappedListing: ...


    def validate(
        self, mapped: MappedListing, spec: ChannelFieldSpec
    ) -> list[FieldViolation]: ...

    def build_request(
        self, mapped: MappedListing, op: PublishOperationRef
    ) -> PreparedRequest: ...

    # 以下会真的碰外部世界
    def render_excel(
        self, mapped: MappedListing, template: TemplateRef
    ) -> ExcelArtifact: ...

    def submit(self, req: PreparedRequest, ctx: ChannelContext) -> SubmitResult: ...

    def poll(self, listing: ChannelListingRef, ctx: ChannelContext) -> str: ...
