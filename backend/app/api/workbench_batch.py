"""运营工作台阶段 3 接口(批量试运营,任务书 §6)。

    POST /api/workbench/batches/plan        计划预览:可执行 / 不可执行(FE-301)
    POST /api/workbench/batches             建批次并执行(FE-301/302)
    GET  /api/workbench/batches             批量任务中心列表(FE-302)
    GET  /api/workbench/batches/{id}        批次详情 + 全部条目(FE-302)
    POST /api/workbench/batches/{id}/retry  失败重试(FE-304)
    POST /api/workbench/batches/{id}/file   生成批量上架文件(幂等,§6.3)
    GET  /api/workbench/batches/{id}/file   下载批量上架文件 + 清单 zip(§6.3)
    POST /api/workbench/batches/{id}/file/regenerate
                                            作废重生成(admin,A-21 的 409 出口)
    GET  /api/workbench/receipts            幂等回执台账(§7.3 注的观测入口)
    GET  /api/workbench/exceptions          简化异常列表(FE-303)
    GET  /api/workbench/spus                SPU-SKU 聚合(FE-306)
    GET  /api/workbench/audit               操作审计查询(FE-307)
    GET  /api/workbench/import/template     固定模板下载(FE-305)
    POST /api/workbench/import/preview      导入预览 + 错误文件(FE-305)
    POST /api/workbench/import/commit       落库(FE-305)

## 为什么建批次和执行是同一个请求

M3 的属性识别是同步的(几毫秒),文案生成走模板生成器同样是同步的。
在这种前提下拆成"建批次 -> 轮询执行"只会多一层没人能观察的间接
(同 `api/attributes.py` 里那句话)。接真实异步模型后 `POST /batches`
应当只建不跑,由 worker 消费 PENDING 条目 —— 届时 `run_batch` 原样搬进
task 即可,判定层完全不用动。

`?run=false` 已经预留了"只建不跑"的入口,给验收时构造 PENDING 状态用。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import current_actor, db_session, require_admin, require_operator
from app.attributes import service as attr_service
from app.core.clock import iso_utc
from app.core.config import settings
from app.core.enums import AuditAction
from app.core.errors import ErrorCode, ValidationError
from app.core.http_headers import download_headers
from app.core.search import ESCAPE_CHAR, like_pattern
from app.models.attribute import ProductAttributeValue
from app.models.audit_log import AuditLog
from app.models.listing_copy import ListingCopy, ListingDraft
from app.models.listing_image import ListingImageSet
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.services import audit, product_service
from app.services.product_import import parse_csv
from app.workbench import audit_view, import_plan
from app.workbench import batch as rules
from app.workbench import batch_service as bs
from app.workbench import exceptions as exc_rules
from app.workbench import spu as spu_rules
from app.workbench.batch import BatchAction

router = APIRouter(
    prefix="/workbench", tags=["workbench-batch"], dependencies=[Depends(require_operator)]
)

#: 导入文件上限。与 `/api/products/import` 保持同一个数字 ——
#: 两条路径给出不同的上限,运营会以为是随机失败。
MAX_IMPORT_BYTES = 5 * 1024 * 1024

# ---------------------------------------------------------------- 派发状态
#
# `dispatched: bool` 一个字段背了三种意思(报告 6.18「字段过载」):
#
#   False = 同步模式,已经跑完了          → 前端应当直接展示统计
#   False = 异步模式,但投递失败了        → 前端应当提示"没人会来跑,点重试"
#   True  = 异步模式,已排队              → 前端应当开始轮询
#
# 前端拿一个布尔值分不出这三种,于是"创建后直接展示执行统计"和"不轮询"
# 两个前端缺陷有一半根因在这里。布尔留着不动(兼容既有调用方),
# 另给一个说得清楚的枚举。
DISPATCH_INLINE_DONE = "INLINE_DONE"
DISPATCH_QUEUED = "QUEUED"
DISPATCH_FAILED = "DISPATCH_FAILED"
DISPATCH_NOT_REQUESTED = "NOT_REQUESTED"


def _storage():
    """对象存储句柄。与 `api/workbench.py` 里的同名函数同一份构造。"""
    # 函数内 import 是刻意保留的吗?不是 —— 但改成模块级要连 storage 一起挪,
    # 而 `app.services.storage` 在导入期会碰文件系统。这里只把 settings 提到
    # 模块级:`api/deps.py` 顶部记着的那个坑(函数内 import 拿到的不是模块属性,
    # 测试里 monkeypatch 打不到)对它同样成立。
    from app.services.storage import build_storage

    return build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )


def _action(raw: str) -> BatchAction:
    try:
        return BatchAction(raw)
    except ValueError as exc:
        allowed = "、".join(a.value for a in BatchAction)
        raise ValidationError(
            f"不支持的批量动作 {raw!r};可用:{allowed}",
            code=ErrorCode.INPUT_INVALID,
        ) from exc


# ==========================================================
# 批量动作(FE-301 / 302 / 304)
# ==========================================================


class BatchPlanIn(BaseModel):
    action: str = Field(description="EXTRACT / GENERATE_COPY / VALIDATE_DRAFT / EXPORT")
    product_ids: list[UUID] = Field(min_length=1)
    include_exported: bool = Field(
        default=False, description="导出动作:是否包含已导出且未过期的商品"
    )


class BatchCreateIn(BatchPlanIn):
    label: str | None = Field(default=None, max_length=120, description="批次备注名")


class BatchRetryIn(BaseModel):
    item_ids: list[UUID] | None = Field(
        default=None, description="不传 = 重试全部可重试项"
    )


def _plan_out(plan: rules.BatchPlan) -> dict[str, Any]:
    """FE-301 的验收结果:执行前显示可执行 / 不可执行数量。

    不止两个数字:`blocked` 逐条带上原因与跳转目标,否则"12 件不可执行"
    只会引出一句"为什么",而运营得自己去 12 个详情页里找。
    """
    return {
        "action": plan.action.value,
        "action_label": rules.BATCH_ACTION_LABELS[plan.action],
        "executable_count": plan.executable_count,
        "blocked_count": plan.blocked_count,
        "by_code": plan.by_code,
        "executable": [
            {"product_id": i.product_id, "sku": i.sku, "spu": i.spu}
            for i in plan.executable
        ],
        "blocked": [
            {
                "product_id": i.product_id,
                "sku": i.sku,
                "spu": i.spu,
                "code": str(i.code),
                "code_label": rules.error_meta(str(i.code)).label,
                "reason": i.reason,
                "advice": rules.error_meta(str(i.code)).advice,
                "target_step": None if i.target_step is None else i.target_step.value,
                "target_ref": i.target_ref,
            }
            for i in plan.blocked
        ],
    }


@router.post("/batches/plan")
def plan_batch(
    payload: BatchPlanIn, session: Session = Depends(db_session)
) -> dict[str, Any]:
    """只算不做:**不建批次、不执行任何动作**,运营可以反复点。

    唯一的写入是判定本身的副作用 —— `collect()` 发现草稿过期时会把它标成
    STALE(和商品列表页一样)。这个结论要落库:过期草稿禁止导出靠它。
    刻意不 rollback,否则预览与列表页对同一件商品会给出不同的草稿状态。
    """
    plan = bs.plan(
        session,
        _action(payload.action),
        payload.product_ids,
        include_exported=payload.include_exported,
    )
    out = _plan_out(plan)
    session.commit()
    return out


@router.post("/batches")
def create_batch(
    payload: BatchCreateIn,
    request: Request,
    run: bool = Query(default=True, description="false = 只建批次不执行"),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """建批次(FE-301/302)。

    评审第 2 条:批次**先落库并提交**,再决定谁来执行。

        BATCH_EXECUTION_MODE=celery   提交后投给 worker,立刻返回 batch_id。
                                      前端轮询 `GET /batches/{id}`
        BATCH_EXECUTION_MODE=inline   在本请求内执行,但**每件一个独立事务**
                                      (run_batch 的 commit_each)

    两种模式下"批次已创建"都不再依赖执行是否跑完:请求断在半路时,
    已经完成的条目、回执和审计都已落盘,而不是连同已经计费的外部调用
    一起回滚。

    ## `Idempotency-Key`(A45-batch12-2 / EX-06)

    带这个头时,同键同入参**返回第一次创建的那个批次**,不建第二个;
    同键不同入参报 409。不带头时行为一字不变。

    它补的是这条链路上一直缺的一层:`batch_action_receipts` 挡的是"同一商品
    同一动作被跑两遍"(批次内部),挡不住"同一个创建请求被受理两遍" ——
    而运营双击、以及第一次响应在回程丢失后客户端重发,都是后者。表现是
    批次中心里出现两个逻辑相同的任务、两份进度、两套审计,而没有任何依据
    判断哪一个有效。

    读头而不是读 body:这是 HTTP 层的语义(RFC draft 的 `Idempotency-Key`),
    放进 body 会让它看起来像一个业务参数,而它不是 —— 它描述的是这次**传输**,
    不是这次请求要做的事。
    """
    from app.core.config import settings

    actor = current_actor(request)
    action = _action(payload.action)
    job = bs.create_batch(
        session,
        action=action,
        product_ids=payload.product_ids,
        actor=actor,
        label=payload.label,
        include_exported=payload.include_exported,
        request_key=request.headers.get("Idempotency-Key"),
    )
    # A45-batch12-2 / EX-06:同一把键的重发**什么都不做**,原样返回第一次的结果。
    #
    # 不能往下走去执行:那正是这条幂等要挡的事 —— 双击的第二次会把同一个批次
    # 再跑一遍,而"跑一遍"意味着重新领条目、重新走一遍付费判定。回执那一层
    # 确实还挡着重复的模型调用,但依赖下一层兜底就等于这一层没做。
    if getattr(job, "reused_request", False):
        out = bs.job_out(session, job, with_items=True)
        out["dispatched"] = False
        out["dispatch_state"] = DISPATCH_NOT_REQUESTED
        out["reused_request"] = True
        session.rollback()
        return out

    # 先提交:批次和它的全部条目从这一刻起是既成事实
    session.commit()
    batch_id = job.id

    mode = (settings.BATCH_EXECUTION_MODE or "inline").strip().lower()
    if run and mode == "celery":
        # 返回值不能丢(评审第 4 条)。Redis / broker 不可用时 `send_batch`
        # 返回 False,而原来这里无条件写 True —— 前端显示"已提交",批次却
        # 永远停在 PENDING,运营无从判断是跑得慢还是根本没派出去。
        #
        # 投递失败**不是创建失败**:批次和它的条目已经提交,那批 PENDING 行
        # 本身就是意图记录(见 `dispatch_service.send_batch` 的说明),点一次
        # 重试就能重新执行。所以这里如实上报,而不是抛错让运营再建一个批次。
        dispatched = _dispatch_batch(str(batch_id), actor)
        out = bs.job_out(session, bs.get_job(session, batch_id), with_items=True)
        out["dispatched"] = dispatched
        out["dispatch_state"] = DISPATCH_QUEUED if dispatched else DISPATCH_FAILED
        out["reused_request"] = False
        session.rollback()
        return out

    if run:
        bs.run_batch(session, job, actor=actor)
    out = bs.job_out(session, bs.get_job(session, batch_id), with_items=True)
    out["dispatched"] = False
    out["dispatch_state"] = DISPATCH_INLINE_DONE if run else DISPATCH_NOT_REQUESTED
    out["reused_request"] = False
    session.commit()
    return out


def _dispatch_batch(batch_id: str, actor: str) -> bool:
    """投递入口。真正碰队列的是 `dispatch_service` —— 全仓只有它可以
    (见那边 `send_batch` 的说明)。"""
    from app.services import dispatch_service

    return dispatch_service.send_batch(batch_id, actor)


@router.get("/batches")
def list_batches(
    action: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    jobs = bs.list_jobs(session, limit=limit, action=action)
    # A-32:条目一次取回。逐批次调 `job_out` 的话,30 个批次 = 30 次条目全查
    return {"items": bs.jobs_out(session, jobs)}


@router.get("/batches/{batch_id}")
def get_batch(
    batch_id: UUID, session: Session = Depends(db_session)
) -> dict[str, Any]:
    job = bs.get_job(session, batch_id)
    return bs.job_out(session, job, with_items=True)


@router.post("/batches/{batch_id}/retry")
def retry_batch(
    batch_id: UUID,
    payload: BatchRetryIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """重试失败项(FE-304)。

    **和创建批次走同一套执行模式**(BLOCK-03)。原来这里无视
    `BATCH_EXECUTION_MODE`,一律在 HTTP 请求里同步跑完 —— 于是同一个批次
    创建时交给 worker、重试时却占着请求线程,而重试恰恰是上一次跑挂之后
    走的那条路,条目更多也更慢。请求超时后前端提示失败,后台还在继续跑,
    运营再点一次就是第二遍。
    """
    from app.core.config import settings

    actor = current_actor(request)
    job = bs.get_job(session, batch_id)

    mode = (settings.BATCH_EXECUTION_MODE or "inline").strip().lower()
    if mode == "celery":
        # 先重置并**提交**:这批 PENDING 行本身就是意图记录,投递失败也丢不了
        bs.reset_items_for_retry(session, job, actor=actor, item_ids=payload.item_ids)
        session.commit()
        dispatched = _dispatch_batch(str(batch_id), actor)
        out = bs.job_out(session, bs.get_job(session, batch_id), with_items=True)
        out["dispatched"] = dispatched
        out["dispatch_state"] = (
            DISPATCH_QUEUED if dispatched else DISPATCH_FAILED
        )
        session.rollback()
        return out

    bs.retry_items(session, job, actor=actor, item_ids=payload.item_ids)
    out = bs.job_out(session, job, with_items=True)
    out["dispatched"] = False
    out["dispatch_state"] = DISPATCH_INLINE_DONE
    session.commit()
    return out


@router.get("/batches/{batch_id}/file-audit")
def batch_file_audit(
    batch_id: UUID, session: Session = Depends(db_session)
) -> dict[str, Any]:
    """OPS-REVIEW P4:这个批次的压缩包现在还能不能传平台。

    在下载按钮**旁边**回答,而不是等运营点下去撞一个 409:
    过期草稿会被 `batch_file` 里的导出闸拦住,而那条错误消息说的是
    "草稿已过期",不会说"所以你手里那个上周的压缩包也别传了"。

    A6:这个 GET **不写库**。过期口径仍然是 `service.refresh_draft` 那一份
    (P4 的约束:不许批次页自己再推断一遍),只是走它的 dry-run 模式:
    同一份判定逻辑,只算不写。要把 STALE 结论落库,由列表页/详情页的
    既有写路径完成 —— 浏览器预取、重试或刷新不该改变业务状态。
    """
    job = bs.get_job(session, batch_id)
    out = bs.file_audit(session, job, dry_run=True)
    # 故意不 commit:这条路径不产生任何写入,commit 只会掩盖将来引入的副作用
    session.rollback()
    return out


@router.post("/batches/{batch_id}/file")
def generate_batch_file(
    batch_id: UUID, request: Request, session: Session = Depends(db_session)
) -> dict[str, Any]:
    """生成这个批次的上架文件(评审第 6 条)。**幂等:已生成则原样返回。**

    生成是一个写动作,所以它是 POST。原来生成藏在下载的 GET 里,
    浏览器预取和重复点击都会重新产出字节并累加 `export_count`。
    """
    job = bs.get_job(session, batch_id)
    meta = bs.ensure_batch_export(
        session, job, actor=current_actor(request), storage=_storage()
    )
    session.commit()
    return meta


class RegenerateFileIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


@router.post(
    "/batches/{batch_id}/file/regenerate",
    dependencies=[Depends(require_admin)],
)
def regenerate_batch_file(
    batch_id: UUID,
    payload: RegenerateFileIn,
    request: Request,
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """作废旧的上架文件、重新生成一份(A-21)。**这是那两个 409 的出口。**

    ## 为什么单独一个动词,而不是让 POST /file 变得不幂等

    `POST /file` 的幂等是评审第 6 条的核心承诺:已经生成过就返回旧的那一份。
    把"重新生成"塞进去等于取消那个承诺,而它正是防住重复点击、浏览器预取、
    前端超时重试的那道闸。两件事就该有两个动词。

    ## 为什么挂 require_admin

    重新生成会换掉 `file_sha256`,而它是平台驳回时"我当时传的是这一版"的
    唯一物证。换掉它是有代价的动作,不该和"点一下下载"共用门槛。
    `reason` 必填,连同 `旧sha → 新sha` 一起进审计。
    """
    job = bs.get_job(session, batch_id)
    meta = bs.regenerate_batch_export(
        session,
        job,
        actor=current_actor(request),
        storage=_storage(),
        reason=payload.reason,
    )
    session.commit()
    return meta


@router.get("/batches/{batch_id}/file")
def download_batch_file(
    batch_id: UUID, request: Request, session: Session = Depends(db_session)
) -> Response:
    """下载已生成的 zip。清单负责"定位到具体商品和原始批次"(§6.3)。

    评审第 6 条:这条路径**只读字节**。不生成文件、不改 `export_count`、
    不写任何一行 —— 浏览器重试、预取、监控探活因此不再改变业务状态。
    还没生成过时报 409 并提示先点生成,而不是顺手生成一份。
    """
    job = bs.get_job(session, batch_id)
    payload, filename, mime = bs.read_batch_file(session, job, storage=_storage())

    # 第 6 条要求"生成、下载分别记录事件,下载不得增加 export_count"。
    # 所以这条 GET 确实写一行 —— 但写的是**只增不改的审计流水**,
    # 不是业务状态:`export_count`、草稿状态、平台状态一个都不动。
    # 与第 19 条不冲突:那一条针对的是"读接口改业务状态",
    # 而"谁在什么时候下载了哪份文件"本来就只能在下载发生时记下来。
    audit.record(
        session,
        actor=current_actor(request),
        action=AuditAction.READ,
        entity_type="BatchJob",
        entity_id=job.id,
        payload={
            "action": "download_export_file",
            "file_name": filename,
            "file_sha256": job.file_sha256,
        },
    )
    session.commit()
    return Response(
        content=payload,
        media_type=mime,
        headers=download_headers(filename),
    )


@router.get("/receipts")
def receipt_ledger(
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """幂等回执台账。**验收"不重复触发付费模型调用"就看这一页。**

    `paid_call_anomaly` 为真的行意味着同一动作、同一作用域、同一上游指纹
    被真的执行了两次 —— 那正是 §7.3 里那个 P0。
    """
    return {"items": bs.receipt_ledger(session, limit=limit)}


# ==========================================================
# 简化异常列表(FE-303)
# ==========================================================


@router.get("/exceptions")
def list_exceptions(
    include_batch: bool = Query(default=True, description="是否并入批次执行失败"),
    search: str | None = Query(default=None),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """按素材/属性/图片集/文案/草稿分类的异常。

    问题条目全部来自 `wb.collect` 的判定结论,和列表页徽标读同一份 ——
    这一页的件数与列表页的阻断数因此不可能对不上(§3.4)。
    """
    stmt = select(Product).order_by(Product.updated_at.desc())
    if search:
        like = like_pattern(search)
        stmt = stmt.where(
            or_(
                Product.spu.ilike(like, escape=ESCAPE_CHAR),
                Product.sku.ilike(like, escape=ESCAPE_CHAR),
                Product.name.ilike(like, escape=ESCAPE_CHAR),
            )
        )
    products = list(session.scalars(stmt))

    entries: list[exc_rules.ExceptionEntry] = []
    for product, ctx in bs.iter_flow_contexts(session, products, dry_run=True):
        entries.extend(
            exc_rules.entries_from_flow(
                str(product.id), product.sku, product.spu, ctx.result
            )
        )
    session.rollback()  # 评审第 19 条:异常列表是只读的,判定不落库

    if include_batch:
        for row in bs.recent_failures(session):
            entry = exc_rules.entry_from_batch_row(row)
            if entry is not None:
                entries.append(entry)

    sections = exc_rules.group(entries)
    return {
        "summary": exc_rules.summarize(sections),
        "sections": exc_rules.serialize(sections),
    }


# ==========================================================
# SPU 聚合(FE-306)
# ==========================================================


@router.get("/spus")
def list_spus(
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    limit: int | None = Query(
        default=None,
        deprecated=True,
        description="旧参数,等价于 page_size。留着是为了不让旧前端静默换页宽",
    ),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """按 SPU 展开 SKU 与颜色/尺码,并报出本该一致却不一致的公共属性。

    属性取**已确认值**:未确认的建议值不参与一致性判定,否则刚跑完识别的
    商品会整页发红(理由见 `spu.py`)。

    ## 翻页在 SQL 上,不在 Python 里(A-01 ～ A-05)

    改之前这一段是四重截断叠在一起,而且**每一重都不报错**:

        1  前端硬编码 `limit: 100`、`pagination={false}`
        2  后端 `groups = aggregate(rows)[:limit]` 先截断
        3  `total` 取截断后的 `len(groups)` -> `total` 恒 <= limit
        4  `inconsistent_spus` 同样只数截断后的那几组

    合起来的表现是:第 101 个 SPU 起在界面上**不存在**,而页面上那句
    「共 100 个 SPU」和红色的「N 个 SPU 有属性不一致」都显得完全正常。
    超过 100 个 SPU 时,这一页会以正常的样子给出错误答案 —— 比报错危险。

    现在的口径:

        total              COUNT(DISTINCT spu),与翻页无关
        inconsistent_spus  全量算,与翻页无关(见 `_inconsistent_spu_count`)
        items              只有本页这些 SPU 的商品才跑 `collect()`

    翻页键是 `spu` 单列,不需要 A-44 那样的 tiebreaker:这条查询按 `spu`
    分组,分组键本身就是唯一的,不存在并列。

    ## 为什么 `collect()` 只跑本页(A-05)

    改之前先物化**全部**商品,逐件跑 `iter_flow_contexts`(每件内部若干次
    库读),最后才 `[:limit]` —— 被丢掉的那些商品的库读一次没省。
    真正贵的是 `collect()`,所以它必须跟着页走。
    """
    if limit is not None:
        # 旧前端只会传 limit。落到 page_size 上而不是忽略它,否则升级后
        # 缓存里那份 bundle 会从 100 行悄悄变成 50 行,又是一个不报错的错答案
        page_size = max(1, min(limit, 200))

    filters = []
    if search:
        like = like_pattern(search)
        filters.append(
            or_(
                Product.spu.ilike(like, escape=ESCAPE_CHAR),
                Product.name.ilike(like, escape=ESCAPE_CHAR),
            )
        )

    # 注意:搜索命中的是**商品**。一个 SPU 只要有一个 SKU 的名称命中就进结果,
    # 而该组里只装命中的那几个 SKU —— 这是改动前就有的语义,原样保留。
    spu_stmt = select(Product.spu).where(*filters).group_by(Product.spu)
    total = session.scalar(select(func.count()).select_from(spu_stmt.subquery())) or 0
    page_spus = list(
        session.scalars(
            spu_stmt.order_by(Product.spu)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )

    inconsistent_spus = _inconsistent_spu_count(session, filters)

    products = (
        list(
            session.scalars(
                select(Product)
                .where(Product.spu.in_(page_spus), *filters)
                .order_by(Product.spu, Product.sku)
            )
        )
        if page_spus
        else []
    )
    # A-06:属性一次取回。改之前是循环体里逐商品一次 `effective_map`,
    # 也就是 N+1 —— 而 N 是本页全部 SKU 数,不是 SPU 数
    attributes = _confirmed_attributes_bulk(session, [str(p.id) for p in products])

    rows: list[tuple[str, str, spu_rules.SkuRow]] = []
    for product, ctx in bs.iter_flow_contexts(session, products, dry_run=True):
        rows.append(
            (
                product.spu,
                product.name,
                spu_rules.SkuRow(
                    product_id=str(product.id),
                    sku=product.sku,
                    # 硬规则 4:主键由后端给,前端不许拿 `spu` 字符串码去凑一个。
                    # 老建档路径建的行这一列仍可能是 NULL —— 那时给 None,
                    # 而不是给一个"看起来像 id"的东西
                    spu_id=str(product.spu_id) if product.spu_id else None,
                    name=product.name,
                    color=product.primary_color or None,
                    size=product.size,
                    attributes=attributes.get(str(product.id), {}),
                    completion=ctx.result.completion,
                    blocking_count=ctx.result.blocking_count,
                    next_action_label=ctx.result.next_action.label,
                ),
            )
        )
    session.rollback()  # 评审第 19 条:SPU 聚合是只读的

    groups = spu_rules.aggregate(rows)
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "inconsistent_spus": inconsistent_spus,
        "items": spu_rules.serialize(groups),
    }


# ==========================================================
# 操作审计查询(FE-307)
# ==========================================================


@router.get("/audit")
def query_audit(
    product_id: UUID | None = Query(default=None, description="按商品查(含其 SPU 下的对象)"),
    actor: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    action: str | None = Query(default=None, description="业务动作,如 export / approve"),
    days: int = Query(default=30, ge=1, le=365),
    workbench_only: bool = Query(
        default=True, description="只看上架闭环相关对象,默认开(降噪)"
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(db_session),
) -> dict[str, Any]:
    """审计查询。**写入在阶段 2 由 BE-206 完成,这里只读。**

    `product_id` 会展开成"这件商品 + 它的属性值 + 它 SPU 下的图片集/文案/草稿"
    的 entity_id 集合 —— 运营问的是"这件商品都被谁改过",而审计表按对象记录,
    一件商品的历史散在五张表的 id 上。不展开的话这个筛选等于没用。
    """
    since = datetime.now(UTC) - timedelta(days=days)
    stmt = select(AuditLog).where(AuditLog.created_at >= since)

    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    elif workbench_only:
        stmt = stmt.where(
            AuditLog.entity_type.in_(list(audit_view.WORKBENCH_ENTITY_TYPES))
        )
    if actor:
        stmt = stmt.where(
            AuditLog.actor.ilike(like_pattern(actor), escape=ESCAPE_CHAR)
        )

    if product_id is not None:
        ids = _related_entity_ids(session, product_id)
        stmt = stmt.where(AuditLog.entity_id.in_(ids))

    if action:
        # 评审第 18 条:业务动作过滤下推到数据库。
        #
        # 原来是先 `.limit(2000)` 再在 Python 里按 action 过滤 —— 于是
        # 两件事同时成立:超出最近 2000 条的匹配记录**永远查不到**
        # (而按动作查审计的人要的恰恰是"三个月前那次导出"),
        # `total` 数的也只是这 2000 条里的匹配数,分页跟着一起错。
        #
        # 判据仍然是 `audit_view.business_action` 那一份:payload 里有
        # `action` 就用它,否则退回数据库列。这里把那句话翻译成 SQL,
        # 而不是在 Python 里重写一遍逻辑 —— 两处各写一份的下场见 §3.4。
        payload_action = AuditLog.payload["action"].astext
        stmt = stmt.where(
            or_(
                payload_action == action,
                and_(
                    or_(payload_action.is_(None), payload_action == ""),
                    AuditLog.action == action,
                ),
            )
        )

    total = session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ) or 0
    page_rows = list(
        session.scalars(
            stmt.order_by(AuditLog.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": str(r.id),
                "at": iso_utc(r.created_at),
                "actor": r.actor,
                "action": r.action,
                "business_action": audit_view.business_action(r.action, r.payload),
                "action_label": audit_view.business_action_label(r.action, r.payload),
                "entity_type": r.entity_type,
                "entity_label": audit_view.entity_label(r.entity_type),
                "entity_id": str(r.entity_id) if r.entity_id else None,
                "request_id": r.request_id,
                "summary": audit_view.describe(
                    actor=r.actor,
                    action=r.action,
                    entity_type=r.entity_type,
                    payload=r.payload,
                ),
                "payload": r.payload or {},
            }
            for r in page_rows
        ],
    }


def _display_attribute_value(row: ProductAttributeValue) -> str | None:
    """属性行 -> 显示用字符串。空值返回 None(调用方跳过)。

    单独抽出来是因为逐件版与批量版必须**逐字节**给出同一个字符串:
    SPU 聚合的"不一致"判定就是拿这个字符串做相等比较的,两处各写一份
    的表现是同一个 SPU 在列表页一致、在批量口径里不一致(§3.4 拦的正是这个)。
    """
    value = row.normalized_value or row.value_json
    if value in (None, "", [], {}):
        return None
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _confirmed_attributes(session: Session, product_id: UUID) -> dict[str, str]:
    """该商品**已确认**的属性值,字段名 -> 显示用字符串。

    走 `attr_service.effective_map` 而不是自己拼 SQL:属性表按
    `owner_type` + `owner_id` 定位、并且只有 `is_current` 那一行算数
    (见 models/attribute.py 的部分唯一索引)。绕开它自己查一次,
    迟早会漏掉 `is_current` 而把历史版本一起算进"不一致"。
    """
    from app.core.enums import AttributeStatus

    out: dict[str, str] = {}
    for name, row in attr_service.effective_map(session, product_id).items():
        if row.status != AttributeStatus.CONFIRMED.value:
            continue
        value = _display_attribute_value(row)
        if value is None:
            continue
        out[name] = value
    return out


def _confirmed_attributes_bulk(session: Session, owner_ids) -> dict[str, dict[str, str]]:
    """一批商品的已确认属性,`str(product_id)` -> {字段名: 显示值}。**一条 SQL。**

    `owner_ids` 收字符串列表,也收一条子查询(`scalar_subquery()`)——
    全量口径要覆盖成千上万件商品,把 id 拉回 Python 再塞进 `IN` 就成了
    几万个绑定参数,让数据库自己算更便宜。

    ## 口径必须等于 `_confirmed_attributes`

    那个函数走 `effective_map(session, product_id)`(**不传 `product`**),
    于是它的查找表只有一项:`owner_type='SPU'` + `owner_id=str(product.id)`
    的存量位置,而 `REGISTRY` 那层过滤对 SPU 层不生效。所以这里的
    `where` 就是那一项的批量版,一个条件不多一个不少。

    A43 分层之后写入的新值挂在 `(SPU, product.spu)` / `(VARIANT, …)` 上,
    这一页**看不到** —— 那是改动前就存在的行为,不在本次范围内;要改的话
    两处要一起改,否则同一个属性在两个口径里出现两个值。
    """
    from app.core.enums import AttributeStatus, OwnerType

    rows = session.scalars(
        select(ProductAttributeValue).where(
            ProductAttributeValue.owner_type == OwnerType.SPU.value,
            ProductAttributeValue.owner_id.in_(owner_ids),
            ProductAttributeValue.is_current.is_(True),
            ProductAttributeValue.status == AttributeStatus.CONFIRMED.value,
        )
    )
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        value = _display_attribute_value(row)
        if value is None:
            continue
        out.setdefault(row.owner_id, {})[row.field_name] = value
    return out


def _inconsistent_spu_count(session: Session, filters: list[Any]) -> int:
    """有属性不一致的 SPU 有几个 —— **全量口径,与当前页无关**(A-03)。

    改之前这个数是从截断后的 groups 上数出来的,而前端把它当全局告警显示:
    「3 个 SPU 有属性不一致」在第 101 个 SPU 之后就不再更新,运营以为处理完了。

    ## 为什么不下推成一条 SQL

    判据是"同一 SPU 下某个公共字段出现了两个不同的**显示值**",而显示值
    要经过 `_display_attribute_value`(`normalized_value` 回落 `value_json`、
    非字符串走 `json.dumps(ensure_ascii=False)`)。SQL 里的 `jsonb ->> ''`
    对非字符串值给出的文本和 Python 的 `json.dumps` 并不逐字节相同 ——
    翻译一遍就等于把判据写成两份,某些属性值上两个数字会对不上。
    所以取数批量化、判定仍旧走 `spu_rules.aggregate` 这一份。

    代价是两条查询覆盖全量(属性行 + 商品的 id/spu/sku 三列),没有
    `collect()`、没有 ORM 关系加载。改动前是全量商品 × 每件若干次
    `collect()` 库读,所以这仍是数量级的下降。真要再省,应当在
    `spu_rules` 里加一个只判"有没有不一致"的快路径,而不是重写判据。
    """
    scoped_ids = select(cast(Product.id, String)).where(*filters).scalar_subquery()
    attributes = _confirmed_attributes_bulk(session, scoped_ids)
    if not attributes:
        # 一条已确认属性都没有 -> 不可能有不一致。省掉下面那次全表扫
        return 0

    rows = session.execute(
        select(Product.id, Product.spu, Product.sku).where(*filters)
    ).all()
    groups = spu_rules.aggregate(
        (
            spu,
            "",
            spu_rules.SkuRow(
                product_id=str(product_id),
                sku=sku,
                attributes=attributes.get(str(product_id), {}),
            ),
        )
        for product_id, spu, sku in rows
    )
    return sum(1 for g in groups if g.inconsistent)


def _related_entity_ids(session: Session, product_id: UUID) -> list[UUID]:
    """一件商品在审计表里可能出现的全部 entity_id。

    包括:商品自己、它的属性值行、它的素材、以及它 SPU 下的图片集/文案/草稿。
    SPU 级对象是同 SPU 的 SKU 共享的 —— 于是"这件商品的历史"里会出现
    兄弟 SKU 触发的操作,这是对的:那些操作确实影响了这件商品的上架内容。
    """
    product = session.get(Product, product_id)
    if product is None:
        return [product_id]

    ids: list[UUID] = [product_id]
    # 属性表按 owner_type + owner_id(商品主键的字符串)定位,没有 product_id 列
    ids.extend(
        session.scalars(
            select(ProductAttributeValue.id).where(
                ProductAttributeValue.owner_id == str(product_id)
            )
        )
    )
    ids.extend(
        session.scalars(select(MediaAsset.id).where(MediaAsset.product_id == product_id))
    )
    for model in (ListingImageSet, ListingCopy, ListingDraft):
        ids.extend(session.scalars(select(model.id).where(model.spu == product.spu)))
    # P1:驳回记录也按 SPU 关联
    from app.models.platform_rejection import PlatformRejection
    ids.extend(
        session.scalars(
            select(PlatformRejection.id).where(PlatformRejection.spu == product.spu)
        )
    )
    return ids


# ==========================================================
# CSV 导入最小版(FE-305)
# ==========================================================


@router.get("/import/template")
def import_template() -> Response:
    """固定模板。列名与 `product_import.ALL_COLUMNS` 同源,不另写一份。"""
    body = import_plan.template_csv().encode("utf-8-sig")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers=download_headers("product-import-template.csv"),
    )


@router.get("/import/columns")
def import_columns() -> dict[str, Any]:
    """列说明对照表。模板文件里不放中文表头,理由见 `import_plan.template_csv`。"""
    return {"columns": import_plan.column_help()}


async def _read_csv(file: UploadFile | None) -> str:
    if file is None:
        raise ValidationError("请上传 CSV 文件", code=ErrorCode.INPUT_INVALID)
    raw = await file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValidationError("导入文件超过 5 MB", code=ErrorCode.FILE_TOO_LARGE)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "导入文件必须是 UTF-8 编码的 CSV。Excel 里用「CSV UTF-8」另存",
            code=ErrorCode.INPUT_INVALID,
        ) from exc


@router.post("/import/preview")
async def preview_import(
    session: Session = Depends(db_session),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """落库前的预览 + 错误文件(FE-305)。**不写库。**

    已存在的 SKU 记为"跳过"而不是错误 —— 增量补充是批量导入的常态。

    它是全仓唯一一个不提交的写方法端点:用 POST 只因为要收一个文件体,
    语义上是读。`tests/pure/test_transaction_boundaries.py` 的白名单里
    单独列着它,**白名单同时要求这个函数里一处会话写都不许有** ——
    哪天它真开始写库,漏掉的提交会被那条用例当场抓住。
    """
    content = await _read_csv(file)
    parsed = parse_csv(content)
    skus = [row["sku"] for row in parsed.rows]
    existing = (
        set(session.scalars(select(Product.sku).where(Product.sku.in_(skus))))
        if skus
        else set()
    )
    return import_plan.serialize(import_plan.preview(content, existing))


@router.post("/import/commit")
async def commit_import(
    request: Request,
    session: Session = Depends(db_session),
    file: UploadFile | None = File(default=None),
) -> dict[str, Any]:
    """落库。**错误行不影响正确行**(FE-305 的验收结果)。

    逐行保存点在 `product_service.import_products` 里,这里只负责把
    错误行连同原始内容回带 —— 运营改完可以直接重传那份文件。
    """
    content = await _read_csv(file)
    parsed = parse_csv(content)
    skus = [row["sku"] for row in parsed.rows]
    existing = (
        set(session.scalars(select(Product.sku).where(Product.sku.in_(skus))))
        if skus
        else set()
    )
    view = import_plan.preview(content, existing)

    if view.blocked:
        # 表头都不对,一行也不该进库
        return {
            "committed": False,
            **import_plan.serialize(view),
            "created": 0,
            "skipped_existing": 0,
            "failed": len(view.header_problems),
        }

    result = product_service.import_products(
        session, parsed, actor=current_actor(request)
    )
    session.commit()
    return {
        "committed": True,
        **import_plan.serialize(view),
        "created": result["created"],
        "skipped_existing": result["skipped_existing"],
        "failed": result["failed"],
        "created_ids": [str(i) for i in result["created_ids"]],
    }
