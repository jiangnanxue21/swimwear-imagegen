"""样品完整度门禁的单点派生(PRD §6.2)。**纯判定,零三方依赖。**

「这件商品的样品够不够,能不能往下走」——今天这个问题由
`workbench/service.py` 的一行回答:

    usable_roles = frozenset(r.role for r in usable if r.role)

它不问角色是谁标的,也不问那张图是不是**关于本商品**的证据。
门禁今天是对的,但它对得没有道理 —— 它至今没漏,靠的是两件**尚未发生**的事,
而两件都写在代码注释里当成计划(见下面第二段)。

## 为什么要两个条件,不是一个

**只筛角色来源挡不住 AI 图。** `ingest()` 的去重键是 `(product_id, sha256)`,
`_fill_missing_role()` 允许在去重命中时补一次角色,而 AI 影子行正好是
`role=None` + `role_source=UNSET` —— 运营把生成好的图下载下来、当样品传给
另一个颜色(这条流水线上会发生的事),命中的是那条 AI 行,于是一条
`source=AI_GENERATED` 的记录拿到了 `role_source=HUMAN`。

**只筛来源挡不住模型猜的角色。** `media/service.py` 里那句注释
(「留 UNSET,由 M3 的角色识别或人工来定」)是一个计划:M3 落地那天,
每一张 AI 候选图都会拿到一个模型给的角色。那时**没有任何测试会变红** ——
代码逐字相同,变的只是库里的数据。

所以判定要求**同时**满足:

    这张图是关于本商品的证据      → 复用 §5.1 那一个判定,不重写
    这个角色是人定的              → §6.2 的 role_source 白名单

## 落码时和 §6.2 原文对不上的三处

**一、`CONFIRMED` 不是 `RoleSource` 的成员。**§6.2 写
`role_source ∈ {HUMAN, CONFIRMED}`,而枚举只有 `HUMAN / RULE / MODEL / UNSET`。
和 batch14-8 撞上的 `MODEL_REFERENCE` 是同一种东西:规格里有、枚举里没有。

处理是**保留这个字符串**而不是删掉它。删掉的话,将来「人工确认模型建议」
这个动作落地时,没有人会记得规格写过它,于是那条路径会被重新发明一次 ——
而重新发明的那一版多半会直接放行 `MODEL`。守卫钉住「今天它确实还不是成员」,
成员那天守卫会红,那时该做的是把它加进枚举、删掉守卫。

**二、`RULE` 不在白名单里,尽管它不是模型猜的。**§6.2 给的是白名单不是排除法。
两种写法今天等价(见第三条),分歧在将来:排除法之下,新增任何来源取值都
默认合格,而新增取值时没有人会想起这道门禁。fail closed。

规则推断(文件名、上传位)确实比模型可靠,但它可靠的是**批量导入那一刻**,
不是「有人看过这张图并且认可它是正面图」。门禁问的是后一件事。

**三、白名单今天一个取值都拦不住。**全仓只有两处写 `role_source`:
`shadow_from_product_asset` 写 `HUMAN`、`shadow_from_candidate` 写 `UNSET`
且角色为空。`RULE` 与 `MODEL` **一个写入点都没有**。所以这条收紧今天
不改变任何一件商品的判定 —— 把它当成「反正没影响」而省掉,等于把落地时机
排在 M3 之后,而那时要拦的数据已经在库里了。

## 一处放宽,它是真的在假阻断

§6.2 原文是「至少一张 PRODUCT_FRONT/FLAT_LAY」,而 `flow.REQUIRED_ROLES`
只有 `PRODUCT_FRONT`。旧枚举 `GARMENT_CUTOUT` 映射成 `FLAT_LAY`
(`media/mapping.py` 自注有损),于是**只有透明底商品图的商品被永久判
「缺少正面图」**,而运营解除它的唯一办法是把平铺图改标成正面图。

这正是 `flow.py` 那段注释写过的道理的另一半:门禁定得不对,人会去改数据
来迁就门禁 —— 而被改坏的是素材库的角色标注,下游每一处按角色取图的地方
跟着错。所以必备角色写成**满足组**:组内任一角色即满足。

## 双作用域

与 `attributes/scope_fingerprint.py` 同一套口径,两个模块对「作用域」这个词
必须是同一个意思:

    gate_roles(assets)                  该 SPU 全部证据素材
    variant_gate_roles(assets, variant) 该颜色的证据素材子集

通用素材(`color_variant_id` 为空)**只进 SPU 作用域,不进任何颜色子集**。
理由在这里比在指纹那边更直白:一张没有归属的图证明不了颜色 A 有正面照。

颜色那一半今天**接不上线** —— `color_variant_id` 是阶段 2 的归属外键本身,
表上只有 `variant_hint`,而 §4.8 明写它是「识别建议位」。拿 hint 分子集
等于让模型猜出来的值决定运营能不能往下走,**而本模块收紧 role 口径
要禁的正是这件事**。在同一道门禁上违反两次没有道理。欠账由
`test_the_variant_scope_cannot_be_wired_yet_and_here_is_exactly_why` 记账。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.media.evidence_rules import asset_is_extraction_input

#: §6.2 的角色来源白名单。**白名单,不是排除法**,理由见模块文档第二条。
#:
#: `CONFIRMED` 今天不是 `RoleSource` 的成员(模块文档第一条)。留着它是
#: 为了让「人工确认模型建议」落地时有个能接住的取值,而不是让下一个人
#: 从零发明一遍。守卫钉着「今天它确实还不是成员」这个凑合状态。
CONFIRMED_ROLE_SOURCES: frozenset[str] = frozenset({"HUMAN", "CONFIRMED"})

#: 必备角色的**满足组**:组内任一角色即满足该组(§6.2 的 `A/B` 写法)。
#:
#: 写成组而不是单个角色,修的是一条真的在假阻断的规则 —— 见模块文档
#: 「一处放宽」。组内顺序有意义:`ref`(前端跳转依据)取第一个,
#: 所以最典型的那个角色排前面。
REQUIRED_ROLE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("PRODUCT_FRONT", "FLAT_LAY"),
)

#: 角色 -> 给运营看的一句话。**全仓唯一一份。**
#:
#: 它从 `workbench/flow.py` 搬到这里,因为门禁的提示语要拼「正面图或平铺图」,
#: 而角色本身是素材层的概念。留两份表的下场是:改了一处,判定说「平铺图」、
#: 按钮说 `FLAT_LAY`,而两处都不会报错。
ROLE_LABELS: Mapping[str, str] = {
    "PRODUCT_FRONT": "正面图",
    "PRODUCT_BACK": "背面图",
    "DETAIL": "细节图",
    "MODEL_FRONT": "模特正面图",
    "MODEL_BACK": "模特背面图",
    "SIZE_CHART": "尺码表",
    "FLAT_LAY": "平铺图",
    "PACKAGING": "包装图",
    "OTHER": "其它",
}


def _text_or_none(value: Any) -> str | None:
    """枚举成员 / 字符串 / None 统一成一个可比较的值。

    与 `scope_fingerprint._text_or_none` 同一份口径:空串和 None 都是
    「没有值」。两处必须一致 —— 不一致的表现是同一条素材在指纹里算「未归属」、
    在门禁里算「归属于空字符串那个颜色」。
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def role_label(role: str) -> str:
    """认不出的角色如实回显原值,不写「其它」。

    回显一个陌生的码,下一步是去查它是谁写进来的;写「其它」把这条线索抹掉。
    """
    return ROLE_LABELS.get(role, role)


