"""属性识别接口的出入参(M3)。"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.field_limits import (
    MAX_CONFIRM_ITEMS,
    MAX_EXTRACT_FIELDS,
    MAX_EXTRACT_MEDIA_IDS,
)
from app.listings.sku_matrix import MAX_VARIANTS_PER_SPU


class ExtractionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    product_id: UUID
    extractor: str
    model_name: str | None = None
    prompt_version: str | None = None
    target_fields: list[str] = []
    image_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    #: 这次识别算哪一档(§4.6,取值见 `core.enums.ExtractionRunStatus`)。
    #: **由后端派生**(`run_state.terminal_status_for`),前端只展示 ——
    #: 在这一列之前,前端拿两个计数自己判三档,而它判漏了「循环没跑完
    #: 但一张都没失败」那一种,把跑了一半的识别显示成「识别完成」
    status: str = "FAILED"
    #: 模型输出目标清单外字段并被过滤的次数(§6.3)。如实报出来,
    #: 界面才有机会说"模型这次编了 3 个没问它的字段"
    fabricated_field_count: int = 0
    duration_ms: int | None = None
    created_at: datetime | None = None


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    media_asset_id: UUID
    field_name: str
    raw_value_json: Any = None
    #: 归一失败时为 None —— 模型自造的枚举值不会流到下游
    normalized_value: str | None = None
    model_confidence: float | None = None
    evidence_text: str | None = None
    #: 非空 = 这是一条"没有值"的证据(§4.5),取值见 `core.enums.MissingReason`。
    #: 界面按它区分"模型说看不清"和"模型给了个归一不了的值"——两者的
    #: normalized_value 都是 None,少了这一列它们长得一模一样
    missing_reason: str | None = None
    model_name: str = ""


class FieldSpecOut(BaseModel):
    """这个字段**是什么类型**(BLOCK-11)。来自 `attributes/registry.py`。

    ## 为什么必须由后端给

    前端要提交一个正确类型的值,就得知道 `secondary_colors` 是列表而
    `material` 是文本。它拿不到这件事:`GET /attributes` 原来只返回值,
    而值可能是 `null`(还没识别过),`Array.isArray(value_json)` 在那时
    分不出"这是个空的列表字段"和"这是个空的文本字段"。

    于是前端只能拿一个纯文本 `Input` 兜住所有字段,回传时一律是字符串 ——
    `["RED","BLUE"]` 在界面上显示成 `RED / BLUE`,原样提交回来就变成了
    一个**字符串**,而库里那一列从此不再是数组。下游按列表消费的地方
    (渠道层的颜色映射)拿到的是一个字符 `R`、一个字符 `E`……

    让前端按 `Array.isArray` 猜也不行,那是硬规则第 4 条禁止的:
    注册表是这件事的唯一事实源,再推一遍就是等着两边分叉。

    ## 三个字段各回答一个问题

        value_type    ENUM / ENUM_LIST / TEXT / TEXT_LIST / NUMBER / BOOL
        multi_value   要不要提交成数组。它与 value_type 冗余是**故意的** ——
                      前端最常问的就是这一句,让它去记哪几个类型算多值,
                      等于把注册表的 `__post_init__` 抄一份到前端
        options       枚举字段的合法取值。非枚举字段是空列表,不是 null:
                      "没有约束"和"约束读不到"在界面上是两种控件
    """

    value_type: str
    multi_value: bool = False
    options: list[str] = []


class AttributeValueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    field_name: str
    value_json: Any = None
    normalized_value: str | None = None
    source: str
    status: str
    #: 字段类型元数据。接口层用 `spec_for()` 填,ORM 上没有这个属性
    spec: FieldSpecOut | None = None
    model_confidence: float | None = None
    #: **唯一参与阈值判定的量。** None 表示未校准 —— 不是"置信度为零"
    system_confidence: float | None = None
    #: 因子分解。前端要能回答"0.71 是因为只有一张图还是因为图太糊"
    confidence_breakdown: dict[str, Any] | None = None
    evidence_ids: list[str] = []
    #: 这条事实依据的那批样品变了没有(§5.3 / AC-21)。
    #: 由 `attributes.service.stale_fields` 派生,**库里没有这一列** ——
    #: §8.2 写死了「下游过期一律派生比较,不写 STALE 列」。
    #:
    #: 默认 True 是刻意的:这个字段的两个来源(接口层填、Pydantic 兜底)
    #: 里,兜底那条意味着**没有人算过它**,而"没算过"必须显示成"证明不了
    #: 它仍然成立"。默认 False 的话,一次忘记赋值的表现是全库事实显示正常,
    #: 而那正是这一整条机制要防的事。
    facts_stale: bool = True
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None


class ExtractionDetailOut(ExtractionOut):
    evidence: list[EvidenceOut] = []


class ExtractRequest(BaseModel):
    """触发识别的入参。

    两个集合都有上限(BE-GLOBAL-07)。识别当前是**同步执行真实模型链路**的,
    一次传 5000 个素材 id 意味着一个跑不完的 HTTP 请求,而调用方看到的
    只是超时 —— 不指向"你传太多了"这个真正的原因。
    """

    #: 只识别这几条素材(增量)。不传则识别该商品全部可用素材
    media_asset_ids: list[UUID] | None = Field(
        default=None, max_length=MAX_EXTRACT_MEDIA_IDS
    )
    fields: list[str] | None = Field(default=None, max_length=MAX_EXTRACT_FIELDS)
    #: 只重跑这几个颜色作用域(§11 第一行的另一半)。
    #:
    #: ## 这个入参在此之前**不存在**,而重试范围已经算出来了
    #:
    #: `run_state.outcome_by_scope()` 从 14-13 起就在算 `RunOutcome.retry_scope`
    #: ——「这次 run 里哪几个颜色一条证据都没拿到」—— service 也把它回给了
    #: 调用方。缺的一直是**回来的那一跳**:调用方拿到「重试藏青和米白」这个
    #: 答案,却没有任何入参能表达它,只能人工去挑图 id。
    #:
    #: 挑图 id 不是"麻烦一点"而已:重试范围决定下一次付多少钱,而人工挑漏
    #: 一张的表现是那个颜色重试完仍然缺证据,于是再重试一次 —— 每一轮都是
    #: 真金白银。
    #:
    #: ## 与 `media_asset_ids` 是**交集**,不是二选一
    #:
    #: 两个都传时取交集(该颜色的、且在这个清单里的)。取并集的话
    #: 「只重跑这几张图」会被一个颜色作用域悄悄扩大成整个颜色,
    #: 而那是一次更贵的调用 —— 放宽范围这件事必须是有人明确要求的。
    #:
    #: ## 上限借 `MAX_VARIANTS_PER_SPU`,不另立一个数
    #:
    #: 一个 SPU 的颜色数上限就是这个量的天花板,再定义一个只会分叉。
    color_variant_ids: list[UUID] | None = Field(
        default=None, max_length=MAX_VARIANTS_PER_SPU
    )


class ConfirmItem(BaseModel):
    field_name: str = Field(min_length=1, max_length=64)
    value: Any


class ConfirmRequest(BaseModel):
    items: list[ConfirmItem] = Field(min_length=1, max_length=MAX_CONFIRM_ITEMS)

    @model_validator(mode="after")
    def _no_duplicate_fields(self) -> ConfirmRequest:
        """同一个字段不能在一次请求里出现两次(报告 6.6)。

        原来允许重复,而服务层是逐项写版本的 —— 于是一次请求会为同一个字段
        连续产生两个版本,响应里返回的却是**已经被后一项覆盖掉的前一项**。
        调用方看到的确认结果和库里的当前值不一致,且两者都"成功"了。

        挡在入参层而不是让服务层去重:去重要选保留哪一个,而那个选择
        没有业务依据 —— 提交者自己也不知道他想要哪个。
        """
        seen: set[str] = set()
        for index, item in enumerate(self.items):
            if item.field_name in seen:
                raise ValueError(
                    f"第 {index + 1} 项重复:字段 {item.field_name} 在一次请求里只能出现一次"
                )
            seen.add(item.field_name)
        return self
