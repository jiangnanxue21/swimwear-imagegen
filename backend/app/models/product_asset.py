"""商品素材。

关键约束(需求第八章):
- 相同文件哈希不得重复存储 —— 物理落盘路径由 sha256 推导,天然去重;
  数据库层再加 (product_id, file_hash) 唯一约束,避免同一商品挂载重复素材。
- 原始文件永远不得覆盖 —— 写盘前检查目标是否存在,存在则跳过写入。
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.product import Product


class ProductAsset(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "product_assets"
    __table_args__ = (
        # 去重键必须带上角色:同一张图既可能是正面图也可能是抠图,
        # 只按 (商品, 哈希) 去重会让「换个角色重传」静默返回旧记录,
        # 而且不会按新角色重新校验(抠图要求 alpha 通道,正面图不要求)。
        UniqueConstraint(
            "product_id", "file_hash", "asset_type",
            name="uq_product_assets_product_id_file_hash",
        ),
        Index("ix_product_assets_file_hash", "file_hash"),
        Index("ix_product_assets_asset_type", "asset_type"),
    )

    product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- 文件元信息 ---
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- 基础检查结果 ---
    check_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    check_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    product: Mapped[Product] = relationship(back_populates="assets")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ProductAsset {self.asset_type} {self.file_hash[:8]}>"