def group_label(group: Iterable[str]) -> str:
    """一个满足组的中文,例如 `正面图或平铺图`。

    **按传入顺序拼**,不按集合迭代序。顺序飘的表现是:同一件商品刷新两次,
    提示语里两个角色先后颠倒 —— 谁都不会把这件事报成 bug,只会觉得
    这个界面有点飘。
    """
    return "或".join(role_label(role) for role in group)


def role_source_is_confirmed(role_source: Any) -> bool:
    """这个角色是不是「人定的」。§6.2 白名单的判定。

    空值(库里 NULL、存量行)**不算**已确认。给它开缝的话,存量素材会
    一次性全部合格,而这道门禁存在的全部理由就是不认那一批。
    """
    return _text_or_none(role_source) in CONFIRMED_ROLE_SOURCES


def counts_for_gate(asset: Any, *, imported_url_trusted: bool = False) -> bool:
    """这条素材算不算「一张够格的样品」。**门禁的最小判定单元。**

    三个条件缺一不可:

        是本商品的证据    复用 §5.1 的 `asset_is_extraction_input` —— 它同时
                          吃掉了「AI 图」「状态不是 READY」「参考素材」三类。
                          **不重写一遍条件**:重写第二套正是 D3 描述的漂移,
                          而漂移在这里的表现是识别用一套图、门禁认另一套
        角色是人定的      §6.2 白名单,模块文档第二段说明了为什么它不够、也
                          为什么它不能少
        角色不是空的      `role=None` 的行没有角色可谈。它落在这里而不是靠
                          调用方过滤,是因为 `_fill_missing_role` 那条路径
                          正好产出「角色空、来源 HUMAN」的组合
    """
    if not asset_is_extraction_input(asset, imported_url_trusted=imported_url_trusted):
        return False
    if not role_source_is_confirmed(getattr(asset, "role_source", None)):
        return False
    return _text_or_none(getattr(asset, "role", None)) is not None


def _in_variant(asset: Any, variant_id: str) -> bool:
    """这条素材归属于这个颜色。

    通用素材(归属为空)**不在任何颜色子集里**,见模块文档「双作用域」。
    """
    return _text_or_none(getattr(asset, "color_variant_id", None)) == variant_id


def _roles_of(assets: Iterable[Any], *, imported_url_trusted: bool) -> frozenset[str]:
    roles: set[str] = set()
    for asset in assets:
        if not counts_for_gate(asset, imported_url_trusted=imported_url_trusted):
            continue
        role = _text_or_none(getattr(asset, "role", None))
        if role is not None:
            roles.add(role)
    return frozenset(roles)


