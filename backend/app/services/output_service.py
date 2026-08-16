"""成品图落库(需求第十五章 / 阶段 6)。

把 `formatting_service` 渲染出的多尺寸图写进存储和 `output_assets` 表。

两个不变量:

**重算不删旧图。** 网站可能还缓存着旧 URL,直接删会造成一批 404。
重算时把旧记录 `enabled=False` 并把新记录的 `generation` 加一,旧图仍在存储里可回溯。

**输出图也走内容寻址。** 和素材同一套 sha256 分片路径,因此不同商品若渲染出
完全相同的缩略图(纯色背景很容易撞),存储层自动去重,数据库仍是两条记录。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import OutputPurpose
from app.core.hashing import hash_bytes
from app.core.logging import get_logger
from app.models.generation import GenerationCandidate, GenerationTask
from app.models.output_asset import OutputAsset
from app.models.product import Product
from app.services.formatting_service import DEFAULT_SPECS, plan_outputs, render_outputs
from app.services.storage import ObjectStorage, asset_url

logger = get_logger(__name__)

#: 成品图与素材分开放,便于运维单独清理或同步到 CDN
OUTPUT_PREFIX = "outputs"


def list_outputs(
    session: Session, product_id: UUID, *, enabled_only: bool = True
) -> list[OutputAsset]:
    stmt = select(OutputAsset).where(OutputAsset.product_id == product_id)
    if enabled_only:
        stmt = stmt.where(OutputAsset.enabled.is_(True))
    rows = list(session.scalars(stmt))
    order = {p.value: i for i, p in enumerate(OutputPurpose)}
    return sorted(rows, key=lambda r: (-r.generation, order.get(r.purpose, 99)))


def outputs_for_products(
    session: Session, product_ids: list[UUID], *, enabled_only: bool = True
) -> dict[UUID, list[OutputAsset]]:
    """一次取回多个商品的成品图,按商品分桶。

    批量导出用(报告 6.14 的 N+1)。排序口径与 `list_outputs` 完全一致 ——
    两个函数给出不同顺序的话,"单个导出"和"批量导出"里同一个 SKU 的
    同一个用途可能取到不同的图,而那种不一致没有任何报错。
    """
    if not product_ids:
        return {}
    stmt = select(OutputAsset).where(OutputAsset.product_id.in_(product_ids))
    if enabled_only:
        stmt = stmt.where(OutputAsset.enabled.is_(True))

    order = {p.value: i for i, p in enumerate(OutputPurpose)}
    buckets: dict[UUID, list[OutputAsset]] = {pid: [] for pid in product_ids}
    for row in session.scalars(stmt):
        buckets.setdefault(row.product_id, []).append(row)
    for rows in buckets.values():
        rows.sort(key=lambda r: (-r.generation, order.get(r.purpose, 99)))
    return buckets


def complete_products_query(product_ids: list[UUID] | None = None):
    """"五个用途齐全"的商品 id —— 返回**查询本身**,不返回结果。

    `only_complete` 原来是把商品全取出来、组装完 payload 再用 Python 过滤,
    而过滤发生在 `limit` **之后**(见 `api/exports.py` 的说明)。
    要把过滤挪到分页之前,判据就必须能进 SQL。

    ## 为什么返回 select 而不是 set

    第一版返回的是 `set[UUID]`,调用方拿它去 `Product.id.in_(那个集合)`。
    在几百件商品时没问题,几万件时那是一条带着几万个参数的 SQL ——
    为了修一个"漏数据"的缺陷,换来一个"库大了就慢"的缺陷。
    交给数据库做子查询,规模问题不存在。
    """
    stmt = (
        select(OutputAsset.product_id)
        .where(OutputAsset.enabled.is_(True))
        .group_by(OutputAsset.product_id)
        .having(func.count(func.distinct(OutputAsset.purpose)) >= len(OutputPurpose))
    )
    if product_ids is not None:
        stmt = stmt.where(OutputAsset.product_id.in_(product_ids))
    return stmt


def products_with_all_purposes(
    session: Session, product_ids: list[UUID] | None = None
) -> set[UUID]:
    """同上,但直接给结果。只在**已知规模有限**时用(例如判定当前这一页)。"""
    if product_ids is not None and not product_ids:
        return set()
    return set(session.scalars(complete_products_query(product_ids)))


def outputs_for_task(session: Session, task_id: UUID) -> list[OutputAsset]:
    return list(
        session.scalars(
            select(OutputAsset)
            .where(OutputAsset.task_id == task_id, OutputAsset.enabled.is_(True))
        )
    )


def _lock_product(session: Session, product_id: UUID) -> None:
    """锁住商品行,把同一个商品的出图串行化。

    为什么锁 `products` 而不是 `output_assets`:``FOR UPDATE`` 只能锁**已经存在**
    的行。首次出图时一条输出记录都没有,那把锁锁了个空 —— 两个事务并排走过去,
    各自渲染、各自写对象存储,最后一个在部分唯一索引上失败,留下一批
    没有数据库记录的孤儿文件,任务还失败了。

    更隐蔽的是跨任务的情况:同一个商品跑了两个生成任务,它们的候选图不同,
    所以锁候选行也彼此锁不住,但它们要写的是同一个 (product_id, purpose) 唯一键。

    商品行永远存在,而且正好是"这批输出属于谁"的那个粒度。锁它是唯一
    既锁得住又不会把无关商品串起来的选择。

    锁在事务结束时自动释放,不需要显式解锁。
    """
    session.execute(
        select(Product.id).where(Product.id == product_id).with_for_update()
    ).one_or_none()


def _next_generation(session: Session, candidate_id: UUID | None) -> int:
    """算下一个重算代数。

    ``max(...) + 1`` 是典型的读-改-写,并发时会算出同一个代数。
    这里不再单独加锁 —— 调用方已经先锁了商品行,同一个商品的出图整体串行,
    这个计算天然是安全的。多一把锁只会多一处死锁的可能。
    """
    if candidate_id is None:
        return 1
    existing = list(
        session.scalars(
            select(OutputAsset).where(OutputAsset.candidate_id == candidate_id)
        )
    )
    return max((row.generation for row in existing), default=0) + 1


def _retire_previous(session: Session, product_id: UUID, purposes: set[str]) -> int:
    """停用该商品这些用途下所有仍然启用的成品图。

    为什么按**商品**而不是按候选图:网站上一个 SKU 的详情图只能有一张。
    以前只按候选图停用,于是商品跑了新任务、或人工在抽检里换了一张候选之后,
    旧候选的成品图仍然 enabled —— `public_urls()` 遍历时谁最后被赋值谁赢,
    网站上挂哪一张取决于查询返回顺序。这不是配置问题,是数据模型缺了一条约束。
    """
    if not purposes:
        return 0
    rows = list(
        session.scalars(
            select(OutputAsset).where(
                OutputAsset.product_id == product_id,
                OutputAsset.purpose.in_(purposes),
                OutputAsset.enabled.is_(True),
            )
        )
    )
    for row in rows:
        row.enabled = False
    return len(rows)


def _still_referenced(session: Session, paths: list[str]) -> set[str]:
    """这些路径里,还有哪些被数据库里的记录指着。

    内容寻址意味着**同样的内容 = 同样的路径**,而这个等式是跨商品成立的:
    两个 SKU 渲染出同一张纯色缩略图,拿到的是同一个 storage_path。

    于是"这个对象是我这次创建的"这句话不足以支持删除它 —— 另一个商品的
    事务可能已经成功提交,它的 OutputAsset 正指着同一个路径。删掉之后,
    数据库里那条记录还在、网站上的图 404,而且没有任何日志说明为什么。

    查全部六张表:成品图、商品素材、候选图、模特模板,以及 M1 之后的
    素材与派生版本。它们共用同一套分片路径,理论上任何一张都可能撞上。
    """
    if not paths:
        return set()

    from app.models.generation import GenerationCandidate
    from app.models.media_asset import MediaAsset, MediaDerivative
    from app.models.model_template import ModelTemplate
    from app.models.product_asset import ProductAsset

    referenced: set[str] = set()
    # 六张表,不是四张。M1 之后候选图与成品图的路径**同时**存在于
    # media_assets / media_derivatives —— 漏掉它们,这个函数就会
    # 把一条还有素材记录指着的对象判成孤儿并删掉,
    # 也就是上一轮评审第 9 条修过的那个错误换了个地方复现。
    for model in (
        OutputAsset,
        ProductAsset,
        GenerationCandidate,
        ModelTemplate,
        MediaAsset,
        MediaDerivative,
    ):
        column = getattr(model, "storage_path", None)
        if column is None:
            continue
        referenced.update(
            row for row in session.scalars(select(column).where(column.in_(paths))) if row
        )
    return referenced


def _discard_orphans(session: Session, storage: ObjectStorage, paths: list[str]) -> None:
    """删掉写进存储但没能登记的对象。

    两道闸,缺一不可:

    1. 只考虑**这次新建**的那些(``StoredFile.deduplicated`` 为 False)。
       存储层现在用原子的 create-if-absent 回答这个问题,不再是"先 exists
       再写"那种两个并发写入者都以为自己是创建者的猜测。
    2. 删之前再查一遍数据库还有没有记录指着它。第 1 道闸挡不住这种情况:
       另一个商品在我们失败之前就把同样内容的图提交成功了。

    删除失败只记日志、不抛 —— 这个函数只在别的异常处理路径里被调用,
    在这里抛异常会把真正的失败原因盖掉,那才是排查时最难受的事。
    """
    if not paths:
        return
    deleter = getattr(storage, "delete", None)
    if deleter is None:
        logger.warning(
            "storage backend cannot delete; orphaned objects left behind",
            extra={
                "extra_fields": {
                    "event": "output.delete_unsupported",
                    "count": len(paths),
                    "paths": paths[:5],
                }
            },
        )
        return

    try:
        keep = _still_referenced(session, paths)
    except Exception:  # noqa: BLE001
        # 查不动库(会话可能已经因为上游异常不可用了)。**一个都不删** ——
        # 多留几个孤儿文件只是占空间,删错一个是把别人的图删了。
        logger.warning(
            "cannot check references before cleanup; leaving every object in place",
            extra={"extra_fields": {"event": "output.reference_check_failed", "count": len(paths)}},
        )
        return

    removed = 0
    for path in paths:
        if path in keep:
            logger.info(
                "keeping an object that another record still points at",
                extra={"extra_fields": {"event": "output.orphan_kept_referenced", "path": path}},
            )
            continue
        try:
            deleter(path)
            removed += 1
        except Exception:  # noqa: BLE001
            logger.warning(
                "cannot remove an orphaned output object",
                extra={"extra_fields": {"event": "output.orphan_delete_failed", "path": path}},
            )
    logger.info(
        "cleaned up orphaned output objects after a failed publish",
        extra={
            "extra_fields": {
                "event": "output.orphans_cleaned",
                "removed": removed,
                "kept": len(keep),
                "total": len(paths),
            }
        },
    )


def source_asset_problem(
    candidate: GenerationCandidate, *, storage: ObjectStorage
) -> str | None:
    """出图之前先确认那张候选图**真的还能读**。返回错误码,没问题时返回 None。

    ## 为什么需要它(A45-batch12-2 / EX-05)

    `generation_service.can_resume_formatting()` 判\"能不能从出图接着跑\"的
    依据只有两条数据库事实:有一行 SELECTED 候选、且没有启用中的成品图。
    两条都成立并不代表**那一行指着的对象还在**。

    对象被清理脚本删掉、写入时被截断、或者存的根本不是图片时,续跑会一次次
    回到 FORMATTING 读同一个坏路径:`FAILED -> FORMATTING -> FAILED` 循环,
    永远产不出成品图,而每一次界面上都显示\"正在出图\"。库说可用,盘上没有。

    ## 为什么三步都要做

        没有路径     库里那一行本身就是坏的
        对象不存在   `exists()` 一次往返,比读完整个文件再失败便宜
        解不出图     存在不等于是图 —— 零字节、截断、写进去的是 HTML 错误页

    最后一步必须**真的解码**。只看长度会把一个 200 字节的 S3 错误响应
    当成可用文件,然后在 `render_outputs` 里炸成一个 INTERNAL_ERROR ——
    那正是 EX-05 里那个不收敛的循环的开头。

    读整个对象的代价是可接受的:紧接着的 `build_outputs` 本来也要读它一遍,
    而这一次读能把\"坏文件\"从一个通用异常变成一个运营看得懂、且**挡得住
    重试**的错误码。
    """
    from app.core.enums import SOURCE_ASSET_CORRUPT_CODE, SOURCE_ASSET_MISSING_CODE
    from app.services.image_probe import probe_image

    path = (candidate.storage_path or "").strip()
    if not path:
        return SOURCE_ASSET_MISSING_CODE
    try:
        if not storage.exists(path):
            return SOURCE_ASSET_MISSING_CODE
    except Exception:  # noqa: BLE001
        # 存储层自己出错(S3 不通、凭据过期)**不是**文件坏了。
        # 判成 MISSING 会让运营去查一个其实还在的对象,而且会挡住重试 ——
        # 而这一类恰恰是重试就能好的。交给调用方按普通异常处理
        raise
    try:
        data = storage.read(path)
    except FileNotFoundError:
        # exists 与 read 之间被删掉。少见,但清理脚本正在跑时会发生
        return SOURCE_ASSET_MISSING_CODE
    if not data:
        return SOURCE_ASSET_CORRUPT_CODE
    try:
        probe_image(data)
    except Exception:  # noqa: BLE001 - 解不出来就是解不出来,原因不影响结论
        return SOURCE_ASSET_CORRUPT_CODE
    return None


def build_outputs(
    session: Session,
    task: GenerationTask,
    candidate: GenerationCandidate,
    *,
    storage: ObjectStorage,
    specs=DEFAULT_SPECS,
    actor: str = "system",
) -> list[OutputAsset]:
    """渲染并落库。返回新建的记录。

    候选图读不出来时抛 `ValueError` —— 由调用方决定是让任务失败还是记一条错误,
    这里不替它做决定。
    """
    if not candidate.storage_path:
        raise ValueError("候选图没有存储路径,无法生成成品图")

    # 渲染和写存储之前先把商品锁住。这之后同一个商品的出图是串行的,
    # 唯一索引不会再被并发撞到,也就不会留下孤儿文件。
    _lock_product(session, task.product_id)

    source = storage.read(candidate.storage_path)
    width = candidate.width or 0
    height = candidate.height or 0
    if width <= 0 or height <= 0:
        from app.services.image_probe import probe_image

        info = probe_image(source)
        width, height = info.width, info.height

    plan = plan_outputs(width, height, specs)
    rendered = render_outputs(source, plan)

    generation = _next_generation(session, candidate.id)
    # 发布新一批之前,把该商品这些用途下所有仍然启用的旧图停用 ——
    # 不只是同一个候选的旧代,而是**同一个商品同一个用途**的全部。
    # 停用而不删除:网站缓存里的旧 URL 仍然能打开。
    retired = _retire_previous(
        session, task.product_id, {item.plan.purpose.value for item in rendered}
    )
    session.flush()

    created: list[OutputAsset] = []
    #: 这次**新建**的对象(去重命中的不算 —— 那些文件本来就在,别人还在用)。
    #: 落库失败时要把它们删掉,否则存储里会攒下一堆没有任何记录指向的文件,
    #: 既占空间又没人敢清理:没有记录就没法判断它还有没有用。
    written: list[str] = []
    try:
        for item in rendered:
            file_hash = hash_bytes(item.data)
            stored = storage.save(
                item.data,
                file_hash=file_hash,
                extension=item.extension,
                prefix=OUTPUT_PREFIX,
            )
            if not stored.deduplicated:
                written.append(stored.storage_path)
            asset = OutputAsset(
                product_id=task.product_id,
                task_id=task.id,
                candidate_id=candidate.id,
                purpose=item.plan.purpose.value,
                generation=generation,
                storage_path=stored.storage_path,
                file_hash=file_hash,
                mime_type="image/jpeg" if item.image_format == "JPEG" else "image/png",
                image_format=item.image_format,
                file_size=len(item.data),
                width=item.width,
                height=item.height,
                enabled=True,
                note=(item.plan.note or "")[:255],
                meta={
                    "fit": item.plan.fit.value,
                    "source_width": width,
                    "source_height": height,
                    "deduplicated": stored.deduplicated,
                },
            )
            session.add(asset)
            created.append(asset)

        session.flush()

        # 影子写(迁移 A):成品图落成**派生版本**,不是新素材(§4.3)。
        #
        # 它是候选图的多尺寸转换结果。当成独立素材的话,同一张图会在
        # 素材库里出现五遍(五个用途),而去重、授权、保留策略全是
        # 按素材算的 —— 一张图五份授权记录是说不通的。
        from app.media import service as media_service

        source_media_id = candidate.media_asset_id
        for asset in created:
            media_service.shadow_from_output_asset(
                session, asset, source_media_asset_id=source_media_id
            )
        session.flush()
    except Exception:
        # 落库失败:把这次刚写进存储、但没有任何数据库记录指向的对象删掉。
        # 不删的话它们会一直躺在那里 —— 既占空间,又没人敢清理,
        # 因为没有记录就没法判断它还有没有用。
        #
        # 传 session 进去是必须的:内容寻址会让不同商品的相同内容落到同一个
        # 路径上,删之前得先确认没有别的记录还指着它。
        _discard_orphans(session, storage, written)
        raise

    logger.info(
        "outputs built",
        extra={
            "extra_fields": {"event": "output.built",
                "task_id": str(task.id),
                "candidate_id": str(candidate.id),
                "generation": generation,
                "count": len(created),
                "retired": retired,
            }
        },
    )

    from app.core.enums import AuditAction
    from app.services import audit

    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="OutputAsset",
        entity_id=task.id,
        payload={
            "task_id": str(task.id),
            "candidate_id": str(candidate.id),
            "generation": generation,
            "purposes": [a.purpose for a in created],
        },
    )
    return created


def public_urls(assets: list[OutputAsset], storage: ObjectStorage) -> dict[str, dict[str, Any]]:
    """按用途给出网站直接可用的 URL 表。

    同一用途出现多条时取代数最大、其次创建时间最新的那条。数据库层已经有
    (product_id, purpose) WHERE enabled 的部分唯一索引兜底,这里再排一次序是因为
    这个函数也会被喂进跨商品或历史数据 —— 让「谁赢」由明确规则决定,
    而不是由 SQL 的返回顺序决定。
    """
    result: dict[str, dict[str, Any]] = {}
    ordered = sorted(
        (a for a in assets if a.enabled),
        key=lambda a: (a.generation or 0, a.created_at or datetime.min),
    )
    for asset in ordered:
        result[asset.purpose] = {
            # 成品图落在公开前缀下,asset_url 会给出公开地址;
            # 用统一入口而不是 public_url,是为了让"哪些前缀是公开的"
            # 只有一处定义 —— 见 services/storage.PUBLIC_PREFIXES
            "url": asset_url(storage, asset.storage_path),
            "width": asset.width,
            "height": asset.height,
            "format": asset.image_format,
            "file_size": asset.file_size,
            "hash": asset.file_hash,
        }
    return result
