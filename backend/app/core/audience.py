"""受众判定的**唯一执行点**(PRD v2 §23.5)。

受众会在六处被用到:模特筛选、提示词选择、槽位表、检查项、必填属性、
平台字段。每一处都从 `product.audience` 读、经这个模块判 ——
不允许任何一处自己从 `category_code` 做字符串推断,也不允许前端缓存一份。
判断散落两处的后果 §18.1 说得很具体:`unisex_` 与 `women_` 的前缀匹配
迟早会写错,而且错的那一处不报错。

**纯标准库。** 与 `core/enums.py` 同一档:可被无三方依赖的纯测试直接导入。
"""
from __future__ import annotations

from app.core.enums import AUDIENCE_LABELS, Audience

#: category_code 里的受众前缀(§18.1)。**派生方向是单向的**:
#: `audience + garment_family -> category_code`。反向(从码里抠受众)只在
#: 一个地方允许 —— `embedded_audience_of`,而它的唯一用途是**校验一致性**,
#: 不是当作受众的来源。
_AUDIENCE_PREFIX: dict[Audience, str] = {
    Audience.WOMEN: "women_",
    Audience.MEN: "men_",
    Audience.UNISEX: "unisex_",
}


class AudienceConflictError(ValueError):
    """`audience` 字段与 `category_code` 里编入的受众前缀不一致。

    §18.1:两者不一致时**以 audience 为准并报错,不静默取其一**。
    静默取其一的话,取 audience 会让草稿指纹和 spec 文件名对不上,
    取前缀会让 §4.2 的"受众不被静默改写"变成空话 —— 两边都不能选,
    所以只能停下来让人看。
    """


def coerce(value: object) -> Audience | None:
    """把库里的字符串列(或已是枚举的值)变成 `Audience`。

    NULL / 空串 -> None,含义是"待确认受众"(§3.4 规则 3)。
    不认识的取值**抛错**而不是返回 None:一个写错的受众字符串被当成
    "没填"处理,后果是该商品静默走回旧的单受众路径,而没人会发现。
    """
    if value is None:
        return None
    if isinstance(value, Audience):
        return value
    text = str(value).strip()
    if not text:
        return None
    return Audience(text.upper())


def embedded_audience_of(category_code: str) -> Audience | None:
    """category_code 里编入的受众前缀。没有前缀 -> None(受众前时代的码)。

    **只用于一致性校验**,不用于取受众 —— 受众的事实源是商品字段(§18.1)。
    """
    for audience, prefix in _AUDIENCE_PREFIX.items():
        if category_code.startswith(prefix):
            return audience
    return None


def garment_family_of(category_code: str) -> str:
    """去掉受众前缀后的品类族。`women_swimwear` -> `swimwear`。"""
    embedded = embedded_audience_of(category_code)
    if embedded is None:
        return category_code
    return category_code[len(_AUDIENCE_PREFIX[embedded]) :]


def category_code_for(audience: object, category: str) -> str:
    """派生规则包键(§18.1):`audience + garment_family -> category_code`。

    - 受众未确认(None):返回品类族本身。这条草稿因此**可追溯到具体商品**
      (阶段 0 验收),但它走的是受众前时代的规则包 —— 提交闸口会按
      `workbench/audience_rules.py` 的矩阵拦下所有不一致组合。
    - 受众已确认:前缀 + 品类族。传入的 `category` 若已带前缀,先剥掉再拼,
      两边不一致则抛 `AudienceConflictError`(以 audience 为准并**报错**)。
    """
    resolved = coerce(audience)
    family = str(category or "").strip()
    embedded = embedded_audience_of(family)
    if embedded is not None:
        if resolved is not None and embedded is not resolved:
            # 措辞按 §20.4:给运营看的是中文受众,不是枚举值。
            # 这条会经 ValidationError 一路显示到界面上
            raise AudienceConflictError(
                f"商品受众是{AUDIENCE_LABELS[resolved]},"
                f"但规则包 {family} 属于{AUDIENCE_LABELS[embedded]};"
                "以商品受众为准,请重新生成草稿"
            )
        family = garment_family_of(family)
        if resolved is None:
            # 品类码带受众前缀、受众字段却是空 —— 同样是不一致,
            # 不能顺手把前缀里的受众"捡"进字段(§4.2:受众不被静默改写)
            raise AudienceConflictError(
                f"规则包 {_AUDIENCE_PREFIX[embedded]}{family} 属于"
                f"{AUDIENCE_LABELS[embedded]},但商品受众未确认;"
                "请先确认商品受众,再重新生成草稿"
            )
    if resolved is None:
        return family
    return f"{_AUDIENCE_PREFIX[resolved]}{family}"


# ---------------------------------------------------------------- §10.5 / §12.4


def model_matches_product(product_audience: object, model_audience: object) -> bool:
    """受众匹配是**硬约束,不是排序偏好**(§10.5)。

        商品 WOMEN  -> 只有 WOMEN 模特
        商品 MEN    -> 只有 MEN 模特
        商品 UNISEX -> WOMEN、MEN 均可(运营选择)

    商品受众未确认(None)时返回 True:候选集不过滤 —— 但那个商品会被
    工作台排去"待确认受众"一步(§8.2),而不是在这里替它猜一个受众。
    模特受众未标注(存量数据)同样放行,由授权待补清单收口,不在筛选层硬拦。
    """
    p = coerce(product_audience)
    m = coerce(model_audience)
    if p is None or m is None:
        return True
    if p is Audience.UNISEX:
        return m in (Audience.WOMEN, Audience.MEN)
    return p is m


def generation_block_reason(product_audience: object, model_audience: object) -> str | None:
    """生成前硬阻断(§12.4)。返回 None 表示放行,否则返回给运营看的理由。

    这条正常情况下走不到 —— §10.5 已经让不匹配的模特不出现在列表里 ——
    但它是后端的最后一道,**必须独立成立,不能依赖前端筛选**。
    措辞按 §20.4 的翻译口径,不吐 `AUDIENCE_MISMATCH` 原始码。
    """
    if model_matches_product(product_audience, model_audience):
        return None
    from app.core.enums import AUDIENCE_LABELS

    p = coerce(product_audience)
    m = coerce(model_audience)
    return (
        f"模特受众({AUDIENCE_LABELS[m]})与商品受众({AUDIENCE_LABELS[p]})不符,"
        "不能创建生成任务;请先在模特库中选择受众匹配的模特"
    )


def body_types_allowed(audience: object) -> tuple[str, ...]:
    """该受众可选的体型词(§10.4)。UNKNOWN 恒许;受众未确认时返回全集。"""
    from app.core.enums import BODY_TYPES_FOR_AUDIENCE, ModelBodyType

    resolved = coerce(audience)
    if resolved is None or resolved is Audience.UNISEX:
        # UNISEX 模特不存在(见 BODY_TYPES_FOR_AUDIENCE 注释),这个分支
        # 只服务"受众未定时先别拦"的存量路径
        return tuple(v.value for v in ModelBodyType)
    allowed = BODY_TYPES_FOR_AUDIENCE[resolved]
    return (*(v.value for v in allowed), ModelBodyType.UNKNOWN.value)
