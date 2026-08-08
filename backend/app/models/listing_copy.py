"""刊登文案与内容计划(M6,§8)。

## 两层结构

    content_plans   平台无关、语言无关的**事实锚**
    listing_copies  某个 (渠道, 站点, 语言) 的具体文案

多语言一致性靠**共享同一份 facts** 保证,不是靠一次调用同时出多语言。
后者的代价:JSON 更长更易截断、一个语言失败拖垮全部、各语言字符限制不同、
巴葡需要本地表达而不是翻译、语言越多成本越高。

## claims 不是模型的判断

它是**模型对自己文本的结构化标注**:「我在标题里第 12-21 个字符处
写了 navy blue,对应 primary_color=NAVY_BLUE」。每一条都能被后端逐字验证。

这与「不采信模型自报结论」不冲突 —— 我们采信的不是它的判断,
是一个可以被机械校验的位置索引。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import CopyStatus, PlatformStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ContentPlan(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """平台无关的事实锚(§8.1)。"""

    __tablename__ = "content_plans"
    __table_args__ = (Index("ix_content_plans_spu", "spu", "created_at"),)

    spu: Mapped[str] = mapped_column(String(64), nullable=False)
    #: **只能来自 status=CONFIRMED 的属性。** 由后端组装,模型不得新增或修改键值
    facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: 可由模型生成,但每条必须能追溯到某个 fact 或人工填写的卖点库
    selling_points: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    #: 类目规则 + 站点法规生成。例如未做检测就不许写 UPF 数值
    forbidden_claims: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    facts_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    #: 组装时用到的属性版本 id。进草稿指纹 —— 属性一改,草稿就该 STALE
    attr_snapshot_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )


class ListingCopy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """某个 (渠道, 站点, 语言) 的文案(§8.3)。

    键是 `(spu, channel, site, locale, version)`。v1 只按 locale 分版本 ——
    那样 SHEIN 和 Mercado Livre 的英文标题会互相覆盖,而两家的长度上限、
    是否允许品牌词、bullet 条数完全不同。
    """

    __tablename__ = "listing_copies"
    __table_args__ = (
        UniqueConstraint(
            "spu", "channel", "site", "locale", "version",
            name="uq_listing_copies_version",
        ),
        Index("ix_listing_copies_lookup", "spu", "channel", "site", "locale", "status"),
    )

    spu: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 'GENERIC' 表示平台无关的来源稿
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="GENERIC")
    #: 'ANY' 表示该渠道通用。
    #: 用哨兵值而不是 NULL:这两列进唯一键,而 PostgreSQL 把 NULL 当成互不相等
    site: Mapped[str] = mapped_column(String(32), nullable=False, default="ANY")
    category_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_plan_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("content_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    bullet_points: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: 模型对自己文本的结构化标注(§8.4)。后端逐条验证
    claims: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CopyStatus.DRAFT.value
    )
    violations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    model_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    repair_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 生成时依据的渠道规则版本。规则换版之后旧文案要重新校验
    spec_version: Mapped[str] = mapped_column(String(32), nullable=False, default="0")
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # ---- 快审退回(A10,迁移 0020)----
    #
    # 文案这一侧有个图片集没有的坑:`CopyStatus.REJECTED` **本来就有人用** ——
    # 规则校验有硬失败时 `save_copy` / `revalidate` 也置这个状态。于是
    # "状态是 REJECTED"回答不了"是谁退的":是禁词扫出来的,还是人看完退的?
    #
    # 判据因此是 `reject_reason` 有没有值,不是状态本身(见 `flow._evaluate_copy`)。
    # 只看状态的话,一版因禁词被驳回的文案会在界面上显示
    # 「文案被快审退回:未注明原因」—— 一句完全错误的话,而且不会有任何报错。
    #
    # 退回为什么留得住:改文案走 `save_copy`,每次都是**新版本行**,
    # 而 `approve_copy` 要求状态必须是 VALIDATED —— 被退回的那一版
    # 既不会被原地改回去,也不会被直接批准。它就停在那里,直到有人真的改。
    reject_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reject_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ListingDraft(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """持久化的刊登草稿(§9.1)。

    纯数据层是 `app/listings/contracts.ListingDraftData`。两者不是同一个东西:
    那个是渠道适配器的输入(可离线测试),这个是页面、审核、导出、发布
    共同指向的实体。

    v1 把两者混成一个「无数据库依赖的纯数据类」,同时又给它设计了
    `POST /api/listings/drafts` 和长期引用它的发布任务 —— 自相矛盾。
    """

    __tablename__ = "listing_drafts"
    __table_args__ = (
        Index("ix_listing_drafts_lookup", "spu", "channel", "site", "status"),
    )

    spu: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    site: Mapped[str] = mapped_column(String(32), nullable=False)
    category_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="BUILDING")

    #: 构建时的 CanonicalProduct(含属性版本 id)
    canonical_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    image_set_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("listing_image_sets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: locale -> listing_copies.id
    copy_version_ids: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    #: 运营手填的渠道字段,可再编辑
    manual_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    mapped_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    validation_warnings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    mapping_version: Mapped[str] = mapped_column(String(32), nullable=False, default="0")
    spec_version: Mapped[str] = mapped_column(String(32), nullable=False, default="0")
    template_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 上游事实的指纹。任一变化 -> 指纹变化 -> 草稿 STALE -> 不允许导出与提交
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    # ---- 颜色维的上游快照(PRD v3.1 §4.10,迁移 0049)----
    #
    # 指纹回答「变没变」,`canonical_snapshot["components"]` 回答「哪个轴变了」,
    # 而两者都**没有颜色维**:新增一个 ACTIVE 颜色、某个颜色的样品换了、
    # 某个颜色的方案换了指纹,这三件事今天在草稿这一侧一个字都说不出来。
    #
    # 形状与判定在 `app/workbench/upstream_snapshot.py`(零依赖纯函数),
    # 这里只落列。**草稿不复制事实值,只存版本引用与提交快照**(§4.10 原话)。
    #
    # ## 为什么可空,且不给 server_default
    #
    # 与 `ListingDraft` 上其它 JSONB 列(`canonical_snapshot` 等)刻意不同。
    # 那几列给 `{}` 是对的:它们从第一版起就有写入点,空字典的含义是
    # 「这份草稿确实没有手填字段」。
    #
    # 这两列不是。0049 之前建的草稿一行都没算过颜色维,而 `{}` 的含义是
    # 「算过,颜色维是空的」—— READY 门禁读到它会在颜色轴上**静默放行**,
    # 表现是一份缺了整个颜色的草稿显示「上游全部有效」。
    #
    # 留 NULL,让「没算过」如实显示成「不知道」;判定层把 None 判成
    # 不可证明、不放行(`diff_upstream` / `map_problems` 的第一个分支)。
    # 与迁移 0042 / 0045 / 0048 是同一条规矩的第四次应用:**默认值不许让
    # 任何人被静默放行或静默拦下。**
    #
    # 写入点在 `workbench/service.build_draft`,**本批(5-1)未接**,
    # 记在 `tools/audit_column_writers.LEDGER`,还款日:阶段 5。
    upstream_versions: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: 颜色 → SKU → 主图/附图 的最终映射(§4.10)。可空口径同上一列
    color_sku_image_map: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 最近一次导出(迁移 0017)。完整导出历史在审计日志里,这里只放页面要直接显示的
    exported_at: Mapped[datetime | None] = mapped_column(nullable=True)
    exported_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    export_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 平台侧状态(迁移 0019,OPS-REVIEW P1)。取值见 `core.enums.PlatformStatus`。
    #: 系统看不见平台,这三列由运营在导出页手工录入;REJECTED 只能由
    #: 「记录驳回」写入(`workbench/platform_service.py`),不开手工直设
    platform_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PlatformStatus.NONE.value, server_default="NONE"
    )
    platform_status_at: Mapped[datetime | None] = mapped_column(nullable=True)
    platform_status_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
