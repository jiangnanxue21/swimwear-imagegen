"""通用上架模板适配器(任务书阶段 2)。

一个渠道、一个站点、一个泳衣类目、一个模板 —— 任务书 §1 的首期范围。

## 它和 SHEIN 适配器的关系

`app/channels/shein/` 那份 spec 整份是 `TODO_`,加载即拒绝,因为官方模板
和 OpenAPI 文档还没拿到(硬规矩 1)。等文档到手,那边会写自己的 spec 和
字典表,**不会回来改这里**。

这一份用的是我们自己定义的字段名和列头。它不冒充任何平台,但它是真的:
运营用它导出的 Excel 是一份内容完整、字段可追溯、和页面预览逐格一致的文件。
阶段 2 的验收要的正是这个。

## 纯函数

`map_fields` / `validate` 不查库、不发网络、不读文件(spec 由调用方传入)。
字段映射是最需要穷举测试的部分,而只要它能查库,就没人会写那个穷举测试。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.attributes.contracts import AttrValue
from app.channels.base import (
    ChannelFieldSpec,
    FieldSpec,
    PreparedRequest,
    PublishOperationRef,
)
from app.channels.source_expr import (
    MISSING,
    ResolveContext,
    SourceExprError,
    parse_source,
    resolve,
)
from app.channels.spec_loader import load_spec_file
from app.core.enums import PublishOperation
from app.listings.contracts import (
    FieldViolation,
    ImageItemSnapshot,
    ListingDraftData,
    MappedListing,
)

#: 首期范围。四个「一个」写在同一个地方,不散落在各处字符串里。
CHANNEL = "GENERIC"
SITE = "MAIN"
#: **受众前时代的兜底类目,不再是草稿构造的直接来源。**
#:
#: PRD v2 §0.2 点名的缺陷:workbench 构造上架草稿的五个地方曾直接取这个
#: 模块常量,不看 `Product.category` —— 库里只有一个品类时不出问题,加了
#: 男装之后,一件男泳裤会用女装 spec 静默导出成功(spec 校验的是"这份 spec
#: 的必填字段填了没有",而男泳裤确实可以在"领型"留空时通过一份不要求领型
#: 的 spec)。**草稿的类目一律走 `category_id_for(product)`**;这个常量只
#: 保留给两处:受众未确认商品的派生兜底(见 core/audience.category_code_for)
#: 和环境探针(registry.build_facet 只关心"spec 加载得动吗",与具体商品无关)。
CATEGORY_ID = "swimwear"
LOCALE = "en"

SPEC_DIR = Path(__file__).parent / "spec"


def category_id_for(product) -> str:
    """这件商品该用哪个规则包(阶段 0 修复 + §18.1 派生)。

    从 `product.audience + product.category` 派生,**派生逻辑只在
    `core/audience.category_code_for` 一处**(§23.5),这里只是把商品行
    的两列递进去。受众与品类码里编入的前缀不一致时它会抛
    `AudienceConflictError` —— 以 audience 为准并报错,不静默取其一。

    参数刻意不做类型标注成 `Product`:channels 层是纯函数层(见模块
    docstring),不 import ORM 模型;鸭子类型只要求 `.audience` / `.category`
    两个属性,测试可以用任何简单对象。
    """
    from app.core.audience import category_code_for

    return category_code_for(
        getattr(product, "audience", None), getattr(product, "category", CATEGORY_ID)
    )

#: 字段来源的中文说明。草稿页每个字段都要显示「这个值是哪来的」
#: (任务书 FE-232:每个字段来源清楚)。
SOURCE_KIND_LABELS: Mapping[str, str] = {
    "product": "商品库",
    "attr": "已确认属性",
    "variant": "变体字段",
    "copy": "文案",
    "images": "已批准图片集",
    "manual": "运营手填",
    "const": "模板常量",
}


def field_spec(*, category_id: str = CATEGORY_ID, site: str = SITE) -> ChannelFieldSpec:
    """加载字段规格。**每次都重新校验** —— spec 是外部文件,可能被改坏。"""
    return load_spec_file(
        SPEC_DIR / f"{category_id}.yaml", site=site, category_id=category_id
    )


# ---------------------------------------------------------------- 变换


def _apply_transform(value: Any, transform: Mapping[str, Any]) -> Any:
    """执行一条已通过白名单校验的变换。

    只处理本模板实际用到的几种。碰到白名单里有、这里没实现的,
    **抛错而不是原样返回** —— 静默跳过一个 transform 的表现是
    「标题没被截断」,而那要到平台驳回时才会被发现。
    """
    name = transform.get("name")
    if value is MISSING or value is None:
        return value
    if name == "truncate":
        limit = int(transform["max_length"])
        text = str(value)
        return text[:limit]
    if name == "join":
        sep = str(transform["separator"])
        if isinstance(value, (list, tuple)):
            return sep.join(str(v) for v in value if str(v).strip())
        return str(value)
    if name == "upper":
        return str(value).upper()
    if name == "lower":
        return str(value).lower()
    if name == "pad":
        length = int(transform["length"])
        fill = str(transform.get("fill", "0"))
        return str(value).rjust(length, fill)
    if name == "map":
        # 字典表要从 dict/<name>.yaml 读。本模板没有用到映射字典,
        # 所以这里显式不支持 —— 假装支持会产出一个原样未映射的值
        raise SourceExprError(
            "本模板未配置映射字典;需要 map 变换时先在 spec/dict/ 下建表"
        )
    raise SourceExprError(f"未实现的 transform:{name!r}")


def _resolve_field(spec: FieldSpec, ctx: ResolveContext) -> Any:
    if not spec.source:
        return MISSING
    value = resolve(parse_source(spec.source), ctx)
    for transform in spec.transforms:
        value = _apply_transform(value, transform)
    return value


def source_label(spec: FieldSpec) -> str:
    """给页面看的来源说明。"""
    if not spec.source:
        return "未配置"
    kind = spec.source.split(".", 1)[0]
    return SOURCE_KIND_LABELS.get(kind, kind)


# ---------------------------------------------------------------- 映射


def _copy_bucket(draft: ListingDraftData, locale: str) -> dict[str, Any]:
    snapshot = draft.copies.get(locale)
    if snapshot is None:
        return {}
    return {
        "title": snapshot.title,
        "description": snapshot.description,
        "bullet_points": list(snapshot.bullet_points),
        "keywords": list(snapshot.keywords),
    }


def _role_of(item: ImageItemSnapshot) -> str:
    return item.role.value if hasattr(item.role, "value") else str(item.role)


def _header_image_bucket(draft: ListingDraftData) -> dict[str, Any]:
    """SPU 级表头的角色 -> 图片路径。**只取 enabled 的项。**

    表头不在颜色→SKU→图片映射里(那份映射按颜色铺开,而表头是 SPU 级的),
    所以这一段仍然从图片集快照里取:通用图 -> 编辑序最靠前的任意图。

    表头的主图是必填的,而一个所有图都绑了颜色的图片集并不是配置错误 ——
    这时候取编辑序第一张,和 A45-batch24 之前的行为一致。**行级不再走
    这条路**,理由见 `_row_image_bucket`。
    """
    items = sorted(draft.image_set.enabled_items(), key=lambda i: i.sort_order)
    bucket: dict[str, Any] = {}

    def fill(candidates) -> None:
        for item in candidates:
            bucket.setdefault(_role_of(item), item.storage_path)

    fill(i for i in items if i.variant_id is None)
    fill(items)
    return bucket


def _row_image_bucket(
    draft: ListingDraftData,
    image_map: Mapping[str, Any],
    *,
    variant_id: str,
    sku: str,
) -> dict[str, Any]:
    """一行 SKU 的角色 -> 图片路径。**成员与顺序全部来自已存映射。**

    ## 这里改过一次行为,而且它是 AC-19 的后半句(A45-batch24)

    原来这个桶自己从图片集快照里挑:该变体的图按 `sort_order` 取第一张,
    取不到就回落到 SPU 通用图。而**同一份草稿的另一半**
    (`color_sku_image_map`,预览读的那一份)是按另一套规则挑的:
    主图取显式 `is_primary` 的那一项,通用图只有勾了「可混入」才进附图位,
    而且**缺图就是缺图,不回落**(§6.5 / BLOCK-02)。

    两套规则各自都合理,合在一起就不成立:一个合法且 READY 的图片集里,
    只要显式主图不是排序第一张(两张正面图是最常见的形状),
    **预览显示 A 图、Excel 与 API 报文用 B 图**,而两边都不报错 ——
    这正是 AC-19 那句「导出预览与最终导出读取同一份已存映射」要防的结局。

    现在行级只做一件事:把映射里点名的素材 id 翻译成路径。挑哪些图、
    按什么顺序,答案只有一个来源。`primary` 排在 `extras` 之前,
    于是它先占住自己的角色位 —— 显式主图是正面图时(绝大多数情况),
    `variant_image: images.PRODUCT_FRONT` 拿到的就是预览里显示的那一张。

    ## 映射里没有这个 (颜色, SKU) 时返回空桶

    不回落到图片集、不借别的颜色的图。空桶的表现是那几个图片字段为空,
    被 `CHANNEL_FIELD_MISSING` 或颜色维 READY 门禁拦下来;而"补一张看起来
    合理的图"会让一行错图安静地进平台。§6.5 已经为同一件事定过调。
    """
    by_id = {i.media_asset_id: i for i in draft.image_set.enabled_items()}
    colour = (image_map.get("colors") or {}).get(variant_id) or {}
    cell = (colour.get("skus") or {}).get(sku) or {}

    bucket: dict[str, Any] = {}
    ordered = [cell.get("primary"), *(cell.get("extras") or [])]
    for asset_id in ordered:
        item = by_id.get(str(asset_id)) if asset_id else None
        if item is None:
            # 映射点名了一张这一版图片集里不存在(或已停用)的素材。
            # **跳过而不是报错**:映射是草稿生成那一刻的快照,而这里正在
            # 用它构造同一次草稿的字段 —— 两者不同步只可能是有人手改过库。
            # 少一张图会被必填校验与 READY 门禁看见,抛异常只会让整份草稿
            # 建不出来,而运营完全指不到原因
            continue
        bucket.setdefault(_role_of(item), item.storage_path)
    return bucket



def _confirmed_values(attrs: Mapping[str, AttrValue]) -> dict[str, Any]:
    """**只有 CONFIRMED 的属性进得来。**

    用 SUGGESTED 的值填平台字段,等于把一个还没人看过的模型猜测
    直接发布出去。这条限制在这里而不是在调用方,是因为它是映射层的
    不变量,不该依赖每个调用方都记得过滤。

    多值属性(`secondary_colors` 这类)折成逗号分隔的一格。**三层
    属性共用这一个函数**,不是三处各写一遍:原来 SKU 层是另一段
    行内推导式,既没过 `is_confirmed`、也没折多值,于是同一个属性
    挂在 SPU 上和挂在 SKU 上导出的格式不一样。
    """
    out: dict[str, Any] = {}
    for name, attr in attrs.items():
        if not attr.is_confirmed:
            continue
        value = attr.value
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        out[name] = value
    return out


def _attr_bucket(draft: ListingDraftData) -> dict[str, Any]:
    """SPU 级属性桶。表头字段的 `attr.*` 从这里取。"""
    return _confirmed_values(draft.product.confirmed_attrs())


def _variant_attr_bucket(draft: ListingDraftData, variant_id: str) -> dict[str, Any]:
    """颜色层属性桶。

    `CanonicalVariant.attrs` 在改动前全程没有任何读取方 —— 契约里
    建了这一层,映射层却直接跳过它,于是「这个颜色的图案是波点、
    那个颜色的是纯色」这类颜色级事实无论怎么确认都进不了导出文件。
    """
    for variant in draft.product.variants:
        if variant.variant_id == variant_id:
            return _confirmed_values(variant.attrs)
    return {}


def map_fields(
    draft: ListingDraftData,
    spec: ChannelFieldSpec,
    *,
    image_map: Mapping[str, Any],
) -> MappedListing:
    """把草稿映射成平台字段。**纯函数。**

    `header` 是 SPU 级字段,`rows` 是 SKU 行。Excel 与将来的 API 提交
    共用同一个结构 —— 这是「不允许表格里对、接口里错」那条目标的落点。

    ## `image_map` 是必需的关键字参数(AC-19,A45-batch24)

    它就是 `listing_drafts.color_sku_image_map` 的内容(形状见
    `workbench/upstream_snapshot.build_color_sku_image_map`)。行级图片字段
    **只能**从它翻译,不再自己从图片集里挑 —— 理由见 `_row_image_bucket`。

    做成必需而不是"给个默认值,不传就照旧":不传就照旧的写法会让这次
    修复只在记得传的调用点上生效,而漏传的地方**继续静默走老路**,
    表现和修复前一模一样。这与 `export_preview` 的签名里只有 `mapping`
    是同一手法 —— 把"重新推断"变成一件做不到的事,而不是一件不该做的事。

    传 `{}` 是合法的,含义是"这份草稿没有颜色维映射":行级图片字段全空,
    由必填校验与颜色维 READY 门禁去说明缺什么。
    """
    attrs = _attr_bucket(draft)
    images = _header_image_bucket(draft)
    copies = {loc: _copy_bucket(draft, loc) for loc in draft.copies}
    product = {
        "spu": draft.product.spu,
        "category_path": "/".join(draft.product.category_path),
    }

    header_ctx = ResolveContext(
        product=product,
        attrs=attrs,
        manual=draft.manual,
        consts=spec.consts,
        copies=copies,
        images=images,
        variant=None,
    )

    header: dict[str, Any] = {}
    violations: list[FieldViolation] = []
    for field in spec.header_fields:
        value = _resolve_field(field, header_ctx)
        header[field.key] = None if value is MISSING else value

    rows: list[dict[str, Any]] = []
    for sku in draft.product.skus:
        # 属性从粗到细叠三层,细的覆盖粗的:SPU -> 颜色 -> 颜色×尺码。
        # 三层都只放 CONFIRMED 的值 —— SKU 层原来没过这道滤网,而它是
        # 最后展开的一层,于是一个 SUGGESTED 的 SKU 属性不但自己流了出去,
        # 还**覆盖掉**了同名的已确认 SPU 值。表头一列显示已确认的值、
        # 同一份文件的 SKU 行显示模型的猜测,两边对不上。
        variant = {
            **attrs,
            **_variant_attr_bucket(draft, sku.variant_id),
            **_confirmed_values(sku.attrs),
            # 身份字段放最后:它们来自商品行本身,是这一行的定义,
            # 不该被任何一层属性覆盖。同名属性(比如有人往注册表里加了
            # 一个 `size`)覆盖掉真实尺码,导出的每一行都会是错的
            "sku": sku.sku,
            "variant_id": sku.variant_id,
            "size": sku.size,
            "size_group": sku.size_group,
            "gtin": sku.gtin,
        }
        row_ctx = ResolveContext(
            product=product,
            attrs=attrs,
            manual=draft.manual,
            consts=spec.consts,
            # 图片逐 SKU 从**已存映射**翻译,不共用表头那一份、也不重新挑
            # (见 `_row_image_bucket`:AC-19 的「预览与导出读同一份」)
            images=_row_image_bucket(
                draft, image_map, variant_id=sku.variant_id, sku=sku.sku
            ),
            copies=copies,
            variant=variant,
        )

        row: dict[str, Any] = {}
        for field in spec.row_fields:
            value = _resolve_field(field, row_ctx)
            row[field.key] = None if value is MISSING else value
        rows.append(row)

    return MappedListing(
        rows=tuple(rows), header=header, violations=tuple(violations)
    )


# ---------------------------------------------------------------- 校验


def _is_empty(value: Any) -> bool:
    if value is None or value is MISSING:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def _as_decimal(value: Any) -> Decimal | None:
    """把一个单元格值折成 `Decimal`,折不动返回 None。

    **数字字符串必须被接受。** 价格、库存、备货时效一律走 `manual`
    (见 `source_expr.py` 开头那条),而 `manual` 里装的是运营在页面上
    手填的东西 —— `price` 到这里就是字符串 `"39.90"`。只认 `int`/`float`
    的话,真实数据会被整批判成「不是数字」,而那正是这个校验最该放行的
    情况。

    `bool` 单独挡掉:它是 `int` 的子类,`Decimal(True)` 得到 1。
    一个「是/否」被当成数量 1 通过校验,比直接报错难查得多。

    `nan` / `Infinity` 也挡掉:`Decimal` 认这两个字面量,而它们和任何
    边界比较都返回 False,于是一个 `Infinity` 库存能干净地通过
    `minimum` / `maximum`。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, float):
        # 经字符串中转,不用 Decimal(float):后者会带出二进制浮点的尾巴
        parsed = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except InvalidOperation:
            return None
    else:
        return None
    return parsed if parsed.is_finite() else None


