"""事实值 ↔ 文案表述的服务端词表(评审第 1、3、13、14 条)。**零依赖纯函数。**

## 为什么需要这一层

上一版有三处各自用「子串出现即成立」做判断,三处都不成立:

    claims 校验     text_span 只要出现在标题里就算数据背书
                    -> 用 "swimsuit" 给 primary_color 背书也能过
    卖点溯源        任一事实值是候选文本的子串就保留
                    -> 尺码 "M" 出现在 "Medical grade" 里,于是这条卖点被保留
    禁词与禁止宣称   lower() 之后子串比对
                    -> "UPF-50+"、"ＵＰＦ50+"、带零宽字符的写法全部绕过

三处的共同缺口是**没有一份服务端可控的规范化词表**:文本片段和事实值之间
没有任何被登记过的对应关系,于是「出现」被当成了「对应」。

## 这个模块提供什么

    normalize()        Unicode NFKC + casefold + 去零宽 + 连字符/下划线归一为空格
    contains_term()    带词边界的匹配(拉丁字母按词边界,CJK 天然无边界)
    terms_for()        某个 (字段, 值) 允许的全部表述,含本地化写法
    span_supports()    这段文本能不能给这个 (字段, 值) 背书

## 默认拒绝

`terms_for()` 查不到登记项、且值本身短到无法当作证据(`M`、`S`、`1`)时,
返回空元组 —— 调用方据此判定「无法机械验证」并**拦下**,而不是放过。

放过的代价是校验形同虚设(评审第 1 条的原文);拦下的代价是有人要来
这张表里补一行。后者会出现在 diff 里,前者不会。
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: 短于这个长度的值不能单独当作证据。
#:
#: 尺码 `M`、`S`,杯型 `A`,领型缩写 `V`,数量 `1` —— 这些在任何一段英文文案里
#: 都能找到,拿它们做溯源等于没做。评审第 3 条举的例子就是这一类。
MIN_TERM_LENGTH = 3

#: 零宽与软连字符。它们在页面上不可见,却能让任何子串匹配失效 ——
#: 「禁词过滤器上线之后禁词就消失了」通常就是这么来的。
_INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e"), None
)

#: 连字符、下划线、各式空白、斜杠统一折成一个空格。
#: `UPF-50+` / `UPF_50+` / `UPF 50+` 必须落到同一个形态上
_SEPARATORS = re.compile(r"[\s\u00a0_\-\u2010-\u2015/\\]+")


def normalize(value: Any) -> str:
    """归一化。**所有文本比对都必须先过这一步。**

    NFKC 把全角折成半角(`ＵＰＦ` -> `UPF`)、把兼容字符折成基本字符;
    `casefold()` 比 `lower()` 更彻底(德语 ß -> ss);零宽字符直接删掉。

    顺序不能换:先删零宽再 NFKC 的话,NFKC 产生的新组合里可能又出现
    需要清理的字符。
    """
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.translate(_INVISIBLE)
    text = text.casefold()
    text = _SEPARATORS.sub(" ", text)
    return text.strip()


def contains_term(text: Any, term: Any) -> bool:
    """`term` 是否作为一个**完整词**出现在 `text` 里。

    拉丁字母两侧要求非字母数字边界:`cheap` 不该在 `cheaply` 里命中。
    CJK 没有词边界的概念,前后断言对汉字恒为真 —— 这正是想要的,
    「抗菌」出现在任何位置都算命中。

    用前后向断言而不是 `\\b`:禁止宣称里有 `UPF50+` 这种以非词字符结尾的词,
    `\\b` 在 `+` 后面的行为和直觉相反(它要求那里有一次词/非词跃迁)。
    """
    haystack = normalize(text)
    needle = normalize(term)
    if not haystack or not needle:
        return False
    pattern = r"(?<![0-9a-z])" + re.escape(needle) + r"(?![0-9a-z])"
    return re.search(pattern, haystack) is not None


def contains_any(text: Any, terms: Iterable[Any]) -> str | None:
    """命中的第一个词,没命中返回 None。"""
    for term in terms:
        if contains_term(text, term):
            return str(term)
    return None


# ==========================================================
# (字段, 值) -> 允许的表述
# ==========================================================

#: 登记表:字段 -> 规范值 -> 该值在文案里被允许的写法。
#:
#: **只登记真实存在的取值**,和 `copy_generator.ENUM_WORDS` 同一条原则:
#: 没有「把下划线换成空格」的兜底生成,因为那样 `UPF50_LINED` 会变成
#: `Upf50 Lined`,一个谁都不会写的词组,而它看起来像是登记过的。
#:
#: 值本身归一化之后(`ONE_PIECE` -> `one piece`)自动算一条,不必在这里重复写。
#: 这里写的是**它之外**的说法:英文口语写法、巴葡本地表达。
FIELD_TERMS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "garment_type": {
        "ONE_PIECE": ("one piece", "onepiece", "one piece swimsuit", "maiô", "maio"),
        "BIKINI_TOP": ("bikini top", "top de biquíni", "top de biquini"),
        "BIKINI_BOTTOM": ("bikini bottom", "calcinha de biquíni", "calcinha de biquini"),
        "BIKINI_SET": ("bikini set", "bikini", "biquíni", "biquini"),
        "TANKINI": ("tankini",),
        "COVER_UP": ("cover up", "coverup", "saída de praia", "saida de praia"),
        "SWIM_SHORTS": ("swim shorts", "board shorts", "sunga", "bermuda de praia"),
        "DRESS": ("dress", "vestido"),
        "TOP": ("top",),
    },
    "neckline_type": {
        "V_NECK": ("v neck", "vneck", "decote v"),
        "SCOOP": ("scoop neck", "scoop", "decote redondo"),
        "SQUARE": ("square neck", "decote quadrado"),
        "SWEETHEART": ("sweetheart neck", "sweetheart", "decote coração", "decote coracao"),
        "HIGH_NECK": ("high neck", "decote alto", "gola alta"),
        "PLUNGE": ("plunge neck", "plunge", "decote profundo"),
        "BANDEAU": ("bandeau", "tomara que caia"),
    },
    "back_style": {
        "OPEN_BACK": ("open back", "costas abertas"),
        "RACER_BACK": ("racer back", "racerback", "nadador"),
        "CROSS_BACK": ("cross back", "costas cruzadas"),
        "TIE_BACK": ("tie back", "amarração nas costas", "amarracao nas costas"),
        "FULL_BACK": ("full back", "costas fechadas"),
        "LACE_UP": ("lace up", "lace up back", "amarração", "amarracao"),
    },
    "strap_type": {
        "HALTER": ("halter", "frente única", "frente unica"),
        "SPAGHETTI": ("spaghetti strap", "spaghetti straps", "alça fina", "alca fina"),
        "WIDE": ("wide strap", "wide straps", "alça larga", "alca larga"),
        "STRAPLESS": ("strapless", "sem alças", "sem alcas"),
        "ONE_SHOULDER": ("one shoulder", "um ombro só", "um ombro so"),
        "CRISS_CROSS": ("criss cross", "crisscross", "alças cruzadas", "alcas cruzadas"),
    },
    "pattern_type": {
        "SOLID": ("solid", "solid color", "liso", "cor lisa"),
        "STRIPE": ("stripe", "striped", "stripes", "listrado", "listras"),
        "FLORAL": ("floral", "flower print", "estampa floral"),
        "ANIMAL": ("animal print", "animal", "estampa animal"),
        "GEOMETRIC": ("geometric", "geometric print", "geométrico", "geometrico"),
        "TIE_DYE": ("tie dye", "tiedye"),
        "COLOR_BLOCK": ("color block", "colorblock", "bloco de cor"),
        "PRINT_LOGO": ("logo print", "logo", "estampa de logo"),
    },
    #: 颜色是自由文本(我们自己的规范色名),不是枚举。
    #: 这里只登记本地化说法,规范名本身由归一化自动覆盖
    "primary_color": {
        "BLACK": ("preto", "preta"),
        "WHITE": ("branco", "branca"),
        "NAVY_BLUE": ("navy", "azul marinho", "azul-marinho"),
        "BLUE": ("azul",),
        "RED": ("vermelho", "vermelha"),
        "GREEN": ("verde",),
        "PINK": ("rosa",),
        "PURPLE": ("roxo", "lilás", "lilas"),
        "TEAL": ("verde água", "verde agua"),
        "CORAL": ("coral",),
    },
}

#: `secondary_colors` 和 `primary_color` 共用同一张色名表 —— 同一个颜色,
#: 出现在哪一栏和它怎么说无关
_FIELD_ALIASES: Mapping[str, str] = {"secondary_colors": "primary_color"}


def terms_for(
    field_name: str,
    value: Any,
    *,
    lexicon: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
) -> tuple[str, ...]:
    """这个 (字段, 值) 在文案里允许出现的全部写法,已归一化、已去重。

    返回空元组表示**无法机械验证** —— 值本身太短(`M`)且没有登记项。
    调用方必须把它当成"证据不成立",不是"随便什么都行"。
    """
    table = dict(lexicon if lexicon is not None else FIELD_TERMS)
    key = _FIELD_ALIASES.get(field_name, field_name)
    registered = table.get(key, {})

    collected: list[str] = []
    for raw in _value_candidates(value):
        canonical = normalize(raw)
        if len(canonical) >= MIN_TERM_LENGTH:
            collected.append(canonical)
        for term in registered.get(str(raw).strip().upper(), ()):  # 登记表按规范值索引
            candidate = normalize(term)
            if len(candidate) >= MIN_TERM_LENGTH:
                collected.append(candidate)
    return tuple(dict.fromkeys(collected))


def _value_candidates(value: Any) -> tuple[Any, ...]:
    """列表值(如 `secondary_colors`)展开成逐个取值。"""
    if isinstance(value, (list, tuple, set)):
        return tuple(v for v in value if v not in (None, ""))
    if value in (None, ""):
        return ()
    return (value,)


def span_supports(
    field_name: str,
    value: Any,
    span: Any,
    *,
    lexicon: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
) -> bool:
    """这段文本能不能给 (字段, 值) 背书。

    要求 span 里**完整出现**该值的某一种登记写法。反过来不行:
    span 是 `blue` 而值是 `NAVY_BLUE` 不算数 —— 那是另一个颜色。
    """
    allowed = terms_for(field_name, value, lexicon=lexicon)
    if not allowed:
        return False
    return any(contains_term(span, term) for term in allowed)


def text_supports(
    field_name: str,
    value: Any,
    text: Any,
    *,
    lexicon: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
) -> str | None:
    """文本里出现的第一个可溯源写法,没有返回 None。

    和 `span_supports()` 的差别只在返回值:溯源需要知道**是哪一条**命中的,
    校验只需要知道成没成立。
    """
    for term in terms_for(field_name, value, lexicon=lexicon):
        if contains_term(text, term):
            return term
    return None


def matches_value(claimed: Any, fact: Any) -> bool:
    """claim 声称的值和事实值是不是同一个。

    事实值是列表时(`secondary_colors`),命中任一元素即可 —— 生成器那边
    本来就是取第一个元素写进 claim 的(`copy_generator._first`),
    要求 claim 复述整个列表会把正确的文案判成不一致。
    """
    wanted = normalize(claimed)
    if isinstance(fact, (list, tuple, set)):
        return any(normalize(f) == wanted for f in fact)
    return normalize(fact) == wanted


def expand_variants(terms: Sequence[str]) -> tuple[str, ...]:
    """一组写法去重后的归一化形态。给禁止宣称表用。"""
    return tuple(dict.fromkeys(normalize(t) for t in terms if normalize(t)))
