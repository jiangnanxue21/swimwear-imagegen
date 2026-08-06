"""兜底维护:投递 Outbox、回收卡死任务、回收批次租约、捞回历史滞留任务。

    python -m app.scripts.requeue_stranded            # 只看,不动
    python -m app.scripts.requeue_stranded --apply    # 真的执行

正常情况下这个脚本**不需要跑** —— Celery beat 里的 `maintenance.relay_dispatches`、
`maintenance.reap_stalled` 和 `maintenance.reap_batch_leases` 已经在做同样的事。
它存在是为了两种场景:

    没有部署 beat 进程(挂 cron 每 5 分钟一次即可)
    出事之后想先看一眼再决定动不动手(不加 --apply 就是只看)

四件事,按可靠性从高到低排:

1. **投递 Outbox**(评审第 3 条)
   任务和"要派发它"这个意图写在同一个事务里,所以这里不需要猜 ——
   库里是 PENDING 的就是真的还没投出去。

2. **回收卡死任务**(评审第 1 条的配套)
   worker 中途消失后任务停在进行中状态,而这些状态没有回 QUEUED 的边,
   界面上既不能重试也不能取消。落成 FAILED 才能重新被操作。

3. **回收批次租约 + 重投批次**(任务 18)
   同样是"worker 中途消失",但对象是批次条目。判据是租约到期,不是启发式 ——
   `lease_until` 是领取时明写进库的一个时刻。回收之后顺带把"还有 PENDING
   却很久没动"的批次重投一次;重投安全的前提是任务 17 的原子领取。

4. **扫描历史滞留任务**(旧的启发式,保留)
   只对 0008 迁移**之前**创建、因而没有 Outbox 记录的存量任务有意义。
   它是启发式的:靠"停在这个状态超过 N 秒"来猜漏投,队列积压时会误判。
   存量清完之后可以加 --skip-legacy-scan 关掉。

重复派发是安全的 —— worker 侧有原子认领(评审第 2 条),抢不到的直接退出。
"""
from __future__ import annotations

import argparse
import sys

from app.core.enums import DISPATCH_ABANDONED_CODE
from app.core.logging import get_logger, setup_logging
from app.db.session import SessionLocal
from app.services import dispatch_service
from app.services import generation_service as gs
from app.workbench import batch_service as bs
from app.workflows.phase_budget import stall_threshold_seconds

logger = get_logger(__name__)


