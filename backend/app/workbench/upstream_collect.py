"""草稿颜色维上游快照的**取数**(阶段 5 批次 5-2 的接线层)。

判定全部在 `workbench/upstream_snapshot`(零依赖、可穷举)。这个模块只做
一件事:把库里的行翻译成那一层的入参投影 `ColorUpstream`。

分成两个模块而不是把查询写进判定层,与 `generation_plan_service.to_view`、
`scope_fingerprint.AssetView`、`image_set_rules.ItemView` 是同一条规矩:
**判定要能在没有数据库的机器上被穷举**,而只要它能查库,那个穷举测试
就没人会写。

## 一次取数,两列同源

`upstream_versions` 与 `color_sku_image_map` 是同一批颜色的两种投影。
本模块只暴露一个入口 `collect()`,两列都从它的返回值构造 ——
分成两次取数的话,「这两列说的是不是同一组颜色」就没有任何东西保证了,
而它们恰恰要被 READY 门禁**同时**读(§6.7 / AC-18)。

同一个理由让 `ready_problems()` 用的 `colors` 也来自这里:检查用的颜色集
和构建用的颜色集只要有一次不同源,就会凭空造出「SKU 不在映射里」。

## 三处取数,一处都不在按颜色的循环里

    color_variants                    §4.3 的颜色身份 + `sellable_status`
    generation_plan_service.list_plans 每个颜色当前生效的方案指纹与要求角度
    attr_service.scope_fingerprints_for 每个作用域的样品指纹

**样品指纹走 `scope_fingerprints_for()`,不自己拼一遍取数 + `fingerprints()`。**
那是事实侧的唯一取数入口(§5.1),而这里恰好是它的第四个消费者。
自己抄一行的表现最安静:写入侧按 A 集合算指纹、读取侧按 B 集合算,
两边都不报错,而草稿会说一个从没变过的颜色「样品有变动」。
`tests/pure/test_a45_batch14_21_facts_stale.py` 钉着 `fingerprints()`
在 `app/` 下只有两个调用点,所以抄一行会当场变红。

SKU 全集与事实版本**不额外查库**:两者都从调用方已经组装好的
`CanonicalProduct` 上读。这不只是省一次查询 —— 草稿正是按那份快照构建的,
另查一次就等于给「这份草稿用的是哪些事实」造第二个答案,而两个答案漂移时
表现是草稿刚生成就说自己过期。

`tests/pure/test_workbench_query_budget.py` 那条棘轮按 AST 看循环深度,
所以这里的取数必须留在函数顶层,不许挪进 `for color in ...`。

## SPU 身份走外键,**不从 `spu` 字符串码反查**

`product.spu_id` 是权威(§4.4),`products.spu` 是它的反规范化副本。
`select(Spu).where(Spu.spu_code == product.spu)` 是被 §3.39 点名禁掉的形状:
那个码可能已经因改名而与 SPU 行断开,反查出来的是**另一个款**的颜色集,
而错的外键比空的外键难发现得多 —— 表现是这份草稿按别人的颜色去铺 SKU。
`tests/pure/test_a45_batch14_26_decisions.py` 钉着这条。

`spu_id` 为空的存量商品行因此拿不到颜色轴。那是 §4.4 那笔外键欠账的
如实呈现,不是本模块该兜的底。

## 身份只有一份:`color_variants.id`

颜色的身份是 `products.color_variant_id`(经 `variant_key.identity_of`),
图片项的变体标签与属性 owner_id 在迁移 0046 之后也是它。三者同源是
`map_problems` 能成立的前提 —— 任意一处回落到别的口径,表现是每个 ACTIVE
颜色都报「一个 SKU 都没有」,而错因看上去像是运营没建 SKU。

**没回填 `color_variant_id` 的存量商品行因此不会挂到任何颜色上。**
那不是本模块的兜底对象:兜过去(比如按颜色名再猜一次)会让 §3.17 那条
「身份不许由可编辑字段推导」重新长出来。它表现为一条会被运营看见的
READY 问题,而 0046 已经把库里的行回填齐了。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.attributes import scope_fingerprint
from app.attributes import service as attr_service
from app.attributes.contracts import CanonicalProduct
from app.listings import image_set_service
from app.listings.image_set_rules import SetView
from app.models.product import Product
from app.models.spu import ColorVariant
from app.services import generation_plan_service
from app.workbench.upstream_snapshot import ColorUpstream
from app.workflows import generation_plan as gp


@dataclass(frozen=True)
class UpstreamInputs:
    """`collect()` 的产物:两个 builder 与 READY 门禁的**共同**入参。

    做成一个 dataclass 而不是返回一个元组,是因为它有五个字段而调用点有三处;
    元组的第 4 位与第 5 位调换在 Python 里不报错,而它们分别是「图片集」和
    「每个颜色要求的角度」—— 换过来之后 §6.5 的角度检查会静默失效。
    """

    colors: tuple[ColorUpstream, ...] = ()
    shared_fact_versions: dict[str, str] = field(default_factory=dict)
    shared_sample_fingerprint: str | None = None
    #: 已解析的那一版图片集。`build_draft` 走到这里时它一定存在
    #: (`_current_draft_data` 已经把「没有已批准图片集」拦成前置不满足)
    image_set: SetView | None = None
    #: `{颜色 id: [角度]}`,来自该颜色当前生效的方案。
    #: **没有生效方案的颜色不出现在这里**,与 `required_angles_for` 同口径:
    #: 空列表会被读成「这个颜色不要求任何角度」,于是没配方案等于免检
    required_angles: dict[str, list[str]] = field(default_factory=dict)


def collect(
    session: Session,
    product: Product,
    *,
    canonical: CanonicalProduct,
    image_set_row: object | None = None,
) -> UpstreamInputs:
    """按当前上游采集颜色维的全部引用。

    `canonical` 由调用方传进来而不是在这里重新组装:见模块文档第二节。
    `image_set_row` 同理 —— `_current_draft_data` 刚解析过一次,再解析一次
    有可能拿到**另一版**(两次调用之间有人批准了新版),而草稿的
    `image_set_id` 写的是前一次的结果。
    """
    if product.spu_id is None:
        # 这一行商品还没有归属外键(§4.4 的欠账,存量数据上真的走得到)。
        #
        # **给空颜色集,不猜。** 空集在判定层的含义是「这个 SPU 没有颜色轴」,
        # 与 `validate_set(variant_ids=())`、`image_set_service._required_angles`
        # 翻不出 SPU 时返回 `{}` 是同一条口径:少一个维度的检查必须能被显式表达。
        #
        # 注意它与 `upstream_versions = NULL` **不是**同一件事:NULL 是
        # 「没算过」,这里是「算过了,这个 SPU 在颜色轴上没有东西要查」。
        return UpstreamInputs(image_set=_set_view(session, image_set_row))

    variants = list(
        session.scalars(
            select(ColorVariant)
            .where(ColorVariant.spu_id == product.spu_id)
            .order_by(ColorVariant.id)
        )
    )
    plans = [
        generation_plan_service.to_view(row)
        for row in generation_plan_service.list_plans(session, product.spu_id)
    ]

    variant_ids = [str(row.id) for row in variants]
    fingerprints = attr_service.scope_fingerprints_for(session, product)
    plan_fingerprints = gp.effective_fingerprints(plans, variant_ids)
    facts_by_variant = _variant_fact_versions(canonical)
    skus_by_variant = _sku_codes(canonical)
    angles = {
        variant: sorted(gp.required_angles(chosen.angles))
        for variant in variant_ids
        if (chosen := gp.resolve_plan(plans, variant)) is not None and chosen.angles
    }

    colors = tuple(
        ColorUpstream(
            variant_id=str(row.id),
            variant_code=row.variant_code or "",
            sellable_status=row.sellable_status,
            fact_versions=facts_by_variant.get(str(row.id), {}),
            sample_fingerprint=fingerprints.get(str(row.id)),
            plan_fingerprint=plan_fingerprints.get(str(row.id)),
            # 图片集今天是**按 SPU** 批准的,所以每个颜色引用的是同一版。
            # 记进快照的理由是 AC-19 要「可解释的上游版本引用」——
            # 这一格回答的是「这个颜色的映射是从哪一版图片集铺出来的」。
            #
            # **它不参与 `diff_upstream` 的比较**(判定层里没有那一段):
            # 那个轴归 `stale.diff_components` 报,与 `copies` / `spec_version` /
            # `mapping_version` 三项同一条分工(§3.50 D3)。两边都比的话,
            # 重新批准一次图片集会出 1 + N 条提示,而运营看到的
            # 「这份草稿有 4 个上游变了」从此不可信。
            image_set_id=None if image_set_row is None else str(image_set_row.id),
            image_set_version=(
                None if image_set_row is None else int(image_set_row.version)
            ),
            sku_codes=skus_by_variant.get(str(row.id), ()),
        )
        for row in variants
    )
    return UpstreamInputs(
        colors=colors,
        shared_fact_versions=_shared_fact_versions(canonical),
        shared_sample_fingerprint=fingerprints.get(scope_fingerprint.SHARED),
        image_set=_set_view(session, image_set_row),
        required_angles=angles,
    )


def _set_view(session: Session, image_set_row: object | None) -> SetView | None:
    if image_set_row is None:
        return None
    return image_set_service.to_view(session, image_set_row)


def _shared_fact_versions(canonical: CanonicalProduct) -> dict[str, str]:
    """SPU 层已确认事实的 `{version_id: field_key}`。

    **只取 CONFIRMED**(AC-10):把 SUGGESTED 也算进去的话,模型每猜一次
    草稿就过期一次,而运营从没确认过那个值。`confirmed_attrs()` 是同一条
    口径在文案侧的落点,这里不重新写一遍判据,只是它作用在 SPU 那一层。
    """
    return {
        value.version_id: name
        for name, value in canonical.confirmed_attrs().items()
        if value.version_id
    }


def _variant_fact_versions(canonical: CanonicalProduct) -> dict[str, dict[str, str]]:
    """每个颜色 VARIANT 层已确认事实的 `{version_id: field_key}`。

    键是 `CanonicalVariant.variant_id`,也就是 `variant_key.identity_of()`
    的结果 —— 与 `color_variants.id` 同源,见模块文档最后一节。
    """
    return {
        variant.variant_id: {
            value.version_id: name
            for name, value in variant.attrs.items()
            if value.is_confirmed and value.version_id
        }
        for variant in canonical.variants
    }


def _sku_codes(canonical: CanonicalProduct) -> dict[str, tuple[str, ...]]:
    """每个颜色下 `products.sku` 的全集。

    §3.51 第二节把采集口径定死:同一颜色下**全部** SKU,不按
    `Product.status` / 库存 / 条码 / 价格再过滤一次。那些是 READY 的其它门禁,
    在这里再筛一道的表现是映射里少了几行而 `map_problems` 说「不在映射里」——
    一个已经被别的门禁拦下的问题,换了一句指向错误对象的话再报一次。
    """
    out: dict[str, list[str]] = {}
    for sku in canonical.skus:
        out.setdefault(sku.variant_id, []).append(sku.sku)
    return {variant: tuple(sorted(codes)) for variant, codes in out.items()}


__all__ = ["UpstreamInputs", "collect"]
