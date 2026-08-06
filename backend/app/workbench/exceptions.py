"""异常聚合(阶段 3,FE-303 简化异常列表)。**零数据库依赖纯函数。**

## 为什么是"简化"

50 件商品每件 2~3 条问题,平铺出来是一张 120 行的表。运营在那张表上
做的第一件事是用眼睛分组:"哦,十几件都是缺背面图"。所以这里先分组:

    第一层  按步骤(素材 / 属性 / 图片集 / 文案 / 草稿)—— 任务书原话
    第二层  按问题码(同一个原因的商品收在一起,给一个件数)
    第三层  具体商品 + 定位信息(ref),一键跳到那个字段

第二层是这一页的价值所在:它把"120 个问题"变成"7 类问题",而 7 类
是一个人能在一次会议里处理完的量。

## 数据来源只有一处

问题条目**全部来自 flow.py 已经算出的 `Issue`**,不在这里重新判断。
列表页徽标上的阻断数、详情页顶部的阻断数、这一页的件数因此永远一致 ——
三个页面三份实现的话,运营会立刻发现三个不同的数字,然后不再相信任何一个。

批次失败(`batch.py` 的异常码)是**另一类**输入:它记录的是"执行时发生了
什么",而 flow 的 Issue 记录的是"现在的状态哪里不对"。两者在界面上
分两个来源列展示,不合并计数 —— 合并会让同一件商品被数两次。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from app.workbench.flow import (
    STEP_LABELS,
    STEP_ORDER,
    FlowResult,
    FlowStep,
    IssueLevel,
)

#: 异常来源。两类输入分开计数,理由见模块注释。
SOURCE_FLOW = "flow"    # 当前状态判定出的问题
SOURCE_BATCH = "batch"  # 批次执行时发生的失败

SOURCE_LABELS: Mapping[str, str] = {
    SOURCE_FLOW: "状态判定",
    SOURCE_BATCH: "批次执行",
}


@dataclass(frozen=True)
class ExceptionEntry:
    """一条异常,带足够的信息一键落到出问题的地方。

    §6.3.1 第二条:"可从异常列表一键跳转到具体商品的具体字段、图片或属性项"。
    `product_id` + `step` + `ref` 就是那一键需要的三件东西 —— 少了 `ref`,
    运营点进去还要在页面上自己找是哪个字段,那不叫一键。
    """

    product_id: str
    sku: str
    spu: str
    step: FlowStep
    level: IssueLevel
    code: str
    message: str
    hint: str | None = None
    ref: str | None = None
    source: str = SOURCE_FLOW
    #: 批次来源才有:是哪个批次跑出来的
    batch_id: str | None = None


@dataclass(frozen=True)
class ExceptionGroup:
    """同一个问题码的一组商品(第二层)。"""

    step: FlowStep
    code: str
    level: IssueLevel
    message: str
    hint: str | None
    entries: tuple[ExceptionEntry, ...]

    @property
    def count(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class CategorySection:
    """一个步骤下的全部异常(第一层)。"""

    step: FlowStep
    label: str
    groups: tuple[ExceptionGroup, ...]

    @property
    def count(self) -> int:
        return sum(g.count for g in self.groups)

    @property
    def blocking_count(self) -> int:
        return sum(
            g.count for g in self.groups if g.level is IssueLevel.BLOCKING
        )


def entries_from_flow(
    product_id: str, sku: str, spu: str, result: FlowResult
) -> list[ExceptionEntry]:
    """把一件商品的判定结论摊平成异常条目。

    只收阻断与待确认。提醒级不进异常列表 —— §3.3.2 已经定了口径:
    提醒不进徽标。异常列表如果收提醒,它就会变成一张永远清不空的表,
    然后没人再看它。
    """
    out: list[ExceptionEntry] = []
    for issue in result.issues:
        if issue.level is IssueLevel.REMINDER:
            continue
        out.append(
            ExceptionEntry(
                product_id=product_id,
                sku=sku,
                spu=spu,
                step=issue.target_step,
                level=issue.level,
                code=issue.code,
                message=issue.message,
                hint=issue.hint,
                ref=issue.ref,
                source=SOURCE_FLOW,
            )
        )
    return out


def entry_from_batch_row(row: Mapping[str, object]) -> ExceptionEntry | None:
    """把一条批次条目翻成异常条目。成功与跳过的返回 None。

    `hint` 取异常码注册表里的 `advice` —— 那句话就是"下一步该做什么",
    §6.3.1 第三条要的正是它。
    """
    from app.workbench.batch import BatchItemStatus, error_meta

    status = str(row.get("status") or "")
    if status not in (
        BatchItemStatus.FAILED.value,
        BatchItemStatus.NEEDS_HUMAN.value,
    ):
        return None

    code = str(row.get("error_code") or "UNEXPLAINED")
    meta = error_meta(code)
    step_raw = row.get("target_step")
    try:
        step = FlowStep(str(step_raw)) if step_raw else meta.category
    except ValueError:
        step = meta.category

    level = (
        IssueLevel.NEEDS_CONFIRM
        if status == BatchItemStatus.NEEDS_HUMAN.value
        else IssueLevel.BLOCKING
    )
    return ExceptionEntry(
        product_id=str(row.get("product_id") or ""),
        sku=str(row.get("sku") or ""),
        spu=str(row.get("spu") or ""),
        step=step,
        level=level,
        code=code,
        message=str(row.get("error_message") or meta.label),
        hint=meta.advice,
        ref=(None if row.get("target_ref") is None else str(row.get("target_ref"))),
        source=SOURCE_BATCH,
        batch_id=(None if row.get("batch_id") is None else str(row.get("batch_id"))),
    )


def group(entries: Iterable[ExceptionEntry]) -> tuple[CategorySection, ...]:
    """两层分组。步骤按 `STEP_ORDER`,组内按件数降序。

    件数降序而不是按码字典序:运营要先处理影响面最大的那一类。
    空步骤也返回(count=0),这样界面上五个分类始终在同一位置,
    不会因为某一类清空了而让其余几类跳位。
    """
    collected: dict[FlowStep, dict[str, list[ExceptionEntry]]] = {
        step: {} for step in STEP_ORDER
    }
    for entry in entries:
        bucket = collected.setdefault(entry.step, {})
        bucket.setdefault(entry.code, []).append(entry)

    sections: list[CategorySection] = []
    for step in STEP_ORDER:
        buckets = collected.get(step, {})
        groups = [
            ExceptionGroup(
                step=step,
                code=code,
                level=_worst_level(items),
                message=items[0].message,
                hint=items[0].hint,
                entries=tuple(items),
            )
            for code, items in buckets.items()
        ]
        groups.sort(key=lambda g: (-g.count, g.code))
        sections.append(
            CategorySection(step=step, label=STEP_LABELS[step], groups=tuple(groups))
        )
    return tuple(sections)


def _worst_level(entries: Sequence[ExceptionEntry]) -> IssueLevel:
    """同一码下混了两个级别时取更严重的那个,不取第一条。

    会混:同一个 `IMAGESET_*` 码在一件商品上阻断、在另一件上只是待确认。
    取第一条会让整组的颜色由列表顺序决定,而颜色是运营决定先看哪一组的依据。
    """
    if any(e.level is IssueLevel.BLOCKING for e in entries):
        return IssueLevel.BLOCKING
    if any(e.level is IssueLevel.NEEDS_CONFIRM for e in entries):
        return IssueLevel.NEEDS_CONFIRM
    return IssueLevel.REMINDER


def summarize(sections: Iterable[CategorySection]) -> dict[str, int]:
    """顶部计数:每个分类多少条、阻断多少条。"""
    out: dict[str, int] = {"total": 0, "blocking": 0}
    for section in sections:
        out[section.step.value] = section.count
        out["total"] += section.count
        out["blocking"] += section.blocking_count
    return out


def _entry_out(entry: ExceptionEntry) -> dict[str, object]:
    return {
        "product_id": entry.product_id,
        "sku": entry.sku,
        "spu": entry.spu,
        "step": entry.step.value,
        "level": entry.level.value,
        "code": entry.code,
        "message": entry.message,
        "hint": entry.hint,
        "ref": entry.ref,
        "source": entry.source,
        "batch_id": entry.batch_id,
    }


def _group_out(item: ExceptionGroup) -> dict[str, object]:
    return {
        "code": item.code,
        "level": item.level.value,
        "message": item.message,
        "hint": item.hint,
        "count": item.count,
        "entries": [_entry_out(e) for e in item.entries],
    }


def serialize(sections: Iterable[CategorySection]) -> list[dict[str, object]]:
    """接口出参形状。前端一层层展开,不用再做分组。"""
    return [
        {
            "step": section.step.value,
            "label": section.label,
            "count": section.count,
            "blocking_count": section.blocking_count,
            "groups": [_group_out(g) for g in section.groups],
        }
        for section in sections
    ]
