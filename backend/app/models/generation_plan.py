"""生成方案(PRD v3.1 §4.7,修复风险表 D2)。

## 它补的是一个"被引用了但不存在"的实体

v3.0 的 §6.4 与 §7.3 都在引用"生成方案"——SPU 默认 + 颜色覆盖、
方案指纹进幂等键——而 §4 数据模型里没有它。后果不是少一张表,是
**§9.3 的幂等键少了一个输入**:改了角度配置再提交,会静默命中上一个任务,
运营看到"提交成功"、拿到的是旧参数的图,而没有任何地方会说为什么。

## 判定不在这里

指纹怎么算、哪个颜色当前生效的是哪一份、预算过不过,全在
`app/workflows/generation_plan.py`(零依赖、可穷举)。这张表只负责存。

分成两半不是洁癖。把 `plan_fingerprint()` 写在模型模块里(第一版就是那样)
有一个具体的代价:这个文件 import sqlalchemy,而**这台机器上没有
sqlalchemy** —— 于是指纹算法一行都测不到,而指纹算错不报错,
只会让"换了方案"这件事静静地不触发重出图。

## 指纹**存列**,不是每次现算

`plan_fingerprint` 是派生值,而 §8.2 说"下游过期一律派生比较,不写 STALE 列"
—— 两句话不矛盾:那条禁的是**状态**(STALE 与否),不是**派生输入的快照**。
图片集过期判定要拿"这批图当初按哪份方案生成"和"现在生效的是哪份"比,
前者只能是存下来的一个值。不存的话,改一次方案就再也无法回答
"这批图是不是按当前方案出的"——因为旧参数已经被覆盖掉了。

## 两条容易被"顺手修好"的约束

两条部分唯一索引里的 `COALESCE(color_variant_id,'')` 都是必须的。
PostgreSQL 把 NULL 当成互不相等,不套 COALESCE 的话可以插进任意多份
`color_variant_id IS NULL` 的 SPU 默认方案。ACTIVE 索引守"每层当前生效
一份",DRAFT 索引守"每层只有一个正在编辑的草稿"。少了后者,双击保存
会留下两份草稿;把两者合成 `status <> 'ARCHIVED'`,又会让 ACTIVE 挡住
下一份 DRAFT,方案启用后再也改不了。
这和 `listing_image_sets` / `listing_image_items` 上那三条表达式索引
是同一个坑(评审第 7、9 条),那边的注释里记着。

**而且它必须是 `Index(..., unique=True)` 而不是 `UniqueConstraint`:**
唯一约束不接受表达式,写成那样在 `alembic upgrade` 当场报错 ——
那是好的失败方式;真正危险的是有人为了让它跑起来把 COALESCE 删掉。
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import GenerationPlanStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.spu import Spu


class GenerationPlan(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一个 SPU(或其中一个颜色)当前用什么参数出图。"""

    __tablename__ = "generation_plans"
    __table_args__ = (
        # ACTIVE 与 DRAFT 各占一个槽。ACTIVE 保留历史语义,DRAFT 可以在
        # 启用前原地编辑;两者必须能共存,否则启用之后就建不出替代草稿。
        Index(
            "uq_generation_plans_scope",
            "spu_id",
            text("COALESCE(color_variant_id::text, '')"),
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "uq_generation_plans_draft_scope",
            "spu_id",
            text("COALESCE(color_variant_id::text, '')"),
            unique=True,
            postgresql_where=text("status = 'DRAFT'"),
        ),
        Index("ix_generation_plans_spu", "spu_id"),
        Index("ix_generation_plans_fingerprint", "plan_fingerprint"),
        # 预算上限不能是负数。写成 CHECK 而不是只在服务层校验,是因为负上限的
        # 表现是"每一次创建任务都被预算拦下",而运营看到的错误是"预算不足"
        # —— 一个指着错误方向的提示比没有提示更贵
        CheckConstraint(
            "budget_cap IS NULL OR budget_cap >= 0", name="ck_generation_plans_budget_cap"
        ),
    )

    spu_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("spus.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL = SPU 默认方案;非空 = 该颜色的覆盖(§4.7)
    color_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("color_variants.id", ondelete="CASCADE"),
        nullable=True,
    )

    #: 模特模板。**创建/启用时走现有授权闸**(`model_template_service.assert_usable`)
    #: 与受众筛选(§10.5)—— 方案不是绕过授权的第二条路(§6.4 明写这一条)
    model_template_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("model_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    scene: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    pose: Mapped[str] = mapped_column(String(32), nullable=False, default="")

    #: 目标角度与每角度候选数。**存的是规范化之后那一份**
    #: (`workflows.generation_plan.serialize_angles`,形如
    #: `[{"angle": "FRONT", "count": 2}]`)——存原样的话 `{"FRONT":1}` 与
    #: `[{"angle":"FRONT","count":1}]` 会在库里留下两种形状而指纹相同,
    #: 于是"这两份方案一不一样"有两个答案
    angles_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    #: 本方案累计费用上限(§4.7)。超出时新任务创建被拒,读现有花费台账。
    #: Numeric 而不是 Float:`spend.py` 那一侧也是 Numeric,两边不同型的
    #: 表现是边界上"已花 9.999999999 < 上限 10"这种放行
    budget_cap: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    #: 由上面那些字段派生(§4.7)。为什么存列见模块文档第三段。
    #: 唯一写入点是 `services/generation_plan_service`,它调
    #: `workflows.generation_plan.fingerprint_of()` —— 与投影列同一套规矩
    plan_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", info={"projection": True}
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=GenerationPlanStatus.DRAFT.value
    )
    #: 乐观锁。方案是多人会同时编辑的对象(运营配角度、主管配预算),
    #: 后写静默覆盖先写的表现是"我明明改了预算,怎么没生效"
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    spu: Mapped[Spu] = relationship(back_populates="generation_plans")

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        scope = self.color_variant_id or "SPU-DEFAULT"
        return f"<GenerationPlan {self.spu_id}/{scope} {self.status}>"
