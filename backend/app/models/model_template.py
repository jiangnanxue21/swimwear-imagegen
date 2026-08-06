"""模特模板(需求第十四章模特模板页面)。

PRD v2 §11 起,模板上带**受众**与**授权字段组**。这些字段不是"以后补"——
男装模特入库时如果这套字段还不在,第二批素材就会以第一批的方式进来,
而第一批当时没人要求填。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import Audience, ModelBodyType, ModelLicenseStatus, ModelPose
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ModelTemplate(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "model_templates"
    __table_args__ = (
        Index("ix_model_templates_enabled", "enabled"),
        # §10.5:模特筛选按受众硬过滤,这是候选列表查询的第一个谓词
        Index("ix_model_templates_audience", "audience"),
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 模特图存储路径。与 ProductAsset 共用同一套哈希去重存储。
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False, default="image/jpeg")
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    pose: Mapped[str] = mapped_column(String(32), nullable=False, default=ModelPose.UNKNOWN.value)
    body_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ModelBodyType.UNKNOWN.value
    )
    background: Mapped[str] = mapped_column(String(64), nullable=False, default="studio")
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- 受众(PRD v2 §10.5:硬约束的过滤键) ---
    #
    # **NOT NULL**,与 `Product.audience` 可空刻意不同:商品可以"待确认",
    # 模特不行 —— 模特本人总有受众,入库的人当场就知道。迁移 0030 把存量
    # 回填为 WOMEN(理由写在迁移文件里)。也没有 UNISEX:那是**商品**的
    # 取值,含义是两种受众的模特都能穿,不是存在一种"中性模特"。
    audience: Mapped[str] = mapped_column(
        String(16), nullable=False, default=Audience.WOMEN.value,
        server_default=Audience.WOMEN.value,
    )

    # --- 授权与合规(PRD v2 §11:每个模板必须记录的字段组) ---
    #
    # 回填默认值的取向是"**如实记录未核实**",不是"补签一份不存在的授权":
    # license_status=UNVERIFIED、age_verified=false、范围字段全空。
    # UNVERIFIED 不阻断新任务(否则存量全停摆),但会出现在管理员待补清单;
    # EXPIRED / REVOKED / 过了到期日则硬阻断 —— 执行点在
    # `model_template_service.assert_usable`,生成任务创建前必经(§11.3)。
    #: 素材来源(签约摄影、素材库采购、自有拍摄……)
    source: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    license_status: Mapped[str] = mapped_column(
        String(16), nullable=False,
        default=ModelLicenseStatus.UNVERIFIED.value,
        server_default=ModelLicenseStatus.UNVERIFIED.value,
    )
    #: 商业用途范围(自由文本,照抄授权条款,不做结构化猜测)
    commercial_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 可使用平台 / 国家或地区。空列表 = 未登记,**不是**"全部允许"
    allowed_platforms: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    allowed_regions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    license_start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    license_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: 是否允许 AI 换装 / 生成衍生图片
    allow_ai_dressing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    allow_derivative_images: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: 年龄确认(§11.1:泳衣只使用已确认成年的模特,男女同一标准)。
    #: 回填 false = "没人核实过",与授权字段同一取向
    age_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: 禁止使用的品类(category_code 列表)
    prohibited_categories: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    #: 停用原因(§11:enabled=false 时必须能回答"为什么")
    disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
