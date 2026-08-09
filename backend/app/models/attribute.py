"""属性层模型(M3,§5)。

四张表,职责严格分开:

    product_attribute_extractions  一次识别请求(可能覆盖多张图、多个字段)
    attribute_evidence             一图一字段一条**证据**
    product_attribute_values       当前采信的**值**,带版本
    attribute_calibrations         model_confidence -> 真实准确率的分箱表

## 证据不是值

这是本层最重要的一条区分。`attribute_evidence` 记的是「模型看着这张图,
对这个字段说了什么」;`product_attribute_values` 记的是「我们决定采信什么」。

混成一张表的代价:模型说了什么会随模型换版而变,而我们采信什么是一个
需要被审计、被人工推翻、被回滚的业务事实。合并之后,重新识别一次
就会把人工确认过的值冲掉 —— 而那正是 §6.2 规则 4 明令禁止的。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.attributes import run_state
from app.core.enums import AttributeStatus, ExtractionRunStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ProductAttributeExtraction(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一次识别请求。"""

    __tablename__ = "product_attribute_extractions"
    __table_args__ = (
        Index("ix_attr_extractions_product", "product_id", "created_at"),
        # 异步 worker 与兜底重投按状态/时间扫。没有这条索引,队列积压时
        # 每 30 秒的恢复拍都会全表扫描一次识别历史。
        Index("ix_attr_extractions_status_created", "status", "created_at"),
        # 归属维度的查询入口。§9.2 的查重按 `idempotency_key` 走下面那条索引,
        # 而「这个 SPU 最近识别过几次」是运营侧的问题,走这条
        Index("ix_attr_extractions_spu", "spu_id", "created_at"),
        # §9.2 的裁判。**谓词不是手打的**,由 `run_state.unique_index_predicate()`
        # 生成 —— 它和迁移 0040 里那条 DDL 是同一个函数的两次调用。
        #
        # 为什么必须是**部分**唯一索引:全表唯一的话,一次 FAILED 之后同样的
        # 输入再也建不出第二个 run,而那正是重试的定义。理由全文在
        # `run_state.unique_index_predicate` 的文档里,别在这里复述第二遍。
        Index(
            "uq_attr_extractions_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text(run_state.unique_index_predicate()),
        ),
    )

    product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    #: 这次识别算哪一档(§4.6)。**A45-batch14-20 起是真列,不再是派生属性。**
    #:
    #: 派生属性的那一版(14-11)是刻意的过渡:判定验完了、列还没有,
    #: 而前端正在拿两个计数自己判三档。列一落库,派生属性就必须让位 ——
    #: 两者并存的话,`status IN (...)` 这条索引谓词读的是列,
    #: 而接口返回的是属性,两个答案漂移时没有任何地方会报。
    #:
    #: 判定仍然只有 `run_state.terminal_status_for` 一处,调用点从读取方
    #: (原来的属性)挪到了写入方(`attributes/service.run_extraction`)。
    #:
    #: `server_default` 取 FAILED 是**朝不放行那一侧倒**:这一列落库之前
    #: 写下的行没有人算过它的状态,而 FAILED 既不进事实合并
    #: (`AUTHORITATIVE_STATUSES`)也不占幂等键(`KEY_OCCUPYING_STATUSES`)。
    #: 反过来默认 COMPLETED 的话,一次从来没有被判定过的 run 会以
    #: 「算数」的身份参与两件要花钱的事。**不回填**的理由写在迁移 0040 里。
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=ExtractionRunStatus.FAILED.value
    )
    #: 归属(§9.2 幂等键的第一维)。可空 —— 老建档路径(`create_product`、
    #: CSV 导入)今天还不写 `products.spu_id`,那是阶段 1 的剩余项。
    #: 取不到就**不建键**,不拿 `product_id` 凑一个假的 SPU 作用域:
    #: 凑出来的键不报错,只是让两个不同 SPU 的同型请求算出同一个键
    spu_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spus.id", ondelete="SET NULL"), nullable=True
    )
    #: 这次 run 实际吃进去的那批素材的指纹(§5.3)。
    #: `scope_fingerprint.facts_stale` 拿它和当前指纹比 —— 有存量才有比较对象,
    #: 这一列就是那个存量。空值一律判过期(算不出来就算过期)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 请求的作用域(§4.6)。存 `run_state.canonical_scope()` 产出的 token,
    #: **不存人读的描述** —— 它是幂等键的一部分,必须逐字节稳定。
    #: 用 Text 不用 String(n):颜色清单形状下它随变体个数增长,
    #: 而一个被截断的作用域会静静地和另一个作用域算出同一个键
    requested_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: §9.2 的幂等键。可空:建不出键的那几种情况(没有 spu_id、
    #: 抽取器报不出调用前版本、只识别指定素材)一律留空而不是编一个 ——
    #: 空键不参与上面那条唯一索引(NULL 在唯一索引里互不相等),
    #: 于是「建不出键」的表现是回到本批之前的行为,不是错误地拦住请求
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: API 只负责排队，worker 必须能仅凭 run_id 重建请求。把范围只放在
    #: Celery 消息里会让 broker 丢消息后的补投拿不到原请求。
    requested_media_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    requested_variant_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    #: 排队时白名单实际选中的素材快照。worker 只读这份，不把排队期间新传
    #: 的图静默扩大进一次已经确定了幂等键和费用上限的请求。
    input_asset_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    #: 每张实际跑过的图只留一条 `{asset_id, scope, succeeded}`。它既是故障重试时
    #: 跳过已结算图片的 checkpoint，也是失败颜色不能从 evidence 缺席反推的事实来源。
    image_outcomes: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    failed_scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    requested_by: Mapped[str] = mapped_column(
        String(128), nullable=False, default="system", server_default="system"
    )
    #: 协作式取消：接口只写这一位，worker 在每张图之间提交 checkpoint
    #: 后刷新它。终态不允许再改这一位。
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    extractor: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    #: 本次识别覆盖了哪些字段。**存下来**而不是从证据反推:
    #: 模型对某个字段一条证据都没给,和我们根本没问它,是两件事
    target_fields: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 模型输出了目标清单之外的字段几次(§6.3 硬规则:过滤并计入
    #: "不可见属性编造率",不入确认队列)。A45-batch14 / 迁移 0036。
    #:
    #: 是真实列不是 JSON 里的一个键,因为 §14.4 的指标要 SUM 它 ——
    #: 而"编造被过滤掉了"这件事如果只出现在日志里,指标就永远是 0,
    #: 一个恒为 0 的指标比没有指标更糟,它看起来在说"模型从不编造"。
    fabricated_field_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 模型原始响应。**库里只存脱敏摘要 + 对象存储 key**(§12.3)。
    #: 全文进 JSONB 的话,一次识别几十 KB、一天几千次,几周就能把库撑爆,
    #: 而且里面可能有 base64 图片
    raw_response_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    raw_response_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)

    #: token 用量走独立列,不放 JSON —— 它要被聚合成成本曲线,
    #: 而 JSONB 里的数字没法建索引也没法 SUM
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

# A45-batch14-20:原来这里有一个派生属性 `status`。它已经让位给上面那一列。
#
# **不要把它加回来。**并存的后果不是重复,是两个答案:
# `uq_attr_extractions_idempotency_key` 的谓词 `status IN (...)` 由数据库
# 读**列**求值,而接口和界面读**属性** —— 两者漂移时,现象是「代码说该新建
# 一个 run,库说这个键被占了」,而报出来的是一个和用户动作毫无关系的 409。
#
# 判定本身一个字没动,仍然是 `run_state.terminal_status_for`;换的只是
# 调用点:从读取方(属性)挪到了写入方(`attributes/service.run_extraction`)。
# 守卫 `test_the_terminal_verdict_is_asked_once_at_its_host_and_not_re_derived`
# 按**参数化宿主**盯着它,搬家时改那个常量即可(DECISIONS §3.33)。


class AttributeEvidence(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一图一字段一条证据(§5.1)。

    **不含结论。** 合并、比对、判定全在纯函数里(M4 的 `extractors/merge.py`),
    模型只提供这一条条原料。
    """

    __tablename__ = "attribute_evidence"
    __table_args__ = (
        Index("ix_attr_evidence_field_media", "field_name", "media_asset_id"),
        Index("ix_attr_evidence_extraction", "extraction_id"),
    )

    extraction_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("product_attribute_extractions.id", ondelete="CASCADE"),
        nullable=False,
    )
    media_asset_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 模型原始输出,未归一。归一失败时它是唯一能回答"模型到底说了什么"的东西
    raw_value_json: Mapped[Any] = mapped_column(JSONB, nullable=False)
    #: 归一到 core/enums.py 之后的值;归一失败为 NULL
    normalized_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 模型自报。**不直接参与阈值判定** —— 那个量是 system_confidence
    model_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    #: 模型给的理由。仅供人看,不参与任何计算
    evidence_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: "这个字段为什么没有值"(§4.5:落 evidence 不落 value)。
    #: 取值是 `core.enums.MissingReason`;NULL = 这是一条带值的普通证据。
    #: A45-batch14 / 迁移 0036。带 reason 的行 `normalized_value` 恒为 NULL,
    #: 合并层因此天然不让它投票 —— 这不是巧合,是 `apply_evidence` 的
    #: "归一失败不参与合并"分支在兜着,改那条分支前先想到这里。
    missing_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    region_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="")


class ProductAttributeValue(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """当前采信的属性值,带版本(§5.2)。

    `is_current` 上挂部分唯一索引:同一 (owner, field) 只能有一个当前值,
    历史版本全部保留。平台驳回之后要能回答「上次提交时这个字段是什么值」,
    没有版本就只能靠猜。
    """

    __tablename__ = "product_attribute_values"
    __table_args__ = (
        # 部分唯一索引。**必须同时写在 ORM 和迁移里** —— 上一轮评审第 20 条:
        # 只写在迁移里的话 create_all() 建出来的测试库没有它,
        # 并发冲突在测试里永远复现不出来,而生产环境会 500
        Index(
            "uq_pav_current",
            "owner_type",
            "owner_id",
            "field_name",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index(
            "ix_pav_field_value",
            "field_name",
            "normalized_value",
            postgresql_where=text("is_current"),
        ),
    )

    owner_type: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 对应实体主键。CHANNEL 层是 channel_listing_id,所以是 TEXT 不是 UUID。
    #:
    #: 宽度 160 而不是 64(A44 / 0027):VARIANT 层的 owner_id 是
    #: `<len>:<spu>/<variant_id>` —— 上面那条唯一索引里**没有 SPU**,
    #: 不套命名空间的话两个 SPU 只要有同名颜色就共用同一行属性,
    #: 给 A 的黑色确认一次,B 的黑色跟着变(见 `attributes/validation.py`)。
    #: spu 与 variant_id 各自可达 64,64 位塞不下。
    owner_id: Mapped[str] = mapped_column(String(160), nullable=False)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 单值 "V_NECK";多值 ["WHITE","GOLD"];结构值 {...}
    value_json: Mapped[Any] = mapped_column(JSONB, nullable=False)
    #: 单值时的可索引投影;多值时为 NULL
    normalized_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    model_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    #: 唯一参与阈值判定的量。**未校准时为 NULL**,而 NULL 不是 0 ——
    #: 前者是"这个数字没有意义",后者是"置信度很低",两者的处理完全不同
    system_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AttributeStatus.CANDIDATE.value
    )
    evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    #: 置信度的因子分解。前端要能回答"0.71 是因为只有一张图还是因为图太糊"
    confidence_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    calibration_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 这条事实产生/确认时,**它自己那个作用域**的样品指纹(§4.5 / §5.3)。
    #: 迁移 0042。`scope_fingerprint.facts_stale` 拿它和当前作用域指纹比。
    #:
    #: **不是 run 行上那一列。** 那一列(0040,`ProductAttributeExtraction`)
    #: 记的是"这次 run 吃进去的是哪批素材";这一列记的是"这条事实是按哪批
    #: 素材立起来的"。一次 run 产出多条事实,一条事实跨多次 run 存活 ——
    #: 拿 run 行的指纹比属性值,共享事实与颜色事实会共用同一个答案,
    #: 于是给颜色 A 补图会 stale 掉 B 色事实,正是 D1 那个问题从后门回来。
    #:
    #: 该比哪个作用域由 `validation.fingerprint_scope()` 按 owner 决定,
    #: 不由写入点各自判断。空值一律判过期(算不出来就算过期),
    #: **不回填**的理由写在迁移 0042 里
    input_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(nullable=True)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


class AttributeCalibration(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """model_confidence 的分箱校准表(§6.5)。

    没有它,阈值是一个没有意义的数字:模型说 0.93,那到底对应多高的真实准确率?
    不同字段、不同模型、不同提示词版本的答案完全不同。

    **未校准的组合一律不允许自动确认**(fail closed)。这不是保守,
    是因为「看起来很高的置信度」会让人以为它有意义。
    """

    __tablename__ = "attribute_calibrations"
    __table_args__ = (
        UniqueConstraint(
            "field_name",
            "model_name",
            "prompt_version",
            "bin_lower",
            "calibration_version",
            name="uq_attr_calibration_bin",
        ),
    )

    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    bin_lower: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    bin_upper: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    #: 该箱内以人工标注为准的真实准确率
    observed_accuracy: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calibration_version: Mapped[str] = mapped_column(String(32), nullable=False)
