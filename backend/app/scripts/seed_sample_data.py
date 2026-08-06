"""导入 sample-data 下的示例商品与素材。

幂等:重复执行不会产生重复商品或重复素材(依赖 SKU 唯一约束与文件哈希去重)。
用法:python -m app.scripts.seed_sample_data
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.core.config import PROJECT_ROOT, settings
from app.core.enums import AssetType
from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.services import asset_service, product_service
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
    setup_logging(settings.LOG_LEVEL)

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
        result = product_service.import_products(session, parsed, actor="seed-script")
        session.commit()
        print(f"商品:新增 {result['created']},已存在跳过 {result['skipped_existing']}")

        images_dir = SAMPLE_DIR / "images"
        if not images_dir.exists():
            print("未找到 images/,先运行 python sample-data/generate_images.py")
            return 0

        uploaded = deduped = 0
        for row in parsed.rows:
            products, _ = product_service.list_products(session, search=row["sku"], limit=1)
            if not products:
                continue
            product = products[0]
            for view, asset_type in VIEW_TO_ASSET_TYPE.items():
                path = images_dir / f"{row['sku']}_{view}.jpg"
                if not path.exists():
                    continue
                _, was_dup = asset_service.upload_asset(
                    session,
                    product=product,
                    data=path.read_bytes(),
                    filename=path.name,
                    asset_type=asset_type,
                    storage=storage,
                    actor="seed-script",
                )
                deduped += was_dup
                uploaded += not was_dup
            session.commit()

        print(f"素材:新增 {uploaded},命中去重 {deduped}")
        print(f"存储目录:{settings.storage_dir}")
        return 0
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(f"导入失败:{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
