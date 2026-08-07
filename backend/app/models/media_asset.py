"""统一素材模型(M1,§4)。

任何图片 —— 人工上传、外链导入、供应商推送、AI 生成、平台回抓 ——
进系统的第一步都是变成一条 `MediaAsset`。链路里不允许出现第二种「图片对象」。

## 为什么这张表值得单独建

`ProductAsset` / `GenerationCandidate` / `OutputAsset` 三套并行的代价,
在识别层要写第一行代码时就会显现:「这个商品有哪些图可以用来识别属性」
这个问题要查三张表、拼三种形状,而三张表的去重键、角色语义、
可用性判据各不相同。后面每加一层(图片集、草稿、渠道)都要再兼容一次。

## `source` 与 `role` 正交

这是本表最重要的一条设计。v1 把「是不是 AI 生成的」和「它是正面图还是
尺码表」混在 `AssetType` / `OutputAsset.purpose` 里,后果是 AI 图天然只能是
成品图,而供应商推来的正面图和人工上传的正面图要走两条代码路径。

契约层的对应物在 `app/media/contracts.py`,那边是纯数据、可离线测试;
这边是它的一种存储方式。两者不是同一个东西,不要合并。
"""
from __future__ import annotations

# datetime 必须是运行时 import:SQLAlchemy 2 建映射时要解析 Mapped[...] 注解
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import MediaStatus, RoleSource
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MediaConsent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """素材授权(§12.1)。

    四个 scope 标志位相互独立:「可以用 AI 处理」不等于「可以传到境外」,
    更不等于「可以商用发布」。v1 只记一句「授权来源」,这三个问题一个也答不了。

    调用前置检查是 **fail closed** 的:缺任何一项直接拒绝调用,不做降级。
    """

    __tablename__ = "media_consents"

    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: ConsentScope 的值列表
    scope: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    allowed_regions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    allowed_processors: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    #: 默认禁止用于训练。默认值选 True 而不是 False:
    #: 授权范围没写清楚时,保守的那一边才是安全的那一边
    training_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    valid_from: Mapped[datetime | None] = mapped_column(nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(nullable=True)
    document_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)


