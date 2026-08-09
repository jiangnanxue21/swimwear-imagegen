"""颜色维聚合的取数(阶段 6 批次 6-3 / AC-15)。

## 它在分层里的位置

    upstream_collect   采集颜色维的上游引用(已有,batch19)
    image_set_rules    算覆盖率(已有,§6.5)
    **本模块**         把上面这些翻译成 `color_flow.ColorView`
    color_flow         纯判定(6-1)

判定层刻意不查库(`test_the_module_does_not_recompute_any_upstream_rule`
钉着它只 import `flow`),所以翻译这一跳必须有人做 —— 就是这里。

## 为什么不放进 `workbench/service.py`

那个文件已经 1600 行,而这一跳有一个很容易被忽略的性质:**它是唯一决定
「向导上那些颜色状态从哪儿来」的地方**。混进一个巨大的文件里,下一个人
要改颜色维口径时找不到它,于是就近再写一份 —— 而那正是 §3.43 那一族的
起点。单独一个模块,`color_flow` 的输入就只有一个来源。

## 一条边界:**这是活视图,不是草稿快照**

向导问的是「现在这个颜色缺什么」,所以这里读当前上游。
草稿里那份 `color_sku_image_map` 回答的是另一个问题(「这份草稿导出时
用的是哪些图」,AC-19),两者不该合并:

    活视图相同、快照不同   -> 草稿过期了,该重新生成
    活视图不同、快照相同   -> 上游动了,而草稿还没跟上

合并之后这两种情况就分不出来了,而它们要做的事相反。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.attributes.registry import REGISTRY
from app.channels import generic
from app.core.enums import Audience, MediaStatus, OwnerType
from app.listings import image_set_rules
from app.models.listing_copy import ListingCopy
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.workbench import color_flow, upstream_collect, upstream_snapshot
from app.workbench.color_flow import ColorView, SpuColorRollup


def _required_variant_fields(audience: Audience | None) -> frozenset[str]:
    """这个受众下,VARIANT 层有哪些字段是必需的。

    受众为 None(未确认)时按**并集**取:少报必需字段的表现是向导说
    「这个颜色齐了」,而确认受众之后它突然缺两项 —— 运营会以为是系统
    刚刚弄丢了东西。多报的代价只是早一点看见要填什么。
    """
    out = set()
    for name, spec in REGISTRY.items():
        if spec.owner_type is not OwnerType.VARIANT:
            continue
        if not spec.required_for:
            continue
        if audience is None or audience in spec.required_for:
            out.add(name)
    return frozenset(out)


def build_color_views(
    session: Session, product: Product, *, canonical
) -> tuple[ColorView, ...]:
    """把当前上游翻译成颜色子态的输入。

    每一项都注明它从哪儿来 —— 这一跳最容易出的错不是算错,是**从第二个
    地方取数**:同一个"缺不缺图"在这里按 A 口径算、在 READY 门禁按 B 口径算,
    于是向导说齐了、门禁说缺,而两边都不报错(§3.54 的 AC-19 就是这个形状)。
    """
    # 图片集要显式解析后传进去,与 `build_draft` 同一条路 —— 不传的话
    # `collect` 拿不到那一版,于是每个颜色都被算成"没有图",而真相是
    # "这一步还没轮到"。两者要运营做的事相反
    from app.workbench import service as _wb

    image_set_row = _wb._current_image_set(session, product.spu)
    inputs = upstream_collect.collect(
        session, product, canonical=canonical, image_set_row=image_set_row
    )
    cov = image_set_rules.coverage(
        inputs.image_set,
        variant_ids=(c.variant_id for c in upstream_snapshot.active_colors(inputs.colors)),
        required_angles=inputs.required_angles,
    ) if inputs.image_set is not None else {}

    # 图片映射:与草稿落库那一列**同一个 builder**(AC-19 的那一份)。
    # 这里是活视图(见模块文档),但口径必须是同一条 —— 否则"缺主图"
    # 在向导和导出预览上会是两个答案
    image_map = upstream_snapshot.build_color_sku_image_map(
        colors=inputs.colors, coverage=cov
    )
    by_colour = (image_map.get("colors") or {})

    # 每个颜色的样品数:按 color_variant_id 归集,不按 SPU 摊平。
    # 摊平的表现是九色 SPU 里某个颜色一张图没有,而向导说"素材齐了"
    samples: dict[str, int] = {}
    for asset in session.scalars(
        select(MediaAsset).where(
            MediaAsset.spu_id == product.spu_id,
            MediaAsset.status != MediaStatus.DELETED.value,
        )
    ):
        if asset.color_variant_id is None:
            continue
        key = str(asset.color_variant_id)
        samples[key] = samples.get(key, 0) + 1

    # 每个颜色有没有文案:0051 之后文案按颜色分槽,所以这是一次按列聚合,
    # 不是"这个 SPU 有没有文案"
    copies = {
        str(row.color_variant_id)
        for row in session.scalars(
            select(ListingCopy).where(
                ListingCopy.spu == product.spu,
                ListingCopy.channel == generic.CHANNEL,
                ListingCopy.site == generic.SITE,
                ListingCopy.locale == generic.LOCALE,
            )
        )
        if row.color_variant_id
    }

    # 受众取商品行本身,不取 canonical —— `CanonicalProduct` 是**平台无关的
    # 商品事实**(§5.5),它身上没有受众这个字段。向一个不存在的属性取值
    # 只会在运行时炸,而炸的位置离原因很远
    try:
        audience = Audience(product.audience) if product.audience else None
    except ValueError:
        # 存量行可能带着一个已经废弃的取值。判"未确认"而不是抛:未确认时
        # 必需字段按并集取(见 `_required_variant_fields`),多报的代价只是
        # 早一点看见要填什么
        audience = None
    required = _required_variant_fields(audience)

    views: list[ColorView] = []
    for colour in inputs.colors:
        confirmed = frozenset(colour.fact_versions.values())
        cell = by_colour.get(colour.variant_id) or {}
        skus = cell.get("skus") or {}
        missing_primary = frozenset(
            sku for sku, v in skus.items() if not (v or {}).get("primary")
        )
        c = cov.get(colour.variant_id)
        angles = (inputs.required_angles or {}).get(colour.variant_id)
        views.append(
            ColorView(
                variant_id=colour.variant_id,
                variant_code=colour.variant_code,
                sellable_status=colour.sellable_status,
                is_active=colour.is_active,
                skus=colour.sku_codes,
                sample_count=samples.get(colour.variant_id, 0),
                confirmed_fields=confirmed,
                missing_required_fields=required - confirmed,
                # 冲突要另查一次事实表才知道,而 `ColorUpstream` 只带
                # CONFIRMED 的版本 —— 本批不猜:留空表示"这里不报冲突",
                # 属性页仍然会报。硬从"必需字段缺失"推冲突会把两种完全
                # 不同的情况显示成同一句话
                conflicting_fields=frozenset(),
                # `required_angles` 里没有这个颜色 = 它没有生效方案
                # (与 `required_angles_for` 同口径:空列表会被读成"不要求
                # 任何角度",于是没配方案等于免检)
                has_plan=angles is not None,
                plan_is_colour_level=bool(colour.plan_fingerprint),
                # 没有已批准图片集时三项全是"没算过"(None / 空),
                # 而**不是**"缺主图":还没轮到做图片集与做了但缺图,
                # 要运营做的事完全不同。这是 batch25 那条
                # 「没算过 ≠ 没做」在更外一层的同一件事
                owned_image_count=len(c.owned) if c is not None else None,
                missing_angles=c.missing_angles if c is not None else frozenset(),
                skus_missing_primary=(
                    missing_primary if (c is not None and skus) else None
                ),
                has_copy=colour.variant_id in copies,
                copy_stale=False,
            )
        )
    return tuple(views)


def rollup(session: Session, product: Product, *, canonical) -> SpuColorRollup:
    """向导的颜色维汇总。**`color_flow` 的唯一生产调用点。**"""
    return color_flow.evaluate_colors(build_color_views(session, product, canonical=canonical))


def serialize_rollup(result: SpuColorRollup) -> dict:
    """汇总 -> 接口出参。

    `blocking_summary` 在这里下发而不是让前端拼:同一句话会出现在向导、
    列表页悬浮提示和批量跳过理由三个地方,拼三次会分叉成三种说法。
    """
    return {
        "active_count": result.active_count,
        "blocking_codes": list(result.blocking_codes),
        "blocking_summary": color_flow.blocking_summary(result),
        "worst_by_step": {
            step.value: state.value for step, state in result.worst_by_step.items()
        },
        "colors": [
            {
                "variant_id": c.variant_id,
                "variant_code": c.variant_code,
                "is_active": c.is_active,
                "current_step": c.current_step.value if c.current_step else None,
                "is_blocking": c.is_blocking,
                "steps": [
                    {"step": s.step.value, "state": s.state.value, "detail": s.detail}
                    for s in c.steps
                ],
            }
            for c in result.colors
        ],
    }
