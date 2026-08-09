"""费用预估的取数(阶段 6 批次 6-5 / AC-15 的「费用预估」)。

## 它在分层里的位置

    color_rollup.build_color_views   已经算好的颜色维视图(6-3)
    **本模块**                       视图 -> `cost_estimate.Demand`
    workflows/cost_estimate          纯判定:数量 × 单价(6-5)

**输入是已经算好的 `ColorView`,不是数据库。** 这一点是刻意的:向导上的
「这个颜色缺 3 张图」与「预计还要花 3 次出图的钱」必须是同一个 3 ——
另查一次的表现是两个数字并排显示且不相等,而两边都不报错。

## 为什么预估里没有文案生成

`grep record_usage` 今天只有四个调用点:出图(submit / download)、
候选图评分(vision_score)、属性识别(attribute_extract)。**文案生成一条
流水都不写** —— 生成器还没接付费后端。所以这里不给它编一个价,
而是在 `notes` 里说出来:一个凭空出现的文案费用行会让预估看起来更全,
而它是编的。

## 单价查不到时会发生什么

不发生任何降级:`cost_estimate` 把那一行的金额记成 `None` 并把动作名
放进 `unpriced_operations`。默认的 Mock 一个都不在价目表里,所以开发环境
的向导会显示"3 项未配价" —— 那是对的,它正是"这套配置下我们不知道要花多少"。
"""
from __future__ import annotations

from collections.abc import Sequence

from app.workbench.color_flow import ColorView
from app.workflows.cost_estimate import Demand, Estimate, UnknownDemand, estimate


def _image_provider() -> str:
    """出图会记在哪个 provider 名下。

    生产侧写的是 `task.provider`(任务行上的那一列),而预估时任务还不存在,
    只能用默认路由。**这件事要说出来**(见 `basis`):路由模式是 AUTO 时
    真正跑的可能是另一个 provider,那时预估用的单价就不是实际单价。
    """
    from app.providers._config import provider_setting

    return (provider_setting("DEFAULT_PROVIDER", "mock") or "mock").strip().lower()


def _billable_extractor() -> str | None:
    """属性识别记在谁头上;不计费则 `None`。

    判据与 `attributes/service.record_usage(...)` 那一处**同源**:问实现
    自己的 `billable` / `usage_provider`,不在这里维护一张"哪些要花钱"的表。
    维护第二张的表现是新接一个付费抽取器之后预估恒为 0,而账单在涨。
    """
    from app.extractors.registry import get_extractor

    try:
        extractor = get_extractor()
    except Exception:  # noqa: BLE001 - 配错了不该让向导整个打不开
        return None
    if not getattr(extractor, "billable", False):
        return None
    return str(getattr(extractor, "usage_provider", "") or getattr(extractor, "name", ""))


def _billable_evaluator() -> str | None:
    """候选图评分记在谁头上;不计费则 `None`。判据同 `evaluation_service._billing`。"""
    from app.evaluators.registry import get_active_evaluator

    try:
        evaluator = get_active_evaluator()
    except Exception:  # noqa: BLE001 - 同上
        return None
    if not getattr(evaluator, "billable", False):
        return None
    return str(getattr(evaluator, "usage_provider", "") or "")


def build_estimate(views: Sequence[ColorView]) -> Estimate:
    """按当前上游算「接下来还要花多少」。

    只看 **ACTIVE** 颜色(§6.7 / AC-18 的同一条范围口径):停售的颜色缺图
    不拦路,也不该出现在预算里 —— 把它算进去,运营会为了让预估变小去
    给一个不卖的颜色补图。
    """
    active = [v for v in views if v.is_active]

    demands: list[Demand] = []
    unknown: list[UnknownDemand] = []
    notes: list[str] = []

    # ---------------------------------------------------------- 出图
    #
    # 每个颜色还缺几个角度,就还要出几次图。`missing_angles` 是
    # `image_set_rules.coverage()` 算出来的那一份(§6.5),不在这里重算。
    image_units = 0
    unknown_colors: list[str] = []
    for view in active:
        if view.has_plan is False:
            # 没有生成方案 = 不知道要出哪些角度。判 0 的表现是预估说"不用花钱",
            # 而选完方案之后它突然变成十几张 —— 那种数字没有人会再信第二次
            unknown_colors.append(view.variant_code)
            continue
        if view.owned_image_count is None and not view.missing_angles:
            # 还没有已批准图片集:缺角度这件事今天算不出来(与
            # `color_rollup` 里那三项给 None 是同一处口径)
            unknown_colors.append(view.variant_code)
            continue
        image_units += len(view.missing_angles)

    if image_units:
        demands.append(
            Demand(
                operation="submit",
                provider=_image_provider(),
                units=image_units,
                basis=(
                    f"{len(active)} 个在售颜色合计还缺 {image_units} 个角度"
                    f"(按默认 provider「{_image_provider()}」的单价估;"
                    "路由模式为 AUTO 时实际可能落在别的 provider 上)"
                ),
            )
        )
        evaluator = _billable_evaluator()
        if evaluator:
            demands.append(
                Demand(
                    operation="vision_score",
                    provider=evaluator,
                    units=image_units,
                    basis="按每个缺口出 1 张候选图、每张评一次分估",
                )
            )
        else:
            notes.append("候选图评分:当前评分器不计费,未计入预估")

    if unknown_colors:
        unknown.append(
            UnknownDemand(
                operation="submit",
                label="出图",
                reason="这些颜色还没有生成方案或还没有图片集,出图张数取决于方案里的角度",
                refs=tuple(sorted(unknown_colors)),
            )
        )

    # ---------------------------------------------------------- 属性识别
    #
    # 一个颜色还缺必需事实、且它有样品 -> 至少还要跑一次识别。
    # **没有样品的不算**:识别跑不起来,那一步的待办是去传图,不是花钱。
    extractor = _billable_extractor()
    pending_extraction = [
        v.variant_code
        for v in active
        if v.missing_required_fields and (v.sample_count or 0) > 0
    ]
    if pending_extraction:
        if extractor:
            demands.append(
                Demand(
                    operation="attribute_extract",
                    provider=extractor,
                    units=len(pending_extraction),
                    basis=(
                        f"{len(pending_extraction)} 个颜色还缺必需事实,"
                        "按每个颜色一次识别估(实际会随证据张数与重试变化)"
                    ),
                )
            )
        else:
            notes.append("属性识别:当前抽取器不计费,未计入预估")

    # ---------------------------------------------------------- 文案
    #
    # 见模块文档:文案生成今天一条流水都不写。不编价,只说出来
    if any(v.has_copy is False for v in active):
        notes.append("文案生成:目前不写付费流水(生成器未接付费后端),未计入预估")

    from app.services.spend import price_book

    return estimate(
        demands,
        price_book=price_book(),
        unknown=unknown,
        notes=notes,
    )
