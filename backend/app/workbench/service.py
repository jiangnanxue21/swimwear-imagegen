"""工作台 service:把库里的行翻译成 `ProductFlow`,并承接阶段 2 的动作。

分工(见 flow.py 模块注释):

    flow.py     判定。零依赖纯函数,列表页与详情页读同一份结论
    service.py  翻译。查 media_assets / attribute_values / listing_image_sets /
                listing_copies / listing_drafts,组装成 flow 的输入;
                以及文案生成、草稿构建、导出这些**会写库**的动作

首期范围(§1.1)固定为 `GENERIC / MAIN / swimwear / en`,常量从
`app.channels.generic` 取 —— 四个「一个」只写在那一个地方。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.attributes import queue_policy
from app.attributes import service as attr_service
from app.attributes.registry import required_for_listing
from app.channels import generic
from app.core import audience as audience_rules_core
from app.core.enums import (
    AttributeStatus,
    AuditAction,
    CopyStatus,
    DraftStatus,
    ImageSetStatus,
    MediaStatus,
)
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db import locks
from app.listings import (
    copy_generator,
    copy_service,
    export_preview,
    export_writer,
    image_set_rules,
    image_set_service,
    variants,
)
from app.listings import draft as draft_rules
from app.listings.contracts import (
    CopySnapshot,
    FieldViolation,
    ImageItemSnapshot,
    ImageSetSnapshot,
    ListingDraftData,
    MappedListing,
)
from app.media import sample_completeness
from app.models.attribute import ProductAttributeExtraction
from app.models.listing_copy import ContentPlan, ListingCopy, ListingDraft
from app.models.listing_image import ListingImageItem, ListingImageSet
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.services import audit, product_service
from app.workbench import audience_rules as audience_gate
from app.workbench import flow as flow_rules
from app.workbench import platform as pf
from app.workbench import platform_service as ps
from app.workbench import stale as stale_rules
from app.workbench import upstream_collect, upstream_snapshot
from app.workbench.flow import (
    AttributeFacts,
    AudienceFacts,
    CopyFacts,
    DraftFacts,
    FlowResult,
    ImageSetFacts,
    MaterialFacts,
    ProductFlow,
)
from app.workflows import copy_idempotency

logger = get_logger(__name__)

#: 字段映射逻辑自身的版本。改 `channels/generic` 的映射规则时 +1,
#: 让存量草稿按 §4.5 第 5 行整体过期重算
MAPPING_VERSION = "1"


# ==========================================================
# 事实组装:库里的行 -> flow.py 的输入
# ==========================================================

#: 一件商品当前的属性值,`attr_service.effective_map` 的返回形状。
_AttrValues = Mapping[str, Any]


def _attr_values(
    session: Session, product: Product, values: _AttrValues | None
) -> _AttrValues:
    """属性值:调用方给了就用,没给才查(任务 19)。

    ## 为什么是往下传,不是加一层缓存

    `effective_map()` 原来在**一次 `collect()` 里被调了四遍** ——
    `_confirmed_version_ids` / `_attribute_facts` / `_attr_field_names` /
    `build_canonical` 各查各的,同参数、同 session、同事务,四条一模一样的
    SELECT。工作台三个 GET(列表、异常、SPU)都对全量商品逐件 `collect()`,
    所以这个 4 是要乘以商品数的。

    缓存能少打三次,但它会引入一个新问题:缓存该在什么时候失效。而这条
    路径上确实有写(`refresh_draft` 会把草稿标成 STALE),将来也会有更多写。
    一个"大部分时候对"的缓存,错的时候表现为界面上少数商品的阻断数不对,
    没人会把它和缓存联系起来。往下传则是显式的:谁用了哪一份取数,签名上写着。

    留 `None` 分支是因为这几个函数还有 `collect()` 之外的调用方
    (`batch_service` 直接调 `_confirmed_version_ids`,导出路径直接调
    `_attr_field_names`),它们手上没有现成的值。**`None` 是兼容路径,
    不是默认路径** —— `collect()` 必须传。
    """
    if values is not None:
        return values
    return attr_service.effective_map(session, product.id, product=product)


def _audience_facts(product: Product) -> AudienceFacts:
    """库里的受众列 -> 判定层事实(§3.4)。

    `coerce` 抛错的那一档**不能吞成 None**:一个写错的受众字符串被当成
    "没填",后果是该商品静默走回受众前时代的单受众路径,而没人会发现。
    所以这里把它显式标成 `invalid`,由 `flow` 产出一条阻断问题。
    """
    try:
        audience = audience_rules_core.coerce(product.audience)
    except ValueError:
        return AudienceFacts(audience=None, invalid=True)
    return AudienceFacts(audience=audience.value if audience else None)


def _material_facts(session: Session, product: Product) -> MaterialFacts:
    # 颜色完整度是 SPU 门禁：同款各颜色通常落在不同 SKU 行。只按当前
    # product_id 查会把其余已上传颜色误报成缺图。历史无 spu_id 行仍按商品。
    owner_filter = (
        MediaAsset.spu_id == product.spu_id
        if product.spu_id is not None
        else MediaAsset.product_id == product.id
    )
    rows = list(
        session.scalars(
            select(MediaAsset).where(
                owner_filter,
                MediaAsset.status != MediaStatus.DELETED.value,
            )
        )
    )
    usable = [r for r in rows if r.status == MediaStatus.READY.value]
    usable_roles = frozenset(r.role for r in usable if r.role)
    # 声明过的颜色。**取数与上传接口的白名单同源**(`product_service.colours_for`)
    # —— 门禁判「这个颜色缺图」和界面让人「给这个颜色传图」必须是同一组颜色,
    # 否则会出现一个判得出缺图、却选不到的颜色,而那条阻断永远解不开
    colours = product_service.colours_for(session, product)
    return MaterialFacts(
        total=len(rows),
        usable=len(usable),
        quarantined=sum(1 for r in rows if r.status == MediaStatus.QUARANTINED.value),
        pending=sum(1 for r in rows if r.status == MediaStatus.PENDING.value),
        usable_roles=usable_roles,
        # §6.2 门禁。**这一层不判定,只把库里的行递过去** —— 判定在
        # `media/sample_completeness`,因为那里零依赖、整个输入空间能被穷举,
        # 而在这里它就要起一个 PostgreSQL 才验得动。
        #
        # 传 `usable` 而不是 `rows` 是刻意的:判定内部也过一次 READY
        # (它复用 §5.1 的白名单),两道看起来重复。但那条白名单的注释自己
        # 写着状态条件将来可能放宽,而放宽之后传 `rows` 的写法会让**被隔离的
        # 素材满足完整度门禁** —— 一个不报错、只是让隔离形同虚设的洞。
        gate_roles=sample_completeness.gate_roles(usable),
        # A45-batch14-15:归属外键落库之后,颜色作用域这一半才有真数据可递。
        #
        # **A45-batch14-27 改了颜色维的来源。** 原来是
        # `{str(a.color_variant_id) for a in usable if ...}` —— 从素材里数。
        # 那条理由(与 `scope_fingerprint.fingerprints()` 同源)对指纹成立,
        # 对**门禁**是反的:一个一张图都没传的颜色在素材里留不下痕迹,
        # 于是它永远不进这张表、永远不被判缺图,而那正是门禁最该拦的一种。
        # 完整说明在 `MaterialFacts.variant_labels` 上。
        #
        # 取**并集**而不是只取声明清单:一张挂在已下架颜色上的存量图
        # 不该凭空消失 —— 那种行需要有人处理,让它隐身等于把问题藏起来。
        variant_gate_roles={
            variant: sample_completeness.variant_gate_roles(usable, variant)
            for variant in sorted(
                {str(v.id) for v in colours}
                | {
                    str(a.color_variant_id)
                    for a in usable
                    if getattr(a, "color_variant_id", None)
                }
            )
        },
        # 名字而不是 UUID。一件三色商品缺两色正面图时,三条一模一样的
        # 「缺少正面图」让运营无从知道该给哪个颜色补图
        variant_labels={
            str(v.id): (v.working_name or v.display_name or v.variant_code)
            for v in colours
        },
        confirmable_roles=sample_completeness.confirmable_roles(usable),
    )


def _attribute_facts(
    session: Session, product: Product, values: _AttrValues
) -> AttributeFacts:
    by_status: dict[str, set[str]] = {}
    for name, row in values.items():
        by_status.setdefault(row.status, set()).add(name)
    extraction_count = session.execute(
        select(func.count(ProductAttributeExtraction.id)).where(
            ProductAttributeExtraction.product_id == product.id
        )
    ).scalar_one()
    # §11 的确认队列口径。**这一层不判定,只把库里的状态递过去** ——
    # 判定在 `attributes/queue_policy`,因为那里零依赖、`AttributeStatus`
    # 的每一个成员都能被穷举,而在这里它要起一个 PostgreSQL 才验得动。
    #
    # 原来这四行是直接按状态取桶的:`by_status.get(AttributeStatus.X.value)`。
    # 那种写法今天给出同样的结果,坏处在**将来**:枚举新增一个取值时,
    # 它不落进任何一个桶,于是那个字段在确认队列、阻断清单里同时消失,
    # 而商品照样导不出去 —— 一件卡住但没有任何理由的商品。
    # 分桶表是穷举的,漏一个当场红。
    buckets = queue_policy.bucket_fields(by_status)
    return AttributeFacts(
        # PRD v2 §18.3:必填集按受众条件化。受众未确认 -> 与 v1 行为一致的
        # 全受众必填集(见 registry.required_for_listing 的文档)
        required_fields=required_for_listing(audience_rules_core.coerce(product.audience)),
        confirmed=buckets.settled,
        suggested=buckets.confirmable,
        conflicted=buckets.adjudicable,
        # **`candidate_only` 不进确认队列**(§11)。它和 `suggested` 放在
        # 同一个 dataclass 上是刻意的,但两份集合不是一回事:
        #
        #   suggested       有值、等人点头 —— 「待确认」那一档读它
        #   candidate_only  留了证据但不采信 —— 它欠的是「有人去填一个值」
        #
        # 混成一个的后果写在 `AttributeStatus` 自己的文档字符串里。
        candidate_only=buckets.evidence_only,
        # §5.3 / AC-21。**这一层不判定,只把结果递过去** —— 判定在
        # `attributes.service.stale_fields`,它是 `facts_stale` 的唯一调用点。
        #
        # 在这里就地写"取指纹、按 owner 找作用域、比一比"的话,写入侧
        # (`apply_evidence` / `confirm`)和读取侧就会各有一套集合口径,
        # 而漂移的表现是**一条刚确认完的事实立刻显示已过期** ——
        # 两边都不报错,而运营会一直点同一个字段。
        #
        # 多一次取数(深度 0,一件商品一条 SELECT)。查询预算棘轮断言的是
        # 形状不是总数,这一条不踩红;真要省的话该省的是让
        # `_material_facts` 与它共用素材行,那是另一件事,不在本批范围。
        stale=attr_service.stale_fields(session, product, values),
        extraction_count=int(extraction_count),
    )


def _current_image_set(session: Session, spu: str) -> ListingImageSet | None:
    """工作台展示用哪一版:优先已批准,其次最新的非归档版本。"""
    approved = image_set_service.resolve_for_publish(
        session, spu=spu, channel=None, site=None
    )
    if approved is not None:
        return approved
    rows = [
        r
        for r in image_set_service.list_sets(session, spu)
        if r.status != ImageSetStatus.ARCHIVED.value
    ]
    if not rows:
        return None
    return max(rows, key=lambda r: r.version)


def _set_items(
    session: Session, image_set: ListingImageSet
) -> list[tuple[ListingImageItem, MediaAsset | None]]:
    items = list(
        session.scalars(
            select(ListingImageItem)
            .where(ListingImageItem.image_set_id == image_set.id)
            .order_by(ListingImageItem.sort_order)
        )
    )
    assets = {
        a.id: a
        for a in session.scalars(
            select(MediaAsset).where(
                MediaAsset.id.in_([i.media_asset_id for i in items] or [None])
            )
        )
    }
    return [(item, assets.get(item.media_asset_id)) for item in items]


def _image_set_facts(
    session: Session, image_set: ListingImageSet | None
) -> ImageSetFacts:
    if image_set is None:
        return ImageSetFacts()
    violations = image_set_service.validate(session, image_set.id)
    pairs = _set_items(session, image_set)
    downgraded = image_set.status == ImageSetStatus.PENDING_REVIEW.value and any(
        asset is not None
        and asset.status == MediaStatus.QUARANTINED.value
        and item.enabled
        for item, asset in pairs
    )
    return ImageSetFacts(
        exists=True,
        status=image_set.status,
        version=image_set.version,
        item_count=sum(1 for item, _ in pairs if item.enabled),
        violation_codes=tuple(v.code.value for v in violations),
        downgraded=downgraded,
        # A10:退回原因决定下一步。不带上来的话判定层只看见 REJECTED,
        # 下一步会落到兜底的"修复图片集",而运营退回时选的是"需要补素材"
        reject_reason=image_set.reject_reason,
    )


def _confirmed_version_ids(
    session: Session, product: Product, values: _AttrValues | None = None
) -> tuple[str, ...]:
    values = _attr_values(session, product, values)
    return tuple(
        sorted(
            str(row.id)
            for row in values.values()
            if row.status == AttributeStatus.CONFIRMED.value
        )
    )


def _current_copy(session: Session, spu: str) -> ListingCopy | None:
    """工作台展示用哪一版文案:同 scope 下版本号最大的一版。"""
    rows = list(
        session.scalars(
            select(ListingCopy).where(
                ListingCopy.spu == spu,
                ListingCopy.channel == generic.CHANNEL,
                ListingCopy.site == generic.SITE,
                ListingCopy.locale == generic.LOCALE,
                ListingCopy.status != CopyStatus.ARCHIVED.value,
            )
        )
    )
    if not rows:
        return None
    return max(rows, key=lambda r: r.version)


def _copy_is_stale(
    session: Session, row: ListingCopy, confirmed_ids: Sequence[str]
) -> bool:
    """§4.5 第 1 行:已确认属性被修改 -> 文案过期。

    判定本体是 `stale_rules.copy_attrs_stale`(纯函数,§4.5 矩阵的
    文案列在 test_stale_matrix 里按格穷举);这里只负责查库取快照。
    """
    plan = session.get(ContentPlan, row.content_plan_id)
    return stale_rules.copy_attrs_stale(
        row.status,
        None if plan is None else plan.attr_snapshot_ids,
        confirmed_ids,
        plan_missing=plan is None,
    )


def _copy_facts(
    session: Session, row: ListingCopy | None, confirmed_ids: Sequence[str]
) -> CopyFacts:
    if row is None:
        return CopyFacts()
    violations = list(row.violations or [])
    return CopyFacts(
        exists=True,
        status=row.status,
        version=row.version,
        blocking_violations=sum(1 for v in violations if v.get("level") == "error"),
        warning_violations=sum(1 for v in violations if v.get("level") == "warning"),
        stale=_copy_is_stale(session, row, confirmed_ids),
        locale=row.locale,
        # A10:文案的 REJECTED 有两个来源(规则硬失败 / 快审退回),
        # 判定层靠这一列区分。见 `flow._evaluate_copy` 的注释
        reject_reason=row.reject_reason,
    )


def _current_draft(session: Session, spu: str) -> ListingDraft | None:
    rows = list(
        session.scalars(
            select(ListingDraft).where(
                ListingDraft.spu == spu,
                ListingDraft.channel == generic.CHANNEL,
                ListingDraft.site == generic.SITE,
            )
        )
    )
    if not rows:
        return None
    return max(rows, key=lambda r: r.created_at or datetime.min)


# ---------------------------------------------------------------- 草稿数据组装


def _snapshot_from_set(
    session: Session, image_set: ListingImageSet
) -> ImageSetSnapshot:
    items: list[ImageItemSnapshot] = []
    for item, asset in _set_items(session, image_set):
        items.append(
            ImageItemSnapshot(
                media_asset_id=str(item.media_asset_id),
                role=item.role,  # ImageItemSnapshot 接受枚举或字符串;role 列存的就是枚举值
                sort_order=item.sort_order,
                storage_path=asset.storage_path if asset else "",
                width=asset.width if asset else 0,
                height=asset.height if asset else 0,
                variant_id=item.variant_id,
                is_primary=item.is_primary,
                enabled=item.enabled,
                derivative_purpose=item.derivative_purpose,
            )
        )
    return ImageSetSnapshot(
        image_set_id=str(image_set.id),
        spu=image_set.spu,
        version=image_set.version,
        status=image_set.status,
        items=tuple(items),
        channel=image_set.channel,
        site=image_set.site,
    )


def _snapshot_from_copy(row: ListingCopy) -> CopySnapshot:
    return CopySnapshot(
        copy_id=str(row.id),
        locale=row.locale,
        version=row.version,
        title=row.title,
        bullet_points=tuple(row.bullet_points or ()),
        description=row.description,
        keywords=tuple(row.keywords or ()),
        claims=(),  # 映射层不用 claims;校验在文案层已经做完
        extra=dict(row.extra or {}),
    )


def _canonical_with_skus(session: Session, product: Product):
    """SPU 属性 + 同 SPU 全部商品行展开成 SKU 行(§9.5)。

    `products` 表一行就是一个 SKU(spu + sku + size)。变体 id 用
    已确认的 primary_color 兜底到 sku 编码 —— 首期单商品闭环够用,
    多色 SPU 的变体建模是阶段 3 的事。
    """
    from app.attributes.contracts import CanonicalProduct, CanonicalSku, CanonicalVariant

    base = attr_service.build_canonical(session, product)
    siblings = list(
        session.scalars(
            select(Product)
            .where(Product.spu == product.spu)
            .order_by(Product.sku)
        )
    )
    skus = tuple(
        CanonicalSku(
            sku=row.sku,
            variant_id=variants.variant_id_for(row),
            size=row.size,
            size_group=row.size_group,
        )
        for row in siblings
    )

    # ---- 颜色层:**每一个变体都要组装,不只是当前这一件**(A43 / BLOCK-03)
    #
    # `build_canonical()` 只认识传给它的那一行商品,于是它给出的 `variants`
    # 里只有当前 SKU 所属的那个颜色。而草稿覆盖的是整个 SPU ——
    # 一个红蓝双色的 SPU,从红色那一件生成草稿时,蓝色行的 `variant.primary_color`
    # 会解析不到值,导出文件里蓝色那一行的颜色列是空的。
    #
    # 这是多颜色 SPU 那条链路上最安静的一个缺口:单色商品全程正常,
    # 双色商品导出文件"看起来完整",只有一行是空的。
    # 一条 SQL 查完所有变体,**不是每个变体调一次 `build_canonical`**:
    # 那种写法会把一次库读嵌进两层循环,`test_workbench_query_budget.py`
    # 那条棘轮在本轮真的拦下过它一次
    variant_ids = list(dict.fromkeys(sku.variant_id for sku in skus))
    by_variant = attr_service.variant_attr_map(session, product.spu, variant_ids)
    canonical_variants = tuple(
        CanonicalVariant(
            variant_id=vid,
            attrs=attr_service.attr_values_of(by_variant.get(vid, {})),
        )
        for vid in variant_ids
    )
    # **`variants` 必须原样带过来(A43 / BLOCK-03)。**
    # 漏掉它的话 `build_canonical()` 刚组装好的颜色层在这里被丢掉,
    # 而导出走的正是这一条路 —— 表现是"属性页显示颜色已确认,
    # 导出文件的颜色列是空的"。
    return CanonicalProduct(
        spu=base.spu,
        category_path=base.category_path,
        spu_attrs=base.spu_attrs,
        variants=canonical_variants,
        skus=skus,
        media=base.media,
    )


def _current_draft_data(
    session: Session,
    product: Product,
    *,
    manual: Mapping[str, Any],
) -> tuple[ListingDraftData | None, list[str], ListingImageSet | None]:
    """按**当前**上游组装草稿数据。前置不满足时返回 (None, 缺什么, 图片集行)。

    第三项是刚解析出来的那一版图片集(A45-batch20)。返回它而不是让
    颜色维取数自己再 `resolve_for_publish()` 一次:两次解析之间有人批准了
    新版的话,草稿的 `image_set_id` 写的是第一次的结果、颜色维快照记的是
    第二次的 —— 一份自己和自己对不上的草稿,而且它下一次刷新就说自己过期。
    """
    problems: list[str] = []

    image_set = image_set_service.resolve_for_publish(
        session, spu=product.spu, channel=None, site=None
    )
    if image_set is None:
        problems.append("没有已批准的图片集")

    copy_row = copy_service.latest_approved(
        session,
        spu=product.spu,
        channel=generic.CHANNEL,
        site=generic.SITE,
        locale=generic.LOCALE,
    )
    if copy_row is None:
        problems.append(f"没有已批准的 {generic.LOCALE} 文案")

    if problems:
        return None, problems, image_set

    # 阶段 0 修复(PRD v2 §0.2):类目从**商品**派生,不再取模块常量。
    # 男装商品从此拿到 men_swimwear 规则包 —— 用女装 spec 静默导出的路径
    # 在源头上消失,§17 的断言只是它的第二道网
    category_id = generic.category_id_for(product)
    spec = generic.field_spec(category_id=category_id)
    data = ListingDraftData(
        spu=product.spu,
        channel=generic.CHANNEL,
        site=generic.SITE,
        category_id=category_id,
        product=_canonical_with_skus(session, product),
        image_set=_snapshot_from_set(session, image_set),
        copies={generic.LOCALE: _snapshot_from_copy(copy_row)},
        manual=dict(manual),
        spec_version=spec.spec_version,
        mapping_version=MAPPING_VERSION,
    )
    return data, [], image_set


def _components_of(data: ListingDraftData, attr_fields: Mapping[str, str]) -> dict:
    """草稿数据 -> 过期对比用的组件快照(stale.diff_components 的输入)。"""
    return {
        "attrs": list(data.product.attribute_version_ids()),
        "attr_fields": dict(attr_fields),
        "image_set": {
            "id": data.image_set.image_set_id,
            "version": data.image_set.version,
        },
        "copies": {
            locale: {"id": c.copy_id, "version": c.version}
            for locale, c in data.copies.items()
        },
        "manual": dict(data.manual),
        "spec_version": data.spec_version,
        "mapping_version": data.mapping_version,
    }


def _attr_field_names(
    session: Session, product: Product, values: _AttrValues | None = None
) -> dict[str, str]:
    values = _attr_values(session, product, values)
    return {str(row.id): name for name, row in values.items()}


def refresh_draft(
    session: Session,
    product: Product,
    row: ListingDraft,
    *,
    dry_run: bool = False,
    attr_values: _AttrValues | None = None,
) -> tuple[bool, list[stale_rules.StaleChange]]:
    """按当前上游刷新草稿状态。返回 (是否过期, 可解释的变化)。

    §4.5.1:过期草稿一律禁止导出;过期提示须说明哪个上游变了、
    变了哪些字段、该做什么。状态变化落库,列表页与详情页读到同一个结论。

    `dry_run=True`(A6):**只算不写**。判定逻辑仍然是下面这一份 ——
    P4 的约束是"过期口径一律走 refresh_draft,不许别处再推断一遍",
    所以只读接口的修法是给这个函数加一个不落库的模式,
    而不是在批次页复制一遍判定。

    `attr_values`(任务 19):调用方已经取过属性值时传下来,省掉重复的
    那次 SELECT。**不传行为完全不变** —— 见 `_attr_values` 的说明。
    """
    if row.status == DraftStatus.ARCHIVED.value:
        return False, []

    current, problems, image_set_row = _current_draft_data(
        session, product, manual=dict(row.manual_payload or {})
    )
    stored = dict((row.canonical_snapshot or {}).get("components") or {})

    if current is None:
        # 上游批准被撤销(图片集降级、文案归档)。指纹无从重算,
        # 但结论是明确的:这份草稿引用的上游已不可信
        changes = stale_rules.diff_components(
            stored,
            {
                **stored,
                "image_set": None
                if any("图片集" in p for p in problems)
                else stored.get("image_set"),
                "copies": {}
                if any("文案" in p for p in problems)
                else stored.get("copies"),
            },
        )
        if row.status != DraftStatus.STALE.value and not dry_run:
            row.status = DraftStatus.STALE.value
            session.flush()
        return True, changes

    # ---- 颜色轴(§6.7,A45-batch20)----
    #
    # **`source_fingerprint` 不含颜色维**,所以颜色轴必须在 `is_stale` 之外
    # 单独问一次。指纹的输入是 `ListingDraftData`(SPU 属性 + 图片集 + 文案 +
    # 手填 + 两个版本号),没有一项带颜色 —— 于是"新增了一个 ACTIVE 颜色"
    # 算出来的指纹和原来一模一样,而那正是最该重新生成草稿的一种变化。
    #
    # 顺序也是刻意的:先算颜色轴,再和指纹的结论取并集。反过来写成
    # `if not is_stale: return False, []` 在前的话,颜色维就永远走不到 ——
    # 这是 5-1 的判定层最容易被"接线接漏"的一跳。
    color_changes = _color_axis_changes(session, product, row, current, image_set_row)

    is_stale = draft_rules.is_stale(row.source_fingerprint, current)
    if not is_stale and not color_changes:
        return False, []

    if row.status not in (DraftStatus.STALE.value,) and not dry_run:
        row.status = DraftStatus.STALE.value
        session.flush()
    changes = [
        *(
            stale_rules.diff_components(
                stored,
                _components_of(current, _attr_field_names(session, product, attr_values)),
            )
            if is_stale
            else []
        ),
        *color_changes,
    ]
    return True, changes


def _color_axis_changes(
    session: Session,
    product: Product,
    row: ListingDraft,
    current: ListingDraftData,
    image_set_row: ListingImageSet | None,
) -> list[stale_rules.StaleChange]:
    """颜色轴的过期变化(§6.7 / AC-19)。判定全在 `upstream_snapshot`。

    存量草稿(`upstream_versions IS NULL`)在这里拿到的是一条
    「颜色维是否仍然成立无法证明」——**不是空列表**。这是 5-1 D2 定下的
    上线语义:0049 之前建的每一行草稿都一次都没算过颜色维,而放行它们
    等于让一份缺了整个颜色的草稿显示「上游全部有效」。

    收口方式是分批重新生成,不是回填 `{}`(那会把未知伪装成空集)。
    """
    inputs = upstream_collect.collect(
        session, product, canonical=current.product, image_set_row=image_set_row
    )
    return upstream_snapshot.diff_upstream(
        row.upstream_versions,
        upstream_snapshot.build_upstream_versions(
            components=_components_of(current, {}),
            colors=inputs.colors,
            shared_fact_versions=inputs.shared_fact_versions,
            shared_sample_fingerprint=inputs.shared_sample_fingerprint,
        ),
    )


def _draft_facts(
    session: Session,
    product: Product,
    row: ListingDraft | None,
    *,
    dry_run: bool = False,
    attr_values: _AttrValues | None = None,
) -> DraftFacts:
    if row is None:
        return DraftFacts()
    is_stale, _ = refresh_draft(
        session, product, row, dry_run=dry_run, attr_values=attr_values
    )
    errors = list(row.validation_errors or [])
    warnings = list(row.validation_warnings or [])
    open_count = len(ps.open_rejections(session, row.id))
    return DraftFacts(
        exists=True,
        status=row.status,
        error_count=len(errors),
        warning_count=len(warnings),
        stale=is_stale,
        exported=bool(row.exported_at),
        platform_status=getattr(row, "platform_status", None),
        open_rejections=open_count,
    )


# ==========================================================
# 汇总入口
# ==========================================================


@dataclass(frozen=True)
class WorkbenchContext:
    """一件商品的判定结果 + 支撑它的那些行(详情页要展开它们)。"""

    product: Product
    flow: ProductFlow
    result: FlowResult
    image_set: ListingImageSet | None
    copy: ListingCopy | None
    draft: ListingDraft | None


def collect(
    session: Session, product: Product, *, dry_run: bool = False
) -> WorkbenchContext:
    """组装并判定一件商品。**列表页与详情页都走这一个函数。**

    `dry_run=True`(评审第 19 条):判定照做,但**不把 STALE 结论落库**。

    只读接口一律传它。原来列表、流程、异常、SPU 聚合四个 GET 都在
    `collect()` 之后主动 commit,把草稿状态改成 STALE —— 于是页面刷新、
    浏览器预取、监控探活都会改变业务状态,而这几条路径上没有任何一个
    动作是运营主动发起的。

    判定逻辑仍然只有 `refresh_draft` 那一份(P4 的约束),这里只是把
    "算"和"写"分开。落库由上游写入、显式刷新动作或后台一致性任务完成
    (`tasks/maintenance_tasks.refresh_stale_drafts`)。
    """
    # 任务 19:属性值**在这里取一次**,下面三个用到它的地方都从参数拿。
    # 原来 _confirmed_version_ids / _attribute_facts / _attr_field_names
    # 各查各的,一件商品四条相同的 SELECT(第四条在 build_canonical,
    # 那个函数生成链路也在用,不在本轮范围内)
    attr_values = attr_service.effective_map(session, product.id, product=product)

    confirmed_ids = _confirmed_version_ids(session, product, attr_values)
    image_set = _current_image_set(session, product.spu)
    copy_row = _current_copy(session, product.spu)
    draft_row = _current_draft(session, product.spu)

    product_flow = ProductFlow(
        product_id=str(product.id),
        sku=product.sku,
        spu=product.spu,
        audience=_audience_facts(product),
        material=_material_facts(session, product),
        attribute=_attribute_facts(session, product, attr_values),
        image_set=_image_set_facts(session, image_set),
        copy=_copy_facts(session, copy_row, confirmed_ids),
        draft=_draft_facts(
            session, product, draft_row, dry_run=dry_run, attr_values=attr_values
        ),
    )
    return WorkbenchContext(
        product=product,
        flow=product_flow,
        result=flow_rules.evaluate(product_flow),
        image_set=image_set,
        copy=copy_row,
        draft=draft_row,
    )


def review_focus_for(product: Product) -> tuple[str, ...]:
    """本商品审阅时的重点检查项(§13.2 / §19)。

    **来源是规则包,不是这里的一张表**(§19 原话:前端不许自己维护一份
    受众到检查项的映射;同一条道理对后端也成立 —— 在这里再写一份,
    改了规则包界面还在提示旧的那几项,而且不会报错)。

    规则包读不出来时返回空元组而不是一份兜底清单:界面显示"未配置检查项"
    是一个能被人看见、能被追查的状态;显示一份猜出来的清单不是。
    """
    try:
        spec = generic.field_spec(category_id=generic.category_id_for(product))
    except Exception:  # noqa: BLE001
        # 受众未确认(派生不出规则包)、spec 文件缺失或校验不过。
        # 审阅页少一行提示不该让整个详情接口 500
        return ()
    return tuple(spec.review_checks)


def serialize_flow(result: FlowResult) -> dict[str, Any]:
    """判定结果 -> 接口出参。列表与详情共用,保证两处数字一致(§3.4)。"""
    return {
        "completion": result.completion,
        "blocking_count": result.blocking_count,
        "pending_count": result.pending_count,
        "reminder_count": result.reminder_count,
        "current_step": result.current_step.value,
        "current_step_label": flow_rules.STEP_LABELS[result.current_step],
        "next_action": {
            "code": result.next_action.code.value,
            "label": result.next_action.label,
            "step": result.next_action.step.value,
            "reason": result.next_action.reason,
        },
        "steps": [
            {
                "step": s.step.value,
                "label": flow_rules.STEP_LABELS[s.step],
                "state": s.state.value,
                "summary": s.summary,
                "issues": [
                    {
                        "level": i.level.value,
                        "code": i.code,
                        "message": i.message,
                        "target_step": i.target_step.value,
                        "hint": i.hint,
                        "ref": i.ref,
                    }
                    for i in s.issues
                ],
            }
            for s in result.steps
        ],
    }


# ==========================================================
# 动作:文案
# ==========================================================


def _content_plan_or_raise(
    session: Session, product: Product, *, actor: str
) -> ContentPlan:
    plan = copy_service.build_content_plan(session, product=product, actor=actor)
    if not plan.facts:
        raise ValidationError(
            "还没有任何已确认的属性,无法生成文案。先在属性标签页确认属性",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    return plan


def _copy_unit(
    session: Session, product: Product, *, attr_values: _AttrValues | None = None
) -> copy_idempotency.CopyUnit:
    """这次生成属于哪个幂等单元(§4.9 / AC-11)。

    事实版本集走 `_confirmed_version_ids`,与草稿指纹、内容计划读的是
    **同一份取数** —— 另查一次就等于给「这次用的是哪些事实」造第二个答案。

    `color_variant_id` 今天恒为 None:颜色维文案要等 5-4 的颜色结构化字段。
    留着这个位置而不是等到那天再加,是因为它进的是**哈希**:
    那天补上去会让全部存量键失效,而失效表现为一次全量重新生成(全是付费调用)。
    """
    return copy_idempotency.unit_for(
        spu=product.spu,
        channel=generic.CHANNEL,
        site=generic.SITE,
        locale=generic.LOCALE,
        fact_versions=_confirmed_version_ids(session, product, attr_values),
        color_variant_id=None,
    )


def generate_copy(
    session: Session,
    product: Product,
    *,
    only_fields: Sequence[str] | None = None,
    force: bool = False,
    actor: str,
) -> ListingCopy:
    """生成一版文案(FE-221 / FE-222 单字段重生)。

    `only_fields` 给了就只覆盖被点名的字段,其余保留上一版 ——
    合并规则在 `copy_generator.merge_fields`,claims 跟着标题走。

    ## 幂等(§4.9 / AC-11,A45-batch21)

    入口是 SKU 粒度的,而 `listing_copies` 是 SPU 粒度的。一个 S/M/L 三码的
    SPU 在三行上各点一次生成,输入完全相同,而在这之前那是**三次付费调用**。

    所以先算幂等单元、命中就直接返回那一版,**不建内容计划、不调生成器**。
    `_content_plan_or_raise` 必须留在判定之后:它每次都 `session.add` 一行
    `ContentPlan` 并写一条审计 —— 放在前面的话,复用路径会安静地攒垃圾行,
    而"复用了几次"在审计里看起来和"生成了几次"一模一样。

    `force=True` 是运营的明确重做意图(单字段重生蕴含它)。那是一次点击
    一次调用,不随尺码数量翻倍 —— AC-11 要收口的是"翻倍"那一项。
    """
    unit = _copy_unit(session, product)
    previous_for_reuse = _current_copy(session, product.spu)
    verdict = copy_idempotency.decide(
        existing_key=getattr(previous_for_reuse, "idempotency_key", None),
        existing_status=getattr(previous_for_reuse, "status", None),
        wanted_key=unit.key(),
        force=force or bool(only_fields),
    )
    if not copy_idempotency.spends_money(verdict):
        # 命中。**返回既有那一版,不新建版本** —— 新建一个内容相同的版本
        # 会把上一版挤成 ARCHIVED,于是"批准过的那一版"在界面上消失,
        # 而没有任何东西说明发生了什么
        return previous_for_reuse

    # 串行化同一个单元:两个并发请求各自判 GENERATE,各调一次生成器。
    # 拿不到锁就直接告诉调用方"另一次正在跑",不排队 —— 阻塞版会让一次
    # HTTP 请求等在那里占着连接池(理由与 `db/locks` 里那段一字不差)
    if not locks.try_advisory_xact_lock(session, "listing_copy_unit", unit.key()):
        raise ValidationError(
            "这个 SPU 的文案正在生成中,请稍候再试",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    plan = _content_plan_or_raise(session, product, actor=actor)
    rules = copy_service.resolve_rules(generic.CHANNEL, generic.SITE)
    generator = copy_generator.get_generator()

    previous_row = _current_copy(session, product.spu)
    previous: dict[str, Any] | None = None
    if previous_row is not None:
        previous = {
            "title": previous_row.title,
            "bullet_points": list(previous_row.bullet_points or []),
            "description": previous_row.description,
            "keywords": list(previous_row.keywords or []),
            "claims": list(previous_row.claims or []),
        }
    if only_fields and previous is None:
        raise ValidationError(
            "还没有任何文案,无法只重生个别字段;先整体生成一版",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    fresh = generator.generate(
        facts=plan.facts,
        selling_points=list(plan.selling_points),
        forbidden_claims=list(plan.forbidden_claims),
        locale=generic.LOCALE,
        only_fields=only_fields,
        previous=previous,
    )
    merged = copy_generator.merge_fields(previous, fresh, only_fields)

    return copy_service.save_copy(
        session,
        plan=plan,
        channel=generic.CHANNEL,
        site=generic.SITE,
        locale=generic.LOCALE,
        copy=merged.as_copy_dict(),
        claims=[dict(c) for c in merged.claims],
        rules=rules,
        model_name=merged.generator,
        prompt_version=merged.prompt_version,
        idempotency_key=unit.key(),
        actor=actor,
    )


def _surviving_claims(
    claims: Sequence[Mapping[str, Any]], copy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """人工编辑后仍然站得住的 claims。

    编辑动了文本,旧 claim 的 `text_span` 可能已经不在了 —— 保留它只会
    产出一条 `CLAIM_SPAN_NOT_FOUND` 的硬失败,而失败原因和运营刚才做的事
    毫无关系。这里只留 span 仍逐字存在于对应位置的那些;被丢掉的声明
    等于失去溯源,校验层若因此报「必要 claim 缺失」正是设计意图:
    运营改掉了颜色词,就该重新生成或补回一个能溯源的说法。
    """
    survivors: list[dict[str, Any]] = []
    bullets = list(copy.get("bullet_points") or [])
    for claim in claims:
        span = str(claim.get("text_span") or "")
        location = str(claim.get("location") or "")
        if not span or not location:
            continue
        if location == "title":
            haystack = str(copy.get("title") or "")
        elif location == "description":
            haystack = str(copy.get("description") or "")
        elif location.startswith("bullet."):
            try:
                haystack = str(bullets[int(location.split(".", 1)[1])])
            except (ValueError, IndexError):
                haystack = ""
        else:
            haystack = ""
        if span in haystack:
            survivors.append(dict(claim))
    return survivors


def save_copy_manual(
    session: Session,
    product: Product,
    *,
    fields: Mapping[str, Any],
    actor: str,
) -> ListingCopy:
    """人工编辑落成新版本(FE-222)。**照常走全套校验。**"""
    plan = _content_plan_or_raise(session, product, actor=actor)
    rules = copy_service.resolve_rules(generic.CHANNEL, generic.SITE)

    previous_row = _current_copy(session, product.spu)
    base = {
        "title": previous_row.title if previous_row else "",
        "bullet_points": list(previous_row.bullet_points or []) if previous_row else [],
        "description": previous_row.description if previous_row else "",
        "keywords": list(previous_row.keywords or []) if previous_row else [],
    }
    edited = {**base, **{k: v for k, v in fields.items() if k in base}}
    claims = _surviving_claims(
        list(previous_row.claims or []) if previous_row else [], edited
    )

    return copy_service.save_copy(
        session,
        plan=plan,
        channel=generic.CHANNEL,
        site=generic.SITE,
        locale=generic.LOCALE,
        copy=edited,
        claims=claims,
        rules=rules,
        model_name="manual-edit",
        prompt_version=None,
        actor=actor,
    )


# ==========================================================
# 动作:草稿与导出
# ==========================================================


def build_draft(
    session: Session,
    product: Product,
    *,
    manual: Mapping[str, Any] | None = None,
    actor: str,
) -> ListingDraft:
    """构建(或重建)上架草稿(FE-231)。

    重建复用同一行:草稿是「当前该导出什么」的唯一答案,一个 scope
    留一行,历史由审计日志与导出留痕承担。手填字段不传就沿用上一版 ——
    「重新生成一次即可,不会丢手填字段」(flow.py 里对运营的承诺)。
    """
    existing = _current_draft(session, product.spu)
    merged_manual = dict((existing.manual_payload or {}) if existing else {})
    if manual:
        merged_manual.update(
            {k: v for k, v in manual.items() if v is not None}
        )

    data, problems, image_set_row = _current_draft_data(
        session, product, manual=merged_manual
    )
    if data is None:
        raise ValidationError(
            "草稿前置条件不满足:" + ";".join(problems),
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    locale_problems = draft_rules.copy_locale_problems(data)
    if locale_problems:
        raise ValidationError(
            "文案语言绑定有误:" + ";".join(locale_problems),
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    # data.category_id 刚在 _current_draft_data 里从商品派生(阶段 0 修复);
    # spec 与草稿行必须用**同一个**派生结果,不再各取一次常量
    spec = generic.field_spec(category_id=data.category_id)
    mapped = generic.map_fields(data, spec)

    # ---- 颜色维快照与 READY 门禁(§4.10 / §6.7 / AC-18,A45-batch20)----
    #
    # 两列与门禁**共用同一次取数**:检查用的颜色集和构建用的颜色集只要
    # 有一次不同源,就会凭空造出「这个 SKU 不在映射里」,而两边都不报错。
    inputs = upstream_collect.collect(
        session, product, canonical=data.product, image_set_row=image_set_row
    )
    color_sku_image_map = upstream_snapshot.build_color_sku_image_map(
        colors=inputs.colors,
        coverage=image_set_rules.coverage(
            inputs.image_set,
            variant_ids=(c.variant_id for c in upstream_snapshot.active_colors(inputs.colors)),
            required_angles=inputs.required_angles,
        ),
    )
    violations = [
        *generic.validate(mapped, spec),
        *generic.image_count_check(mapped, minimum=1),
        *_color_ready_violations(color_sku_image_map, inputs),
    ]
    blocking = [v for v in violations if v.is_blocking]
    status = DraftStatus.INVALID if blocking else DraftStatus.VALIDATED

    row = existing or ListingDraft(
        spu=product.spu,
        channel=generic.CHANNEL,
        site=generic.SITE,
        category_id=data.category_id,
        image_set_id=UUID(data.image_set.image_set_id),
    )
    row.category_id = data.category_id
    row.status = status.value
    row.image_set_id = UUID(data.image_set.image_set_id)
    row.copy_version_ids = {
        locale: c.copy_id for locale, c in data.copies.items()
    }
    row.manual_payload = merged_manual
    row.mapped_payload = {
        "header": {k: _jsonable(v) for k, v in mapped.header.items()},
        "rows": [
            {k: _jsonable(v) for k, v in r.items()} for r in mapped.rows
        ],
    }
    row.validation_errors = [
        _violation_dict(v) for v in violations if v.is_blocking
    ]
    row.validation_warnings = [
        _violation_dict(v) for v in violations if not v.is_blocking
    ]
    row.mapping_version = MAPPING_VERSION
    row.spec_version = data.spec_version
    row.source_fingerprint = data.source_fingerprint()
    components = _components_of(data, _attr_field_names(session, product))
    row.canonical_snapshot = {
        "components": components,
        "summary": draft_rules.summarize(data),
    }
    row.upstream_versions = upstream_snapshot.build_upstream_versions(
        components=components,
        colors=inputs.colors,
        shared_fact_versions=inputs.shared_fact_versions,
        shared_sample_fingerprint=inputs.shared_sample_fingerprint,
    )
    row.color_sku_image_map = color_sku_image_map
    row.created_by = row.created_by or actor
    if existing is None:
        session.add(row)
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE if existing is None else AuditAction.UPDATE,
        entity_type="ListingDraft",
        entity_id=row.id,
        payload={
            "action": "build",
            "spu": product.spu,
            "status": row.status,
            "errors": len(row.validation_errors),
            "warnings": len(row.validation_warnings),
            "fingerprint": row.source_fingerprint[:12],
        },
    )
    return row


#: 颜色维 READY 门禁产出的违规码。**一个码,不按问题类型再分**。
#:
#: 分成 `MISSING_SKU_IN_MAP` / `FOREIGN_COLOR` / … 的写法在这里是错的:
#: 具体是哪一种已经写在 `message` 里(`ready_problems` 逐条给),而码是
#: 前端分组与 §17 断言的键。一个码对应一次「颜色维不 READY」,
#: 加码要同时改前端与断言表,而那两处不会因为漏改而变红。
COLOR_READY_VIOLATION_CODE = "COLOR_AXIS_NOT_READY"


def _color_ready_violations(
    mapping: Mapping[str, Any], inputs: upstream_collect.UpstreamInputs
) -> list[FieldViolation]:
    """§6.7 的 READY 门禁:图片集规则 **AND** SKU 映射完整性(AC-18)。

    ## 为什么是阻断而不是警告

    §6.7 与 AC-18 的原话是「任一侧失败都不得 READY」。写成警告的话,
    一份红色 SKU 没有映射到任何主图的草稿会照样导出,而平台那边表现为
    「红色那几行没有图」或者更糟 —— 挂着黑色的主图上架。
    5-1 D2 已经为同一件事定过调:默认值不许让任何人被静默放行。

    ## 为什么只调 `ready_problems()`,不在这里各调一次

    `map_problems` 刻意不判主图,`validate_set` 刻意不知道映射里铺了哪些 SKU。
    在这里手写两次调用的话,少读任一侧都只会让本函数少报几条 ——
    **没有任何测试会红**。合取入口在判定层,那里能被穷举。

    ## 没有图片集时不报

    `image_set is None` 只在 `_current_draft_data` 已经把「没有已批准图片集」
    拦成前置不满足时才可能出现,而那条路上根本走不到这里。留这个分支是因为
    传 None 进 `validate_set` 会抛 `AttributeError`,而那个栈指向的是规则层,
    不是真正缺的东西。
    """
    if inputs.image_set is None:
        return []
    return [
        FieldViolation(
            field_key="color_sku_image_map",
            code=COLOR_READY_VIOLATION_CODE,
            message=problem,
            level="error",
        )
        for problem in upstream_snapshot.ready_problems(
            mapping,
            colors=inputs.colors,
            image_set=inputs.image_set,
            required_angles=inputs.required_angles,
        )
    ]


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def _violation_dict(v: FieldViolation) -> dict[str, Any]:
    return {
        "field_key": v.field_key,
        "code": v.code,
        "message": v.message,
        "level": v.level,
        "row_index": v.row_index,
    }


def _mapped_from_row(row: ListingDraft) -> MappedListing:
    """从落库的 mapped_payload 还原映射结果。

    **预览与导出都从这里读**,不重新映射 —— 导出一致率 100% 靠的
    就是两边读同一份数据(§5.3)。
    """
    payload = row.mapped_payload or {}
    violations = tuple(
        FieldViolation(
            field_key=str(v.get("field_key") or ""),
            code=str(v.get("code") or ""),
            message=str(v.get("message") or ""),
            level=str(v.get("level") or "error"),
            row_index=v.get("row_index"),
        )
        for v in [*(row.validation_errors or []), *(row.validation_warnings or [])]
    )
    return MappedListing(
        rows=tuple(dict(r) for r in payload.get("rows") or ()),
        header=dict(payload.get("header") or {}),
        violations=violations,
    )


def draft_preview(session: Session, product: Product, row: ListingDraft) -> dict:
    """字段预览(FE-232):每个字段带值、来源、状态与问题。"""
    # 预览的是**这一行草稿**,spec 按草稿自己的 category_id 取 ——
    # 存量草稿是 "swimwear",受众确认后的新草稿是 women_/men_ 前缀。
    # 按商品重新派生的话,确认受众之后、重建草稿之前的那个窗口里,
    # 预览会用新 spec 去解释旧 payload,列头对不上值
    spec = generic.field_spec(category_id=row.category_id)
    mapped = _mapped_from_row(row)
    preview = export_writer.preview_tables(
        mapped, spec, violations=mapped.violations
    )
    # ---- 颜色→SKU→图片(AC-19 的另一半,A45-batch22)----
    #
    # 读**落库的那一份**(`row.color_sku_image_map`),不重新推断。
    # 重新推断的预览永远自洽:它显示"当前该用哪些图",而导出用的是草稿
    # 生成那一刻存下来的那些 —— 上游变过之后两者不是一回事,
    # 于是运营在预览里看到红色有主图、导出出来那一行是空的,两边都不报错。
    preview["images"] = export_preview.image_preview(row.color_sku_image_map)
    return preview


def stale_reason(
    session: Session, product: Product, row: ListingDraft, *, dry_run: bool = False
) -> dict[str, Any]:
    """BE-205:过期详情。不过期时 changes 为空。

    `dry_run=True`:只读接口用(评审第 19 条)。「为什么过期」是一个
    纯粹的查询,回答它不该顺手改一行状态。
    """
    is_stale, changes = refresh_draft(session, product, row, dry_run=dry_run)
    return {
        "stale": is_stale,
        "status": row.status,
        "changes": stale_rules.serialize(changes),
    }


def draft_file_facts(
    session: Session, product: Product, *, dry_run: bool = False
) -> dict[str, Any]:
    """OPS-REVIEW P4:判断"上周导的文件还能不能传"所需要的三个事实。

    **过期与否一律走 `refresh_draft`**,不让批次页凭指纹自己推断一遍。
    §3.4 禁止的两个数字在这里的具体形态是:批次页说"3 件过期"、
    草稿页说"5 件过期" —— 只要判定在两个地方各写一遍,迟早出现。

    A6:`dry_run=True` 时只算不写。只读接口(GET file-audit)走这一条 ——
    "打开一次批次页顺带把状态刷新到最新"听起来是好事,但它让一个 GET
    产生数据库写入:浏览器预取、重试、刷新都会改业务状态。
    需要把 STALE 结论落库时,由明确的写路径(列表页/详情页)完成。
    """
    row = _current_draft(session, product.spu)
    if row is None:
        return {
            "draft_exists": False,
            "draft_stale": False,
            "current_fingerprint": None,
        }
    is_stale, _ = refresh_draft(session, product, row, dry_run=dry_run)
    return {
        "draft_exists": True,
        "draft_stale": is_stale,
        "current_fingerprint": row.source_fingerprint,
    }


def audience_gate_warnings(product: Product, row: ListingDraft | None) -> list[str]:
    """受众闸口的**警告**(不阻断的那一档)。

    `draft_audience_gate` 一直返回 `warnings`(存量商品"受众未确认"那一档),
    但 `export_gate` 只读了 `problems` —— 于是这半边结论**从来没有出口**。
    它的唯一消费者是界面:那句话要说给准备点导出的人听,告诉他这批文件
    是按受众前时代的规则包导的,确认受众之后要重新生成草稿。

    没有草稿行时返回空:警告是关于"这份草稿用了哪个规则包"的,
    草稿都还没有就无从说起。
    """
    if row is None:
        return []
    gate = audience_gate.draft_audience_gate(product.audience, row.category_id)
    return list(gate.warnings)


def export_gate(
    session: Session, product: Product
) -> tuple[ListingDraft, MappedListing]:
    """导出前的三道闸:草稿存在、指纹未过期、状态可导出。

    **单件导出与批量导出必须共用这一份实现**(阶段 3 的 `batch_service`)。
    抄一份到批量路径上的后果是:界面上单件导出正确地拒绝了过期草稿,
    而批量导出把同一份过期草稿写进了 50 件的文件里 ——
    §4.5.1「过期草稿一律禁止导出」在两条路径上必须是同一句话。

    通过后返回草稿行与已映射的字段。`mapped` 与页面预览读同一份
    `mapped_payload`,这是「导出一致率 100%」的来源(见 export_writer 模块注释)。
    """
    row = _current_draft(session, product.spu)
    if row is None:
        raise NotFoundError("还没有生成上架草稿")

    is_stale, changes = refresh_draft(session, product, row)
    if is_stale:
        heads = ";".join(c.message for c in changes[:3]) or "上游数据已变化"
        raise ValidationError(
            f"草稿已过期,禁止导出:{heads}。请重新生成草稿",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    if row.status not in draft_rules.PUBLISHABLE_STATUSES:
        raise ValidationError(
            f"当前状态 {row.status} 不允许导出;先修复校验问题并重新生成草稿",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    # ---- 第四道闸(PRD v2 §17 第 1、3 条):受众一致性 ----
    #
    # 它拦的正是 §0.2 那条 CATEGORY_ID 缺陷的后果:草稿用女装 spec 构造、
    # 商品是男装、字段校验全过。判定矩阵(含存量商品的兼容缝)在
    # audience_rules 模块文档里,这里只执行不解释。**单件与批量共用这一处**,
    # 与前三道闸同一个理由。第 2 条(图片模特受众)的接线现状也在那份文档里。
    gate = audience_gate.draft_audience_gate(product.audience, row.category_id)
    if not gate.ok:
        raise ValidationError(
            "受众校验未通过:" + ";".join(gate.problems),
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    return row, _mapped_from_row(row)


def record_export(
    session: Session,
    product: Product,
    row: ListingDraft,
    *,
    actor: str,
    fmt: str,
    now: datetime,
    batch_id: str | None = None,
) -> None:
    """导出留痕。单件与批量共用。

    `batch_id` 进审计 payload —— §6.3 最后一条「批量导出结果可定位到具体商品
    和原始批次」的后半句就是它。少了它,导出历史里只看得到"某人某时导过",
    看不出那次是 50 件批次的一部分。
    """
    delivered = fmt in pf.DELIVERY_EXPORT_FORMATS
    if delivered:
        row.exported_at = now.replace(tzinfo=None)
        row.exported_by = actor
        row.export_count = (row.export_count or 0) + 1
    # A45-#35:**比对用的 CSV 不动这三列。**
    #
    # `export_count` 要回答的是"交付给平台的那份文件导出过几次",而 CSV
    # 前端明写着"不作为交付给平台的文件"。两种产物混进一个计数之后,这个数
    # 既不能用来查重复交付,也不能用来查"改完重导过没有"。
    #
    # `exported_at` 同理:它是驳回定位在"审计没带版本"时的回退,指向一次
    # 预览导出会把定位引到错的那一版上。
    #
    # 审计仍然照记(动作名是 `export_preview`)—— 谁在什么时候导了一份比对
    # 文件,这件事本身要留痕,只是它不构成交付证据。
    session.flush()
    # payload 的形状由 platform.export_audit_payload 定义,不在这里手拼:
    # 读它的 `platform.export_entry` 就在同一个文件里。P1 的「驳回定位到哪一版」
    # 全靠这里写下的 components —— 少写一次,那件商品的驳回就永远"版本不可考"
    payload = pf.export_audit_payload(
        fmt=fmt,
        spu=product.spu,
        fingerprint=row.source_fingerprint,
        components=dict((row.canonical_snapshot or {}).get("components") or {}),
        export_count=row.export_count or 0,
        batch_id=batch_id,
    )
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="ListingDraft",
        entity_id=row.id,
        payload=payload,
    )


def export_draft(
    session: Session,
    product: Product,
    *,
    fmt: str = "xlsx",
    actor: str,
) -> tuple[bytes, str, str]:
    """导出上架文件(FE-235,阶段 2 仅 Excel;CSV 留给比对脚本用)。

    三道闸在 `export_gate`,批量导出走同一份。
    """
    row, mapped = export_gate(session, product)

    # 导出文件里的类目 = 刚过闸的那行草稿的类目(阶段 0 修复:可追溯到
    # 具体商品,不来自模块常量)。spec 同源,导出列头才和草稿 payload 对得上
    spec = generic.field_spec(category_id=row.category_id)
    now = datetime.now(UTC)
    meta = export_writer.ExportMeta(
        spu=product.spu,
        channel=generic.CHANNEL,
        site=generic.SITE,
        category_id=row.category_id,
        spec_version=row.spec_version,
        draft_id=str(row.id),
        fingerprint=row.source_fingerprint,
        exported_at=now,
        exported_by=actor,
    )

    if fmt == "xlsx":
        payload = export_writer.to_xlsx(mapped, spec, meta)
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        suffix = "xlsx"
    elif fmt == "csv":
        payload = export_writer.to_csv(mapped, spec).encode("utf-8-sig")
        mime = "text/csv; charset=utf-8"
        suffix = "csv"
    else:
        raise ValidationError(
            f"不支持的导出格式 {fmt!r};阶段 2 支持 xlsx / csv",
            code=ErrorCode.INPUT_INVALID,
        )

    record_export(session, product, row, actor=actor, fmt=suffix, now=now)
    filename = f"{product.spu}-{generic.CHANNEL}-{now.strftime('%Y%m%d-%H%M%S')}.{suffix}"
    return payload, filename, mime
