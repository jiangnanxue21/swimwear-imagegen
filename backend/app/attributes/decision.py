"""属性状态的决策表(M3,§6.4)。**零依赖纯函数。**

判定规则是这一层最需要能穷举测试的东西:它决定一个模型的猜测会不会
变成一条真的会被发到平台上的商品属性。把它和数据库放在一起,
测一个边界条件就要先造一整套表数据 —— 于是没人会去测 0.919 和 0.92 的差别。

`app/attributes/service.py` 从这里取判定,不自己再写一份。
"""
from __future__ import annotations

from typing import Protocol

from app.attributes.registry import get_field
from app.core.enums import AttributeSource, AttributeStatus

#: 低于这个值一律不采信,连建议都不出。
#:
#: 0.5 不是随便定的:低于它意味着模型自己都倾向于"不是这个值",
#: 把它放进待确认队列只会让人工在一堆噪音里找信号,
#: 然后开始整批点"忽略"。
MIN_SUGGEST_CONFIDENCE = 0.5


class ExistingValue(Protocol):
    """判定只需要知道已有值的来源。

    用 Protocol 而不是 ORM 类型:这个模块不该知道数据库里长什么样,
    而测试也不该为了测一个 if 分支去造一行数据。
    """

    source: str


def decide_status(
    *,
    field_name: str,
    system_confidence: float | None,
    existing: ExistingValue | None = None,
) -> AttributeStatus:
    """决策表(§6.4)。

    | 情况 | 动作 |
    | --- | --- |
    | 字段策略是 NEVER | `CANDIDATE`(材质这类看不出来的,永不自动确认) |
    | 未校准 / < 0.5 | `CANDIDATE`(留证据不采信) |
    | 已有 MANUAL 值 | `CONFLICT`(永不覆盖人工值) |
    | ≥ 组阈值 且 库里为空 | `CONFIRMED` |
    | 其余 | `SUGGESTED` |

    顺序是有讲究的:NEVER 和未校准要排在人工值之前,否则一个
    `system_confidence=None` 的材质字段会因为"已有人工值"被判成 CONFLICT,
    而它根本就不该参与判定。
    """
    spec = get_field(field_name)
    threshold = spec.auto_confirm_threshold
    if threshold is None:
        # 看图看不出来的字段。哪怕置信度是 1.0 也不采信 ——
        # 那个 1.0 本身就没有依据
        return AttributeStatus.CANDIDATE
    if system_confidence is None or system_confidence < MIN_SUGGEST_CONFIDENCE:
        # 未校准返回的正是 None。fail closed:没有校准数据时,
        # 任何自动确认都是在假装我们知道准确率
        return AttributeStatus.CANDIDATE
    if existing is not None and existing.source == AttributeSource.MANUAL.value:
        # 人工值永不被自动覆盖(§6.2 规则 4)
        return AttributeStatus.CONFLICT
    if system_confidence >= threshold and existing is None:
        return AttributeStatus.CONFIRMED
    return AttributeStatus.SUGGESTED
