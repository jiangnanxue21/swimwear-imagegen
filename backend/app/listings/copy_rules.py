"""文案校验(M6,§8.4)。**零依赖纯函数。**

## 为什么不能只做字符串比对

v1 的做法是「属性值出现在文案里就算一致」。那条规则两头都不成立:

    属性 NAVY_BLUE 在文案里可能是 deep ocean blue / azul-marinho / 深海蓝
    文案里出现 black buckle 也不能证明主体是黑色

前者是假阴性(正确的文案被拦下),后者是假阳性(错的文案放过去)。
两种错误都会让人很快把这条检查关掉。

## claims:让模型标注自己的文本

要求模型在输出文案的同时输出结构化标注:

    {"field": "primary_color", "value": "NAVY_BLUE",
     "text_span": "navy blue", "location": "title"}

后端逐条验证:field/value 必须在 facts 里且值一致,text_span 必须在对应
location 的文本中**逐字出现**,并且这段文本必须是该字段该取值的一种
**登记过的说法**(`claim_terms`)。

这与「不采信模型自报结论」不冲突 —— 我们采信的不是它的判断,
是一个可以被机械校验的位置索引。它标错了,校验就过不去。

## 「出现」不等于「对应」(评审第 1 条)

只校验 text_span 是否出现在文本里,模型可以用任意一个真实存在的词
给任意一个属性背书:用 `swimsuit` 证明颜色、用 `summer` 证明款式,
逐字校验全部通过。校验看起来在跑,实际上什么都没拦住。

所以 span 还要过 `claim_terms.span_supports()`:它必须是这个 (字段, 值)
在服务端词表里登记过的写法之一。词表查不到、值本身又短到不能当证据的,
**拦下**而不是放过 —— 补一行词表会出现在 diff 里,放过不会。

## 结构先于规则(评审第 2 条)

大模型输出在进规则校验之前先过一遍 `validate_payload()`。
`title` 是整数、`claims` 里混进 `None`、`bullet_points` 是一个字符串 ——
这些以前会在规则层抛 `TypeError` / `AttributeError`,变成一个 500,
而它本质上是「模型没按约定输出」,应当和其它校验失败走同一条路。
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import StrEnum
from typing import Any

from app.listings import claim_terms

#: 校验规则版本。改了判据(加一条硬失败、改一次 claim 的比对口径、
#: 动一次禁用词表)就要动它。
#:
#: 它进批次幂等指纹(`workbench/batch_service._generator_fingerprint`):
#: 规则收紧之后,上游属性和文案一个字没动,旧的「校验通过」结果本该失效 ——
#: 而没有这个版本号的话,指纹完全命中,系统会拿着按旧规则算出来的
#: 结论继续放行(评审第 3 条)。
RULES_VERSION = "rules-1"


class CopyViolationCode(StrEnum):
    """文案问题码。硬失败与 warning 分开 —— 见下方 `is_blocking`。"""

    #: 模型宣称了一个事实源里没有的属性值
    CLAIM_NOT_IN_FACTS = "CLAIM_NOT_IN_FACTS"
    #: claims 里的值和 facts 对不上
    CLAIM_VALUE_MISMATCH = "CLAIM_VALUE_MISMATCH"
    #: text_span 在对应 location 的文本里找不到
    CLAIM_SPAN_NOT_FOUND = "CLAIM_SPAN_NOT_FOUND"
    #: text_span 找到了,但它不是这个字段这个取值的登记说法 ——
    #: 拿 "swimsuit" 给颜色背书就落在这一条上
    CLAIM_SPAN_NOT_EVIDENCE = "CLAIM_SPAN_NOT_EVIDENCE"
    #: 关键属性(颜色、品类)没有任何一条 claim
    REQUIRED_FACT_NOT_CLAIMED = "REQUIRED_FACT_NOT_CLAIMED"
    #: location 指向一个不存在的位置
    CLAIM_LOCATION_INVALID = "CLAIM_LOCATION_INVALID"
    BANNED_WORD = "BANNED_WORD"
    FORBIDDEN_CLAIM = "FORBIDDEN_CLAIM"
    TOO_LONG = "TOO_LONG"
    TOO_SHORT = "TOO_SHORT"
    WRONG_BULLET_COUNT = "WRONG_BULLET_COUNT"
    PLACEHOLDER_LEFT = "PLACEHOLDER_LEFT"
    #: 模型输出的结构就不对(字段类型、claims 形状)。**先于一切规则检查**
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    #: 同义词词典扫出的疑似不一致。**warning** —— 词典永远不全
    SYNONYM_MISMATCH = "SYNONYM_MISMATCH"


#: 只出 warning 的码。其余一律硬失败。
#:
#: 同义词扫描做成硬失败的话,会把大量正常文案拦下来(词典不可能覆盖
#: 所有颜色说法),然后有人把整条检查删掉 —— 那时候真正的不一致也没人管了。
WARNING_CODES: frozenset[CopyViolationCode] = frozenset({CopyViolationCode.SYNONYM_MISMATCH})

#: 必须在文案里体现的事实。漏了就是硬失败。
#:
#: 颜色和品类:消费者搜索和筛选靠它们,标题里不写等于这个商品搜不到。
REQUIRED_FACTS: tuple[str, ...] = ("primary_color", "garment_type")

#: 占位符残留。模型偶尔会把提示词里的模板原样吐出来
PLACEHOLDER_PATTERNS: tuple[str, ...] = (
    r"\{[a-zA-Z_]+\}",      # {product_name}
    r"\[TODO\]",
    r"\bLorem\b",
    r"\bipsum\b",
    r"<[a-z_]+>",           # <color>
)


@dataclass(frozen=True)
class CopyViolation:
    code: CopyViolationCode
    message: str
    location: str | None = None
    detail: Mapping[str, Any] = dc_field(default_factory=dict)

    @property
    def is_blocking(self) -> bool:
        return self.code not in WARNING_CODES


@dataclass(frozen=True)
class CopyRules:
    """渠道规则。外置在 `spec/copy_rules.yaml`,带 `spec_version`。

    **生成时的提示词约束和生成后的校验依据引用同一份文件。**
    各写一遍的话,提示词说"标题 120 字以内"而校验按 100 字判,
    模型会稳定地产出刚好过不了的文案,而没人知道该改哪一边。
    """

    title_max: int = 120
    title_min: int = 10
    description_max: int = 2000
    bullet_min: int = 3
    bullet_max: int = 5
    bullet_item_max: int = 120
    banned_words: tuple[str, ...] = ()
    spec_version: str = "0"
    #: 按**字符**计,不按字节。中文标题按字节算会莫名其妙超长。
    #:
    #: 这个开关以前是死的:配置读得进来,而所有长度规则一律走 `len()`,
    #: 配成 False 行为完全不变(评审第 17 条)。现在全部经由 `measure()`,
    #: 配 False 就真的按 UTF-8 字节计 —— 有渠道确实按字节限长
    count_by_character: bool = True

    def measure(self, text: Any) -> int:
        """按本渠道的口径量一段文本的长度。**所有长度规则的唯一入口。**"""
        value = "" if text is None else str(text)
        if self.count_by_character:
            return len(value)
        return len(value.encode("utf-8"))

    @property
    def length_unit(self) -> str:
        return "字" if self.count_by_character else "字节"


def _bullets_of(copy: Mapping[str, Any]) -> list[str]:
    """卖点列表。**字符串不当成列表。**

    `list("abc")` 会得到 `['a','b','c']` —— 模型把 bullet_points 输出成一个
    字符串时,规则层会当成三条各一个字符的卖点,然后报一个「卖点 3 条,
    每条都很短」的假通过。宁可当成零条,由 `validate_payload()` 报结构错。
    """
    raw = copy.get("bullet_points")
    if isinstance(raw, str) or raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(b) for b in raw]


def _text_at(location: str, copy: Mapping[str, Any]) -> str | None:
    """按 location 取文本。取不到返回 None(而不是空串)。

    `bullet.0` 这种带下标的写法要显式解析 —— 不能用 eval,
    也不能用 getattr 穿透,理由和渠道 DSL 那边一样。

    一律 `str()`:`title` 是整数时,后面那句 `span not in text` 会抛
    `TypeError: argument of type 'int' is not iterable`,而那是个 500,
    不是一条校验结论。结构问题由 `validate_payload()` 单独报。
    """
    if location == "title":
        return str(copy.get("title") or "")
    if location == "description":
        return str(copy.get("description") or "")
    match = re.fullmatch(r"bullet\.(\d+)", location)
    if match:
        bullets = _bullets_of(copy)
        index = int(match.group(1))
        if 0 <= index < len(bullets):
            return bullets[index]
        return None
    return None


def _norm(value: Any) -> str:
    return str(value).strip().upper() if value is not None else ""


# ==========================================================
# 结构闸门:先于一切规则(评审第 2 条)
# ==========================================================

#: 文案对象里允许的字段与它们的类型。
#:
#: 这张表是**服务端**的约定,不是模型自述的形状。多出来的键忽略(渠道
#: 将来会加字段),类型不对的一律拦下 —— 后者才是会炸的那一类。
_COPY_FIELD_TYPES: Mapping[str, tuple[type, ...]] = {
    "title": (str,),
    "description": (str,),
    "bullet_points": (list, tuple),
    "keywords": (list, tuple),
    "extra": (dict,),
}

_CLAIM_STRING_KEYS: tuple[str, ...] = ("text_span", "location")


def validate_payload(
    copy: Any, claims: Any = ()
) -> list[CopyViolation]:
    """结构校验。**在任何规则检查之前跑。**

    模型输出进规则层之前必须先是一个形状正确的东西。以前没有这一步,
    于是三类完全正常会发生的输出各自变成一个未捕获异常:

        title 是整数        -> TypeError(span 在 int 里找不到)
        claims 里有 None    -> AttributeError(None.get)
        bullet_points 是串  -> 被 list() 拆成一串单字符,静默算通过

    前两个是 500,第三个更糟 —— 它不报错,只是给出一个错的结论。
    三者都应该是「这一版文案没通过,原因是结构不对」。
    """
    problems: list[CopyViolation] = []

    if not isinstance(copy, Mapping):
        return [
            CopyViolation(
                CopyViolationCode.MALFORMED_OUTPUT,
                f"文案输出的顶层不是对象,而是 {type(copy).__name__}",
                detail={"got": type(copy).__name__},
            )
        ]

    for key, allowed in _COPY_FIELD_TYPES.items():
        if key not in copy:
            continue
        value = copy[key]
        if value is None:
            continue
        if not isinstance(value, allowed):
            problems.append(
                CopyViolation(
                    CopyViolationCode.MALFORMED_OUTPUT,
                    f"{key} 应当是 {'/'.join(t.__name__ for t in allowed)},"
                    f"实际是 {type(value).__name__}",
                    location=key if key in ("title", "description") else None,
                    detail={"field": key, "got": type(value).__name__},
                )
            )
            continue
        if key in ("bullet_points", "keywords"):
            for index, item in enumerate(value):
                if not isinstance(item, str):
                    problems.append(
                        CopyViolation(
                            CopyViolationCode.MALFORMED_OUTPUT,
                            f"{key} 第 {index + 1} 项应当是字符串,"
                            f"实际是 {type(item).__name__}",
                            detail={"field": key, "index": index},
                        )
                    )

    if claims is None:
        claims = ()
    if isinstance(claims, (str, bytes, Mapping)) or not isinstance(claims, Sequence):
        problems.append(
            CopyViolation(
                CopyViolationCode.MALFORMED_OUTPUT,
                f"claims 应当是一组标注,实际是 {type(claims).__name__}",
                detail={"field": "claims", "got": type(claims).__name__},
            )
        )
        return problems

    for index, claim in enumerate(claims):
        if not isinstance(claim, Mapping):
            problems.append(
                CopyViolation(
                    CopyViolationCode.MALFORMED_OUTPUT,
                    f"claims 第 {index + 1} 条不是对象,而是 {type(claim).__name__}",
                    detail={"field": "claims", "index": index},
                )
            )
            continue
        if not (claim.get("field") or claim.get("field_name")):
            problems.append(
                CopyViolation(
                    CopyViolationCode.MALFORMED_OUTPUT,
                    f"claims 第 {index + 1} 条没有 field",
                    detail={"field": "claims", "index": index},
                )
            )
        for key in _CLAIM_STRING_KEYS:
            value = claim.get(key)
            if value is not None and not isinstance(value, str):
                problems.append(
                    CopyViolation(
                        CopyViolationCode.MALFORMED_OUTPUT,
                        f"claims 第 {index + 1} 条的 {key} 应当是字符串,"
                        f"实际是 {type(value).__name__}",
                        detail={"field": "claims", "index": index, "key": key},
                    )
                )
    return problems


def sanitize_copy(copy: Any) -> dict[str, Any]:
    """落库用的安全形态。**只在结构已被判定为不合格时用。**

    结构错的那一版也要落库 —— 「这一版为什么没通过」是运营要看的东西,
    而把原始输出丢掉之后这个问题就没法回答了。但不能拿它去撞 JSONB 的
    列类型,所以先折成安全的形状。
    """
    if not isinstance(copy, Mapping):
        return {}
    bullets = copy.get("bullet_points")
    keywords = copy.get("keywords")
    extra = copy.get("extra")
    return {
        "title": None if copy.get("title") is None else str(copy.get("title")),
        "description": (
            None if copy.get("description") is None else str(copy.get("description"))
        ),
        "bullet_points": (
            [str(b) for b in bullets] if isinstance(bullets, (list, tuple)) else []
        ),
        "keywords": (
            [str(k) for k in keywords] if isinstance(keywords, (list, tuple)) else []
        ),
        "extra": dict(extra) if isinstance(extra, Mapping) else {},
    }


def sanitize_claims(claims: Any) -> list[dict[str, Any]]:
    """落库用的安全 claims。非对象项直接丢掉,理由同上。"""
    if isinstance(claims, (str, bytes, Mapping)) or not isinstance(claims, Sequence):
        return []
    return [dict(c) for c in claims if isinstance(c, Mapping)]


def validate_claims(
    copy: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    facts: Mapping[str, Any],
    *,
    required_facts: Sequence[str] = REQUIRED_FACTS,
    lexicon: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
) -> list[CopyViolation]:
    """校验 claims(§8.4)。五条硬失败。

    第五条是评审第 1 条的修复:span 找到了还不够,它必须是这个
    (字段, 值) 在 `claim_terms` 里登记过的说法。少了这一条,
    「文本里存在该片段」就等于「该属性成立」,而两者完全不是一回事。
    """
    problems: list[CopyViolation] = []
    claimed_fields: set[str] = set()

    for claim in claims:
        if not isinstance(claim, Mapping):
            # 结构问题由 validate_payload() 报,这里只是不让它把整轮校验炸掉
            continue
        name = str(claim.get("field") or claim.get("field_name") or "")
        value = claim.get("value")
        span = str(claim.get("text_span") or "")
        location = str(claim.get("location") or "")

        if name not in facts:
            # 模型宣称了一个事实源里没有的属性 —— 它在编
            problems.append(
                CopyViolation(
                    CopyViolationCode.CLAIM_NOT_IN_FACTS,
                    f"文案宣称了 {name},但事实源里没有这个属性",
                    location=location or None,
                    detail={"field": name},
                )
            )
            continue

        if not claim_terms.matches_value(value, facts.get(name)):
            problems.append(
                CopyViolation(
                    CopyViolationCode.CLAIM_VALUE_MISMATCH,
                    f"{name} 的文案宣称是 {value},事实源是 {facts.get(name)}",
                    location=location or None,
                    detail={"field": name, "claimed": value, "fact": facts.get(name)},
                )
            )
            continue

        text = _text_at(location, copy)
        if text is None:
            problems.append(
                CopyViolation(
                    CopyViolationCode.CLAIM_LOCATION_INVALID,
                    f"claim 指向了不存在的位置 {location!r}",
                    location=location or None,
                )
            )
            continue

        if not span or span not in text:
            # 防止 claims 与正文脱节:模型可以标注得很漂亮,
            # 而正文里根本没写这件事
            problems.append(
                CopyViolation(
                    CopyViolationCode.CLAIM_SPAN_NOT_FOUND,
                    f"{location} 的文本里找不到 claim 声称的片段 {span!r}",
                    location=location,
                    detail={"field": name, "span": span},
                )
            )
            continue

        if not claim_terms.span_supports(name, facts.get(name), span, lexicon=lexicon):
            # 片段确实在文本里,但它不是这个取值的说法 ——
            # 用 "swimsuit" 给颜色背书、用 "summer" 给款式背书都落在这里。
            # 词表查不到该取值的任何说法时也走这条(默认拒绝),
            # 提示里说清楚要补哪一格,免得变成一个查不出原因的失败
            allowed = claim_terms.terms_for(name, facts.get(name), lexicon=lexicon)
            problems.append(
                CopyViolation(
                    CopyViolationCode.CLAIM_SPAN_NOT_EVIDENCE,
                    f"{span!r} 不是 {name}={facts.get(name)!r} 的登记说法,"
                    + (
                        f"允许的写法:{', '.join(allowed[:5])}"
                        if allowed
                        else "该取值在词表里没有任何登记说法,无法机械验证"
                    ),
                    location=location,
                    detail={
                        "field": name,
                        "span": span,
                        "allowed": list(allowed[:10]),
                    },
                )
            )
            continue

        claimed_fields.add(name)

    for name in required_facts:
        if name in facts and name not in claimed_fields:
            problems.append(
                CopyViolation(
                    CopyViolationCode.REQUIRED_FACT_NOT_CLAIMED,
                    f"{name} 是必须体现的属性,文案里没有对应的 claim",
                    detail={"field": name},
                )
            )
    return problems


def validate_structure(
    copy: Mapping[str, Any], rules: CopyRules
) -> list[CopyViolation]:
    """长度、条数、占位符。计数口径由 `rules.count_by_character` 决定。"""
    problems: list[CopyViolation] = []
    unit = rules.length_unit

    title = str(copy.get("title") or "")
    title_len = rules.measure(title)
    if title_len > rules.title_max:
        problems.append(
            CopyViolation(
                CopyViolationCode.TOO_LONG,
                f"标题 {title_len} {unit},超过上限 {rules.title_max}",
                location="title",
            )
        )
    if title_len < rules.title_min:
        problems.append(
            CopyViolation(
                CopyViolationCode.TOO_SHORT,
                f"标题 {title_len} {unit},不足下限 {rules.title_min}",
                location="title",
            )
        )

    description = str(copy.get("description") or "")
    description_len = rules.measure(description)
    if description_len > rules.description_max:
        problems.append(
            CopyViolation(
                CopyViolationCode.TOO_LONG,
                f"描述 {description_len} {unit},超过上限 {rules.description_max}",
                location="description",
            )
        )

    bullets = _bullets_of(copy)
    if not (rules.bullet_min <= len(bullets) <= rules.bullet_max):
        problems.append(
            CopyViolation(
                CopyViolationCode.WRONG_BULLET_COUNT,
                f"卖点 {len(bullets)} 条,要求 {rules.bullet_min}-{rules.bullet_max} 条",
            )
        )
    for index, bullet in enumerate(bullets):
        bullet_len = rules.measure(bullet)
        if bullet_len > rules.bullet_item_max:
            problems.append(
                CopyViolation(
                    CopyViolationCode.TOO_LONG,
                    f"第 {index + 1} 条卖点 {bullet_len} {unit},超过 {rules.bullet_item_max}",
                    location=f"bullet.{index}",
                )
            )

    blob = "\n".join([title, description, *bullets])
    for pattern in PLACEHOLDER_PATTERNS:
        found = re.search(pattern, blob)
        if found:
            problems.append(
                CopyViolation(
                    CopyViolationCode.PLACEHOLDER_LEFT,
                    f"文案里残留了占位符:{found.group(0)!r}",
                    detail={"pattern": pattern},
                )
            )
    return problems


def validate_wording(
    copy: Mapping[str, Any],
    rules: CopyRules,
    forbidden_claims: Sequence[str] = (),
) -> list[CopyViolation]:
    """禁词与禁止的功效宣称。

    `forbidden_claims` 来自类目规则 + 站点法规(例如未做检测就不许写 UPF 数值)。
    它和 `banned_words` 分开:前者是"这件商品不能这么说",
    后者是"这个渠道不许出现这个词",两者的维护方完全不同。

    ## 归一化与词边界(评审第 13 条)

    以前是 `lower()` 之后子串比对,两头都不成立:

        误伤   禁词 `sale` 会在 `wholesale` 里命中
        绕过   `UPF-50+`、`ＵＰＦ50+`、`U​PF50+`(中间一个零宽空格)全部躲过

    现在统一走 `claim_terms`:NFKC + casefold + 去零宽 + 连字符归一,
    拉丁字母按词边界匹配,CJK 保持子串语义(汉字之间没有词边界可言)。
    """
    problems: list[CopyViolation] = []
    blob = "\n".join(
        [
            str(copy.get("title") or ""),
            str(copy.get("description") or ""),
            *_bullets_of(copy),
        ]
    )

    reported_terms: list[str] = []
    for word in rules.banned_words:
        if word and claim_terms.contains_term(blob, word):
            problems.append(
                CopyViolation(
                    CopyViolationCode.BANNED_WORD,
                    f"文案里出现了禁词:{word}",
                    detail={"word": word},
                )
            )
    for claim in sorted((c for c in forbidden_claims if c), key=len, reverse=True):
        normalized = claim_terms.normalize(claim)
        # 同一处禁令的多个写法(`UPF50+` 与 `UPF50`、`SPF50` 与 `SPF`)
        # 只报一次:更长的写法先判,命中之后被它包含的短写法跳过。
        # 不做这一步的话,一句 "UPF50+" 会同时触发两三条,而它们说的是同一件事
        if any(normalized in reported for reported in reported_terms):
            continue
        if claim_terms.contains_term(blob, claim):
            reported_terms.append(normalized)
            problems.append(
                CopyViolation(
                    CopyViolationCode.FORBIDDEN_CLAIM,
                    f"文案里出现了本商品不允许的宣称:{claim}",
                    detail={"claim": claim},
                )
            )
    return problems


def scan_synonyms(
    copy: Mapping[str, Any],
    facts: Mapping[str, Any],
    synonyms: Mapping[str, str],
) -> list[CopyViolation]:
    """同义词词典扫描。**只出 warning。**

    `synonyms` 是 {文案里的词: 归一化枚举值}。文案里出现一个映射到
    与 facts 不同枚举值的词时报 warning 并转人工。

    做成硬失败的话会把大量正常文案拦下来 —— 词典不可能覆盖所有颜色说法,
    而且 "black buckle" 里的 black 本来就不指主体色。然后有人会把
    整条检查删掉,那时候真正的不一致也没人管了。
    """
    problems: list[CopyViolation] = []
    blob = "\n".join(
        [
            str(copy.get("title") or ""),
            str(copy.get("description") or ""),
            *_bullets_of(copy),
        ]
    )

    fact_values = {_norm(v) for v in facts.values() if v not in (None, "")}
    for word, mapped in synonyms.items():
        if not claim_terms.contains_term(blob, word):
            continue
        # 词典把这个词映射到了某个枚举值,而事实源里没有那个值 -> 可疑。
        # 只提示,不拦截:文案里出现 "black buckle" 完全正常,
        # 而词典分不出"主体色"和"配件色"
        if mapped and _norm(mapped) not in fact_values:
            problems.append(
                CopyViolation(
                    CopyViolationCode.SYNONYM_MISMATCH,
                    f"文案里出现了 {word!r}(通常指 {mapped}),但事实源里没有这个值;"
                    "词典不全,这里只提示,请人工确认",
                    detail={"word": word, "mapped": mapped},
                )
            )
    return problems


def validate_copy(
    copy: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    facts: Mapping[str, Any],
    rules: CopyRules,
    *,
    forbidden_claims: Sequence[str] = (),
    synonyms: Mapping[str, str] | None = None,
    lexicon: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
) -> list[CopyViolation]:
    """一次跑完全部检查。**结构不对就到此为止。**

    结构错的情况下继续跑规则,拿到的是一堆从错误形状推出来的结论
    (「卖点 3 条」其实是一个字符串被拆成了三个字符),它们会把
    真正的原因埋掉。报一条「结构不对」比报五条假问题有用。
    """
    shape = validate_payload(copy, claims)
    if shape:
        return shape
    return [
        *validate_claims(copy, claims, facts, lexicon=lexicon),
        *validate_structure(copy, rules),
        *validate_wording(copy, rules, forbidden_claims),
        *scan_synonyms(copy, facts, synonyms or {}),
    ]


def blocking(violations: Sequence[CopyViolation]) -> list[CopyViolation]:
    return [v for v in violations if v.is_blocking]


def is_publishable(violations: Sequence[CopyViolation]) -> bool:
    return not blocking(violations)
