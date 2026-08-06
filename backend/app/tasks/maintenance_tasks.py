"""周期性兜底任务:Outbox relay 与卡死任务回收。

两件事都属于"正常路径没跑通时,由谁来补"。放在一起是因为它们的失效模式相同 ——
**没人盯着**。所以两者都必须是自动的、可观测的,而不是一份写在部署文档里、
指望运维记得挂 cron 的脚本。

Celery beat 需要单独起进程:

    celery -A app.tasks.celery_app beat -l info

没起 beat 也不至于失效:``python -m app.scripts.requeue_stranded --apply``
做的是同样的事,可以退回挂 cron。
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.tasks.celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(name="maintenance.relay_dispatches")
def relay_dispatches(limit: int = 100) -> dict:
    """把 Outbox 里到期的派发意图投进队列(评审第 3 条)。

    这是"任务落库了但消息没发出去"的唯一兜底。跑得越勤,漏投的恢复越快;
    空跑的代价只是一次带索引的 SELECT。
    """
    from app.services import dispatch_service

    session = SessionLocal()
    try:
        return dispatch_service.relay_once(session, limit=limit)
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("dispatch relay failed")
        # 形状从 `relay_once` 那边取,不在这里手抄一份(§7.8 第四条)。
        # 成功路径将来多一个计数时,错误路径会自动跟上
        return {**dispatch_service.relay_idle(), "error": True}
    finally:
        session.close()


@celery_app.task(name="maintenance.reap_stalled")
def reap_stalled(older_than_seconds: int | None = None) -> dict:
    """把 worker 中途消失、卡在进行中状态的任务落成 FAILED(评审第 1 条的配套)。

    拆短事务之后,worker 被 kill 时任务会**停在它当时走到的那个阶段**,
    而不是回滚到上一次提交。这是我们想要的 —— 现场保住了 —— 但代价是
    状态机里 PROVIDER_RUNNING / SCORING 这些进行中状态没有回 QUEUED 的边,
    人工在界面上既不能重试也不能取消。必须有东西把它们收回来。

    ## 阈值不再写死(A45-batch12-5 / NEW-03)

    默认参数原来是 `= STALL_THRESHOLD_SECONDS`,而 Python 的默认参数在
    **import 那一刻**求值 —— 运营在设置页把评分超时或轮询超时调大之后,
    这个 worker 进程会一直按旧值回收,直到有人重启它。而"调大超时"正好是
    让合法耗时变长、最需要阈值跟着变的那个方向。

    传 None(默认)时由 `gs.reap_stalled` 按**当前配置**算,推导过程见
    `workflows/phase_budget.py`。显式传值仍然有效,给运维脚本留着。
    """
    from app.services import generation_service as gs

    session = SessionLocal()
    try:
        reaped = gs.reap_stalled(session, older_than_seconds=older_than_seconds)
        session.commit()
        if reaped:
            logger.warning(
                "reaped stalled tasks",
                extra={"extra_fields": {"count": len(reaped), "task_ids": reaped[:20]}},
            )
        return {"reaped": len(reaped)}
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("stalled task reaper failed")
        return {"reaped": 0, "error": True}
    finally:
        session.close()


@celery_app.task(name="maintenance.reap_batch_leases")
def reap_batch_leases(limit: int = 200, redispatch_limit: int = 20) -> dict:
    """回收租约过期的批次条目,并把「有活没人干」的批次重投一次(任务 18)。

    两件事放在同一个任务里,因为它们必须**按这个顺序**发生:回收先把停在
    RUNNING 的残骸放回 PENDING,重投才知道那个批次还有活。反过来的话,
    重投看到的是一个"没有 PENDING 条目"的批次,于是不投;等回收器下一拍
    把条目放回去时,重投已经跑完了 —— 那些条目要再等一整拍。

    合成一个任务而不是让 beat 排两条,就是为了让这个顺序**不依赖调度**。

    没起 beat 时退回 `python -m app.scripts.requeue_stranded --apply`,
    那边做的是同一件事。
    """
    from app.workbench import batch_service as bs

    #: 两个阶段各自的零值。**异常分支要在这个基础上报,不能凭空写一串 0** ——
    #: 回收先跑且**自己提交过**,所以重投炸掉时"这一拍回收了几件"是一个
    #: 已经落库的事实。原来那个 `{"scanned": 0, ...}` 会把它抹掉:监控侧看到
    #: 一拍"什么都没回收",而库里刚放回去 200 件。方向反了 —— 兜底任务
    #: 报少不报多是安全的,报假的零不是。
    #:
    #: 键集也必须两个分支一致(§7.8 第四条:异常不许把返回形状也换掉)。
    #: 少了 `batches` 与三个 `redispatch_*`,读这个返回做看板的一侧
    #: 在错误路径上会拿到另一种形状,而那正是最需要它稳定的时候。
    reaped: dict[str, int] = {"scanned": 0, "requeued": 0, "gave_up": 0, "batches": 0}
    sent: dict[str, int] = {"candidates": 0, "dispatched": 0, "failed": 0}

    def _out(**extra: bool) -> dict:
        return {
            **reaped,
            **{f"redispatch_{k}": v for k, v in sent.items()},
            **extra,
        }

    session = SessionLocal()
    try:
        settled = bs.reap_expired_leases(session, limit=limit)
        session.commit()
        # 赋值放在提交**之后**:`reaped` 的含义是"已经落库的回收数"。
        # 写成 `reaped = reap(...)` 再 commit 的话,commit 自己抛异常时
        # 这个变量已经是非零了,异常分支会报出一批实际被回滚掉的回收 ——
        # 那就从"报假的零"翻到了"报假的非零",同样是撒谎,只是方向反过来
        reaped = settled
        sent = bs.redispatch_stalled_batches(session, limit=redispatch_limit)
        session.commit()
        return _out()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("batch lease reaper failed")
        # 回滚只撤得掉**还没提交**的那一段。`reaped` 若已赋值就说明它那次
        # 提交成功了,如实带出去
        return _out(error=True)
    finally:
        session.close()


@celery_app.task(name="maintenance.refresh_stale_drafts")
def refresh_stale_drafts(limit: int = 200) -> dict:
    """把过期草稿的 STALE 结论落库(评审第 19 条的另一半)。

    ## 为什么需要它

    第 19 条把列表、详情、异常、SPU、过期原因五个 GET 改成了只读:
    它们照常判定并显示"已过期",但不再写库。这本身是对的 ——
    浏览器预取一次就改一次业务状态,是个不该存在的副作用。

    但「过期草稿一律禁止导出」(§4.5.1)是**靠落库的状态**执行的,
    不能只存在于某次请求的内存里。所以结论仍然要有人写,只是写的时机
    从"谁碰巧打开了页面"换成三条明确的路径:

        上游写入        改图片集 / 改文案 / 改属性时顺手刷新
        显式刷新动作    运营点"重新生成草稿"
        这个任务        兜底。定期扫一遍,把漏网的落库

    判定逻辑仍然是 `service.refresh_draft` 那一份(P4 的约束:过期口径
    只有一处)。这里只是把它跑起来,并且**不 dry-run** —— 这是唯一
    一条以落库为目的的路径。

    `limit` 限制单次扫描量:一次扫全表会让这个兜底任务自己变成长事务,
    而它的存在恰恰是为了避免别处出现长事务。
    """
    from sqlalchemy import select

    from app.core.enums import DraftStatus
    from app.models.listing_copy import ListingDraft
    from app.models.product import Product
    from app.workbench import service as wb

    session = SessionLocal()
    scanned = marked = 0
    try:
        rows = list(
            session.scalars(
                select(ListingDraft)
                .where(
                    ListingDraft.status.notin_(
                        [DraftStatus.ARCHIVED.value, DraftStatus.STALE.value]
                    )
                )
                .order_by(ListingDraft.updated_at.asc())
                .limit(limit)
            )
        )
        for row in rows:
            product = session.scalars(
                select(Product).where(Product.spu == row.spu).limit(1)
            ).first()
            if product is None:
                continue
            scanned += 1
            # 每件一个保存点:一件商品的上游数据坏掉,不该让整轮扫描
            # 停在那里,后面的草稿永远等不到刷新
            savepoint = session.begin_nested()
            try:
                is_stale, _ = wb.refresh_draft(session, product, row)
                savepoint.commit()
                marked += int(is_stale)
            except Exception:  # noqa: BLE001
                savepoint.rollback()
                logger.warning(
                    "stale refresh failed for one draft",
                    extra={"extra_fields": {"draft_id": str(row.id)}},
                )
        session.commit()
        return {"scanned": scanned, "marked_stale": marked}
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("stale draft refresh failed")
        return {"scanned": scanned, "marked_stale": marked, "error": True}
    finally:
        session.close()