def _decimal_places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def _check_numeric(
    field: FieldSpec, value: Any, *, row_index: int | None
) -> list[FieldViolation]:
    """类型、区间与小数位(评审第 10 条)。只对声明了数值类型的字段生效。"""
    problems: list[FieldViolation] = []
    label = field.column_header or field.key
    parsed = _as_decimal(value)

    if parsed is None:
        return [
            FieldViolation(
                field_key=field.key,
                code="CHANNEL_FIELD_TYPE_INVALID",
                message=f"{label} 需要一个数字,当前是 {str(value)!r}",
                level="error",
                row_index=row_index,
            )
        ]

    if field.value_type == "integer" and parsed != parsed.to_integral_value():
        problems.append(
            FieldViolation(
                field_key=field.key,
                code="CHANNEL_FIELD_TYPE_INVALID",
                message=f"{label} 需要一个整数,当前是 {str(value)!r}",
                level="error",
                row_index=row_index,
            )
        )

    if field.minimum is not None and parsed < field.minimum:
        problems.append(
            FieldViolation(
                field_key=field.key,
                code="CHANNEL_FIELD_OUT_OF_RANGE",
                message=f"{label} 小于下限:{parsed} < {field.minimum}",
                level="error",
                row_index=row_index,
            )
        )
    if field.maximum is not None and parsed > field.maximum:
        problems.append(
            FieldViolation(
                field_key=field.key,
                code="CHANNEL_FIELD_OUT_OF_RANGE",
                message=f"{label} 超过上限:{parsed} > {field.maximum}",
                level="error",
                row_index=row_index,
            )
        )

    if field.decimal_places is not None:
        places = _decimal_places(parsed)
        if places > field.decimal_places:
            problems.append(
                FieldViolation(
                    field_key=field.key,
                    code="CHANNEL_FIELD_TOO_PRECISE",
                    message=(
                        f"{label} 小数位过多:{places}/{field.decimal_places}"
                        f"(当前值 {str(value)!r})"
                    ),
                    level="error",
                    row_index=row_index,
                )
            )
    return problems


