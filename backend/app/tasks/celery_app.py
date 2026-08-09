"""Celery 应用。阶段 1 只提供健康探测任务,生成流水线在阶段 3 接入。"""
from __future__ import annotations

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    settings.APP_NAME,
    broker=settings.broker_url,
    backend=settings.result_backend,
    include=[
        "app.tasks.attribute_tasks",
        "app.tasks.batch_tasks",
        "app.tasks.health_tasks",
        "app.tasks.generation_tasks",
        "app.tasks.maintenance_tasks",
        "app.tasks.publish_tasks",
    ],
)

#: 兜底任务的节拍。要起 beat 进程才生效:
#:     celery -A app.tasks.celery_app beat -l info
#: 没起 beat 时退回 `make requeue`(定时跑 app.scripts.requeue_stranded)。
# ## 为什么下面这些任务**不**配 `autoretry_for=(OperationalError,)`
#
# 因为**下一拍就是它们的重试**。
#
# 用户触发的任务(生成、批量)一旦因为库不可用而失败,消息在 `acks_late` 下
# 照样被 ack —— 没人会再叫它一次,那件事就停在半路。所以那两个必须自己重试。
#
# 节拍任务不同:它们本来就每 30 / 60 / 300 秒被叫一次,失败一拍的代价是
# "晚一拍处理",而 `autoretry` 会在已有节拍之上再叠一层重试队列 ——
# 库恢复的那一刻会有一批堆积的重试和一次正常节拍同时到达。
#
# `tests/pure/test_a45_batch9_fixes.py` 把这条豁免钉成规则:
# **要么自己重试,要么在 beat 里**,两者都没有的任务会红。
celery_app.conf.beat_schedule = {
    "relay-attribute-extractions": {
        "task": "maintenance.relay_attribute_extractions",
        "schedule": 30.0,
    },
    "reap-attribute-extractions": {
        "task": "maintenance.reap_attribute_extractions",
        "schedule": 300.0,
    },
    # Outbox relay:漏投的任务最多等这么久就会被投出去。
    # 30 秒是"恢复够快"和"空扫描够便宜"之间的折中 —— 空跑只是一次带索引的 SELECT。
    "relay-dispatches": {
        "task": "maintenance.relay_dispatches",
        "schedule": 30.0,
    },
    # 卡死回收跑得稀疏一些:它的判定依赖"多久没动过",本来就不需要高频。
    "reap-stalled-tasks": {
        "task": "maintenance.reap_stalled",
        "schedule": 300.0,
    },
    # 批次租约回收 + 重投(任务 18)。60 秒一拍。
    #
    # 节拍与租约时长(`batch.ITEM_LEASE_SECONDS` = 1800)的关系值得写明:
    # 拍得再密也不会提前回收 —— 到不到期由 `lease_until` 决定,这里只决定
    # **到期之后多久被发现**。所以取一个"人还没来得及刷新页面"的量级即可,
    # 空跑的代价是一次带索引的 SELECT(`ix_batch_job_items_lease`)。
    "reap-batch-leases": {
        "task": "maintenance.reap_batch_leases",
        "schedule": 60.0,
    },
    # 过期草稿落库(评审第 19 条)。只读接口不再顺手写 STALE 之后,
    # 这条是"没人打开页面时谁来落库"的答案。10 分钟够快 ——
    # 导出前的 `export_gate` 本来就会当场再判一次,这里只是让列表页上的
    # 状态不至于长期停在旧结论上。
    "refresh-stale-drafts": {
        "task": "maintenance.refresh_stale_drafts",
        "schedule": 600.0,
    },
    # 发布 Outbox 投递(任务 14)。15 秒比 relay 更勤,因为这一条直接决定
    # 运营点完"提交"之后多久能看到结果 —— 而空跑同样只是一次带索引的 SELECT。
    "deliver-publish-outbox": {
        "task": "publish.deliver_outbox",
        "schedule": 15.0,
    },
    # 平台状态轮询(任务 15)。节拍取 20 秒,与 `poll_policy` 的最小轮询间隔
    # 对齐:beat 拍得比最小间隔还密没有意义 —— 每行到不到期由 `next_poll_at`
    # 决定,多出来的那几拍全部是空扫描。反过来拍得比它稀,那个最小间隔就
    # 名存实亡,退避曲线的头几档等于没配。
    "poll-publish-listings": {
        "task": "publish.poll_listings",
        "schedule": 20.0,
    },
}

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,
    task_default_queue="default",
)
