"""批量任务与幂等回执(阶段 3,任务书 §6)。

三张表,各自回答一个退出条件:

    batch_jobs             批次本身。「批量导出结果可定位到原始批次」的那个 id
    batch_job_items        每件商品一行。「单件失败不影响其他商品」在这里可观测
    batch_action_receipts  幂等回执。「不重复创建任务、不重复触发付费模型调用」

## 为什么回执要独立成表

把幂等键放在 `batch_job_items` 上只能防住"同一批次内重复执行"。但真实场景是
**跨批次**的:运营上午跑了一批文案,下午发现漏了几件,又建了一个新批次把
这些商品重新框进去 —— 那些上午已经生成过、上游一点没变的商品会被再跑一次,
每次都是一次真金白银的模型调用。

回执按 `idempotency_key` 全局唯一,和批次无关。于是"同一商品同一动作、上游没变"
在任何批次里都只会真的执行一次,后续全部命中回执并记一次 `reuse_count`。

§7.3 的注写着观测方式是"核对后端模型调用日志中同一商品同一动作的调用次数" ——
这张表就是那份日志的结构化版本:`paid_calls` 是真的调了几次,
`reuse_count` 是省下了几次。两个数字都能在界面上看到。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.workbench.batch import BatchItemStatus, BatchJobStatus


class BatchJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一次批量执行。

    状态取值来自 `workbench.batch.BatchJobStatus`,**没有 FAILED** ——
    §6.3 第三条明确:单件失败不导致整个批量任务失败。有失败的批次是
    `COMPLETED_WITH_ERRORS`,运营该做的是处理那几件,不是整批重跑。
    """

    __tablename__ = "batch_jobs"
    __table_args__ = (
        Index("ix_batch_jobs_created_at", "created_at"),
        Index("ix_batch_jobs_action_status", "action", "status"),
        # A45-batch12-2 / EX-06:请求级幂等。
        #
        # **唯一约束是这条修复的全部**,不是装饰:`create_batch` 里"先查有没有、
        # 没有就建"是一个 check-then-act,两个并发请求会双双查空、双双 INSERT。
        # 只有数据库能裁决谁赢,而 Python 那一层的任务是**认输之后回读**。
        UniqueConstraint("request_key", name="uq_batch_jobs_request_key"),
    )

    action: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=BatchJobStatus.QUEUED.value
    )
    #: 运营给这个批次起的名字。50 件的批次跑三轮之后,靠时间戳分不出哪个是哪个
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    #: 计划参数(include_exported 等)与计划阶段的计数快照。
    #: 存快照是为了让"执行前显示的可执行数"事后仍可对照 —— FE-301 的验收项
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # ---- A45-batch12-2 / EX-06:创建批次这条 HTTP 命令本身的幂等 ----
    #
    # 原来的幂等只覆盖**批次内部的付费动作**(`batch_action_receipts`)。那一层
    # 挡得住"同一商品同一动作被跑两遍",挡不住"同一个创建请求被受理两遍" ——
    # 运营双击、或者第一次的 HTTP 响应在回程丢了而客户端重发,库里就会多出
    # 一个逻辑上完全相同的批次:两份进度、两套审计,而运营分不出哪个是有效的。
    #
    # 通常不会直接重复扣费(两批条目的动作幂等键相同,第二次会命中回执或
    # 并发锁),但它和 EX-02 叠加时风险会累积,而且"批次中心里有两个一样的
    # 任务"本身就足以让人做出错误判断。
    #
    #: 客户端给的请求键(HTTP `Idempotency-Key` 头)。可空 —— 不带头的调用方
    #: 行为完全不变,这条修复不是强制的
    request_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 这次请求的入参指纹(动作 + 商品集合 + include_exported + label)。
    #:
    #: **同键同指纹返回原批次,同键不同指纹报 409。** 后者不能悄悄放过:
    #: 那说明客户端用同一把键提交了两件不同的事,而返回其中任意一个都会让
    #: 它以为另一件也做了。
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- 评审第 6 条:导出文件一次生成、不可变 ----
    #
    # 原来 GET 下载每次现场重建 XLSX + ZIP,并对每个商品 `record_export()`:
    # 浏览器重试、重复点击、预取都会改数据库状态、累加导出次数。
    # 现在文件由 POST 生成一次并落盘,这四列是它的身份;GET 只读字节。
    #: 存储层路径(services.storage 的 storage_path)。None = 还没生成过
    file_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 给人看的下载文件名(batch-xxxx-时间戳.zip)
    file_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    #: 文件字节的 sha256。重复下载校验"拿到的是同一份"靠它
    file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    file_generated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    items: Mapped[list[BatchJobItem]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="BatchJobItem.created_at",
    )


class BatchJobItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """批次里的一件商品。

    `error_code` 只允许取 `workbench.batch.BatchErrorCode` 的值 ——
    §6.3.1 的"映射到预定义的异常码,而非泛化的『处理失败』"是这一列的约束。
    不做数据库枚举:异常码会随着新发现的失败模式增加,加一个码不该触发一次迁移。
    校验责任落在 `batch.status_for()` / `error_meta()`,写库前必须过它们。

    `target_step` + `target_ref` 是"一键跳转到具体字段"的两个坐标。
    没有 ref 的失败仍然可跳到标签页,但那不叫一键 —— 所以执行器能给出
    字段名时必须给(见 `batch_service` 里草稿失败的处理)。
    """

    __tablename__ = "batch_job_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "product_id", name="uq_batch_job_items_batch_product"),
        Index("ix_batch_job_items_batch_status", "batch_id", "status"),
        Index("ix_batch_job_items_product", "product_id"),
        # 回收器的取数条件就是这两列(status='RUNNING' AND lease_until <= now)。
        # 它是一个**全表**扫描 —— 不限批次,因为死掉的 worker 不会告诉我们
        # 它领的是哪个批次
        Index("ix_batch_job_items_lease", "status", "lease_until"),
    )

    batch_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("batch_jobs.id", ondelete="CASCADE"), nullable=False
    )
    #: 评审第 17 条:原来这一列非空且 `ON DELETE CASCADE`,商品一删,
    #: 批次明细整行消失 —— 而下面冗余 sku / spu 的注释写的恰恰是
    #: "商品被删掉之后仍然可读"。两句话只能留一句:现在删商品把这一列
    #: 置 NULL,行留下,冗余的 sku / spu 继续回答"当时跑的是哪件"。
    product_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: 冗余 sku / spu:批次详情要在商品被删掉之后仍然可读(审计要求)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    spu: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: 动作作用域键。同 scope 在一个批次里只执行一次(见 batch.py)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=BatchItemStatus.PENDING.value
    )
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_step: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: 执行过几次(首次 + 重试)。重试不新建行,理由见 batch_service.retry_items
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 这一条真的触发了几次付费模型调用。命中回执时为 0
    paid_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 命中回执、复用了上次结果
    reused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # ---- 任务 17:worker 租约 ----
    #
    # 两列合起来回答一个问题:**这一件现在有人在跑吗。**没有它们时
    # `RUNNING` 有两种截然不同的含义 —— "正在跑"和"跑了一半 worker 死了"
    # —— 而库里长得一模一样,于是后者永远没人回收(见 `batch.lease_expired`)。
    #
    # `lease_owner` 单独看只用于排查(哪台机器领走的):**回收的判据仍然只有
    # `lease_until` 一个** —— 用 owner 判定"能不能抢"就得先知道"哪些 worker
    # 还活着",而那是一份系统里并不存在的名单。
    #
    # 但**落库**的判据不是回收的判据,见下面 `lease_token`。
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ---- A43 / BLOCK-02:fencing token ----
    #
    # 上面两列合起来只回答"现在有人在跑吗",回答不了**"跑完的这个人还是
    # 当初领走的那个人吗"**。少了后一个答案,时序上会出现:
    #
    #     1. worker A 领走,租约 30 分钟
    #     2. 这一件真的跑了 31 分钟(大图 + 重试)
    #     3. 回收器只看 lease_until,把它放回 PENDING
    #     4. worker B 领走并跑完,落 SUCCEEDED
    #     5. A 也跑完了,**无条件**把自己的结果盖上去
    #
    # 第 5 步覆盖的不只是一个状态字符串:`paid_calls` 会被重新累加,
    # `result` 指向的是 A 那一次生成的版本(B 生成的那一版从此没有引用),
    # 而 `finished_at` 变成 A 的时刻 —— 一件被跑了两次、花了两份钱的条目,
    # 在库里看起来和跑了一次一模一样。
    #
    # 每次领取生成一个新令牌,落库时把它写进 WHERE。**A 的 UPDATE 影响 0 行,
    # 于是 A 的结果只能进审计,进不了这一行。** 这就是 fencing:
    # 交接的时刻由令牌吊销定义,不由时钟定义。
    #
    # 可空:0026 之前的存量行没有令牌。它们的落库会被 `lease_still_held()`
    # 判成"已失去执行权" —— 方向是安全的那个(宁可让一个孤儿结果进审计,
    # 也不要让它覆盖一个可能已经正确的状态)。
    lease_token: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    #: 执行结果摘要(生成了哪个版本、导出了哪个指纹)。给审计看,不参与判定
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    job: Mapped[BatchJob] = relationship(back_populates="items")