def _check(
    field: FieldSpec, value: Any, *, row_index: int | None
) -> list[FieldViolation]:
    problems: list[FieldViolation] = []
    if _is_empty(value):
        if field.required:
            problems.append(
                FieldViolation(
                    field_key=field.key,
                    code="CHANNEL_FIELD_MISSING",
                    message=f"{field.column_header or field.key} 是必填字段,当前为空",
                    level="error",
                    row_index=row_index,
                )
            )
        return problems

    if field.is_numeric:
        problems.extend(_check_numeric(field, value, row_index=row_index))

    text = str(value)
    if field.max_length is not None and len(text) > field.max_length:
        problems.append(
            FieldViolation(
                field_key=field.key,
                code="CHANNEL_FIELD_TOO_LONG",
                message=(
                    f"{field.column_header or field.key} 超长:"
                    f"{len(text)}/{field.max_length}"
                ),
                level="error",
                row_index=row_index,
            )
        )
    if field.allowed_values and text not in field.allowed_values:
        problems.append(
            FieldViolation(
                field_key=field.key,
                code="CHANNEL_FIELD_NOT_ALLOWED",
                message=(
                    f"{field.column_header or field.key} 的值 {text!r} 不在允许范围内"
                ),
                level="error",
                row_index=row_index,
            )
        )
    return problems


