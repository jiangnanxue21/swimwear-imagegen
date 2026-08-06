"""商品实体。字段对应需求第八章 Product 一节。"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    BackStyle,
    GarmentType,
    NecklineType,
    PatternType,
    ProductComplexity,
    ProductStatus,
    StrapType,
)
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.product_asset import ProductAsset
    from app.models.spu import ColorVariant


class Product(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("sku", name="uq_products_sku"),
        Index("ix_products_spu", "spu"),
        Index("ix_products_status", "status"),
        Index("ix_products_category", "category"),
        # §26:成功指标每一项都按受众分别统计,聚合查询以它为分组键
        Index("ix_products_audience", "audience"),
        # --- A45-batch13 / §4.4 ---
        #
        # 一个颜色下同一个尺码只能有一行。**这条约束对 size IS NULL 的行不生效**
        # (PostgreSQL 里 NULL 彼此不相等),而那正是它要被认真对待的地方:
        # 尺码留空的行可以在同一个颜色下重复任意多次,唯一约束一声不吭。
        # 新建档路径因此**从不写 NULL 尺码** —— 均码是模板 `ONE_SIZE`,
        # 落到库里是字符串 "OS"(理由写在 `listings/sku_matrix` 顶部)。
        UniqueConstraint("color_variant_id", "size", name="uq_products_variant_size"),
        Index("ix_products_spu_id", "spu_id"),
    )

    # --- 身份外键(A45-batch13,PRD v3.1 §4.4)---
    #
    # **权威归这两列,`spu` 字符串与 `variant_key` 降级。**
    #
    # 在它们之前,"这一行属于哪个款、哪个颜色"是两个字符串约定:同款靠
    # `spu` 字面相等聚在一起,同色靠 `variant_key`。约定的问题不是它不工作,
    # 是**它被打破时没有任何东西会报错** —— 改一个字,那一行就静默脱离了它的款,
    # 而列表页照常显示、导出照常生成。
    #
    # 可空是本批次的边界,不是设计:老建档路径(`product_service.create_product`、
    # CSV 导入)还不知道这两张表,立刻 NOT NULL 会让它们在 flush 阶段整体失败 ——
    # 而这台机器上跑不了任何真库用例,一个改不动就没法验的改动不该一次做完。
    # 收紧条件与守卫见 `app/models/spu.py` 顶部与 `docs/STATUS.md` 阶段 1。
    # `info={"identity": True}` 给 `product_service._is_identity_column()` 那条
    # 禁改名单读(A45-batch13-3 / R3)。那个函数自述"问模型、不维护第二张名单",
    # 而这两列是 batch13 新加的**最强**身份 —— `identity_of()` 把外键排第一级,
    # 改它一次,属性 owner、图片标签、导出变体列同时换主。batch13 忘了打标记,
    # 恰好是那段注释警告的失败形态:名单的唯一来源漏了新成员,而漏了不报错。
    # 今天靠 `ProductUpdate` schema 没有这两个字段挡着 —— 单层防御,不是设计。
    spu_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("spus.id", ondelete="RESTRICT"),
        nullable=True,
        info={"identity": True},
    )
    color_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("color_variants.id", ondelete="RESTRICT"),
        nullable=True,
        info={"identity": True},
    )

    # --- 标识 ---
    #
    # `spu` 从 batch13 起是**反规范化读列**(§4.4):由服务层随 `spu_id` 同步,
    # 禁止作为查询权威。留着它是因为十来个域按它取数,一次性改完既做不完也验不了;
    # 删除它是阶段 1 收尾的动作,不是本批次的。
    spu: Mapped[str] = mapped_column(String(64), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="swimwear")

    # --- 受众(PRD v2 §3.4,一等业务维度) ---
    #
    # **可空,且 NULL 的含义是"待确认受众",不是 UNISEX。**
    # 枚举里刻意没有 UNCONFIRMED 取值(理由见 `core/enums.Audience` 文档):
    # 给"没填"一个枚举值,它会被当成合法受众流进模特筛选和规则包派生。
    #
    # 三处规矩:
    # 1. 导入时可留空,留空进"待确认受众",**不允许按文件名或类目猜一个
    #    默认值填进去**(§9.1)—— 那会让 §4.2 的保护形同虚设;
    # 2. AI 只能建议、不能写这一列(§4.2:受众在禁改清单里)。它不在
    #    `attributes/registry.py` 里,识别 Schema 根本产不出这个字段;
    # 3. 挂在 SPU 上、不参与颜色款继承(§3.4 规则 2)。同 SPU 各行受众
    #    必须一致,导入与更新路径各查一次。
    #
    # 规则包键(category_code)由 `audience + category` 派生,派生逻辑
    # **只在** `core/audience.category_code_for` 一处(§23.5)。
    audience: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- 外观属性 ---
    #
    # **这 8 个列是只读兼容投影,不是事实源(§5.4)。**
    #
    # 事实源在 `product_attribute_values`:那里有来源、置信度、证据链、
    # 版本历史和人工确认记录,而这里只有一个字符串。两边都算数的话,
    # 「这个字段为什么是这个值」就有两个互相矛盾的答案。
    #
    # 唯一允许写它们的地方是 `attributes/service._project_to_legacy_column()`,
    # 而且必须和属性表的写入在**同一个事务**里。
    # `tests/pure/test_attribute_merge.py::test_nobody_writes_the_projection_columns_directly`
    # 用 AST 扫描断言除它之外无人赋值 —— 靠注释约束不住任何东西。
    # (这条守卫原先在 `test_attribute_projection.py`,文件已并入 merge;
    #  本注释一度还指着旧名字,指到了一个不存在的文件。)
    #
    # info 里的标记是给那条扫描和投影巡检用的:它能被程序读到,注释不能。
    primary_color: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", info={"projection": True}
    )
    #: 辅助颜色列表。用 JSONB 而非 ARRAY,便于将来整体替换为结构化配色描述。
    secondary_colors: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]", info={"projection": True})
    pattern_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PatternType.SOLID.value, info={"projection": True})
    garment_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=GarmentType.OTHER.value, info={"projection": True})
    material: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", info={"projection": True}
    )
    strap_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StrapType.UNKNOWN.value, info={"projection": True})
    neckline_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=NecklineType.UNKNOWN.value, info={"projection": True})
    back_style: Mapped[str] = mapped_column(
        String(32), nullable=False, default=BackStyle.UNKNOWN.value, info={"projection": True})

    # --- 变体身份(A44,§3.18)---
    #
    # **这一列不是属性,不是投影,也不允许运营编辑。**
    #
    # 它是「这一行属于哪个颜色变体」的身份,由 `listings/variants.py`
    # 的 `assign_variant_key()` 在**创建时分配一次**,此后任何字段变动
    # 都不重算它。改颜色文案只改名字(`primary_color`),不改身份。
    #
    # 在这一列之前,变体 id 是表达式 `primary_color or sku`,于是运营
    # 改一次颜色名,图片标签、属性 owner_id、导出变体列三样东西同时换主键。
    # 前者有 `unknown_tags` 能看见,后者**无声丢值** —— A43 把属性分层
    # 接到那个表达式上之后,这就是当时最急的一条遗留(DECISIONS §3.17)。
    #
    # 可空是为了让 0027 之前的对象仍然能构造(测试、脚本里的裸 Product);
    # 库里的行由 0027 全表回填,不会有 NULL。回落逻辑在 `variant_id_for()`,
    # `drift_report()` 的 `unassigned` 负责让漏网的行不隐身。
    #
    # `info` 里的标记给 `product_service._is_column()` 那条禁写名单用 ——
    # 它能被程序读到,注释不能。
    variant_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, info={"identity": True}
    )

    # --- SKU 维度(M6,§9.5)---
    #
    # SPU-SKU 展开要按颜色 × 尺码铺行。可空:存量商品没有这两个值,
    # 而它们**不是**投影列 —— 尺码不参与图片识别(注册表里根本没有这个字段),
    # 只能来自商品库或人工录入。
    size: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 尺码组。batch13 起,新建档路径写的是**尺码模板名**
    #: (`listings/sku_matrix.SIZE_TEMPLATES` 的键),不再是自由文本 ——
    #: 那一列从 M6 起就在,取值一直没有来源,于是同一个 SPU 里
    #: "ALPHA" 和 "alpha_4" 并存,而按尺码组聚合的地方只能各自猜
    size_group: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- 商务字段(A45-batch13,§4.4)---
    #
    # 四列都可空:建档时这些数字通常还没定(样品刚到,成本没核完)。
    # **草稿 READY 门禁负责在上架前校验 ACTIVE SKU 非空** —— 门禁在那里,
    # 不在这里,因为"还没填"在建档阶段是正常状态,在上架阶段才是缺陷。
    #
    # 价格用 Numeric 不用 Float:金额的二进制浮点误差会在汇总时冒出来,
    # 而这几列将来要进渠道报文。`Numeric(12, 2)` 够到一亿。
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    price: Mapped[object | None] = mapped_column(Numeric(12, 2), nullable=True)
    cost: Mapped[object | None] = mapped_column(Numeric(12, 2), nullable=True)
    inventory: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- 流程属性 ---
    complexity: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProductComplexity.UNKNOWN.value
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=ProductStatus.DRAFT.value
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    assets: Mapped[list[ProductAsset]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductAsset.created_at",
    )
    #: 颜色变体。**没有 cascade delete-orphan** —— 删一个颜色不该连带删掉
    #: 它下面的 SKU 行(那些行上挂着素材、生成任务、图片集)。
    #: 外键是 RESTRICT,所以数据库会先拦住这件事
    color_variant: Mapped[ColorVariant | None] = relationship(back_populates="skus")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Product {self.sku} {self.name!r}>"
