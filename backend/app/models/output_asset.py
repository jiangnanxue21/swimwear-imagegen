"""网站图片输出模型(需求第十五章 / 阶段 6)。

一张通过审核的候选图,会派生出五条 `output_assets`(原始 / 详情 / 缩略 / 移动 / 正方形)。

为什么不复用 `product_assets`:那张表记录的是**人上传的原始素材**,
有"永不覆盖"的强约束;输出图是**系统派生物**,网站改版时会整批重算重生成。
两者生命周期完全不同,混在一张表里迟早会有人写出一条把原图删掉的清理脚本。
"""
from __future__ import annotations

# datetime 必须运行时 import:SQLAlchemy 2 建映射时会解析 Mapped[...] 注解字符串
from datetime import datetime  # noqa: F401
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class OutputAsset(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """网站可直接使用的成品图。"""

    __tablename__ = "output_assets"
    __table_args__ = (
        # 同一个候选图的同一用途只留一条有效记录;重算时先停用旧的再插新的
        UniqueConstraint(
            "candidate_id", "purpose", "generation", name="uq_output_assets_candidate_purpose"
        ),
        Index("ix_output_assets_product_id", "product_id"),
        Index("ix_output_assets_task_id", "task_id"),
        Index("ix_output_assets_enabled", "enabled"),
        # 一个商品的同一用途只能有一张启用中的成品图。
        #
        # 和 prompt_templates 那条一样,原先只存在于 0006 迁移。
        # 缺了它,`output_service` 的并发出图在测试库里怎么跑都不会撞车,
        # 而生产库会 —— 测试环境证明不了任何东西。
        Index(
            "uq_output_assets_product_purpose_enabled",
            "product_id",
            "purpose",
            unique=True,
            postgresql_where=text("enabled"),
        ),
    )

    product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="SET NULL"), nullable=True
    )
    candidate_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("generation_candidates.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: 图片用途(需求第十五章)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 重算代数。网站改尺寸时 +1,旧代仍可回滚查看
    generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False, default="image/jpeg")
    image_format: Mapped[str] = mapped_column(String(8), nullable=False, default="JPEG")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: 是否启用。下架一张图用停用而不是删除 —— 网站可能还缓存着这个 URL
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    #: 尺寸决策说明("源图已小于 1200×1600,保持原尺寸不放大"这类),排查时不用反推
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