def validate(mapped: MappedListing, spec: ChannelFieldSpec) -> list[FieldViolation]:
    """字段级校验(FE-233):必填、长度、枚举。**纯函数。**"""
    problems: list[FieldViolation] = []
    for field in spec.header_fields:
        problems.extend(_check(field, mapped.header.get(field.key), row_index=None))
    for index, row in enumerate(mapped.rows):
        for field in spec.row_fields:
            problems.extend(_check(field, row.get(field.key), row_index=index))

    if not mapped.rows:
        problems.append(
            FieldViolation(
                field_key="sku_code",
                code="CHANNEL_NO_ROWS",
                message="这份草稿一行 SKU 都没有,导出会是一张空表",
                level="error",
            )
        )
    return problems


def image_count_check(
    mapped: MappedListing, *, minimum: int = 1
) -> list[FieldViolation]:
    """图片数量检查(FE-233 点名的第四类)。

    单列出来是因为它的判据不在 spec 里:spec 只知道「主图必填」,
    不知道「这个类目至少要几张图」。
    """
    filled = sum(
        1
        for key in ("main_image", "back_image", "detail_image")
        if not _is_empty(mapped.header.get(key))
    )
    if filled < minimum:
        return [
            FieldViolation(
                field_key="main_image",
                code="CHANNEL_TOO_FEW_IMAGES",
                message=f"至少需要 {minimum} 张图片,当前 {filled} 张",
                level="error",
            )
        ]
    return []


