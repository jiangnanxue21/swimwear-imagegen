"""人工审核队列(需求第十二、十四、十六章)。

审核对象是**商品任务**,不是每一张低分候选图。一个任务在队列里最多一条待办,
这条约束由数据库的部分唯一索引兜底,应用层只做友好处理。

阻塞与非阻塞两种审核项:
- 阻塞项(轮次耗尽等):任务停在 MANUAL_REVIEW,等人决定;
- 非阻塞项(A 档随机抽检):任务照常往下走,审核项只是事后复核。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.enums import (
    NON_BLOCKING_REVIEW_REASONS,
    AuditAction,
    CandidateStatus,
    ProductStatus,
    ReviewReason,
    ReviewStatus,
    TaskStatus,
)
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.sorting import normalize_sort
from app.models.evaluation import CandidateEvaluation, ReviewItem
from app.models.generation import GenerationAttempt, GenerationCandidate, GenerationTask
from app.models.product import Product
from app.services import audit
from app.services import generation_service as gs
from app.services.sorting import apply_order
from app.workflows import state_machine as sm

logger = get_logger(__name__)

#: 累计轮次的兜底上限,配置读不到时用它
DEFAULT_MAX_TOTAL_ROUNDS = 10


def _now() -> datetime:
    return utc_now()


def _max_total_rounds() -> int:
    """一个任务允许累计跑多少轮(含人工追加)。

    做成配置而不是常量:不同类目的难度差别很大,泳装比纯色 T 恤难得多,
    运营需要能调这个数,但**必须有一个数**。
    """
    try:
        from app.providers._config import provider_int

        # 走 provider_int 而不是直接读 Settings,后台设置页改完才能不重启就生效
        return max(1, provider_int("MAX_TOTAL_ROUNDS", DEFAULT_MAX_TOTAL_ROUNDS))
    except Exception:  # noqa: BLE001
        return DEFAULT_MAX_TOTAL_ROUNDS


def get_review(
    session: Session, review_id: UUID, *, for_update: bool = False
) -> ReviewItem:
    """取审核项。``for_update=True`` 时锁住这一行。

    ``_assert_pending()`` 是一次典型的"检查后使用":读到 PENDING、决定往下走、
    再改成 APPROVED。两个审核员同时点"通过"和"驳回"时,两边都会读到 PENDING,
    于是两边都往下走 —— 审核项被关闭两次,任务被转移两次,而其中一次的
    结论没有任何地方留下痕迹。

    加锁的顺序是**先审核项、后任务**,三个决策函数都一样。反过来其中一个的话,
    两个并发决策会各持一把互相等对方,直接死锁。
    """
    if for_update:
        # 先锁再取:session.get 命中身份映射时不发 SQL,锁就没加上
        session.execute(
            select(ReviewItem.id).where(ReviewItem.id == review_id).with_for_update()
        ).one_or_none()
    item = session.get(ReviewItem, review_id)
    if item is None:
        raise NotFoundError("审核项不存在")
    if for_update:
        session.refresh(item)
    return item


def find_open_review(session: Session, task_id: UUID) -> ReviewItem | None:
    return session.scalar(
        select(ReviewItem).where(
            ReviewItem.task_id == task_id,
            ReviewItem.status == ReviewStatus.PENDING.value,
        )
    )


def _round_summary(session: Session, task: GenerationTask) -> dict[str, Any]:
    """汇总任务历轮情况,写进审核项,省得审核页每次都重算。"""
    evaluations = list(
        session.scalars(
            select(CandidateEvaluation).where(CandidateEvaluation.task_id == task.id)
        )
    )
    attempts = session.scalar(
        select(func.count()).select_from(
            select(GenerationAttempt).where(GenerationAttempt.task_id == task.id).subquery()
        )
    )
    best: CandidateEvaluation | None = None
    for row in evaluations:
        if best is None or float(row.overall_score) > float(best.overall_score):
            best = row

    hard_codes = sorted({c for row in evaluations for c in (row.hard_fail_codes or [])})
    return {
        "best_candidate_id": best.candidate_id if best else None,
        "best_score": float(best.overall_score) if best else None,
        "best_grade": best.grade if best else None,
        "hard_fail_codes": hard_codes,
        "attempt_count": int(attempts or 0),
        "evaluation_count": len(evaluations),
    }


def open_review(
    session: Session,
    task: GenerationTask,
    *,
    reason: ReviewReason,
    actor: str = "system",
    summary: str | None = None,
) -> ReviewItem:
    """把任务放进审核队列。幂等:已有待办时更新它,不再建第二条。"""
    stats = _round_summary(session, task)
    blocking = reason not in NON_BLOCKING_REVIEW_REASONS

    existing = find_open_review(session, task.id)
    if existing is not None:
        existing.reason = reason.value
        existing.round_count = task.current_round
        existing.provider = task.provider
        existing.summary = summary or existing.summary
        for field, value in stats.items():
            if hasattr(existing, field):
                setattr(existing, field, value)
        session.flush()
        return existing

    item = ReviewItem(
        task_id=task.id,
        product_id=task.product_id,
        reason=reason.value,
        status=ReviewStatus.PENDING.value,
        blocking=blocking,
        provider=task.provider,
        round_count=task.current_round,
        attempt_count=stats["attempt_count"],
        best_candidate_id=stats["best_candidate_id"],
        best_score=stats["best_score"],
        best_grade=stats["best_grade"],
        hard_fail_codes=stats["hard_fail_codes"],
        summary=summary,
    )
    session.add(item)
    session.flush()

    # 商品状态跟着走,商品列表页才能一眼看出"这个 SKU 卡在人工审核"
    if blocking:
        product = session.get(Product, task.product_id)
        if product is not None and product.status != ProductStatus.ARCHIVED.value:
            product.status = ProductStatus.NEEDS_REVIEW.value

    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="ReviewItem",
        entity_id=item.id,
        payload={"task_id": str(task.id), "reason": reason.value, "blocking": blocking},
    )
    return item


#: 审核队列允许排序的字段。
#:
#: `best_score` 是这张表上最值钱的一个排序维度,也是唯一可空的一个:轮次耗尽但一张
#: 候选图都没跑出来的项,分数是 NULL。它不是「0 分」——「没有分」和「分很低」在这里
#: 是两回事,所以 apply_order 把空值压到最后,两个方向都是。
REVIEW_SORTABLE = {
    "created_at": ReviewItem.created_at,
    "updated_at": ReviewItem.updated_at,
    "best_score": ReviewItem.best_score,
    "round_count": ReviewItem.round_count,
    "attempt_count": ReviewItem.attempt_count,
    "reason": ReviewItem.reason,
    "status": ReviewItem.status,
}


def list_reviews(
    session: Session,
    *,
    status: str | None = None,
    reason: str | None = None,
    product_id: UUID | None = None,
    sort: str | None = None,
    order: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[ReviewItem], int]:
    sort_field, direction = normalize_sort(
        sort, order, allowed=REVIEW_SORTABLE, default_sort="created_at", default_order="desc"
    )
    stmt = select(ReviewItem)
    if status:
        stmt = stmt.where(ReviewItem.status == status)
    if reason:
        stmt = stmt.where(ReviewItem.reason == reason)
    if product_id:
        stmt = stmt.where(ReviewItem.product_id == product_id)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    ordered = apply_order(
        stmt, columns=REVIEW_SORTABLE, sort=sort_field, order=direction, tiebreak=ReviewItem.id
    )
    rows = list(session.scalars(ordered.offset(offset).limit(limit)))
    return rows, total


def _assert_pending(item: ReviewItem) -> None:
    if item.status != ReviewStatus.PENDING.value:
        raise ValidationError(
            f"该审核项已处理({item.status}),不能重复决策",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )


def _close(
    item: ReviewItem, status: ReviewStatus, *, actor: str, note: str | None, tags: list[str] | None
) -> None:
    item.status = status.value
    item.decided_by = actor
    item.decision_note = (note or "")[:2000] or None
    if tags:
        item.manual_tags = sorted({*(item.manual_tags or []), *tags})
    item.decided_at = _now()


def _clear_other_selections(session: Session, task_id, *, keep) -> None:
    """同一任务里除 keep 之外的候选一律取消 SELECTED。"""
    for candidate in gs.list_candidates(session, task_id):
        if candidate.id != keep and candidate.status == CandidateStatus.SELECTED.value:
            # 退回 SCORED 而不是 REJECTED:它只是没被选中,不是被判为不合格
            candidate.status = CandidateStatus.SCORED.value


def approve(
    session: Session,
    review_id: UUID,
    *,
    actor: str,
    note: str | None = None,
    tags: list[str] | None = None,
    candidate_id: UUID | None = None,
) -> ReviewItem:
    """人工通过。任务转 MANUALLY_APPROVED,选定的候选图标记为 SELECTED。"""
    # 先锁审核项、再锁任务。顺序在三个决策函数里必须一致,否则并发决策死锁
    item = get_review(session, review_id, for_update=True)
    _assert_pending(item)
    task = gs.get_task(session, item.task_id, for_update=True)
    previous_best = item.best_candidate_id

    chosen = candidate_id or item.best_candidate_id

    # 阻塞审核「通过」的含义是「这张图可以发布」,所以必须真的有那张图。
    # 空结果任务会开出 best_candidate_id 为空的审核项,以前允许直接通过 ——
    # 结果是任务转成 MANUALLY_APPROVED、审核关闭、却一张成品图都不产出,
    # 而且没有任何路径能把它弄回队列。这种任务只能重生或驳回。
    if item.blocking and chosen is None:
        raise ValidationError(
            "这条审核没有可用的候选图,不能直接通过;请选择「重新生成」或「驳回」",
            code=ErrorCode.INPUT_INVALID,
        )

    if chosen is not None:
        candidate = session.get(GenerationCandidate, chosen)
        if candidate is None or candidate.task_id != task.id:
            raise ValidationError("选定的候选图不属于该任务", code=ErrorCode.INPUT_INVALID)
        # 一个任务同时只能有一张 SELECTED:抽检里换了候选却不取消旧的,
        # 会出现「两张都标着已选中,发布的却是第一张」的错位(评审第 16 条)。
        _clear_other_selections(session, task.id, keep=candidate.id)
        candidate.status = CandidateStatus.SELECTED.value
        item.best_candidate_id = candidate.id

    if item.blocking:
        gs.transition(session, task, TaskStatus.MANUALLY_APPROVED)
        # 商品状态在出图成功之后才改,见 _format_after_approval

    _close(item, ReviewStatus.APPROVED, actor=actor, note=note, tags=tags)
    session.flush()

    # 阶段 6:人工通过与自动通过走同一条出图路径,不给人工开小灶
    if item.blocking and chosen is not None:
        _format_after_approval(session, task, chosen, actor=actor)
    elif not item.blocking and candidate_id is not None and candidate_id != previous_best:
        # 抽检(不阻塞)时人换了一张候选。任务早就自动通过并且已经发布了旧候选的图,
        # 只改候选状态不重新出图,网站上挂的仍然是被推翻的那张 —— 必须重新发布。
        _format_after_approval(session, task, candidate_id, actor=actor)
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="ReviewItem",
        entity_id=item.id,
        payload={"action": "approve", "task_id": str(task.id), "candidate_id": str(chosen or "")},
    )
    return item


def _format_after_approval(session: Session, task, candidate_id, *, actor: str) -> None:
    """人工通过后产出多尺寸图。

    出图失败**不回滚人工通过的结论** —— 人已经判过了,图是好图,
    只是转换没成功。任务转 FAILED 并留下错误码,重试即可,不用重新生成。

    ## 抽检换图走的是另一条路(A45 batch12 / R-01)

    这个函数原来假设「进来的任务一定还没走完」,而 A 档抽检恰恰不满足:
    抽检项是在 `AUTO_APPROVED` 那一刻开出来的,任务随后照常
    `FORMATTING -> COMPLETED`。审核员事后换一张候选时,任务**已经是终态**,
    而下面第一件事就是 `transition(task, FORMATTING)` —— `COMPLETED` 的
    出边是空集,`assert_can` 当场抛 `IllegalTransition(409)`。

    后果不是「换图没生效」这么轻:接口在 `session.commit()` 之前抛,
    审核决策、候选图的 SELECTED 改动**一起回滚**,而运营看到的是一句
    「不能从 COMPLETED 转到 FORMATTING;这是终态」。上面那句注释
    (「网站上挂的仍然是被推翻的那张 —— 必须重新发布」)描述的是意图,
    这条路径实际上一次都没走通过。

    修法**不是**给状态机开一条 `COMPLETED -> FORMATTING` 的边:终态封闭
    是「已完成任务不会被后台任务重新拉起」的唯一依据,为一个人工动作
    把它拆掉,代价远大于收益。

    真正要做的只有出图本身 —— `build_outputs` 不看任务状态,它按
    (商品, 用途) 停用旧图、代数加一,重跑是它设计内的动作。所以终态任务
    走一条**只重出图、不动状态**的分支:任务确实已经完成了,换的是选图。
    """
    from app.core.config import settings
    from app.services import output_service
    from app.services.storage import build_storage

    candidate = session.get(GenerationCandidate, candidate_id)
    if candidate is None:
        return

    storage = build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )

    # ---- A45-batch12-7 / R-04:出图之前先确认那张候选图真的还能读 ----
    #
    # `_format_outputs`(自动流水线那条)从 batch12-2 起就做这件事了,
    # 而**人工审核这条路径没有**。两条路最后调的是同一个 `build_outputs`,
    # 于是同一个原因(原图被清理脚本删了 / 写截断 / 存进去的是 HTML 错误页)
    # 在两条路上得到两种结果:
    #
    #     自动流水线   SOURCE_ASSET_MISSING —— 有名字,`retry_task` 挡得住,
    #                  界面告诉运营该换一张候选
    #     人工审核     INTERNAL_ERROR「多尺寸输出失败:FileNotFoundError」——
    #                  `retry_task` 眼里和别的崩溃没区别,续跑判定照样成立,
    #                  于是点重试 -> FORMATTING -> 又读同一个坏文件
    #
    # 后者正是 R-04 报的那件事:重试恒失败,而错误码不指向根因。运营手上
    # 唯一的动作(再点一次)在这条路上没有出路。
    #
    # 判定不在这里重写 —— `output_service.source_asset_problem()` 是唯一那份,
    # 两条路径共用它。这个函数只负责把结论翻译成各自路径下的去向。
    problem = _readable_source_problem(candidate, storage=storage)

    if sm.is_terminal(task.status):
        _rebuild_outputs_for_finished_task(
            session, task, candidate, storage=storage, actor=actor, problem=problem
        )
        return

    if problem is not None:
        # 直接落 FAILED,**不经过 FORMATTING**:出图这件事一步都没开始,
        # 中间那一跳只会在界面上留下一条「正在出图」的假象。
        #
        # 错误码用专用码而不是 INTERNAL_ERROR,这一条就是修复本身:
        # `retry_task` 的 `SOURCE_ASSET_BROKEN_CODES` 分支据此把人挡在
        # 重试之外,并告诉他该做的是换一张候选(免费)或确认后重新生成(花钱)。
        logger.error(
            "the approved candidate cannot be read, refusing to format",
            extra={
                "extra_fields": {
                    "task_id": str(task.id),
                    "candidate_id": str(candidate.id),
                    "reason": problem,
                }
            },
        )
        gs.transition(
            session,
            task,
            TaskStatus.FAILED,
            error_code=problem,
            error_message=(
                f"选中的候选图无法读取({problem});"
                "重试不会好转,请回审核页改选一张可用的候选,或确认后重新生成"
            ),
        )
        session.flush()
        return

    gs.transition(session, task, TaskStatus.FORMATTING)
    try:
        output_service.build_outputs(session, task, candidate, storage=storage, actor=actor)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "output formatting failed after manual approval",
            extra={"extra_fields": {"task_id": str(task.id)}},
        )
        task.error_code = ErrorCode.INTERNAL_ERROR.value
        task.error_message = f"多尺寸输出失败:{type(exc).__name__}"[:500]
        gs.transition(session, task, TaskStatus.FAILED)
        session.flush()
        return

    gs.transition(session, task, TaskStatus.COMPLETED)
    task.finished_at = _now()

    product = session.get(Product, task.product_id)
    if product is not None and product.status != ProductStatus.ARCHIVED.value:
        product.status = ProductStatus.COMPLETED.value

    session.flush()


def _readable_source_problem(candidate, *, storage) -> str | None:
    """候选图还能不能读。返回错误码,没问题返回 None(A45-batch12-7 / R-04)。

    ## 存储层不通 ≠ 文件坏了

    `source_asset_problem()` 的 docstring 写明它会把存储层自己的异常**抛出去**
    (S3 不通、凭据过期),因为那一类重试就能好,判成 `SOURCE_ASSET_*` 会把它
    错误地挡在重试之外。两条调用路径都必须接住这件事,否则一次 S3 抖动会让
    一批候选图被永久标成「坏了」。

    `_format_outputs` 里有同形状的 try —— 那里吞掉之后往下走通用异常分支,
    这里吞掉之后返回 None,同样往下走 `build_outputs` 的通用异常分支。
    两处的去向一致:**说不清的时候按可重试处理**。
    """
    from app.services import output_service

    try:
        return output_service.source_asset_problem(candidate, storage=storage)
    except Exception:  # noqa: BLE001 - 存储层不通不是文件坏了,见 docstring
        logger.warning(
            "cannot verify the selected candidate before formatting",
            extra={"extra_fields": {"candidate_id": str(candidate.id)}},
        )
        return None


#: 允许「换一张候选图重出」的终态。**只有 COMPLETED。**
#:
#: 另外三个终态各有各的不能:CANCELLED / MANUALLY_REJECTED / AUTO_REJECTED
#: 的共同点是「这个任务的图不该出现在网站上」,而重出图恰恰会把它挂上去 ——
#: `build_outputs` 会停用该商品同用途的旧图并启用新的一代。抽检项本身
#: 只可能开在自动通过的任务上,所以正常情况下走不到那三个;写死这一条
#: 是因为它错了不会报错,只会安静地把一张被驳回的图发上线。
_REBUILDABLE_TERMINAL_STATUSES: frozenset[str] = frozenset({TaskStatus.COMPLETED.value})


def _rebuild_outputs_for_finished_task(
    session: Session, task, candidate, *, storage, actor: str, problem: str | None = None
) -> None:
    """任务已经走完时,只重出图,不动任务状态。

    ## 为什么失败要抛,而不是像正常路径那样落 FAILED

    正常路径上「出图失败」有下一步:任务转 FAILED,`can_resume_formatting()`
    认得出这种失败,运营点重试就从 FORMATTING 接着走,不重新生成。

    这条路径没有那个下一步 —— 任务已经是 COMPLETED,既转不了 FAILED
    (终态封闭),也没有任何重试入口会捡起它。这时候如果吞掉异常返回,
    结果是:

        候选图的 SELECTED 换成了新的那张   库里说换了
        审核项关闭,记 APPROVED            界面上说换成功了
        成品图还是旧的那一代                网站上挂的仍然是被推翻的那张

    三者不一致,而且**没有任何一处会报错**。那正是本轮第 8 条要防的形状
    (外部/下游操作失败,本地却标记成功)。

    所以这里抛。抛出去的后果是整笔回滚:候选没换、审核项还是 PENDING、
    运营看到一句说得清楚的话,可以重来。**什么都没发生**比
    **三个地方各说一套**好得多。

    ## `problem` 为什么是参数,不在这里算(A45-batch12-7 / R-04)

    调用方两条分支共用同一次检查,算两遍就是读两遍对象存储 —— 而 S3 后端下
    那是一次真实往返。更要紧的是:算两遍就有两个结论,而两次读之间文件状态
    可以变(清理脚本正在跑),于是「挡不挡」和「说什么」可能来自不同的事实。

    传进来的是**同一个结论**,所以界面上那句话和实际做出的决定永远一致。
    """
    from app.services import output_service

    if task.status not in _REBUILDABLE_TERMINAL_STATUSES:
        raise ValidationError(
            f"任务处于 {task.status},不能通过换图重新出图。"
            "被取消或被驳回的任务不应该有图挂在网站上;"
            "如需重做,请为该商品新建一个生成任务",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    # 顺序有讲究:上面那条问的是「**该不该**把这个任务的图挂上去」,
    # 下面这条问的是「那张图**读不读得出来**」。状态不对时给出
    # 「换一张候选」的建议是错的 —— 被驳回的任务换哪张都不该上线。
    if problem is not None:
        # 原图读不出来,而这条路径**没有重试出口**(任务已经是 COMPLETED,
        # 转不了 FAILED、也没有任何入口会捡起它)。原来这里会往下走,
        # 撞进通用异常分支,得到一句「请稍后重试」——
        #
        #     错误码   INTERNAL_ERROR,不指向根因
        #     建议     稍后重试。而文件已经没了,再点一百次也是同一个结果
        #
        # 这正是 R-04 说的「人工反复点没有出路」。文件丢了是个**确定的**
        # 结论,不是一次可能会好的失败,所以话要说死,并且给出真正的退路:
        # 换一张原图还在的候选(免费,不动线上图)。
        logger.error(
            "refusing to rebuild outputs: the swapped candidate cannot be read",
            extra={
                "extra_fields": {
                    "task_id": str(task.id),
                    "candidate_id": str(candidate.id),
                    "reason": problem,
                }
            },
        )
        raise ValidationError(
            f"选中的候选图读不出来({problem}),无法重新出图。"
            "本次换图**未生效**,候选图与审核结论都已回滚,"
            "网站上仍然是原来那张图。"
            "**重试不会好转** —— 请回审核页改选一张原图还在的候选",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    try:
        output_service.build_outputs(session, task, candidate, storage=storage, actor=actor)
    except Exception as exc:  # noqa: BLE001 - 转成运营看得懂的话再抛,见 docstring
        logger.exception(
            "rebuilding outputs for a finished task failed",
            extra={
                "extra_fields": {
                    "task_id": str(task.id),
                    "candidate_id": str(candidate.id),
                    "status": task.status,
                }
            },
        )
        raise ValidationError(
            f"换图后重新出图失败({type(exc).__name__}),本次换图**未生效**,"
            "候选图与审核结论都已回滚。网站上仍然是原来那张图。"
            "请稍后重试;若反复失败,检查该候选图的原图是否还在存储里",
            code=ErrorCode.INTERNAL_ERROR,
            http_status=409,
        ) from exc

    logger.info(
        "outputs rebuilt after a spot-check candidate swap",
        extra={
            "extra_fields": {
                "task_id": str(task.id),
                "candidate_id": str(candidate.id),
                "actor": actor,
            }
        },
    )
    session.flush()


def reject(
    session: Session,
    review_id: UUID,
    *,
    actor: str,
    note: str | None = None,
    tags: list[str] | None = None,
) -> ReviewItem:
    """人工驳回。任务进终态 MANUALLY_REJECTED。"""
    item = get_review(session, review_id, for_update=True)
    _assert_pending(item)
    task = gs.get_task(session, item.task_id, for_update=True)

    if item.blocking:
        gs.transition(session, task, TaskStatus.MANUALLY_REJECTED)
        product = session.get(Product, task.product_id)
        if product is not None and product.status == ProductStatus.NEEDS_REVIEW.value:
            # 驳回后回到"可再生成"而不是"已完成":这个 SKU 还没有能用的图
            product.status = ProductStatus.READY.value

    _close(item, ReviewStatus.REJECTED, actor=actor, note=note, tags=tags)
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="ReviewItem",
        entity_id=item.id,
        payload={"action": "reject", "task_id": str(task.id)},
    )
    return item


def request_regeneration(
    session: Session,
    review_id: UUID,
    *,
    actor: str,
    provider: str | None = None,
    extra_rounds: int = 1,
    note: str | None = None,
    tags: list[str] | None = None,
) -> tuple[ReviewItem, GenerationTask]:
    """人工要求重新生成,可同时切换 Provider(需求第十四章)。

    这里必须**加轮次**:任务正是因为轮次耗尽才进的队列,
    不加轮次就重新排队,它会立刻再次判定耗尽并弹回来,形成空转。
    """
    item = get_review(session, review_id, for_update=True)
    _assert_pending(item)
    task = gs.get_task(session, item.task_id, for_update=True)

    # **先判「这个任务还能不能重生」,再动任何字段。**(A45 batch12 / R-02)
    #
    # 下面会改 provider、改 max_rounds,然后才 `transition(-> REGENERATING)`。
    # 任务已经走完时那次转移必然抛 `IllegalTransition`,而它的措辞是状态机
    # 内部语言(「不能从 COMPLETED 转到 REGENERATING;这是终态」)——
    # 运营看到这句话既不知道自己做错了什么,也不知道该做什么。
    #
    # 这条最容易撞上的入口是 **A 档抽检**:抽检项不阻塞,任务照常跑到
    # COMPLETED,而审核项还在队列里 PENDING。审核员觉得这批图不行、
    # 点「重新生成」—— 撞的就是这里。
    if not sm.can_transition(task.status, TaskStatus.REGENERATING):
        if not item.blocking:
            raise ValidationError(
                f"这是一条抽检复核项,任务本身已经跑完({task.status}),不能再要求重新生成。"
                "抽检项请用「通过」(可顺便换一张候选图)或「驳回」关闭;"
                "确实要重做这个 SKU,请新建一个生成任务",
                code=ErrorCode.INPUT_INVALID,
                http_status=409,
            )
        raise ValidationError(
            f"任务当前状态是 {task.status},不能要求重新生成。"
            "请刷新页面确认这条审核是否已经被别人处理过",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    if provider and provider != task.provider:
        from app.providers.base import GenerationMode
        from app.providers.registry import get_provider

        target = get_provider(provider)
        if not target.is_configured():
            raise ValidationError(
                f"{target.display_name or provider} 未配置,不能切换",
                code=ErrorCode.PROVIDER_NOT_CONFIGURED,
                http_status=409,
            )
        if not target.capabilities().supports(GenerationMode(task.mode)):
            raise ValidationError(
                f"{provider} 不支持 {task.mode}", code=ErrorCode.INPUT_INVALID
            )
        task.provider = target.provider_name

    # 累计上限:人工可以反复要求重生,但不能无限加下去。
    # 没有上限时,一个始终不达标的 SKU 可以把额度慢慢烧光而没有任何一处报警。
    limit = _max_total_rounds()
    requested = task.max_rounds + max(1, extra_rounds)
    if requested > limit:
        raise ValidationError(
            f"累计轮次上限为 {limit},当前已是 {task.max_rounds} 轮,"
            f"不能再加 {extra_rounds} 轮。这个 SKU 反复不达标,"
            f"建议改素材或换 Provider,而不是继续重生",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    task.max_rounds = requested
    task.cancel_requested = False
    gs.transition(session, task, TaskStatus.REGENERATING)

    _close(item, ReviewStatus.REGENERATION_REQUESTED, actor=actor, note=note, tags=tags)
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="ReviewItem",
        entity_id=item.id,
        payload={
            "action": "regenerate",
            "task_id": str(task.id),
            "provider": task.provider,
            "max_rounds": task.max_rounds,
        },
    )
    return item, task


def review_stats(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(ReviewItem.status, func.count()).group_by(ReviewItem.status)
    ).all()
    counts = {status: int(count) for status, count in rows}
    return {
        "pending": counts.get(ReviewStatus.PENDING.value, 0),
        "approved": counts.get(ReviewStatus.APPROVED.value, 0),
        "rejected": counts.get(ReviewStatus.REJECTED.value, 0),
        "regeneration_requested": counts.get(ReviewStatus.REGENERATION_REQUESTED.value, 0),
    }
