"""发布链路的周期性任务(任务 14 的投递 + 任务 15 的轮询)。

两件事都必须是自动的,理由同 `maintenance_tasks.py`:它们的失效模式是
**没人盯着**。一份写在部署文档里、指望运维记得挂 cron 的脚本,在出事那天
一定没在跑。

Celery beat 要单独起进程:

    celery -A app.tasks.celery_app beat -l info

没起 beat 也不至于完全失效:`publish_service.run_due` 与 `poll_service.poll_due`
都是纯函数式的批处理入口,可以退回挂 cron 调用。

## 为什么两个任务都吞异常并返回统计,而不是让 Celery 重试

Celery 的自动重试会把**整批**重跑一遍。投递和轮询都已经在自己内部按件处理、
按件保存结果了(每件一个事务),整批重跑等于把已经成功的那些再发一次 ——
对投递来说那是给平台多发一次请求,正是幂等键要防的事情。所以这一层只负责
把异常变成一条可观测的日志,重试交给 outbox 的 `next_attempt_at` 与 listing 的
`next_poll_at`,它们本来就是为这件事存在的。
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.tasks.celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(name="publish.deliver_outbox")
def deliver_outbox(limit: int = 20) -> dict:
    """把 `publish_outbox` 里到期的提交发给平台(任务 14)。

    `run_due` 要的是**会话工厂**不是会话:它内部分三段事务,每段自己开自己关。
    传一个已经开好的会话进去,第一段 commit 之后后面两段就在一个已提交的
    会话上继续写,而那正是 7.8 节要拆开的东西。
    """
    from app.services import publish_service

    try:
        return publish_service.run_due(SessionLocal, limit=limit)
    except Exception:  # noqa: BLE001 - 见模块文档
        logger.exception("publish outbox delivery failed", extra={
            "extra_fields": {
                "event": "publish.outbox_delivery_failed",
            }
        })
        return {"claimed": 0, "succeeded": 0, "retry": 0, "failed": 0, "error": True}


@celery_app.task(name="publish.poll_listings")
def poll_listings(limit: int = 50) -> dict:
    """问平台那些还没有结论的商品现在怎么样了(任务 15)。"""
    from app.services import poll_service

    try:
        return poll_service.poll_due(SessionLocal, limit=limit)
    except Exception:  # noqa: BLE001 - 见模块文档
        logger.exception("publish status polling failed", extra={
            "extra_fields": {
                "event": "publish.poll_failed",
            }
        })
        return {"claimed": 0, "changed": 0, "unchanged": 0, "error": True}