def column_headers(spec: ChannelFieldSpec) -> tuple[Sequence[str], Sequence[str]]:
    """(SPU 列头, SKU 列头)。导出与页面预览共用它,列序永远一致。"""
    return (
        [f.column_header or f.key for f in spec.header_fields],
        [f.column_header or f.key for f in spec.row_fields],
    )


# ================================================================ 报文构造(任务 13)
#
# `build_request` 从 `submit` 里切出来的理由写在 `base.py` 顶部:v1 只有
# submit(),干跑就只能是"submit 里加一个 if",而那两条分支迟早长出差异 ——
# 干跑通过、真实提交报错,是这类设计最典型的失败方式。
#
# 切开之后干跑与真实提交走**同一段构造逻辑**,干跑产出的报文可以直接做
# 快照测试;submit() 只剩下"把这个报文发出去"。

#: 各操作对应的 HTTP 动作与路径。写成表而不是 if 链 —— 加一个操作时
#: 漏改一处的代价是"更新请求发到了创建端点",而平台会照单全收,
#: 于是同一个商品被创建了两次
_ROUTES: Mapping[str, tuple[str, str]] = {
    PublishOperation.CREATE.value: ("POST", "/v1/listings"),
    PublishOperation.UPDATE.value: ("PUT", "/v1/listings/{external_spu_id}"),
    PublishOperation.REPUBLISH.value: ("POST", "/v1/listings/{external_spu_id}/republish"),
    PublishOperation.DELIST.value: ("POST", "/v1/listings/{external_spu_id}/delist"),
}


