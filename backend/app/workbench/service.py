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
from app.listings import (
    copy_generator,
    copy_service,
    export_writer,
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
from app.services import audit
from app.workbench import audience_rules as audience_gate
from app.workbench import flow as flow_rules
from app.workbench import platform as pf
from app.workbench import platform_service as ps
from app.workbench import stale as stale_rules
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
    rows = list(
        session.scalars(
            select(MediaAsset).where(
                MediaAsset.product_id == product.id,
                MediaAsset.status != MediaStatus.DELETED.value,
            )
        )
    )
    usable = [r for r in rows if r.status == MediaStatus.READY.value]
    usable_roles = frozenset(r.role for r in usable if r.role)
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
        # 颜色维**由素材自己带出来**,不传一份颜色清单 —— 与
        # `scope_fingerprint.fingerprints()` 同一条理由:清单漏了一个颜色时,
        # 那个颜色永远不会被判「缺图」,而漏了不会有任何征兆
        variant_gate_roles={
            variant: sample_completeness.variant_gate_roles(usable, variant)
            for variant in sorted(
                {
                    str(a.color_variant_id)
                    for a in usable
                    if getattr(a, "color_variant_id", None)
                }
            )
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
) -> tuple[ListingDraftData | None, list[str]]:
    """按**当前**上游组装草稿数据。前置不满足时返回 (None, 缺什么)。"""
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
        return None, problems

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
    return data, []


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

    current, problems = _current_draft_data(
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

    is_stale = draft_rules.is_stale(row.source_fingerprint, current)
    if not is_stale:
        return False, []

    if row.status not in (DraftStatus.STALE.value,) and not dry_run:
        row.status = DraftStatus.STALE.value
        session.flush()
    changes = stale_rules.diff_components(
        stored,
        _components_of(current, _attr_field_names(session, product, attr_values)),
    )
    return True, changes


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


def generate_copy(
    session: Session,
    product: Product,
    *,
    only_fields: Sequence[str] | None = None,
    actor: str,
) -> ListingCopy:
    """生成一版文案(FE-221 / FE-222 单字段重生)。

    `only_fields` 给了就只覆盖被点名的字段,其余保留上一版 ——
    合并规则在 `copy_generator.merge_fields`,claims 跟着标题走。
    """
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

    data, problems = _current_draft_data(session, product, manual=merged_manual)
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
    violations = [
        *generic.validate(mapped, spec),
        *generic.image_count_check(mapped, minimum=1),
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
    row.canonical_snapshot = {
        "components": _components_of(data, _attr_field_names(session, product)),
        "summary": draft_rules.summarize(data),
    }
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
    return export_writer.preview_tables(
        mapped, spec, violations=mapped.violations
    )


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
