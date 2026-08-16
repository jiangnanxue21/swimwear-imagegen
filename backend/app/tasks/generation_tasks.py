"""生成流水线编排(阶段 3 + 阶段 4)。

驱动范围:
    QUEUED → PREPROCESSING → SUBMITTING → PROVIDER_RUNNING → DOWNLOADING → SCORING
    → AUTO_APPROVED / REGENERATING(下一轮)/ MANUAL_REVIEW

一次调用只跑**一轮**。要重生时先提交事务,再把自己重新投进队列,
而不是在一个 worker 里循环跑三轮 —— 长事务会让取消失效、失败无法从中途恢复,
而且一轮的失败会连坐已经落库的前几轮。

几个刻意的设计:
- 每个步骤边界检查 cancel_requested,取消随时生效(协作式取消);
- 提交后遇超时不重复提交,先查外部状态(需求第九章);
- 第三方 URL 立刻下载进自有存储(需求第十九章);
- 每次 Provider 调用落一条 GenerationAttempt 和一条用量流水;
- 每次重生都把"为什么重生 + 采取了什么修复策略"写进 attempt(需求第九章)。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.exc import OperationalError

from app.core.clock import utc_now
from app.core.config import settings
from app.core.enums import (
    AttemptStatus,
    CandidateStatus,
    ProductStatus,
    RegenerationReason,
    ReviewReason,
    TaskStatus,
)
from app.core.errors import (
    ConcurrentTransition,
    ErrorCode,
    ManualReviewRequired,
    ValidationError,
)
from app.core.hashing import hash_bytes
from app.core.logging import get_logger
from app.core.redaction import safe_error_message
from app.db.session import SessionLocal
from app.evaluators.decision import RoundOutcome
from app.evaluators.repair import apply_prompt_additions
from app.models.generation import GenerationAttempt, GenerationCandidate, GenerationTask
from app.models.model_template import ModelTemplate
from app.models.product import Product
from app.models.product_asset import ProductAsset
from app.providers import call_accounting
from app.providers.base import (
    GenerationMode,
    GenerationRequest,
    ImageGenerationProvider,
    provider_input_reference,
    settle_billable_units,
)
from app.providers.errors import ProviderError, ResultDownloadError
from app.providers.registry import get_provider, next_configured_provider
from app.services import evaluation_service, review_service
from app.services import generation_service as gs
from app.services.image_probe import probe_image
from app.services.storage import asset_url, build_storage
from app.tasks.celery_app import celery_app
from app.workflows import state_machine as sm
from app.workflows.idempotency import next_seed

logger = get_logger(__name__)

#: 落终态失败后的就地重试间隔(秒)。总共约 3.5 秒 —— 够扛过一次连接池抖动或
#: 主从切换,又不至于把 worker 占住太久。超过这个就不是"抖一下"了,交给上层。
_FAIL_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0)

#: 单张候选图下载上限,防止被超大文件拖垮(需求第十九章)
MAX_CANDIDATE_BYTES = 25 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30


class _Cancelled(Exception):
    """协作式取消的内部信号,不对外暴露。

    带上**实际观察到的状态**:被别人抢先落成终态时,如实回报那个状态,
    而不是一律报 CANCELLED —— 后者会让 MANUALLY_REJECTED 的任务在日志里
    显示成"被取消",排查时对不上账。
    """

    def __init__(self, observed_status: str = TaskStatus.CANCELLED.value) -> None:
        super().__init__(observed_status)
        self.observed_status = observed_status


def _now():
    return utc_now()


def _checkpoint(session, task: GenerationTask, phase: str) -> None:
    """提交当前阶段,**释放任务行上的写锁**。

    这是整条流水线里最不起眼、也最要命的一个动作。每次 ``transition()`` 都是一条
    UPDATE,拿到 generation_tasks 那一行的写锁,直到事务提交才放。而下一步往往是
    一次外部网络调用:轮询最长 300 秒,每张图评分 90 秒,一轮下来锁能持有十几分钟。

    后果不是"慢",是**取消功能整体失效**,而且是个闭环:

        worker 持锁 -> 取消接口的 UPDATE 阻塞 -> 取消事务提交不了
                    -> worker 的 refresh 永远读不到 cancel_requested=True
                    -> worker 继续持锁

    每个阶段边界提交一次,锁的持有时间就从"一整轮"缩到"一条 UPDATE",
    取消能立刻落库,worker 在下一个边界就能看到。

    顺带解决失败现场丢失:提交过的阶段不会被后续的 rollback 抹掉。
    以前一次评分崩溃会把已经下载入库的候选图连同 attempt、用量流水一起回滚 ——
    图还在对象存储里躺着,库里却什么都没有,只能重新花钱生成一遍。
    """
    session.commit()
    logger.debug(
        "phase committed",
        extra={
            "extra_fields": {
                "event": "gen.phase_committed",
                "task_id": str(task.id),
                "phase": phase,
            }
        },
    )


def _check_cancelled(session, task: GenerationTask) -> None:
    """步骤边界的取消检查。**会先结束当前事务。**

    必须先提交再读:在自己的事务里 refresh,读到的要么是自己写的值,要么是一个
    因为被自己的锁挡住而根本没能提交的值。想看见别人的取消,先得把锁放掉。
    """
    _checkpoint(session, task, "cancel-check")
    session.refresh(task, ["cancel_requested", "status"])
    if task.cancel_requested or sm.is_terminal(task.status):
        raise _Cancelled(task.status)


def _claim(session, task: GenerationTask) -> bool:
    """用一条带状态条件的 UPDATE 认领任务。抢到返回 True,没抢到返回 False。

    为什么必须原子:``task_acks_late=True`` 意味着重复投递是**正常现象** ——
    worker 被 kill、心跳超时,消息都会重回队列。先查后改的两个 worker
    会同时看到「可以认领」,各自调一次 Provider:出两倍的图,付两倍的钱。
    条件写进 WHERE 之后,数据库是唯一的裁判。

    这里直接写状态而没走 ``gs.transition``,是因为转移判定必须和 UPDATE 同属一条语句;
    合法性改由 CLAIMABLE_STATUSES 的静态断言保证。
    """
    from sqlalchemy import func, update

    now = _now()
    result = session.execute(
        update(GenerationTask)
        .where(
            GenerationTask.id == task.id,
            GenerationTask.status.in_(sm.CLAIMABLE_STATUSES),
        )
        .values(
            status=sm.CLAIM_TARGET,
            # 首轮记录开工时间,重生轮次保留最初那一次
            started_at=func.coalesce(GenerationTask.started_at, now),
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        session.rollback()
        session.refresh(task)
        return False
    session.commit()
    session.refresh(task)
    return True


#: 续跑阶段的租约时长。**不再是一个手写常量**(A45-batch12-5 / NEW-03)。
#:
#: 上一版是 `PHASE_LEASE_SECONDS = 900`,而一轮 8 张候选图的合法评分耗时是
#: 8 × 3 × 90 = 2160 秒 —— 租约会在 worker 正常评分到第 4 张时到期,
#: 于是一条重复消息就能把这批图再评一遍:重复的评分费用、两个 worker 同时
#: 写 EvaluationAttempt、后来者和原 worker 都可能提交结果。
#:
#: 修法不是把 900 改大。租约现在**每个提交点续一次**(见 `_heartbeat`),
#: 所以它只需要覆盖"两个提交点之间"那一格,不需要覆盖整段。长度由
#: `phase_budget` 按当前配置推出来,配置一改它跟着改。
def _phase_lease_seconds() -> int:
    from app.workflows.phase_budget import phase_lease_seconds

    return phase_lease_seconds()


class _LeaseLost(Exception):
    """阶段租约已经被别人接管。**拿到它就立刻停手,一个字都不许再写。**

    ## 为什么必须有这个信号

    租约到期只意味着"可以接管",不代表原持有者死了 —— 它可能只是被一次
    很慢的外部调用拖住了。于是接管之后会出现两个 worker 同时认为自己在跑
    这个阶段,而上一版对这种情况**没有任何防御**:

        `_release_phase()` 检查 `phase_lease_owner == owner`   只在**还**的时候检查
        业务写入(评分结果、状态转移、出图)                     一律不检查

    也就是说,被接管之后旧 worker 仍然可以把它那一份结果照常提交上去,
    盖掉新 worker 正在写的东西。只在释放时检查 owner 不是 fencing,
    那只是"别把别人的锁还掉"。

    真正的 fencing 是:**每一次要写业务数据之前,先确认自己还持有租约**。
    `_heartbeat()` 在每个提交点做这件事,拿不到就抛这个异常,由
    `run_generation_task` 静默收尾 —— 不落 FAILED(那会盖掉新 worker 的结论)、
    不释放租约(那把租约已经不是我们的了)。
    """

    def __init__(self, task_id: str, owner: str) -> None:
        super().__init__(f"阶段租约已被接管:task={task_id} owner={owner}")
        self.task_id = task_id
        self.owner = owner


def _worker_identity() -> str:
    """这个 worker 的自述标识。只用于排查"是谁在跑",不参与任何判断。"""
    import os
    import socket
    import uuid as _uuid

    return f"{socket.gethostname()[:24]}:{os.getpid()}:{_uuid.uuid4().hex[:8]}"


def _lease_owner(lease: dict[str, str] | None) -> str | None:
    """取当前持有的租约标识。没持有(首轮正常流程的一部分路径)返回 None。"""
    return (lease or {}).get("owner")


def _heartbeat(session, task: GenerationTask, lease: dict[str, str] | None) -> None:
    """推进心跳,顺带续租约,并在租约已经易主时抛 `_LeaseLost`。

    ## 三件事为什么捆在一起

    它们回答的是同一个问题的两面:

        updated_at        对**回收器**说"我还活着,别收我"
        phase_lease_until 对**其它 worker** 说"这个阶段还有人,别接管"
        rowcount == 0     对**自己**说"你已经不是持有者了,停手"

    分开做的话必然出现三者不一致的瞬间,而那正是重复评分和误杀发生的地方。

    ## 为什么它必须 commit

    回收器读的是库里的 `updated_at`。不提交的话这一行只存在于我们自己的
    事务里,外面看到的仍然是几分钟前那个值 —— 心跳等于没打。

    提交顺带把行锁放掉,这和 `_checkpoint()` 是同一个理由:评分是每张图
    最长 90 秒的大模型调用,带着锁进去会把取消接口和回收器全挡在外面。

    ## 没有租约时也要打心跳

    首轮流程走的是 `_claim()` 那条路(状态前后不同,天然排他),不一定持有
    阶段租约。但**回收器不管这个** —— 它只看 `updated_at`。所以没有租约时
    仍然要推进心跳,只是跳过续期和 fencing 检查。
    """
    from sqlalchemy import update

    owner = _lease_owner(lease)
    now = _now()
    values: dict[str, Any] = {"updated_at": now}
    stmt = update(GenerationTask).where(GenerationTask.id == task.id)
    if owner:
        values["phase_lease_until"] = now + timedelta(seconds=_phase_lease_seconds())
        stmt = stmt.where(GenerationTask.phase_lease_owner == owner)

    result = session.execute(stmt.values(**values))
    session.commit()
    session.expire(task)

    if owner and result.rowcount != 1:
        # 只有一种解释:租约已经被别人接管(或被人工清掉)。
        # **不回滚、不落任何状态** —— 现在做主的是新持有者。
        logger.warning(
            "phase lease was taken over while we were still working; stopping",
            extra={
                "extra_fields": {
                    "event": "gen.phase_lease_taken_over",
                    "task_id": str(task.id),
                    "owner": owner,
                }
            },
        )
        raise _LeaseLost(str(task.id), owner)


def _claim_phase(session, task: GenerationTask, expected_status: str) -> str | None:
    """抢续跑阶段的执行租约。抢到返回持有者标识,没抢到返回 None。

    ## 为什么续跑必须单独有一把锁

    正常流程的排他性来自 `_claim()`:它把"能认领的状态"改成"认领后的状态",
    前后不同,所以同一条 UPDATE 只可能有一个赢家。续跑进来时前后是**同一个**
    状态(SCORING -> 还是 SCORING),那个手法直接失效。

    而重复消息是**正常现象**,不是异常:Outbox 明确采用至少一次投递,
    `task_acks_late=True` 又会让被 kill 的 worker 的消息重回队列。于是:

        两个 worker 同时进 SCORING 续跑 -> 同一批图被评两次 -> 评分费用翻倍
        两个 worker 先后进 FORMATTING   -> 同一批成品图发布两次,代数凭空多一代

    出图那条尤其容易被误判成"已经安全了":`output_service` 里的商品行锁只能
    让两次格式化**排队**,第二个等到第一个提交完照样会完整再做一遍。
    锁保证的是不并发,不是不重复。

    ## 为什么是租约不是行锁

    评分是每张图最长 90 秒的大模型调用,出图要跑多次缩放编码并逐个写对象存储。
    带着行锁走完这些,取消接口和回收器全被挡在外面 —— 那正是当初拆短事务
    要解决的问题。租约只占一次 UPDATE 的锁,之后立刻提交。

    到期时间让接管不需要人工干预:持有者被 kill 时没有人会来释放它。

    ## 到期不等于死亡,所以接管之后还要有 fencing

    到期只是"可以接管"。原持有者可能只是被一次很慢的调用拖住了,接管之后
    两个 worker 会同时认为自己在跑这个阶段 —— 这一版靠 `_heartbeat()` 在
    每个提交点复查 owner 把旧 worker 挡在写入之外(见 `_LeaseLost`)。
    """
    from sqlalchemy import or_, update

    now = _now()
    owner = _worker_identity()
    result = session.execute(
        update(GenerationTask)
        .where(
            GenerationTask.id == task.id,
            GenerationTask.status == expected_status,
            # 没人持有,或者上一个持有者的租约已经过期
            or_(
                GenerationTask.phase_lease_until.is_(None),
                GenerationTask.phase_lease_until < now,
            ),
        )
        .values(
            phase_lease_owner=owner,
            phase_lease_until=now + timedelta(seconds=_phase_lease_seconds()),
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        session.rollback()
        session.expire(task)
        return None
    session.commit()
    session.expire(task)
    return owner


def _release_phase(session, task_id: UUID, owner: str) -> None:
    """还回租约。**只还自己那把。**

    ``phase_lease_owner == owner`` 这个条件不能省:租约过期后可能已经被
    别人接管,这时候把它清掉等于把别人正在用的租约释放掉,重复执行又回来了。

    失败只记日志:租约有到期时间兜底,还不回去最坏也就是下一个 worker
    多等一会儿,不该让一个已经做完的阶段因为清理失败而报错。
    """
    from sqlalchemy import update

    try:
        session.execute(
            update(GenerationTask)
            .where(
                GenerationTask.id == task_id,
                GenerationTask.phase_lease_owner == owner,
            )
            .values(phase_lease_owner=None, phase_lease_until=None)
        )
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.warning(
            "cannot release the phase lease; it will expire on its own",
            extra={
                "extra_fields": {
                    "event": "gen.lease_release_failed",
                    "task_id": str(task_id),
                    "owner": owner,
                }
            },
        )


def _release_lease_before_dispatch(
    session, task_id: UUID, lease: dict[str, str] | None
) -> None:
    """派发下一条消息**之前**把租约还回去(A45-batch12-5 / NEW-02)。

    ## 要修的那件事

    上一版的顺序是:

        提交任务状态和 Outbox -> 派发新消息 -> finally 里释放租约

    实际注入出来的顺序记录是 `['commit', 'dispatch', 'release', 'close']`。
    新消息在租约还没还回去的时候就已经进队列了,于是:

        新 worker 看到 SCORING
        -> `_claim_phase()` 因为旧租约还在而失败
        -> 返回 claimed=False,消息被正常 ack
        -> Outbox 早已标成 DISPATCHED,不会再投第二次

    任务就此停在 SCORING,直到大约 30 分钟后被回收器判成卡死。**没有任何
    一层能察觉**:派发本身是成功的,Outbox 只知道"消息发出去了",它不知道
    消费者拿到之后因为租约还在而主动退出了。

    ## 为什么先释放是安全的

    走到这里时这一轮该做的事已经全部提交完了,当前 worker 除了返回没有别的
    动作。租约的作用是"防止两个 worker 同时干活",而我们已经不干活了。

    反过来,如果释放之后、派发之前崩掉:任务停在 SCORING 且没有租约,
    下一次投递(relay 会重投 PENDING 的 Outbox 行)能正常接管。这比现在
    "消息发了但没人能执行"的死局好得多 —— 前者会自愈,后者只能等回收器。

    从 `lease` 里把 owner 摘掉,是为了让 `finally` 里那次释放变成 no-op:
    重复释放本身无害(条件里带着 owner),但留着它会让"谁还持有租约"这件事
    在代码里有两个答案。
    """
    owner = (lease or {}).pop("owner", None)
    if owner:
        _release_phase(session, task_id, owner)


@celery_app.task(
    name="generation.run",
    bind=True,
    # 业务异常一律不重试:重复投递由 `_claim()` 的原子认领挡着,
    # 而"再跑一遍"对一个已经落成 FAILED 的任务没有意义,只会再花一次钱。
    #
    # **唯一的例外是基础设施故障。** 数据库短暂不可用时,失败状态写不进库,
    # 而 acks_late 会把消息 ack 掉 —— 任务就此悬在进行中状态。
    # 只对 OperationalError 开重试,范围窄到不会顺带把业务失败也重试掉。
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def run_generation_task(self, task_id: str) -> dict[str, Any]:  # noqa: ARG001
    """主编排入口。异常一律转成 FAILED 落库,不让 Celery 静默吞掉。"""
    session = SessionLocal()
    #: 续跑阶段抢到的租约。放在这里而不是 `_run` 里面,是为了让释放动作
    #: 落在 finally 上 —— 中途抛异常时也要还回去,否则这个任务要等到
    #: 租约自然过期(15 分钟)才有人能接手。
    lease: dict[str, str] = {}
    try:
        task = gs.get_task(session, UUID(task_id))
        result = _run(session, task, lease)
        session.commit()
        # ---- A45-batch12-5 / NEW-02:释放必须排在派发**前面** ----
        #
        # 顺序原来是 commit -> dispatch -> finally: release。新消息在租约还
        # 挂着的时候就进了队列,新 worker 抢不到租约、静默退出,而 Outbox 已经
        # 标成 DISPATCHED 不会再投 —— 任务停在原状态直到被回收器判成卡死。
        # 详见 `_release_lease_before_dispatch` 的文档。
        _release_lease_before_dispatch(session, UUID(task_id), lease)
        # 派发下一轮必须在 commit **之后**:先派发的话,worker 可能读到旧状态,
        # 于是这一轮的 REGENERATING 还没落库,下一轮就已经开始改它了。
        if result.pop("redispatch", False):
            _dispatch_next_round(session, task_id)
        return result
    except _LeaseLost as lost:
        # 租约在我们干活的时候被别人接管了。**什么都不写。**
        #
        # 特别不能落 FAILED:现在做主的是新持有者,它可能正要提交一个成功结论,
        # 而这个 FAILED 会把它盖掉 —— 那正是 fencing 要防的事情本身。
        # 租约也不用还:它已经不是我们的了(`_release_phase` 带 owner 条件,
        # finally 里那次会自动成为 no-op)。
        session.rollback()
        logger.info(
            "stopped: the phase lease was taken over by another worker",
            extra={
                "extra_fields": {
                    "event": "gen.task_stopped",
                    "task_id": task_id,
                    "owner": lost.owner,
                }
            },
        )
        return {"task_id": task_id, "status": "superseded", "claimed": False,
                "lease_lost": True}
    except ConcurrentTransition as clash:
        # 状态在写库那一刻被别人抢先了 —— 取消、人工审核决策、或者另一个
        # worker。这不是错误,是"世界变了",该做的是停下来。
        #
        # 特别**不能**在这里把任务落成 FAILED:抢先的那一方刚刚写下的
        # 结论(CANCELLED / MANUALLY_APPROVED …)会被这个 FAILED 盖掉,
        # 而那正是这条异常存在的意义。
        session.rollback()
        logger.info(
            "task stopped: another writer won the transition",
            extra={
                "extra_fields": {
                    "event": "gen.task_stopped",
                    "task_id": task_id,
                    **(clash.detail or {}),
                }
            },
        )
        return {"task_id": task_id, "status": "superseded", "claimed": False}
    except _Cancelled as stop:
        # 只回滚当前这一个阶段。之前每个阶段都已各自提交,现场完整保留。
        session.rollback()
        logger.info(
            "task stopped by cancellation",
            extra={
                "extra_fields": {
                    "event": "gen.task_stopped",
                    "task_id": task_id,
                    "status": stop.observed_status,
                }
            },
        )
        return {"task_id": task_id, "status": stop.observed_status}
    except ProviderError as exc:
        # 刻意不 rollback。attempt 与用量流水在抛错前已写入,它们是排查问题的
        # 全部依据(需求第七章:Provider 调用失败时保存错误信息)。
        # 一律回滚会把失败记录一起抹掉,线上只剩一个没有上下文的 FAILED。
        return _fail(session, task_id, str(exc.code), exc.message)
    except OperationalError:
        # 数据库不可用。不落 FAILED —— 落不进去,而且这不是任务的错。
        # 抛出去交给 autoretry(见任务装饰器上的说明)。
        session.rollback()
        logger.error(
            "database unavailable during the pipeline; will retry",
            extra={"extra_fields": {"event": "gen.db_unavailable", "task_id": task_id}},
        )
        raise
    except Exception as exc:  # noqa: BLE001
        # 阶段提交之后,这次 rollback 只丢掉**当前阶段**的半成品。
        # 已经落库的 attempt、候选图、用量流水都还在 —— 它们是排查的全部依据,
        # 也是"这轮的钱已经花了"的凭证,不能因为下一步崩了就一起消失。
        session.rollback()
        logger.exception(
            "generation pipeline crashed", extra={
                "extra_fields": {
                    "event": "gen.pipeline_crashed",
                    "task_id": task_id,
                }
            }
        )
        return _fail(session, task_id, "INTERNAL_ERROR", f"流水线异常:{type(exc).__name__}")
    finally:
        owner = lease.get("owner")
        if owner:
            _release_phase(session, UUID(task_id), owner)
        session.close()


def _fail(session, task_id: str, code: str, message: str) -> dict[str, Any]:
    """把任务置为 FAILED 并提交。

    调用前不要 rollback 可用的事务:失败现场(attempt / 用量流水)必须一起落库。
    只有会话本身已经不可用时(未知异常)才允许先回滚再进来。
    """
    # 落终态是**唯一**没做成的那件事,所以在这里就地重试它,而不是把整个任务
    # 交给 Celery 重投。
    #
    # 任务级 autoretry 在这条路径上几乎没用:中断发生时任务多半停在
    # SUBMITTING / PROVIDER_RUNNING,而 `CLAIMABLE_STATUSES` 只有
    # QUEUED / REGENERATING —— 重投进来 `_claim()` 直接返回 False,
    # 打一行"已被别人认领"就退出,什么都没恢复。
    #
    # 而这里要写的只是一条 UPDATE。库抖一下就地等两秒再写,成功率远高于
    # 绕一整圈重投;真的连不上再抛出去,让 autoretry 和 `reap_stalled` 兜底。
    for delay in _FAIL_RETRY_DELAYS:
        try:
            task = gs.get_task(session, UUID(task_id))
            if not sm.is_terminal(task.status):
                gs.transition(
                    session, task, TaskStatus.FAILED, error_code=code, error_message=message
                )
            session.commit()
            return {"task_id": task_id, "status": TaskStatus.FAILED.value, "error_code": code}
        except OperationalError:
            session.rollback()
            logger.warning(
                "database hiccup while persisting failure state; retrying",
                extra={
                    "extra_fields": {
                        "event": "gen.db_unavailable",
                        "task_id": task_id,
                        "retry_in": delay,
                    }
                },
            )
            time.sleep(delay)
        except Exception:  # noqa: BLE001 - 非基础设施故障,重试没有意义
            session.rollback()
            logger.exception("failed to persist failure state", extra={
                "extra_fields": {
                    "event": "gen.failure_state_unpersisted",
                }
            })
            return {"task_id": task_id, "status": TaskStatus.FAILED.value, "error_code": code}

    try:
        task = gs.get_task(session, UUID(task_id))
        if not sm.is_terminal(task.status):
            gs.transition(session, task, TaskStatus.FAILED, error_code=code, error_message=message)
        session.commit()
    except OperationalError:
        # 数据库这一刻不可用。**往上抛,让 Celery 的 autoretry 接管。**
        #
        # 以前这里和别的异常一样只记日志、然后照常返回 FAILED,而
        # `task_acks_late=True` 的语义是"执行完成后 ack"—— 正常返回算完成,
        # 消息就此消失。于是任务停在 SUBMITTING / PROVIDER_RUNNING,
        # 要等 30 分钟后 `reap_stalled` 才有人管,而那 30 分钟里它在界面上
        # 看起来是活的。
        #
        # 注意**不能**靠"抛异常让 Celery 不 ack"来修:acks_late 下任务抛异常
        # 同样会被 ack(只是结果标成 FAILURE),消息不会重投。真正起作用的是
        # 任务装饰器上的 `autoretry_for=(OperationalError,)`。
        session.rollback()
        logger.error(
            "database unavailable while persisting failure state; will retry",
            extra={
                "extra_fields": {
                    "event": "gen.db_unavailable",
                    "task_id": task_id,
                    "error_code": code,
                }
            },
        )
        raise
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("failed to persist failure state", extra={
            "extra_fields": {
                "event": "gen.failure_state_unpersisted",
            }
        })
    return {"task_id": task_id, "status": TaskStatus.FAILED.value, "error_code": code}


def _run(
    session, task: GenerationTask, lease: dict[str, str] | None = None
) -> dict[str, Any]:
    # 续跑:上次只是多尺寸转换那步崩了,候选图已经选好。
    # 直接重做出图,不重新生成 —— 重新生成要再花一次钱,而且换了 seed
    # 之后拿到的根本不是当初通过的那张图。retry_task 负责把状态置成 FORMATTING。
    if task.status == TaskStatus.FORMATTING.value:
        leased = _claim_phase(session, task, TaskStatus.FORMATTING.value)
        if leased is None:
            logger.info(
                "formatting already leased by another worker, skipping",
                extra={
                    "extra_fields": {
                        "event": "gen.formatting_already_leased",
                        "task_id": str(task.id),
                    }
                },
            )
            return {"task_id": str(task.id), "status": task.status, "claimed": False}
        if lease is not None:
            lease["owner"] = leased
        selected = next(
            (
                c
                for c in gs.list_candidates(session, task.id)
                if c.status == CandidateStatus.SELECTED.value
            ),
            None,
        )
        logger.info(
            "resuming output formatting",
            extra={"extra_fields": {"event": "gen.resuming_phase", "task_id": str(task.id)}},
        )
        if selected is None:
            # 续跑却找不到当初选中的候选:数据被改过或候选被清理了。
            # 以前这里会往下走并静默返回一个错误字典,任务永远停在 FORMATTING ——
            # 既不是终态,现有的重试接口也捞不回来。必须给它一个明确的结局。
            logger.error(
                "cannot resume formatting: selected candidate is gone",
                extra={
                    "extra_fields": {
                        "event": "gen.formatting_source_missing",
                        "task_id": str(task.id),
                    }
                },
            )
            gs.transition(
                session, task, TaskStatus.FAILED,
                error_code="SELECTED_CANDIDATE_MISSING",
                error_message="找不到当初选中的候选图,无法继续出图;请重新生成",
            )
            return {
                "task_id": str(task.id),
                "status": task.status,
                "error": "SELECTED_CANDIDATE_MISSING",
            }
        return _format_outputs(
            session, task, str(selected.id), already_formatting=True
        )

    # 续跑:外部任务已经受理、ID 也落库了,崩的是"把结果取回来"那一步
    # (A45-batch12-2 / EX-03)。**跳过 submit** —— 那一步的钱已经付过了,
    # 再调一次就是同一轮生成买第二次。`retry_task` 负责把状态置成
    # PROVIDER_RUNNING 并保留 external_task_id。
    if task.status == TaskStatus.PROVIDER_RUNNING.value:
        external_id = (task.external_task_id or "").strip()
        if not external_id:
            # 停在 PROVIDER_RUNNING 却没有外部 ID:这一行不该存在。
            # **不往下走** —— 往下走会重新 submit,而这条任务恰恰是
            # "可能已经受理过"的那一类(EX-01 的形状)。交给对账
            gs.transition(
                session, task, TaskStatus.FAILED,
                error_code=gs.SUBMIT_UNKNOWN_CODE,
                error_message=(
                    "任务停在等待外部结果,却没有外部任务 ID —— 无法确认 Provider "
                    "是否已经受理。请先到 Provider 后台核对再决定是否强制重试"
                ),
            )
            return {
                "task_id": str(task.id),
                "status": task.status,
                "error": gs.SUBMIT_UNKNOWN_CODE,
            }
        leased = _claim_phase(session, task, TaskStatus.PROVIDER_RUNNING.value)
        if leased is None:
            logger.info(
                "provider results already leased by another worker, skipping",
                extra={
                    "extra_fields": {
                        "event": "gen.provider_results_already_leased",
                        "task_id": str(task.id),
                    }
                },
            )
            return {"task_id": str(task.id), "status": task.status, "claimed": False}
        if lease is not None:
            lease["owner"] = leased
        attempts = gs.list_attempts(session, task.id)
        attempt = next(
            (a for a in reversed(attempts) if a.external_task_id == external_id),
            attempts[-1] if attempts else None,
        )
        if attempt is None:
            # 有外部 ID 却一条 attempt 都没有:同样是不该存在的形状。
            # 与上面同一条理由,不重新提交
            gs.transition(
                session, task, TaskStatus.FAILED,
                error_code=gs.SUBMIT_UNKNOWN_CODE,
                error_message=(
                    f"任务带着外部 ID {external_id} 却找不到对应的提交记录,"
                    "无法安全续跑。请到 Provider 后台核对"
                ),
            )
            return {
                "task_id": str(task.id),
                "status": task.status,
                "error": gs.SUBMIT_UNKNOWN_CODE,
            }
        logger.info(
            "resuming provider result collection",
            extra={
                "extra_fields": {
                    "event": "gen.resuming_phase",
                    "task_id": str(task.id),
                    "external_id": external_id,
                }
            },
        )
        return _await_and_collect(
            session,
            task,
            attempt,
            get_provider(task.provider),
            external_id,
            started=time.monotonic(),
            resuming=True,
            lease=lease,
        )

    # 续跑:上一次是评分那步没做成(限流、超时……),图已经在库里了。
    # 重新评这批图,**不重新生成** —— 生成要再花一次钱,而且换了 seed 之后
    # 拿到的是另一批图,连"至少还留着这批"都保不住。和上面 FORMATTING 的
    # 续跑是同一个套路:从崩的地方接着走,不回退到花钱的那一步。
    if task.status == TaskStatus.SCORING.value:
        leased = _claim_phase(session, task, TaskStatus.SCORING.value)
        if leased is None:
            logger.info(
                "scoring already leased by another worker, skipping",
                extra={
                    "extra_fields": {
                        "event": "gen.scoring_already_leased",
                        "task_id": str(task.id),
                    }
                },
            )
            return {"task_id": str(task.id), "status": task.status, "claimed": False}
        if lease is not None:
            lease["owner"] = leased
        scored = [
            c
            for c in gs.list_candidates(session, task.id)
            if c.round_number == task.current_round
            and c.status != CandidateStatus.DOWNLOAD_FAILED.value
        ]
        logger.info(
            "resuming scoring",
            extra={
                "extra_fields": {
                    "event": "gen.resuming_phase",
                    "task_id": str(task.id),
                    "candidates": len(scored),
                }
            },
        )
        if not scored:
            # 停在 SCORING 却一张候选图都没有:数据被改过。让它有个明确结局,
            # 而不是继续往下走去重新生成一批(那会在用户不知情时花掉一笔钱)。
            gs.transition(
                session, task, TaskStatus.FAILED,
                error_code="CANDIDATES_MISSING",
                error_message="停在评分阶段却找不到本轮候选图,无法继续;请重新生成",
            )
            return {
                "task_id": str(task.id),
                "status": task.status,
                "error": "CANDIDATES_MISSING",
            }
        storage = build_storage(
            settings.STORAGE_BACKEND,
            settings.storage_dir,
            settings.PUBLIC_BASE_URL,
            settings.API_PREFIX,
        )
        latest = gs.list_attempts(session, task.id)
        return _score_and_decide(
            session, task, latest[-1] if latest else None, scored,
            storage=storage, lease=lease,
        )

    if task.status == TaskStatus.CREATED.value:
        gs.transition(session, task, TaskStatus.QUEUED)

    # 原子认领:task_acks_late=True 意味着重复投递是**正常现象**(worker 被 kill、
    # 心跳超时都会让消息重回队列)。没有认领就直接往下走的话,两个 worker 会读到
    # 同一个 QUEUED 状态、各自调一次 Provider —— 出两倍的图,花两倍的钱。
    # 用一条带状态条件的 UPDATE 来定胜负,输的那个直接退出。
    if not _claim(session, task):
        logger.info(
            "task already claimed by another worker, skipping",
            extra={
                "extra_fields": {
                    "event": "gen.task_already_claimed",
                    "task_id": str(task.id),
                    "status": task.status,
                }
            },
        )
        return {"task_id": str(task.id), "status": task.status, "claimed": False}

    # 认领**本身**就是 -> PREPROCESSING 这一步转移,这里不再转第二次
    _check_cancelled(session, task)
    provider = get_provider(task.provider)
    request = _build_request(session, task, provider)

    _check_cancelled(session, task)
    attempt = _new_attempt(session, task, request.seed)

    gs.transition(session, task, TaskStatus.SUBMITTING)
    # 提交前先落库。attempt 是"我们向 Provider 发过一次请求"的唯一证据,
    # 如果只 flush 不 commit,worker 在 submit 期间被 kill 时它会随事务一起消失 ——
    # 对方可能已经受理并开始计费,我们这边却查无此事。
    # 同时也把行锁放掉,submit 最长要 60 秒。
    _checkpoint(session, task, "submitting")

    started = time.monotonic()
    try:
        external_id = asyncio.run(provider.submit(request))
    except ProviderError as exc:
        # 提交阶段的超时**不等于**没提交成功。
        # POST 超时只说明我们没收到响应,请求很可能已经到了对方那里并开始计费。
        # 以前一律记成 FAILED,用户在界面上点重试就会再提交一次 —— 付两次钱,
        # 而且第一次那张图永远没人认领。这里把它单独标成「结果未知」,
        # 并挡住自动重试,由人去 Provider 后台对账之后再决定。
        unknown = exc.code.value == "NETWORK_TIMEOUT"
        # 分批提交中途失败:前几次已经被 Provider 受理、很可能已经计费。
        # 那几个外部 ID 必须落库,否则它们在对方那里跑着、在我们这里查无此事,
        # 而人工重试会再买一次(详见 providers/fashn.py 的 `_with_partial_ids`)。
        partial = _partial_ids(exc)
        if partial:
            composite = ",".join(partial)
            attempt.external_task_id = composite
            task.external_task_id = composite
            # 有凭证 = 钱可能已经花出去了,一律按「结果未知」处理,
            # 不能让它走进可以一键自动重试的分支
            unknown = True
        _finish_attempt(
            session, attempt,
            AttemptStatus.TIMEOUT if unknown else AttemptStatus.FAILED,
            error=exc,
        )
        # **失败也要按发出去的请求数记账**(A45-batch18 / P2-2)。
        #
        # 原来这里不传 `billable_units`,于是走 `record_usage` 的默认值
        # `max(candidate_count, 1)` —— 失败分支 candidate_count 是 0,恒为 1。
        # 四次提交前三次成功第四次失败时少记 3 笔已经花掉的钱;
        # 素材下载在第一个 POST 之前失败时,又凭空记了一笔从没花过的钱。
        # 判定在 `providers/call_accounting.py`(纯模块),这里只取数。
        failed_units, units_source = call_accounting.failed_submit_units(
            getattr(exc, "detail", None)
        )
        gs.record_usage(
            session, provider=task.provider, task_id=task.id, attempt_id=attempt.id,
            operation="submit", succeeded=False, error_code=str(exc.code),
            duration_ms=int((time.monotonic() - started) * 1000),
            billable_units=failed_units,
            units_source=units_source,
            provider_attempts=call_accounting.attempted_calls(
                getattr(exc, "detail", None)
            ),
            # 这条 attempt 的生成费用身份。走到这里说明这一笔已经记过账了,
            # 后面任何一条路径(续跑成功、收尾)都只会更新它,不会再记一笔
            billing_key=gs.submit_billing_key(attempt.id),
        )
        if unknown:
            logger.error(
                "submit did not complete cleanly; provider may have accepted part of it",
                extra={
                    "extra_fields": {"event": "gen.submit_incomplete",
                        "task_id": str(task.id),
                        "provider": task.provider,
                        "partial_external_ids": len(partial),
                    }
                },
            )
            session.commit()
            # 两种「结果未知」要说不同的话:一种是我们没收到响应,
            # 另一种是我们**明确知道**前几张已经受理了。后者运营能直接照着
            # ID 去后台核对,把它压成前者那句话等于让他从头找起。
            detail = (
                f"分批提交在第 {len(partial) + 1} 次失败,前 {len(partial)} 次已被 "
                f"Provider 受理(外部 ID 已记录在任务上)。"
                "重试会重新提交全部候选,请先到 Provider 后台核对已受理的那几次,避免重复计费"
                if partial
                else "提交时网络超时,无法确认 Provider 是否已经受理。"
                "重试前请先到 Provider 后台核对,避免重复计费"
            )
            return _fail(session, str(task.id), gs.SUBMIT_UNKNOWN_CODE, detail)
        raise
    except Exception as exc:
        # ProviderError 之外的一切。收尾之后原样抛出去,由外层落任务状态 ——
        # 这里只负责让这条 attempt 不要停在 SUBMITTED
        _abandon_attempt(session, task, attempt, exc, operation="submit", started=started)
        raise

    attempt.external_task_id = external_id
    task.external_task_id = external_id
    # 立刻提交。后面任何一步崩掉都会 rollback,而 external_task_id 是**已经花过钱的凭证** ——
    # 丢了它就再也查不到那次生成,只能重新提交一次、再付一次费。
    # 这一小段提交换来的代价是事务边界变多,值得。
    session.commit()

    return _await_and_collect(
        session, task, attempt, provider, external_id, started=started, lease=lease
    )


#: 这些错误说的是「**我们**没问出来」,不是「外部任务失败了」(EX-03)。
#:
#: 碰到它们时外部那笔生成仍然有效:ID 还在、结果多半还在对方那里等着取。
#: 重试必须保留 external_task_id 从 `get_status` 接着走,而不是重新 submit。
#:
#: 反过来,**刻意不含**这几个:
#:
#:     CONTENT_SAFETY     对方明确拒绝了,再问一百次答案相同
#:     GENERATION_FAILED  外部任务自己失败了,只能重新提交
#:     INPUT_INVALID      报文有问题,重问不会变对
#:     AUTH_FAILED        凭据不对,先去改配置;当作"稍后再试"会把它藏起来
#:
#: 判据是**错误码**不是阶段:同样发生在 status 阶段的这两类,恢复路径相反。
_RESULT_RETRIEVABLE_CODES: frozenset[str] = frozenset(
    {
        ErrorCode.NETWORK_TIMEOUT.value,
        ErrorCode.RATE_LIMITED.value,
        ErrorCode.PROVIDER_SERVICE_ERROR.value,
        ErrorCode.RESULT_DOWNLOAD_FAILED.value,
    }
)


def _result_still_retrievable(exc: ProviderError) -> bool:
    """这次失败之后,外部那笔生成还值不值得再去取一次。"""
    return str(exc.code.value) in _RESULT_RETRIEVABLE_CODES


def _await_and_collect(
    session,
    task,
    attempt,
    provider,
    external_id: str,
    *,
    started: float,
    resuming: bool = False,
    lease: dict[str, str] | None = None,
) -> dict[str, Any]:
    """提交之后的下半程:查状态 -> 取结果 -> 下载入库 -> 评分。

    ## 为什么它是一个独立函数(A45-batch12-2 / EX-03)

    这段代码原来长在 `_run` 的尾巴上,只有\"刚 submit 完\"一条进入路径。
    于是 `get_status` / `fetch_results` 失败之后,**没有任何入口能再走一遍** ——
    任务落 FAILED,重试走 `retry_task` 的通用分支,那条分支会清掉
    `external_task_id` 回 QUEUED,下一个 worker 重新 `submit()`。

    第一次那笔生成还在对方那里跑着、钱已经花了,而我们主动丢掉了它的身份:
    查不到、下不了、也取消不了。这就是 EX-03。

    抽出来之后它有了第二条进入路径:`_run` 顶部认出「PROVIDER_RUNNING +
    external_task_id 已知」时直接调它,**跳过 submit**。和 FORMATTING /
    SCORING 两条续跑是同一个套路 —— 从崩的地方接着走,不回退到花钱的那一步。

    `resuming` 只影响两件事:不再重复转 `PROVIDER_RUNNING`(续跑进来时
    任务已经在这个状态上,再转一次是非法跳转),以及日志里标一句,
    好让\"这一轮 Provider 调了几次\"在事后可数。
    """
    if not resuming:
        gs.transition(session, task, TaskStatus.PROVIDER_RUNNING)
    _check_cancelled(session, task)

    # 查状态与取结果放在同一个失败处理块里。
    # 以前只有 fetch_results 被包住,get_status 抛错会直接冒到最外层 ——
    # 任务确实会变成 FAILED,但这条 attempt 永远停在 SUBMITTED,
    # 也不会留下一条失败用量记录,事后翻库看到的是一个「还在跑」的调用。
    operation = "status"
    try:
        # 超时不重提:先查外部状态,由它决定下一步(需求第九章)
        status = asyncio.run(provider.get_status(external_id))
        logger.info(
            "provider status",
            extra={
                "extra_fields": {"event": "gen.provider_status",
                    "task_id": str(task.id), "provider": task.provider,
                    "external_id": external_id, "status": str(status),
                }
            },
        )
        operation = "fetch"
        candidates = asyncio.run(provider.fetch_results(external_id))
    except ProviderError as exc:
        _finish_attempt(session, attempt, AttemptStatus.TIMEOUT
                        if exc.code.value == "NETWORK_TIMEOUT" else AttemptStatus.FAILED, error=exc)
        gs.record_usage(
            session, provider=task.provider, task_id=task.id, attempt_id=attempt.id,
            operation=operation, succeeded=False, error_code=str(exc.code),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        # ---- A45-batch12-2 / EX-03 ----
        #
        # 这里原来一律 `raise`,由最外层落成 `FAILED + <Provider 原始码>`。
        # 那个码(NETWORK_TIMEOUT / 429 / 500)在 `retry_task()` 眼里是普通失败,
        # 于是重试走通用分支:**清掉 external_task_id、回 QUEUED、重新 submit** ——
        # 而外部那笔生成还在跑、钱已经花了,我们只是主动丢掉了它的身份。
        #
        # 区别在于**这次失败说的是什么**:
        #
        #     取不到结果   外部任务还在,只是这一次没问出来 -> 保留 ID,接着问
        #     生成失败了   外部任务自己有了结论           -> 重新提交才有意义
        #
        # 只有前一类改判。判据是错误码,不是阶段 —— `CONTENT_SAFETY`、
        # `GENERATION_FAILED` 这些即使发生在 status 阶段也是后一类,
        # 再问一百次答案相同。
        if _result_still_retrievable(exc):
            session.commit()
            detail = (
                f"外部任务已存在(ID {external_id}),但"
                f"{'查询状态' if operation == 'status' else '下载结果'}"
                f"失败:{exc.message}。重试会保留这个 ID 从这一步继续,不会重新提交生成"
            )
            logger.warning(
                "provider result not retrievable yet; keeping external id for resume",
                extra={
                    "extra_fields": {"event": "gen.provider_result_not_ready",
                        "task_id": str(task.id),
                        "external_id": external_id,
                        "operation": operation,
                        "error_code": str(exc.code),
                    }
                },
            )
            return _fail(
                session, str(task.id), gs.PROVIDER_RESULT_PENDING_CODE, detail
            )
        raise
    except Exception as exc:
        _abandon_attempt(session, task, attempt, exc, operation=operation, started=started)
        raise

    _check_cancelled(session, task)
    gs.transition(session, task, TaskStatus.DOWNLOADING)
    # 下载是 N 张图 × 最长 30 秒的 HTTP,不能带着任务行锁进去
    _checkpoint(session, task, "downloading")

    # ---- A45-batch12-3 / REG-03:整段落库套进保存点 ----
    #
    # 崩在这一段时的收尾是 `_abandon_attempt()`,而它**内部会 commit** —— 那是它
    # 的职责(外层 `except` 会 rollback,不自己提交等于没写)。但它提交的是
    # **整个会话**,于是这一刻已经 `add()` / `flush()` 过的候选行会跟着一起落库,
    # 而不是随异常消失。上一版这里的注释写着「这次的 `session.add()` 全部随外层
    # rollback 掉了」—— 那句话在 `_abandon_attempt()` 存在的前提下不成立,
    # 库里留下的是半截数据:
    #
    #     任务   FAILED + PROVIDER_RESULT_PENDING,external_task_id 保留
    #     候选   第一张已提交,第二张之后不存在
    #
    # 续跑重新 `fetch_results()` 之后,同一个外部结果又被存了一遍 —— 候选数、
    # 排序、评分输入全部失真,而这条路径**从头到尾没有报错**。
    #
    # 保存点把「候选 + 素材 + 影子写」变成一个可回滚的单元:异常时
    # `ROLLBACK TO SAVEPOINT` 把它们整段撤掉,外层会话仍然可用,
    # `_abandon_attempt()` 提交的就只剩 attempt 收尾和用量流水这两样它真正该写的。
    #
    # 进保存点之前会话必须是干净的:`begin_nested()` 会先把当前 pending 对象
    # flush 一次,而那次 flush 发生在 SAVEPOINT **之前**,回滚不掉。
    # 上面那句 `_checkpoint(session, task, "downloading")` 刚提交过,这里成立。
    # (同一条机制在 `batch_service.create_batch` 里咬过一次,见那边的 REG-04。)
    savepoint = session.begin_nested()
    try:
        outcome = _persist_candidates(session, task, attempt, candidates)
        savepoint.commit()
    except _Cancelled:
        _rollback_savepoint(savepoint, task)
        raise
    except Exception as exc:
        _rollback_savepoint(savepoint, task)
        # 下载与落库同样在这条 attempt 的生命周期内:图已经生成、钱已经花了,
        # 崩在这一步不该让那次调用从账上消失
        _abandon_attempt(session, task, attempt, exc, operation="download", started=started)
        if isinstance(exc, OperationalError):
            # 库不可用:落不进 FAILED,而且这不是任务的错。
            # 交给最外层那条 `except OperationalError` 走 autoretry
            raise
        # ---- A45-batch12-3:EX-03 原来停在这一步之前 ----
        #
        # 走到这里意味着 `fetch_results` 已经返回过 —— **钱已经花了,图也确实
        # 生成出来了**,崩的是我们这一侧的落库(存储层构造失败、`flush()` 失败、
        # 影子写失败)。单张下载失败不会到这里,`_persist_candidates` 自己吞掉了;
        # 能到这里的是基础设施级故障,也就是 EX-05 花了一整段论证的那个
        # S3 / 库抖动家族。
        #
        # 原来这里是裸 `raise`,外层落 `INTERNAL_ERROR`。而 `retry_task` 眼里
        # 那是个普通失败:不是 `SUBMIT_RESULT_UNKNOWN`、不是 `SOURCE_ASSET_*`、
        # 不是 `PROVIDER_RESULT_PENDING`,`can_resume_formatting` 没有 SELECTED 行、
        # `can_resume_scoring` 要 `WORKER_STALLED` —— 于是掉进最后那个 `else`,
        # **清掉 external_task_id 回 QUEUED,重新 submit,再花一次钱。**
        #
        # 与上面 `get_status` / `fetch_results` 那一支是同一句话:外部那笔生成
        # 还在,只是我们这次没把它收回来。所以用同一个码、同一条续跑闸门。
        # 续跑会重新 `fetch_results` 拿一批新的短效 URL,而不是拿旧的那批去下 ——
        # 那批链接本来就快过期了,这也是必须重走一遍而不是"接着存"的理由。
        #
        # 重复候选不会发生 —— 但**不是**因为"外层会 rollback"(REG-03 证明了
        # 那句话是错的:`_abandon_attempt()` 会 commit)。真正的依据有两条,
        # 缺一不可:
        #
        #   1. 上面那个保存点已经把这次写下的候选、素材、影子写整段撤掉;
        #   2. `_persist_candidates()` 按 (attempt, round, candidate_index)
        #      认领已有行,续跑进来是**改写同一行**而不是插新行。
        #
        # 第 2 条单独也够用,它同时覆盖"保存点提交成功、但一张都没下下来"
        # 那条路径(REG-01)—— 那条路径上候选行是**已提交**的,回滚兜不住。
        session.commit()
        detail = (
            f"外部任务已存在(ID {external_id}),结果也已取回,但落库失败:"
            f"{type(exc).__name__}。重试会保留这个 ID 重新取一次结果,不会重新提交生成"
        )
        logger.warning(
            "persisting candidates failed; keeping external id for resume",
            exc_info=True,
            extra={
                "extra_fields": {"event": "gen.candidates_persist_failed",
                    "task_id": str(task.id),
                    "external_id": external_id,
                }
            },
        )
        return _fail(session, str(task.id), gs.PROVIDER_RESULT_PENDING_CODE, detail)
    duration_ms = int((time.monotonic() - started) * 1000)
    stored = outcome.stored

    # ---- A45-batch12-3 / REG-01:"没有候选图"和"候选全部下载失败"不是一回事 ----
    #
    # 这两件事在库里长得几乎一样(本轮零张可用候选),但**下一步相反**:
    #
    #     Provider 一张都没返回   这一轮什么都没产出 -> 按轮次规则重生,再买一次
    #     返回了、我们没下下来     图在对方那里、钱已经花了 -> 保留 ID,重新取一次
    #
    # 把后者当成前者,系统会在没有任何人点击的情况下自动买第二次生成,
    # 而第一次那批图永远失联。判据只能是 **Provider 返回了几个候选**,
    # 不是我们成功存下几个 —— 那正是 `_persist_candidates()` 现在要分开报
    # `provider_count` / `stored` / `download_failed` 三个数的原因。
    all_downloads_failed = bool(outcome.provider_count) and not stored

    _finish_attempt(
        session,
        attempt,
        AttemptStatus.FAILED if all_downloads_failed else AttemptStatus.SUCCEEDED,
        candidate_count=len(stored),
        duration_ms=duration_ms,
    )
    if all_downloads_failed:
        attempt.error_code = gs.PROVIDER_RESULT_PENDING_CODE
        attempt.error_message = (
            f"Provider 返回了 {outcome.provider_count} 个候选,全部下载失败"
        )[:2000]
        attempt.regeneration_reason = RegenerationReason.DOWNLOAD_FAILED.value
    # ---- A45-batch14-18 / 任务 9:能问到厂商就别自己数 ----
    #
    # 下面那个 `max(outcome.provider_count, 1)` 是**推算**:它数的是"Provider
    # 返回了几张图"。FASHN 的计价口径不是张数 —— 官方参考表里一张图是 2~5 个
    # 额度,取决于 `FASHN_RESOLUTION` 与 `generation_mode`,而这两个旋钮运营
    # 在设置页都能改。所以推算值少记的倍数不是常数,是跟着配置浮动的系数,
    # 方向恒定是**少记**。
    #
    # 而厂商每一次 status 响应都在 `x-fashn-credits-used` 里把真数告诉了我们,
    # `fetch_results` 也早就把它抄在候选图上了 —— 在这一批之前没有任何一处读它。
    usage = provider.usage_from_candidates(candidates)
    billable_units, units_source = settle_billable_units(
        usage, inferred=max(outcome.provider_count, 1)
    )
    if usage.reported and usage.units != max(outcome.provider_count, 1):
        # 两个数不一致是**常态**而不是异常(一张图本来就不等于一个额度),
        # 所以这里是 info 不是 warning。记它的理由是对账:§10.2 第 5 条要人
        # 拿这张表去和厂商账单核,而"我们当初推算的是多少"在账单到达之前
        # 就该是可查的,不是等对不上了再回来翻代码算一遍。
        logger.info(
            "provider reported a different billable amount than we would have inferred",
            extra={
                "extra_fields": {"event": "gen.provider_billing_mismatch",
                    "task_id": str(task.id),
                    "provider": task.provider,
                    "reported_units": usage.units,
                    "inferred_units": max(outcome.provider_count, 1),
                    "detail": usage.detail,
                }
            },
        )
    gs.record_usage(
        session, provider=task.provider, task_id=task.id, attempt_id=attempt.id,
        operation="submit", succeeded=not all_downloads_failed,
        candidate_count=len(stored), duration_ms=duration_ms,
        error_code=gs.PROVIDER_RESULT_PENDING_CODE if all_downloads_failed else None,
        # 厂商报了就用厂商的数;没报才退回推算,并**如实标记是哪一种**。
        #
        # 推算这一路的口径不变(REG-01 那一条仍然成立):按 **Provider 产出了
        # 几张** 记,不按我们存下几张 —— 三张里坏一张会记成两张的账,
        # 全部下载失败会记成一张,而钱是按三张收的。
        billable_units=billable_units,
        units_source=units_source,
        # ---- A45-batch12-5 / NEW-01:同一笔生成只能有一条计费流水 ----
        #
        # REG-01 的修复给这个函数开了第二条进入路径(`resuming=True`),而这
        # 一句在两条路径上都会执行。没有计费身份的话,一次"全部下载失败 ->
        # 恢复成功"会在台账上留下两条 submit 流水、6 个计费单位,
        # 而 Provider 只跑过一次、只收过一次钱。
        #
        # 传了键之后第二次是**更新**:结论从失败改成成功、候选数补上,
        # 计费单位取两次的较大值。详见 `gs.record_usage` 的文档。
        billing_key=gs.submit_billing_key(attempt.id),
    )
    # 这一轮"钱花了、图拿到了"的完整现场,先钉死在库里再往下走。
    # 后面评分那步无论怎么崩,都不该让这些重新变成未知。
    _checkpoint(session, task, "candidates-persisted")

    if all_downloads_failed:
        # **不进 `_empty_round()`** —— 那条路会转 REGENERATING 并自动排下一轮。
        # 走和落库失败完全相同的出口:保留 external_task_id,落可续跑的码。
        # 重试从 `PROVIDER_RUNNING` 接着走,重新 `fetch_results()` 拿一批新的
        # 短效 URL 再下一次;Provider 的 submit 次数保持 1。
        detail = (
            f"外部任务已存在(ID {external_id}),Provider 返回了 "
            f"{outcome.provider_count} 个候选,但全部下载失败。"
            "重试会保留这个 ID 重新取一次结果,不会重新提交生成"
        )
        logger.warning(
            "every candidate download failed; keeping external id for resume",
            extra={
                "extra_fields": {"event": "gen.all_candidate_downloads_failed",
                    "task_id": str(task.id),
                    "external_id": external_id,
                    "provider_candidates": outcome.provider_count,
                    "download_failed": outcome.download_failed,
                }
            },
        )
        return _fail(session, str(task.id), gs.PROVIDER_RESULT_PENDING_CODE, detail)

    if not stored:
        # 走到这里 `provider_count == 0`:提交成功,Provider 确实一张都没返回。
        # 这一轮等同于全军覆没,走和"没有 A 档候选"完全相同的轮次判定:
        # 还有轮次就重生,轮次用尽就转人工。不给它开一条特殊通道 ——
        # 特殊通道正是"某些任务永远卡在某个状态"的来源。
        attempt.regeneration_reason = RegenerationReason.NO_CANDIDATE_RETURNED.value
        session.flush()
        return _empty_round(session, task, attempt)

    # ---- A45-batch12-5 / NEW-03:首轮评分也要在租约保护下跑 ----
    #
    # 上一版只有**续跑**分支调 `_claim_phase()`,首轮从 DOWNLOADING 直接转
    # SCORING,整段评分不持有任何租约。而那一段恰恰是最长、最贵的一段:
    #
    #     worker A 首轮评分中(SCORING,无租约)
    #     重复消息到达 -> worker B 走 `_run` 的 SCORING 续跑分支
    #     -> `_claim_phase()` 因为没人持有而**成功**
    #     -> 同一批图被两个 worker 同时送进视觉模型
    #
    # `_claim()` 挡不住它:那条 UPDATE 只认 QUEUED / REGENERATING。
    #
    # 租约在**转 SCORING 之前**抢,不是之后:抢在之前的话,任何看到 SCORING
    # 的 worker 必然也看到租约(两者的可见顺序是"先租约后状态");抢在之后
    # 就留下一道缝,而重复投递恰好是随时可能发生的事。
    #
    # 抢不到时不中止:这一轮的钱已经花了、图已经在库里了,为了一把租约把它
    # 丢回去反而更糟。降级成"没有租约地跑完"—— 也就是上一版的行为,不会更差。
    #
    # `lease is None` 时**一次都不抢**:那时候拿到的持有者标识没有地方放,
    # `run_generation_task` 的 finally 找不到它,于是这把租约要一直挂到过期
    # (一千多秒),期间这条任务既不能被接管也不能被回收。宁可不抢。
    if lease is not None and _lease_owner(lease) is None:
        leased = _claim_phase(session, task, TaskStatus.DOWNLOADING.value)
        if leased is None:
            logger.warning(
                "could not take the scoring lease; another phase lease is still alive",
                extra={
                    "extra_fields": {
                        "event": "gen.scoring_lease_unavailable",
                        "task_id": str(task.id),
                    }
                },
            )
        else:
            lease["owner"] = leased

    gs.transition(session, task, TaskStatus.SCORING)
    # _check_cancelled 里就会提交,这正是我们要的:评分是每张图最长 90 秒的
    # 大模型调用,N 张图串起来比轮询还久,绝不能带着行锁进去。
    _check_cancelled(session, task)

    storage = build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )
    return _score_and_decide(session, task, attempt, stored, storage=storage, lease=lease)


#: 整轮评分失败之后允许重新排的次数。超过就转人工。
#:
#: 为什么要有上限:短暂故障(限流、超时)重排一次通常就好,但如果上游一直不通,
#: 无限重排等于把任务钉在 SCORING 上永远转圈 —— 那和卡死没有区别,
#: 只是多了一堆日志。三次之后交给人比机器继续试有价值。
MAX_SCORING_RETRIES = 3


def _scoring_retries_so_far(session, task: GenerationTask) -> int:
    """这一轮已经重排过几次评分。

    从 `evaluation_attempts` 里数,不另外加计数字段:那张表本来就为了
    \"每次评分请求都留痕\"而存在,重排次数是它的自然推论。多一个字段就多一处
    可能和事实不一致的地方。
    """
    from sqlalchemy import func, select

    from app.core.enums import EvaluationOutcome
    from app.models.evaluation import EvaluationAttempt

    return (
        session.scalar(
            select(func.count())
            .select_from(EvaluationAttempt)
            .where(
                EvaluationAttempt.task_id == task.id,
                EvaluationAttempt.round_number == task.current_round,
                EvaluationAttempt.outcome == EvaluationOutcome.ROUND_RETRY_SCHEDULED.value,
            )
        )
        or 0
    )


def _score_and_decide(
    session, task, attempt, stored, *, storage, lease: dict[str, str] | None = None
) -> dict[str, Any]:
    """评分阶段。**这里出的任何问题都不允许重新生成图片。**

    图已经拿到了,钱已经花了。评分器不好使是我们这一侧的问题,拿它去
    重新生成一批图既修不好问题,又要再付一次生成费用 —— 而且重生会换 seed,
    连"至少还留着这批图"都保不住。所以这个函数的所有出口只有三种:
    正常决策、重新排一次评分、转人工审核。

    以前这里只捕获 `EvaluatorUnavailableError`,于是限流、模型超时、鉴权失败、
    额度不足、内容安全拒绝这些 `ProviderError` 会一路冒泡到最外层,
    把整个任务标成 FAILED —— 候选图虽然还在库里,却没有任何东西把它们送进人工审核,
    只能靠人自己发现。

    ## 逐张心跳(A45-batch12-5 / NEW-03)

    传给 `evaluate_round` 的那个回调**每张图评分之前**跑一次,做三件事:
    提交上一张的结果、推进 `updated_at`、给租约续期。缺了它,一轮 8 张图
    在库里看起来是"2160 秒没有任何动静",而回收器的阈值是 1800 ——
    一个正在正常花钱评分的任务会被判成卡死。

    顺带修掉的是另一件事:上一版整轮评分共用一个事务,第 5 张崩掉时前 4 张
    **已经发生并计费**的视觉调用会随 rollback 一起消失。逐张提交之后,
    花掉的钱一定留在账上。
    """
    def _beat() -> None:
        _heartbeat(session, task, lease)

    try:
        decision = evaluation_service.evaluate_round(
            session, task, stored, storage=storage, heartbeat=_beat
        )
    except ManualReviewRequired as exc:
        # 评分器用不了、没有商品参考图、提示词读不到、整轮都没评成……
        # 共同点是"我们判断不了",不是"图片不好"。
        if getattr(exc, "retry_scoring", False):
            retries = _scoring_retries_so_far(session, task)
            if retries < MAX_SCORING_RETRIES:
                return _reschedule_scoring(session, task, exc, retries=retries, lease=lease)
            logger.error(
                "scoring still failing after retries, escalating to manual review",
                extra={
                    "extra_fields": {
                        "event": "gen.scoring_escalated",
                        "task_id": str(task.id),
                        "retries": retries,
                    }
                },
            )
        return _escalate_to_review(session, task, exc, candidates=len(stored))
    # 落结论之前再确认一次租约还在自己手上。
    #
    # 评分刚刚跑完的这一段是**最贵的一段**:接下来要写的是"这一轮通过/重生/
    # 转人工"这个结论,而它会决定要不要再花一次钱。租约如果在评分期间被接管,
    # 新持有者也在算同一个结论 —— 两份结论谁后写谁赢,而没有任何东西保证
    # 后写的那份是对的。这里抛 `_LeaseLost`,让旧 worker 安静退出。
    _heartbeat(session, task, lease)
    return _apply_decision(session, task, attempt, decision, candidates=len(stored))


def _reschedule_scoring(
    session, task, exc, *, retries: int, lease: dict[str, str] | None = None
) -> dict[str, Any]:
    """短暂故障:把评分重新排一次,任务留在 SCORING。

    刻意**不**改状态:留在 SCORING 意味着下一次 worker 进来会走 `_run` 顶部的
    续跑分支,直接对已有候选图重新评分,而不是从头生成。这和 FORMATTING 的
    续跑是同一个套路 —— 中途崩了就从崩的地方接着走,不回退到花钱的那一步。

    ## 释放租约必须排在登记意图**前面**(A45-batch12-5 / NEW-02)

    上一版的顺序是 `commit -> enqueue -> commit`,租约要等到 `run_generation_task`
    的 `finally` 才还。而 Outbox 行一提交,relay 就可能立刻把消息投出去 ——
    这中间租约还挂在我们身上:

        新 worker 收到消息 -> 看到 SCORING -> `_claim_phase()` 失败
        -> 返回 claimed=False -> 消息被正常 ack
        -> Outbox 已经是 DISPATCHED,不会再投

    任务停在 SCORING,直到大约 30 分钟后被回收器判成卡死。而这条路径上
    **每一层都认为自己成功了**:派发成功、消费成功、退出正常。

    走到这里时本轮该做的已经做完(评分失败、重排次数已经算过),当前 worker
    除了返回没有别的动作,所以先还租约是安全的。反过来,还了之后崩在
    `enqueue` 前:任务停在 SCORING 且无人持有租约,下一次投递能正常接管 ——
    会自愈的死法,比不会自愈的那种好。
    """
    from app.core.enums import DispatchReason
    from app.services import dispatch_service

    session.commit()
    _release_lease_before_dispatch(session, task.id, lease)
    dispatch_service.enqueue(session, task.id, reason=DispatchReason.RETRY.value)
    session.commit()
    logger.warning(
        "rescheduling scoring after a transient evaluator failure",
        extra={
            "extra_fields": {"event": "gen.scoring_rescheduled",
                "task_id": str(task.id),
                "round": task.current_round,
                "retry": retries + 1,
                "max_retries": MAX_SCORING_RETRIES,
            }
        },
    )
    return {
        "task_id": str(task.id),
        "round": task.current_round,
        "status": task.status,
        "scoring_rescheduled": True,
        "retry": retries + 1,
        "redispatch": True,
    }


def _escalate_to_review(session, task, exc, *, candidates: int) -> dict[str, Any]:
    """转人工审核。候选图原样保留,人可以直接在审核页上看图决定。"""
    logger.error(
        "escalating to manual review without regenerating",
        extra={
            "extra_fields": {"event": "gen.escalated_without_regeneration",
                "task_id": str(task.id),
                "error": type(exc).__name__,
                **(exc.detail or {}),
            }
        },
    )
    if task.status != TaskStatus.MANUAL_REVIEW.value:
        gs.transition(session, task, TaskStatus.MANUAL_REVIEW)
    review_service.open_review(
        session, task,
        reason=ReviewReason.REQUIRES_HUMAN_ERROR,
        summary=exc.message[:2000],
    )
    session.flush()
    return {
        "task_id": str(task.id),
        "round": task.current_round,
        "candidates": candidates,
        "outcome": RoundOutcome.MANUAL_REVIEW.value,
        "status": task.status,
        "evaluator_unavailable": True,
    }


def _apply_decision(session, task, attempt, decision, *, candidates: int) -> dict[str, Any]:
    """把轮次决策落到任务状态上(需求第十二章)。

    这里只做"执行",不做"判断" —— 判断全在 evaluators/decision.py 那个纯函数里。
    """
    result: dict[str, Any] = {
        "task_id": str(task.id),
        "round": task.current_round,
        "candidates": candidates,
        "outcome": decision.outcome.value,
    }

    if decision.outcome is RoundOutcome.AUTO_APPROVED:
        gs.transition(session, task, TaskStatus.AUTO_APPROVED)
        # 商品状态不在这里改 —— 出图成功之后才算完成,见 _format_outputs
        if decision.spot_check:
            # 抽检项不阻塞任务:任务照常通过,只是留一条事后复核(需求第十二章 A 档)
            review_service.open_review(
                session, task, reason=ReviewReason.SPOT_CHECK,
                summary="A 档随机抽检,任务已自动通过,仅需事后复核",
            )
            result["spot_check"] = True
        logger.info(
            "task auto approved",
            extra={"extra_fields": {"event": "gen.task_auto_approved", "task_id": str(task.id),
                                    "candidate_id": decision.selected_key}},
        )
        result["selected_candidate_id"] = decision.selected_key
        # "这一轮通过了、选中的是这张"先定下来再去出图。出图要读原图、跑多次
        # 缩放编码、写对象存储,是另一个量级的耗时;崩在那里不该让"通过"这个
        # 结论跟着一起消失 —— 结论没了,重试就会重新生成、重新花钱,
        # 而且换了 seed 之后拿到的根本不是当初通过的那张图。
        _checkpoint(session, task, "approved")
        # 阶段 6:通过之后立刻产出网站要用的多尺寸图
        result.update(_format_outputs(session, task, decision.selected_key))
        result["status"] = task.status
        return result

    if decision.outcome is RoundOutcome.MANUAL_REVIEW:
        # 注意:进队列的是**任务**,不是那些低分候选图(需求第十二章)
        gs.transition(session, task, TaskStatus.MANUAL_REVIEW)
        review_service.open_review(
            session, task,
            reason=decision.review_reason or ReviewReason.ROUNDS_EXHAUSTED,
            summary=";".join(decision.reasons)[:2000] or None,
        )
        logger.info(
            "task escalated to manual review",
            extra={
                "extra_fields": {
                    "event": "gen.task_escalated",
                    "task_id": str(task.id),
                    "round": task.current_round,
                    "max_rounds": task.max_rounds,
                }
            },
        )
        result["status"] = task.status
        return result

    # ---- 重生 ----
    plan = decision.repair_plan
    if plan is not None and attempt is None:
        # 续跑评分时进来的,本次调用没有新建 attempt。修复策略无处可记,
        # 而需求第九章要求每次重生都必须留下"为什么重生 + 用了什么策略"。
        # 与其把这条记录悄悄丢掉,不如转人工 —— 重生本来就要再花一次钱,
        # 花之前把账记清楚不算苛刻。
        logger.error(
            "cannot record the repair strategy, escalating instead of regenerating",
            extra={"extra_fields": {"event": "gen.repair_unrecordable", "task_id": str(task.id)}},
        )
        gs.transition(session, task, TaskStatus.MANUAL_REVIEW)
        review_service.open_review(
            session, task, reason=ReviewReason.REQUIRES_HUMAN_ERROR,
            summary="续跑评分后需要重生,但找不到对应的生成记录,无法留痕;请人工确认",
        )
        result["outcome"] = RoundOutcome.MANUAL_REVIEW.value
        result["status"] = task.status
        return result
    if plan is not None:
        # 需求第九章:每次重生必须记录原因和使用的修复策略
        attempt.regeneration_reason = plan.reason.value
        attempt.repair_strategy = plan.to_dict()
        if plan.switch_provider:
            alternative = next_configured_provider(task.provider, GenerationMode(task.mode))
            if alternative is not None:
                logger.info(
                    "switching provider by repair rule",
                    extra={
                        "extra_fields": {
                            "event": "gen.repair_switch_provider",
                            "task_id": str(task.id),
                            "from": task.provider,
                            "to": alternative.provider_name,
                        }
                    },
                )
                attempt.repair_strategy = {
                    **attempt.repair_strategy,
                    "provider_switched_to": alternative.provider_name,
                }
                task.provider = alternative.provider_name
            else:
                # 诚实记录:规则要求换,但没有第二个已配置的 Provider 可换
                attempt.repair_strategy = {
                    **attempt.repair_strategy,
                    "provider_switch_skipped": "没有其它已配置且支持该模式的 Provider",
                }

    gs.transition(session, task, TaskStatus.REGENERATING)
    # 意图和状态转移同一个事务:REGENERATING 落库了,"要跑下一轮"就一定也落库了。
    _enqueue_next_round(session, task)
    session.flush()
    result["status"] = task.status
    result["redispatch"] = True
    return result


def _format_outputs(
    session, task, candidate_id: str | None, *, already_formatting: bool = False
) -> dict[str, Any]:
    """通过 -> 多尺寸输出 -> 完成(需求第十五章)。

    渲染失败不回滚"已通过"这个结论 —— 图是好图,只是没转换成功。
    任务转 FAILED 并保留错误码;重试时 `retry_task` 会认出这种情况,
    直接回到 FORMATTING 重做出图,不重新生成。
    """
    from app.services import output_service

    # 这两条以前只返回一个错误字典就完事,任务留在 AUTO_APPROVED 或 FORMATTING ——
    # 既不是终态,也不会被任何重试路径捞回来,人工在界面上看到的是一个永远「在出图」
    # 的任务。没有成品图就是没做完,必须落到 FAILED 上。
    def _abort(code: str, message: str) -> dict[str, Any]:
        logger.error(
            "formatting aborted",
            extra={
                "extra_fields": {
                    "event": "gen.formatting_aborted",
                    "task_id": str(task.id),
                    "reason": code,
                }
            },
        )
        if not sm.is_terminal(task.status):
            gs.transition(
                session, task, TaskStatus.FAILED, error_code=code, error_message=message
            )
        return {"outputs": 0, "output_error": message, "status": task.status}

    if not candidate_id:
        return _abort("SELECTED_CANDIDATE_MISSING", "没有选中的候选图,无法出图")

    candidate = session.get(GenerationCandidate, UUID(candidate_id))
    if candidate is None:
        return _abort("SELECTED_CANDIDATE_MISSING", "选中的候选图不存在,无法出图")

    if not already_formatting:
        gs.transition(session, task, TaskStatus.FORMATTING)
    # 出图要读原图、跑多次缩放编码、逐个写对象存储(S3 时还是网络往返),
    # 是另一个量级的耗时。带着行锁进去,回收器和其它写路径都会被挡在外面。
    #
    # 提交还顺带改善了崩溃后的可恢复性:worker 死在渲染中途时,任务**留在**
    # FORMATTING —— 这是准确的描述,回收器落它成 FAILED 之后,
    # retry_task 的 can_resume_formatting() 会认出"图已经选好了",
    # 直接重做出图而不重新生成,省下一次 Provider 的钱。
    _checkpoint(session, task, "formatting")
    storage = build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )

    # ---- A45-batch12-2 / EX-05:续跑之前先确认源文件还在、还能解 ----
    #
    # `can_resume_formatting()` 只查库:有 SELECTED 行、没有成品图,就判定
    # "可以从出图接着跑"。它看不见那一行指着的对象还在不在。文件被清理脚本
    # 删掉、写入截断、或者存进去的是一段 HTML 错误页时,续跑会一次次读同一个
    # 坏路径,`FAILED -> FORMATTING -> FAILED` 循环,永远产不出成品图。
    #
    # 这里把它变成一个**有名字的失败**。落通用 INTERNAL_ERROR 是不够的:
    # 那个码在 `retry_task()` 眼里和别的崩溃没有区别,重试照样进 FORMATTING。
    # 专用码才挡得住(见 `enums.SOURCE_ASSET_BROKEN_CODES`),而且它告诉运营
    # 该做的是换一张候选或重新生成,不是再点一次重试。
    try:
        problem = output_service.source_asset_problem(candidate, storage=storage)
    except Exception:  # noqa: BLE001
        # 存储层自己不通(S3 挂了、凭据过期)**不是**文件坏了。让它照常
        # 走下面的通用异常分支落成可重试的失败 —— 这一类重试就能好,
        # 判成 SOURCE_ASSET_* 会把它错误地挡在重试之外
        logger.warning(
            "cannot verify the selected candidate before formatting",
            extra={
                "extra_fields": {
                    "event": "gen.candidate_unverifiable",
                    "task_id": str(task.id),
                }
            },
        )
        problem = None
    if problem is not None:
        return _abort(
            problem,
            f"选中的候选图无法读取({problem});"
            "重试不会好转,请回审核页改选一张可用的候选,或确认后重新生成",
        )

    try:
        assets = output_service.build_outputs(
            session, task, candidate, storage=storage
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "output formatting failed",
            extra={
                "extra_fields": {
                    "event": "gen.formatting_failed",
                    "task_id": str(task.id),
                    "candidate_id": candidate_id,
                }
            },
        )
        task.error_code = ErrorCode.INTERNAL_ERROR.value
        task.error_message = f"多尺寸输出失败:{type(exc).__name__}"[:500]
        gs.transition(session, task, TaskStatus.FAILED)
        session.flush()
        return {"outputs": 0, "output_error": task.error_message}

    gs.transition(session, task, TaskStatus.COMPLETED)
    task.finished_at = _now()

    # 商品状态放在出图成功**之后**改:出图失败却把商品标成"已完成",
    # 会让商品列表显示一个根本没有图可用的 SKU。
    product = session.get(Product, task.product_id)
    if product is not None and product.status != ProductStatus.ARCHIVED.value:
        product.status = ProductStatus.COMPLETED.value

    session.flush()
    return {"outputs": len(assets), "purposes": [a.purpose for a in assets]}


def _empty_round(session, task, attempt) -> dict[str, Any]:
    """本轮一张图都没拿到时的去向。复用同一套轮次规则。"""
    from app.evaluators.decision import decide_round
    from app.services.rule_set_service import load_active_rule_set

    decision = decide_round(
        [],
        round_number=task.current_round,
        max_rounds=task.max_rounds,
        thresholds=load_active_rule_set(session).thresholds,
        task_key=str(task.id),
    )
    if decision.outcome is RoundOutcome.MANUAL_REVIEW:
        gs.transition(
            session, task, TaskStatus.MANUAL_REVIEW,
            error_code="NO_CANDIDATE", error_message="Provider 未返回任何候选图",
        )
        review_service.open_review(
            session, task, reason=ReviewReason.ROUNDS_EXHAUSTED,
            summary="轮次已耗尽,且 Provider 未返回任何候选图",
        )
        return {"task_id": str(task.id), "status": task.status, "candidates": 0,
                "outcome": decision.outcome.value}

    gs.transition(
        session, task, TaskStatus.REGENERATING,
        error_code="NO_CANDIDATE", error_message="Provider 未返回任何候选图",
    )
    _enqueue_next_round(session, task)
    session.flush()
    return {"task_id": str(task.id), "status": task.status, "candidates": 0,
            "outcome": decision.outcome.value, "redispatch": True}


def _enqueue_next_round(session, task: GenerationTask) -> None:
    """在当前事务里登记「要跑下一轮」。不提交 —— 提交了 Outbox 就白做了。"""
    from app.core.enums import DispatchReason
    from app.services import dispatch_service

    dispatch_service.enqueue(session, task.id, reason=DispatchReason.NEXT_ROUND.value)


def _dispatch_next_round(session, task_id: str) -> None:
    """把任务重新投进队列跑下一轮。

    意图在 ``_apply_decision`` / ``_empty_round`` 里就随 REGENERATING 一起写进了
    Outbox,和状态转移同一个事务。这里只是提交之后立刻投一次,省掉等 relay 的延迟;
    投失败也不要紧,relay 会接手。

    以前这里是 ``delay()`` + ``except: logger.exception``。Broker 抖一下,
    任务就永远停在 REGENERATING —— 而重试接口只认能转回 QUEUED 的状态,
    REGENERATING 不在其列,人工也捞不回来。
    """
    from app.services import dispatch_service

    dispatch_service.deliver_pending(session, UUID(task_id))


def _task_plan_angles(session, task: GenerationTask):
    """这份任务按哪些角度出图。没有方案就是空元组。

    读的是 `task.generation_plan_id` —— 也就是**创建任务那一刻**解析并记下的
    那一份,不是"现在这个颜色生效的那一份"。区别是真实的:方案可以在任务
    排队期间被换掉,而这一轮图是按旧方案的角度提交的。用新方案的角度去标
    候选图,等于给一批图贴上它们没有被要求过的标签。

    `override_plan` 建的任务这两列都是空的(§5.3),所以它自然走"没有方案"
    那条路 —— 绕过了方案就不该有方案角度。
    """
    from app.models.generation_plan import GenerationPlan
    from app.workflows import generation_plan as gp

    plan_id = getattr(task, "generation_plan_id", None)
    if not plan_id:
        return ()
    row = session.get(GenerationPlan, plan_id)
    if row is None:
        # 方案行被删了。**不拦**:图已经在生成或已经生成完,这里只是标角度
        logger.warning(
            "generation plan row is gone; candidates will not carry a target angle",
            extra={
                "extra_fields": {
                    "event": "gen.plan_row_missing",
                    "task_id": str(task.id),
                    "plan_id": str(plan_id),
                }
            },
        )
        return ()
    return gp.normalize_angles(row.angles_json)


def _plan_angle_assignments(session, task: GenerationTask, total: int):
    """第 i 张候选图该覆盖哪个角度。判定在 `generation_plan.angle_assignments`。"""
    from app.workflows import generation_plan as gp

    return gp.angle_assignments(_task_plan_angles(session, task), total)


def _build_request(
    session, task: GenerationTask, provider: ImageGenerationProvider
) -> GenerationRequest:
    storage = build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )

    def image_reference(storage_path: str) -> str:
        return provider_input_reference(
            provider,
            storage_backend=settings.STORAGE_BACKEND,
            storage_path=storage_path,
            external_url=lambda: asset_url(storage, storage_path),
        )

    assets = list(
        session.query(ProductAsset).filter(
            ProductAsset.id.in_([UUID(a) for a in task.input_asset_ids])
        )
    )
    by_type = {a.asset_type: a for a in assets}
    # 没有「随便拿第一张」的兜底:详情图、背景图、模特图当成商品图送出去,
    # Provider 会照单全收地生成一张错得很自信的图,而且钱已经花了。
    # 创建任务时已经验过一次(generation_service._assert_assets_are_usable),
    # 这里是第二道 —— 素材可能在排队期间被改过。
    garment = by_type.get("GARMENT_CUTOUT") or by_type.get("GARMENT_FRONT")
    if garment is None:
        from app.core.errors import ValidationError

        raise ValidationError("任务没有可用的商品图(需要正面图或抠图)")

    # 上一轮结束时留下的修复计划(需求第十二章 B/C 档)
    plan = _pending_repair_plan(session, task)

    model_url = None
    template_id = task.model_template_id
    if plan.get("change_model_template"):
        # 换模特模板:人体/面部错误和遮挡靠换 seed 基本没用(evaluators/repair.py)
        template_id = _alternate_model_template(session, template_id)
        if template_id != task.model_template_id:
            task.model_template_id = template_id

    if template_id:
        template = session.get(ModelTemplate, template_id)
        if template and template.enabled:
            model_url = image_reference(template.storage_path)
    # ---- 这里原来有一条退回自由上传模特图的兜底,已删(2026-08-11 评审)----
    #
    #     if model_url is None and "MODEL_REFERENCE" in by_type:
    #         model_url = image_reference(by_type["MODEL_REFERENCE"].storage_path)
    #
    # 它是 C-10 那条合规绕行缝的**第二道门**,而且比第一道更隐蔽:创建任务
    # 那一刻模板是好的、四道检查全过了,而执行时模板可能已经被停用或换掉
    # (`change_model_template` 挑不到别的启用模板时会原样返回),于是
    # worker **静默地**换成一张没有受众、没有年龄确认、没有授权范围的图,
    # 把钱花出去。日志里连一行都没有。
    #
    # 现在拿不到模板图就让它按缺模特图失败:`validate_request` 会以
    # 「虚拟试穿必须提供模特图」拦在发请求之前,一分钱不花。
    #
    # **代价说明白**:改之前建的、依赖这条兜底的排队任务会失败。那是刻意的 ——
    # 一个合规闸的正确失败方向是拒绝执行,不是换一张图继续跑。
    if model_url is None and "MODEL_REFERENCE" in by_type:
        logger.warning(
            "task has no usable model template; refusing to fall back to the "
            "free-uploaded MODEL_REFERENCE asset (PRD 6.4)",
            extra={
                "extra_fields": {"event": "gen.model_template_missing",
                    "task_id": str(task.id),
                    "model_template_id": str(template_id or ""),
                }
            },
        )

    task.current_round = (task.current_round or 0) + 1
    base = task.base_seed if task.base_seed is not None else 20240101
    seed = next_seed(base, task.current_round)  # 每轮换新 seed(需求第九章)

    prompt = apply_prompt_additions(task.prompt, plan.get("prompt_additions") or [])
    negative_prompt = apply_prompt_additions(
        task.negative_prompt, plan.get("negative_prompt_additions") or []
    )
    options = {**(task.provider_params or {}), **(plan.get("provider_param_overrides") or {})}
    session.flush()

    return GenerationRequest(
        mode=GenerationMode(task.mode),
        garment_image_url=image_reference(garment.storage_path),
        model_image_url=model_url,
        prompt=prompt,
        negative_prompt=negative_prompt,
        candidate_count=task.candidate_count,
        width=task.output_width,
        height=task.output_height,
        seed=seed,
        options=options,
        angle_units=_angle_units_for(session, task, prompt),
    )


def _angle_units_for(session, task: GenerationTask, prompt: str | None):
    """把方案的角度翻成 Provider 层的工作单元(2026-08-11 评审)。

    翻译在这里而不是让 Provider 去读方案:`providers/` 不认识
    `app.workflows`,同 `channels` 只接 `CanonicalProduct` 是一条规矩。

    **角度张数之和与 `task.candidate_count` 必须相等**,而这一点是由
    `create_task` 保证的(`gp.governed_candidate_count` 就是那个和)。
    对不上时**不拆**:`validate_request` 会在发请求前把不一致拦成
    `ProviderInputError`,但那时任务已经建好、运营看到的是"生成失败"。
    这里退回一次出完并留一条日志 —— 少了角度约束是可以接受的降级,
    而角度和张数对不上会让**每一张**候选图的角度都标错。

    能走到这个分支的只有一种情况:任务建好之后有人原地改了方案的角度
    (DRAFT 可以原地改,而 `create_task` 解析到的可能正是它)。
    """
    from app.providers.base import AngleWorkUnit
    from app.workflows import generation_plan as gp

    angles = _task_plan_angles(session, task)
    if not angles:
        return ()
    units = gp.angle_units(angles, base_prompt=prompt)
    planned = sum(u.count for u in units)
    if planned != task.candidate_count:
        logger.warning(
            "plan angle counts no longer match the task candidate count; "
            "submitting without per-angle work units",
            extra={
                "extra_fields": {"event": "gen.plan_angle_mismatch",
                    "task_id": str(task.id),
                    "planned": planned,
                    "candidate_count": task.candidate_count,
                }
            },
        )
        return ()
    return tuple(
        AngleWorkUnit(angle=u.angle, count=u.count, prompt=u.prompt) for u in units
    )


def _pending_repair_plan(session, task: GenerationTask) -> dict[str, Any]:
    """取上一轮留下的修复计划。

    计划在第 N 轮**结束时**算出来,写在第 N 轮的 attempt 上,由第 N+1 轮读取执行。
    没写成任务上的一个字段,是因为 attempt 本来就是"这一轮发生了什么"的账本,
    修复决策属于账本的一部分,放到任务上会丢掉它属于哪一轮。
    """
    if not task.current_round:
        return {}
    latest = (
        session.query(GenerationAttempt)
        .filter(
            GenerationAttempt.task_id == task.id,
            GenerationAttempt.repair_strategy.isnot(None),
        )
        .order_by(GenerationAttempt.round_number.desc(), GenerationAttempt.attempt_number.desc())
        .first()
    )
    if latest is None or latest.round_number != task.current_round:
        return {}
    return dict(latest.repair_strategy or {})


def _alternate_model_template(session, current_id):
    """挑一个不同于当前的启用模板。只有一个可用时保持不变。

    刻意按 id 排序取"下一个",而不是随机挑:重放同一个任务必须得到同样的模板序列。
    """
    rows = list(
        session.query(ModelTemplate)
        .filter(ModelTemplate.enabled.is_(True))
        .order_by(ModelTemplate.id)
    )
    if len(rows) <= 1:
        return current_id
    ids = [r.id for r in rows]
    if current_id not in ids:
        return ids[0]
    return ids[(ids.index(current_id) + 1) % len(ids)]


def _rollback_savepoint(savepoint, task: GenerationTask) -> None:
    """回滚保存点。**回滚失败只记日志,不往上抛。**

    走到这里时手上已经有一个异常了,而它才是要排查的那个。让
    `ROLLBACK TO SAVEPOINT` 自己再抛一次会把原始异常盖掉 —— 库已经不可用
    (`OperationalError`)时这条路必然发生,而那正是最需要看清原始异常的时候。

    回滚不成功也不影响正确性:那种情况下整个连接都要被丢弃,保存点里的写入
    根本到不了库。
    """
    try:
        savepoint.rollback()
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.warning(
            "could not roll back the candidate savepoint",
            exc_info=True,
            extra={
                "extra_fields": {
                    "event": "gen.candidate_savepoint_rollback_failed",
                    "task_id": str(task.id),
                }
            },
        )


def _abandon_attempt(
    session,
    task: GenerationTask,
    attempt: GenerationAttempt,
    exc: BaseException,
    *,
    operation: str,
    started: float,
) -> None:
    """非 `ProviderError` 逃出 Provider 交互区时,**把这条 attempt 收尾**。

    ## 要修的那件事

    `_checkpoint(session, task, "submitting")` 已经把 `status=SUBMITTED` 的
    attempt 提交进库了 —— 这是刻意的,它是「我们向 Provider 发过一次请求」的
    唯一证据。但收尾只在 `except ProviderError` 里做。任何别的异常
    (TypeError、KeyError、JSON 解析、httpx 未被归类的错误、下载环节的意外)
    会一路冒到最外层,那里 `session.rollback()` 之后把**任务**落成 FAILED,
    而这条 attempt 永远停在:

        status        SUBMITTED      看起来还在跑
        finished_at   NULL           查不到它什么时候结束的
        error_code    NULL           查不到为什么
        用量流水       一条都没有      这次调用从账上彻底消失

    最后一条和 A23 的花费台账叠加起来尤其糟:一次**已经花了钱**的调用,
    在库里既不是成功也不是失败,而是不存在。

    ## 为什么这里必须自己 commit

    外层的 `except Exception` 会 `session.rollback()`。不在这里提交的话,
    刚写下的收尾会跟着被回滚掉,等于没写。

    提交失败就只记日志:这条路径本来就是在处理一次异常,让收尾动作再抛一次
    只会把原始异常盖掉 —— 而原始异常才是要排查的那个。

    ## 这个 commit 提交的是**整个会话**(A45-batch12-3 / REG-03)

    这是上一轮踩到的那个坑:调用方以为"崩了就随 rollback 消失"的候选行,
    会被这一句一起提交,于是库里留下半截数据。真正的防线在调用方 ——
    候选落库整段套在保存点里,异常时先 `ROLLBACK TO SAVEPOINT`。

    这里再加一道:进来时会话里如果还挂着别的待写业务行,说明某条调用路径
    忘了套保存点。**摘掉它们再提交**,并且大声记一条日志 —— 这条日志出现
    就意味着有一条路径需要修,而不是让它安静地写进库里。
    """
    _drop_stray_pending_rows(session, task, attempt)
    try:
        _finish_attempt(
            session, attempt, AttemptStatus.FAILED,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        attempt.error_code = "INTERNAL_ERROR"
        attempt.error_message = f"流水线异常:{type(exc).__name__}"[:2000]
        gs.record_usage(
            session, provider=task.provider, task_id=task.id, attempt_id=attempt.id,
            operation=operation, succeeded=False, error_code="INTERNAL_ERROR",
            duration_ms=int((time.monotonic() - started) * 1000),
            # 只有 submit 那一种操作有计费身份:一条 attempt 只可能生成一次,
            # 而 status / fetch / download 每一次都是真的又调了一次
            # (或者根本不是 Provider 调用),它们不该被折叠成一行
            billing_key=(
                gs.submit_billing_key(attempt.id)
                if operation == gs.SUBMIT_BILLING_OPERATION
                else None
            ),
        )
        session.commit()
    except Exception:  # noqa: BLE001 - 见 docstring 最后一段
        session.rollback()
        logger.exception(
            "could not close out the attempt after an unexpected failure",
            extra={
                "extra_fields": {
                    "event": "gen.attempt_close_failed",
                    "task_id": str(task.id),
                    "attempt_id": str(attempt.id),
                }
            },
        )


def _drop_stray_pending_rows(
    session, task: GenerationTask, attempt: GenerationAttempt
) -> None:
    """把 attempt 收尾之外的待写行从会话里摘掉。

    `_abandon_attempt()` 那一句 `session.commit()` 是会话级的,它分不清
    「我要写的收尾」和「上一步写到一半的业务行」。保存点已经在调用方把后者
    挡住了,这里是第二道:漏了一条路径的代价是**半截数据静悄悄进库**,
    而那种数据事后没有任何办法和正常数据区分开。

    只处理 `session.new`(还没 INSERT 的):`dirty` 里多半是 task / attempt
    自己的字段改动,那是收尾要写的东西。真正危险的是新行 —— 它们是
    "第一张候选已经 flush、第二张抛了异常"那种形状。
    """
    stray = [obj for obj in session.new if obj is not attempt and obj is not task]
    if not stray:
        return
    logger.error(
        "unexpected pending rows while abandoning an attempt; dropping them",
        extra={
            "extra_fields": {"event": "gen.stray_pending_rows",
                "task_id": str(task.id),
                "attempt_id": str(attempt.id),
                # 类型名够定位是哪条路径漏了保存点,又不会把业务字段写进日志
                "kinds": sorted({type(obj).__name__ for obj in stray}),
                "count": len(stray),
            }
        },
    )
    for obj in stray:
        try:
            session.expunge(obj)
        except Exception:  # noqa: BLE001 - 摘不掉就让它去撞保存点/约束,别在这里抛
            logger.warning("could not expunge a stray pending row", exc_info=True, extra={
                "extra_fields": {
                    "event": "gen.stray_pending_rows",
                }
            })


def _partial_ids(exc: ProviderError) -> list[str]:
    """从 Provider 异常里取出「已经提交成功的那几个外部 ID」。

    读 detail 而不是让编排层认识 FASHN:分批提交是 provider 的实现细节,
    但「已受理的凭证不能丢」是编排层的责任。用一个约定好的键把两者接起来,
    别的 provider 将来要分批提交时挂同一个键即可,这里不用改。

    容错到底:detail 的形状是 provider 给的,拿到什么都不该让失败处理**再**失败 ——
    那会把一次可解释的失败变成一次没有任何记录的崩溃。
    """
    from app.providers.fashn import PARTIAL_IDS_KEY

    raw = (getattr(exc, "detail", None) or {}).get(PARTIAL_IDS_KEY)
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw if item]


def _new_attempt(session, task: GenerationTask, seed: int | None) -> GenerationAttempt:
    existing = (
        session.query(GenerationAttempt)
        .filter(GenerationAttempt.task_id == task.id,
                GenerationAttempt.round_number == task.current_round)
        .count()
    )
    attempt = GenerationAttempt(
        task_id=task.id,
        round_number=task.current_round,
        attempt_number=existing + 1,
        provider=task.provider,
        seed=seed,
        status=AttemptStatus.SUBMITTED.value,
        started_at=_now(),
        request_payload={"candidate_count": task.candidate_count, "mode": task.mode},
    )
    session.add(attempt)
    session.flush()
    return attempt


def _finish_attempt(
    session, attempt: GenerationAttempt, status: AttemptStatus, *,
    error: ProviderError | None = None, candidate_count: int = 0, duration_ms: int | None = None,
) -> None:
    attempt.status = status.value
    attempt.finished_at = _now()
    attempt.candidate_count = candidate_count
    attempt.duration_ms = duration_ms
    if status is AttemptStatus.SUCCEEDED:
        # ---- 同一条 attempt 会被收尾**第二次**(A45-batch12-4) ----
        #
        # 这个函数原来是按"一条 attempt 只收尾一次"写的:失败时写三列错误信息,
        # 成功时不碰它们 —— 成功路径上那三列本来就是空的,不写等于写空。
        #
        # REG-01 / REG-02 / REG-03 各开了一条"同一条 attempt 再跑一遍"的续跑路径,
        # 那个前提就没了。第一遍失败写下的三列,在第二遍成功之后仍然留在行上:
        #
        #     status              SUCCEEDED
        #     error_code          PROVIDER_RESULT_PENDING
        #     error_message       Provider 返回了 3 个候选,全部下载失败
        #     regeneration_reason DOWNLOAD_FAILED
        #
        # 三列都经 `AttemptOut` 原样回到接口,运营看到的是一条"成功但写着
        # 全部下载失败"的记录 —— 而这一轮修复的整个目的就是让人能相信界面上
        # 那句话。`_abandon_attempt()` 写下的 `INTERNAL_ERROR` 同理。
        #
        # 清空放在 `if error is not None` 之前:`_await_and_collect` 会在
        # `_finish_attempt(SUCCEEDED)` 之后补写 `regeneration_reason`
        # (Provider 一张都没返回那条),那次赋值必须留得住。
        attempt.error_code = None
        attempt.error_message = None
        attempt.regeneration_reason = None
    if error is not None:
        attempt.error_code = str(error.code)
        attempt.error_message = error.message[:2000]
        attempt.regeneration_reason = (
            RegenerationReason.PROVIDER_TIMEOUT.value
            if status is AttemptStatus.TIMEOUT
            else RegenerationReason.PROVIDER_ERROR.value
        )
    session.flush()


@dataclass
class _PersistOutcome:
    """一次候选落库的三个数。**它们必须分开报,合成一个就是 REG-01。**

    `_await_and_collect` 原来只拿到一个 `stored` 列表,于是「Provider 一张都
    没返回」和「返回了、我们一张都没下下来」在它眼里长得一模一样 —— 而这两件事
    的下一步相反:前者该按轮次规则重生(再买一次),后者该保留 external_task_id
    重新取结果(不再买)。判据只能是 `provider_count`。

    `download_failed` 不参与判定,它是给日志和运营看的:全部失败时要能一眼
    看出这一轮到底有几张图没拿回来,而不是从别处倒推。
    """

    #: Provider 这一轮返回了几个候选。**钱是按这个数收的。**
    provider_count: int
    #: 本轮真正躺在自有存储里、可以拿去评分的候选行(含上一次续跑就已经存好的)
    stored: list[GenerationCandidate] = field(default_factory=list)
    #: 这一遍下载失败的张数
    download_failed: int = 0
    #: 这一遍直接复用了上一次结果、没有重新下载的张数
    reused: int = 0


def _drop_shrunk_tail(
    session,
    task: GenerationTask,
    existing: dict[int, GenerationCandidate],
    *,
    kept: int,
) -> None:
    """续跑这一遍比上一遍少拿回来几张时,清掉尾巴上那几行(A45-batch12-4)。

    `_persist_candidates()` 按 `candidate_index` 认领已有行,但它只遍历**这一遍**
    的候选。上一遍存了 3 行、这一遍 `fetch_results()` 只返回 2 个的话,index 2
    那一行既不会被改写、也不会进 `stored` —— 它以 `DOWNLOAD_FAILED` 的样子
    永远留在库里,界面上就是一张查不出所以然的失败候选。

    分批提交的 provider(FASHN 是一次一张凑够张数)在部分子任务失效时会出现
    这个形状。Mock 不会,所以它不在上一轮的测试视野里。

    ## 只删还没成功的那些

    `PENDING` / `DOWNLOAD_FAILED` 的行没有字节、没有影子素材、更不可能有评分,
    删掉不牵连任何东西。`DOWNLOADED` 的不删:那是一张**真的存在于我们自己存储里**
    的图,而 `evaluations` 是 `ON DELETE CASCADE`。这种形状目前没有已知成因,
    出现就说明有别的地方错了 —— 记一条 error 让人来看,比替人做决定安全。
    """
    stale = [row for index, row in existing.items() if index >= kept]
    if not stale:
        return
    removable = [
        row for row in stale if row.status != CandidateStatus.DOWNLOADED.value
    ]
    kept_back = [row for row in stale if row not in removable]
    logger.warning(
        "provider returned fewer candidates than the previous round; trimming the tail",
        extra={
            "extra_fields": {"event": "gen.provider_fewer_candidates",
                "task_id": str(task.id),
                "now": kept,
                "before": len(existing),
                "deleted": len(removable),
                "kept_downloaded": len(kept_back),
            }
        },
    )
    for row in removable:
        session.delete(row)
    if kept_back:
        logger.error(
            "a previously downloaded candidate fell out of the provider result set",
            extra={
                "extra_fields": {"event": "gen.provider_result_missing",
                    "task_id": str(task.id),
                    "candidate_indexes": sorted(r.candidate_index for r in kept_back),
                }
            },
        )


def _persist_candidates(session, task, attempt, candidates) -> _PersistOutcome:
    """把候选图落到自有存储。

    第三方 URL 有效期通常很短,必须立刻下载(需求第十九章)。
    单张下载失败不拖垮整轮 —— 标记该候选并继续,让阶段 4 决定是否重生。

    ## 这个函数必须可以被跑第二遍(A45-batch12-3 / REG-01、REG-03)

    它有两条续跑进入路径,两条都是「同一个 attempt 的同一轮再走一次」:

        全部候选下载失败    -> PROVIDER_RESULT_PENDING -> 重试 -> 重新 fetch_results
        reaper 回收 DOWNLOADING -> 同上

    原来它无条件 `GenerationCandidate(...)` + `session.add()`,于是续跑一次就
    多一整套候选行:候选数、排序、评分输入全部失真,而且**没有任何报错**。

    现在按 `(attempt_id, round_number, candidate_index)` 认领已有行:

        已有行是 DOWNLOADED 且文件还在  直接复用,不重下(我们已经有那些字节了)
        已有行是 PENDING / DOWNLOAD_FAILED  就地改写这一行,不插新行

    `candidate_index` 能当键用,是因为同一个 `external_task_id` 的
    `fetch_results()` 返回的是同一批图、同样的顺序,变的只是那批**短效签名 URL**。
    这也是续跑必须重走一遍而不是"接着存旧 URL"的理由。

    数据库那一侧由迁移 0033 的唯一约束兜底:这里漏了的话,那条约束会把
    "又存了一遍"变成一次 IntegrityError,而不是一批看不出来的重复候选。

    反过来"这一遍比上一遍少"由 `_drop_shrunk_tail()` 收拾 —— 认领只覆盖
    这一遍有的 index,上一遍多出来的那几行不会自己消失。
    """
    storage = build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )
    # 续跑进来时这一轮可能已经有行了。一次查出来按 index 索引,
    # 后面每张图都走同一份映射 —— 逐张查会在 N 张图上打 N 次库,
    # 而且中间任何一次读到的都可能是别人刚写的
    existing: dict[int, GenerationCandidate] = {
        row.candidate_index: row
        for row in session.query(GenerationCandidate)
        .filter(
            GenerationCandidate.attempt_id == attempt.id,
            GenerationCandidate.round_number == task.current_round,
        )
        .all()
    }
    outcome = _PersistOutcome(provider_count=len(candidates))
    touched: list[GenerationCandidate] = []
    # 第 i 张**本来该覆盖**哪个角度(2026-08-11 评审)。从任务上那份方案
    # 现算,不从请求里带 —— `_await_and_collect` 有一条续跑进入路径
    # (PROVIDER_RUNNING),那时手上没有 request。方案是同一份、
    # `angle_assignments` 是纯函数,所以两条路径算出来一样
    intended_angles = _plan_angle_assignments(session, task, len(candidates))

    for index, item in enumerate(candidates):
        row = existing.get(index)
        if (
            row is not None
            and row.status == CandidateStatus.DOWNLOADED.value
            and row.storage_path
        ):
            # 上一次已经下好了。**不重下** —— 字节就在我们自己的存储里,
            # 再下一次既慢又可能因为签名过期而把一张好图改判成失败
            outcome.reused += 1
            touched.append(row)
            continue

        if row is None:
            row = GenerationCandidate(
                task_id=task.id,
                attempt_id=attempt.id,
                round_number=task.current_round,
                candidate_index=index,
            )
            session.add(row)
        # 续跑时这几列要跟着新的 fetch_results 走:URL 换了一批,
        # 留着上一次那条过期地址会让排查的人以为我们下的是它
        row.external_id = item.external_id
        row.source_url = item.image_url
        row.seed = item.metadata.get("seed")
        row.candidate_metadata = {
            k: v for k, v in item.metadata.items() if k != "inline_bytes"
        }
        # ---- 这一张打算覆盖的角度(2026-08-11 评审)----
        #
        # 落在 `candidate_metadata` 而不是新开一列:JSONB 已经在那儿,
        # 加列要一次迁移,而这个值的消费者只有一个
        #(`image_set_service` 在入集时缺省继承它)。
        #
        # **Provider 自己报的优先。** Mock 会在 metadata 里回带 `target_angle`,
        # 那是它自己按角度分段的结果;按位置推是给不回带的 Provider 用的兜底。
        # 顺序反过来的话,一家不保序的 Provider 明明说了角度,我们还是按位置
        # 猜一个 —— 而猜错的表现是"明明有背面图却说缺背面"。
        reported = item.metadata.get("target_angle")
        intended = intended_angles[index] if index < len(intended_angles) else None
        target_angle = reported or intended
        if target_angle:
            row.candidate_metadata["target_angle"] = str(target_angle)
        else:
            # 没有方案(或方案没配角度)时**不写这个键**,而不是写 None:
            # 缺席的含义是"这一维不参与",而一个值为 null 的键读起来像
            # "算过了,答案是没有角度"
            row.candidate_metadata.pop("target_angle", None)
        row.error_message = None
        try:
            payload = _load_bytes(item)
            info = probe_image(payload)
            file_hash = hash_bytes(payload)
            blob = storage.save(
                payload, file_hash=file_hash, extension=info.extension, prefix="candidates"
            )
            row.storage_path = blob.storage_path
            row.file_hash = file_hash
            row.mime_type = info.mime_type
            row.file_size = len(payload)
            row.width, row.height = info.width, info.height
            row.status = CandidateStatus.DOWNLOADED.value
        except Exception as exc:  # noqa: BLE001
            row.status = CandidateStatus.DOWNLOAD_FAILED.value
            outcome.download_failed += 1
            # **不落原文**(BLOCK-15)。httpx 的异常原文整条带着下载地址,
            # 而候选图地址是**带签名的**:
            #
            #   HTTPStatusError: Client error '403 Forbidden' for url
            #   'https://.../x.png?X-Amz-Credential=AKIA...&X-Amz-Signature=...'
            #
            # 这一列会经 `GET /api/generation-tasks/{id}` 原样回到浏览器,
            # 再经仪表盘的"最近失败任务"显示给任何能登录的人 —— 等于把一张
            # 未过审的图连同一把有效期内的免鉴权钥匙一起贴出去。
            # 状态码保留:403(签名过期,可重试)和 404(图没了,重试无用)
            # 决定运营下一步做什么。完整原文在日志里,那边是滚动清理的
            row.error_message = safe_error_message(exc)
            logger.warning(
                "candidate download failed",
                exc_info=True,
                extra={
                    "extra_fields": {
                        "event": "gen.candidate_download_failed",
                        "task_id": str(task.id),
                        "index": index,
                    }
                },
            )
        touched.append(row)

    _drop_shrunk_tail(session, task, existing, kept=len(candidates))

    session.flush()

    outcome.stored = [
        row
        for row in sorted(touched, key=lambda r: r.candidate_index)
        if row.status == CandidateStatus.DOWNLOADED.value
    ]

    # 影子写(迁移 A):候选图落成 MediaAsset(source=AI_GENERATED)。
    #
    # 这一步同时把 candidate.media_asset_id 补上,于是"生成任务产出的图"
    # 和"人工上传的图"从这里开始是同一种东西 —— 上架链路不再需要知道
    # 一张图是不是生成出来的(§4.5)。
    #
    # 下载失败的候选不产生素材:它记录的是"要过图但没拿到",那不是一张图。
    #
    # 已经有素材的行跳过:续跑复用的那几张上一次就影子写过了,再写一次会在
    # MediaAsset 上多出一份指向同一个文件的记录
    from app.media import service as media_service

    product = session.get(Product, task.product_id)
    if product is not None:
        for row in outcome.stored:
            if row.media_asset_id is None:
                media_service.shadow_from_candidate(session, product, row)
        session.flush()
    return outcome


def _load_bytes(item) -> bytes:
    """取候选图字节。Mock 直接内联,真实 Provider 走 HTTP 下载。"""
    inline = item.metadata.get("inline_bytes")
    if inline:
        return inline
    if not item.image_url:
        raise ResultDownloadError("候选图既没有内联数据也没有 URL")

    # Provider 返回的地址是**别人的输入**决定我们去访问哪里,先过 SSRF 校验(需求第十九章)
    from app.core.net_safety import (
        UnsafeDownloadURL,
        check_download_url,
        pinned_transport,
        stream_checked,
    )
    from app.providers._config import provider_setting

    # A45-#31:白名单从 `provider_setting` 读,不从 `settings` 直读。
    #
    # `DOWNLOAD_ALLOWED_HOSTS` 是设置页可编辑字段,而 `providers/_config` 的模块
    # 文档写着"全系统读配置只有一个入口……设置页对**所有**调用点同时生效"。
    # 这三行原来直读 `settings`,于是那句承诺在候选图下载这条路径上不成立:
    #
    #     管理员在设置页把自建 ComfyUI 主机加进白名单(页面写着"改完就生效")
    #     → 提交素材可读、评分可取图(那两处走的是 provider_setting)
    #     → 候选图下载被拒,任务停在 DOWNLOADING
    #     → 运营看到的是"白名单明明加了却没用",唯一修法是改 .env 重启后端
    #
    # 一次取值供本函数三处共用:三次调用会读三遍缓存,而它们必须是同一份值 ——
    # 中间被改掉的话,校验用旧值、连接用新值,那是一道自己开的缝
    allowed_hosts = provider_setting("DOWNLOAD_ALLOWED_HOSTS")
    # 候选 metadata 里的 provider 名由适配器写入,不是厂商响应透传。让适配器
    # 只为自己的精确结果域名补充信任,可兼容代理 fake-IP,同时不削弱其他 URL
    # 的 SSRF 校验。未知/缺失 Provider 保持原允许清单,即默认拒绝。
    provider_name = str(item.metadata.get("provider") or "").strip().lower()
    if provider_name:
        try:
            result_provider = get_provider(provider_name)
        except ValidationError:
            pass
        else:
            allowed_hosts = result_provider.result_download_allowed_hosts(
                item.image_url, allowed_hosts
            )

    try:
        check_download_url(item.image_url, allowed_hosts=allowed_hosts)
    except UnsafeDownloadURL as exc:
        raise ResultDownloadError(f"拒绝下载:{exc}") from exc

    import httpx

    # 三层,各管一件事,缺一不可:
    #   check_download_url  早失败、给清楚的错误信息
    #   stream_checked      逐跳跟随重定向,每一跳都重新校验
    #   pinned_transport    连接真正打到已校验的那个 IP —— 这一层才堵住 DNS Rebinding
    # 前两层都是"发请求之前"的检查,而 rebinding 攻击的正是检查和连接之间那道缝。
    with httpx.Client(
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=False,
        transport=pinned_transport(allowed_hosts=allowed_hosts),
    ) as client:
        try:
            response = stream_checked(
                client, item.image_url, allowed_hosts=allowed_hosts
            )
        except UnsafeDownloadURL as exc:
            raise ResultDownloadError(f"拒绝下载:{exc}") from exc
        try:
            response.raise_for_status()
            chunks, total = [], 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_CANDIDATE_BYTES:
                    raise ResultDownloadError("候选图超过下载上限")
                chunks.append(chunk)
        finally:
            # httpx.Response(包括 0.28.x)不是上下文管理器。这里拿到的又是
            # stream=True 的响应,无论状态检查、迭代还是大小上限在哪一步抛错,
            # 都必须显式关闭,否则连接池会被失败候选逐个耗尽。
            response.close()
    return b"".join(chunks)
