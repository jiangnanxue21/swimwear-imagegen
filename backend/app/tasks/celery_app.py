"""Celery 应用。阶段 1 只提供健康探测任务,生成流水线在阶段 3 接入。"""
from __future__ import annotations

from celery import Celery
from celery.signals import beat_init, worker_process_init

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
    # 远端 broker 曾在任务完成后主动断开消费连接。Kombu 会重连；这里把保活、
    # 超时重试和启动/运行期重连语义显式固定，避免依赖升级后默认值改变。
    # 协议刻意不在这里覆写：redis-py 8.1 的默认 RESP3 已在真实 worker 上跑通，
    # 强制 RESP2 反而会让当前服务端在首次握手返回 server:RedisError。
    broker_transport_options={
        "health_check_interval": 15,
        "socket_keepalive": True,
        "retry_on_timeout": True,
    },
    broker_connection_retry_on_startup=True,
    broker_connection_retry=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    # broker 断线后，Redis 会重新投递尚未 ack 的消息。继续运行原任务会让新旧两个
    # worker 并行执行同一次付费调用；取消旧任务后由租约/checkpoint 路径安全恢复。
    # Celery 6 会把它改成默认值，这里提前显式固定，避免升级时语义暗变。
    worker_cancel_long_running_tasks_on_connection_loss=True,
    worker_prefetch_multiplier=1,
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,
    task_default_queue="default",
)


# ---------------------------------------------------------------- prefork 与连接池(a51)
#
# prefork 的每个子进程都必须**自己**建连接。父进程如果在 fork 之前建过连接,
# 子进程会继承同一个 socket 文件描述符,而两个进程往同一条 PostgreSQL 连接上
# 交替写,表现是随机的 `server closed the connection unexpectedly` /
# `SSL error` / 拿到别人那条查询的结果 —— 全部难以复现,而且只在有并发时出现。
#
# **今天这件事碰巧不会发生**:`app.db.session` 在导入期只构造 engine 对象,
# `create_engine()` 是惰性的,不建连接。也就是说这个防护现在是空转的。
#
# 那为什么还要加:因为"父进程不查库"是一个**没有任何东西在守的隐性前提**。
# 哪天有人在某个 task 模块的导入期读一次配置表、或者在 beat 里加一句启动自检,
# 前提就没了,而症状不会指向这里。dispose() 的代价是零(池是空的就什么都不做),
# 换掉的是一类没有人能靠读代码发现的故障。
@worker_process_init.connect
def _reset_db_pool_after_fork(**_kwargs: object) -> None:
    """子进程起来后丢掉从父进程继承来的连接池。"""
    from app.db.session import engine

    engine.dispose()


# ---------------------------------------------------------------- worker 的日志(a54 修)
#
# ## 上一版:worker 的日志既不是 JSON,也一条都进不了环形
#
# `setup_logging()` 只在 `app/main.py` 的模块顶层调用过一次,而 worker 的
# 入口是 `celery -A app.tasks.celery_app.celery_app worker` —— 它不 import
# `app.main`,全仓也没有任何模块 import 它。于是:
#
#     API 进程    JsonFormatter + RingHandler,日志进环形
#     worker      Celery 自己的默认格式,**环形里一条都没有**
#
# 而"为什么是 Redis 而不是进程内环形"的**全部理由**就是这件事:
# `log_ring.py` 的模块文档和 `docs/LOG-CONSOLE.md` §4.1 都写着「Celery worker
# 与 API 是不同进程,进程内环形会漏掉最有价值的那部分日志(tasks 域 59 个
# 调用点全在 worker 里)」。跨进程的存储上齐了,产日志的那个进程没接上。
#
# 症状是**沉默**的:打开运行日志页,`gen` / `batch` / `publish` 三个域基本空着,
# 而页面不会说"这里少了一个进程的日志",它只会看起来像"这段时间没跑任务"。
# a53 交接里那条自检顺序(先看 `held` 涨不涨)恰好也发现不了 —— API 进程
# 自己在写,`held` 一直在涨。
#
# ## 为什么是 `worker_process_init` 而不是 `setup_logging` 信号
#
# prefork 下每个子进程各自持有一份 logging 配置,而 `RingHandler` 里的 `seq`
# 是**进程内**计数 + 进程标识:在父进程挂一次再 fork,几个子进程会共享同一个
# 进程标识,跟随模式的去重会把不同 worker 的同序号日志当成同一条丢掉。
# 每个子进程自己挂一次,`os.getpid()` 才是各自的。
#
# `celery.signals.setup_logging` 是另一回事:接上它等于告诉 Celery
# "日志我全包了",连它自己的启动横幅和任务生命周期行都要自己接管。这里只要
# root handler 是我们的,不需要接管 Celery 的那一套。
@worker_process_init.connect
def _install_json_logging_in_worker(**_kwargs: object) -> None:
    """worker 子进程自己装一次日志(JsonFormatter + 环形)。"""
    from app.core.logging import setup_logging

    setup_logging(settings.LOG_LEVEL)


@beat_init.connect
def _install_json_logging_in_beat(**_kwargs: object) -> None:
    """beat 同理。它产出的是节拍与投递日志,归 `ops` 域。"""
    from app.core.logging import setup_logging

    setup_logging(settings.LOG_LEVEL)
