"""识别结果 -> 该落哪些证据。**纯判定,零三方依赖。**

## 为什么单独一个模块

`run_extraction` 里的这段判断是 §6.3 与 §4.5 两条硬规则的执行点:

    目标清单之外的字段     过滤掉,并计入"不可见属性编造率"
    模型说没有值的字段     落证据、不落值,带上原因
    对没问过的字段说没值   丢弃,但不算编造(它没给出确定值)

判定留在这里而不是长在服务层里,理由和 `workflows/publish_view.py` 顶部
那条一模一样:这里零依赖,所以能在 `tests/pure/` 被穷举;搬进服务层之后,
覆盖"模型多答一个字段时到底写不写"就要起一个数据库,而那种测试
没人会为一个分支去写 —— 于是这两条硬规则的分支覆盖会退化成零。

服务层只负责把这里算出来的计划**物化**成 ORM 行。两件事分开的好处在
batch13-2 的 F1 上有过教训:判定和落库混在一起时,"决定只落了一半"
不会被任何守卫看见。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 与 `core.enums.MissingReason` 的取值一致。这里重复一份字符串是刻意的:
#: 本模块必须零依赖(`core.enums` 本身也零依赖,但它在 import 链上牵着
#: 一整套业务枚举),而 `tests/pure/` 有一条守卫比对两边不许漂移。
REASON_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
REASON_NOT_VISUALLY_DETERMINABLE = "NOT_VISUALLY_DETERMINABLE"
REASON_AWAITING_SUPPLIER_DATA = "AWAITING_SUPPLIER_DATA"

ALLOWED_REASONS: frozenset[str] = frozenset(
    {
        REASON_INSUFFICIENT_EVIDENCE,
        REASON_NOT_VISUALLY_DETERMINABLE,
        REASON_AWAITING_SUPPLIER_DATA,
    }
)


@dataclass(frozen=True)
class PlannedEvidence:
    """一条待落库的证据。`missing_reason` 非空时 `value` 无意义。"""

    field_name: str
    value: Any = None
    confidence: float | None = None
    evidence_text: str | None = None
    missing_reason: str | None = None

    @property
    def is_missing(self) -> bool:
        return self.missing_reason is not None


@dataclass(frozen=True)
class EvidencePlan:
    """一张图算出来的落库计划。

    `fabricated` 是**计数不是清单**:字段名可能是模型自造的任意字符串,
    存清单等于让模型往我们的库里写自由文本。名字进日志(截断),
    数字进 `product_attribute_extractions.fabricated_field_count`。
    """

    evidence: tuple[PlannedEvidence, ...] = ()
    fabricated: int = 0
    fabricated_names: tuple[str, ...] = ()


def plan_evidence(result, targets) -> EvidencePlan:
    """把一张图的识别结果变成落库计划。

    `result` 只要求有 `fields` / `unreadable_fields`,`missing` 可缺
    (v1 抽取器没有这个属性)—— 鸭子类型,不 import 抽取器层。

    ## 三条规则的执行顺序

    1. **目标过滤先于一切。** 判据是"这次问了什么"(`targets`),
       不是"注册表里有没有"。原来按注册表判,于是增量识别时
       (`fields=` 只点了两个字段)模型顺手多答的那个会被当成本次产物
       写进去,带着一个没人要过的值参与合并。
    2. **给出了确定值的越界字段算编造**,进计数;只是宣布"没有值"的
       越界字段丢弃、不计数 —— 它没有编造任何东西,只是答非所问。
    3. **两条无值通道归一**:v1 的 `unreadable_fields` 按
       INSUFFICIENT_EVIDENCE 处理,v2 的 `missing` 保留模型给的原因。
    4. **同一个字段既给了值又说没有值时,"没有值"胜出,并计入编造。**
       这不是随手取一边:模型自己承认这个字段判断不了,却仍然给出了一个值,
       那正是"不可见属性编造"这个指标要测量的东西的字面定义。
       让值胜出的话,一个模型亲口说看不清的值会进合并、进确认队列。
    5. **一张图一个字段只出一条证据**(A45-batch14-2 / F1)。模型在同一份
       响应里把一个字段答两遍是完全可能的(`fields` 是数组,Schema 挡不住
       重复项,`parse_extraction_payload` 明说"只解析不过滤")。不去重的话
       这一张图就投了两票 —— 而 `extractors/merge.py` 的分歧阈值 0.34
       是按「三张图里有一张不同意」定的,那个推理**假设一图一票**。
       两条重复证据能凭空把 3:1(0.25,通过)变成 3:2(0.40,CONFLICT),
       也能让一张图自己跟自己二比一。

       同名同值折成一条(重复不是编造,只是啰嗦,第一条的置信度与理由留下);
       同名异值时**两个值都不要**,落一条 INSUFFICIENT_EVIDENCE 的无值证据,
       多出来的那几条计入编造 —— 与规则 4 同源:模型在同一张图上给出两个
       互斥的值,其中至少有一个是造的,而挑哪个都是替模型做决定。
    """
    wanted = set(targets)
    fabricated_names: list[str] = []

    no_value: dict[str, str] = {}
    for name in getattr(result, "unreadable_fields", ()) or ():
        name = str(name or "").strip()
        if name:
            no_value[name] = REASON_INSUFFICIENT_EVIDENCE
    for item in getattr(result, "missing", ()) or ():
        name = str(getattr(item, "name", "") or "").strip()
        if not name:
            continue
        reason = str(getattr(item, "reason", "") or "").strip().upper()
        # 认不出来的原因降级成"看不清",**不放弃"没有值"这件事**:
        # reason 是诊断信息,写错只让人少一条线索;丢掉整条证据会让这个字段
        # 退回"凭空缺席"那种不可分辨的状态
        no_value[name] = reason if reason in ALLOWED_REASONS else REASON_INSUFFICIENT_EVIDENCE

    evidence: list[PlannedEvidence] = []
    # 先按字段名归拢,**不要边读边落**(规则 5)。边读边落的话重复项已经
    # 变成两条证据了,后面再想合并就得比对已经建好的对象;而且顺序信息
    # (谁是第一条)会丢掉。`order` 单独记出现顺序:dict 在 3.7+ 保序,
    # 但依赖那件事的代码不该只在注释里说自己依赖它
    by_name: dict[str, list[PlannedEvidence]] = {}
    order: list[str] = []
    for observation in getattr(result, "fields", ()) or ():
        name = str(getattr(observation, "name", "") or "").strip()
        if not name:
            continue
        if name not in wanted:
            fabricated_names.append(name[:64])
            continue
        if name in no_value:
            # 规则 4:模型自己说这个字段判断不了,却又给了一个值
            fabricated_names.append(name[:64])
            continue
        text = getattr(observation, "evidence", None)
        if name not in by_name:
            by_name[name] = []
            order.append(name)
        by_name[name].append(
            PlannedEvidence(
                field_name=name,
                value=getattr(observation, "value", None),
                confidence=getattr(observation, "confidence", None),
                evidence_text=(str(text)[:2000] if text else None) or None,
            )
        )

    for name in order:
        items = by_name[name]
        if len(items) == 1:
            evidence.append(items[0])
            continue
        # 用 `==` 逐个比,不做成集合:value 可能是列表(secondary_colors)
        # 或字典,不可哈希;而 repr 比较对字典是键序敏感的假阴性
        if all(item.value == items[0].value for item in items[1:]):
            # 同名同值:折成一条。第一条的置信度与理由留下 ——
            # 挑哪一条都一样,但"挑第一条"是能被守卫钉住的确定行为
            evidence.append(items[0])
            continue
        fabricated_names.extend([name[:64]] * (len(items) - 1))
        evidence.append(
            PlannedEvidence(
                field_name=name, missing_reason=REASON_INSUFFICIENT_EVIDENCE
            )
        )

    for name, reason in no_value.items():
        if name not in wanted:
            # 对没问过的字段宣布"没有值"不算编造(它没给出确定值),
            # 但也没有任何用处 —— 丢弃
            continue
        evidence.append(PlannedEvidence(field_name=name, missing_reason=reason))

    return EvidencePlan(
        evidence=tuple(evidence),
        fabricated=len(fabricated_names),
        fabricated_names=tuple(fabricated_names),
    )
