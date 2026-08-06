"""内容计划:事实锁定(M6,§8.1)。**零依赖纯函数。**

`facts` **只能来自 `status=CONFIRMED` 的属性**。用 SUGGESTED 的值去生成文案,
等于把一个还没人看过的模型猜测写进面向消费者的商品描述里。

模型不得新增或修改 facts 键值 —— 它只能在 `selling_points` 里发挥,
而每条卖点必须能追溯到某个 fact 或人工填写的卖点库,追溯不到的丢弃。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.listings import claim_terms
from app.listings.contracts import ContentPlanData

#: 需要检测报告才能宣称的功效,按**规范 claim ID** 组织(评审第 14 条)。
#:
#: 这几条不是"平台不喜欢",是**法规**:UPF 数值、抗菌、塑形效果在多数
#: 站点都要求实测依据。让模型自由发挥的话它一定会写 —— 这类词的
#: 转化率很高,训练语料里到处都是。
#:
#: ## 为什么是 ID 而不是展示文本
#:
#: 上一版拿展示字符串做精确比对,于是 `UPF50+` / `UPF 50+` / `UPF-50+`
#: 是三个互不相干的"认证",巴葡的 `FPU 50+` 更是第四个。运营在检测报告
#: 那一栏填了 `UPF 50+`,系统认定 `UPF50+` 仍未认证 —— 反过来也一样,
#: 一个变体没登记就等于一条禁令没生效。
#:
#: 认证集合存的是**键**(`UPF50`),值这一侧是它在各语言各格式下的写法。
CERTIFIED_CLAIM_VARIANTS: Mapping[str, tuple[str, ...]] = {
    "UPF50": (
        "UPF50+", "UPF 50+", "UPF-50+", "UPF50", "UPF 50",
        # 巴葡:防晒系数写作 FPU(布料)/ FPS(防晒霜),两种都有人用
        "FPU50+", "FPU 50+", "FPS50+", "FPS 50+",
    ),
    "SPF": ("SPF", "SPF50", "SPF 50", "SPF30", "SPF 30"),
    "ANTIBACTERIAL": (
        "antibacterial", "anti-bacterial", "antimicrobial",
        "抗菌", "抑菌", "antibacteriano", "antibacteriana",
    ),
    "SLIMMING": (
        "slimming", "body shaping", "tummy control",
        "塑形", "瘦身", "收腹",
        "modelador", "redutor de medidas", "efeito redutor",
    ),
    "MEDICAL": (
        "medical", "medical grade", "medical-grade",
        "医用", "医疗级",
        "grau médico", "grau medico", "uso médico", "uso medico",
    ),
}

#: 全部受管制写法的扁平清单。**只读**,由上表推导。
#:
#: 保留这个名字是因为它是对外的判据("这些说法要有报告才能写"),
#: 而分组信息只有生成禁令表时用得上。
CLAIMS_REQUIRING_CERTIFICATION: tuple[str, ...] = tuple(
    dict.fromkeys(v for variants in CERTIFIED_CLAIM_VARIANTS.values() for v in variants)
)

#: 归一化写法 -> 规范 claim ID。运营填 `UPF-50+` 也能落到 `UPF50` 上
_VARIANT_TO_ID: Mapping[str, str] = {
    claim_terms.normalize(variant): claim_id
    for claim_id, variants in CERTIFIED_CLAIM_VARIANTS.items()
    for variant in variants
}


def canonical_claim_id(value: str) -> str | None:
    """把任意写法解析成规范 claim ID。认不出返回 None。

    ID 本身(`UPF50`)也要认 —— 库里存的就是 ID,回读时不能失配。
    """
    text = claim_terms.normalize(value)
    if not text:
        return None
    for claim_id in CERTIFIED_CLAIM_VARIANTS:
        if claim_terms.normalize(claim_id) == text:
            return claim_id
    return _VARIANT_TO_ID.get(text)


def certified_claim_ids(certified: Sequence[str]) -> tuple[str, ...]:
    """把运营填的认证清单折成规范 ID 集合。

    认不出来的条目**丢弃并不放行**:一个拼错的认证项应当表现为
    "这条宣称仍被禁止",而不是"这条宣称被放开了"。
    """
    ids = [canonical_claim_id(c) for c in certified]
    return tuple(dict.fromkeys(i for i in ids if i))


def build_facts(
    confirmed: Mapping[str, Any],
    *,
    include: Sequence[str] | None = None,
) -> dict[str, Any]:
    """从已确认属性组装 facts。

    ``confirmed`` 的形状是 {字段: 值},调用方负责只传 CONFIRMED 的 ——
    这个函数不查库,也就无法自己过滤状态。本模块的 `build_plan()`
    是唯一的组装入口,过滤在那里做。
    """
    keys = tuple(include) if include else tuple(confirmed)
    return {k: confirmed[k] for k in keys if k in confirmed and confirmed[k] not in (None, "")}


def derive_forbidden_claims(
    *,
    certified: Sequence[str] = (),
    extra: Sequence[str] = (),
) -> tuple[str, ...]:
    """算出这个商品不允许出现的宣称。

    ``certified`` 是**已经有检测报告**的项,可以填 ID(`UPF50`)也可以填
    任意一种登记写法(`UPF 50+`)—— 两者解析到同一个 ID。没在里面的一律禁止。

    默认全禁而不是默认全放:一个漏配的检测报告清单,后果是
    文案里出现了一句没有依据的功效宣称,而那在多数站点是下架级别的问题。

    返回的是**展示写法**,不是 ID:它要交给 `validate_wording()` 去正文里找,
    而正文里出现的是写法。一个 ID 被放行,它名下的全部写法一起放行 ——
    这正是评审第 14 条要的:认证的是那件事,不是那个字符串。
    """
    approved = set(certified_claim_ids(certified))
    forbidden = [
        variant
        for claim_id, variants in CERTIFIED_CLAIM_VARIANTS.items()
        if claim_id not in approved
        for variant in variants
    ]
    forbidden.extend(e for e in extra if e.strip())
    return tuple(dict.fromkeys(forbidden))


@dataclass(frozen=True)
class SellingPointSource:
    """一条卖点及其**结构化来源**(评审第 3 条)。

    上一版只留下卖点文本,来源在函数返回的一瞬间就丢了。于是
    「这条卖点凭什么保留」事后无法回答,而这正是人工复核唯一要问的问题。
    """

    text: str
    #: fact / manual
    origin: str
    field_name: str | None = None
    value: Any = None
    #: 实际命中的那个写法。人工复核时一眼能看出是不是碰巧撞上的
    matched_term: str | None = None


def trace_selling_points(
    candidates: Sequence[str],
    facts: Mapping[str, Any],
    *,
    manual_library: Sequence[str] = (),
    lexicon: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
) -> tuple[SellingPointSource, ...]:
    """逐条给卖点找来源。找不到的**不返回**。

    ## 为什么不能用子串

    上一版的判据是「任一事实值是候选文本的子串」。尺码 `M`、罩杯 `S`、
    领型缩写 `V`、数量 `1` 这些值,在任何一段英文里都能找到:

        facts = {"size": "M"}  ->  "Medical grade compression" 被判定为可溯源

    于是评审第 3 条列的那批无依据卖点(医用级压缩、抗氯面料)全部保留下来,
    而它们恰恰是最需要被丢掉的 —— 这类词有法规风险。

    现在改成:值先经 `claim_terms` 展开成登记写法(自动排除短于三个字符的、
    低信息量的取值),再按词边界匹配。命中的写法一并返回,来源可查。
    """
    library = {claim_terms.normalize(m): m.strip() for m in manual_library if m.strip()}
    traced: list[SellingPointSource] = []
    seen: set[str] = set()

    for point in candidates:
        text = point.strip()
        if not text or text in seen:
            continue

        if claim_terms.normalize(text) in library:
            seen.add(text)
            traced.append(SellingPointSource(text=text, origin="manual"))
            continue

        for field_name, value in facts.items():
            term = claim_terms.text_supports(field_name, value, text, lexicon=lexicon)
            if term:
                seen.add(text)
                traced.append(
                    SellingPointSource(
                        text=text,
                        origin="fact",
                        field_name=field_name,
                        value=value,
                        matched_term=term,
                    )
                )
                break
    return tuple(traced)


def filter_selling_points(
    candidates: Sequence[str],
    facts: Mapping[str, Any],
    *,
    manual_library: Sequence[str] = (),
    lexicon: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
) -> tuple[str, ...]:
    """卖点必须能追溯到某个 fact 或人工卖点库。追溯不到的**丢弃**。

    不丢的话,模型会稳定地生成一批读起来很好、但和这件商品无关的卖点
    ——「采用高弹面料」在它不知道面料的情况下照样写得出来。

    只要文本的那一层。要来源就用 `trace_selling_points()`。
    """
    return tuple(
        s.text
        for s in trace_selling_points(
            candidates, facts, manual_library=manual_library, lexicon=lexicon
        )
    )


def build_plan(
    *,
    spu: str,
    confirmed: Mapping[str, Any],
    attr_version_ids: Sequence[str] = (),
    selling_points: Sequence[str] = (),
    manual_library: Sequence[str] = (),
    certified_claims: Sequence[str] = (),
    extra_forbidden: Sequence[str] = (),
    lexicon: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
) -> ContentPlanData:
    """组装一份内容计划。"""
    facts = build_facts(confirmed)
    return ContentPlanData(
        spu=spu,
        facts=facts,
        selling_points=filter_selling_points(
            selling_points, facts, manual_library=manual_library, lexicon=lexicon
        ),
        forbidden_claims=derive_forbidden_claims(
            certified=certified_claims, extra=extra_forbidden
        ),
        attr_snapshot_ids=tuple(sorted(attr_version_ids)),
    )
