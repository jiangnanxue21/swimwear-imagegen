"""哪些事实进人工确认队列(PRD §11「未校准字段大量涌入」)。**纯判定,零三方依赖。**

## 它修的是什么

`AttributeStatus` 的文档字符串自己写着这条不变式:

    `CANDIDATE` 与 `SUGGESTED` 的区别是这一版的关键:前者是「留了证据但不采信」
    (未校准、置信度过低、模型说看不清),后者是「够格进人工确认队列」。
    混成一个的话,未校准字段会和低分字段一起涌进待确认列表,人只会全部忽略。

而 `workbench/flow.py` 的属性步**正在混**:它把 `candidate_only` 和 `suggested`
写在同一个 `or` 里,于是一个 CANDIDATE 字段会产出「有建议值待确认」这句话、
级别 NEEDS_CONFIRM、步骤状态跟着进 NEEDS_CONFIRM(完成度记 0.6)。
运营点进去会发现**没有任何值可以确认** —— 那个字段的证据按定义就是不采信的。

PRD §11 新增的第三行把这条写成了明文规则:「确认队列只出现 SUGGESTED/CONFLICT,
避免运营被低质建议淹没」。

## 为什么现在要紧,而不是一条纸面洁癖

阶段 3 把真实抽取器接上了,而校准分箱是空的。`decision.decide_status` 的
第二条分支写得很清楚:

    if system_confidence is None or system_confidence < MIN_SUGGEST_CONFIDENCE:
        return AttributeStatus.CANDIDATE

未校准返回的正是 `None`。也就是说**真实模型接上的第一天,每一个字段都是
CANDIDATE**。按今天的判定,那批商品会集体显示成「有建议值待确认」、
属性步 60% 完成度 —— 而它们一个可确认的值都没有。

这正是 §11 那一行的字面场景,而它今天**一条守卫都没有**:全仓
`candidate_only` 只在 `flow.py` 出现两次、`service.py` 出现一次,
`tests/` 里零次。

## 为什么是一张穷举表,不是一句 `!= CANDIDATE`

写成排除法今天等价,分歧在将来:`AttributeStatus` 新增一个取值时,
排除法之下它默认进队列,而新增取值的那个人不会想起这道口径。
这里改成**每一个成员都要被显式归档**,漏一个由守卫当场报出来 ——
和 `media/sample_completeness.CONFIRMED_ROLE_SOURCES` 挑白名单是同一条理由。

## 归档表之外的两处判断

**一、`REJECTED` / `SUPERSEDED` 归 `DISMISSED`,不归 `EVIDENCE_ONLY`。**
两者都不进队列,所以合并不改变任何行为 —— 但它们不是一回事:
`EVIDENCE_ONLY` 说的是「系统看过了,不采信」,那是一条**还需要有人去填值**的
待办;`DISMISSED` 说的是「这个值已经被处理掉了」,它不欠任何人任何动作。
合并的代价要到「有证据但不采信」这条动线接上界面那天才付:那张列表会混进
一批人工早就拒绝过的字段。

**二、认不出来的状态归 `EVIDENCE_ONLY`,不归 `DISMISSED`。**
兜底方向选的是「产出一条待办」而不是「静默消失」。反过来兜的话,
库里一个拼错的状态字符串会让那个字段从确认队列和阻断清单里**同时**消失,
而商品照样导不出去 —— 运营看到的是一件卡住但没有任何理由的商品。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.enums import AttributeStatus


class Disposition(StrEnum):
    """一条事实此刻的去向。**四档,互斥。**

    分档的依据是「运营要做什么」,不是「它有多可信」:
    前两档要人动手,后两档不要。
    """

    #: 已确认。不在队列里,也不欠任何动作
    SETTLED = "SETTLED"
    #: 有值,等人点头。**进确认队列**
    CONFIRMABLE = "CONFIRMABLE"
    #: 多图结论打架,等人裁决。**进确认队列**
    ADJUDICABLE = "ADJUDICABLE"
    #: 留了证据但不采信(未校准 / 置信度不足 / 模型说看不清)。
    #: **不进确认队列** —— 队列里没有任何东西可点。它欠的是「有人去填一个值」
    EVIDENCE_ONLY = "EVIDENCE_ONLY"
    #: 已经被处理掉了(人工拒绝、被新版本取代)。不进队列,也不欠动作
    DISMISSED = "DISMISSED"


#: 状态 -> 去向。**必须覆盖 `AttributeStatus` 的每一个成员**,
#: 由 `tests/pure` 的穷举守卫钉着。加一个状态而不加这一行,守卫当场红。
DISPOSITION_BY_STATUS: Mapping[str, Disposition] = {
    AttributeStatus.CONFIRMED.value: Disposition.SETTLED,
    AttributeStatus.SUGGESTED.value: Disposition.CONFIRMABLE,
    AttributeStatus.CONFLICT.value: Disposition.ADJUDICABLE,
    AttributeStatus.CANDIDATE.value: Disposition.EVIDENCE_ONLY,
    AttributeStatus.REJECTED.value: Disposition.DISMISSED,
    AttributeStatus.SUPERSEDED.value: Disposition.DISMISSED,
}

#: 进人工确认队列的两档(§11 原文:只出现 SUGGESTED / CONFLICT)。
#:
#: 写成 `Disposition` 的集合而不是状态字符串的集合:队列成员资格问的是
#: 「运营要不要对它做一个决定」,而那是去向这一层的问题。写成状态集合的话,
#: 将来两个状态映到同一档时会有人只改一处。
CONFIRM_QUEUE: frozenset[Disposition] = frozenset(
    {Disposition.CONFIRMABLE, Disposition.ADJUDICABLE}
)


def _text(value: Any) -> str:
    """枚举成员 / 字符串 / None 统一成一个可比较的字符串。

    与 `media/evidence_rules._text` 同一份口径。
    """
    if value is None:
        return ""
    return str(value).strip()


def disposition(status: Any) -> Disposition:
    """这条事实的去向。认不出来的一律 `EVIDENCE_ONLY`,见模块文档第二条。"""
    return DISPOSITION_BY_STATUS.get(_text(status), Disposition.EVIDENCE_ONLY)


def enters_confirm_queue(status: Any) -> bool:
    """这条事实进不进人工确认队列。**§11 那条规则的判定。**"""
    return disposition(status) in CONFIRM_QUEUE


@dataclass(frozen=True)
class FieldBuckets:
    """按去向分好的字段名集合。

    返回**字段名**而不是条数:提示语要能点名到字段,而运营在属性页
    唯一能筛的维度就是字段名(与 `flow.AttributeFacts` 的口径一致)。
    """

    settled: frozenset[str] = frozenset()
    confirmable: frozenset[str] = frozenset()
    adjudicable: frozenset[str] = frozenset()
    evidence_only: frozenset[str] = frozenset()
    dismissed: frozenset[str] = frozenset()

    @property
    def confirm_queue(self) -> frozenset[str]:
        """确认队列里的全部字段。**这是 §11 那句话的落点。**"""
        return self.confirmable | self.adjudicable


#: 去向 -> `FieldBuckets` 的哪个字段。
#:
#: 写成表而不是五个 `if`:少写一档的表现是那一档的字段**从每一个集合里
#: 同时消失**,而商品照样导不出去 —— 一件卡住但没有任何理由的商品。
_BUCKET_ATTR: Mapping[Disposition, str] = {
    Disposition.SETTLED: "settled",
    Disposition.CONFIRMABLE: "confirmable",
    Disposition.ADJUDICABLE: "adjudicable",
    Disposition.EVIDENCE_ONLY: "evidence_only",
    Disposition.DISMISSED: "dismissed",
}


def bucket_fields(by_status: Mapping[Any, Iterable[str]]) -> FieldBuckets:
    """把「状态 -> 字段名」翻成「去向 -> 字段名」。**服务层的唯一入口。**

    入参是 `service.py` 手里已经有的那个 `by_status`,不额外查库 ——
    这一层要的只是把状态翻成去向,而翻译规则必须只有一份。

    同一个字段名出现在多个状态下(库里同名字段有多行、状态不同)时,
    它会同时进多个桶。这是**刻意的**:属性行按 `(product, field, version)`
    存,一个字段有一条 SUPERSEDED 的旧行加一条 SUGGESTED 的新行是正常形态,
    而「它进不进确认队列」应当由那条 SUGGESTED 说了算。去重成一个桶就要
    先定优先级,而优先级是一个没有依据的发明。
    """
    collected: dict[str, set[str]] = {name: set() for name in _BUCKET_ATTR.values()}
    for status, names in by_status.items():
        collected[_BUCKET_ATTR[disposition(status)]].update(names)
    return FieldBuckets(**{key: frozenset(value) for key, value in collected.items()})
