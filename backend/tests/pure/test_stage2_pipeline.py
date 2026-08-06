"""阶段 2 闭环冒烟:CSV 导入 -> 素材校验 -> 存储去重。

不碰数据库,只把服务层串起来,验证各模块接口能对上。
真正的库内闭环由 tests/test_api_products.py 覆盖(需 PostgreSQL)。

## v4.1 Phase 0:这里少了四条

原来还有四条在验 `sample-data/` 那份数据本身(CSV 合不合法、SKU 唯不唯一、
样例图齐不齐、几张图内容一不一样)。方案 8.2 节把它们移到了
`tools/verify_sample_data.py`,由 `make verify-sample-data` 和 CI 执行。

分开的理由:那四条验的是**数据**,这两条验的是**代码**。混在一起时,
换一批样图会让「阶段 2 管线」这个名字下面冒出失败,而根因和管线无关。
另外那四条原来在图片不存在时静默 return —— 一条不声不响就通过的检查,
和没有这条检查是一样的。搬出去之后改成显式报告「没有图」。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.core.enums import AssetType
from app.core.hashing import hash_bytes
from app.services.product_import import parse_csv
from app.services.storage import LocalObjectStorage
from app.services.upload_validation import validate_upload
from tests.pure._helpers import PROJECT_ROOT, jpeg_bytes, temporary_secret_key

SAMPLE_CSV = PROJECT_ROOT / "sample-data" / "products.csv"
SAMPLE_IMAGES = PROJECT_ROOT / "sample-data" / "images"


def test_end_to_end_import_then_store():
    """走一遍:解析 CSV -> 校验图片 -> 落盘 -> 重复落盘命中去重。"""
    parsed = parse_csv(SAMPLE_CSV.read_text(encoding="utf-8"))
    assert parsed.ok

    with tempfile.TemporaryDirectory() as tmp:
        storage = LocalObjectStorage(tmp, "http://localhost:8000")
        image = jpeg_bytes(900, 1200)
        stored_paths = set()

        for row in parsed.rows[:3]:
            validated = validate_upload(
                image,
                asset_type=AssetType.GARMENT_FRONT,
                max_bytes=20 * 1024 * 1024,
                min_edge_px=256,
            )
            assert validated.check_passed
            stored = storage.save(
                validated.data,
                file_hash=hash_bytes(image),
                extension=validated.info.extension,
                prefix="uploads",
            )
            stored_paths.add(stored.storage_path)
            _ = row

        # 三个商品用了同一张图 -> 物理上只应存一份
        assert len(stored_paths) == 1
        files = [p for p in Path(tmp).rglob("*") if p.is_file()]
        assert len(files) == 1


def test_storage_url_is_servable_path():
    with tempfile.TemporaryDirectory() as tmp:
        storage = LocalObjectStorage(tmp, "http://localhost:8000")
        image = jpeg_bytes()
        stored = storage.save(
            image, file_hash=hash_bytes(image), extension=".jpg", prefix="uploads"
        )
        # 上传的原始素材是**私有**的:/files 挂载点现在只服务 outputs/,
        # 其余前缀必须走带签名和有效期的 /api/media(评审第 2 条)
        from app.services.storage import asset_url

        # 签名密钥平时从主密钥派生,而主密钥要读配置(需要 pydantic)。
        # 这批用例刻意零三方依赖,所以借一把只在本块内有效的临时密钥。
        #
        # v4.1 Phase 0 / 任务 4:这里原来是手写的「存旧值 → 改 os.environ →
        # finally 还原」。问题不在还原写得对不对,在于它**只改了一个变量**:
        # 真实 pytest 下 `app.core.config.settings` 早就把空值读走了,于是
        # secrets 会去走「自动生成密钥文件」那条路,在仓库根写出 `.secrets/`。
        # 同一段代码在两个运行器下行为不同——纯运行器里它是对的,pytest 里它有副作用。
        #
        # 换成 `_helpers.temporary_secret_key()`:它同时钉住密钥**和密钥目录**,
        # 所以无论走到哪条分支都写不进仓库树。真实 pytest 那一侧另有
        # conftest 顶部的会话级隔离兜底,两层不冲突。
        with temporary_secret_key():
            url = asset_url(storage, stored.storage_path)
            public_url = asset_url(
                storage,
                storage.save(
                    image, file_hash=hash_bytes(image), extension=".jpg", prefix="outputs"
                ).storage_path,
            )

        # 前缀是 /api/media-files 而不是 /api/media:后者留给了素材库的
        # 增删改查(M1),两者鉴权方式不同 —— 这一条走 URL 签名,
        # 那一条走操作口令,共用前缀会把素材库一起放开成匿名的
        assert url.startswith("http://localhost:8000/api/media-files/uploads/")
        assert "exp=" in url and "sig=" in url

        # 成品图仍然是公开的,地址不变 —— 网站上挂的就是它
        assert public_url.startswith("http://localhost:8000/files/outputs/")
        assert "sig=" not in public_url
