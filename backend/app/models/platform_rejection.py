"""平台驳回记录(OPS-REVIEW P1:驳回回流)。

一条记录 = 平台驳回了某个 SKU 的一次导出。OPEN 的记录会以阻断问题出现在
工作台,驳回件从此不能顶着"已完成"沉底。

## 为什么不合并进 listing_drafts

驳回记录和草稿行的生命周期不同:
    草稿可以被重建(指纹变了,新草稿覆盖旧行)
    驳回记录不可变 —— 平台的那个判定是历史事实,不能被后续操作覆盖

而且一份草稿可能被多次提交,每次都可能产生新的驳回。一张表里存一列
json 会让"打开了哪几条驳回"变成一个字符串解析问题,那是下一个维护者的
头痛来源。独立一张表,每条记录可以独立 RESOLVED,可以被外键引用,
可以被独立查询 —— 这才是"驳回件收回工作台"的地基。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import RejectionStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PlatformRejection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一条平台驳回记录。

    `draft_id` 指向被驳回的那份草稿(不是当前最新草稿,是**当时**导出的那份);
    导出后草稿被重建了也无妨 —— 驳回记录仍然指向历史上那次导出的草稿行。

    `located_by` 记录定位把握:
        audit          导出审计带了组件版本,最可信
        current_draft  审计没带版本但草稿其后没重建,可信
        unlocated      老导出且草稿已重建,指纹在但版本不可考
    """

    __tablename__ = "platform_rejections"
    __table_args__ = (
        Index("ix_platform_rejections_draft_status", "draft_id", "status"),
        Index("ix_platform_rejections_spu_status", "spu", "status"),
    )

    draft_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("listing_drafts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    spu: Mapped[str] = mapped_column(String(64), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 运营在下拉里选的原因码(`workbench/platform.py RejectionReason`)
    reason_code: Mapped[str] = mapped_column(String(48), nullable=False)
    #: 平台的原话,照贴不改
    platform_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 指向哪一次导出的指纹(可能只有 12 位前缀)
    export_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    export_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: 被驳回那一次导出时的组件快照(图片集 id/版本 + 文案 id/版本 + 模板版本)
    #: 由 `platform.trim_components` 精简,不含属性版本列表
    component_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: audit / current_draft / unlocated
    located_by: Mapped[str] = mapped_column(String(24), nullable=False, default="unlocated")

    #: 评审第 7 条加了中间态 `FIXED_PENDING_EXPORT`,20 个字符 ——
    #: 原来的 String(16) 装不下,写入会被 PostgreSQL 直接拒掉。
    #: 32 留了余量:下一个状态名不该再触发一次迁移。
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RejectionStatus.OPEN.value
    )
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    recorded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
