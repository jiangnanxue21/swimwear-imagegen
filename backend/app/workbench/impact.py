"""上游变化的影响提示(PRD §13 阶段 6 批次 6-5 / AC-17)。**零依赖纯函数。**

AC-17 的原话:

> 在**修改之前**展示这次修改将使哪些对象失效(对象 / 字段 / 需要执行的动作),
> 口径取自 `stale_matrix`,不在提示处另算一份。

后半句是这个模块存在的全部理由。`stale_matrix` 已经是那张 32 格的封闭表,
每一格还点名了代码里负责它的机制。提示处再写一份"我觉得改这个会影响什么"
的判断,就是给同一个问题造第二个答案 —— 而那两个答案分叉的那天,
运营看到的提示会说"不影响",而系统照样把草稿打成 STALE。

所以本模块只做两件 `stale_matrix` 没做的事:

    翻译    枚举 -> 中文(它是一张规格表,没有文案)
    接续    「过期了之后要做什么」-> `NextActionCode`

## 「不受影响」也要显示,这不是多余

只列受影响的对象,运营看到的是一张短清单,而他真正在问的是
「我这一改,**文案要不要重做**」。清单里没有文案有两种可能:不受影响,
或者这张表漏了它。两者在界面上长得一样,而后者本仓刚好有过 ——
`stale_matrix` 的 FACTS 那一列就是补上去的(§8.1),补之前"事实会不会过期"
在表里连一个格子都没有。所以四列全给,`affected` 是一个字段而不是一次筛选。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.workbench.flow import ACTION_LABELS, NextActionCode
from app.workbench.stale_matrix import (
    TRIGGERS,
    ChangeSource,
    Effect,
    Target,
    cell,
)

#: 变更源 -> 给运营看的一句话。**八行穷举**,由守卫钉着。
#:
#: 文案表放在这一层而不是 `stale_matrix`:那个模块的文档第一句写着它是
#: 「任务书里的表格变成可穷举的规格」,措辞要与任务书逐字对齐。中文提示
#: 是界面的事,混进规格表之后,改一句提示会变成改一次规格。
SOURCE_LABELS: Mapping[ChangeSource, str] = {
    ChangeSource.CONFIRMED_ATTRIBUTE_MODIFIED: "修改一个已确认的属性",
    ChangeSource.ASSET_REPLACED_OR_QUARANTINED: "替换或隔离一张素材",
    ChangeSource.APPROVED_IMAGE_SET_REARRANGED: "重新编排已批准的图片集",
    ChangeSource.APPROVED_COPY_EDITED: "编辑已批准的文案",
    ChangeSource.EXPORT_SPEC_VERSION_CHANGED: "更新导出模板版本",
    ChangeSource.COPY_RULES_CHANGED: "更新文案规则或禁词库",
    ChangeSource.GENERATION_PLAN_CHANGED: "更换生成方案",
    ChangeSource.APPROVED_IMAGE_SET_REPLACED: "批准新一版图片集",
}

#: 目标 -> 给运营看的名字。四列穷举。
TARGET_LABELS: Mapping[Target, str] = {
    Target.FACTS: "商品事实",
    Target.IMAGE_SET: "图片集",
    Target.COPY: "文案",
    Target.DRAFT: "上架草稿",
}

#: 目标过期之后**要执行的动作**(AC-17 原话里的第三样东西)。
#:
#: 这张表是本模块唯一"新增"的判断,而它必须存在:`stale_matrix` 回答的是
#: 「什么会过期」,回答不了「过期了我该点哪个按钮」。而 AC-17 明确要求
#: 提示里有"需要执行的动作" —— 少了它,提示说"文案会过期",运营的下一个
#: 问题("那我现在要干什么")在界面上没有答案,他会去点重新生成草稿。
#:
#: 取值全部来自 `flow.NextActionCode`,不另造动作码:向导上的按钮按那个
#: 枚举渲染,另造一个的表现是提示里写着一个界面上不存在的动作。
TARGET_ACTION: Mapping[Target, NextActionCode] = {
    # 事实过期 = 那些字段要人再看一眼(不是重跑识别 —— 识别产出的是建议值,
    # 盖不掉人工确认过的值。`flow._evaluate_attribute` 的 ATTR_FACT_STALE
    # 分支为这一条写过整段理由)
    Target.FACTS: NextActionCode.CONFIRM_ATTRIBUTES,
    Target.IMAGE_SET: NextActionCode.APPROVE_IMAGE_SET,
    Target.COPY: NextActionCode.GENERATE_COPY,
    Target.DRAFT: NextActionCode.REFRESH_DRAFT,
}


@dataclass(frozen=True)
class ImpactRow:
    target: Target
    target_label: str
    effect: Effect
    affected: bool
    #: `stale_matrix` 里点名的负责机制。**原样透出** —— 这一行是给排查
    #: 的人看的:提示说会过期而实际没过期时,要查的就是这个函数
    mechanism: str
    note: str
    action: NextActionCode | None
    action_label: str | None


def preview(source: ChangeSource) -> tuple[ImpactRow, ...]:
    """这次修改会影响哪些对象。**四列全给,`affected` 是字段不是筛选。**"""
    rows: list[ImpactRow] = []
    for target in Target:
        c = cell(source, target)
        affected = c.effect is not Effect.NOT_AFFECTED
        action = TARGET_ACTION[target] if affected else None
        rows.append(
            ImpactRow(
                target=target,
                target_label=TARGET_LABELS[target],
                effect=c.effect,
                affected=affected,
                mechanism=c.mechanism,
                note=c.note,
                action=action,
                action_label=ACTION_LABELS[action] if action else None,
            )
        )
    return tuple(rows)


def describe_sources() -> list[dict[str, object]]:
    """全部变更源的清单(界面上"我要改什么"那个选择器的数据源)。

    **在后端而不是前端硬编码一份**:少一项的表现是运营改那一样东西之前
    看不到提示,而 AC-17 要的正是"修改之前"。硬规则 4 的同一条。
    """
    return [
        {
            "source": source.value,
            "label": SOURCE_LABELS[source],
            "trigger": TRIGGERS[source],
            "affects": [
                TARGET_LABELS[row.target] for row in preview(source) if row.affected
            ],
        }
        for source in ChangeSource
    ]


def serialize(source: ChangeSource, rows: tuple[ImpactRow, ...]) -> dict[str, object]:
    return {
        "source": source.value,
        "source_label": SOURCE_LABELS[source],
        "trigger": TRIGGERS[source],
        "rows": [
            {
                "target": row.target.value,
                "target_label": row.target_label,
                # `effect` 的取值本身就是中文(`Effect` 的成员值抄的是任务书
                # §4.5 表格原文),所以不需要第二张文案表
                "effect": row.effect.value,
                "affected": row.affected,
                "mechanism": row.mechanism,
                "note": row.note,
                "action": row.action.value if row.action else None,
                "action_label": row.action_label,
            }
            for row in rows
        ],
    }
