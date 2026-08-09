"""文案生成的幂等单元(PRD §4.9 / AC-11)。**零依赖纯函数。**

## 它防的是一件正在花钱的事

`listing_copies` 从第一版起就是 **SPU 粒度**的(`spu` + `channel` + `site` +
`locale` 唯一)。而**入口是 SKU 粒度**的:
`POST /products/{product_id}/copy/generate` 收的是一行商品。

一个 S/M/L 三码的 SPU,运营在三行上各点一次「生成文案」,得到的是:

    三次真实的 LLM 调用          —— 三次付费
    同一个 scope 下的三个版本    —— 后两次把前一次挤成历史版本
    完全相同的输入              —— 已确认事实没变、语言没变、渠道没变

**三次调用产出的是同一件东西。** 尺码不进文案:§4.9 明写同一颜色下
尺码不换图,文案同理 —— 标题、卖点、描述里没有任何一个字来自 `products.size`。

批量那条路径的形状**早就是对的**(计划阶段按 `scope_key` 去重、回执表
挡重跑),所以这个缺口只长在单件入口上,而单件入口恰恰是运营日常用的
那一个。注意批量的作用域也在 batch24 跟着改细了:
`batch.ACTION_SCOPE[GENERATE_COPY] == "spu_colour"` —— 去重键必须和
`listing_copies` 的槽位一样细,粗一档的表现是三颜色 SPU 只出一稿文案。


## 幂等单元的四个输入,以及**刻意不在里面**的那些

    channel / site / locale   不同渠道站点语言是不同的文案
    spu                       身份
    fact_versions             已确认事实的版本集合 —— 事实变了就该重新生成
    color_variant_id          这一稿写的是哪个颜色(AC-11 第二句)

`color_variant_id` 从 A45-batch24 起**由生产接线真的传值**,不再恒为 None。
在那之前它是一个"留好位置等以后接"的形参,而**键其实早就随颜色变了**:
`fact_versions` 里带着该颜色的 `primary_color` 版本。于是键按颜色分,
存储按 SPU 分 —— 多颜色 SPU 在颜色轴上来回点生成,每次都判"不命中,
重新生成",两个颜色的文案还互相把对方挤成历史版本。缺口与修法见 §3.54。

留着这个显式轴而不是"反正 fact_versions 带得动":它让键的作用域**写在
签名上**,而不是从一条间接链推出来。哪天颜色事实不再进事实集(比如
改成只把 SPU 层字段喂给文案),显式轴仍然把两个颜色分开;
反过来这个轴被谁改回 None 时,`fact_versions` 仍在兜着。
两条同向的防线,守卫各钉一头。


**不在里面的:`size` / `sku` / `product_id`。** 这是本模块存在的全部理由,
所以它由一条反向守卫钉着,而不是靠这段注释。

`fact_versions` 用**集合**不用列表:同一批事实按不同顺序查出来是同一件事,
而按列表算的话换一次 `ORDER BY` 就会算出一个新键,于是"上游没变"也重新
付费生成一次 —— 与 `upstream_snapshot.active_colors` 排序那条同一个坑。

## 为什么复用要挑状态,而不是"有就复用"

`REJECTED` 的那一版是规则硬失败落库的产物(`copy_service.save_copy` 会把
不合格的输出也存下来,好让运营看见模型到底吐了什么)。拿它当复用对象的话,
**这个 SPU 的文案永远修不好** —— 每次点生成都返回那一版失败的,
而运营看到的现象是"点了没反应"。

与 `attributes/run_state.unique_index_predicate()` 是同一条规矩的第二次应用:
**失败态不许占着幂等槽位。**

## `force`:显式重做不算重复

单字段重生(`only_fields`)与「我就是想再要一版」是运营的明确意图,
不是重复点击。它们走 `force=True`,跳过复用。

这不会把缺口放回去:那是**一次点击一次调用**,不随尺码数量翻倍 ——
而 AC-11 要收口的正是"翻倍"那一项。
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

#: 键的算法版本。**换算法必须换它**,否则旧行的键与新算出来的键混在一张表里,
#: 而"没命中"和"算法换了"在查询结果上长得一模一样(都是查不到)。
COPY_UNIT_VERSION = "1"

#: 可以被复用的文案状态。
#:
#: `ARCHIVED` 不在里面:它是被更高版本挤下去的历史版本,复用它等于回滚。
#: `REJECTED` 不在里面,理由见模块文档。
REUSABLE_STATUSES: frozenset[str] = frozenset({"DRAFT", "VALIDATED", "APPROVED"})


class CopyUnitVerdict(StrEnum):
    """这次「生成文案」该做什么。"""

    #: 命中同一个幂等单元且状态可复用 —— **不调生成器,不花钱**
    REUSE = "REUSE"
    #: 没有命中,或命中的那一版不可复用 —— 正常生成
    GENERATE = "GENERATE"
    #: 调用方显式要求重做(单字段重生 / 强制重生)
    FORCED = "FORCED"


@dataclass(frozen=True)
class CopyUnit:
    """一次文案生成的幂等单元。

    做成 dataclass 而不是直接传一串字符串,是因为它有五个字段而其中两个
    (`channel` / `site`)在本仓今天恒为常量 —— 位置参数传法下,把
    `locale` 传到 `site` 的位置不会报错,只会安静地算出一个永不命中的键。
    """

    spu: str
    channel: str
    site: str
    locale: str
    #: 已确认事实的版本 id。**集合语义**,顺序与重复不影响键
    fact_versions: tuple[str, ...] = ()
    #: 这一稿写的是哪个颜色(变体身份串)。
    #:
    #: None 的含义是"SPU 级文案",今天只出现在两处:0051 之前的存量行,
    #: 以及不经过工作台的构造。生产接线一律传值(A45-batch24)
    color_variant_id: str | None = None

    def key(self) -> str:
        """这个单元的幂等键。同一份输入必然算出同一个键。"""
        facts = ",".join(sorted({str(v) for v in self.fact_versions if str(v).strip()}))
        payload = "|".join(
            (
                COPY_UNIT_VERSION,
                self.spu,
                self.channel,
                self.site,
                self.locale,
                self.color_variant_id or "-",
                facts,
            )
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:48]
        return f"c1-{digest}"


def unit_for(
    *,
    spu: str,
    channel: str,
    site: str,
    locale: str,
    fact_versions: Iterable[str],
    color_variant_id: str | None = None,
) -> CopyUnit:
    """关键字构造。**位置参数在这里是个真实的坑**,见 `CopyUnit` 的文档。"""
    return CopyUnit(
        spu=spu,
        channel=channel,
        site=site,
        locale=locale,
        fact_versions=tuple(str(v) for v in fact_versions),
        color_variant_id=color_variant_id,
    )


def decide(
    *,
    existing_key: str | None,
    existing_status: str | None,
    wanted_key: str,
    force: bool = False,
) -> CopyUnitVerdict:
    """要不要复用已有的那一版。

    `existing_key` 是库里最新那一版文案上记的键;`None` 表示那一版建于
    幂等键落列之前(迁移 0050 之前)。**存量行判 GENERATE,不判 REUSE** ——
    与 `upstream_snapshot` 两列的 NULL 口径反向而同源:那边的 NULL 会让人
    被静默放行(所以判不可证明),这边的 NULL 只会让人多生成一次
    (所以判重新生成)。**默认值的方向要按代价选,不按对称选。**
    """
    if force:
        return CopyUnitVerdict.FORCED
    if not existing_key or existing_key != wanted_key:
        return CopyUnitVerdict.GENERATE
    if (existing_status or "") not in REUSABLE_STATUSES:
        return CopyUnitVerdict.GENERATE
    return CopyUnitVerdict.REUSE


def spends_money(verdict: CopyUnitVerdict) -> bool:
    """这个判定会不会通往一次真实的生成调用。

    存在的理由是别处不要再写 `verdict != REUSE`:那种写法在新增一个
    判定取值时**默认把它算成花钱的**,而新取值多半是新的复用形态 ——
    默认方向反了,表现是台账上凭空多出一批调用。
    """
    return verdict is not CopyUnitVerdict.REUSE


__all__ = [
    "COPY_UNIT_VERSION",
    "REUSABLE_STATUSES",
    "CopyUnit",
    "CopyUnitVerdict",
    "decide",
    "spends_money",
    "unit_for",
]