def build_request(mapped: MappedListing, op: PublishOperationRef) -> PreparedRequest:
    """把映射结果构造成一个**还没发出去**的平台请求。

    纯函数:不查库、不发网络、不读文件。三条约束的理由同 `map_fields`。

    ## 三个不显然的决定

    **一、非创建操作必须已有 external_spu_id。** 少了它,路径里的占位符
    会被渲染成字面量 `{external_spu_id}`,请求发到一个不存在的地址,
    平台返回 404 —— 而 404 在这条链路上会被读成"商品不见了",
    于是有人去手工重建。当场抛错比那个结局好得多。

    **二、报文里带 `operation`。** 它已经在幂等键里了,再放一份进 body 是
    刻意的冗余:排查时看到的是报文快照,而快照里没有操作类型的话,
    "这次是创建还是更新"要靠猜路径。

    **三、`headers` 里不放任何凭证。** 签名在 `submit()` 里现算。
    这个对象会被脱敏后存进 `PublishAttempt.safe_request_snapshot`,
    而快照是会被人翻出来看的东西(见 7.5 节)。
    """
    operation = PublishOperation(str(op.operation).upper())
    method, path_tpl = _ROUTES[operation.value]

    if "{external_spu_id}" in path_tpl:
        if not op.external_spu_id:
            raise ValueError(
                f"{operation.value} 需要 external_spu_id,但它是空的 —— "
                "说明这个商品还没有成功创建过。先走 CREATE。"
            )
        path = path_tpl.format(external_spu_id=op.external_spu_id)
    else:
        path = path_tpl

    return PreparedRequest(
        method=method,
        path=path,
        query={},
        body={
            "operation": operation.value,
            "header": dict(mapped.header),
            "rows": [dict(r) for r in mapped.rows],
        },
        headers={"Content-Type": "application/json"},
        # 幂等键由调用方算好塞进 PublishOperationRef 之前的那一层 ——
        # 这里不算,因为算它需要 shop_id / site / 草稿指纹,
        # 而那些是运行期上下文,不属于"字段映射"这件事
        idempotency_key=None,
    )
