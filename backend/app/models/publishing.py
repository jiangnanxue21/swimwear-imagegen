"""发布领域模型(方案 v4.1 第 7.2 节 / 任务 11)。

## 这三张表此前一张都不存在

附录 B.5 已核实:`backend/app/models/` 下 17 个模型文件没有任何发布域表。
所以这是**全新建模 + 全新迁移**,不是在既有表上加列 —— 9.1 节把 Phase 2 工期
上调的主要依据就是这件事。

12.2 节的关键路径 `0 → 11 → 12/13 → 14 → 15/16 → 20 → 24` 上,任务 11 是
除 CI 之外唯一没有前置依赖的起点:Simulator(12)、Adapter(13)、幂等创建更新
(14)、状态轮询(15)、DELIST(16)、前端发布页(20)全部要往这三张表里写东西。

## 三张表的分工

    ChannelListing   当前事实 —— 这个商品在这个渠道这个店铺现在是什么状态
    PublishAttempt   历史记录 —— 第 N 次提交发生了什么,写完不改
    PublishOutbox    调度状态 —— 这次提交什么时候发、谁领走了、还剩几次重试

三者不合并的理由,是它们的**可变性**不同。把尝试历史压进 listing 的一个 JSON
列里,「上一次超时的那次调用到底有没有到平台」就变成一个字符串解析问题;
而这恰恰是出事之后第一个要查的东西。`platform_rejection.py` 里已经为同一类
问题写过一次同样的论证。

## 与既有 `listing_drafts` 的关系

草稿是**本地内容**(图片集 + 文案 + 属性映射出来的那份 payload),
ChannelListing 是**外部结果**。一份草稿可以被发到多个渠道/站点,
每个组合一行 ChannelListing —— 这就是唯一约束选在
`(channel, shop_id, site, product_id)` 而不是 `draft_id` 上的原因。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
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

from app.core.enums import PublishAttemptStatus, PublishOutboxStatus, PublishStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChannelListing(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一个商品在一个渠道/店铺/站点上的当前状态。

    唯一约束是这张表的核心:同一个商品在同一个店铺的同一个站点,只能有一行。
    没有它,一次重复点击就会产生第二行,而「这个商品上架了吗」从此需要
    先决定信哪一行 —— 那种问题没有正确答案,只能靠人去平台上核对。
    """

    __tablename__ = "channel_listings"
    __table_args__ = (
        # 幂等的最后一道防线。应用层的幂等键挡的是「同一次提交发两遍」,
        # 这条挡的是「两个不同的请求都以为自己是第一次」——并发下应用层
        # 的先查后写挡不住,数据库的唯一索引挡得住。
        UniqueConstraint(
            "channel",
            "shop_id",
            "site",
            "product_id",
            name="uq_channel_listings_channel_shop_site_product",
        ),
        # 轮询器的取数路径(任务 15)。原来这条索引建在 `last_polled_at` 上,
        # 因为当时设想的取数是「按上次轮询时间排序」。真接上轮询器之后,
        # 取数条件其实是 `next_poll_at <= now` —— 退避必须落到列上,不能
        # 靠"上次问过之后隔多久"现算(现算就没法对不同的行用不同的间隔)。
        # 索引跟着真实查询走,而不是让查询迁就一条已经建好的索引。
        Index("ix_channel_listings_status_next_poll", "status", "next_poll_at"),
        # 4.1 节 H 的清理清单:按测试批次一次性列出本轮创建了哪些外部商品
        Index("ix_channel_listings_test_batch", "test_batch_tag"),
        Index("ix_channel_listings_product", "product_id"),
        # A-13 的提交证据链:`platform_service._publish_attempt_entries()` 按
        # `draft_id` 反查这条 listing 名下成功的 PublishAttempt,拿它当作与
        # "新导出"等价的修复证据。上面几条索引没有一条以 `draft_id` 打头
        # (唯一约束的前导列是 `channel`),没有它这个 WHERE 只能顺序扫全表。
        # 顺带覆盖 `ondelete="RESTRICT"` 的约束检查 —— PG 不会给外键自动建索引。
        # 迁移 0029。
        Index("ix_channel_listings_draft", "draft_id"),
    )

    #: 渠道代号,与 `app/channels/` 下的适配器目录名一致(shein / generic / ...)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 站点/国别代码。同一个店铺可能同时经营多个站点,它们的类目和属性都不同
    site: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 店铺 ID。凭证按店铺隔离,真实测试要求专用测试店铺(4.1 节 H)
    shop_id: Mapped[str] = mapped_column(String(64), nullable=False)

    product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: 当前指向的草稿。RESTRICT 而不是 CASCADE —— 平台上还挂着的商品,
    #: 不能因为本地删了一份草稿就悄悄失去它的来源记录
    draft_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("listing_drafts.id", ondelete="RESTRICT"),
        nullable=True,
    )

    #: 平台侧 SPU ID。创建成功之前为空 —— 它是「这次提交真的到达了平台」
    #: 的唯一凭据,4.1 节 H 的清理预案靠它列出「本轮测试创建了哪些外部商品」
    external_spu_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 平台侧 SKU ID 映射 {本地 sku: 平台 sku_id}。数量随规格组合走,
    #: 用 JSONB 而不是再开一张表:它整体读写,没有单独查询某个 SKU 的场景
    external_sku_ids: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: 本地状态机(`PublishStatus`)。String 而不是数据库枚举 ——
    #: 理由见 core/enums.py,加一个取值不该触发一次迁移
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PublishStatus.DRAFT.value
    )
    #: 平台返回的原始状态字符串,照存不翻译。本地状态是我们的解读,
    #: 这一列是平台的原话 —— 两者不一致时,能查出是解读错了还是平台变了
    last_platform_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 上一次真的问过平台的时刻。**只用于展示与排查**(「这个状态是什么时候的」),
    #: 调度不看它 —— 调度看 `next_poll_at`。两者分开是因为它们会不一致:
    #: 一次问不到答案的轮询也会推进 next_poll_at(退避),但它没有更新我们
    #: 对平台的了解,所以不该动 last_polled_at
    last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: 下次可以问平台的时刻。退避写在列上而不是 sleep 里,理由同
    #: `PublishOutbox.next_attempt_at`:sleep 在进程重启之后就没了
    next_poll_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: 已经问过几次。退避曲线的输入(`poll_policy.poll_backoff_seconds`)。
    #: 每次拿到平台的**新状态**时归零 —— 退避针对的是"一直没动静",
    #: 而不是"这一行存在了多久"
    poll_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: 测试批次号(4.1 节 H)。真实提交前由调用方带进来,之后
    #: 「本轮测试创建了哪些外部商品」就是一次带索引的等值查询。
    #:
    #: 为什么不靠 `created_at` 的时间窗代替:人工测试会跨天、会中断、会和
    #: 别人的测试重叠,而"上周二下午三点到五点之间创建的"这种口径在需要
    #: 它的那一刻(清理的时候)最不可靠 —— 那时人只记得批次叫什么。
    test_batch_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PublishAttempt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一次提交尝试。**写完不改**(除了收尾时补 finished_at 与结果)。

    幂等键上的唯一约束是 4.1 节 D「重试不会重复创建」的地基:
    同一个键第二次插入会被数据库直接拒掉,而不是产生第二次真实调用。
    """

    __tablename__ = "publish_attempts"
    __table_args__ = (
        # 幂等键全局唯一。不加 channel 前缀是因为键本身已经包含渠道、
        # 店铺、站点和操作类型(见 workflows/publish_idempotency.py)
        UniqueConstraint("idempotency_key", name="uq_publish_attempts_idempotency_key"),
        Index("ix_publish_attempts_listing_started", "channel_listing_id", "started_at"),
        Index("ix_publish_attempts_status", "status"),
    )

    channel_listing_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("channel_listings.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: `PublishOperation`:CREATE / UPDATE / REPUBLISH / DELIST
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 见 7.4 节。128 字符容得下外部系统传进来的显式键
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 实际发出去的 payload 的指纹。和幂等键的分工见
    #: `publish_idempotency.build_request_fingerprint` 的文档
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PublishAttemptStatus.PENDING.value
    )
    #: 平台侧的请求 ID/追踪号。出事时这是唯一能和平台对账的东西
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: **脱敏后**的请求/响应快照。7.5 节:凭证明文不进 ChannelContext,
    #: 日志和审计不得包含签名。写入前必须过 `core/logging.py` 的脱敏,
    #: 这两列的命名带 safe_ 前缀就是为了让「直接塞原始响应」看起来不对劲
    safe_request_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    safe_response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class PublishOutbox(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一次待发送的提交。事务内写行,事务外发请求。

    没有这张表的话,「库里记了已提交、但请求其实没发出去」和
    「请求发出去了、但库里那条事务回滚了」这两种情况都无法避免 ——
    它们分别对应「平台上没有商品但本地显示已上架」和「平台上多了个
    没人认领的商品」,后者正是 4.1 节 H 要防的东西。

    `lease_until` 是 worker 领取的租约:领走时置未来时间,做完置 DONE。
    worker 崩了不会让这一行永久卡住 —— 租约过期后别人可以再领。
    这比「LEASED 就跳过」多一层保障,而两者的差别只有在 worker 真的
    崩过之后才看得出来。
    """

    __tablename__ = "publish_outbox"
    __table_args__ = (
        # 一次尝试只排一次队
        UniqueConstraint("publish_attempt_id", name="uq_publish_outbox_publish_attempt_id"),
        # 调度器的取数路径:挑出到点且没被别人领走的行。
        # 三列的顺序按选择性排:status 先把 DONE/DEAD 全部排除
        Index("ix_publish_outbox_due", "status", "next_attempt_at", "lease_until"),
    )

    publish_attempt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("publish_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PublishOutboxStatus.PENDING.value
    )
    #: 下次可发送的时间。退避策略写在这一列上而不是 sleep 里 ——
    #: 4.1 节 D 要求「状态轮询有退避」,而 sleep 在进程重启后就没了
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 租约到期时间。为空表示没人领。
    #: **它是回收的判据,不是落库的判据** —— 后者见下面 `lease_token`
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: 租约 fencing token(迁移 0047 / A45-batch17-2)。每一次领取生成一个新的,
    #: **上一任的令牌从此作废**:它的落库打在 `WHERE lease_token = :token` 上,
    #: 影响 0 行。无令牌调用由服务层显式拒绝;不能写成 `column == None`,
    #: SQLAlchemy 会把后者编译成 `IS NULL` 并匹配存量行。
    #:
    #: 为什么不能用 `lease_until` 代替:`_renew_lease()` 在发请求之前就把它改了,
    #: 于是"领取时的值"在落库那一刻已经不在库里 —— 拿它当判据会让**正常路径**
    #: 也落不了库。判据必须是一个续租时不变、重领时必变的值。
    #:
    #: 与 `batch_job_items.lease_token` 是同一个东西同一套语义(迁移 0026),
    #: 两条链路当时只修了一条,发布这条的缺口由本列关掉
    lease_token: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
