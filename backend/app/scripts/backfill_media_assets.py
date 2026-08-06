"""历史数据回填:旧表 → `media_assets`(迁移 A 的后半段)。

    python -m app.scripts.backfill_media_assets              # 只看,不动
    python -m app.scripts.backfill_media_assets --apply
    python -m app.scripts.backfill_media_assets --apply --reconcile   # 顺带对账

影子写只覆盖**上线之后**的新数据。库里已有的商品素材、生成候选、成品图
都还没有对应的 `MediaAsset`,由这个脚本补上。

## 为什么是脚本而不是数据迁移

回填量可能很大(素材是按图算的),而 Alembic 迁移跑在一个事务里。
一次几十万行的 UPDATE 会长时间持锁,期间线上写入全部阻塞。
脚本可以分批提交、可以中断续跑、可以先干跑看一眼。

## 幂等

按 `(legacy_kind, legacy_id)` 判断是否已回填,重复跑安全。
中途 Ctrl-C 之后直接再跑一次即可,不需要记录进度。
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.core.logging import get_logger, setup_logging
from app.db.session import SessionLocal
from app.media import service as media_service
from app.models.generation import GenerationCandidate
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.models.product_asset import ProductAsset

logger = get_logger(__name__)

#: 每批提交一次。太小则提交开销占比过高,太大则单次持锁时间过长。
#: 500 是一个在几万行量级上试过还算合适的值,可以用 --batch 调
DEFAULT_BATCH = 500


def main() -> int:
    parser = argparse.ArgumentParser(description="把历史图片回填进 media_assets")
    parser.add_argument("--apply", action="store_true", help="真的写入;不加只统计")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="每批提交行数")
    parser.add_argument(
        "--reconcile", action="store_true",
        help="回填后立刻跑一次双读对账(迁移 B 的判据)",
    )
    args = parser.parse_args()

    setup_logging("INFO")
    session = SessionLocal()
    try:
        before = media_service.coverage(session)
        _print_coverage("回填前", before)

        if not args.apply:
            pending_assets = _pending_product_assets(session)
            pending_candidates = _pending_candidates(session)
            print(
                f"\n待回填:商品素材 {len(pending_assets)},生成候选 {len(pending_candidates)}"
            )
            print("只是看看。要真的执行,加 --apply")
            return 0

        filled_assets = _backfill_product_assets(session, batch=args.batch)
        filled_candidates = _backfill_candidates(session, batch=args.batch)
        print(f"\n回填完成:商品素材 {filled_assets},生成候选 {filled_candidates}")

        after = media_service.coverage(session)
        _print_coverage("回填后", after)

        if args.reconcile:
            mismatches = _reconcile_all(session)
            print(f"\n对账发现 {mismatches} 处不一致")
            if mismatches:
                print("  详见 media_migration_mismatches 表。")
                print("  迁移 B 的出口条件是**一周无新增**,现在还不能切换读路径。")
                return 1
        return 0
    finally:
        session.close()


def _print_coverage(label: str, data: dict[str, int]) -> None:
    print(f"\n{label}:")
    print(f"  商品素材      {data['product_assets_shadowed']}/{data['product_assets']}")
    print(f"  生成候选      {data['candidates_linked']}/{data['candidates_with_image']}")
    print(f"  media_assets  {data['media_assets']}")


def _pending_product_assets(session) -> list[ProductAsset]:
    """还没有影子记录的商品素材。

    用 NOT EXISTS 而不是把已回填的 id 拉进内存做差集:后者在几十万行时
    会先吃掉几百 MB,而这个脚本多半跑在一台内存不宽裕的运维机上。
    """
    shadowed = select(MediaAsset.legacy_id).where(
        MediaAsset.legacy_kind == media_service.LEGACY_PRODUCT_ASSET
    )
    return list(
        session.scalars(select(ProductAsset).where(ProductAsset.id.not_in(shadowed)))
    )


def _pending_candidates(session) -> list[GenerationCandidate]:
    return list(
        session.scalars(
            select(GenerationCandidate).where(
                GenerationCandidate.storage_path.is_not(None),
                GenerationCandidate.media_asset_id.is_(None),
            )
        )
    )


def _backfill_product_assets(session, *, batch: int) -> int:
    done = 0
    for asset in _pending_product_assets(session):
        product = session.get(Product, asset.product_id)
        if product is None:
            # 商品被删了但素材还在:外键是 CASCADE,理论上不会发生。
            # 真发生了说明有人手工删过数据,跳过并记一笔
            logger.warning(
                "orphan product asset skipped",
                extra={"extra_fields": {"asset_id": str(asset.id)}},
            )
            continue
        media_service.shadow_from_product_asset(session, product, asset)
        done += 1
        if done % batch == 0:
            session.commit()
            print(f"  ...商品素材 {done}")
    session.commit()
    return done


def _backfill_candidates(session, *, batch: int) -> int:
    done = 0
    for candidate in _pending_candidates(session):
        task = candidate.task
        product = session.get(Product, task.product_id) if task is not None else None
        if product is None:
            logger.warning(
                "orphan candidate skipped",
                extra={"extra_fields": {"candidate_id": str(candidate.id)}},
            )
            continue
        media_service.shadow_from_candidate(session, product, candidate)
        done += 1
        if done % batch == 0:
            session.commit()
            print(f"  ...生成候选 {done}")
    session.commit()
    return done


def _reconcile_all(session) -> int:
    """全量对账。返回不一致条数。

    成品图不在这里对账:它落的是派生版本,和素材不是同一种东西,
    比对逻辑要等 M5 图片集把变换规则外置之后才有意义。
    """
    total = 0
    for asset in session.scalars(select(ProductAsset)):
        total += len(media_service.reconcile_product_asset(session, asset))
    for candidate in session.scalars(
        select(GenerationCandidate).where(GenerationCandidate.storage_path.is_not(None))
    ):
        total += len(media_service.reconcile_candidate(session, candidate))
    session.commit()
    return total


if __name__ == "__main__":
    sys.exit(main())
