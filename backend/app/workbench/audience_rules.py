"""上架前的受众一致性断言(PRD v2 §17 前三条)。**纯函数,零依赖。**

§17 在提交前要求后端重新验证的三条(按 PRD 原文顺序):

    1. 商品受众有值,不是"待确认";
    2. 图片使用的模特受众与商品受众一致;
    3. 草稿使用的规则包受众与商品受众一致。

第三条尤其重要 —— 它拦的正是 §0.2 那条 `CATEGORY_ID` 硬编码缺陷的后果:
草稿用女装 spec 构造、商品是男装、所有字段校验都过。没有这条断言,
那个缺陷在提交环节不会暴露。

## 存量兼容矩阵(为什么第 1 条不是无条件阻断)

0030 迁移**不回填**商品受众(理由见迁移文件):存量商品全部 NULL,
它们的存量草稿 category_id 都是无前缀的 "swimwear"。对这批组合无条件
执行第 1 条,等于一夜之间把所有在途商品拦在导出上,而运营此刻能做的
只有逐件确认受众 —— 那是 §23.4 的灰度节奏该管的事,不是一条断言该
造成的事故。矩阵因此是:

    商品受众    草稿规则包受众    判定
    ---------  --------------  ------------------------------------------
    已确认      一致             放行
    已确认      不一致 / 无前缀   **阻断**(第 3 条;含"确认受众后旧草稿
                                 必须重新生成"这一情形)
    NULL       有前缀           **阻断**(数据不一致:草稿声明了受众,
                                 商品却没有 —— 不许从草稿把受众"捡"回来,
                                 §4.2 受众不被静默改写)
    NULL       无前缀           放行 + 警告"商品受众未确认"。这是唯一的
                                 兼容缝,只对受众前时代的存量成立;运营
                                 确认受众的那一刻,该商品永久离开这一行

矩阵保证的硬性质(§25.3 最后一条):**男装商品不可能用女装规则包完成
提交** —— audience=MEN 派生 men_swimwear,任何其它 category_id 都落在
"不一致"那一行。

## 第 2 条的现状

图片 ↔ 模特受众的核对需要素材层带生成溯源(MediaAsset 目前没有
generation_task_id 一类的列,只有迁移期的 legacy_kind/legacy_id)。
判定函数 `model_audience_problems` 已就绪并有穷举测试;接线等溯源列
落地(docs/DECISIONS.md §3.21 记了这条与后续迁移)。在那之前,
§12.4 的**生成前硬阻断**(generation_service)保证了错配的图根本
产生不出来 —— 第 2 条是第三道网,不是唯一一道。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.core.audience import (
    AudienceConflictError,
    category_code_for,
    coerce,
    embedded_audience_of,
    model_matches_product,
)
from app.core.enums import AUDIENCE_LABELS


@dataclass(frozen=True)
class AudienceGateResult:
    """三条断言的判定结果。problems 非空即阻断;warnings 只随草稿展示。"""

    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


def draft_audience_gate(
    product_audience: object, draft_category_id: str
) -> AudienceGateResult:
    """§17 第 1、3 条:商品受众 × 草稿规则包受众的判定矩阵(见模块文档)。"""
    draft_code = str(draft_category_id or "").strip()
    embedded = embedded_audience_of(draft_code)

    try:
        audience = coerce(product_audience)
    except ValueError:
        # 库里出现了一个不认识的受众字符串。它不是"没填",不能走兼容缝 ——
        # 那样这个错值会静默存在到永远
        return AudienceGateResult(
            problems=(
                f"商品受众取值不合法:「{product_audience}」,请先修正商品资料",
            )
        )

    if audience is None:
        if embedded is not None:
            return AudienceGateResult(
                problems=(
                    f"草稿使用了 {AUDIENCE_LABELS[embedded]}规则包"
                    f"({draft_code}),但商品受众未确认;"
                    "请先确认商品受众,再重新生成草稿",
                )
            )
        return AudienceGateResult(
            warnings=(
                "商品受众未确认;确认受众后需要重新生成草稿,"
                "届时将按受众规则包重新校验",
            )
        )

    try:
        expected = category_code_for(audience, draft_code if embedded else draft_code)
    except AudienceConflictError as exc:
        return AudienceGateResult(problems=(str(exc),))

    if draft_code != expected:
        got = (
            f"{AUDIENCE_LABELS[embedded]}规则包" if embedded else "受众前时代的规则包"
        )
        return AudienceGateResult(
            problems=(
                f"草稿使用的是{got}({draft_code}),与商品受众"
                f"({AUDIENCE_LABELS[audience]})不一致;请重新生成草稿",
            )
        )
    return AudienceGateResult()


def model_audience_problems(
    product_audience: object, model_audiences: Iterable[object]
) -> tuple[str, ...]:
    """§17 第 2 条:图片使用的模特受众必须与商品受众一致。

    `model_audiences` 是这批已批准图片各自溯源到的模特受众。
    接线现状见模块文档;判定本身在这里定死,接上溯源列时只加查询、不加规则。
    """
    audience = coerce(product_audience)
    problems: list[str] = []
    for value in model_audiences:
        model = coerce(value)
        if model is None:
            continue
        if not model_matches_product(audience, model):
            problems.append(
                f"已批准图片使用了{AUDIENCE_LABELS[model]}模特,"
                f"与商品受众({AUDIENCE_LABELS[audience]})不符;"
                "请退回该图片并更换受众匹配的模特重新生成"
            )
    # 去重保序:同一模特的多张图只报一次
    return tuple(dict.fromkeys(problems))
