"""属性字段注册表(M0)。

**这张表同时是三样东西的唯一来源**:

    识别 Schema        模型被允许输出哪些字段、每个字段的取值范围
    DSL 白名单         spec 里 `attr.<field>` 能引用什么
    阈值表             哪个字段用哪一组自动确认阈值

写成三份的代价很具体:spec 里引用了一个识别层根本不会输出的字段,
要到 M7 拿真实模板做映射时才会发现;而那时候改的是提示词、Schema、
阈值配置和 spec 四个地方。写成一份,
`tests/pure/test_m0_contracts.py::test_registry_rejects_self_inconsistent_fields`
在 CI 里就能把这类错误挡下来(原先在 `test_attribute_registry.py`,
文件已并入 m0 契约集;本行一度还指着旧名字)。

**纯数据。** 只 import `core/enums.py`,不碰数据库。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.core.enums import (
    Audience,
    BackStyle,
    ClosureType,
    FitType,
    GarmentType,
    InseamLength,
    LinerType,
    NecklineType,
    OwnerType,
    PatternType,
    PocketConfig,
    StrapType,
    WaistbandType,
)

#: 注册表 schema 版本。字段增删改要动它,落库的属性值上会记录当时的版本。
#: v2:受众条件化(applies_to / required_for)+ 男装字段组(PRD v2 §18.3/§18.4)。
ATTRIBUTE_SCHEMA_VERSION = "2"

#: 全受众。字段默认对所有受众适用 —— 显式写出来而不是用"空集表示全部",
#: 因为空集在 `required_for` 里的含义是"谁都不必填",两个空集含义相反,
#: 让同一个哨兵值背两种语义迟早会有人用反。
ALL_AUDIENCES: frozenset[Audience] = frozenset(Audience)


class ValueType(StrEnum):
    ENUM = "ENUM"
    ENUM_LIST = "ENUM_LIST"
    TEXT = "TEXT"
    #: 多值自由文本。颜色词表属于这一类 —— 平台的颜色词各不相同,
    #: 我们存自己的规范色名,归一化交给渠道层的 dict 映射。
    #: 硬做成枚举的话,每接一个新站点都要改一次 core/enums.py
    TEXT_LIST = "TEXT_LIST"
    NUMBER = "NUMBER"
    BOOL = "BOOL"


class AutoConfirm(StrEnum):
    """自动确认策略。

    `NEVER` 不是「阈值定得很高」,是**根本不参与自动确认**。
    材质、成分、克重、内衬这些字段看图看不出来 —— 让模型猜一个
    「82% 锦纶」出来,它会给得非常自信,而且没有任何图像证据能推翻它。
    """

    THRESHOLD = "THRESHOLD"
    NEVER = "NEVER"


#: 分层阈值(§6.4)。作用于 `system_confidence`,不是模型自报的 confidence。
THRESHOLD_GROUPS: Mapping[str, float] = {
    "identity": 0.95,     # 颜色、品类 —— 错了直接上错货
    "structure": 0.92,    # 领型、背部款式、肩带、袖型
    "decorative": 0.92,   # 图案、风格标签(且仅在库中为空时)
}

#: 永不自动确认的字段用这个组,只是为了让阈值表在结构上完整。
#: 真正的判定看 `auto_confirm_policy`,不看这个数。
NEVER_GROUP = "never"


@dataclass(frozen=True)
class AttributeField:
    """一个属性字段的完整声明。"""

    name: str
    #: 界面上叫什么。**没有默认值是刻意的** —— 新注册一个字段时,
    #: 忘了给中文名会在导入期直接 TypeError,而不是让 `waistband_type`
    #: 这种词悄悄出现在运营的属性表、颜色维明细和缺失清单里。
    #:
    #: 放在注册表而不是前端:同一个字段名会在四个地方被显示(属性页表格、
    #: 证据抽屉标题、颜色维的「缺:…」、批量结果里的字段清单),
    #: 而前端各翻一次的下场是同一个字段有四种中文名。
    label: str
    owner_type: OwnerType
    value_type: ValueType
    #: ENUM / ENUM_LIST 时指向 `core/enums.py` 里的枚举类
    enum: type[StrEnum] | None = None
    multi_value: bool = False
    #: 能不能从图片判断出来。**False 的字段不允许出现在识别 Schema 里。**
    visual: bool = True
    #: 该字段的可见性先验,进 `system_confidence`(§6.3)
    visibility_prior: float = 1.0
    auto_confirm_policy: AutoConfirm = AutoConfirm.THRESHOLD
    threshold_group: str = "structure"
    #: 是否投影到 `products` 表的兼容列(§5.4)。投影是**单向只读**的
    legacy_column: str | None = None
    #: 这个字段在哪些受众的商品上**存在**(PRD v2 §18.3)。
    #: 与 `visual=False` 是同一个道理的第二条:
    #: 看图看不出来的字段,禁止让模型猜;**这件商品上不存在的字段,同样禁止**。
    #: 问一个泳裤的"领型",模型会给一个非常自信的答案。
    #: 执行点与 visual 相同:`extractable_fields(audience)`。
    applies_to: frozenset[Audience] = ALL_AUDIENCES
    #: 生成刊登内容时,哪些受众必须有这个字段(替代旧的全局 bool
    #: `required_for_listing`,PRD v2 §18.3)。空集 = 谁都不必填。
    required_for: frozenset[Audience] = frozenset()

    def __post_init__(self) -> None:
        # 契约的自洽性在构造时就查,不留到运行期。
        # 一个 ENUM 字段没给枚举类,后果是识别 Schema 里那个字段没有取值约束,
        # 模型可以自由发挥 —— 而这件事直到有人看到一个自造的枚举值才会被发现。
        if self.value_type in (ValueType.ENUM, ValueType.ENUM_LIST) and self.enum is None:
            raise ValueError(f"{self.name}: {self.value_type} 字段必须声明 enum")
        if self.value_type not in (ValueType.ENUM, ValueType.ENUM_LIST) and self.enum is not None:
            raise ValueError(f"{self.name}: 非枚举字段不该声明 enum")
        if self.multi_value != (
            self.value_type in (ValueType.ENUM_LIST, ValueType.TEXT_LIST)
        ):
            raise ValueError(f"{self.name}: multi_value 必须与列表类型一致")
        if not self.visual and self.auto_confirm_policy is not AutoConfirm.NEVER:
            # 看不出来的字段却允许自动确认,等于让模型凭空写业务数据
            raise ValueError(f"{self.name}: visual=False 的字段必须是 NEVER")
        if not 0.0 < self.visibility_prior <= 1.0:
            raise ValueError(f"{self.name}: visibility_prior 必须在 (0, 1]")
        if (
            self.auto_confirm_policy is AutoConfirm.THRESHOLD
            and self.threshold_group not in THRESHOLD_GROUPS
        ):
            raise ValueError(f"{self.name}: 未知阈值组 {self.threshold_group}")
        if not self.applies_to:
            # 对谁都不适用的字段不该注册 —— 它进不了任何识别 Schema,
            # 也进不了任何必填集,留着只会让"注册表是穷举清单"这句话变假
            raise ValueError(f"{self.name}: applies_to 不能为空")
        if not self.required_for <= self.applies_to:
            # "对男装必填、但只在女装上存在"是自相矛盾的声明。
            # 放过它的表现是:男装商品永远缺一个它填不了的必填字段
            raise ValueError(
                f"{self.name}: required_for 必须是 applies_to 的子集"
                f"(required_for={sorted(a.value for a in self.required_for)},"
                f" applies_to={sorted(a.value for a in self.applies_to)})"
            )

    @property
    def auto_confirm_threshold(self) -> float | None:
        """自动确认所需的 `system_confidence`。NEVER 策略返回 None。"""
        if self.auto_confirm_policy is AutoConfirm.NEVER:
            return None
        return THRESHOLD_GROUPS[self.threshold_group]


#: ---------------------------------------------------------------- 字段清单
#:
#: 前 8 个对应 `products` 表现有的外观列,它们从 M4 起降级为**只读兼容投影**;
#: 事实源是 `product_attribute_values`。
#:
#: 泳装类目第一版,刻意只登记「这一版真的要用」的字段。
#: 注册表的价值在于它是一份**穷举**清单 —— 加一堆将来可能用的字段,
#: 会让「spec 引用了不存在的字段」这类断言失去意义。
_FIELDS: tuple[AttributeField, ...] = (
    AttributeField(
        name="garment_type",
        label="品类",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=GarmentType,
        visibility_prior=1.0,
        threshold_group="identity",
        legacy_column="garment_type",
        required_for=ALL_AUDIENCES,
    ),
    AttributeField(
        name="primary_color",
        label="主色",
        owner_type=OwnerType.VARIANT,
        value_type=ValueType.TEXT,
        # 颜色不用枚举:平台的颜色词表各不相同,归一化交给渠道层的 dict 映射。
        # 这里存的是我们自己的规范色名,不是某个平台的色号。
        visibility_prior=1.0,
        threshold_group="identity",
        legacy_column="primary_color",
        required_for=ALL_AUDIENCES,
    ),
    AttributeField(
        name="secondary_colors",
        label="辅助色",
        owner_type=OwnerType.VARIANT,
        # 和 primary_color 一样是自由文本:颜色不做枚举。
        # 上一版这里借用了 PatternType 当占位枚举 —— 那会让
        # "枚举取值与 core/enums.py 对齐"这条断言变成假的:
        # 它确实对齐了,只是对齐到了一张语义无关的表上。
        value_type=ValueType.TEXT_LIST,
        multi_value=True,
        visibility_prior=0.9,
        threshold_group="decorative",
        legacy_column="secondary_colors",
    ),
    AttributeField(
        name="pattern_type",
        label="图案",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=PatternType,
        visibility_prior=0.85,
        threshold_group="decorative",
        legacy_column="pattern_type",
    ),
    # --- 女装结构三件套(PRD v2 §18.3:applies_to={WOMEN}) ---
    #
    # 不含 UNISEX:中性款按 §14.3 走男装那组结构字段(腰头/贴身度/开合),
    # 防晒衣的"领口"不是比基尼的领型,复用这三个字段只会让模型在
    # 水母衣上编一个"深 V 领"。
    AttributeField(
        name="strap_type",
        label="肩带样式",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=StrapType,
        applies_to=frozenset({Audience.WOMEN}),
        visibility_prior=0.95,
        threshold_group="structure",
        legacy_column="strap_type",
    ),
    AttributeField(
        name="neckline_type",
        label="领型",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=NecklineType,
        applies_to=frozenset({Audience.WOMEN}),
        visibility_prior=0.95,
        threshold_group="structure",
        legacy_column="neckline_type",
    ),
    AttributeField(
        name="back_style",
        label="背部样式",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=BackStyle,
        applies_to=frozenset({Audience.WOMEN}),
        visibility_prior=0.9,
        threshold_group="structure",
        legacy_column="back_style",
    ),
    AttributeField(
        name="material",
        label="材质成分",
        owner_type=OwnerType.SPU,
        value_type=ValueType.TEXT,
        # 看图看不出成分。这条 v1 就是对的,v2 把它从「提示词里叮嘱一句」
        # 变成注册表的静态断言:visual=False 的字段进不了识别 Schema
        visual=False,
        auto_confirm_policy=AutoConfirm.NEVER,
        threshold_group=NEVER_GROUP,
        legacy_column="material",
        required_for=ALL_AUDIENCES,
    ),
    # --- 男装字段组(PRD v2 §18.4;中性款共用,见 §14.3) ---
    #
    # 没有 legacy_column:`products` 表的 8 个投影列是**受众前时代的兼容层**,
    # 男装字段的事实源只在 `product_attribute_values`,不再开新投影 ——
    # 投影列的存在理由是"旧查询还在读",而男装字段没有旧查询。
    AttributeField(
        name="waistband_type",
        label="腰头形制",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=WaistbandType,
        applies_to=frozenset({Audience.MEN, Audience.UNISEX}),
        required_for=frozenset({Audience.MEN}),
        visibility_prior=0.95,
        threshold_group="structure",
    ),
    AttributeField(
        name="inseam_length",
        label="裤长(内缝)",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=InseamLength,
        applies_to=frozenset({Audience.MEN, Audience.UNISEX}),
        required_for=frozenset({Audience.MEN}),
        # 平铺图上裤长常被折进去(§9.3),先验低于腰头
        visibility_prior=0.9,
        threshold_group="structure",
    ),
    AttributeField(
        name="liner_type",
        label="内衬",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=LinerType,
        applies_to=frozenset({Audience.MEN, Audience.UNISEX}),
        # §18.4:平铺图和模特图通常都看不见内衬。与 material 同一档 ——
        # 看不出来的字段必须 NEVER 自动确认,来源只能是商品库或人工
        visual=False,
        auto_confirm_policy=AutoConfirm.NEVER,
        threshold_group=NEVER_GROUP,
    ),
    AttributeField(
        name="pocket_config",
        label="口袋配置",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=PocketConfig,
        applies_to=frozenset({Audience.MEN, Audience.UNISEX}),
        visibility_prior=0.85,
        threshold_group="structure",
    ),
    AttributeField(
        name="fit_type",
        label="版型",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=FitType,
        applies_to=frozenset({Audience.MEN, Audience.UNISEX}),
        required_for=frozenset({Audience.MEN}),
        # 版型要看穿着效果,平铺图上修身和常规几乎无法区分
        visibility_prior=0.8,
        threshold_group="structure",
    ),
    AttributeField(
        name="closure_type",
        label="闭合方式",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=ClosureType,
        applies_to=frozenset({Audience.MEN, Audience.UNISEX}),
        visibility_prior=0.85,
        threshold_group="structure",
    ),
)


REGISTRY: Mapping[str, AttributeField] = {f.name: f for f in _FIELDS}

#: 枚举值的运营界面文案。数据库与渠道映射继续使用稳定英文编码；接口同时
#: 下发这张中文词表，前端只负责显示，不能自己再维护一份翻译。
#:
#: 这里按值而不是按字段存：重复编码（UNKNOWN / BUTTON / VELCRO）在各字段
#: 中语义相同。`test_attribute_field_spec` 会逐个枚举值检查，新增取值漏翻译
#: 会当场变红，而不是在页面上悄悄退回英文。
_OPTION_LABELS: Mapping[str, str] = {
    "ONE_PIECE": "连体泳衣",
    "BIKINI_TOP": "比基尼上衣",
    "BIKINI_BOTTOM": "比基尼下装",
    "BIKINI_SET": "比基尼套装",
    "TANKINI": "坦基尼",
    "COVER_UP": "罩衫/沙滩外搭",
    "DRESS": "泳裙",
    "TOP": "上衣",
    "SWIM_SHORTS": "平角泳裤/沙滩裤",
    "BOARDSHORTS": "冲浪裤",
    "SWIM_BRIEFS": "三角泳裤",
    "JAMMERS": "及膝竞速泳裤",
    "SWIM_TRUNKS_SET": "泳裤套装",
    "RASH_GUARD": "防晒衣/水母衣",
    "OTHER": "其他",
    "SOLID": "纯色",
    "STRIPE": "条纹",
    "FLORAL": "花卉",
    "ANIMAL": "动物纹",
    "GEOMETRIC": "几何图案",
    "TIE_DYE": "扎染",
    "COLOR_BLOCK": "拼色",
    "PRINT_LOGO": "标志印花",
    "HALTER": "挂脖",
    "SPAGHETTI": "细肩带",
    "WIDE": "宽肩带",
    "STRAPLESS": "无肩带",
    "ONE_SHOULDER": "单肩",
    "CRISS_CROSS": "交叉肩带",
    "V_NECK": "V 形领",
    "SCOOP": "圆弧领",
    "SQUARE": "方领",
    "SWEETHEART": "心形领",
    "HIGH_NECK": "高领",
    "PLUNGE": "深 V 领",
    "BANDEAU": "抹胸",
    "OPEN_BACK": "露背",
    "RACER_BACK": "工字背",
    "CROSS_BACK": "交叉背",
    "TIE_BACK": "系带背",
    "FULL_BACK": "全包背",
    "LACE_UP": "绑带背",
    "ELASTIC": "松紧腰",
    "DRAWSTRING": "抽绳腰",
    "ELASTIC_DRAWSTRING": "松紧抽绳腰",
    "BUTTON": "纽扣",
    "VELCRO": "魔术贴",
    "SHORT": "短款",
    "MID": "中长款",
    "LONG": "长款",
    "KNEE_LENGTH": "及膝",
    "MESH_LINER": "网眼内衬",
    "BRIEF_LINER": "三角内衬",
    "NO_LINER": "无内衬",
    "NO_POCKETS": "无口袋",
    "SIDE": "侧袋",
    "BACK": "后袋",
    "SIDE_AND_BACK": "侧袋和后袋",
    "SLIM": "修身",
    "REGULAR": "常规",
    "LOOSE": "宽松",
    "NONE": "无",
    "ZIPPER": "拉链",
    "UNKNOWN": "未知",
}


def get_field(name: str) -> AttributeField:
    """按名取字段。未注册的字段一律拒绝 —— 属性名不是随便写的字符串。"""
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"未注册的属性字段:{name}") from None


def field_names() -> tuple[str, ...]:
    return tuple(REGISTRY)


def field_label(name: str) -> str:
    """字段的中文名。**未注册的字段回落成字段名本身,不抛。**

    调用点全是显示路径(颜色维的「缺:…」、缺失清单、批量结果),
    而历史数据里可能留着注册表已经删掉的字段名 —— 那时候界面该做的事
    是把这个名字原样说出来,不是整页读不出来(与 `field_spec_out`
    的兜底同一条理由)。
    """
    spec = REGISTRY.get(name)
    return spec.label if spec is not None else name


def field_labels(names: Iterable[str]) -> tuple[str, ...]:
    """一串字段名 -> 一串中文名。顺序保持不变。"""
    return tuple(field_label(n) for n in names)


def _applies(field: AttributeField, audience: Audience | None) -> bool:
    """受众过滤。None = 受众未确认,退回全集 —— 那个商品会先被排去
    "待确认受众"(§8.2),识别层不替它猜受众后再按猜的结果缩 Schema。"""
    return audience is None or audience in field.applies_to


def extractable_fields(audience: Audience | None = None) -> tuple[str, ...]:
    """允许出现在识别 Schema 里的字段。

    两条排除规则,同一个执行点(PRD v2 §18.3):

        `visual=False`            看图看不出来的,禁止让模型猜
        `audience not in applies_to`  这件商品上不存在的,同样禁止让模型猜

    问一个泳裤的"领型",模型会给一个非常自信的答案 ——
    提示词里叮嘱一句是靠不住的,过滤必须发生在 Schema 生成之前。
    """
    return tuple(
        name
        for name, f in REGISTRY.items()
        if f.visual and _applies(f, audience)
    )


def enum_definitions(audience: Audience | None = None) -> Mapping[str, tuple[str, ...]]:
    """交给识别层的枚举定义。只含可识别、且本受众适用的字段。"""
    return {
        name: tuple(v.value for v in REGISTRY[name].enum)  # type: ignore[union-attr]
        for name in extractable_fields(audience)
        if REGISTRY[name].enum is not None
    }


def fields_for(audience: Audience | None = None) -> tuple[str, ...]:
    """本受众适用的全部字段(含 visual=False 的)。属性页按它出列。"""
    return tuple(name for name, f in REGISTRY.items() if _applies(f, audience))


def legacy_projection_map() -> Mapping[str, str]:
    """字段名 -> `products` 表的兼容投影列名(§5.4)。

    这张映射是**单向**的:属性表写入时同事务更新投影列,
    反过来任何业务代码都不得直接写这些列。
    """
    return {name: f.legacy_column for name, f in REGISTRY.items() if f.legacy_column}


def required_for_listing(audience: Audience | None = None) -> tuple[str, ...]:
    """刊登必填集(PRD v2 §18.3:按受众条件化)。

    - 受众已确认:该受众在 `required_for` 里的字段。
    - 受众未确认(None):**所有受众都必填**的字段。这与 v1 的全局
      bool 行为逐字段一致(当年 required 的三个字段 —— garment_type、
      primary_color、material —— 恰好对两个受众都必填),受众前时代的
      商品因此不多不少地保持原有必填集。
    """
    if audience is None:
        return tuple(
            name for name, f in REGISTRY.items() if f.required_for == ALL_AUDIENCES
        )
    return tuple(
        name for name, f in REGISTRY.items() if audience in f.required_for
    )


def field_spec_out(name: str) -> dict[str, object]:
    """一个字段的类型元数据,给接口层原样透出(BLOCK-11)。

    放在注册表里而不是 schema 里:`options` 要从 `enum` 上取值,而
    "枚举有哪些取值"这句话在本仓库只有 `core/enums.py` 一个来源。
    在 schema 层再展开一次,等于给它开第二个出口。

    未注册的字段返回一个**不带约束**的文本规格,而不是抛错:属性行是历史数据,
    字段可能在注册表里被删掉过,而那时候界面该做的事是把值原样显示出来,
    不是整页读不出来。
    """
    spec = REGISTRY.get(name)
    if spec is None:
        return {
            # 认不出的字段用字段名当标题:显示 `legacy_thing` 比显示空白强,
            # 后者会让人以为这一行坏了
            "label": name,
            "value_type": ValueType.TEXT.value,
            "multi_value": False,
            "options": [],
            "option_labels": {},
        }
    options = [v.value for v in spec.enum] if spec.enum is not None else []
    return {
        "label": spec.label,
        "value_type": spec.value_type.value,
        "multi_value": spec.multi_value,
        "options": options,
        "option_labels": {value: _OPTION_LABELS[value] for value in options},
    }
