"""事实一致性维度组(PRD v3.1 §5.2 / §6.5)。**零依赖纯函数。**

## 它是什么

§6.5 给评分器加了一环:

    视觉评分(现有 11 维)+ 新增"事实一致性"维度组
    → 逐项对照 `CONFIRMED` 事实,命中即 hard_fail 阻断自动批准

清单沿用 v3.0 九项:主辅色、图案、肩带、领口、背部、覆盖度、结构改写、
不对称、融合缺陷。

## 三条必须写在最前面的判断

**一、它不是第二套一票否决。**
否决权已经在 `rules.grade_candidate` 的第一条线上(`result.hard_fail` → D +
REJECT)。本模块的产物是**硬错误代码**,交给那一条线。另建一条"事实不一致
就拒绝"的分支,系统就会有两个地方能把一张图判死,而两者漂移时
"这张图为什么是 D"会有两个都说得通的答案。

**二、模型只回答"看到了什么",不回答"对不对"。**
每一项模型给的是 `observed`(它在图里看到的值)与 `verdict`
(MATCH / MISMATCH / UNCERTAIN)。**是不是不一致,由这里拿 `CONFIRMED` 事实比。**
让模型自己比的写法有一个具体的坏处:事实值是我们的、图是它的,
它没有理由知道"这个 SPU 的肩带确认为可拆卸";它会去猜,而猜错的方向
恒定偏向"一致"(模型天然倾向于认可自己看到的东西)。

**三、没有确认事实的项一律不判不一致,但要留痕。**
一个字段还没确认(§6.3 没走完)时,拿它去否决候选图等于用"还不知道"
当成"不对" —— 而 §6.3 完成之前本来就不该走到生成。这类项进
`uncertain_items`,不进 `hard_fail_codes`。**反过来写是致命的**:
默认判不一致会让整批图在事实确认之前全部被否,而运营看到的是
"模型说所有图都错",查不到是自己少确认了一个字段。

## 为什么受众相关项复用 review_focus 而不是在这里分叉

§6.5 明写「受众相关项复用 review_focus 下发」。九项里 `strap` /
`neckline` / `back_style` / `coverage` 是女装词表,`waistband` / `inseam` /
`liner` 是男装词表 —— 而"这次启用哪些码"已经由规则包的
`enabled_hard_fail_codes` 回答过一次(§14.4)。在这里再判一次受众,
就会有两处决定"男装要不要查肩带",而它们漂移时没有任何地方会红。
本模块只做一件事:**把清单按启用码过滤**。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.core.enums import HardFailCode, ProblemSeverity

#: 载荷里这一段的键。写成常量是因为它有三个消费者(Schema、提示词、解析),
#: 而三处各写一遍字符串的表现是:改了键名之后模型照常返回、解析照常成功、
#: 这一组检查静静地不再生效。
PAYLOAD_KEY = "fact_consistency"


class FactVerdict(StrEnum):
    """模型对某一项的回答。

    `UNCERTAIN` 不是 `MISMATCH` 的弱化版,两者的下游待遇完全不同:
    不一致要否决,看不清只是记一笔。合并它们的任何一个方向都错 ——
    并成不一致会让一张被水花挡住背部的好图被判死,
    并成一致会让"看不清"变成"没问题"。
    """

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class FactCheck:
    """清单里的一项。

    `fact_fields` 是这一项要对照的**属性注册表字段名**。一项可以对多个字段
    (主辅色要同时看 `primary_color` 与 `secondary_color`),
    只要其中任何一个还没确认,这一项就不具备判定条件 —— 见模块文档第三条。
    """

    key: str
    label: str
    code: HardFailCode
    fact_fields: tuple[str, ...]


#: §6.5 的九项。**顺序即提示词里的呈现顺序**,改动要连着提示词一起改。
#:
#: 前七项各自对应一个已有的硬错误码;末两项(不对称、融合缺陷)在 A45-batch14-20
#: 之前**没有码**,理由与新增两条码的取舍写在 `HardFailCode` 那两行上面。
FACT_CHECKS: tuple[FactCheck, ...] = (
    FactCheck(
        "color",
        "主辅色",
        HardFailCode.COLOR_VARIANT_WRONG,
        ("primary_color", "secondary_color"),
    ),
    FactCheck("pattern", "图案", HardFailCode.PATTERN_DISTORTED, ("pattern_type",)),
    FactCheck("strap", "肩带", HardFailCode.STRAP_CHANGED, ("strap_type",)),
    FactCheck("neckline", "领口", HardFailCode.NECKLINE_CHANGED, ("neckline_type",)),
    FactCheck("back_style", "背部", HardFailCode.BACK_STYLE_CHANGED, ("back_style",)),
    FactCheck("coverage", "覆盖度", HardFailCode.COVERAGE_CHANGED, ("coverage_level",)),
    FactCheck(
        "structure",
        "结构改写",
        HardFailCode.GARMENT_PART_ADDED,
        ("garment_type",),
    ),
    #: 末两项对照的是"这件衣服本身",没有单独的注册表字段 —— 它们问的是
    #: 渲染工艺而不是商品事实。用 `garment_type` 当确认门槛:连品类都还没
    #: 确认的时候,"对称不对称"没有参照物
    FactCheck("asymmetry", "不对称", HardFailCode.ASYMMETRY_INTRODUCED, ("garment_type",)),
    FactCheck("fusion", "融合缺陷", HardFailCode.FUSION_DEFECT, ("garment_type",)),
)

#: 按 key 取一项。清单是封闭集合,取不到说明调用方拿着一个不存在的项。
CHECKS_BY_KEY: dict[str, FactCheck] = {c.key: c for c in FACT_CHECKS}


@dataclass(frozen=True)
class FactFinding:
    """一项的判定结果。"""

    key: str
    label: str
    verdict: FactVerdict
    code: HardFailCode | None = None
    observed: str = ""
    expected: str = ""
    #: 事实没确认 / 这一项本次没启用 / 模型没答 —— 三种都算"没判"
    skipped_reason: str = ""

    @property
    def is_hard_fail(self) -> bool:
        return self.verdict is FactVerdict.MISMATCH and self.code is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "verdict": self.verdict.value,
            "code": self.code.value if self.code else None,
            "observed": self.observed,
            "expected": self.expected,
            "skipped_reason": self.skipped_reason,
        }


def enabled_checks(allowed_codes: Sequence[str] | None = None) -> tuple[FactCheck, ...]:
    """本次规则包启用的那几项(§14.4)。

    传空或 None 退回全集,与 `vision_schema.hard_fail_code_values` 同一条口径:
    受众前时代的旧包没有启用清单,行为不能因此改变。
    """
    if not allowed_codes:
        return FACT_CHECKS
    wanted = {str(c).strip().upper() for c in allowed_codes}
    return tuple(c for c in FACT_CHECKS if c.code.value in wanted)


def evaluate(
    reported: Any,
    *,
    confirmed_facts: Mapping[str, Any] | None = None,
    allowed_codes: Sequence[str] | None = None,
) -> tuple[FactFinding, ...]:
    """把模型这一段的回答判成结论。见模块文档第二、三条。

    `confirmed_facts` 只应包含 **`CONFIRMED` 状态**的值;过滤在服务层做
    (那里才知道状态)。这里对"值为 None 或空串"一律按未确认处理 ——
    多一道是因为空值从服务层漏下来时,下面那句 `expected` 会变成空,
    而"图里的肩带与 `''` 不一致"是一句没有意义的否决理由。
    """
    facts = {str(k): v for k, v in (confirmed_facts or {}).items()}
    answers = _index_answers(reported)
    active = {c.key for c in enabled_checks(allowed_codes)}

    findings: list[FactFinding] = []
    for check in FACT_CHECKS:
        if check.key not in active:
            findings.append(
                _skipped(check, "本次规则包未启用这一项")
            )
            continue

        missing = [f for f in check.fact_fields if not _has_value(facts.get(f))]
        if missing:
            findings.append(
                _skipped(check, f"事实未确认:{'、'.join(missing)}")
            )
            continue

        answer = answers.get(check.key)
        if answer is None:
            findings.append(_skipped(check, "评分器没有回答这一项"))
            continue

        verdict = _verdict_of(answer)
        expected = "、".join(_text(facts.get(f)) for f in check.fact_fields)
        observed = _text(answer.get("observed"))[:120]
        findings.append(
            FactFinding(
                key=check.key,
                label=check.label,
                verdict=verdict,
                code=check.code if verdict is FactVerdict.MISMATCH else None,
                observed=observed,
                expected=expected,
            )
        )
    return tuple(findings)


def hard_fail_codes(findings: Iterable[FactFinding]) -> list[str]:
    """这一组检查产出的硬错误代码。**去重且保持清单顺序。**

    顺序稳定是为了让"同一张图跑两次给出同一条否决理由"成立 ——
    否决理由会进审核页和审计,顺序漂移会让两条本来相同的记录看起来不同。
    """
    codes: list[str] = []
    for finding in findings:
        if finding.is_hard_fail and finding.code is not None:
            value = finding.code.value
            if value not in codes:
                codes.append(value)
    return codes


def problem_dicts(findings: Iterable[FactFinding]) -> list[dict[str, Any]]:
    """不一致项转成 `problems` 条目的形状。

    严重度一律 CRITICAL:`SEVERITY_GRADE_CAP` 把 CRITICAL 封到 D 档,
    与 `hard_fail` 那条线同向。写 MAJOR 的话会出现一种自相矛盾的结论 ——
    硬错误说 D、严重度上限说 C,而取最差之后仍是 D,于是这个字段
    看起来无所谓;直到有人调了阈值,它才开始悄悄放行。
    """
    items = list(findings)
    # **从 `hard_fail_codes()` 派生,不各自再判一次 `is_hard_fail`。**
    #
    # 两处各判一次的表现很难看:变异 F4 把 `hard_fail_codes()` 短路掉之后,
    # 否决**照样发生**(problems 那条路还在),于是那个函数变成一段没人走的
    # 死代码 —— 而它是登记在 `WIRED_MODULES` 里的。一个"接了线但走不到"的
    # 判定比没接更糟:它看起来被守着。
    codes = set(hard_fail_codes(items))
    return [
        {
            "code": f.code.value,
            "severity": ProblemSeverity.CRITICAL.value,
            "message": f"{f.label}与已确认事实不一致:确认为「{f.expected}」,图中呈现「{f.observed or '无法描述'}」",
            "dimension": "fact_consistency",
            "hard_fail": True,
        }
        for f in items
        if f.code is not None and f.code.value in codes
    ]


def uncertain_notes(findings: Iterable[FactFinding]) -> list[str]:
    """看不清与没判成的项。进 `uncertain_items`,不影响分档。

    跳过项也进来,是因为"这一项没查"和"这一项查了没问题"在界面上必须
    区分得开:后者是绿灯,前者是**这次评分没覆盖到的范围**,
    而一份看起来全绿的评分报告如果实际只查了两项,审核的人会误以为
    其余七项都过了。
    """
    notes: list[str] = []
    for f in findings:
        # **先问有没有跳过,再问判定是什么。**顺序反过来的话跳过项会被
        # 报成"看不清" —— 而跳过项的 verdict 恰恰就是 UNCERTAIN(它没判),
        # 于是"少确认了一个字段"会被显示成"模型看不清这一项",
        # 运营按后者去补图,而实际该做的是去确认事实。
        if f.skipped_reason:
            notes.append(f"事实一致性「{f.label}」未判定:{f.skipped_reason}")
        elif f.verdict is FactVerdict.UNCERTAIN:
            notes.append(f"事实一致性「{f.label}」看不清")
    return notes


def coverage(findings: Iterable[FactFinding]) -> dict[str, int]:
    """这一组一共判了几项、几项没判。给审核页的小结用。"""
    items = list(findings)
    return {
        "total": len(items),
        "judged": sum(1 for f in items if not f.skipped_reason),
        "mismatched": sum(1 for f in items if f.is_hard_fail),
        "uncertain": sum(1 for f in items if f.verdict is FactVerdict.UNCERTAIN),
        "skipped": sum(1 for f in items if f.skipped_reason),
    }


# ---------------------------------------------------------------- 内部


def _skipped(check: FactCheck, reason: str) -> FactFinding:
    return FactFinding(
        key=check.key,
        label=check.label,
        verdict=FactVerdict.UNCERTAIN,
        code=None,
        skipped_reason=reason,
    )


def _index_answers(reported: Any) -> dict[str, Mapping[str, Any]]:
    """把模型返回的那一段索引成 `{key: 条目}`。

    容忍两种形状(数组与对象),理由与 `normalize_angles` 那条一样:
    形状在判定层炸掉的后果是整次评分失败,而真正该发生的是"这一项没答"。
    未知的 key 直接丢 —— 它进不了 `FACT_CHECKS` 的循环,所以丢掉是等价的,
    而留着会让下游以为清单可以被模型扩展。
    """
    out: dict[str, Mapping[str, Any]] = {}
    if isinstance(reported, Mapping):
        entries: list[Any] = []
        for key, value in reported.items():
            if isinstance(value, Mapping):
                entries.append({"key": key, **value})
            else:
                entries.append({"key": key, "verdict": value})
    elif isinstance(reported, (list, tuple)):
        entries = list(reported)
    else:
        return out

    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("key") or entry.get("item") or "").strip().lower()
        if key in CHECKS_BY_KEY and key not in out:
            out[key] = entry
    return out


def _verdict_of(answer: Mapping[str, Any]) -> FactVerdict:
    """模型答的那个词。**认不出来一律当看不清。**

    当成 MATCH 的写法会让一个拼错的词变成放行;当成 MISMATCH 会让它变成
    误杀。两种错误的代价里,误杀只是多生成一轮,放行是把错图送上网站 ——
    但这里两个都不选:认不出来的**本质**就是没答,而"没答"已经有一档了。
    """
    raw = str(answer.get("verdict") or "").strip().upper()
    if raw in FactVerdict.__members__:
        return FactVerdict(raw)
    return FactVerdict.UNCERTAIN


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    return bool(str(value).strip())


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()