class BatchActionReceipt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """幂等回执。**跨批次生效**,理由见模块注释。

    唯一约束在 `idempotency_key` 上,而键由「动作 + 作用域 + 上游指纹」算出
    (`batch.idempotency_key`)。上游一变,键就变,于是新的执行是"另一件事",
    不会被旧回执挡住 —— 幂等不等于"永远只做一次"。
    """

    __tablename__ = "batch_action_receipts"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_batch_action_receipts_key"),
        Index("ix_batch_action_receipts_scope", "action", "scope_key"),
    )

    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 评审第 5 条:回执生命周期。取值见 `workbench.batch.ReceiptStatus`。
    #: 原来"回执存在"被硬编码成"做成了",于是「模型已生成、合规校验拒了」
    #: 这种**钱已经花了**的失败没有落点,普通重试会再付一次费。
    #: 老数据没有这一列时按 SUCCEEDED 读(迁移里的 server_default 同一口径):
    #: 老回执只在成功时写,这个默认不改变它们的语义。
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SUCCEEDED", server_default="SUCCEEDED"
    )
    #: 首次执行它的商品。SPU 级动作下同 SPU 的其他 SKU 也命中同一条回执
    product_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    #: 真的调了几次付费模型。**逐图计费**:一件 4 图商品首次识别就是 4。
    #: 它回答的是"这条作用域一共花了多少",不是"跑过几趟" —— 后者看 `executions`
    paid_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: A-17:这条作用域被**真的开跑**过几次。`_claim_in_flight` 每认领一次 +1。
    #:
    #: 加这一列是因为"重复付费"原来判的是 `paid_calls > 1`,而第 4 条把识别
    #: 改成逐图计费之后,一件 4 图商品**首次**执行就是 4 —— 于是每个多图商品
    #: 都被台账标成 P0 重复付费,费用监控信号被自身正确的计费语义噪声化。
    #:
    #: 两个数各管一头:`paid_calls` 是钱,`executions` 是次数。判"同一上游指纹
    #: 被执行了两次"只能看后者。老数据没有这一列时按 1 读(迁移的 server_default
    #: 同一口径):存在即至少跑过一次,这个默认不会凭空造出异常。
    executions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    #: 被复用了几次。这个数字就是"省下的付费调用次数"
    reuse_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: A45-batch12-2 / EX-02:**这一次执行真的把请求发出去了吗,什么时候发的。**
    #:
    #: `status=IN_FLIGHT` 原来把两种处境完全不同的事压成了一个值:
    #:
    #:     崩在调用**之前**   请求根本没发出去,钱一分没花 —— 重跑免费且正确
    #:     崩在调用**之后**   对方可能已受理并计费 —— 重跑就是重复计费
    #:
    #: 分不出来时只能折中(`MAX_BILLED_EXECUTIONS = 2`:放过一次自动重跑),
    #: 而那一次重跑正是 EX-02 报的那笔**无人值守的重复付费**。这一列把区分
    #: 变成库里读得到的事实:由 `_mark_provider_call()` 在独立事务里、紧挨着
    #: 付费调用之前写入,于是崩溃之后它还在。
    #:
    #: ## 诚实说明:能被判成\"安全\"的那一侧窗口很窄
    #:
    #: 认领与调用之间只隔着几条语句,所以\"崩在调用之前但回执已存在\"是少数。
    #: 这一列的主要价值不是省人工,是**让\"停下来\"这个决定有依据**,并且给出
    #: 对账真正需要的东西:一个时刻。运营拿着它才能去服务商后台按时间窗查,
    #: 而不是对着一条只写着\"费用不明\"的记录发呆。
    #:
    #: 为空有两种含义,处理方式相同(见 `receipt_route` 的 `call_dispatched`):
    #: 一是这次执行还没走到调用;二是 0031 之前的存量行。后者一律按\"可能已发出\"
    #: 处理 —— IN_FLIGHT 本是瞬时状态,留在库里的存量行恰恰是有疑问的那些。
    provider_call_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    last_actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
