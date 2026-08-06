"""设置页出入参(阶段 8)。

出参刻意是\"自描述\"的:字段类型、选项、范围、来源一起给,
前端不需要认识任何一个具体配置项就能把页面画出来。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SettingOptionOut(BaseModel):
    value: str
    label: str


class SettingFieldOut(BaseModel):
    key: str
    label: str
    type: str
    help: str = ""
    placeholder: str = ""
    secret: bool = False
    advanced: bool = False
    minimum: float | None = None
    maximum: float | None = None
    options: list[SettingOptionOut] = Field(default_factory=list)
    #: 当前生效值;密钥字段是打码串,永远不是明文
    value: str = ""
    has_value: bool = False
    #: db(后台改过) | env(环境变量给的) | default(代码默认值)
    source: str = "default"
    env_present: bool = False
    #: 真值表示这一项被 SETTINGS_ENV_LOCK 钉死,后台改不了
    locked: bool = False
    updated_at: str | None = None
    updated_by: str | None = None


class SettingGroupOut(BaseModel):
    key: str
    title: str
    description: str = ""
    badge: str = ""
    fields: list[SettingFieldOut] = Field(default_factory=list)


class EncryptionStatusOut(BaseModel):
    available: bool
    source: str
    message: str


class SettingsOut(BaseModel):
    groups: list[SettingGroupOut]
    env_lock: bool
    encryption: EncryptionStatusOut


class SettingsUpdate(BaseModel):
    #: 只送改动过的项。空字符串 = 删掉覆盖,回到环境变量/默认值。
    #: 密钥字段不送就是不改 —— 界面上的打码串不需要、也不能回传。
    values: dict[str, Any] = Field(default_factory=dict)


class SettingsReset(BaseModel):
    keys: list[str] = Field(default_factory=list)


class SettingsWriteResult(BaseModel):
    updated: list[str]
    message: str
