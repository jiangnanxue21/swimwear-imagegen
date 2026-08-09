"""派发意图的读写与投递(事务性 Outbox,评审第 3 条)。

三个角色,职责不重叠:

``enqueue()``
    在**业务事务里**插入一条 PENDING 意图。调用方随后正常 commit,
    于是"任务落库"和"打算派发它"要么一起成功,要么一起没发生。

``deliver_pending()``
    业务事务提交**之后**的快路径,立刻试一次 ``.delay()``。
    失败没关系 —— 意图还在库里,relay 会接手。它存在的唯一目的是让正常情况下
    的派发延迟保持在零,而不是等下一次 relay 扫描。

``relay_once()``
    兜底。捞出到期的 PENDING 逐个投递,失败按指数退避重排。

## 为什么允许重复投递

``deliver_pending()`` 投递成功、但标记 DISPATCHED 之前进程挂掉,relay 会再投一次。
我们**不去消除**这个窗口:消除它需要队列和数据库之间的分布式事务,代价极高,
而收益是零 —— worker 侧的原子认领(评审第 2 条)已经保证第二条消息抢不到任务、
直接退出。至少一次投递 + 幂等消费,是这个问题的标准解法。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.enums import (
    DISPATCH_ABANDONED_CODE,
    DispatchReason,
    DispatchStatus,
    TaskStatus,
)
from app.core.logging import get_logger
from app.models.task_dispatch import TaskDispatch
from app.workflows.dispatch_policy import MAX_DISPATCH_ATTEMPTS, backoff_seconds

#: 还在等派发的状态。派发被放弃时,只有停在这两个状态的任务才需要被落成 FAILED。
STRANDED_STATUSES = (TaskStatus.CREATED.value, TaskStatus.REGENERATING.value)

logger = get_logger(__name__)

def _now() -> datetime:
    return utc_now()


def enqueue(
    session: Session,
    task_id: UUID,
    *,
    reason: str = DispatchReason.CREATE.value,
) -> TaskDispatch:
    """登记一条派发意图。**必须在业务事务内调用,由调用方一起提交。**

    这里刻意只 ``flush()`` 不 ``commit()``:一旦这里提交了,就等于把意图
    和业务数据拆成了两个事务,Outbox 也就白做了。
    """
    row = TaskDispatch(
        task_id=task_id,
        reason=DispatchReason(reason).value,
        status=DispatchStatus.PENDING.value,
        attempts=0,
        available_at=_now(),
    )
    session.add(row)
    session.flush()
    return row


def mark_dispatched(session: Session, row: TaskDispatch) -> None:
    row.status = DispatchStatus.DISPATCHED.value
    row.dispatched_at = _now()
    row.last_error = None
    session.flush()


def _fail_orphaned_task(session: Session, task_id: UUID) -> bool:
    """放弃派发之后,把还在等这条消息的业务任务落成 FAILED。

    不落的话任务会**永久**停在 CREATED / REGENERATING:那两个状态在界面上
    既不是\"进行中\"也不是\"失败\",没人会去看它,而 `pending_count()` 只数 PENDING,
    健康检查还一切正常。放弃投递是个终局判断,业务状态必须跟着走完。

    只动仍在等派发的任务。别的状态说明消息其实投出去了(至少一次投递允许重复),
    或者人已经手工处理过 —— 那种情况下把任务改成 FAILED 才是真的错。
    """
    from app.models.generation import GenerationTask

    task = session.get(GenerationTask, task_id)
    if task is None or task.status not in STRANDED_STATUSES:
        return False

    from app.services import generation_service as gs

    gs.transition(
        session,
        task,
        TaskStatus.FAILED,
        error_code=DISPATCH_ABANDONED_CODE,
        error_message=(
            f"派发连续失败 {MAX_DISPATCH_ATTEMPTS} 次已放弃,任务没有进入队列。"
            "请检查 Broker 是否可用,修复后在派发记录里重新激活或直接重试任务"
        ),
    )
    return True


def describe_failure(exc: BaseException) -> str:
    """把一次投递失败压成**可以长期留存**的一句话。

    只保留异常类型,不保留 ``str(exc)``。模型注释一直写着"不塞第三方异常原文",
    但 `_deliver` 实际写进去的是 ``f"{type(exc).__name__}: {exc}"`` ——
    而 Celery / Kombu / redis-py 的连接异常原文里经常整条带着 Broker URL:

        ConnectionError: Error connecting to redis://:hunter2@10.0.0.5:6379/0

    这张表是要长期留存的,而且会出现在维护脚本的输出和运维的终端回滚里。
    密码进去之后再想清掉,得改库。

    需要更多上下文时看日志:那里有完整异常和堆栈,而日志是滚动清理的,
    留存期比这张表短得多 —— 这正是两者该有的区别。
    """
    name = type(exc).__name__
    code = getattr(exc, "code", None)
    if isinstance(code, (int, str)) and str(code).strip():
        # 有些客户端库会给一个稳定的内部错误码,它不含凭据,留着有用
        return f"{name}({str(code)[:32]})"
    return name


def mark_failed(session: Session, row: TaskDispatch, error: str) -> None:
    """记一次投递失败并安排下次重试;超过上限则放弃。

    ``error`` 必须已经是脱敏后的说明 —— 走 `describe_failure()`。
    """
    row.attempts = (row.attempts or 0) + 1
    row.last_error = error[:2000]
    if row.attempts >= MAX_DISPATCH_ATTEMPTS:
        row.status = DispatchStatus.ABANDONED.value
        row.available_at = None
        failed_task = _fail_orphaned_task(session, row.task_id)
        logger.error(
            "dispatch abandoned after repeated failures",
            extra={
                "extra_fields": {
                    "task_id": str(row.task_id),
                    "attempts": row.attempts,
                    "reason": row.reason,
                    "task_marked_failed": failed_task,
                }
            },
        )
    else:
        row.available_at = _now() + timedelta(seconds=backoff_seconds(row.attempts))
    session.flush()


def reactivate(session: Session, task_id: UUID, *, actor: str = "maintenance") -> int:
    """把一个因派发放弃而滞留的任务真正带回流水线。返回本次投出的意图条数。

    ## 以前为什么不管用

    派发放弃时 `_fail_orphaned_task()` 已经把业务任务改成了 **FAILED**。
    旧实现只把 Outbox 记录从 ABANDONED 拨回 PENDING,任务状态一个字没动。
    于是 relay 兴高采烈地重投一次,worker 收到消息、发现任务既不是 QUEUED
    也不是 REGENERATING,原子认领失败,直接跳过 —— 消息投出去了,任务原地
    不动,而脚本注释还写着"任务由下一次 relay 带回流水线"。那句话从来没成立过。

    ## 现在的做法

    在**一个事务**里:锁住任务 -> 走 `retry_task()` 做一次合法的状态重试
    -> 由它产生的新派发意图接手。旧的 ABANDONED 记录只标记成已处理,
    **不再复活**:那些消息对应的是上一次的意图,重新投出去只会让 worker
    再跳过一次,除了污染日志没有任何作用。

    状态转移一律走 `retry_task()`,不在这里自己改 —— 它是唯一懂得
    "该续跑出图还是续跑评分还是重新生成"的地方(评审第 6 条)。
    """
    from app.core.enums import DISPATCH_ABANDONED_CODE, DispatchReason
    from app.services import generation_service as gs

    rows = list(
        session.scalars(
            select(TaskDispatch).where(
                TaskDispatch.task_id == task_id,
                TaskDispatch.status == DispatchStatus.ABANDONED.value,
            )
        )
    )
    if not rows:
        return 0

    # 锁住任务再判断:同一时间可能有人在界面上点重试
    task = gs.get_task(session, task_id, for_update=True)

    # 旧意图一律作废。它们描述的是"上一次打算投递这个任务",而那次已经
    # 被判定放弃了;retry_task 会登记一条新的
    for row in rows:
        row.status = DispatchStatus.SUPERSEDED.value
        row.available_at = None
    session.flush()

    if task.status != TaskStatus.FAILED.value or task.error_code != DISPATCH_ABANDONED_CODE:
        # 任务已经被人处理过了(手工重试、取消、或者干脆跑完了)。
        # 这时候再把它拉回队列是**错的** —— 覆盖掉的是一个人做过的决定。
        logger.info(
            "skipping reactivation: the task is no longer stranded",
            extra={
                "extra_fields": {
                    "task_id": str(task_id),
                    "status": task.status,
                    "error_code": task.error_code,
                }
            },
        )
        return 0

    # `force=True` 在这里是安全的:上面已经确认 error_code 是
    # DISPATCH_ABANDONED_CODE,而 force 的那道闸只拦「提交结果未知」。
    # 对账结论仍然写清楚 —— 审计里的 force 一律要能回答"依据是什么",
    # 系统自己发起的也不例外
    gs.retry_task(
        session,
        task_id,
        actor=actor,
        force=True,
        reconciliation_note=(
            "系统复活:投递被判定放弃(DISPATCH_ABANDONED),"
            "对应消息从未被 worker 认领,不存在重复执行风险"
        ),
    )
    enqueue(session, task_id, reason=DispatchReason.REQUEUE.value)
    session.flush()
    logger.info(
        "stranded task reactivated",
        extra={"extra_fields": {"task_id": str(task_id), "superseded": len(rows)}},
    )
    return 1


def _send(task_id: UUID) -> None:
    """真正把消息投进队列。单独抽出来,测试里可以直接替换。"""
    from app.tasks.generation_tasks import run_generation_task

    run_generation_task.delay(str(task_id))


def send_attribute_extraction(extraction_id: UUID) -> int:
    """投递一个已提交的识别 run。失败留在 QUEUED，beat 下一拍补投。"""
    from app.tasks.attribute_tasks import run_attribute_extraction

    try:
        run_attribute_extraction.delay(str(extraction_id))
    except Exception:  # noqa: BLE001
        # Broker URL 可能含密码，异常原文不进入日志。
        logger.warning(
            "attribute extraction dispatch failed; queued run will be relayed",
            extra={"extra_fields": {"extraction_id": str(extraction_id)}},
        )
        return 0
    return 1


def _deliver(session: Session, row: TaskDispatch, *, send=_send) -> bool:
    try:
        send(row.task_id)
    except Exception as exc:  # noqa: BLE001
        # 不抛。派发失败不该让创建任务的接口 500 —— 任务已经落库,
        # 意图也已经落库,relay 会把它捡起来。
        # 只记类型/内部码,绝不记异常原文:Broker URL 里带着密码
        mark_failed(session, row, describe_failure(exc))
        logger.warning(
            "dispatch attempt failed (%s), will retry from outbox",
            type(exc).__name__,
            extra={
                "extra_fields": {
                    "task_id": str(row.task_id),
                    "attempts": row.attempts,
                    "next_try_at": str(row.available_at),
                }
            },
        )
        return False
    mark_dispatched(session, row)
    return True


def deliver_pending(session: Session, task_id: UUID, *, send=_send) -> bool:
    """业务事务提交后的快路径:立刻投递这个任务所有待投的意图。

    调用前调用方**必须已经 commit** —— 否则 worker 可能抢在提交之前
    读到一个还不存在的任务。
    """
    rows = list(
        session.scalars(
            select(TaskDispatch).where(
                TaskDispatch.task_id == task_id,
                TaskDispatch.status == DispatchStatus.PENDING.value,
            )
        )
    )
    if not rows:
        return False
    delivered = all(_deliver(session, row, send=send) for row in rows)
    session.commit()
    return delivered


def claim_batch(session: Session, *, limit: int = 50) -> list[TaskDispatch]:
    """取一批到期的待投意图,并锁住它们。

    ``FOR UPDATE SKIP LOCKED`` 是关键:多个 relay 实例同时跑时,
    各自拿到互不相交的一批,而不是排队等同一批 —— 后者会让 relay 的
    吞吐等于单实例,横向扩容毫无意义。
    """
    stmt = (
        select(TaskDispatch)
        .where(
            TaskDispatch.status == DispatchStatus.PENDING.value,
            TaskDispatch.available_at <= _now(),
        )
        .order_by(TaskDispatch.available_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(stmt))


def relay_idle() -> dict[str, int]:
    """一拍什么都没做时的统计。**形状的唯一出处。**

    `maintenance_tasks.relay_dispatches` 的异常分支也用它。原来那边手抄了
    一份 `{"claimed": 0, "dispatched": 0, "failed": 0, "error": True}` ——
    今天键名恰好对得上,但它是**抄**的:这里将来加一个计数,错误路径
    不会跟着变,而读这个返回做看板的一侧只在出错时才会发现少了一个键。
    §7.8 最后一条禁止项要防的就是这个 —— 异常不许把返回形状也换掉。

    写成函数而不是模块常量,是因为它会被 `{**relay_idle(), ...}` 展开:
    返回可变字典的常量,调用方一旦就地改一下,下一次调用拿到的就是被改过的那份。
    """
    return {"claimed": 0, "dispatched": 0, "failed": 0}


def relay_once(session: Session, *, limit: int = 50, send=_send) -> dict[str, int]:
    """兜底扫描:把到期的待投意图投出去。返回本次统计。"""
    rows = claim_batch(session, limit=limit)
    if not rows:
        session.commit()
        return relay_idle()

    dispatched = sum(1 for row in rows if _deliver(session, row, send=send))
    session.commit()
    stats = {
        "claimed": len(rows),
        "dispatched": dispatched,
        "failed": len(rows) - dispatched,
    }
    logger.info("dispatch relay pass", extra={"extra_fields": stats})
    return stats


def send_batch(batch_id: str, actor: str) -> bool:
    """把一个批次投给 worker(评审第 2 条)。

    ## 为什么它在这里,而不是在 API 里直接 `.delay()`

    全仓只允许这一个模块碰队列(`test_only_dispatch_service_talks_to_the_queue`
    钉着)。多一个入口就多一条绕过登记的路径,而这种绕过只会表现为
    "偶尔有任务莫名其妙不跑"。

    ## 为什么它不写 TaskDispatch

    Outbox 表解决的是"业务行落库了、消息没发出去"。批次没有这个问题:
    它的**条目本身就是意图记录** —— `batch_job_items` 里那一批 PENDING
    行在业务事务里落库,投递失败时它们照样在,批次详情页上看得见。
    换句话说批次自带 outbox,再叠一层 `TaskDispatch` 只是把同一件事记两遍。

    投不出去之后由谁来补:A35 起是 `batch_service.redispatch_stalled_batches()`
    (beat 每 60 秒一拍,经本函数重投),运营点重试退化成兜底。
    在那之前只有人工点重试 —— 那正是这一条曾经的短板,
    补法与理由见 `docs/DECISIONS.md` §3.12。

    返回是否投递成功。**失败不抛**:批次已经提交,投不出去的后果是
    "没人来跑",而不是"创建失败"。抛出去的话运营会再建一个批次,
    库里就有了两个同样的批次。
    """
    try:
        from app.tasks.batch_tasks import run_batch_task

        run_batch_task.delay(batch_id, actor)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "batch dispatch failed; items stay PENDING",
            extra={
                "extra_fields": {
                    "batch_id": batch_id,
                    "error": describe_failure(exc),
                }
            },
        )
        return False


def _count(session: Session, status: str) -> int:
    from sqlalchemy import func

    return (
        session.scalar(
            select(func.count()).select_from(TaskDispatch).where(TaskDispatch.status == status)
        )
        or 0
    )


def pending_count(session: Session) -> int:
    """待投数量。给健康检查和看板用 —— 这个数持续上涨就是 Broker 出问题了。"""
    return _count(session, DispatchStatus.PENDING.value)


def abandoned_count(session: Session) -> int:
    """已放弃投递的数量。

    必须和 pending 一起报:放弃之后记录就**离开** PENDING 了,只看 pending
    的话 Broker 挂到重试耗尽之后,曲线会漂亮地回落到零,而实际情况是
    一批任务再也没人管。健康检查看的是这两个数的组合。
    """
    return _count(session, DispatchStatus.ABANDONED.value)


def queue_health(session: Session) -> dict[str, int]:
    """给健康检查和维护脚本的一次性快照。"""
    return {
        "pending": pending_count(session),
        "abandoned": abandoned_count(session),
    }
