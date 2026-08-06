"""可编辑的提示词模板(带版本)。

沿用 ``RuleSet`` 的做法:同一个 ``key`` 下多个版本,只有一条 ``is_active``。
改提示词 = 新建一个版本并切换 active,旧版本留档。

为什么必须留档:提示词直接决定评分口径。三个月后有人问"这批图当时为什么判了 C",
如果提示词是原地覆盖的,这个问题就答不了了 —— 评分记录里存着分数,
但产生那个分数的判断标准已经没了。``CandidateEvaluation.raw_response`` 里
会记下当次用的 ``prompt_version``,两边对得上。
"""
from __future__ import annotations

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PromptTemplate(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "prompt_templates"
    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_prompt_templates_key_version"),
        Index("ix_prompt_templates_key_active", "key", "is_active"),
        # 同一个 key 只允许一条启用中的记录。
        #
        # 这条约束原先**只**写在 0007 迁移里,ORM 层没有。后果是
        # `Base.metadata.create_all()` 建出来的测试库缺了它 ——
        # 于是"并发启用会不会留下两条 active"这个问题,在测试环境里
        # 永远测不出来,而生产环境会 500。测试库和生产库的结构不一致,
        # 比没有测试更危险:它让人以为测过了。
        Index(
            "uq_prompt_templates_one_active",
            "key",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    #: 提示词用途。目前只有 vision_system_prompt 一个,留成字符串是为了
    #: 将来加别的用途时不必改表结构
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: 这一版改了什么、为什么改。回滚时唯一能依据的信息
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 谁改的。设置页的口令不区分人,这里存的是可选的自述署名
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
