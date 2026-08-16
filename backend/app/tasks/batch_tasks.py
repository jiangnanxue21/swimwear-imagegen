"""批量执行的异步入口(评审第 2 条)。

## 为什么批次要离开 HTTP 请求

`POST /workbench/batches` 原来默认 `run=true`,在同一个请求、同一个事务里
把 50 件全部跑完。三件事因此绑在一起:

    超时         50 次模型调用挤在一个请求里,网关先断,人看到的是"失败"
    计费与回滚   请求断开时外部调用可能已经计费,而事务整个回滚 ——
                 钱花了,回执一行没留
    锁占用       advisory lock 一直持有到最后一件跑完

拆开之后:接口只建批次、立刻提交、返回 `batch_id`;这个任务领走执行,
**每件一个独立事务**(`run_batch(commit_each=True)`);前端轮询状态。

## 判定层一行没动

`run_batch` 原样搬进来,不是复制过来。批量的业务判定只有 `batch.py`
那一份纯函数 —— 同步跑和异步跑必须给出同一个结论,否则"验收时用同步跑
通过了"这句话不成立。

## 重复投递是安全的

派发走 `dispatch_service` 的 Outbox,至少一次投递。第二条消息进来时,
`run_batch` 只捞 PENDING 条目 —— 已经跑完的不在里面;真的撞上并发执行,
`_execute` 里的 advisory lock 会让后来者拿不到锁、报可重试的失败,
**不会重复调用付费模型**。
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.exc import OperationalError

from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.tasks.celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(
    name="workbench.run_batch",
    bind=True,
    # **只对基础设施故障重试,业务失败一次都不重试。**
    #
    # 这个区分与 `generation_tasks.run_generation_task` 完全一致,理由见下。
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def run_batch_task(self, batch_id: str, actor: str = "system") -> dict:  # noqa: ARG001
    """执行一个批次里所有 PENDING 条目。

    ## 业务失败不重试(原来的 `max_retries=0`,理由不变)

    失败的**单件**已经在批次明细里带着异常码和重试入口(FE-304),由运营
    决定要不要重跑。让 celery 自动整批重试等于绕过那个决定,而且每一次重试
    都可能是一次真金白银的模型调用。

    业务异常仍然走下面的 `except Exception` —— 记日志、返回、**不重投**。
    `max_retries=3` 只对 `autoretry_for` 里那一个异常生效。

    ## 数据库不可用要重试(新增,与 `run_generation_task` 同一条理由)

    `task_acks_late=True` 的语义是"执行完成后 ack",而**正常返回也算完成**。
    原来这里 `except Exception` 把 `OperationalError` 一起吞了、照常返回 ——
    消息就此消失。而那一刻:

        已领取的条目停在 RUNNING,带着 1800 秒的租约
        `reap_batch_leases` 要等租约**到期**才回收(节拍 60 秒,但到不到期
        由 `lease_until` 决定)
        -> 最长半小时里,这个批次在界面上看起来是活的

    与生成任务那边一字不差的形状,那边修了,这边漏了。

    **重投是安全的**,理由写在模块头:`run_batch` 只捞 PENDING 条目,
    真撞上并发执行时 `_execute` 的 advisory lock 会让后来者拿不到锁,
    不会重复调用付费模型。
    """
    session = SessionLocal()
    try:
        from app.workbench import batch_service as bs

        job = bs.get_job(session, UUID(batch_id))
        counts = bs.run_batch(session, job, actor=actor, commit_each=True)
        logger.info(
            "async batch run finished",
            extra={
                "extra_fields": {"event": "batch.async_finished",
                    "batch_id": batch_id,
                    "succeeded": counts.succeeded,
                    "failed": counts.failed,
                    "paid_calls": counts.paid_calls,
                }
            },
        )
        return {
            "batch_id": batch_id,
            "succeeded": counts.succeeded,
            "failed": counts.failed,
            "needs_human": counts.needs_human,
            "paid_calls": counts.paid_calls,
        }
    except OperationalError:
        # **必须排在 `except Exception` 前面,并且原样抛出去。**
        #
        # 被下面那个分支接住的话,`autoretry_for` 永远不会触发 —— 装饰器只能
        # 看见抛出函数的异常。这不是防御性写法,是这条修复的**全部内容**:
        # 装饰器改了而这里不改,等于什么都没做。
        session.rollback()
        logger.error(
            "database unavailable during batch run; will retry",
            extra={"extra_fields": {"event": "batch.db_unavailable", "batch_id": batch_id}},
        )
        raise
    except Exception:  # noqa: BLE001
        # 每件已经各自提交过,这里回滚的只是最后一段没提交的部分。
        # 已经跑完的条目和回执留在库里 —— 那正是拆事务的目的
        session.rollback()
        logger.exception("batch run failed", extra={
            "extra_fields": {
                "event": "batch.run_failed",
                "batch_id": batch_id,
            }
        })
        return {"batch_id": batch_id, "error": True}
    finally:
        session.close()
