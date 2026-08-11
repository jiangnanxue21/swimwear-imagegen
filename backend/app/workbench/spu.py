"""SPU-SKU 聚合(阶段 3,FE-306)。**零数据库依赖纯函数。**

验收结果只有一句话:**公共属性与变体属性不混淆**。这句话在实现上有三层:

    1  变体维度(颜色 × 尺码)按 SKU 挂,不能显示成 SPU 的属性
    2  公共属性(款式、面料、领口…)按 SPU 挂,不该在每个 SKU 上重复一遍
    3  **本该公共的属性在各 SKU 上取值不一致时,必须报出来**

第 3 层是这一页真正的价值。任务书 §1.5 说"若为 SPU 级,则公共属性挂 SPU、
颜色/尺码挂 SKU",但现实里同一款泳衣的 4 个 SKU 常常识别出 3 种面料 ——
属性识别是逐图跑的,而 4 个 SKU 各有各的图。这种不一致如果只是"取第一个
SKU 的值显示在 SPU 上",那么导出的 SPU 级字段就是随机的,而没有任何人会发现。

所以这里的 `common` 只收**全体一致**的字段,不一致的进 `inconsistent`,
并且带上每个取值对应的 SKU 清单 —— 运营看到"面料:涤纶(2 件)/ 氨纶(2 件)"
才知道该去改哪两件。

## 与阶段 1-2 的关系

图片集 / 文案 / 草稿三张表本来就以 spu 为 scope(见 service.py 的 `_current_*`),
所以这一页不需要动数据模型 —— FE-306 的注解说"若阶段1–2 已按 SPU 级建模,
本项工作量可下降",这里是那句话成立的地方。

它同时解释了 `batch.py` 里为什么 SPU 级动作要去重:同一 SPU 的 4 个 SKU
共享一份文案,批量生成时跑 4 次就是付 4 次钱、留 1 份结果。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

#: 变体维度。这两个字段**永远**按 SKU 展示,不参与公共/不一致判定。
#:
#: 尺码不参与图片识别(属性注册表里根本没有这个字段,见 product.py 注释),
#: 颜色则是识别得出的 —— 但它在 SPU 下本来就该不同,不一致不是问题。
VARIANT_FIELDS: tuple[str, ...] = ("primary_color", "size")

VARIANT_FIELD_LABELS: Mapping[str, str] = {
    "primary_color": "颜色",
    "size": "尺码",
}


@dataclass(frozen=True)
class SkuRow:
    """一个 SKU 的聚合输入。

    `attributes` 只放**已确认**的属性值:未确认的建议值不该参与
    "各 SKU 是否一致"的判定,否则一件商品刚跑完识别就会报一堆不一致。
    """

    product_id: str
    sku: str
    name: str = ""
    color: str | None = None
    size: str | None = None
    #: 这一行商品挂的 SPU 主键(`products.spu_id`,阶段 1 落的外键)。
    #:
    #: **它不是 `spu` 那个字符串码。** 两者的区别是这一批存在的全部理由:
    #: 生成方案、颜色变体、按颜色上传三条动线的接口收的都是 UUID,而这一页
    #: 此前只有字符串码 —— 前端因此没有任何一条路径拿得到主键,方案面板
    #: 写完了三批也接不进去(见 `docs/DECISIONS.md` §3.48)。
    #:
    #: 可空:老建档路径(`create_product` / CSV 导入)建的行今天仍可能没有
    #: 这一列。**空就是空,前端据此不给链接**,不许拿字符串码顶上去。
    spu_id: str | None = None
    attributes: Mapping[str, str] = None  # type: ignore[assignment]
    #: 这个 SKU 自己的流程状态,用于在展开行上显示卡点
    completion: int = 0
    blocking_count: int = 0
    next_action_label: str = ""
    #: 这一只**卡在哪几步**:有 BLOCKING 问题的步骤名(`FlowStep` 的值)。
    #:
    #: 一只 SKU 可以同时卡在两步(属性没确认 + 图片集缺角度),所以这是个
    #: 集合而不是单值。取数层从 `FlowResult.steps[].issues` 现算,
    #: **这一层不判定 issue 的级别** —— 那是 `flow.py` 的事。
    blocked_steps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.attributes is None:
            object.__setattr__(self, "attributes", {})


@dataclass(frozen=True)
class FieldConsistency:
    """一个公共字段在各 SKU 上的取值分布。"""

    field_name: str
    #: 取值 -> 持有它的 SKU 列表。一致时只有一个键
    values: Mapping[str, tuple[str, ...]]

    @property
    def consistent(self) -> bool:
        return len(self.values) <= 1

    @property
    def value(self) -> str | None:
        """一致时的那个值。不一致时为 None —— **不猜一个出来。**"""
        if not self.consistent:
            return None
        return next(iter(self.values), None)


@dataclass(frozen=True)
class SpuGroup:
    spu: str
    name: str
    skus: tuple[SkuRow, ...]
    #: 全体 SKU 取值一致的属性(挂 SPU)
    common: tuple[FieldConsistency, ...]
    #: 本该一致却不一致的属性。**这是要人处理的那一列**
    inconsistent: tuple[FieldConsistency, ...]
    #: 变体维度的取值汇总,如 {"颜色": ("黑","白"), "尺码": ("S","M")}
    variants: Mapping[str, tuple[str, ...]]

    @property
    def spu_id(self) -> str | None:
        """这一组的 SPU 主键。**推不出来时给 None,不猜一个出来。**

        分组键是 `spu` 字符串码,而主键住在每一行商品上,所以这里做的是
        一次收敛而不是一次读取。三种情况:

            全组同一个非空值   给它 —— 这是建档路径建出来的正常形状
            一个非空值都没有   给 None(老建档路径,那批商品还没有主键)
            **两个以上不同值** 给 None

        第三种是这个属性存在的理由。它意味着两个不同的 SPU 行共用了同一个
        字符串码 —— §4.1 的唯一约束不允许,但**约束在库里、这个函数在纯层**,
        而纯层拿到的是别人喂进来的行。挑一个出来的代价是运营点进去看到的是
        另一个款:方案面板会照着那个主键列方案、启用、把**别人的**图片集
        判过期,而每一步都不会报错。

        与 `FieldConsistency.value` 同一条口径(不一致时返回 None),
        理由也同一条:**这一页宁可少给一个链接,不可给一个错的。**
        """
        ids = {row.spu_id for row in self.skus if row.spu_id}
        if len(ids) != 1:
            return None
        return next(iter(ids))

    @property
    def sku_count(self) -> int:
        return len(self.skus)

    @property
    def blocking_count(self) -> int:
        return sum(s.blocking_count for s in self.skus)

    @property
    def completion(self) -> int:
        """SPU 级完成度 = 各 SKU 的最小值,不是平均值。

        平均值会给出"4 个 SKU 三个做完了 = 75%"这种读数,而这个 SPU
        一件也上不了架:上架文件是整个 SPU 一起导的。取最小值才对得上
        "这个 SPU 还能不能导出"这个真问题。
        """
        if not self.skus:
            return 0
        return min(s.completion for s in self.skus)

    @property
    def blocked_steps(self) -> Mapping[str, int]:
        """卡点分布:`{步骤: 卡在该步的 SKU 数}`(a47 §8.1)。

        ## 为什么是分布,而不是一个款级「下一步」

        v2.0 的 PRD 要求聚合出款级 `next_action_label`,规则是"取阻断数最大
        的那只 SKU 的下一步"。那和明令禁止的"拿第一个 SKU 的 next_action
        当款级"**只差在任意性的形式**,本质一样 —— 把一个没有真实语义的
        推导从前端搬到后端,并不会让它变成事实。

        款级"下一步"在这套状态机里**没有定义**:不同 SKU 卡在不同步骤时,
        任何单选都是编的。所以给分布,让运营自己看"这款有 3 只卡在图片集、
        1 只卡在文案",然后自己决定先动哪一只。

        **纪律的对象不是"哪一侧写代码",是"这个值有没有真实定义"。**

        ## 口径:数的是 SKU,不是问题条数

        一只 SKU 同时卡在两步时,两步各记它一次。因此

            sum(blocked_steps.values()) >= 有阻断的 SKU 数
            sum(blocked_steps.values()) != blocking_count

        后一条容易被拿去对账,所以写在这里:`blocking_count` 数的是**问题
        条数**(一只 SKU 在一步里可以有三条阻断),这里数的是**SKU 个数**。
        两个数字回答两个不同的问题,不该相等。

        没有任何 SKU 卡住的步骤**不出现在结果里**,不给 0 —— 一张全是 0
        的表会让真正有值的那一格淹掉。
        """
        counts: dict[str, int] = {}
        for row in self.skus:
            # 同一只 SKU 在同一步里的多条阻断只算一次:去重在这里,
            # 而不是指望取数层送进来的已经是集合
            for step in set(row.blocked_steps):
                counts[step] = counts.get(step, 0) + 1
        return counts


def aggregate(
    rows: Iterable[tuple[str, str, SkuRow]],
    *,
    common_fields: Sequence[str] | None = None,
) -> tuple[SpuGroup, ...]:
    """按 SPU 分组。入参是 (spu, spu_name, sku) 三元组。

    `common_fields` 不传时取所有 SKU 上出现过的属性字段并集(减去变体维度)。
    传了则只看这几个 —— 属性字典锁定后(§2.1)按字典来,免得把某个
    只在一个 SKU 上被确认过的字段也拿去比对。
    """
    buckets: dict[str, list[SkuRow]] = {}
    names: dict[str, str] = {}
    for spu, spu_name, row in rows:
        buckets.setdefault(spu, []).append(row)
        if spu_name and spu not in names:
            names[spu] = spu_name

    groups: list[SpuGroup] = []
    for spu in sorted(buckets):
        skus = sorted(buckets[spu], key=lambda r: r.sku)
        fields = _fields_to_compare(skus, common_fields)

        common: list[FieldConsistency] = []
        inconsistent: list[FieldConsistency] = []
        for field_name in fields:
            consistency = _consistency(skus, field_name)
            if not consistency.values:
                continue  # 没有任何 SKU 确认过这个字段,不显示空行
            (common if consistency.consistent else inconsistent).append(consistency)

        groups.append(
            SpuGroup(
                spu=spu,
                name=names.get(spu, ""),
                skus=tuple(skus),
                common=tuple(common),
                inconsistent=tuple(inconsistent),
                variants=_variants(skus),
            )
        )
    return tuple(groups)


def _fields_to_compare(
    skus: Sequence[SkuRow], common_fields: Sequence[str] | None
) -> list[str]:
    if common_fields is not None:
        return [f for f in common_fields if f not in VARIANT_FIELDS]
    seen: set[str] = set()
    for row in skus:
        seen.update(row.attributes.keys())
    return sorted(f for f in seen if f not in VARIANT_FIELDS)


def _consistency(skus: Sequence[SkuRow], field_name: str) -> FieldConsistency:
    """一个字段的取值分布。**没有值的 SKU 不算一种取值。**

    不算的理由:一件 SKU 还没确认这个属性,和它确认成了另一个值,
    是完全不同的两件事。前者是"还没做",后者是"做错了",混在一起会让
    刚导入的商品整页都是红色不一致。
    """
    values: dict[str, list[str]] = {}
    for row in skus:
        raw = row.attributes.get(field_name)
        if raw is None or str(raw).strip() == "":
            continue
        values.setdefault(str(raw), []).append(row.sku)
    return FieldConsistency(
        field_name=field_name,
        values={value: tuple(sorted(skus_)) for value, skus_ in values.items()},
    )


def _variants(skus: Sequence[SkuRow]) -> dict[str, tuple[str, ...]]:
    colors = sorted({s.color for s in skus if s.color})
    sizes = sorted({s.size for s in skus if s.size})
    out: dict[str, tuple[str, ...]] = {}
    if colors:
        out[VARIANT_FIELD_LABELS["primary_color"]] = tuple(colors)
    if sizes:
        out[VARIANT_FIELD_LABELS["size"]] = tuple(sizes)
    return out


def serialize(groups: Iterable[SpuGroup]) -> list[dict[str, object]]:
    """接口出参。公共属性、不一致属性、变体维度三段分开给前端。"""
    return [
        {
            "spu": g.spu,
            # 主键与字符串码**并排给出**,不合并成一个字段。前端要判「这一行
            # 点不点得进去」,而那个判断的依据是主键在不在,不是码长什么样
            "spu_id": g.spu_id,
            "name": g.name,
            "sku_count": g.sku_count,
            "completion": g.completion,
            "blocking_count": g.blocking_count,
            # a47 §8.1:卡点分布。**没有款级 next_action**,理由见
            # `SpuGroup.blocked_steps` 的文档 —— 那个值在这套状态机里
            # 没有定义,编一个出来只会让运营照着它做错事
            "blocked_steps": dict(g.blocked_steps),
            "variants": {k: list(v) for k, v in g.variants.items()},
            "common": [
                {"field_name": c.field_name, "value": c.value} for c in g.common
            ],
            "inconsistent": [
                {
                    "field_name": c.field_name,
                    "values": [
                        {"value": value, "skus": list(skus)}
                        for value, skus in sorted(c.values.items())
                    ],
                }
                for c in g.inconsistent
            ],
            "skus": [
                {
                    "product_id": s.product_id,
                    "sku": s.sku,
                    "name": s.name,
                    "color": s.color,
                    "size": s.size,
                    "completion": s.completion,
                    "blocking_count": s.blocking_count,
                    "next_action_label": s.next_action_label,
                    "blocked_steps": list(s.blocked_steps),
                }
                for s in g.skus
            ],
        }
        for g in groups
    ]
