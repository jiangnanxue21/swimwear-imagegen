"""向导上的费用预估(PRD §13 阶段 6 批次 6-5 / AC-15 的「费用预估」那一项)。

**零基础设施依赖**:只 import `app.core.pricing`,不碰模型、库与 service。

## 它与 `services/spend.py` 是两件事,不要合并

    spend.summary     **已经发生的**花费。数字来自 `provider_usage_records`
                      的真实行,一条都不是估的(那个模块顶部的第一句话)
    本模块            **还没发生的**花费。数量来自当前上游还差多少,
                      单价来自同一份价目表

合并的后果是台账里混进估算值,而台账的全部价值就是"它不是估的"。
所以这里产出的每一行都带着 `is_estimate=True`,出参的键也叫 `estimate`
而不是 `cost` —— 两个数字同屏时,运营必须一眼分得出哪个是账、哪个是猜。

## 三条硬规矩

1. **查不到单价给 `None`,不给 0。** `core/pricing` 反复写过这一条:
   NULL 是「不知道」,0 是「免费」。给 0 的表现是向导上写着「预计 ¥0」,
   而运营点下去花了真钱 —— 一个比不显示更糟的数字。
2. **算不出数量的部分单独列,不并进总额。** 一个还没有图片集的颜色,
   要生成几张图今天算不出来(缺角度要等图片集才知道)。把它当成 0
   就是本仓那条「没算过 ≠ 没做」在费用上的又一次。
3. **不同币种不相加。** 与 `spend._daily` 同一条理由:micros 是某个币种的
   百万分之一,不同币种相加得到的数没有单位,但它长得像钱。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.core.pricing import UnitPrice, format_money, resolve_unit_price

#: 计费动作 -> 给运营看的名字。**键必须与 `record_usage(operation=...)` 传的
#: 字面量一致**,否则预估查的是一个厂商永远不会收费的动作,金额恒为"未配价"。
#:
#: 今天有五类动作会写流水(`grep record_usage`):
#:
#:     submit             出图。`generation_tasks` 提交那一刻
#:     download           取回候选图(多数厂商不单独计费,列出来是为了
#:                        价目表配了它的时候预估不会漏)
#:     vision_score       候选图评分。`evaluation_service` 每张一次
#:     attribute_extract  属性识别。`attributes/service` 每次 run 一条
#:     text_copy          文本文案生成。当前由独立能力测试的真实模型调用写入
OPERATION_LABELS: Mapping[str, str] = {
    "submit": "出图",
    "download": "取回候选图",
    "vision_score": "候选图评分",
    "attribute_extract": "属性识别",
    "text_copy": "文案生成",
}


@dataclass(frozen=True)
class Demand:
    """一项"还要做多少次"的用量需求。**由取数层算好递进来。**

    `units` 是**计费单位数**,不是张数 —— 两者常常相等但不总是
    (`UnitPrice.cost_for` 的文档写过同一句)。取数层负责翻译。
    """

    operation: str
    provider: str
    units: int
    #: 这个数是怎么来的,一句话。**空串不允许** —— 一个说不出来历的
    #: 数量在界面上会被当成权威,而它是估的
    basis: str


@dataclass(frozen=True)
class UnknownDemand:
    """算不出数量的那一部分。**它不是 0,是"不知道"。**"""

    operation: str
    label: str
    reason: str
    #: 涉及的对象(颜色码之类),排序过
    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class EstimateLine:
    operation: str
    label: str
    provider: str
    units: int
    basis: str
    #: 查不到价时 `None`。**不是 0**
    unit_price_micros: int | None
    currency: str | None
    cost_micros: int | None

    @property
    def priced(self) -> bool:
        return self.cost_micros is not None


@dataclass(frozen=True)
class Estimate:
    lines: tuple[EstimateLine, ...] = ()
    unknown: tuple[UnknownDemand, ...] = ()
    #: 按币种的合计。**只含查得到价的行**
    totals: Mapping[str, int] = field(default_factory=dict)
    #: 查不到价的动作名,排序过。非空时界面必须说"另有 N 项未配价"
    unpriced_operations: tuple[str, ...] = ()
    #: 「这一项为什么不在清单里」。**空清单与"没有这一项"要分得开** ——
    #: 当前评分器是 Mock 时它一分钱不花,而界面上"评分"那一行的消失
    #: 与"我们忘了算评分"长得一模一样
    notes: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        """这个预估算全了没有。**假的时候界面不许只显示一个总数。**

        两种不全:有动作没配价(金额未知),或有需求算不出数量(用量未知)。
        两者都会让总额偏小,而偏小的方向恒定 —— 一个总是偏低的预算数字
        比没有数字更危险。
        """
        return not self.unpriced_operations and not self.unknown


def estimate(
    demands: Sequence[Demand],
    *,
    price_book: Mapping[str, Mapping[str, UnitPrice]],
    unknown: Sequence[UnknownDemand] = (),
    notes: Sequence[str] = (),
) -> Estimate:
    """把用量需求 × 价目表算成预估。

    **同 (operation, provider) 的多条需求不在这里合并**:取数层给的每一条
    都带着自己的 `basis`(这个数是怎么来的),合并之后那句话只能留一条,
    而运营问"为什么是 12 张"时要的正是那句话。
    """
    lines: list[EstimateLine] = []
    totals: dict[str, int] = {}
    unpriced: set[str] = set()

    for demand in demands:
        if demand.units < 0:
            # **不 clamp 到 0。** 一个算出负张数的取数层有 bug,而把它悄悄
            # 改成 0 只会让那个 bug 表现成"预估偏小" —— 一个总是偏低的
            # 预算数字比没有数字更危险,而且没有任何地方会说发生过什么
            raise ValueError(
                f"{demand.operation}: 用量不能为负({demand.units});"
                "取数层算出负数说明它有 bug,这里不替它兜底"
            )
        if demand.units == 0:
            # 不产出零行。一行"0 次 × ¥0.04 = ¥0"在界面上占一格,
            # 而它说的是"这一项不用做" —— 那件事由步骤状态负责说
            continue
        price = resolve_unit_price(dict(price_book), demand.provider, demand.operation)
        if price is None:
            unpriced.add(demand.operation)
            lines.append(
                EstimateLine(
                    operation=demand.operation,
                    label=OPERATION_LABELS.get(demand.operation, demand.operation),
                    provider=demand.provider,
                    units=demand.units,
                    basis=demand.basis,
                    unit_price_micros=None,
                    currency=None,
                    cost_micros=None,
                )
            )
            continue
        cost = price.cost_for(demand.units)
        totals[price.currency] = totals.get(price.currency, 0) + cost
        lines.append(
            EstimateLine(
                operation=demand.operation,
                label=OPERATION_LABELS.get(demand.operation, demand.operation),
                provider=demand.provider,
                units=demand.units,
                basis=demand.basis,
                unit_price_micros=price.micros,
                currency=price.currency,
                cost_micros=cost,
            )
        )

    return Estimate(
        lines=tuple(lines),
        unknown=tuple(unknown),
        totals=dict(sorted(totals.items())),
        unpriced_operations=tuple(sorted(unpriced)),
        notes=tuple(notes),
    )


def serialize(result: Estimate) -> dict[str, object]:
    """预估 -> 接口出参。

    `is_estimate` 恒为 True 且写在最外层:这一段与 `/usage/spend` 的真实台账
    会出现在同一个界面上,而两者混淆的代价是有人拿预估去对账。
    """
    return {
        "is_estimate": True,
        "is_complete": result.is_complete,
        "totals": [
            {
                "currency": currency,
                "cost_micros": micros,
                "cost_display": format_money(micros, currency),
            }
            for currency, micros in result.totals.items()
        ],
        "unpriced_operations": [
            {"operation": op, "label": OPERATION_LABELS.get(op, op)}
            for op in result.unpriced_operations
        ],
        "unknown": [
            {
                "operation": u.operation,
                "label": u.label,
                "reason": u.reason,
                "refs": list(u.refs),
            }
            for u in result.unknown
        ],
        "notes": list(result.notes),
        "lines": [
            {
                "operation": line.operation,
                "label": line.label,
                "provider": line.provider,
                "units": line.units,
                "basis": line.basis,
                "unit_price_micros": line.unit_price_micros,
                "currency": line.currency,
                "cost_micros": line.cost_micros,
                # 查不到价时给 None 而不是 "¥0.00":格式化一个不存在的
                # 金额就是把"不知道"写成了"免费"
                "cost_display": (
                    format_money(line.cost_micros, line.currency or "")
                    if line.cost_micros is not None
                    else None
                ),
            }
            for line in result.lines
        ],
        # 一句给运营看的话,写在后端 —— 与 `spend.disclaimer` 同一条理由:
        # 「这不是账」这句话必须和产生数字的地方待在一起
        "disclaimer": (
            "以上为按当前上游用量与配置价目表推算的**预计**花费,"
            "不是已发生的费用。实际张数会随重试、评分退回与人工重做变化;"
            "已发生的花费见「付费调用花费」页。"
        ),
    }
