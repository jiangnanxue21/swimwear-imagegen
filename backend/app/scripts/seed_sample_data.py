"""导入 sample-data 下的示例商品与素材。

幂等:重复执行不会产生重复商品或重复素材(依赖 SKU 唯一约束与文件哈希去重)。
用法:python -m app.scripts.seed_sample_data

## 两份样例,用途不同

    products.csv   CSV 解析与素材文件的回归样本。阶段 1 收口后导入要求 SPU
                   与颜色先存在,所以不能再把这份十个旧款号的平表直接灌库
    spus.json      新路径(三步建档)。PRD §13 阶段 1 验收「可构造三颜色
                   九 SKU 的 SPU」要的就是它

CSV 仍在 `verify_sample_data.py` 里逐行解析并校验对应图片，但播种只走
`spu_service.create_spu()`。否则脚本会在找不到 SPU 时得到十条业务错误，
却打印「商品新增 0」并以退出码 0 结束 —— 看起来像播种成功，实际人工环境是空的。

## 新结构走 `spu_service.create_spu()`,不另写一条

那是 `POST /spus` 用的同一个服务。在这里另写一条展开逻辑的下场是:
样例数据能建出接口建不出来的东西(比如一个跳过 SKU 展开规则的 SPU),
而演示时看不出来 —— 直到有人照着样例的形状去调接口。

## 新结构的素材**按颜色挂**

`<spu>-<variant>_<view>.jpg` 挂到该颜色的第一个 SKU 行上,并带
`color_variant_id`;`<spu>__<view>.jpg` 是通用图,不带。

为什么挂到"第一个 SKU 行":素材表的外键是 `product_id`(SKU 行),而归属
是 `color_variant_id`。一个颜色的样品图对该颜色下每个尺码都算数 ——
逐个尺码各传一份的话,`(product_id, sha256)` 去重键挡不住(product_id 不同),
于是同一张图在库里有三份,而 §5.3 的指纹会把它们当成三张不同的证据。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.core.config import PROJECT_ROOT, settings
from app.core.enums import AssetType
from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.services import asset_service, spu_service
from app.services.product_import import parse_csv
from app.services.storage import build_storage

SAMPLE_DIR = Path("/sample-data") if Path("/sample-data").exists() else PROJECT_ROOT / "sample-data"

#: 文件名后缀 -> 素材类型
VIEW_TO_ASSET_TYPE = {
    "front": AssetType.GARMENT_FRONT,
    "back": AssetType.GARMENT_BACK,
    "detail": AssetType.GARMENT_DETAIL,
}


def main() -> int:
    setup_logging(settings.LOG_LEVEL, service="script")

    csv_path = SAMPLE_DIR / "products.csv"
    if not csv_path.exists():
        print(f"找不到示例数据:{csv_path}", file=sys.stderr)
        return 1

    parsed = parse_csv(csv_path.read_text(encoding="utf-8"))
    if parsed.errors:
        for err in parsed.errors:
            print(f"  第 {err.row_number} 行 {err.field}: {err.message}", file=sys.stderr)
        return 1

    storage = build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )
    session = SessionLocal()
    try:
        print(
            f"旧格式 CSV: {len(parsed.rows)} 行解析通过（回归样本，不直接落库；"
            "导入前须先建 SPU 与颜色）"
        )

        seed_new_structure(session, storage)
        session.commit()

        print(f"存储目录:{settings.storage_dir}")
        return 0
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(f"导入失败:{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def seed_new_structure(session, storage) -> None:
    """播 `spus.json`:SPU → 颜色 → SKU,素材按颜色挂。

    幂等靠 `uq_spus_spu_code`:已经建过的 SPU 直接跳过,不去改它 ——
    改的话会把运营在演示环境里做过的确认冲掉,而那看起来像"数据自己变了"。
    """
    manifest = SAMPLE_DIR / "spus.json"
    if not manifest.exists():
        print("未找到 spus.json,跳过新结构样例")
        return

    data = json.loads(manifest.read_text(encoding="utf-8"))
    images_dir = SAMPLE_DIR / "images"
    views = tuple(data["sample_images"]["views"])
    shared_views = tuple(data["sample_images"]["shared_views"])

    created = skipped = 0
    uploaded = deduped = 0
    for spec in data["spus"]:
        code = spec["spu_code"]
        if session.scalar(select(Spu).where(Spu.spu_code == code)) is not None:
            skipped += 1
            continue
        spu = spu_service.create_spu(
            session,
            {k: v for k, v in spec.items() if not k.startswith("_") and k != "notes"},
            actor="seed-script",
        )
        session.flush()
        created += 1

        if not images_dir.exists():
            continue
        u, d = _seed_colour_images(
            session, storage, spu, images_dir, views, shared_views
        )
        uploaded += u
        deduped += d

    print(f"SPU:新增 {created},已存在跳过 {skipped}")
    if images_dir.exists():
        print(f"新结构素材:新增 {uploaded},命中去重 {deduped}")


def _seed_colour_images(
    session, storage, spu: Spu, images_dir: Path, views, shared_views
) -> tuple[int, int]:
    """把该 SPU 的样品图挂上去。返回 (新增, 去重)。

    **每个颜色只挂一次**,落在该颜色的第一个 SKU 行上 —— 理由见模块文档
    末段。通用图挂在整个 SPU 的第一个 SKU 行上,`color_variant_id` 留空。
    """
    variants = list(
        session.scalars(
            select(ColorVariant)
            .where(ColorVariant.spu_id == spu.id)
            .order_by(ColorVariant.sort_order)
        )
    )
    uploaded = deduped = 0

    def _first_sku(variant_id: UUID | None) -> Product | None:
        stmt = select(Product).where(Product.spu_id == spu.id)
        if variant_id is not None:
            stmt = stmt.where(Product.color_variant_id == variant_id)
        return session.scalars(stmt.order_by(Product.sku).limit(1)).first()

    def _upload(product: Product, path: Path, variant_id: UUID | None) -> None:
        nonlocal uploaded, deduped
        _, was_dup = asset_service.upload_asset(
            session,
            product=product,
            data=path.read_bytes(),
            filename=path.name,
            asset_type=VIEW_TO_ASSET_TYPE[path.stem.rsplit("_", 1)[-1]],
            storage=storage,
            actor="seed-script",
            color_variant_id=variant_id,
        )
        deduped += was_dup
        uploaded += not was_dup

    for variant in variants:
        product = _first_sku(variant.id)
        if product is None:
            continue
        for view in views:
            path = images_dir / f"{spu.spu_code}-{variant.variant_code}_{view}.jpg"
            if path.exists():
                _upload(product, path, variant.id)

    shared_host = _first_sku(None)
    if shared_host is not None:
        for view in shared_views:
            path = images_dir / f"{spu.spu_code}__{view}.jpg"
            if path.exists():
                _upload(shared_host, path, None)

    return uploaded, deduped


if __name__ == "__main__":
    raise SystemExit(main())
