"""草稿的上游快照:颜色轴的版本引用与颜色→SKU→图片映射(PRD §4.10 / §6.7)。

**零依赖纯函数。** 只用标准库 + 本项目的零依赖模块(`core.enums`、
`workbench.stale`、`listings.image_set_rules`)。

## 它补的是哪一格

`stale.diff_components` 今天能解释六个轴:属性版本集、图片集、文案、手填、
导出模板版本、字段映射版本。**没有一个带颜色维。** 于是这些话都说不出来:

    这个 SPU 新增了一个 ACTIVE 颜色,草稿里没有它的 SKU 行
    红色的样品换了,红色的事实版本变了,而草稿引用的还是上一版
    红色的生成方案换了指纹,它那一版图片集已经不该用了
    某个颜色被停用了,它的 SKU 还在导出文件里

§6.7 把判定口径写死成「`upstream_versions_json` 逐项与当前版本比较派生,
**不落 STALE 状态列**」—— 也就是现有草稿过期机制的推广,不是第二套机制。
所以这里输出的是 `stale.StaleChange`,与 `diff_components` 同一种东西,
由同一处(`workbench/service.refresh_draft`)汇总。

## 与 `diff_components` 的分工:每个轴只有一个人报

`build_upstream_versions()` 落进快照的 `copies` / `spec_version` /
`mapping_version` 三项,**值是从 `components` 抄过来的**(单一写入点),
落列是因为 §4.10 点名要它们在这一列里。但 `diff_upstream()` **不比它们** ——
那三个轴归 `diff_components`。

两边都比的话,改一次文案版本会出两条过期提示,而运营看到的是
「这份草稿有 2 个上游变了」,于是「变了几处」这个数字从此不可信。
反向守卫在 `tests/pure/test_a45_batch19_draft_upstream_snapshot.py`。

**每个颜色的 `image_set` 是这条分工的第四项(A45-batch20 补的)。**
5-1 落码时它是被比较的,而接线一做就暴露了:图片集今天**按 SPU 批准**
(`image_set_service.resolve_for_publish` 解析的是 `(spu, channel, site)`),
于是每个 ACTIVE 颜色引用的是同一版,重新批准一次会让本模块报 N 条、
`diff_components` 再报 1 条,合计 N+1 条说的是同一件事。

值仍然落进快照,理由是 AC-19 要「可解释的上游版本引用」——
「这个颜色的映射是从哪一版图片集铺出来的」在出事之后是第一个要查的东西。
**记下来但不比较**,与上面那三项一字不差是同一条规矩。
按颜色批准的图片集哪天真的存在了,把它移回被比较的一侧要连同
`diff_components` 那一侧一起改,不能只加回来。

## 只看 ACTIVE 颜色 —— 而「只看」不等于「不记」

§6.7:范围口径只检查 `sellable_status=ACTIVE` 的颜色与其 SKU;
PLANNED / PAUSED / DISABLED 不阻塞、不参与映射完整性判定。

快照因此分两格:

    colors     ACTIVE 的颜色,带全部版本引用。**只有这一格参与判定**
    inactive   其余颜色,只记 `{颜色 id: 可售状态}`,不记任何版本

`inactive` 不参与判定,它是给过期提示用的:一个颜色从 `colors` 里消失时,
「它被停用了」和「它被删了」对运营是两句不同的话,而只留 `colors` 的快照
分不出这两者。**同时它必须不带版本** —— 带上版本的话,一个 PLANNED 颜色
的事实每被改一次,草稿就过期一次,而 §6.7 说的正是那不该发生。

## 空值口径:None 是「没算过」,`{}` 是「算过,是空的」

两列都可空且没有 `server_default`,理由与迁移 0042 / 0045 / 0048 同:
`{}` 会让 0049 之前建的存量草稿看起来「颜色维算过且没有变化」,
于是它们在颜色轴上**静默放行**。

所以 `diff_upstream(stored=None, ...)` 返回一条变化而不是空列表,
`map_problems(None, ...)` 返回一条问题而不是通过 —— 与
`stale.copy_attrs_stale(plan_missing=True)` 同向:没有快照就无法证明
这份草稿仍然成立,而这里的默认必须偏保守。

## 本批(5-1)的边界

**只有判定,没有接线。** `build_*` 的调用点在 `workbench/service.build_draft`,
`diff_upstream` / `map_problems` 的调用点在 `refresh_draft` 与 READY 门禁,
两者都是 5-2 的事。欠账守卫见上面那个测试文件。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import SellableStatus
from app.listings.image_set_rules import SetView, VariantCoverage, validate_set
from app.workbench.stale import StaleChange

#: 快照自身的形状版本。**换形状必须换它。**
#:
#: 形状变了而版本没变时,`diff_upstream` 会拿新形状的键去读旧快照,
#: 读不到的一律等于 None —— 于是每个轴都「没变」,存量草稿全部静默放行。
#: 版本对不上时本模块判「不可比」,与 stored=None 同一档。
UPSTREAM_SCHEMA_VERSION = "1"
#: 图片映射与上游版本是两列、两个读取方。分开版本后,只改映射形状不会把
#: `upstream_versions` 一起判成不可比,反向亦然。两列仍由 5-2 同批重建。
IMAGE_MAP_SCHEMA_VERSION = "1"

#: `StaleChange.component` 在本模块里的**封闭取值集**。
#:
#: 写成常量而不是散在各处的字符串字面量,是因为前端与过期提示要按 component
#: 分组展示;散着写的话,改一个拼写不会有任何东西红,表现是那一类提示
#: 从分组里消失(而不是报错)。`diff_components` 那六个取值同理,
#: 只是它落在 §4.5 矩阵上,那边由矩阵守着。
COMPONENTS: tuple[str, ...] = (
    "upstream_snapshot",   # 快照本身缺失 / 形状版本对不上
    "shared_facts",        # SPU 层已确认事实的版本集
    "shared_sample",       # 共享作用域样品指纹
    "color_set",           # 哪些颜色是 ACTIVE(新增 / 停用)
    "color_facts",         # 某个颜色的已确认事实版本集
    "color_sample",        # 某个颜色的样品指纹
    "color_plan",          # 某个颜色当前生效的 GenerationPlan 指纹
)
# 这里**刻意没有** `color_image_set`:那个轴归 `diff_components` 的
# `image_set` 报,见模块文档「每个轴只有一个人报」的第四项。
# 加回来之前先读那一段 —— 它不是漏了,是 batch20 接线时算出 N+1 条重复提示
# 之后拿掉的。前端的 `STALE_COMPONENT_LABEL` 与本元组由
# `tests/pure/test_frontend_contract.py` 做集合相等,所以加回来会当场变红。


@dataclass(frozen=True)
class ColorUpstream:
    """一个颜色在快照里的全部上游引用。**判定投影,不是 ORM 行。**

    做得很窄的理由与 `image_set_rules.ItemView` 同:传 ORM 行进来的话,
    这一层就开始依赖数据库的形状,而它的全部价值在于不依赖。

    两个 builder 共用这一个视图,不各建一个:`upstream_versions` 与
    `color_sku_image_map` 是同一批颜色的两种投影,分成两个视图之后
    「这两列说的是不是同一组颜色」就没有任何东西保证了 —— 而它们
    恰恰要被 READY 门禁同时读。
    """

    #: `color_variants.id`。**身份是 id 不是名字**(§4.3 / DECISIONS §3.17)
    variant_id: str
    variant_code: str = ""
    sellable_status: str = SellableStatus.PLANNED.value
    #: 该颜色 VARIANT 层 `CONFIRMED` 事实的 `version_id -> field_key` 映射。
    # 只存 id 会让 `StaleChange.fields` 退化成 UUID,
    # 与 §4.5.1「能点名就点名」相违背；映射也让版本差异可直接还原字段名。
    fact_versions: Mapping[str, str] = field(default_factory=dict)
    #: 该颜色作用域的样品指纹(`attributes.scope_fingerprint.fingerprints` 的一项)
    sample_fingerprint: str | None = None
    #: 该颜色**当前生效**的方案指纹(`workflows.generation_plan.effective_fingerprints`)
    plan_fingerprint: str | None = None
    #: 该颜色的图片集。没有已批准的那一版时两项都是 None。
    #: **落进快照但不被 `diff_upstream` 比较** —— 见模块文档第四项
    image_set_id: str | None = None
    image_set_version: int | None = None
    #: 这个颜色下 `products.sku` 的全集。采集口径是
    #: `products.color_variant_id == variant_id`,不再按 Product.status / 库存 / 条码 /
    #: 价格二次过滤；那些是 READY 的其它门禁。build 与 check 必须消费同一个
    #: `ColorUpstream` 投影,否则两侧各筛一次会制造假的「SKU 不在映射里」。
    sku_codes: tuple[str, ...] = ()

    @property
    def is_active(self) -> bool:
        return self.sellable_status == SellableStatus.ACTIVE.value


def active_colors(colors: Iterable[ColorUpstream]) -> tuple[ColorUpstream, ...]:
    """§6.7 的范围口径。**两个 builder 都从这里过一遍,不各自筛。**

    按 `variant_id` 排序,不按输入顺序:快照要落进 JSONB 并被逐项比较,
    而「同一份事实算出同一个快照」是比较能成立的前提。查询顺序变一次
    (换个 ORDER BY、换个 join)就重新算出一个不等的快照的话,
    草稿会在没有任何上游变化时被判过期,而这个机制立刻变成噪音。
    """
    return tuple(sorted((c for c in colors if c.is_active), key=lambda c: c.variant_id))


# ================================================================ 快照构建


def build_upstream_versions(
    *,
    components: Mapping[str, Any],
    colors: Iterable[ColorUpstream],
    shared_fact_versions: Mapping[str, str] | None = None,
    shared_sample_fingerprint: str | None = None,
) -> dict[str, Any]:
    """组装 `listing_drafts.upstream_versions`(§4.10)。

    `components` 是 `workbench/service._components_of()` 的产物。
    从它身上抄 `copies` / `spec_version` / `mapping_version` 三项 ——
    **抄,不是重新采集**:§4.10 点名这一列要含 Copy 版本,而那三个轴
    今天已经有一个写入点了,再采集一次就是第二个漂移源。

    这三项落进列里但不参与本模块的比较,见模块文档「每个轴只有一个人报」。
    """
    kept = active_colors(colors)
    inactive = {
        c.variant_id: c.sellable_status
        for c in sorted(colors, key=lambda c: c.variant_id)
        if not c.is_active
    }
    return {
        "schema": UPSTREAM_SCHEMA_VERSION,
        "shared": {
            "fact_versions": _fact_snapshot(shared_fact_versions or {}),
            "sample_fingerprint": shared_sample_fingerprint,
        },
        "colors": {
            c.variant_id: {
                "variant_code": c.variant_code,
                "fact_versions": _fact_snapshot(c.fact_versions),
                "sample_fingerprint": c.sample_fingerprint,
                "plan_fingerprint": c.plan_fingerprint,
                "image_set": (
                    None
                    if c.image_set_id is None
                    else {"id": c.image_set_id, "version": c.image_set_version}
                ),
            }
            for c in kept
        },
        # 不带版本。带上的话 PLANNED 颜色的事实一改草稿就过期,而 §6.7 说不该
        "inactive": inactive,
        # ---- 以下三项:值的唯一来源是 components,本模块不比较它们 ----
        "copies": dict(components.get("copies") or {}),
        "spec_version": str(components.get("spec_version") or ""),
        "mapping_version": str(components.get("mapping_version") or ""),
    }


def build_color_sku_image_map(
    *,
    colors: Iterable[ColorUpstream],
    coverage: Mapping[str, VariantCoverage],
) -> dict[str, Any]:
    """组装 `listing_drafts.color_sku_image_map`(§4.10):颜色 → SKU → 主图/附图。

    `coverage` 是 `image_set_rules.coverage()` 的产物。**「哪几张图属于这个
    颜色」这个判断不在这里重写一遍** —— §6.5 的那套规则(通用图只能进附图位、
    不得回退用别的颜色的图、通用图必须被显式标记)全部长在 `image_set_rules`
    上,这里只做两件它做不了的事:把结果按 SKU 铺开,并记下最终的素材 id。

    为什么要按 SKU 铺开而不是只记颜色:导出与提交都是 SKU 行,而
    「这一行用的是哪张主图」在出事之后是第一个要查的东西。只记到颜色一层的话,
    那个问题的答案要靠重新跑一遍映射才能得到,而那时上游可能已经变了。

    同一个颜色下每个 SKU 的图**是一样的**(尺码不换图,§4.9 同一条口径)。
    铺开是为了让「每个 ACTIVE SKU 都拿到了图」成为一次查表而不是一次推理。
    """
    out: dict[str, Any] = {"schema": IMAGE_MAP_SCHEMA_VERSION, "colors": {}}
    for color in active_colors(colors):
        cov = coverage.get(color.variant_id)
        primary = None if cov is None or cov.primary is None else cov.primary.media_asset_id
        extras = [] if cov is None else _extra_ids(cov)
        out["colors"][color.variant_id] = {
            "variant_code": color.variant_code,
            "skus": {
                sku: {"primary": primary, "extras": list(extras)}
                for sku in sorted(color.sku_codes)
            },
        }
    return out


def _extra_ids(cov: VariantCoverage) -> list[str]:
    """附图位的素材 id,按图片集里的排序走。

    主图不进附图位(它已经在 `primary` 那一格),`shared` 排在自有图之后 ——
    §6.5:通用图只能出现在附图位,而排序上让自有图优先是同一条规矩的延伸。
    """
    owned = sorted(
        (i for i in cov.owned if not i.is_primary), key=lambda i: (i.sort_order, i.media_asset_id)
    )
    shared = sorted(cov.shared, key=lambda i: (i.sort_order, i.media_asset_id))
    return [i.media_asset_id for i in (*owned, *shared)]


def primary_of(mapping: Mapping[str, Any] | None, *, variant_id: str, sku: str) -> str | None:
    """从落库的映射里取一行 SKU 的主图。取不到返回 None。

    存在的理由是别处不要再手写三层 `.get()`:那种写法在中间任何一层
    拼错键时都返回 None,而 None 在这一列的语义里是「这个 SKU 没主图」——
    一个拼写错误会被读成一条业务事实。
    """
    if not isinstance(mapping, Mapping):
        return None
    color = (mapping.get("colors") or {}).get(variant_id) or {}
    row = (color.get("skus") or {}).get(sku) or {}
    value = row.get("primary")
    return str(value) if value else None


# ================================================================ 判定


def diff_upstream(
    stored: Mapping[str, Any] | None, current: Mapping[str, Any]
) -> list[StaleChange]:
    """颜色轴逐项比较。返回空列表 = 颜色维没有可解释的变化。

    §6.7:「所有上游版本和指纹仍然有效」= 逐项与当前版本比较**派生**,
    不落 STALE 状态列。所以这个函数只回答「变了什么」,状态由
    `refresh_draft` 一处落库(P4 的约束:过期口径不许在别处再推断一遍)。

    颜色集合比的是**并集**,不是交集 —— 与
    `scope_fingerprint.changed_scopes` 和
    `generation_plan.image_set_stale_scopes` 同一条口径,同一个坑:
    只比交集的话,「某个颜色被停用」会因为那个键在 current 里不存在而被漏掉,
    而它恰恰是最该让草稿过期的一种变化(§7.2「停用颜色 → 重新计算」)。
    """
    if not stored:
        return [
            StaleChange(
                component="upstream_snapshot",
                message="这份草稿建于上游快照落列之前,颜色维是否仍然成立无法证明",
                action="重新生成一次草稿,新草稿会带上颜色维的版本快照",
            )
        ]
    if str(stored.get("schema") or "") != str(current.get("schema") or ""):
        return [
            StaleChange(
                component="upstream_snapshot",
                message="上游快照的形状换了版本,旧快照与当前口径不可比",
                old_value=str(stored.get("schema") or "无"),
                new_value=str(current.get("schema") or "无"),
                action="重新生成草稿,按新形状重新采集上游版本",
            )
        ]

    changes: list[StaleChange] = []
    changes.extend(_shared_changes(stored, current))
    changes.extend(_color_changes(stored, current))
    return changes


def _shared_changes(
    stored: Mapping[str, Any], current: Mapping[str, Any]
) -> list[StaleChange]:
    old = dict(stored.get("shared") or {})
    new = dict(current.get("shared") or {})
    out: list[StaleChange] = []

    old_facts = _fact_map(old.get("fact_versions"))
    new_facts = _fact_map(new.get("fact_versions"))
    if old_facts != new_facts:
        out.append(
            StaleChange(
                component="shared_facts",
                message="SPU 层已确认事实在草稿生成之后被修改",
                fields=_changed_fact_fields(old_facts, new_facts),
                old_value=f"{len(old_facts)} 个事实版本",
                new_value=f"{len(new_facts)} 个事实版本",
                action="重新生成草稿;文案若也过期,先重新生成并批准文案",
            )
        )

    if old.get("sample_fingerprint") != new.get("sample_fingerprint"):
        out.append(
            StaleChange(
                component="shared_sample",
                message="共享作用域的样品素材在草稿生成之后有变动",
                old_value=_short(old.get("sample_fingerprint")),
                new_value=_short(new.get("sample_fingerprint")),
                action="先确认共享事实是否需要重新识别,再重新生成草稿",
            )
        )
    return out


def _color_changes(
    stored: Mapping[str, Any], current: Mapping[str, Any]
) -> list[StaleChange]:
    old = {str(k): dict(v or {}) for k, v in (stored.get("colors") or {}).items()}
    new = {str(k): dict(v or {}) for k, v in (current.get("colors") or {}).items()}
    was_inactive = {str(k): str(v) for k, v in (stored.get("inactive") or {}).items()}
    now_inactive = {str(k): str(v) for k, v in (current.get("inactive") or {}).items()}

    out: list[StaleChange] = []
    for variant in sorted(set(old) | set(new)):
        before, after = old.get(variant), new.get(variant)
        label = _color_label(variant, before or after or {})

        if after is None:
            # 停用与删除对运营是两句不同的话。`inactive` 存在的全部理由
            # 就是让这里说得出是哪一种 —— 只有 `colors` 的快照分不出来
            status = now_inactive.get(variant)
            reason = f"被改成 {status}" if status else "已不存在"
            out.append(
                StaleChange(
                    component="color_set",
                    message=f"颜色 {label} 在草稿生成时是 ACTIVE,现在{reason}",
                    fields=(variant,),
                    old_value="ACTIVE",
                    new_value=status or "已删除",
                    action="重新生成草稿,它的 SKU 行不再参与导出",
                )
            )
            continue

        if before is None:
            prior_status = was_inactive.get(variant)
            message = (
                f"颜色 {label} 从 {prior_status} 转为 ACTIVE"
                if prior_status
                else f"草稿生成之后新建了 ACTIVE 颜色 {label}"
            )
            out.append(
                StaleChange(
                    component="color_set",
                    message=message,
                    fields=(variant,),
                    old_value=prior_status or "不存在",
                    new_value="ACTIVE",
                    action="重新生成草稿,补上这个颜色的 SKU 行与图片映射",
                )
            )
            continue

        out.extend(_one_color_changes(variant, label, before, after))
    return out


def _one_color_changes(
    variant: str, label: str, before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[StaleChange]:
    out: list[StaleChange] = []

    old_facts = _fact_map(before.get("fact_versions"))
    new_facts = _fact_map(after.get("fact_versions"))
    if old_facts != new_facts:
        out.append(
            StaleChange(
                component="color_facts",
                message=f"颜色 {label} 的已确认事实在草稿生成之后被修改",
                fields=_changed_fact_fields(old_facts, new_facts),
                old_value=f"{len(old_facts)} 个事实版本",
                new_value=f"{len(new_facts)} 个事实版本",
                action="重新生成草稿;这个颜色的文案若也过期,先重新批准文案",
            )
        )

    if before.get("sample_fingerprint") != after.get("sample_fingerprint"):
        out.append(
            StaleChange(
                component="color_sample",
                message=f"颜色 {label} 的样品素材在草稿生成之后有变动",
                fields=(variant,),
                old_value=_short(before.get("sample_fingerprint")),
                new_value=_short(after.get("sample_fingerprint")),
                action="先确认这个颜色的事实是否需要重新识别,再重新生成草稿",
            )
        )

    if before.get("plan_fingerprint") != after.get("plan_fingerprint"):
        out.append(
            StaleChange(
                component="color_plan",
                message=f"颜色 {label} 当前生效的生成方案换了",
                fields=(variant,),
                old_value=_short(before.get("plan_fingerprint")),
                new_value=_short(after.get("plan_fingerprint")),
                action="按新方案重新出图并批准图片集,再重新生成草稿",
            )
        )

    # `image_set` 落在快照里但**不在这里比**(模块文档第四项)。
    # 图片集按 SPU 批准,`diff_components` 已经报过那一次换版了。
    return out


def map_problems(
    mapping: Mapping[str, Any] | None, *, colors: Iterable[ColorUpstream]
) -> list[str]:
    """颜色→SKU→图片映射的完整性(§6.7 READY 门禁的颜色维)。

    **只查 SKU 这一轴。** 「这个颜色的主图缺不缺、通用图是不是被违规放进
    主图位」全部归 `image_set_rules.validate_set` —— 那边是 §6.5 的落点,
    在这里再判一次的表现是同一个问题出两条不同措辞的提示,而运营会以为
    是两个问题。

    **给 5-2 接线的人:READY 门禁要同时读这两处**,不是二选一。
    这里查得出「红色有 3 个 ACTIVE SKU 而映射里只有 2 个」,
    查不出「红色一张自有图都没有」;那边反过来。
    """
    wanted = {c.variant_id: c for c in active_colors(colors)}

    if mapping is None:
        return ["草稿没有颜色→SKU→图片映射快照,无法确认每个 ACTIVE SKU 都拿到了图"]
    if str(mapping.get("schema") or "") != IMAGE_MAP_SCHEMA_VERSION:
        return [
            f"映射快照的形状版本是 {mapping.get('schema') or '空'},"
            f"当前口径是 {IMAGE_MAP_SCHEMA_VERSION},不可比"
        ]

    mapped = {str(k): dict(v or {}) for k, v in (mapping.get("colors") or {}).items()}
    problems: list[str] = []

    for variant in sorted(set(mapped) - set(wanted)):
        label = _color_label(variant, mapped[variant])
        problems.append(f"映射里还留着颜色 {label},它已经不是 ACTIVE")

    for variant, color in sorted(wanted.items()):
        label = _color_label(variant, {"variant_code": color.variant_code})
        if variant not in mapped:
            problems.append(f"ACTIVE 颜色 {label} 不在映射里")
            continue
        skus = {str(k) for k in (mapped[variant].get("skus") or {})}
        expected = {str(s) for s in color.sku_codes}
        if not expected:
            problems.append(f"ACTIVE 颜色 {label} 一个 SKU 都没有")
            continue
        for missing in sorted(expected - skus):
            problems.append(f"颜色 {label} 的 SKU {missing} 不在映射里")
        for extra in sorted(skus - expected):
            problems.append(f"映射里颜色 {label} 的 SKU {extra} 已经不存在")

    return problems


def ready_problems(
    mapping: Mapping[str, Any] | None,
    *,
    colors: Iterable[ColorUpstream],
    image_set: SetView,
    required_angles: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    """READY 的颜色维合取门禁:图片集规则 **AND** SKU 映射完整性。

    `map_problems` 刻意不判主图,`validate_set` 刻意不知道草稿映射里铺了哪些
    SKU。5-2 只调用本函数,不在服务层手写两次调用；这样少读任一侧都会让本函数
    的纯测试变红,而不是等平台驳回后才发现。
    """
    kept = active_colors(colors)
    violations = validate_set(
        image_set,
        variant_ids=(color.variant_id for color in kept),
        required_angles=required_angles,
    )
    return [
        *(f"{problem.code.value}: {problem.message}" for problem in violations),
        *map_problems(mapping, colors=kept),
    ]


# ================================================================ 小工具


def _short(value: Any) -> str:
    """指纹只显示前 8 位。整串 64 位在提示里没有人读,而它会把那句话挤没。"""
    text = str(value or "")
    return f"{text[:8]}…" if text else "无"


def _fact_snapshot(values: Mapping[str, str]) -> dict[str, str]:
    """把事实版本映射归一成稳定、可 JSON round-trip 的形状。"""
    return {str(version): str(values[version]) for version in sorted(values, key=str)}


def _fact_map(value: Any) -> dict[str, str]:
    """读取 v1 事实映射。非映射形状按空处理,由 schema 版本负责隔离旧形状。"""
    if not isinstance(value, Mapping):
        return {}
    return {str(version): str(field_key) for version, field_key in value.items()}


def _changed_fact_fields(
    old: Mapping[str, str], new: Mapping[str, str]
) -> tuple[str, ...]:
    changed_versions = {
        version for version in set(old) | set(new) if old.get(version) != new.get(version)
    }
    return tuple(
        sorted(
            {
                field_key
                for version in changed_versions
                for field_key in (old.get(version), new.get(version))
                if field_key
            }
        )
    )


def _color_label(variant_id: str, payload: Mapping[str, Any]) -> str:
    """给运营看的颜色标识:有 `variant_code` 就用它,没有就退回短 id。

    **不用 `display_name`。** 那一列今天恒为默认值(`audit_column_writers`
    台账上记着,还款日阶段 5),拿它当标识会让每个颜色都显示成空。
    """
    code = str(payload.get("variant_code") or "").strip()
    return code or f"{variant_id[:8]}…"


__all__ = [
    "COMPONENTS",
    "IMAGE_MAP_SCHEMA_VERSION",
    "UPSTREAM_SCHEMA_VERSION",
    "ColorUpstream",
    "active_colors",
    "build_color_sku_image_map",
    "build_upstream_versions",
    "diff_upstream",
    "map_problems",
    "primary_of",
    "ready_problems",
]
