"""发布接口(方案 v4.1 任务 18 / 6.4 节)。

    POST /api/publish/products/{product_id}/submit  提交:创建/更新/重上架,支持干跑
    GET  /api/publish/listings                      发布清单
    GET  /api/publish/listings/{listing_id}         发布详情(6.4 的发布前/中/后三段)
    POST /api/publish/listings/{listing_id}/poll    刷新平台状态
    POST /api/publish/listings/{listing_id}/delist  下架(4.1 节 H 的唯一入口)
    POST /api/publish/listings/{listing_id}/reconcile 带原幂等键去平台确认(EX-04)
    POST /api/publish/listings/{listing_id}/redeliver 重新投递已放弃的记录(EX-04)
    GET  /api/publish/cleanup/inventory             本轮测试建了哪些外部商品

## 前端在哪(A45-#8 / 台账 B-02)

    frontend/src/api/publish.ts         这六个端点的客户端
    frontend/src/pages/PublishPage.tsx  「发布上架」页:清单 + 详情 + 清理预案
    ExportTab 的「发布到平台」按钮       跳 /publish?product_id=…,提交的入口

**这一段是一条纪律,不是索引。** 在它之前这六个端点前端零消费了很久,而
"后端做完了"和"这条链路能被用"是两件事:上架是流水线的终点,没有入口
意味着人工评测只能走到导出为止。往这个模块加端点时,同时回答一句
"运营从哪儿点得到它" —— 答不上来就说明这一次只做了一半。

## 这一层只做三件事

    取数   把四个来源的行读出来,拼成 `PublishFacts`
    判定   调 `workflows/publish_view.build_view` —— **判定不写在这里**
    编排   调 service,并持有事务

判定分出去的理由见 `publish_view` 顶部:它要能在 `tests/pure/` 里被穷举。
写在接口函数里的话,覆盖"某个状态组合下按钮不该亮"就得起一个 FastAPI 应用
外加一个数据库 —— 那种测试不会有人为了一个枚举分支去写,于是那个分支就没人验。

## 事务归这里

`publish_service.enqueue()` 只 flush 不 commit(7.8 节:事务归调用方所有)。
所以每个写端点必须自己 `session.commit()`。忘了的表现是"点了没反应",
而且 outbox 里没有行、beat 也捡不到任何东西 —— 没有任何报错。

## 为什么下架也走 `enqueue`

下架不是一个特殊通道,它是 `PublishOperation.DELIST`。走同一条 Outbox
意味着它同样有幂等键、同样有退避、同样在异常队列里可见。给它开一条
"直接调平台"的近路会让 4.1 节 H 的清理动作成为全链路上唯一没有重试、
没有留痕的一步,而那恰恰是最不该出错的一步。


## A45-#8:**这组端点当前没有任何界面在消费。**

`frontend/src` 全文对 submit / listings / poll / delist / cleanup **零引用**。
发布链路(任务 14/15/18)有 API、有 beat 节拍、有 Outbox、有轮询退避,
唯独没有 UI —— 那就是评审台账里的 **B-02**。

写在这里是为了两件事:

1. 下一轮评审不必再发现一遍(它看起来像"死代码",实际是"页面还没做");
2. A-07 / A-08 / A-13 修的是这几条端点的**后端质量**,而在 B-02 落地之前,
   那三条修复**只能用 API 直接验收**,没有页面可点。

做页面时连同 `docs/STATUS.md` 的已知限制一起更新。
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.api.deps import current_actor, db_session, require_operator
from app.channels.simulator import is_simulated_external_id
from app.core.clock import iso_utc
from app.core.enums import (
    UNRESOLVED_REJECTION_STATUSES,
    AuditAction,
    PublishAttemptStatus,
    PublishOperation,
)
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models.listing_copy import ListingDraft
from app.models.platform_rejection import PlatformRejection
from app.models.product import Product
from app.models.publishing import ChannelListing, PublishAttempt, PublishOutbox
from app.services import audit, cleanup_service, poll_service, publish_service
from app.workbench import audience_rules as audience_gate
from app.workbench import service as wb
from app.workflows import publish_view as pv

router = APIRouter(
    prefix="/publish", tags=["publish"], dependencies=[Depends(require_operator)]
)
logger = get_logger(__name__)


# ================================================================ 取数


def _product(session: Session, product_id: UUID) -> Product:
    row = session.get(Product, product_id)
    if row is None:
        raise NotFoundError("商品不存在")
    return row


def _listing(session: Session, listing_id: UUID) -> ChannelListing:
    row = session.get(ChannelListing, listing_id)
    if row is None:
        raise NotFoundError("发布记录不存在")
    return row


def _latest_attempt(session: Session, listing_id: UUID) -> PublishAttempt | None:
    """最近一次尝试。

    按 `created_at` 取而不是按 `started_at`:后者在尝试还没发出去时是 NULL,
    而"刚排上队还没发"恰恰是运营最想看到状态的时刻。

    ## `id` 是 tiebreaker,不是排序意图

    `created_at` 的默认值是 `server_default=func.now()`,而 PostgreSQL 的
    `now()` 取的是**事务开始时间** —— 同一个事务里写下的两条 attempt,
    时间戳一模一样。只按 `created_at DESC` 排,数据库在并列里挑哪一条是
    未定义的,而 A-08 的批量路径(`list_listings`)是另一条独立的 SQL,
    两边可能挑中不同的行 —— 于是列表说"已提交"、详情说"失败"。
    这正是 §3.4 那条「任何情况下不得出现两个数字」要拦的东西。

    主键是 `uuid4`,所以并列时选中哪条是**任意的**;但两条路径用同一条
    规则,选中的一定是**同一条**。这里要的是后者,不是"更新的那条"。
    A-07 给 `ChannelListing` 的翻页加 `id` 是同一个理由。
    """
    return session.scalar(
        select(PublishAttempt)
        .where(PublishAttempt.channel_listing_id == listing_id)
        .order_by(desc(PublishAttempt.created_at), desc(PublishAttempt.id))
        .limit(1)
    )


def _outbox_for(session: Session, attempt_id: UUID | None) -> PublishOutbox | None:
    if attempt_id is None:
        return None
    return session.scalar(
        select(PublishOutbox).where(PublishOutbox.publish_attempt_id == attempt_id)
    )


def _current_draft(session: Session, listing: ChannelListing) -> ListingDraft | None:
    """这条 listing 当前对应的草稿。

    优先用 `listing.draft_id` —— 那是提交时钉住的那一版。取不到才按
    spu/channel/site 找最新的一版:老数据里 `draft_id` 可能是空的,而
    "找不到草稿"会让整个详情页塌掉,那个代价比拿到一版稍新的草稿大得多。
    """
    if listing.draft_id is not None:
        row = session.get(ListingDraft, listing.draft_id)
        if row is not None:
            return row
    # `ChannelListing` 上没有 spu 列 —— 唯一约束建在 product_id 上
    # (见 models/publishing.py 顶部),所以退路必须绕一次商品
    product = session.get(Product, listing.product_id)
    if product is None:
        return None
    return session.scalar(
        select(ListingDraft)
        .where(
            ListingDraft.spu == product.spu,
            ListingDraft.channel == listing.channel,
            ListingDraft.site == listing.site,
        )
        .order_by(desc(ListingDraft.created_at))
        .limit(1)
    )


def _draft_for_product(session: Session, product: Product, listing: ChannelListing | None):
    """按商品找当前草稿。提交端点用它 —— 那时还没有 listing。"""
    if listing is not None and listing.draft_id is not None:
        row = session.get(ListingDraft, listing.draft_id)
        if row is not None:
            return row
    rows = list(
        session.scalars(
            select(ListingDraft)
            .where(ListingDraft.spu == product.spu)
            .order_by(desc(ListingDraft.created_at))
        )
    )
    return rows[0] if rows else None


def _unresolved_rejections(
    session: Session, draft: ListingDraft | None
) -> list[PlatformRejection]:
    """未解决的驳回。

    口径用 `UNRESOLVED_REJECTION_STATUSES` 而不是 `== OPEN` —— 含
    FIXED_PENDING_EXPORT。少了它的话,运营点一次"我改好了"就能让商品
    从阻断列表消失,而那正是三态改造要修掉的行为(见 `RejectionStatus`)。
    """
    if draft is None:
        return []
    return list(
        session.scalars(
            select(PlatformRejection).where(
                PlatformRejection.draft_id == draft.id,
                PlatformRejection.status.in_(UNRESOLVED_REJECTION_STATUSES),
            )
        )
    )


def _auto_closeable(rejections: list[PlatformRejection]) -> bool | None:
    """系统有没有可能自己看到"这条驳回修好了"。

    A-13 之后这个洞补上了:`resolve_gate()` 现在接受两种等价证据 ——
    驳回之后有一次新的**导出**,或有一次新的**成功提交**(PublishAttempt,
    见 `platform_service._publish_attempt_entries`)。API 自动上架的商品
    不走导出,但修改草稿并重新提交成功后,闸门同样能自证并放行;
    未修改草稿的重复提交仍然被指纹检查拦下。

    于是两条路径来的驳回都可能被系统自证关闭,这里对存在未解决驳回的
    情况统一返回 True。

    ## True 是"有可能",不是"一定能"

    这个函数只拿得到驳回行,拿不到 session,所以它答的是**这类驳回在原理上
    能不能自证**,不是"这一条现在过不过得了闸"。具体那一条由
    `platform_service.resolve_gates()` 逐条算,界面上"还缺哪一步"用的是它。

    有一处 True 会偏乐观:A-13 的提交证据链经 `ChannelListing.draft_id` 关联,
    `draft_id` 为空的老数据取不到留痕,那类 API 驳回实际仍然只能人工关闭。
    仍然选择返回 True —— 反过来(逐条查库判 False)要在清单页每行加一次
    查询,而 A-08 刚把这些查询收掉;况且过不了闸的那条,闸门本身会说清楚。

    None = 没有未解决的驳回,这个问题不适用。**不要用 True 代替 None** ——
    "能自动关闭"和"没有东西要关闭"是两件事,合并之后界面分不出来。
    """
    if not rejections:
        return None
    return True


def _assemble_facts(
    listing: ChannelListing | None,
    draft: ListingDraft | None,
    attempt: PublishAttempt | None,
    outbox: PublishOutbox | None,
    rejections: list[PlatformRejection],
) -> pv.PublishFacts:
    """已取到的行 -> 判定层的输入。**判定的组装只有这一份**(§3.4):
    详情路径(`_facts`,逐行取数)和清单路径(批量取数,A-08)都走这里。

    ## 它保证的是什么,不保证什么

    保证:**同样的四行输入,两条路径给出同样的判定**。判定逻辑不会再各写
    一份然后各自漂移 —— 那是原来最容易出现"两个数字"的地方。

    不保证:两条路径**取到的四行一定相同**。已知的一处差异在草稿上 ——
    详情走 `_current_draft()`,`listing.draft_id` 为空时会按 spu/channel/site
    回退找一版;清单只认 `listing.draft_id`(回退要绕商品表,批量化不了,
    A-08 没有改这个行为,原来也是这样)。于是 `draft_id` 为空的**老数据**,
    清单看到 `draft=None`(不可提交、驳回数 0),详情看到回退找来的那一版。

    新数据不受影响:`publish_service.enqueue` 一定会把 `draft_id` 钉上。
    要彻底收敛得把回退也批量化(一次 `IN` 取商品 + 一次按 (spu, channel,
    site) 取草稿),留给 A-01~A-06 那批一起做,那里正在动同一类取数。"""
    submittable = bool(
        draft is not None
        and draft.status in publish_service.SUBMITTABLE_DRAFT_STATUSES
    )
    return pv.PublishFacts(
        listing_status=listing.status if listing else None,
        external_spu_id=listing.external_spu_id if listing else None,
        simulated=is_simulated_external_id(listing.external_spu_id) if listing else False,
        draft_submittable=submittable,
        attempt_status=attempt.status if attempt else None,
        outbox_status=outbox.status if outbox else None,
        unresolved_rejections=len(rejections),
        rejection_auto_closeable=_auto_closeable(rejections),
    )


def _facts(session: Session, listing: ChannelListing | None, draft) -> pv.PublishFacts:
    """四个来源 -> 判定层的输入。单行路径(详情、提交、轮询)的取数口。"""
    attempt = _latest_attempt(session, listing.id) if listing else None
    outbox = _outbox_for(session, attempt.id if attempt else None)
    rejections = _unresolved_rejections(session, draft)
    return _assemble_facts(listing, draft, attempt, outbox, rejections)


# ================================================================ 序列化


def _attempt_out(attempt: PublishAttempt | None) -> dict[str, Any] | None:
    """6.4「发布中」那一段:提交时间 / attempt / request ID / 当前状态。

    `safe_request_snapshot` 已经脱敏(`publish_service._safe_snapshot`),
    直接给前端。不脱敏的原文任何时候都不该离开后端。
    """
    if attempt is None:
        return None
    return {
        "id": str(attempt.id),
        "operation": attempt.operation,
        "status": attempt.status,
        "idempotency_key": attempt.idempotency_key,
        "request_fingerprint": attempt.request_fingerprint,
        "provider_request_id": attempt.provider_request_id,
        "started_at": iso_utc(attempt.started_at),
        "finished_at": iso_utc(attempt.finished_at),
        "error_code": attempt.error_code,
        "error_message": attempt.error_message,
        "request_snapshot": attempt.safe_request_snapshot,
        "response_snapshot": attempt.safe_response_snapshot,
    }


def _outbox_out(outbox: PublishOutbox | None) -> dict[str, Any] | None:
    if outbox is None:
        return None
    return {
        "status": outbox.status,
        "attempt_count": outbox.attempt_count,
        "next_attempt_at": (
            iso_utc(outbox.next_attempt_at)
        ),
        "last_error": outbox.last_error,
    }


def _listing_out(listing: ChannelListing, view: pv.PublishView) -> dict[str, Any]:
    """6.4「发布后」那一段 + 判定结果。

    `simulated` 单独给一个布尔而不是塞进状态文案里:4.1 节 F 要求
    「真实渠道 / Simulator」在关键页面**可见**,而可见的前提是它是一个
    前端能拿来做样式的字段,不是一句需要人去读的话。
    """
    return {
        "listing_id": str(listing.id),
        "product_id": str(listing.product_id),
        "channel": listing.channel,
        "site": listing.site,
        "shop_id": listing.shop_id,
        "status": listing.status,
        "external_spu_id": listing.external_spu_id,
        "external_sku_ids": listing.external_sku_ids,
        "simulated": is_simulated_external_id(listing.external_spu_id),
        "last_platform_status": listing.last_platform_status,
        "last_polled_at": (
            iso_utc(listing.last_polled_at)
        ),
        "next_poll_at": (
            iso_utc(listing.next_poll_at)
        ),
        "poll_attempt_count": listing.poll_attempt_count,
        "test_batch_tag": listing.test_batch_tag,
        "created_at": iso_utc(listing.created_at),
        **view.as_dict(),
    }


def _rejection_out(row: PlatformRejection) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "reason_code": row.reason_code,
        "platform_note": row.platform_note,
        "status": row.status,
        "located_by": row.located_by,
        "created_at": iso_utc(row.created_at),
    }


# ================================================================ 提交


class SubmitIn(BaseModel):
    shop_id: str = Field(..., min_length=1, max_length=64)
    #: 不给就由 `publish_policy.resolve_operation` 按有没有外部 ID 推。
    #: 显式给是为了 DELIST 和 REPUBLISH —— 那两个推不出来
    operation: PublishOperation | None = None
    dry_run: bool = False
    #: 人工测试期间**务必带上**。不带的话商品仍然进得了清理清单(靠 shop_id
    #: 过滤),但分不出是哪一轮测试建的,而 4.1 节 H 要的恰恰是"本轮建了哪些"
    test_batch_tag: str | None = Field(None, max_length=64)


@router.post("/products/{product_id}/submit")
def submit(
    product_id: UUID,
    payload: SubmitIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """提交上架。干跑时只构造报文不发送(6.4「发布前」的干跑请求)。

    ## 返回里最重要的是 `notice`

    它回答的是「这次点击之后到底会不会发生一次真的提交」。四种结局里有两种
    看起来都是"已提交":沿用了在途的那一条,和沿用了**已经死掉**的那一条。
    后者再点多少次都不会有变化 —— 见 `publish_view.describe_enqueue`,
    以及 `EnqueueResult.reused_terminal` 的注释。
    """
    product = _product(session, product_id)
    draft = _draft_for_product(session, product, None)
    if draft is None:
        raise NotFoundError("还没有生成上架草稿,无法提交")

    # ---- 提交前与导出对等的两道闸(A45 独立审查 C-05) ----
    #
    # `export_gate` 有四道:存在 / 指纹 / 状态 / 受众;这里在此之前只有
    # `_assert_submittable` 那一道**只看存储状态**的。而 STALE 结论的落库
    # 恰恰不在提交路径上:读路径刻意 dry_run(GET 不许改业务状态),
    # 于是"改完受众直接点提交"会把旧受众内容排队发上平台。
    # DELIST 保持豁免 —— 它不发内容,`enqueue` 里的注释写明了理由;
    # 自动解析(operation 不传)永远不会解析出 DELIST。
    if str(payload.operation or "").upper() != "DELIST":
        is_stale, stale_changes = wb.refresh_draft(session, product, draft)
        if is_stale:
            heads = ";".join(c.message for c in stale_changes[:3]) or "上游数据已变化"
            raise ValidationError(
                f"草稿已过期,禁止提交:{heads}。请刷新草稿后重试",
                code=ErrorCode.DRAFT_STALE,
                http_status=409,
            )
        gate = audience_gate.draft_audience_gate(product.audience, draft.category_id)
        if not gate.ok:
            raise ValidationError(
                "受众校验未通过:" + ";".join(gate.problems),
                code=ErrorCode.INPUT_INVALID,
                http_status=409,
            )

    result = publish_service.enqueue(
        session,
        product=product,
        draft=draft,
        shop_id=payload.shop_id,
        actor=current_actor(request),
        requested_operation=payload.operation,
        dry_run=payload.dry_run,
        test_batch_tag=payload.test_batch_tag,
    )
    # enqueue 只 flush。事务归这里 —— 见模块顶部
    session.commit()

    notice = pv.describe_enqueue(
        reused=result.reused, reused_terminal=result.reused_terminal, dry_run=result.dry_run
    )
    view = pv.build_view(_facts(session, result.listing, draft))
    return {
        "listing": _listing_out(result.listing, view),
        "operation": result.operation.value,
        "idempotency_key": result.idempotency_key,
        "dry_run": result.dry_run,
        "reused": result.reused,
        "reused_terminal": result.reused_terminal,
        "notice": {
            "code": notice.code.value,
            "message": notice.message,
            "will_deliver": notice.will_deliver,
        },
        # 干跑的全部产出就是这份报文(6.4「发布前」)。非干跑也给,
        # 因为「我们到底发了什么」是出事之后第一个要问的问题
        "prepared_request": publish_service.safe_preview(result.prepared),
    }


# ================================================================ 清单与详情


@router.get("/listings")
def list_listings(
    session: Session = Depends(db_session),
    channel: str | None = Query(None),
    shop_id: str | None = Query(None),
    status: str | None = Query(None, description="按 ChannelListing.status 过滤"),
    test_batch_tag: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    """发布清单。每行都带完整判定结果 —— 列表页和详情页共用同一个判定。

    共用不是省代码,是 §3.4 那条「任何情况下不得出现两个数字」的同类问题:
    列表说"可提交"、点进去详情说"待修复",运营会以为是自己看错了。
    """
    # A-07:分页在数据库层完成。原来先物化全表再在 Python 切片,
    # 客户端要 20 条也会把全部 Listing 读进进程内存。
    conds = []
    if channel:
        conds.append(ChannelListing.channel == channel.strip().upper())
    if shop_id:
        conds.append(ChannelListing.shop_id == shop_id)
    if status:
        conds.append(ChannelListing.status == status.strip().upper())
    if test_batch_tag:
        conds.append(ChannelListing.test_batch_tag == test_batch_tag)

    total = session.scalar(
        select(func.count()).select_from(ChannelListing).where(*conds)
    ) or 0
    window = list(
        session.scalars(
            select(ChannelListing)
            .where(*conds)
            .order_by(desc(ChannelListing.created_at), ChannelListing.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )

    # A-08:每行的 draft / 最新 attempt / outbox / 未解决驳回改为按页批量取。
    # 原来逐行 4+ 次查询,页大小 100 时一次列表请求会打出几百条 SQL。
    listing_ids = [row.id for row in window]
    draft_ids = {row.draft_id for row in window if row.draft_id is not None}

    drafts: dict[UUID, ListingDraft] = {}
    if draft_ids:
        drafts = {
            d.id: d
            for d in session.scalars(
                select(ListingDraft).where(ListingDraft.id.in_(list(draft_ids)))
            )
        }

    # 每个 listing 的最新 attempt:一次取回本页全部 attempt,按
    # (listing, created_at desc, id desc) 排好序后在内存里各留第一条。
    # 排序口径必须与 `_latest_attempt` **逐字相同** —— 含 `id` 那个
    # tiebreaker。少了它,同事务写下的并列行由两条独立 SQL 各挑各的,
    # 列表和详情会给出两个状态(理由见 `_latest_attempt` 的注释)。
    latest_attempt: dict[UUID, PublishAttempt] = {}
    if listing_ids:
        for a in session.scalars(
            select(PublishAttempt)
            .where(PublishAttempt.channel_listing_id.in_(listing_ids))
            .order_by(
                PublishAttempt.channel_listing_id,
                desc(PublishAttempt.created_at),
                desc(PublishAttempt.id),
            )
        ):
            latest_attempt.setdefault(a.channel_listing_id, a)

    outboxes: dict[UUID, PublishOutbox] = {}
    attempt_ids = [a.id for a in latest_attempt.values()]
    if attempt_ids:
        outboxes = {
            o.publish_attempt_id: o
            for o in session.scalars(
                select(PublishOutbox).where(
                    PublishOutbox.publish_attempt_id.in_(attempt_ids)
                )
            )
        }

    # 未解决驳回按草稿分组。口径与 `_unresolved_rejections` 相同
    # (UNRESOLVED_REJECTION_STATUSES,含 FIXED_PENDING_EXPORT)。
    rejections_by_draft: dict[UUID, list[PlatformRejection]] = {}
    if draft_ids:
        for r in session.scalars(
            select(PlatformRejection).where(
                PlatformRejection.draft_id.in_(list(draft_ids)),
                PlatformRejection.status.in_(UNRESOLVED_REJECTION_STATUSES),
            )
        ):
            rejections_by_draft.setdefault(r.draft_id, []).append(r)

    items = []
    for listing in window:
        draft = drafts.get(listing.draft_id) if listing.draft_id else None
        attempt = latest_attempt.get(listing.id)
        facts = _assemble_facts(
            listing,
            draft,
            attempt,
            outboxes.get(attempt.id) if attempt else None,
            rejections_by_draft.get(draft.id, []) if draft else [],
        )
        items.append(_listing_out(listing, pv.build_view(facts)))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/listings/{listing_id}")
def get_listing(
    listing_id: UUID, session: Session = Depends(db_session)
) -> dict[str, Any]:
    """发布详情。6.4 的三段一次给全,前端不用再拼第二个请求。"""
    listing = _listing(session, listing_id)
    draft = _current_draft(session, listing)
    attempt = _latest_attempt(session, listing.id)
    outbox = _outbox_for(session, attempt.id if attempt else None)
    rejections = _unresolved_rejections(session, draft)
    view = pv.build_view(_facts(session, listing, draft))

    return {
        **_listing_out(listing, view),
        # 发布前
        "draft": (
            {
                "id": str(draft.id),
                "status": draft.status,
                "locale": draft.locale,
                "version": draft.version,
                "submittable": draft.status in publish_service.SUBMITTABLE_DRAFT_STATUSES,
            }
            if draft is not None
            else None
        ),
        # 发布中
        "latest_attempt": _attempt_out(attempt),
        "outbox": _outbox_out(outbox),
        # 发布后
        "unresolved_rejections": [_rejection_out(r) for r in rejections],
        "pollable_statuses": list(poll_service.pollable_statuses()),
    }


# ================================================================ 刷新与下架


@router.post("/listings/{listing_id}/poll")
def poll(listing_id: UUID, session: Session = Depends(db_session)) -> dict[str, Any]:
    """立刻问一次平台(6.4「发布后」的最近轮询 / 刷新)。

    走 `poll_one` 而不是 `poll_due`:后者只捡到期的、状态在
    `POLLABLE_STATUSES` 里的行,而人点刷新时要问的是"平台上现在到底还有
    没有这个商品",这个问题对已经 LISTED、甚至 DELISTED 的行同样成立。

    传 `SessionLocal` 而不是当前会话:`poll_one` 内部分段提交,理由同
    `publish_tasks.deliver_outbox` —— 一个事务不许跨越一次外部调用。
    """
    listing = _listing(session, listing_id)
    if not (listing.external_spu_id or "").strip():
        # 没有外部 ID = 那次提交从没到达平台。**不静默成功** ——
        # 静默成功会让界面显示"已刷新",而实际上一个字都没问出去。
        # 用 409 不用 404:记录是存在的,是它的状态还轮不到这一步
        raise ValidationError(
            "这条发布记录还没有外部商品 ID,平台上没有可查的目标。",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    # 当前会话在外部调用之前必须让开,否则连接会被占着等一次 HTTP 往返
    session.commit()
    answer = poll_service.poll_one(SessionLocal, listing_id)

    session.expire_all()
    listing = _listing(session, listing_id)
    draft = _current_draft(session, listing)
    view = pv.build_view(_facts(session, listing, draft))
    return {"answer": answer, "listing": _listing_out(listing, view)}


@router.post("/listings/{listing_id}/delist")
def delist(
    listing_id: UUID, request: Request, session: Session = Depends(db_session)
) -> dict[str, Any]:
    """下架。人工测试期间清理测试商品的唯一入口(6.4 / 4.1 节 H)。

    走 `enqueue(DELIST)`,不开近路 —— 理由见模块顶部。DELIST **不看草稿
    状态**:要下架的恰恰是那批草稿早已过期的商品,用一条"防止发错内容"的
    规则去挡一个根本不发内容的操作,会让清理在最需要它的时候执行不下去。
    """
    listing = _listing(session, listing_id)
    if not (listing.external_spu_id or "").strip():
        raise ValidationError(
            "这条发布记录还没有外部商品 ID,没有可下架的目标。",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    product = _product(session, listing.product_id)
    draft = _current_draft(session, listing)
    if draft is None:
        raise NotFoundError("找不到对应草稿,无法排队下架")

    result = publish_service.enqueue(
        session,
        product=product,
        draft=draft,
        shop_id=listing.shop_id,
        actor=current_actor(request),
        requested_operation=PublishOperation.DELIST,
        dry_run=False,
    )
    session.commit()

    notice = pv.describe_enqueue(
        reused=result.reused, reused_terminal=result.reused_terminal, dry_run=False
    )
    view = pv.build_view(_facts(session, result.listing, draft))
    return {
        "listing": _listing_out(result.listing, view),
        "notice": {
            "code": notice.code.value,
            "message": notice.message,
            "will_deliver": notice.will_deliver,
        },
    }


# ================================================================ 异常恢复(EX-04)
#
# 这两条端点补的是 A45-batch12-2 报告的 EX-04:CREATE 结果未知、或者退避
# 耗尽落 DEAD 之后,**运营手上一个可执行的动作都没有**。
#
# `publish_service.reconcile_unknown()` 从任务 18 起就写好了,而全仓唯一
# 的调用点是一条数据库测试 —— 也就是说那段安全的对账实现从来没有被接到
# 任何操作面上。DEAD 那一侧连服务函数都没有。
#
# 没有出口的代价不是"少两个按钮"。终态幂等复用(`_reuse()` 返回
# `reused_terminal=True`)会挡住一切用相同输入的重新提交,于是运营要把这件
# 商品弄上架,只剩下改草稿、换输入 —— 而那会**算出一把新的幂等键**,
# 也就是真正会在平台上多出一件商品的那条路。闸门没有出口时,人会自己找一条
# 更危险的路绕过去,而且他做的每一步看起来都很合理。


class RecoverIn(BaseModel):
    """两条恢复动作共用的入参。**说明必填。**

    与 `generation_service.retry_task(force=True)` 完全同一条理由:这是由人
    决定"再对外发一次请求"的动作,事后必须能回答是谁、依据什么。
    填不出来通常说明核对根本没做。
    """

    note: str = Field(min_length=1, max_length=500, description="人工核对结论 / 重投依据")


def _latest_attempt_in(
    session: Session, listing_id: UUID, statuses: set[str]
) -> PublishAttempt:
    """挑出最近一条落在指定状态里的尝试。

    按 attempt 而不是按 listing 定位,是因为幂等键挂在 attempt 上 ——
    而这两条动作的全部安全性都来自"复用原键"。
    """
    row = session.scalars(
        select(PublishAttempt)
        .where(
            PublishAttempt.channel_listing_id == listing_id,
            PublishAttempt.status.in_(sorted(statuses)),
        )
        .order_by(desc(PublishAttempt.created_at))
        .limit(1)
    ).first()
    if row is None:
        raise ValidationError(
            "这条发布记录上没有需要恢复的提交尝试",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    return row


@router.post("/listings/{listing_id}/reconcile")
def reconcile(
    listing_id: UUID,
    payload: RecoverIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """带**原幂等键**去平台确认那次结果未知的提交到底有没有到达(7.4 节)。

    这不是重发。`publish_service.reconcile_unknown()` 发的是同一份报文、
    同一把键,平台要么返回 409 带回既有 ID、要么返回它上一次的结果 ——
    两种都是确认,都不会多出商品。真正危险的是换一把键再发一次,而这条
    路径从头到尾没有算过新键。

    传 `SessionLocal` 而不是当前会话:`reconcile_unknown` 内部分段提交,
    理由同 `poll` —— 一个事务不许跨越一次外部调用。
    """
    listing = _listing(session, listing_id)
    attempt = _latest_attempt_in(
        session, listing.id, {PublishAttemptStatus.UNKNOWN.value}
    )
    attempt_id = attempt.id
    actor = current_actor(request)

    # 审计先落、先提交。外部调用之后再写的话,请求打出去而进程崩掉时
    # 库里不会留下"是谁让它发的"—— 而这正是这条动作唯一需要回答的问题
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="PublishAttempt",
        entity_id=attempt_id,
        payload={
            "action": "reconcile_unknown",
            "reused_idempotency_key": True,
            "note": payload.note.strip()[:500],
        },
    )
    # 当前会话在外部调用之前必须让开,否则连接会被占着等一次 HTTP 往返
    session.commit()

    answer = publish_service.reconcile_unknown(SessionLocal, attempt_id)

    session.expire_all()
    listing = _listing(session, listing_id)
    draft = _current_draft(session, listing)
    view = pv.build_view(_facts(session, listing, draft))
    return {"answer": answer, "listing": _listing_out(listing, view)}


@router.post("/listings/{listing_id}/redeliver")
def redeliver(
    listing_id: UUID,
    payload: RecoverIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """把一条已放弃自动投递(DEAD)的记录重新排队。**复用原 attempt 与原键。**

    与 `reconcile` 的分工是「知不知道」:那一条处理"不知道到没到",
    这一条处理**"明确知道没到"** —— 六次投递都收到了平台的错误响应。
    两者都不新建幂等键,所以都不会在平台上多出一件商品。

    重新排队之后由 beat 的 `deliver_outbox` 正常领走,退避与上限从零重算。
    """
    listing = _listing(session, listing_id)
    # 这里刻意**不限定 attempt 状态**:DEAD 的成因既可能是 FAILED(平台一直
    # 拒),也可能是 UNKNOWN 之后再没查清。判据交给 `redeliver_dead()` 去看
    # outbox 那一行 —— 它才是"还投不投"的权威表示
    attempt = _latest_attempt(session, listing.id)
    if attempt is None:
        raise ValidationError(
            "这条发布记录上没有提交尝试,没有可重新投递的目标",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    publish_service.redeliver_dead(
        session, attempt.id, actor=current_actor(request), note=payload.note
    )
    session.commit()

    draft = _current_draft(session, listing)
    view = pv.build_view(_facts(session, listing, draft))
    return {"listing": _listing_out(listing, view)}


# ================================================================ 清理预案


@router.get("/cleanup/inventory")
def cleanup_inventory(
    session: Session = Depends(db_session),
    test_batch_tag: str | None = Query(None),
    shop_id: str | None = Query(None),
    channel: str | None = Query(None),
    include_simulated: bool = Query(True),
) -> dict[str, Any]:
    """本轮测试在平台上建了哪些商品(4.1 节 H 第二条)。

    **必须给至少一个作用域参数**,否则 400。不限定的话这份清单会覆盖全部
    渠道的全部商品 —— 而运营会拿着它去平台后台逐条核对,一份混进了别人
    商品的清单核不上,然后就没人信它了。护栏在 `cleanup_service._require_scope`。

    `include_simulated=false` 是给真实清理用的:拿着一份混着 SIM- 的清单
    去平台后台,同样核不上。
    """
    report = cleanup_service.inventory(
        session,
        test_batch_tag=test_batch_tag,
        shop_id=shop_id,
        channel=channel,
        include_simulated=include_simulated,
    )
    return report.as_dict()
