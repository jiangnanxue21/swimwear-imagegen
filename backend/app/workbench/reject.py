"""快审退回(A10)。

## 为什么退回需要自己一个模块

批准是"这件可以发了",退回是"这件不行,而且我知道下一步该干什么"。
第二句话是全部难点所在:计划 §3.2 的验收标准写的是

    运营遇到不合格商品时不需要离开队列自行猜测处理路径

也就是说退回**必须产出一个明确的下一步**,不能只是把状态推回去然后让运营
自己想。所以这里有两样东西:一份受控的退回原因枚举,和一个从原因推出
下一步的纯函数。两样都不碰数据库,判定可以单独测。

## 为什么原因是枚举而不是自由文本

自由文本的退回理由在三周后一文不值:「不行」「图不对」「重做」。而这九个原因
是要拿来**统计**的 —— UAT 结束时要回答"退回集中在哪一类",那是决定先修
提示词还是先补素材的依据。自由文本回答不了这个问题。

补充说明仍然可以写(`note`),但它是附加,不是替代。

## 原因 -> 下一步的映射为什么在后端

前端也能写一份 switch。但这张表决定的是**判定结果**(`next_action`),
而 §3.2.1 定过纪律:唯一下一步由后端算,前端只翻译。两份实现的后果是
快审页说"去重新生成"、工作台列表说"去补素材",同一件商品两个说法。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.workbench.flow import (
    ACTION_LABELS,
    REJECT_REASON_MESSAGES,
    NextActionCode,
    reject_next_action,
)


class RejectReason(StrEnum):
    """退回原因。取值来自计划 §3.2 A10 点名的那九个,不多不少。

    前四个是图片问题,中间三个是文案问题,`NEEDS_MATERIAL` 与 `OTHER` 两边都可能。
    这个分工不写进枚举(枚举只是取值),而是体现在下面的映射里。
    """

    STRUCTURE_CHANGED = "STRUCTURE_CHANGED"      # 商品结构改变
    COLOR_MISMATCH = "COLOR_MISMATCH"            # 颜色不一致
    PRINT_CHANGED = "PRINT_CHANGED"              # 印花改变
    LOW_IMAGE_QUALITY = "LOW_IMAGE_QUALITY"      # 图片质量低
    COPY_FACT_ERROR = "COPY_FACT_ERROR"          # 文案事实错误
    UNNATURAL_PT = "UNNATURAL_PT"                # 葡语不自然
    IRRELEVANT_KEYWORDS = "IRRELEVANT_KEYWORDS"  # 关键词不相关
    NEEDS_MATERIAL = "NEEDS_MATERIAL"            # 需要补素材
    OTHER = "OTHER"                              # 其他


#: 界面文案。**只有一份中文,在 `flow.REJECT_REASON_MESSAGES`**,这里按枚举取。
#:
#: 原先这里手抄了第二份九条中文。两份的问题不是费事,是它们**不会一起改**:
#: 契约测试当时只校验两张表各自覆盖了整个枚举,没有校验两边的字是同一句 ——
#: 改一处漏一处,界面和判定结论会各说各的,而且没有任何东西会报错。
#: 现在从枚举推导,漏一个键在导入时就 KeyError。
REJECT_REASON_LABELS: dict[RejectReason, str] = {
    reason: REJECT_REASON_MESSAGES[reason.value] for reason in RejectReason
}


#: 哪些原因适用于图片集,哪些适用于文案。
#:
#: 拿它做**入参校验**:用「葡语不自然」退回一个图片集是运营点错了行,
#: 而不是一次有意义的退回 —— 那条退回记录事后没人看得懂,统计也会被污染。
#: 所以在接口层就挡住,不等它落库。
IMAGE_SET_REASONS: frozenset[RejectReason] = frozenset(
    {
        RejectReason.STRUCTURE_CHANGED,
        RejectReason.COLOR_MISMATCH,
        RejectReason.PRINT_CHANGED,
        RejectReason.LOW_IMAGE_QUALITY,
        RejectReason.NEEDS_MATERIAL,
        RejectReason.OTHER,
    }
)

COPY_REASONS: frozenset[RejectReason] = frozenset(
    {
        RejectReason.COPY_FACT_ERROR,
        RejectReason.UNNATURAL_PT,
        RejectReason.IRRELEVANT_KEYWORDS,
        RejectReason.STRUCTURE_CHANGED,
        RejectReason.OTHER,
    }
)


#: 选了「其他」就必须写清楚是什么。
#:
#: 不强制的话它会变成默认选项:九个里挑一个要想,「其他」不用想。
#: 而一条只有「其他」没有说明的退回记录,对下一个接手的人等于没有信息。
REASONS_REQUIRING_NOTE: frozenset[RejectReason] = frozenset({RejectReason.OTHER})


def resolve_reason(value: str) -> RejectReason:
    """把接口传来的字符串转成枚举。不认识就抛 ValueError,由调用方转 422。"""
    return RejectReason(value)


def next_action_for(reason: RejectReason, target: str) -> NextActionCode:
    """退回之后该干什么。**判定只有一份实现**,在 `flow.reject_next_action`。

    要带 `target`:「其他」的下一步由被退回的对象决定,而不是由原因决定 ——
    文案选「其他」退回不该把人送去重排图片(理由见 flow 那张表旁边的注释)。
    """
    return reject_next_action(target, reason.value)


def note_required(reason: RejectReason) -> bool:
    return reason in REASONS_REQUIRING_NOTE


def reasons_for(target: str) -> frozenset[RejectReason]:
    """`target` 是 'image_set' 或 'copy'。给接口层做入参校验用。"""
    if target == "image_set":
        return IMAGE_SET_REASONS
    if target == "copy":
        return COPY_REASONS
    raise ValueError(f"未知的退回对象:{target}")


#: 补充说明的长度上限。与 `listing_image_sets.reject_note` 一样是 Text 列,
#: 不写上限也存得下 —— 这个数字防的不是数据库,是有人把一整份聊天记录贴进来,
#: 而退回台账要能一屏扫过。
MAX_NOTE_LENGTH = 500


@dataclass(frozen=True)
class RejectInput:
    """一次合法的退回。**服务层只接受这个类型**,不接受两个裸字符串。"""

    reason: RejectReason
    note: str | None


def validate_reject(target: str, reason: str, note: str | None) -> RejectInput:
    """把接口收到的一次退回收敛成合法输入,不合法抛 `ValueError`。

    ## 为什么三条校验挤在一个函数里

    它们回答的是同一个问题:**这条退回记录三周后还看得懂吗**。

        原因不认识    落库之后没人知道那是什么;判定层会走兜底动作
        原因对不上号  用「葡语不自然」退回图片集,记录本身自相矛盾
        「其他」没说明 等于没有信息,而它是九个里最省事的那个选项

    ## 为什么不放在接口层

    A1 的教训写在 `test_gate_a_guards` 里:**写库的守卫要和写库放在一起**。
    接口层校验完就走,而服务层还会被批量路径、脚本和以后的 worker 调用 ——
    那些入口不会重复抄一遍这三条。这个函数自己不碰数据库(纯判定,可单测),
    但它是服务层写库前的最后一道,由服务层调用,不是由路由调用。
    """
    try:
        resolved = resolve_reason(reason)
    except ValueError:
        raise ValueError(f"未知的退回原因:{reason}") from None

    allowed = reasons_for(target)
    if resolved not in allowed:
        raise ValueError(
            f"「{REJECT_REASON_LABELS[resolved]}」不适用于这一步的退回;"
            # 按枚举定义顺序列出,不按字母序:定义顺序是运营在界面上看到的顺序,
            # 两处不一致会让人在报错和选项之间来回找
            f"可选:{'、'.join(REJECT_REASON_LABELS[r] for r in RejectReason if r in allowed)}"
        )

    text = (note or "").strip()
    if note_required(resolved) and not text:
        raise ValueError(
            f"选了「{REJECT_REASON_LABELS[resolved]}」必须写明具体是什么问题"
        )
    if len(text) > MAX_NOTE_LENGTH:
        raise ValueError(f"补充说明最多 {MAX_NOTE_LENGTH} 字")
    return RejectInput(reason=resolved, note=text or None)


def describe(target: str) -> list[dict[str, object]]:
    """给界面用的退回原因清单。**中文只有这一份**,前端不再抄。

    每一条都带上退回之后的下一步:运营在选原因的那一刻就该看见后果 ——
    「商品结构改变」会把他送去改属性而不是重排图片,事先知道和事后发现
    是两种体验。选错了原因就是选错了下一步,这一点不该藏起来。
    """
    return [
        {
            "code": reason.value,
            "label": REJECT_REASON_LABELS[reason],
            "note_required": note_required(reason),
            "next_action": next_action_for(reason, target).value,
            "next_action_label": ACTION_LABELS[next_action_for(reason, target)],
        }
        # 按枚举定义顺序给,不按字母序:定义顺序是「图片问题 -> 文案问题 -> 兜底」,
        # 而字母序会把「其他」排到中间,它应该永远在最后一个
        for reason in RejectReason
        if reason in reasons_for(target)
    ]