def gate_roles(
    assets: Iterable[Any], *, imported_url_trusted: bool = False
) -> frozenset[str]:
    """SPU 作用域:该商品全部够格样品的角色集合。

    **这不是 `usable_roles`。** 那一份回答「素材库里有哪些角色的图」,
    这一份回答「哪些角色的图够格进门禁」。两份集合放在同一个
    `MaterialFacts` 上是刻意的:提醒类问题(缺背面图 / 细节图)要读前者 ——
    一张模型猜出角色的背面图,说「没有背面图」是假的,而假提醒会训练人
    忽略这一列。
    """
    return _roles_of(assets, imported_url_trusted=imported_url_trusted)


def variant_gate_roles(
    assets: Iterable[Any], variant_id: str, *, imported_url_trusted: bool = False
) -> frozenset[str]:
    """颜色作用域:该颜色够格样品的角色集合。

    **A45-batch14-21 改掉了这里的一句陈旧理由。** 原文写的是「今天没有
    调用点,因为归属外键还没落库」—— 外键在迁移 0037 就落了,而这个函数
    从 14-15 起就在 `workbench/service._material_facts` 里被逐颜色调着。

    §3.33:一条过期的理由比没有理由更糟。照着它去查的人会发现列就在那儿,
    然后不知道该信文档还是信代码。
    """
    return _roles_of(
        (a for a in assets if _in_variant(a, variant_id)),
        imported_url_trusted=imported_url_trusted,
    )


def missing_role_groups(roles: Iterable[str]) -> tuple[tuple[str, ...], ...]:
    """哪些满足组还没被满足。**门禁的结论。**

    空元组 = 样品完整。返回组而不是角色名,是因为提示语必须说
    「正面图**或**平铺图」——只报第一个角色的话,运营为了消掉那句话
    会把平铺图改标成正面图,而那正是本批要修的坏循环。
    """
    have = {r for r in roles}
    return tuple(group for group in REQUIRED_ROLE_GROUPS if not (set(group) & have))


def _confirmable(
    assets: Iterable[Any], satisfied: Iterable[str], *, imported_url_trusted: bool
) -> tuple[str, ...]:
    wanted = {role for group in missing_role_groups(satisfied) for role in group}
    found: set[str] = set()
    for asset in assets:
        role = _text_or_none(getattr(asset, "role", None))
        if role not in wanted:
            continue
        if role_source_is_confirmed(getattr(asset, "role_source", None)):
            continue
        if not asset_is_extraction_input(
            asset, imported_url_trusted=imported_url_trusted
        ):
            continue
        found.add(role)
    # **排序不是洁癖。** 集合迭代序由字符串哈希决定,而字符串哈希每个进程
    # 都不一样 —— 表现是同一件商品刷新两次,提示语里的角色先后颠倒,
    # `ref`(前端跳转依据)跟着换一个。
    return tuple(sorted(found))


def confirmable_roles(
    assets: Iterable[Any], *, imported_url_trusted: bool = False
) -> tuple[str, ...]:
    """「确认一下角色就能解开这道门禁」的角色,按名字排序。

    进这个集合要同时满足:是本商品的证据、角色**不是**人定的、
    而且那个角色属于某个**还没被满足**的组。

    最后一条让这个集合有一条可断言的性质:**它非空就意味着门禁没过。**
    少了它,一件已经有人工正面图的商品会因为多了一张模型猜的平铺图而
    显示「有素材待确认角色」——一条没有下一步的待办。

    返回角色名而不是素材条数:运营在素材页要找的是「哪几张图要看一眼」,
    而角色是他在那个页面上唯一能筛的维度。`role` 为空的行不在这里 ——
    那种行没有角色可确认,它需要的是有人去**指定**一个角色,
    由缺图那条阻断负责说这句话。
    """
    return _confirmable(
        assets,
        gate_roles(assets, imported_url_trusted=imported_url_trusted),
        imported_url_trusted=imported_url_trusted,
    )


def variant_confirmable_roles(
    assets: Iterable[Any], variant_id: str, *, imported_url_trusted: bool = False
) -> tuple[str, ...]:
    """颜色作用域的同一件事。**今天确实没有调用点,但理由和上面那个不同。**

    上面那个的"没有调用点"在 14-15 就不成立了(见它的文档)。这一个还成立,
    而原因不是缺列,是**缺界面**:"该颜色还差哪几个角色、哪几张现有素材
    可以补上去"这句话要显示在按颜色上传的界面里,而那是阶段 2 的剩余项。
    算出来没有地方显示的清单,接了也只是多一个没人读的返回值(§3.32)。

    判定本身已经穷举验过,那个界面落地的那天不需要重新证明它。
    """
    subset = [a for a in assets if _in_variant(a, variant_id)]
    return _confirmable(
        subset,
        variant_gate_roles(assets, variant_id, imported_url_trusted=imported_url_trusted),
        imported_url_trusted=imported_url_trusted,
    )