class MediaAsset(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一条素材。

    ## 去重键(§4.8,A45-batch14-26 / 迁移 0044)

    PRD §4.8 逐字要求 ``UNIQUE(spu_id, COALESCE(color_variant_id,''), sha256)``。
    本表落的是**两条局部唯一索引**,不是一条 —— 理由写在 0044 的模块文档里,
    一句话版本:``spu_id`` 可空,而 UNIQUE 里 ``NULL <> NULL``,一条式落法会让
    **所有没有 SPU 的存量素材彻底失去去重**,那比今天更糟。

        uq_media_assets_spu_colour_sha256   WHERE spu_id IS NOT NULL   §4.8 那个键
        uq_media_assets_product_sha256      WHERE spu_id IS NULL       旧键,兜底

    两条互斥且合起来覆盖全表,所以**任何一行都恰好落在一条键下**,不存在
    「两条都不管」的行。第二条是**会自净的**:老建档路径从本批起必带
    ``spu_id``(见 `product_service.create_product`),存量回填完成之后它
    覆盖零行,那时 ``spu_id`` 收 NOT NULL 并把它删掉是同一个动作。

    跨 SPU **不去重**:两个 SPU 用了同一张图是两条记录,因为角色、变体绑定、
    授权、隔离状态都是按商品算的。物理存储层仍然只存一份(内容寻址)。
    """

    __tablename__ = "media_assets"
    __table_args__ = (
        # §4.8 的键。局部索引 —— `spu_id` 为空的行走下面那条兜底键。
        # 用 `func.coalesce` 而不是 Python 侧的 `or ''`:索引表达式必须由
        # 数据库求值,写成 Python 表达式时它会在建表那一刻被求成常量。
        Index(
            "uq_media_assets_spu_colour_sha256",
            "spu_id",
            func.coalesce(text("color_variant_id::text"), text("''")),
            "sha256",
            unique=True,
            postgresql_where=text("spu_id IS NOT NULL"),
        ),
        # 兜底键:没有 SPU 的行仍按商品去重。**不是历史遗留,是覆盖面的另一半。**
        Index(
            "uq_media_assets_product_sha256",
            "product_id",
            "sha256",
            unique=True,
            postgresql_where=text("spu_id IS NULL"),
        ),
        Index("ix_media_assets_product_status", "product_id", "status"),
        Index("ix_media_assets_spu_role", "spu", "role"),
        Index("ix_media_assets_sha256", "sha256"),
        # §4.8 / 风险表 D3:AI 来源的图永远不许是商品证据。
        # 这是 `evidence_rules.violates_ai_check` 的库级孪生 —— 那个函数让
        # 纯层能穷举证明"派生函数产不出违规组合",这条 CHECK 拦的是
        # **绕过派生函数的直写**。两者缺一:只有函数时,一条 raw UPDATE 就穿透;
        # 只有 CHECK 时,这条不变式只能在有 PostgreSQL 的地方验,而真库用例池
        # 里已经压着 60 条没跑过的了。
        #
        # `evidence_class IS NULL` 放行:存量行还没回填,而回填是脚本的事。
        # 这不是缺口 —— NULL 在读取侧当作"没算过",本来就进不了白名单。
        CheckConstraint(
            "evidence_class IS NULL "
            "OR NOT ("
            "  (source = 'AI_GENERATED'"
            "   OR generation_task_id IS NOT NULL"
            "   OR generation_candidate_id IS NOT NULL)"
            "  AND evidence_class = 'PRODUCT_EVIDENCE'"
            ")",
            name="ck_media_assets_ai_not_product_evidence",
        ),
    )

    product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    #: **SPU 归属的权威列(§4.8)。A45-batch14-26 / 迁移 0043。**
    #:
    #: 在它之前,这张表挂 SPU 靠的是下面那个 `spu: String(64)` 字符串码,
    #: 而 `products` 从 batch13 起挂的是 `spu_id` 外键。两张表指着同一个
    #: SPU、用两种口径回答「是不是同一个 SPU」—— 改一次 `spu_code`,
    #: `products` 跟着走(外键),`media_assets` 原地断开(字符串码),
    #: 而断开之后按 SPU 聚合的素材会**静默少掉一批**,没有任何地方会报。
    #:
    #: 可空,理由与 `products.spu_id` 相同:老建档路径的存量行还没有 SPU。
    #: 但**新写入必须有** —— 写入侧的闸在 `media/service.py`,
    #: 因为去重键的覆盖面(见 `__table_args__`)依赖这一列的空/非空来分流。
    #:
    #: `ondelete="RESTRICT"`:与 `products.spu_id` 不同,这里不允许级联。
    #: 删掉一个 SPU 而把它的素材留成孤儿,等于让那批图落进兜底键,
    #: 于是同一张图在「删 SPU 前」和「删 SPU 后」的去重结果不一致。
    spu_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("spus.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    #: 反规范化读列。**权威是上面的 `spu_id`。**由服务层随 `spu_id` 同步,
    #: 与 `products.spu` 同口径(§4.4)。保留它是因为 `ix_media_assets_spu_role`
    #: 与几处按 SPU 码聚合的查询在读,换掉它是另一批的事。
    spu: Mapped[str | None] = mapped_column(String(64), nullable=True)

    source: Mapped[str] = mapped_column(String(24), nullable=False)
    role: Mapped[str | None] = mapped_column(String(24), nullable=True)
    #: 角色是谁定的。模型定的不能直接用于主图位(§7.3)
    role_source: Mapped[str] = mapped_column(
        String(8), nullable=False, default=RoleSource.UNSET.value,
        server_default=RoleSource.UNSET.value,
    )
    role_confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    #: 该图对应哪个颜色/变体的**识别建议位**(§4.8)。可空,人工或识别补。
    #:
    #: **它不是归属。**权威归属是下面那一列 `color_variant_id`。
    #: 两者并存是刻意的:hint 是模型/文件名猜出来的,外键是有人认下的。
    #: 拿 hint 决定「这张图算哪个颜色的证据」等于让模型猜出来的值决定
    #: 运营能不能往下走、以及下一次付多少钱 —— 14-9 / 14-10 / 14-13
    #: 三批各自拒绝过一次。
    variant_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: **阶段 2 的溯源列(§4.8)。A45-batch14-16 / 迁移 0038。**
    #:
    #: 「这张图是本系统生成出来的吗、是哪一次生成的哪一张候选」。
    #: 在它们之前,这个问题由 `legacy_kind='generation_candidate'` 回答 ——
    #: 那是迁移期的过渡记号,而三个判定模块都已经写好在读这两列
    #: (`getattr` 取,列不存在时退回 None):
    #:
    #:     evidence_rules.derive_evidence_class   §4.8 规则表的第一条
    #:     provenance_conflict.verdict            AC-22「AI 图伪装拦截」
    #:     provenance_conflict.may_fill_role      去重命中那一路的补角色闸
    #:
    #: 第二、三条在这两列落库之前**只有过渡记号那一路真的会命中**
    #: (§11 批自己写下的那句「今天唯一真的会命中的那一路」)。
    #:
    #: `ondelete="SET NULL"`:清理生成任务不该连坐删掉素材文件。但代价要写明 ——
    #: **溯源断了之后,那张 AI 图会重新变成一条看起来干净的素材**
    #: (`evidence_class` 由 source 派生,而 `source=AI_GENERATED` 还在,
    #: 所以它仍然拦得住;真正靠溯源列拦的是**被人重新上传成 MANUAL_UPLOAD**
    #: 的那一份 —— 那一份指着的是候选行,而候选行不会随任务删除消失)。
    generation_task_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("generation_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    generation_candidate_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("generation_candidates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    #: **阶段 2 的归属外键(§4.8)。A45-batch14-15 / 迁移 0037。**
    #:
    #: 这一列此前不存在,而它挡着四批已经验完的判定:
    #:
    #:     scope_fingerprint      双作用域指纹(14-9)——AC-21 的落点
    #:     sample_completeness    §6.2 门禁的颜色作用域(14-10)
    #:     run_state.scope_of     §11 第一行的失败归集(14-13)
    #:     evidence_assets_for    §5.1 白名单的 scope 参数
    #:
    #: 四批各自留了一条「这一列落库那天我会红」的欠账守卫,本批把它们收掉。
    #:
    #: **可空,且不设默认。**存量素材的归属是**未确认**,而不是「属于某个
    #: 颜色」也不是「通用」——把它们默认成通用会让每一张历史图凭空
    #: 成为全部颜色的共享证据。可空的代价是下游必须处理 None,
    #: 而那正是「通用素材只进共享作用域」那条规则本来就要处理的情况。
    #:
    #: `ondelete="SET NULL"`:删掉一个颜色不该连坐删掉素材文件 ——
    #: 素材是实物样品的照片,颜色是业务分组,后者没了前者还在。
    color_variant_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("color_variants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    #: **§4.8 的证据等级。A45-batch14-26 / 迁移 0045。**
    #:
    #: 在它之前这是一个纯派生量(`evidence_rules.derive_evidence_class`),
    #: 每个读取点各算一次。落成存储列的理由**不是省 CPU**,是 §5.1 白名单
    #: 需要在 SQL 里过滤:派生量过滤不了,于是白名单只能先把全部素材取出来
    #: 再在 Python 里筛 —— 而"先全取"这件事在素材上万之后本身就是问题。
    #:
    #: ## 落了列之后,"唯一写入点"这句话为什么还成立
    #:
    #: D3 那条风险(独立赋值的存储列会与 `source` / 溯源列漂移)是真的,
    #: 所以这一列**不接受赋值**:
    #:
    #:   - 应用层唯一写入路径仍是 `derive_evidence_class`,由
    #:     `media/service.py` 在落库前调用一次;
    #:   - 库级 `CheckConstraint` 钉住那条不变式(AI 来源 → 不是商品证据),
    #:     它是 `violates_ai_check` 的孪生,拦的是绕过派生函数的直写;
    #:   - `tests/pure` 有一条 AST 守卫钉着"路由层与脚本不许直接赋值"。
    #:
    #: 三件套之前是"派生 + CHECK",现在是"派生 + 存储 + CHECK"。中间那件
    #: 新增的是**缓存**,不是第二个事实源 —— 区别在于它没有独立的写入路径。
    #:
    #: ## 可空,且不回填在迁移里
    #:
    #: 存量行留 NULL,由 `app/scripts/backfill_evidence_class.py` 补。
    #: 迁移不许 import `app.*`(全仓 42 份没有一份破例),所以在迁移里回填
    #: 只能把四条规则冻成一段 SQL CASE —— **那就是第二个判定点**,
    #: 也正是 0040 当时逐条权衡后拒绝的那个做法。脚本可以 import 派生函数,
    #: 于是回填与运行时走的是同一段代码。
    #:
    #: 读取侧遇到 NULL 的口径:**当作没算过,不当作 REFERENCE_ONLY**。
    #: 后者会让一批存量商品证据凭空退出识别输入,而那是静默的。
    evidence_class: Mapped[str | None] = mapped_column(String(24), nullable=True)

    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: BigInteger:原图可能超过 2GB 的场景不存在,但列类型不该是将来某天
    #: 需要一次迁移才能放开的那种限制
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MediaStatus.PENDING.value
    )
    #: 确定性图像指标(清晰度、亮度、模糊度、是否有水印)。
    #: **不是模型打的分** —— 它要进 system_confidence 的 quality_factor,
    #: 用一个不确定的量去修正另一个不确定的量没有意义
    quality_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # --- 合规与数据治理(§12) ---
    processor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processing_region: Mapped[str | None] = mapped_column(String(16), nullable=True)
    training_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    consent_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("media_consents.id", ondelete="SET NULL"),
        nullable=True,
    )
    retention_policy: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: 隔离原因。`status=QUARANTINED` 时必须能回答「为什么」,
    #: 否则人工放行时只能靠猜
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: --- 迁移期溯源(M1 专用) ---
    #: 这条素材是从哪张旧表记录影子写/回填过来的。
    #: 双读对账靠它把新旧两边对上;迁移收口后这两列可以删,
    #: 但**在删除旧表之前不要删它们** —— 它们是唯一能回答
    #: 「这条素材对应旧表的哪一行」的东西
    legacy_kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    legacy_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)


class MediaDerivative(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """同一张图的不同尺寸/底色/带水印版本(§4.3)。

    **派生版本不是新素材。** 属性识别、图片集、去重、授权、审计一律以
    `MediaAsset` 为单位。平台要 1:1 主图时,取的是「图片集里那一项的素材的
    SQUARE_1_1 派生」,而不是另一条素材 —— 否则同一张图会在素材库里
    出现五遍,去重、授权和保留策略全部失效。

    唯一键带 `transform_version`:裁切算法改了之后,新旧两版可以共存,
    网站缓存里的旧 URL 不会 404。
    """

    __tablename__ = "media_derivatives"
    __table_args__ = (
        UniqueConstraint(
            "source_media_asset_id",
            "purpose",
            "transform_version",
            name="uq_media_derivatives_source_purpose_version",
        ),
        Index("ix_media_derivatives_source", "source_media_asset_id"),
    )

    source_media_asset_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    height: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    #: 裁切/补白/压缩算法版本,可复现
    transform_version: Mapped[str] = mapped_column(String(32), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class MediaMigrationMismatch(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """双读对账发现的不一致(迁移 B,§4.4)。

    这张表**只在迁移期存在**,出口条件是「一周无新增」。

    为什么要落表而不是只打日志:日志是滚动清理的,而「一周无 mismatch」
    是一个需要被查询的判据。只打日志的话,到了该切换的那天,
    没有任何人能拿出证据说明这一周确实是干净的。
    """

    __tablename__ = "media_migration_mismatches"
    __table_args__ = (
        Index("ix_media_mismatch_kind_created", "legacy_kind", "created_at"),
    )

    legacy_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    legacy_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    media_asset_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    #: 哪个字段对不上
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    legacy_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
