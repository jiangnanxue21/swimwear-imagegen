"""图片集(M5,§7)。

    MediaAsset ──► ListingImageSet ──► ListingDraft ──► Excel / API

## 为什么必须有这一层

`MediaAsset → ListingDraft.images` 解决不了六件事:哪张是主图、排序、
颜色绑定、版本、人工图与 AI 图混用、替换与回滚。这些都不是渠道映射
能顺手解决的问题 —— 它们是**编辑决定**,需要被保存、被审批、被回滚。

## 不可原地修改

批准之后任何改动生成 `version+1` 的新集(`derived_from` 指向旧集)。
于是「平台驳回的是哪一版图」永远可查,回滚就是把旧版重新置为 APPROVED。

草稿里记的是 `image_set_id`(**含 version**),不是图片列表的副本 ——
副本会在素材被隔离之后继续指向一张不能用的图。
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ImageSetStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ListingImageSet(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一个 SPU 在某个 (渠道, 站点) 下的一版图片编排。"""

    __tablename__ = "listing_image_sets"
    __table_args__ = (
        # channel / site 可空(NULL = 通用),而 PostgreSQL 的唯一约束
        # 把 NULL 当成互不相等 —— 所以这条约束**挡不住**两个都是 NULL 的重复。
        # 下面那个表达式索引才是真正的守卫。
        UniqueConstraint(
            "spu", "channel", "site", "version", name="uq_listing_image_sets_version"
        ),
        # 0013 的迁移里建了这条,而 ORM 上一直没声明(评审第 10 条)。
        # 后果:用 create_all() 或按 ORM 元数据建出来的库缺这条索引,
        # 于是并发重复在那种库上复现不出来,而正式库会 500 ——
        # 测试库和生产库不同形比没有测试更危险,它让人以为测过了
        Index(
            "uq_listing_image_sets_version_coalesced",
            "spu",
            text("COALESCE(channel, '')"),
            text("COALESCE(site, '')"),
            "version",
            unique=True,
        ),
        # 同一个 scope 最多只能有一个 APPROVED(评审第 7 条)。
        # 服务层的 advisory lock 只覆盖走那条代码路径的进程,
        # 迁移脚本和手工 SQL 不会去拿锁 —— 数据库这一层才是最终保证
        Index(
            "uq_listing_image_sets_one_approved",
            "spu",
            text("COALESCE(channel, '')"),
            text("COALESCE(site, '')"),
            unique=True,
            postgresql_where=text("status = 'APPROVED'"),
        ),
        Index("ix_listing_image_sets_spu_status", "spu", "status"),
    )

    spu: Mapped[str] = mapped_column(String(64), nullable=False)
    #: NULL = 平台无关的默认集
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: NULL = 该渠道全站点通用
    site: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ImageSetStatus.DRAFT.value
    )
    #: 从哪一版派生。回滚与"驳回的是哪一版"都靠它
    derived_from: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("listing_image_sets.id", ondelete="SET NULL"),
        nullable=True,
    )
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- 快审退回(A10,迁移 0020)----
    #
    # 为什么不只靠 `status = 'REJECTED'`:状态回答"能不能发",这四列回答
    # "为什么退回、谁退的、什么时候"。而 §3.2 A10 的验收标准是**退回必须产出
    # 明确的下一步** —— 下一步由原因推出(`workbench/reject.py` 的映射表),
    # 没有原因就只能落到兜底动作,运营看到的又是"自己想办法"。
    #
    # 另外 UAT 结束要回答"退回集中在哪一类",那是决定先修提示词还是先补素材
    # 的依据。`reason` 是受控枚举正是为了这个统计,自由文本回答不了。
    #: 取值见 `app.workbench.reject.RejectReason`。列宽按最长的枚举值留余量
    reject_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 补充说明。选「其他」时必填(服务层 `validate_reject` 挡),其余可选
    reject_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ListingImageItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """图片集里的一项。"""

    __tablename__ = "listing_image_items"
    __table_args__ = (
        UniqueConstraint("image_set_id", "sort_order", name="uq_listing_image_items_order"),
        UniqueConstraint(
            "image_set_id",
            "media_asset_id",
            "variant_id",
            name="uq_listing_image_items_asset_variant",
        ),
        # 上面那条同样挡不住 variant_id 为 NULL 的重复(评审第 9 条):
        # NULL 互不相等,于是"这张素材已经在这个集里了"这个约束在
        # 最常见的情况(没有绑定变体)下完全不生效,同一张图可以插进去任意多次。
        Index(
            "uq_listing_image_items_asset_variant_coalesced",
            "image_set_id",
            "media_asset_id",
            text("COALESCE(variant_id, '')"),
            unique=True,
        ),
        # 每个变体只能有一张启用中的主图。
        #
        # COALESCE 是必须的:variant_id 为 NULL 表示"SPU 通用",而
        # PostgreSQL 的唯一索引把 NULL 当成互不相等 —— 不套 COALESCE 的话
        # 可以插进任意多张 variant_id 为 NULL 的主图,而这个错误
        # 要到平台那边显示错主图时才会被发现。
        Index(
            "uq_image_primary",
            "image_set_id",
            text("COALESCE(variant_id, '')"),
            unique=True,
            postgresql_where=text("is_primary AND enabled"),
        ),
    )

    image_set_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("listing_image_sets.id", ondelete="CASCADE"),
        nullable=False,
    )
    media_asset_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("media_assets.id", ondelete="RESTRICT"), nullable=False
    )
    #: 该图在刊登中的角色。**可以和 MediaAsset.role 不同** ——
    #: 一张模特正面图完全可以被编排成这个 SPU 的主图
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 绑定颜色变体;NULL = SPU 通用
    variant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    #: 指定用哪个派生版本;NULL = 原图。平台要 1:1 主图时填 SQUARE_1_1
    derivative_purpose: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ---- PRD v3.1 §6.5:颜色图片规则(BLOCK-02 挂起所缺的业务决定)----
    #
    #: 运营在编辑器里明确勾了"这张通用图可以混进颜色附图"。
    #:
    #: **默认 false** —— §6.5 的原话是"默认不混入"。默认 true 在今天看不出
    #: 差别(库里的 `variant_id` 全是 NULL),而颜色绑定入口一上线,每个颜色的
    #: 附图位都会自动灌满所有通用图。那不是混排规则,是没有规则。
    #:
    #: 判定在 `listings/image_set_rules._section_6_5`,这里只是那个判定的输入。
    shared_opt_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: 这一项覆盖的拍摄角度(`ImageAngle` 取值)。候选入集时继承生成任务的方案,
    #: 人工只处理例外(§6.5 UI 落点那一句)。
    #:
    #: NULL = 没标注,而**没标注不算覆盖任何角度** —— 反过来(没标注就算通过)
    #: 会让角度验收在接线完成之前一直是绿的,而那正是它要防的
    angle: Mapped[str | None] = mapped_column(String(24), nullable=True)
