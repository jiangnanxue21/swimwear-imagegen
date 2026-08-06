"""人工审核队列接口(需求第十六章)。

    GET    /api/reviews
    GET    /api/reviews/{id}
    POST   /api/reviews/{id}/approve
    POST   /api/reviews/{id}/reject
    POST   /api/reviews/{id}/regenerate

另外挂了两个只读端点:规则集与评分器列表。
它们不在需求第十六章的清单里,但分档标准和当前评分器是运营**必须看得见**的东西 ——
否则"这张图为什么被判 C"只能去翻代码。两者都只读,不提供编辑入口。
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_actor, db_session, require_operator
from app.core import audience as audience_rules
from app.core.config import settings
from app.core.enums import AUDIENCE_LABELS, DispatchReason, ReviewReason, ReviewStatus
from app.core.logging import get_logger
from app.evaluators.registry import describe_all as describe_evaluators
from app.models.evaluation import CandidateEvaluation
from app.models.generation import GenerationTask
from app.models.product import Product
from app.models.product_asset import ProductAsset
from app.schemas.common import Page
from app.schemas.evaluation import (
    EvaluationOut,
    EvaluatorOut,
    ReviewApprove,
    ReviewCandidateOut,
    ReviewDecision,
    ReviewDetailOut,
    ReviewOut,
    ReviewRegenerate,
    RuleSetOut,
)
from app.services import dispatch_service, evaluation_service, review_service, rule_set_service
from app.services import generation_service as gs
from app.services.storage import asset_url, build_storage

# 整个路由挂 require_operator(读接口也要)。为什么挂在路由器上见 `deps.require_operator`
router = APIRouter(tags=["review"], dependencies=[Depends(require_operator)])
logger = get_logger(__name__)


def _storage():
    return build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )


def _apply_audience(out, product: Product, focus_cache: dict | None = None) -> None:
    """把受众三件套贴到审核出参上(§13.2 / §19)。

    **与 `workbench._product_out` 是同一套语义**,不是另一份实现:
    `audience=None` 表示待确认(不是 UNISEX),标签由后端算(§21.3),
    `review_focus` 从规则包读(§19 —— 前端不许自己维护一份受众到检查项
    的映射,维护两份的后果是规则包改了而界面还在提示旧的那几项)。

    受众取值不合法时按"待确认"显示而不是抛错:审核页少一个标签,
    不该让运营打不开这一页。真正拦住它的是工作台的阻断问题。

    ## `focus_cache` 不是可选的优化

    `review_focus_for` 走 `generic.field_spec`,而那个函数**每次都重新
    读并校验 YAML**(它自己的 docstring 写着理由:spec 是外部文件,
    可能被改坏)。实测一次约 9ms —— 队列页一屏 20 行就是 180ms,
    而这一屏上**最多只有三种规则包**(女装/男装/中性)。

    所以列表路径必须传一个按 `category_id` 索引的缓存进来。缓存的键
    是规则包,不是商品:同受众同品类的两件商品,重点检查项必然相同。
    单条详情不传,那时只有一次调用。
    """
    from app.workbench import service as wb

    try:
        audience = audience_rules.coerce(product.audience)
    except ValueError:
        audience = None
    out.audience = audience.value if audience else None
    out.audience_label = AUDIENCE_LABELS[audience] if audience else "待确认"

    if focus_cache is None:
        out.review_focus = list(wb.review_focus_for(product))
        return
    # 键用规则包本身而不是 (audience, garment_type):派生规则只有
    # `core.audience.category_code_for` 一份,这里不重新实现一遍
    key = (product.audience, product.category, product.garment_type)
    if key not in focus_cache:
        focus_cache[key] = list(wb.review_focus_for(product))
    out.review_focus = list(focus_cache[key])


def _basic_review_out(
    item,
    *,
    products: dict[UUID, Product],
    task_statuses: dict[UUID, str],
    focus_cache: dict | None = None,
) -> ReviewOut:
    """一条审核项的列表形态。**商品和任务由调用方批量取好后传进来。**

    ## 为什么不再自己查(报告 6.5)

    原来每一行都 `session.get(Product)` + `gs.get_task()`,一页 20 条就是
    41 条查询。更糟的是那个 `try/except Exception: out.task_status = ""`:
    它本意是兜底,实际效果是**把任何数据库异常变成一个空字符串**,
    而空字符串在前端会被渲染成"没有状态"。查询挂了和任务没状态,
    在界面上长得一模一样。

    批量取之后这两件事都没了:查询次数是常数,而数据库异常会照常往上抛,
    由全局处理器变成 500 —— 那才是它应有的样子。
    """
    out = ReviewOut.model_validate(item)
    product = products.get(item.product_id)
    if product is not None:
        out.product_sku = product.sku
        out.product_name = product.name
        # 规则包按 category_id 缓存:一屏 20 行最多只有三种规则包,
        # 而 field_spec 每次都重读并校验 YAML(约 9ms/次)
        _apply_audience(out, product, focus_cache)
    # 外键是 CASCADE,任务不存在意味着审核项也该没了。真取不到时留空串,
    # 但这已经不是"查询失败"的同义词了
    out.task_status = task_statuses.get(item.task_id, "")
    return out


def _single_review_out(session: Session, item) -> ReviewOut:
    """一条审核项的写接口返回形态。

    ## 这里曾经有一个 500(A42 修)

    `_basic_review_out` 在「报告 6.5」那轮被改成批量形态
    (`item, *, products, task_statuses`),列表页那个调用点跟着改了,
    **approve / reject / regenerate 三个写接口没有** —— 它们仍按老签名写着
    `_basic_review_out(session, item)`。后果不是少几个字段,是
    `TypeError: takes 1 positional argument but 2 were given`,
    也就是**人工审核的三个动作全部无条件 500**。

    它能活到现在,是因为唯一会碰到它的 `tests/test_api_reviews.py` 需要真库,
    而没库时整个模块 skip —— 而 CI 至今没有在真实 runner 上跑绿过一次。
    「跳过」和「通过」在汇总行里长得一模一样,这是那条差别的代价。

    所以这个函数存在的理由不是复用三行代码,是**让写接口和列表页共用同一个
    出口**:签名再变一次时,改不动的地方会是编译期的错,不是运行期的 500。
    """
    products, task_statuses = _batch_context(session, [item])
    return _basic_review_out(item, products=products, task_statuses=task_statuses)


def _batch_context(
    session: Session, items: list
) -> tuple[dict[UUID, Product], dict[UUID, str]]:
    """列表页要的商品与任务状态,两条查询取完。"""
    if not items:
        return {}, {}
    product_ids = {i.product_id for i in items}
    task_ids = {i.task_id for i in items}
    products = {
        row.id: row
        for row in session.scalars(select(Product).where(Product.id.in_(product_ids)))
    }
    task_statuses = {
        row_id: status
        for row_id, status in session.execute(
            select(GenerationTask.id, GenerationTask.status).where(
                GenerationTask.id.in_(task_ids)
            )
        ).all()
    }
    return products, task_statuses


@router.get("/reviews", response_model=Page[ReviewOut])
def list_reviews(
    session: Session = Depends(db_session),
    status_filter: ReviewStatus | None = Query(ReviewStatus.PENDING, alias="status"),
    reason: ReviewReason | None = Query(None),
    product_id: UUID | None = Query(None),
    sort: str | None = Query(
        None, description=f"排序字段,可用:{', '.join(review_service.REVIEW_SORTABLE)}"
    ),
    order: str | None = Query(None, description="asc / desc(也认 antd 的 ascend / descend)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> Page[ReviewOut]:
    rows, total = review_service.list_reviews(
        session,
        status=status_filter.value if status_filter else None,
        reason=reason.value if reason else None,
        product_id=product_id,
        sort=sort,
        order=order,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    products, task_statuses = _batch_context(session, rows)
    focus_cache: dict = {}
    return Page[ReviewOut](
        items=[
            _basic_review_out(
                r,
                products=products,
                task_statuses=task_statuses,
                focus_cache=focus_cache,
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


def _candidate_out(candidate, evaluation, storage) -> ReviewCandidateOut:
    return ReviewCandidateOut(
        id=candidate.id,
        round_number=candidate.round_number,
        candidate_index=candidate.candidate_index,
        # 候选图未过审,只给短期签名地址
        url=asset_url(storage, candidate.storage_path),
        width=candidate.width,
        height=candidate.height,
        seed=candidate.seed,
        status=candidate.status,
        grade=candidate.grade,
        # `is not None` 而不是真值判断(报告 6.5)。0 分是一个**有效的评分**,
        # 而且恰好是最需要被看见的那个 —— 原来的写法把它显示成"未评分",
        # 于是一张被判 0 分的图在审核页上和一张还没评的图长得一样
        overall_score=(
            float(candidate.overall_score)
            if candidate.overall_score is not None
            else None
        ),
        evaluation=EvaluationOut.model_validate(evaluation) if evaluation else None,
    )


@router.get("/reviews/{review_id}", response_model=ReviewDetailOut)
def get_review(review_id: UUID, session: Session = Depends(db_session)) -> ReviewDetailOut:
    item = review_service.get_review(session, review_id)
    task = gs.get_task(session, item.task_id)
    storage = _storage()

    detail = ReviewDetailOut.model_validate(item)
    product = session.get(Product, item.product_id)
    if product is not None:
        detail.product_sku = product.sku
        detail.product_name = product.name
        _apply_audience(detail, product)
    detail.task_status = task.status
    detail.task_mode = task.mode
    detail.max_rounds = task.max_rounds

    # 商品原图(需求第十四章:审核页要能对照原图)
    assets = list(
        session.scalars(
            select(ProductAsset).where(ProductAsset.product_id == item.product_id)
        )
    )
    detail.product_assets = [
        {
            "id": str(a.id),
            "asset_type": a.asset_type,
            "url": asset_url(storage, a.storage_path),
            "width": a.width,
            "height": a.height,
        }
        for a in assets
    ]

    candidates = gs.list_candidates(session, task.id)
    evaluations = evaluation_service.evaluations_by_candidate(session, task.id)
    all_out = [_candidate_out(c, evaluations.get(c.id), storage) for c in candidates]
    detail.all_candidates = all_out

    # 各轮最佳候选:每轮取总分最高的一张
    best_by_round: dict[int, ReviewCandidateOut] = {}
    for out in all_out:
        current = best_by_round.get(out.round_number)
        if current is None or (out.overall_score or 0) > (current.overall_score or 0):
            best_by_round[out.round_number] = out
    detail.round_best_candidates = [best_by_round[k] for k in sorted(best_by_round)]
    return detail


@router.post(
    "/reviews/{review_id}/approve",
    response_model=ReviewOut,
    dependencies=[Depends(require_operator)],
)
def approve_review(
    review_id: UUID,
    payload: ReviewApprove,
    request: Request,
    session: Session = Depends(db_session),
) -> ReviewOut:
    item = review_service.approve(
        session,
        review_id,
        actor=current_actor(request),
        note=payload.note,
        tags=payload.tags,
        candidate_id=payload.candidate_id,
    )
    # A42 合入:提交归接口所有(任务 19),出口走 _single_review_out(修 500)。
    # 两个补丁各改了这两行的一半,取并集 —— 只取任一半都是缺陷:
    # 只要 commit 会 500,只要 _single_review_out 会返回 200 但什么都没存。
    session.commit()
    return _single_review_out(session, item)


@router.post(
    "/reviews/{review_id}/reject",
    response_model=ReviewOut,
    dependencies=[Depends(require_operator)],
)
def reject_review(
    review_id: UUID,
    payload: ReviewDecision,
    request: Request,
    session: Session = Depends(db_session),
) -> ReviewOut:
    item = review_service.reject(
        session,
        review_id,
        actor=current_actor(request),
        note=payload.note,
        tags=payload.tags,
    )
    # A42 合入:提交归接口所有(任务 19),出口走 _single_review_out(修 500)。
    # 两个补丁各改了这两行的一半,取并集 —— 只取任一半都是缺陷:
    # 只要 commit 会 500,只要 _single_review_out 会返回 200 但什么都没存。
    session.commit()
    return _single_review_out(session, item)


@router.post(
    "/reviews/{review_id}/regenerate",
    response_model=ReviewOut,
    dependencies=[Depends(require_operator)],
)
def regenerate_review(
    review_id: UUID,
    payload: ReviewRegenerate,
    request: Request,
    session: Session = Depends(db_session),
) -> ReviewOut:
    item, task = review_service.request_regeneration(
        session,
        review_id,
        actor=current_actor(request),
        provider=payload.provider,
        extra_rounds=payload.extra_rounds,
        note=payload.note,
        tags=payload.tags,
    )
    # 派发意图和"放宽轮次上限"这个决定写在同一个事务里。
    # 分开写的话,轮次改了但消息没发出去,任务就停在 REGENERATING 等一个
    # 永远不会到来的 worker —— 审核页上看着像在重生,其实什么都没发生。
    dispatch_service.enqueue(
        session, task.id, reason=DispatchReason.REVIEW_REGENERATE.value
    )
    # 先提交,再派发。反过来的话 worker 可能读到还没更新的轮次上限。
    session.commit()
    dispatch_service.deliver_pending(session, task.id)
    return _single_review_out(session, item)


@router.get("/generation-tasks/{task_id}/evaluations", response_model=list[EvaluationOut])
def list_task_evaluations(
    task_id: UUID, session: Session = Depends(db_session)
) -> list[CandidateEvaluation]:
    gs.get_task(session, task_id)  # 不存在时抛 404
    return evaluation_service.list_evaluations(session, task_id)


@router.get("/rule-sets", response_model=list[RuleSetOut])
def list_rule_sets(session: Session = Depends(db_session)):
    """当前分档规则。只读 —— 改规则请走迁移或运维脚本,不从后台随手改。"""
    return rule_set_service.list_rule_sets(session)


@router.get("/evaluators", response_model=list[EvaluatorOut])
def list_evaluators() -> list[dict]:
    """评分器列表。不返回任何密钥(需求第十四章)。"""
    return describe_evaluators()
