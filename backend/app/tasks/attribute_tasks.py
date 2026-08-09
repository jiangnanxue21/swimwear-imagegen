"""属性识别的异步 worker、漏投恢复与卡死回收。"""
from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.attributes import run_state
from app.attributes import service as attr_service
from app.core.clock import utc_now
from app.core.enums import ExtractionRunStatus
from app.core.logging import get_logger
from app.core.redaction import safe_error_message
from app.db.session import SessionLocal
from app.extractors.registry import get_extractor
from app.models.attribute import ProductAttributeExtraction
from app.models.product import Product
from app.tasks.celery_app import celery_app

logger = get_logger(__name__)
S = ExtractionRunStatus

# 逐图 checkpoint 会刷新 updated_at。阈值覆盖最多素材数下的合法网络等待，
# 又能在 worker 消失后于人工测试可接受的时间内把 run 收回终态。
ATTRIBUTE_RUN_STALL_SECONDS = 3600


def _uuids(values: list[str] | None) -> list[UUID] | None:
    return None if values is None else [UUID(value) for value in values]


def _claim(
    session, run_id: UUID, *, allow_resume: bool = False
) -> ProductAttributeExtraction | None:
    now = utc_now()
    row = session.scalar(
        select(ProductAttributeExtraction)
        .where(ProductAttributeExtraction.id == run_id)
        .with_for_update()
    )
    if row is None or row.cancel_requested:
        session.rollback()
        return None
    if row.status == S.QUEUED.value:
        row.status = S.RUNNING.value
        row.started_at = now
    elif not (allow_resume and row.status == S.RUNNING.value):
        session.rollback()
        return None
    row.updated_at = now
    session.commit()
    return session.get(ProductAttributeExtraction, run_id)


def _persist_failure(run_id: UUID, exc: BaseException) -> None:
    """失败结论用新事务写，不能依赖已经 aborted 的执行事务。"""
    with SessionLocal() as session:
        row = session.get(ProductAttributeExtraction, run_id)
        if row is None or run_state.is_terminal(row.status):
            return
        row.status = S.FAILED.value
        row.error_code = type(exc).__name__[:48]
        row.error_message = safe_error_message(exc)
        row.finished_at = utc_now()
        session.commit()


@celery_app.task(
    name="attributes.run_extraction",
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def run_attribute_extraction(self, extraction_id: str) -> dict[str, str]:
    run_id = UUID(extraction_id)
    session = SessionLocal()
    try:
        row = _claim(session, run_id, allow_resume=bool(self.request.retries))
        if row is None:
            current = session.get(ProductAttributeExtraction, run_id)
            return {"id": extraction_id, "status": current.status if current else "MISSING"}

        product = session.get(Product, row.product_id)
        if product is None:
            raise RuntimeError("识别 run 对应的商品不存在")
        extraction = attr_service.run_extraction(
            session,
            product=product,
            extractor=get_extractor(row.extractor),
            fields=tuple(row.target_fields),
            # 排队时的真实白名单快照，而不是 worker 开始时重新扩大范围。
            only_media_ids=_uuids(row.input_asset_ids),
            only_variant_ids=_uuids(row.requested_variant_ids),
            spu_scope_id=row.spu_id,
            existing_run=row,
            cooperative=True,
            actor=row.requested_by,
        )
        if run_state.run_is_authoritative(extraction.status):
            attr_service.apply_evidence(
                session,
                product=product,
                extraction=extraction,
                actor=row.requested_by,
            )
        extraction.finished_at = utc_now()
        session.commit()
        return {"id": extraction_id, "status": extraction.status}
    except OperationalError:
        # 让 Celery 保留消息；同一任务重试时允许从 RUNNING checkpoint 继续。
        session.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        _persist_failure(run_id, exc)
        logger.exception(
            "attribute extraction worker failed",
            extra={"extra_fields": {"extraction_id": extraction_id}},
        )
        return {"id": extraction_id, "status": S.FAILED.value}
    finally:
        session.close()


@celery_app.task(name="maintenance.relay_attribute_extractions")
def relay_attribute_extractions(limit: int = 100) -> dict[str, int]:
    """补投仍在 QUEUED 的 run；重复消息由 `_claim` 的条件 UPDATE 吸收。"""
    from app.services import dispatch_service

    with SessionLocal() as session:
        ids = list(
            session.scalars(
                select(ProductAttributeExtraction.id)
                .where(ProductAttributeExtraction.status == S.QUEUED.value)
                .order_by(ProductAttributeExtraction.created_at)
                .limit(limit)
            )
        )
    sent = sum(dispatch_service.send_attribute_extraction(value) for value in ids)
    return {"queued": len(ids), "dispatched": sent}


@celery_app.task(name="maintenance.reap_attribute_extractions")
def reap_attribute_extractions(limit: int = 100) -> dict[str, int]:
    """把失去 worker 且心跳过期的 RUNNING run 收成 FAILED。"""
    cutoff = utc_now() - timedelta(seconds=ATTRIBUTE_RUN_STALL_SECONDS)
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(ProductAttributeExtraction)
                .where(
                    ProductAttributeExtraction.status == S.RUNNING.value,
                    ProductAttributeExtraction.updated_at < cutoff,
                )
                .order_by(ProductAttributeExtraction.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        now = utc_now()
        for row in rows:
            row.status = S.FAILED.value
            row.error_code = "ATTRIBUTE_WORKER_STALLED"
            row.error_message = "识别 worker 心跳超时；可重新发起识别"
            row.finished_at = now
        session.commit()
        return {"reaped": len(rows)}
