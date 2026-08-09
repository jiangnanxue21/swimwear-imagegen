"""SPU 与颜色变体 —— 身份规范化的两张新表(PRD v3.1 §4.2 / §4.3)。

## 在这两张表之前,"SPU"和"颜色"是什么

    SPU     `products.spu`,一个字符串。同一个款的几行 SKU 靠**字符串相等**
            聚在一起 —— 改一个字,那一行就静默脱离了它的款
    颜色    `products.variant_key`(0027),SPU 内的一个字符串标识,
            由 `listings/variant_key.py` 在创建时分配一次

两者都能工作,代价是**没有地方可以挂"这个款/这个颜色自己的东西"**:受众只能
复制到每一行 SKU 上(于是要有一致性检查),颜色事实的 owner_id 只能靠
`<len>:<spu>/<variant>` 拼出来(于是要有一个解析器),而"这个颜色停售了"
根本无处存放。

这两张表把身份从"字符串约定"改成"外键"。**PRD §3.2 是规范化不是重建**:
`products` 仍然是 SKU 表,`products.spu` 降级为反规范化读列,
不删列、不双写、不做兼容层(§3.1:系统尚未正式使用,没有存量数据要迁)。

## 本批次的边界(A45-batch13)

`spu_id` / `color_variant_id` 这一版**可空**,原因不是留后路,是顺序:
现存的建档路径(`product_service.create_product`、CSV 导入)还不知道这两张表,
立刻 NOT NULL 会让它们在 flush 阶段整体失败,而这台机器上跑不了任何真库用例
——一个改不动就没法验的改动不该一次做完。收紧的前提写在
`docs/STATUS.md` 阶段 1 那一节,守卫在
`tests/pure/test_a45_batch13_stage1_identity.py`。
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import SellableStatus, SpuStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.generation_plan import GenerationPlan
    from app.models.product import Product


class Spu(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一个款。**不保存任何视觉事实**(颜色、图案、领型、肩带都不在这里)。

    §4.2 把这句话写成规格是有原因的:视觉事实的事实源是属性层
    (`product_attribute_values`,带来源/置信度/证据/版本/人工确认),
    在这里再放一份,"这个字段为什么是这个值"就有两个互相矛盾的答案。
    """

    __tablename__ = "spus"
    __table_args__ = (
        UniqueConstraint("spu_code", name="uq_spus_spu_code"),
        Index("ix_spus_status", "status"),
        # §26:成功指标按受众分组统计。受众的权威从今天起在这一列上
        Index("ix_spus_audience", "audience"),
    )

    #: 全局唯一业务编码。同时是 SKU 编码的第一段(`listings/sku_matrix`),
    #: 所以它的字符集比 `products.spu` 窄 —— 入口在 `sku_matrix.normalize_code`
    spu_code: Mapped[str] = mapped_column(String(32), nullable=False)
    internal_name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: 受众。**必填,且这里是唯一权威**(§4.2)。
    #:
    #: `products.audience` 从今天起是它的反规范化副本:新建档路径从 SPU 抄一份
    #: 下去,老路径不变。抄下去而不是让读取方各自 join,是因为受众在六处被用到
    #: (模特筛选、提示词、槽位表、检查项、必填属性、平台字段),每一处都改成
    #: join 是本批次做不完、也验不了的量 —— 而**判定**始终只有
    #: `core/audience.category_code_for` 一处,那条规矩没有松动。
    #:
    #: 这里不允许 NULL,于是 §3.4 的"待确认受众"在 SPU 这一层不存在:
    #: 建档表单第一步就要选,选不出来说明这个款还不该建档。
    audience: Mapped[str] = mapped_column(String(16), nullable=False)

    #: 基础类目。规则包键仍由 `core/audience.category_code_for` 从
    #: `audience + 品类` 唯一派生 —— 这一列是那个派生的输入,不是它的替代
    base_category: Mapped[str] = mapped_column(
        String(64), nullable=False, default="swimwear"
    )
    supplier_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SpuStatus.DRAFT.value
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 乐观锁。**不是自增主键的替代品** —— 它防的是"两个人同时改同一个 SPU,
    #: 后写的静默覆盖先写的"。本批次只落列,并发编辑接口在阶段 1 第二批
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    color_variants: Mapped[list[ColorVariant]] = relationship(
        back_populates="spu",
        cascade="all, delete-orphan",
        order_by="ColorVariant.sort_order",
    )

    generation_plans: Mapped[list[GenerationPlan]] = relationship(
        back_populates="spu",
        cascade="all, delete-orphan",
        foreign_keys="GenerationPlan.spu_id",
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Spu {self.spu_code} {self.internal_name!r}>"


class ColorVariant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一个颜色。**身份是 `id`,不是名字。**

    这张表的存在本身就是 DECISIONS §3.17 那条的终点:变体身份原来是表达式
    `primary_color or sku`,改一次颜色名,图片标签、属性 owner_id、导出变体列
    三样东西同时换主键。0027 用 `products.variant_key` 止住了血(分配一次、
    不再推导),而 UUID 主键让"改名换主键"在结构上不可能。
    """

    __tablename__ = "color_variants"
    __table_args__ = (
        # §4.3:SPU 内稳定唯一。跨 SPU 同名颜色是常态(几乎每个款都有黑色),
        # 所以唯一性必须带 spu_id —— 这正是 owner_id 命名空间当初要解决的事,
        # 那边靠拼字符串,这里靠一列外键
        UniqueConstraint("spu_id", "variant_code", name="uq_color_variants_spu_code"),
        Index("ix_color_variants_spu", "spu_id"),
        Index("ix_color_variants_sellable_status", "sellable_status"),
    )

    spu_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spus.id", ondelete="CASCADE"), nullable=False
    )

    #: SPU 内的稳定标识,同时是 SKU 编码的第二段。**分配后不改** ——
    #: 改它等于换一个颜色,而挂在旧编码下的素材和事实不会跟着走
    variant_code: Mapped[str] = mapped_column(String(16), nullable=False)

    #: 建档阶段的内部临时名称("供应商说的那个蓝")。运营可编辑,不对外
    working_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    #: 正式颜色名称。**投影列,唯一写入点是属性服务**(§4.3):VARIANT 层的
    #: 颜色事实被 CONFIRMED 时,在同一个事务里写下来
    #: (`attributes/service._project_colour_name`)。触发点挂在事实的
    #: 写入边界 `set_value` 上(A45-batch24),所以**人工确认与识别自动确认
    #: 两条路都会写它** —— batch23 只挂在 `confirm()` 时,自动确认出的颜色
    #: 这一列仍是空串,证据链在 `docs/DECISIONS.md` §3.54。
    #:
    #: **那个字段叫 `primary_color`,不叫 `standard_color_name`。**
    #: 这里原来写的是后者,而全仓没有那个字段 —— 照着它去找的人会找不到,
    #: 然后得出"还缺前置"的结论。字段名定死在
    #: `attributes/colour_projection.SOURCE_FIELD`,守卫钉着它真的在注册表里
    #: 且真的是 VARIANT 层。
    #:
    #: 与 `products` 上那 8 个投影列同一套规矩和同一条 AST 守卫 ——
    #: `info` 里的标记是给那条扫描读的,注释读不到。
    #:
    #: (这里原来还留着一句「本批次只落列:写入点要等 owner_id 切 UUID」。
    #: 那个前置在迁移 0046 就完成了,而这半句注释又多活了几批 ——
    #: 正是这笔账躲过去的方式之一,见 §3.53 第三节。按硬规则 5 判定:
    #: 过期的是注释,不是代码,故删。)
    display_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", info={"projection": True}
    )

    supplier_color_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sellable_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SellableStatus.PLANNED.value
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    spu: Mapped[Spu] = relationship(back_populates="color_variants")
    #: `passive_deletes="all"` 不是优化,是 RESTRICT 能不能生效的前提(A45-batch13-3 / R1)。
    #:
    #: 少了它,`session.delete(variant)` 时 SQLAlchemy 对这种"无 delete cascade 的
    #: 一对多"的默认动作是:**先把子行外键置 NULL,再删父行** —— 而
    #: `products.color_variant_id` 本批次恰好可空,数据库一声不吭。于是
    #: "删一个颜色"不会被 RESTRICT 拦住,而是"成功"并把它下面的 SKU 行
    #: 悄悄孤儿化:属性 owner、图片标签、导出变体列同时指空,三者都不报错 ——
    #: 这正是这张表要消灭的那件事,只是换了一个入口。
    #:
    #: `"all"` 让 ORM 对子行**一个字节都不碰**(连已加载的也不改),DELETE 直接
    #: 交给数据库,由 `fk_products_color_variant_id_color_variants` 的 RESTRICT
    #: 抛 IntegrityError。`True` 不够:它只是不去加载未加载的子行,已经在
    #: session 里的仍然会被置 NULL —— 恰好是"刚建完档马上删颜色"那条路。
    #:
    #: 守卫:`tests/pure/test_a45_batch13_3_fixes.py` 按 AST 钉这个 kwarg;
    #: 行为由 `tests/test_a45_batch13_3_db.py` 在真库上验(外键必须原样留在行上)。
    skus: Mapped[list[Product]] = relationship(
        back_populates="color_variant",
        order_by="Product.sku",
        passive_deletes="all",
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ColorVariant {self.variant_code} of {self.spu_id}>"
