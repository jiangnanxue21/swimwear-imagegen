"""图片集规则(M5,§7.3)。**零依赖纯函数。**

这一层回答四个问题:

    解析     给定 (渠道, 站点),该用哪一版图片集
    校验     这一版能不能发布
    主图     每个变体的主图是哪张
    降级     素材被隔离时,引用它的已批准集该怎么办

全是判定,没有 I/O。图片编排的错误(主图缺失、变体没图、拿一张模型猜的
角色当主图)在平台那边的表现通常是「审核不通过」加一句含混的理由,
所以它必须能在本地被穷举测试出来。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.core.enums import ImageSetStatus, RoleSource


class ImageSetViolation(StrEnum):
    """图片集问题码。进 `FieldViolation.code`,也进驳回回流的映射。"""

    MISSING_PRIMARY_FOR_VARIANT = "MISSING_PRIMARY_FOR_VARIANT"
    MULTIPLE_PRIMARY_FOR_VARIANT = "MULTIPLE_PRIMARY_FOR_VARIANT"
    MISSING_IMAGE_FOR_VARIANT = "MISSING_IMAGE_FOR_VARIANT"
    #: 角色是模型猜的、把握不足,不能当主图
    UNTRUSTED_PRIMARY_ROLE = "UNTRUSTED_PRIMARY_ROLE"
    #: 引用了被隔离/未就绪的素材
    UNUSABLE_ASSET = "UNUSABLE_ASSET"
    EMPTY_SET = "EMPTY_SET"
    # ---- PRD v3.1 §6.5(BLOCK-02 挂起所缺的那个业务决定,本期定死) ----
    #: SPU 通用图占了某个颜色的主图位。§6.5:「颜色主图只能来自
    #: `variant_id = 该颜色` 的条目」
    SHARED_IMAGE_AS_PRIMARY = "SHARED_IMAGE_AS_PRIMARY"
    #: 通用图混进了颜色图位却没有被明确标记"通用"。§6.5:「必须由运营在
    #: 图片集编辑器中明确标记"通用",默认不混入」
    UNMARKED_SHARED_IMAGE = "UNMARKED_SHARED_IMAGE"
    #: 图片集里出现了不属于这个 SPU 的颜色标签。原来只在
    #: `image_set_service.variant_coverage()` 的 `unknown_tags` 里报,
    #: 是诊断不是门禁 —— 而一个指向已删除商品的标签会让覆盖率算出假的绿
    FOREIGN_VARIANT_IMAGE = "FOREIGN_VARIANT_IMAGE"
    #: 某个颜色缺 GenerationPlan 要求的角度。§6.5:「必要角度按
    #: `angles_json` 配置验收,**不是只数总张数**」
    MISSING_REQUIRED_ANGLE = "MISSING_REQUIRED_ANGLE"


#: 模型判定的角色要用于主图位所需的最低把握(§7.3)。
#:
#: 主图放错是消费者第一眼就看到的错误,而平铺图和模特图恰恰是
#: 角色识别最容易混的一对。
MODEL_ROLE_PRIMARY_MIN_CONFIDENCE = 0.9


@dataclass(frozen=True)
class ItemView:
    """参与判定的一项。

    刻意做得很窄:只有规则真正要用的字段。传 ORM 对象进来的话,
    这个模块就会开始依赖数据库的形状,而它的全部价值在于不依赖。
    """

    media_asset_id: str
    role: str
    sort_order: int
    variant_id: str | None = None
    is_primary: bool = False
    enabled: bool = True
    #: 素材侧的状态与角色可信度
    asset_status: str = "READY"
    asset_role_source: str = RoleSource.HUMAN.value
    asset_role_confidence: float | None = None
    #: 运营在编辑器里明确勾了"这张通用图可以混进颜色附图"(§6.5)。
    #:
    #: **默认 False**,也就是"默认不混入"。反过来默认 True 的写法在今天
    #: 看不出差别(库里的图 `variant_id` 全是 NULL),而颜色绑定入口一上线,
    #: 每个颜色的附图位都会自动灌满所有通用图 —— 那不是混排规则,是没有规则。
    shared_opt_in: bool = False
    #: 这一项覆盖的拍摄角度(`ImageAngle` 取值)。候选入集时继承生成任务,
    #: 空表示"没标注" —— 没标注的图不算覆盖任何角度,见 `coverage()`
    angle: str | None = None


@dataclass(frozen=True)
class SetView:
    image_set_id: str
    spu: str
    version: int
    status: str
    items: tuple[ItemView, ...] = ()
    channel: str | None = None
    site: str | None = None


@dataclass(frozen=True)
class Violation:
    code: ImageSetViolation
    message: str
    variant_id: str | None = None
    media_asset_id: str | None = None


# ---------------------------------------------------------------- 解析


def resolve_set(
    candidates: Sequence[SetView], *, channel: str | None, site: str | None
) -> SetView | None:
    """按 `(channel, site)` 专用 → `(channel, NULL)` → `(NULL, NULL)` 解析。

    每一级取**已批准的最高版本**。找不到返回 None。

    为什么是三级而不是"最匹配的一个":SHEIN 的巴西站可能需要不同的图片
    组合,但绝大多数 SPU 全站点通用。三级回落让"只为个别站点做特例"
    成为可能,而不必给每个站点都维护一套。
    """
    approved = [s for s in candidates if s.status == ImageSetStatus.APPROVED.value]
    for want_channel, want_site in ((channel, site), (channel, None), (None, None)):
        matched = [
            s for s in approved if s.channel == want_channel and s.site == want_site
        ]
        if matched:
            return max(matched, key=lambda s: s.version)
    return None


# ---------------------------------------------------------------- 校验


@dataclass(frozen=True)
class VariantCoverage:
    """一版图片集对某个颜色的覆盖情况(§6.5)。

    **它同时是诊断和门禁的输入。**`image_set_service.variant_coverage()`
    以前把覆盖算了一遍供界面看,`validate_set` 里又用另一段推导式判
    "有没有图" —— 两处各算一次的表现是:界面说缺一个颜色、批准却通过了,
    而两边都不报错。现在只有这一处算,`validate_set` 从它派生违规。
    """

    variant_id: str
    #: 绑定到这个颜色的启用项
    owned: tuple[ItemView, ...] = ()
    #: 被明确标记为通用、可以给这个颜色当附图的项
    shared: tuple[ItemView, ...] = ()
    #: 这个颜色缺的角度(GenerationPlan 要求了但没有对应项)
    missing_angles: frozenset[str] = frozenset()

    @property
    def has_own_image(self) -> bool:
        return bool(self.owned)

    @property
    def primary(self) -> ItemView | None:
        for item in self.owned:
            if item.is_primary:
                return item
        return None

    @property
    def covered_angles(self) -> frozenset[str]:
        return frozenset(i.angle for i in self.owned if i.angle)


def coverage(
    image_set: SetView,
    *,
    variant_ids: Iterable[str] = (),
    required_angles: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, VariantCoverage]:
    """逐颜色算覆盖。**通用图不算覆盖**(§6.5)。

    这是本批把 `variant_coverage` 从诊断变成门禁的那一刀。原来那条放行是:

        if variant not in covered and None not in covered:   # 有通用图就算覆盖

    §6.5 明写「不得回退使用其他颜色的图片,缺图就是缺图(BLOCKED)」,
    通用图同理 —— 它只能进附图位。留着那条放行的话,一个九色 SPU 只要有
    一张通用图就永远"覆盖完整",而红色 SKU 会挂着一张黑色主图上架。

    `required_angles` 按颜色给,来自该颜色**当前生效**的 GenerationPlan
    (`workflows/generation_plan.effective_fingerprints` 那一套的同一份方案)。
    不传就不查角度 —— 与 `variant_ids` 传空的口径一致:少一个维度的检查
    要能被显式表达,否则接线未完成的阶段会被迫在"全查"和"全不查"之间选。
    """
    enabled = [i for i in image_set.items if i.enabled]
    wanted = {str(v) for v in variant_ids if str(v or "").strip()}
    angles = {str(k): frozenset(str(a) for a in v) for k, v in (required_angles or {}).items()}

    out: dict[str, VariantCoverage] = {}
    for variant in sorted(wanted):
        owned = tuple(i for i in enabled if i.variant_id == variant)
        shared = tuple(i for i in enabled if i.variant_id is None and i.shared_opt_in)
        have = frozenset(i.angle for i in owned if i.angle)
        out[variant] = VariantCoverage(
            variant_id=variant,
            owned=owned,
            shared=shared,
            missing_angles=frozenset(angles.get(variant, frozenset())) - have,
        )
    return out


def validate_set(
    image_set: SetView,
    *,
    variant_ids: Iterable[str] = (),
    required_angles: Mapping[str, Iterable[str]] | None = None,
) -> list[Violation]:
    """校验一版图片集能不能发布。

    ``variant_ids`` 是该 SPU 需要覆盖的颜色变体。**空集合表示"不做颜色级
    检查"** —— 单色 SPU 与颜色绑定入口尚未接线的调用点走这条路,§6.5 的
    混排规则在那种情况下没有意义(没有"别的颜色"可以混)。

    ``required_angles`` 见 `coverage()`。
    """
    problems: list[Violation] = []
    enabled = [i for i in image_set.items if i.enabled]

    if not enabled:
        return [Violation(ImageSetViolation.EMPTY_SET, "图片集里没有启用中的图片")]

    # 只有 READY 的素材能入集
    for item in enabled:
        if item.asset_status != "READY":
            problems.append(
                Violation(
                    ImageSetViolation.UNUSABLE_ASSET,
                    f"素材 {item.media_asset_id} 当前状态是 {item.asset_status},不能用于发布",
                    media_asset_id=item.media_asset_id,
                )
            )

    # 主图:每个出现过的变体(含 SPU 通用)有且只有一张
    by_variant: dict[str | None, list[ItemView]] = {}
    for item in enabled:
        by_variant.setdefault(item.variant_id, []).append(item)

    for variant, items in by_variant.items():
        primaries = [i for i in items if i.is_primary]
        if not primaries:
            problems.append(
                Violation(
                    ImageSetViolation.MISSING_PRIMARY_FOR_VARIANT,
                    f"变体 {variant or 'SPU 通用'} 没有主图",
                    variant_id=variant,
                )
            )
        elif len(primaries) > 1:
            problems.append(
                Violation(
                    ImageSetViolation.MULTIPLE_PRIMARY_FOR_VARIANT,
                    f"变体 {variant or 'SPU 通用'} 有 {len(primaries)} 张主图",
                    variant_id=variant,
                )
            )
        for primary in primaries:
            if not can_be_primary(primary):
                problems.append(
                    Violation(
                        ImageSetViolation.UNTRUSTED_PRIMARY_ROLE,
                        f"素材 {primary.media_asset_id} 的角色是模型判定的且把握不足 "
                        f"{MODEL_ROLE_PRIMARY_MIN_CONFIDENCE},不能当主图;"
                        "请人工确认角色后再设为主图",
                        variant_id=variant,
                        media_asset_id=primary.media_asset_id,
                    )
                )

    problems.extend(_section_6_5(image_set, enabled, variant_ids, required_angles))
    return problems


def _section_6_5(
    image_set: SetView,
    enabled: Sequence[ItemView],
    variant_ids: Iterable[str],
    required_angles: Mapping[str, Iterable[str]] | None,
) -> list[Violation]:
    """§6.5 的颜色图片规则。**只在这个 SPU 真的有颜色时生效。**

    单独一个函数是为了让"哪几条属于 §6.5"这件事在代码里能被指出来 ——
    混在 `validate_set` 里的话,将来有人放宽其中一条时,不会意识到自己
    动的是一个写在 PRD 里的业务决定(BLOCK-02 挂了几个版本才等到它)。
    """
    wanted = [str(v) for v in variant_ids if str(v or "").strip()]
    if not wanted:
        return []

    problems: list[Violation] = []
    known = set(wanted)

    # 一、不属于本 SPU 的颜色标签
    for item in enabled:
        if item.variant_id is not None and item.variant_id not in known:
            problems.append(
                Violation(
                    ImageSetViolation.FOREIGN_VARIANT_IMAGE,
                    f"图片 {item.media_asset_id} 标着颜色 {item.variant_id},"
                    "而这个 SPU 没有这个颜色",
                    variant_id=item.variant_id,
                    media_asset_id=item.media_asset_id,
                )
            )

    # 二、通用图不能当主图,未标记的通用图不许混入
    for item in enabled:
        if item.variant_id is not None:
            continue
        if item.is_primary:
            problems.append(
                Violation(
                    ImageSetViolation.SHARED_IMAGE_AS_PRIMARY,
                    f"图片 {item.media_asset_id} 是 SPU 通用图,不能当颜色主图;"
                    "颜色主图只能来自该颜色自己的图",
                    media_asset_id=item.media_asset_id,
                )
            )
        elif not item.shared_opt_in:
            problems.append(
                Violation(
                    ImageSetViolation.UNMARKED_SHARED_IMAGE,
                    f"图片 {item.media_asset_id} 是 SPU 通用图,"
                    "需要在编辑器里明确标记为「通用」才会混入颜色附图",
                    media_asset_id=item.media_asset_id,
                )
            )

    # 三、逐颜色的覆盖与角度
    for variant, cover in coverage(
        image_set, variant_ids=wanted, required_angles=required_angles
    ).items():
        if not cover.has_own_image:
            problems.append(
                Violation(
                    ImageSetViolation.MISSING_IMAGE_FOR_VARIANT,
                    f"颜色变体 {variant} 没有任何属于它自己的图片"
                    "(通用图不算覆盖,不得回退使用其他颜色的图片)",
                    variant_id=variant,
                )
            )
            continue
        for angle in sorted(cover.missing_angles):
            problems.append(
                Violation(
                    ImageSetViolation.MISSING_REQUIRED_ANGLE,
                    f"颜色变体 {variant} 缺生成方案要求的角度 {angle}",
                    variant_id=variant,
                )
            )
    return problems


def can_be_primary(item: ItemView) -> bool:
    """能不能放主图位(§7.3)。

    角色是模型猜的、且把握不足 0.9 时不行。人工或规则指定的不看置信度 ——
    人不会给自己的判断打分,而"规则"是文件名/上传位这类确定性依据。
    """
    if not item.enabled or item.asset_status != "READY":
        return False
    if item.asset_role_source == RoleSource.MODEL.value:
        return (item.asset_role_confidence or 0.0) >= MODEL_ROLE_PRIMARY_MIN_CONFIDENCE
    return True


def primary_for(image_set: SetView, variant_id: str | None) -> ItemView | None:
    """取某个变体的主图。**不回退。**

    ## 这里改过一次行为,而且是刻意的

    原来的写法是 `for want in (variant_id, None)` —— 颜色没有专属主图时
    回落到 SPU 通用图。§6.5 把 BLOCK-02 那个挂了几版的业务决定定死了:

        颜色主图与颜色附图只能来自 `variant_id = 该颜色` 的条目;
        不得回退使用其他颜色的图片,缺图就是缺图(BLOCKED)。

    回退是"看起来更友好"的那一侧,而它的具体后果是**红色 SKU 挂着黑色主图
    上架** —— 因为在颜色绑定入口上线之前,通用图就是第一个颜色的图。
    缺图必须表现成缺图,由 `validate_set` 的 `MISSING_IMAGE_FOR_VARIANT`
    在批准这一步拦住,而不是在发布时用另一张图顶上。

    `variant_id=None` 仍然取通用主图:那是单色 SPU 与 SPU 级展示位的用法。
    """
    for item in image_set.items:
        if item.enabled and item.is_primary and item.variant_id == variant_id:
            return item
    return None


def ordered_items(image_set: SetView, variant_id: str | None = None) -> list[ItemView]:
    """按发布顺序排。主图永远第一,其余按 sort_order。

    平台通常按传入顺序决定展示位,所以"主图第一"不是显示层的事,
    是数据层就要保证的。
    """
    if variant_id is None:
        items = [i for i in image_set.items if i.enabled]
    else:
        # §6.5:通用图**只能出现在附图位**,且必须被明确标记"通用"。
        # 原来的写法是 `i.variant_id in (variant_id, None)` —— 无条件混入。
        items = [
            i
            for i in image_set.items
            if i.enabled
            and (
                i.variant_id == variant_id
                or (i.variant_id is None and i.shared_opt_in and not i.is_primary)
            )
        ]
    return sorted(items, key=lambda i: (not i.is_primary, i.sort_order, i.media_asset_id))


# ---------------------------------------------------------------- 降级


def needs_downgrade(image_set: SetView) -> bool:
    """已批准的集里出现了不可用素材,是否应当降级为待复核(§7.3)。

    素材被隔离时(水印、logo、露出),引用它的已批准集**自动降级**
    并告警。不降级的话,一张已经被判定为不合规的图会继续被发布 ——
    而隔离动作看起来是成功的。
    """
    if image_set.status != ImageSetStatus.APPROVED.value:
        return False
    return any(i.enabled and i.asset_status != "READY" for i in image_set.items)


def next_version(existing: Sequence[SetView]) -> int:
    """下一个版本号。

    图片集不可原地修改:批准后任何改动都生成新版本。
    """
    return max((s.version for s in existing), default=0) + 1
