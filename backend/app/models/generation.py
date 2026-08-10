"""生成任务相关模型(需求第八、九章)。

阶段 3 建三张表:任务、单次调用、候选图,外加 Provider 用量记录。
评分相关的 CandidateEvaluation / EvaluationProblem / ReviewItem / RuleSet 属于阶段 4;
OutputAsset 属于阶段 6。不提前建表。
"""
from __future__ import annotations

# datetime 必须是运行时 import:SQLAlchemy 2 会在建映射时解析 Mapped[...] 注解的字符串,
# 放进 TYPE_CHECKING 块会让 Mapped[datetime | None] 解析失败,整个模型层无法导入。
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    AttemptStatus,
    CandidateStatus,
    RoutingMode,
    TaskStatus,
    UnitsSource,
)
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class GenerationTask(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "generation_tasks"
    __table_args__ = (
        # 幂等键全局唯一:重复提交同一份输入不会产生第二个任务(需求第九章)
        UniqueConstraint("idempotency_key", name="uq_generation_tasks_idempotency_key"),
        Index("ix_generation_tasks_product_id", "product_id"),
        Index("ix_generation_tasks_status", "status"),
        Index("ix_generation_tasks_created_at", "created_at"),
        Index("ix_generation_tasks_plan_fingerprint", "plan_fingerprint"),
        Index("ix_generation_tasks_color_variant_id", "color_variant_id"),
    )

    product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    model_template_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("model_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: 参与生成的素材 ID 列表(顺序无关,幂等键会排序)
    input_asset_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    routing_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RoutingMode.MANUAL.value
    )

    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    current_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    output_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    #: Provider 侧的任务标识。FASHN 的 product-to-model 一次只出一张,
    #: 凑 4 张要提交 4 次,这里存的是 4 个 UUID 拼起来的复合 ID(约 147 字符),
    #: 早期的 VARCHAR(128) 会在**请求已经发出去之后**才写库失败 —— 钱花了,ID 丢了。
    external_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)

    #: 这个任务按哪一份方案生成的(§4.7)。**SET NULL 而不是 CASCADE**:
    #: 删掉一份方案不该连着删掉它出过的图 —— 那些图可能已经批准、已经发布
    generation_plan_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("generation_plans.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: 生成时那份方案的指纹快照。进幂等键(§9.3),也是 §8.1
    #: 「更换生成方案 → 对应作用域图片集过期」比较的那一半。
    #:
    #: **与 `generation_plan_id` 两列都要**,这一条容易被读成冗余:
    #: 外键回答"用的哪一份",指纹回答"当时那份长什么样"。只留外键的话,
    #: 方案被原地编辑之后,"这批图是不是按当前参数出的"就永远答不了 ——
    #: 而那正是要判过期的场景;只留指纹的话,追不到是谁改的
    plan_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 绑定的颜色变体(§6.5「创建 GenerationTask(绑定 color_variant_id + plan
    #: + 双指纹)」)。NULL = SPU 级任务。
    #:
    #: 这一列让"新增一个颜色只新增该颜色的任务"成为可以判定的事 ——
    #: 在它之前任务只挂在 `product_id`(SKU 粒度)上,而一个颜色有多个尺码
    color_variant_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("color_variants.id", ondelete="SET NULL"),
        nullable=True,
    )

    #: §5.3 双作用域样品指纹里**该颜色那一份**的快照。
    #:
    #: 共享作用域那一份不在这里:它属于事实层(§4.6 的 `input_fingerprint`),
    #: 那批列还没落。**只落一半是刻意的** —— 两个作用域各有各的比较对象,
    #: 把共享那份也塞进任务行,会造出一个没有对照方的值,而
    #: `facts_stale(stored=None)` 恒为 True 会让全库事实一次性集体过期
    sample_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=TaskStatus.CREATED.value
    )
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: 谁正在执行这个任务的当前阶段,以及租约什么时候过期。
    #:
    #: 只给**续跑阶段**用(SCORING / FORMATTING)。正常流程的排他性来自
    #: "认领前后状态不同"的条件 UPDATE(QUEUED -> PREPROCESSING),而续跑
    #: 进来时前后状态是同一个,那个手法用不上:两个 worker 拿到同一条重复
    #: 投递的消息,会同时开始重新评分同一批图,或者一前一后各出一次成品图。
    #: Outbox 明确是"至少一次投递",重复消息属于正常现象,不是异常。
    #:
    #: 租约而不是行锁:评分是每张图最长 90 秒的大模型调用,出图要跑多次缩放
    #: 编码并逐个写对象存储 —— 带着行锁进去会把取消和回收器全挡在外面,
    #: 而那正是当初拆短事务要解决的问题。租约把"排他"和"持锁"分开了。
    #:
    #: 过期时间存的是 worker 自称的截止时刻,因此它**不能**用来证明对方还活着;
    #: 那件事由 `updated_at` 心跳和 reap_stalled 负责。租约只回答一件事:
    #: 现在轮到谁。
    phase_lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phase_lease_until: Mapped[datetime | None] = mapped_column(nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    attempts: Mapped[list[GenerationAttempt]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="GenerationAttempt.round_number",
    )
    candidates: Mapped[list[GenerationCandidate]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class GenerationAttempt(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """一次 Provider 调用 = 一条记录(需求第九章)。

    换 Provider 重试也算新的一次调用,因此同一 round 可能有多条 attempt。
    """

    __tablename__ = "generation_attempts"
    __table_args__ = (
        Index("ix_generation_attempts_task_id", "task_id"),
        Index("ix_generation_attempts_provider", "provider"),
    )

    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="CASCADE"), nullable=False
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Provider 侧的任务标识。FASHN 的 product-to-model 一次只出一张,
    #: 凑 4 张要提交 4 次,这里存的是 4 个 UUID 拼起来的复合 ID(约 147 字符),
    #: 早期的 VARCHAR(128) 会在**请求已经发出去之后**才写库失败 —— 钱花了,ID 丢了。
    external_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AttemptStatus.SUBMITTED.value
    )
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 本轮为何重生 + 采取了什么修复策略(需求第九章)
    regeneration_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    repair_strategy: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    task: Mapped[GenerationTask] = relationship(back_populates="attempts")
    candidates: Mapped[list[GenerationCandidate]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class GenerationCandidate(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """单张候选图。

    第三方返回的 URL 必须尽快下载到自有存储(需求第十九章),
    因此 storage_path 是落地后的自有路径,source_url 只作溯源用。
    """

    __tablename__ = "generation_candidates"
    __table_args__ = (
        # A45-batch12-3 / REG-03:同一轮的"第几张图"只能有一行。
        #
        # `_persist_candidates()` 有两条续跑进入路径(全部下载失败、reaper 回收
        # DOWNLOADING),都会对同一个 attempt 再走一遍落库。应用层按这三列认领
        # 已有行,这条约束是它背后的硬保证 —— 没有它,漏掉认领那一步的后果是
        # 一批看不出来的重复候选;有它,后果是一次 IntegrityError。
        #
        # 名字与迁移 0033 保持一致:两边不一致时 `create_all()` 建出来的表
        # (测试夹具走的正是那条路)和迁移建出来的表会有两个不同名的约束,
        # 而下一次 autogenerate 会提议 DROP 其中一个。
        UniqueConstraint(
            "attempt_id",
            "round_number",
            "candidate_index",
            name="uq_generation_candidates_attempt_round_index",
        ),
        Index("ix_generation_candidates_task_id", "task_id"),
        Index("ix_generation_candidates_attempt_id", "attempt_id"),
        Index("ix_generation_candidates_status", "status"),
    )

    task_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("generation_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    candidate_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: 指向统一素材(M1)。**可空是迁移期的必然状态**:
    #: 影子写上线之前的历史候选没有对应素材,回填脚本跑完才会补上。
    #: 收口(迁移 C)之后才考虑置为非空 —— 在那之前置非空会让
    #: 任何一条回填漏网的历史数据把整张表锁死。
    media_asset_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="SET NULL"),
        nullable=True,
    )
    #: 旧的图片字段。迁移期与 media_asset_id **并存**,由双读对账比对。
    #: 需求文档 §4.2 把这张表写成新建,实际它 0002 就存在且直接持图,
    #: 所以这里是一次带数据迁移的重构,不是建表(见 M0 迁移计划 §0.2)。
    storage_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=CandidateStatus.PENDING.value
    )
    #: 阶段 4 由评分器写入,阶段 3 恒为空
    overall_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(2), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    task: Mapped[GenerationTask] = relationship(back_populates="candidates")
    attempt: Mapped[GenerationAttempt] = relationship(back_populates="candidates")


class ProviderUsageRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Provider 调用流水。用于仪表盘统计与将来的成本核算。"""

    __tablename__ = "provider_usage_records"
    __table_args__ = (
        Index("ix_provider_usage_records_provider", "provider"),
        Index("ix_provider_usage_records_created_at", "created_at"),
        # 看板主查询是「本月按 provider 汇总」。迁移 0023 建了它,
        # 但**只建在迁移里** —— 于是 `create_all()` 建出来的表没有它
        # (测试夹具走的正是那条路),而下一次 autogenerate 会提议 DROP 它。
        # `test_upgrade_matches_orm_metadata` 只比对列,兜不住索引漂移。
        Index("ix_provider_usage_records_created_provider", "created_at", "provider"),
        # A45-batch12-5 / NEW-01:一笔实际发生的 Provider 消费只能有一行。
        #
        # 名字与迁移 0034 保持一致:两边不一致时 `create_all()` 建出来的表
        # (测试夹具走的正是那条路)和迁移建出来的表会有两个不同名的约束,
        # 而下一次 autogenerate 会提议 DROP 其中一个。
        UniqueConstraint("billing_key", name="uq_provider_usage_records_billing_key"),
        # A45-batch14-18 / 任务 9:计费量的来源只有两档。
        #
        # 名字与迁移 0039 保持一致,理由和上面那条唯一约束一样:两边不一致时
        # `create_all()` 建出来的表(测试夹具走的正是那条路)和迁移建出来的表
        # 会有两个不同名的约束,而下一次 autogenerate 会提议 DROP 其中一个。
        #
        # 靠 CHECK 而不是只靠应用层:这一列的读者是**对账的人**,一个拼错的
        # 取值不会让任何代码报错,只会让一行流水从两个口径的统计里同时消失。
        CheckConstraint(
            f"units_source IN ('{UnitsSource.PROVIDER.value}', "
            f"'{UnitsSource.INFERRED.value}')",
            name="ck_provider_usage_records_units_source",
        ),
    )

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("generation_tasks.id", ondelete="SET NULL"), nullable=True
    )
    attempt_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False, default="submit")
    #: 这一行记的是**哪一笔**实际消费。NULL = 这一类调用没有幂等身份。
    #:
    #: ## A45-batch12-5 / NEW-01:没有它,恢复一次就多记一笔生成费用
    #:
    #: REG-01 的修复给 `_await_and_collect()` 开了第二条进入路径(续跑),
    #: 而这个函数的公共出口无条件写一条 `operation=submit` 的流水。于是:
    #:
    #:     第一次   submit / billable_units=3 / succeeded=False   全部下载失败
    #:     恢复后   submit / billable_units=3 / succeeded=True    同一笔外部生成
    #:
    #: Provider 只跑过一次、只收过一次钱,内部台账却记了 6 个计费单位。
    #: 影响不是"多扣钱"(外部没有重复 submit),是**账对不上**:月度花费、
    #: 调用次数、失败率全部失真,而厂商账单和内部台账再也没法核对。
    #:
    #: 键的形状是 `<attempt_id>:submit` —— 一次生成调用的业务身份就是
    #: "哪一条 attempt 的哪一种操作"。轮询、取结果这些**每次都是真的又调了
    #: 一次**,所以它们不传键:那不是重复记账,那是真的调了 N 次。
    #:
    #: 用独立列而不是 `(attempt_id, operation)` 联合唯一:`attempt_id` 可空
    #: (评分流水就没有),而 PostgreSQL 的联合唯一在含 NULL 时形同虚设。
    billing_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)

    # ------------------------------------------------------------ 费用(A23)
    #
    # 这张表的文档字符串从建表起就写着「将来的成本核算」。下面五列就是那个"将来"。
    #
    # 金额一律**整数微单位**(1 货币单位 = 1_000_000 微),不用 Numeric 也不用 Float:
    # 浮点会在月末汇总时留下对不上的尾数,而 Numeric 在 Python 侧要来回转 Decimal,
    # 累加上万条流水时既慢又容易在某一处被隐式转成 float。算术全在
    # `core/pricing.py`,那是个纯模块。

    #: 计费单位数。**不等于 candidate_count** —— 一次失败但已计费的调用
    #: 可能 units=1 而一张图都没产出,而那正是最需要被记上账的一种
    billable_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 这一行代表**几次真实的网络请求**(A45-batch18 / P1-2,迁移 0048)。
    #:
    #: ## 为什么它不能并进 `billable_units`
    #:
    #: 两者在识别链路上恰好相等(一次往返 = 一个计费单位),在生成链路上
    #: 不等:FASHN 一次 POST 可能出多张图,按额度计价,`x-fashn-credits-used`
    #: 报的是额度不是请求数。合成一列的话,对账的人拿着它去和供应商后台的
    #: 调用条数比,在一半的链路上会得到一个必然对不上的数,
    #: 而**对不上的原因是口径,不是漏账** —— 那是最浪费时间的一种红。
    #:
    #: ## 为什么可空,而且 NULL 不等于 0
    #:
    #: NULL = 这一类调用还没接上次数上报(评分、轮询、取结果今天都是)。
    #: 0 = **确认**一个请求都没发出去(preflight 失败:未配置、目标为空、
    #: Schema 拒绝、图片准备失败)。把"不知道"写成 0 会让一次真实调用
    #: 在对账表上凭空消失,而那个方向的错误没有人会去发现 ——
    #: 与 `ProviderUsage.units is None` 是同一条规矩。
    provider_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 当时那次调用的单价快照。NULL = **没配价**,不是免费 ——
    #: 两者混同的话看板会画出一条平坦的绿线,而钱照花。
    #: 快照而非实时查价:厂商调价不能让上个月的报表变成另一个数字
    unit_price_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    #: 算这笔钱时用的价目表版本。对账时要回答"这个数是按哪一版价算的"
    pricing_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 上面那个 `billable_units` **是谁说的**(A45-batch14-18 / 任务 9)。
    #:
    #: 取值只有两档,定义在 `core/enums.py` 的 `UnitsSource`:
    #:
    #:     provider   厂商自己报的,和它的账单同源
    #:     inferred   我们数候选图数出来的,和账单只是**碰巧**可能相等
    #:
    #: ## 没有这一列的时候,§10.2 第 5 条准入无法执行
    #:
    #: 那一条是「用量记录与 Provider 后台账单条数一致」。对账的人发现某一行
    #: 对不上时,第一个问题是"这个数是厂商说的还是我们猜的"—— 前者对不上
    #: 要去查厂商,后者对不上是我们算错了。缺这一列的话每一行看起来同样权威,
    #: 而在这一批之前**所有行都是猜的**:FASHN 从 `x-fashn-credits-used`
    #: 报回来的真实额度被解析出来、抄在候选图上,然后全仓再没有第二处读它。
    #:
    #: 默认 `inferred` 而不是 `provider`:忘了接线的表现是台账诚实地说
    #: "这是估的",不是一张估算表冒充账单同源。
    units_source: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=UnitsSource.INFERRED.value,
        server_default=UnitsSource.INFERRED.value,
    )