def _expired_lease_count(session) -> int:
    """有多少批次条目的租约过期了。**只读**,给 dry-run 那一档用。

    条件必须与 `reap_expired_leases` 的取数完全一致 —— 两处写岔的表现是
    "只看时说有 3 条,--apply 之后回收了 5 条",而这种不一致恰恰会在
    出事、有人盯着屏幕对数字的时候出现。
    """
    from sqlalchemy import func, select

    from app.core.clock import utc_now
    from app.models.batch_job import BatchJobItem
    from app.workbench.batch import BatchItemStatus

    moment = utc_now()
    return int(
        session.scalar(
            select(func.count())
            .select_from(BatchJobItem)
            .where(
                BatchJobItem.status == BatchItemStatus.RUNNING.value,
                (BatchJobItem.lease_until.is_(None))
                | (BatchJobItem.lease_until <= moment),
            )
        )
        or 0
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="兜底维护:Outbox 投递 / 卡死回收 / 滞留扫描")
    parser.add_argument("--apply", action="store_true", help="真的执行;不加只打印")
    parser.add_argument(
        "--older-than", type=int, default=120,
        help="历史滞留扫描的时间阈值(秒),默认 120",
    )
    parser.add_argument(
        # 默认值按**当前配置**算,不写死(A45-batch12-5 / NEW-03):
        # 评分超时、轮询超时、单次重试次数都能在设置页改,而改大它们
        # 正是最需要阈值跟着变的方向。`--help` 里的那个数会随配置一起变。
        "--stalled-after", type=int, default=stall_threshold_seconds(),
        help="卡死判定阈值(秒),默认按当前配置推导。调低会误杀正在正常跑的任务",
    )
    parser.add_argument(
        "--skip-legacy-scan", action="store_true",
        help="跳过旧的启发式滞留扫描。存量任务清完之后建议加上",
    )
    parser.add_argument(
        "--reactivate", action="store_true",
        help="把已放弃的派发记录重新排队。确认 Broker 已恢复后再用",
    )
    args = parser.parse_args()

    setup_logging("INFO")
    session = SessionLocal()
    try:
        problems = 0

        # ---- 1. Outbox ----
        health = dispatch_service.queue_health(session)
        print(f"Outbox 待投递:{health['pending']},已放弃:{health['abandoned']}")
        if health["pending"] and args.apply:
            stats = dispatch_service.relay_once(session, limit=500)
            print(f"  投出 {stats['dispatched']}/{stats['claimed']},失败 {stats['failed']}")
            problems += stats["failed"]
        if health["abandoned"]:
            # 放弃的记录不自动重投:走到这一步说明 Broker 挂了很久,
            # 需要有人确认外部故障已经排除,再用 --reactivate 明确地捞回来
            print(
                f"  有 {health['abandoned']} 条派发已放弃,对应任务已落成 "
                f"{DISPATCH_ABANDONED_CODE};确认 Broker 恢复后加 --reactivate 重新排队"
            )
            problems += health["abandoned"]
            if args.apply and args.reactivate:
                revived = _reactivate_abandoned(session)
                print(f"  已把 {revived} 个滞留任务重新排回流水线")
                problems -= revived

        # ---- 2. 卡死回收 ----
        stalled = gs.find_stalled(session, older_than_seconds=args.stalled_after)
        if stalled:
            print(f"\n卡死任务({args.stalled_after} 秒无心跳):{len(stalled)}")
            for task in stalled:
                print(f"  {task.id}  {task.status:<16} 更新于 {task.updated_at}")
            if args.apply:
                reaped = gs.reap_stalled(session, older_than_seconds=args.stalled_after)
                session.commit()
                print(f"  已回收 {len(reaped)} 个,现在可以在界面上重试")
        else:
            print("\n没有卡死任务")

        # ---- 3. 批次条目租约回收 + 重投(任务 18) ----
        #
        # 与上面两件事的区别在于"只看"这一档:回收要先扫出候选才知道有多少,
        # 而 `reap_expired_leases` 是扫完就改。所以只看模式下**不调它**,
        # 改用一次同条件的计数 —— 让 `--apply` 之前的那一眼是真的不写库
        pending_leases = _expired_lease_count(session)
        if pending_leases:
            print(f"\n租约过期的批次条目:{pending_leases}")
            if args.apply:
                stats = bs.reap_expired_leases(session, limit=500)
                session.commit()
                print(
                    f"  放回队列 {stats['requeued']},交给人 {stats['gave_up']},"
                    f"涉及 {stats['batches']} 个批次"
                )
                # 交给人的那些是真的失败,计入退出码;放回队列的不算 ——
                # 它们马上就会被重新执行
                problems += stats["gave_up"]
        else:
            print("\n没有租约过期的批次条目")

        if args.apply:
            sent = bs.redispatch_stalled_batches(session, limit=100)
            session.commit()
            if sent["candidates"]:
                print(
                    f"  有活没人干的批次 {sent['candidates']} 个:"
                    f"重投成功 {sent['dispatched']},失败 {sent['failed']}"
                )
                problems += sent["failed"]

        # ---- 4. 历史滞留(旧启发式) ----
        if not args.skip_legacy_scan:
            stranded = gs.find_stranded(session, older_than_seconds=args.older_than)
            if stranded:
                print(f"\n疑似滞留的存量任务(0008 之前创建的):{len(stranded)}")
                for task in stranded:
                    flag = "已标记" if task.error_code == gs.DISPATCH_FAILED_CODE else "疑似漏投"
                    print(f"  {task.id}  {task.status:<16} {flag}  更新于 {task.updated_at}")
                if args.apply:
                    requeued = _requeue_legacy(session, stranded)
                    print(f"  重新派发 {requeued}/{len(stranded)} 个")
                    problems += len(stranded) - requeued
            else:
                print("\n没有滞留的存量任务")

        if not args.apply:
            print("\n只是看看。要真的执行,加 --apply")
            return 0
        return 0 if problems == 0 else 1
    finally:
        session.close()


def _reactivate_abandoned(session) -> int:
    """把所有因派发放弃而滞留的任务带回流水线。返回真正捞回来的任务数。

    实际动作全在 `dispatch_service.reactivate()` 里:锁任务 -> 走 retry_task
    做一次合法的状态重试 -> 登记一条新的派发意图,旧的标成 SUPERSEDED。

    以前这里的注释说"任务由界面上的重试或下一次 relay 带回流水线",
    那句话是错的:relay 重投的消息会被 worker 的原子认领挡掉,因为任务
    还停在 FAILED。只拨 Outbox 不动任务状态,等于什么都没做。
    """
    from sqlalchemy import select

    from app.core.enums import DispatchStatus
    from app.models.task_dispatch import TaskDispatch

    task_ids = list(
        session.scalars(
            select(TaskDispatch.task_id)
            .where(TaskDispatch.status == DispatchStatus.ABANDONED.value)
            .distinct()
        )
    )
    revived = 0
    for task_id in task_ids:
        try:
            # 一个任务一个事务:某一条捞不回来不该拖累其余的
            revived += dispatch_service.reactivate(session, task_id, actor="requeue-script")
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception(
                "reactivate failed", extra={"extra_fields": {"task_id": str(task_id)}}
            )
    if revived:
        # 立刻投一次,省掉等下一轮 relay 的时间;投失败也不要紧,意图还在库里
        try:
            dispatch_service.relay_once(session, limit=max(revived, 50))
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("relay after reactivation failed")
    return revived


def _requeue_legacy(session, stranded) -> int:
    """给没有 Outbox 记录的存量任务补一条派发意图,然后投出去。

    补记录而不是直接 delay:这样它们从此和新任务走同一条路径,
    投递失败也有退避和放弃逻辑兜着,不会像以前那样只剩一行日志。
    """
    from app.core.enums import DispatchReason

    requeued = 0
    for task in stranded:
        try:
            dispatch_service.enqueue(session, task.id, reason=DispatchReason.REQUEUE.value)
            task.error_code = None
            task.error_message = None
            session.commit()
            if dispatch_service.deliver_pending(session, task.id):
                requeued += 1
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception(
                "requeue failed", extra={"extra_fields": {"task_id": str(task.id)}}
            )
    return requeued


if __name__ == "__main__":
    sys.exit(main())
