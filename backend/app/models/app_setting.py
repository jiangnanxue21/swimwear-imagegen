"""后台设置覆盖值(阶段 8)。

只存\"被后台改过\"的项 —— 没有行就表示这一项还是环境变量或默认值说了算。
这样做的好处是回滚很干净:删掉一行,配置立刻回到部署时的样子。

密钥类字段的 ``value`` 是密文(见 app/core/secrets.py),``value_hint`` 是给人看的
末位打码串。列表接口只读 hint,不需要解密,少一次动到明文的机会。
"""
from __future__ import annotations

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AppSetting(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "app_settings"

    #: 配置项名,与 Settings 的字段名一一对应,例如 FASHN_API_KEY
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: 明文;is_secret 为真时这里是密文
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 密钥的末位打码串,仅用于展示
    value_hint: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
