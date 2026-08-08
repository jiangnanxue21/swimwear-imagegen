"""素材颜色归属的写入路径 —— **真库那一半**(A45-batch14-22)。

## 这个文件在本轮一次都没跑过

打包这一批的机器上没有 PostgreSQL、没有 sqlalchemy。下面每一条都是**规格**,
不是成绩。

## 为什么这些不能改写成纯层守卫来凑数

纯层那 13 条钉的是**链路形状**:哪一层收得下、哪一层往下传、哪一层写进
`MediaAsset(...)`。它们全是 AST 断言 —— 证明得了"参数一路传到了构造点",
证明不了"那一列真的存下来了"。

而本批修的洞恰恰在**存下来之后**才看得见:归属是外键,它有

    一条 FK 约束(指向不存在的颜色时数据库拒绝)
    一条 ondelete=SET NULL(删颜色不连坐删素材)
    一条与去重键 `(product_id, sha256)` 的交互

三件事纯层一件也验不到。而其中第三件是本批最要紧的一条:**同一张图先传成
通用图、再补颜色**这条动线,靠的正是去重命中那一路的 `_fill_missing_colour`。

## 跑法

    docker compose up -d db
    cd backend && pytest tests/test_a45_batch14_22_colour_attribution_db.py -v

## 先看这一条

`test_the_colour_survives_all_the_way_into_the_row` 最该先跑:它红了说明
写入链在真环境里断在某处,而纯层那 13 条会全部绿着 —— 那是本批修的洞
原样回来的样子。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.attributes import scope_fingerprint as fp
from app.core.enums import AssetType, MediaSource, MediaStatus
from app.core.errors import ValidationError
from app.media import service as media_service
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from app.services import asset_service, product_service

pytestmark = pytest.mark.requires_db


@pytest.fixture
def spu(session):
    row = Spu(
        spu_code=f"SPU-CA-{uuid.uuid4().hex[:6]}",
        internal_name="归属用例款",
        audience="WOMEN",
        base_category="swimwear",
        created_by="test",
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def colours(session, spu):
    made = []
    for code, name in (("A", "黑"), ("B", "蓝")):
        row = ColorVariant(
            spu_id=spu.id, variant_code=code, working_name=name, display_name=name
        )
        session.add(row)
        made.append(row)
    session.flush()
    return made


@pytest.fixture
def products(session, spu, colours):
    made = []
    for variant in colours:
        row = Product(
            spu=spu.spu_code,
            sku=f"{spu.spu_code}-{variant.variant_code}",
            name=f"归属用例 {variant.working_name}",
            category="swimwear",
            audience="WOMEN",
            spu_id=spu.id,
            color_variant_id=variant.id,
        )
        session.add(row)
        made.append(row)
    session.flush()
    return made


def _ingest(session, product, *, colour=None, digest=None):
    asset, deduped = media_service.ingest(
        session,
        product=product,
        storage_path=f"products/{product.id}/{uuid.uuid4().hex[:8]}.jpg",
        sha256=digest or (uuid.uuid4().hex + uuid.uuid4().hex),
        mime_type="image/jpeg",
        width=800,
        height=1200,
        byte_size=1024,
        source=MediaSource.MANUAL_UPLOAD,
        color_variant_id=colour.id if colour is not None else None,
    )
    return asset, deduped


# ---------------------------------------------------------------- 写入链


def test_the_colour_survives_all_the_way_into_the_row(session, products, colours):
    """归属真的落进了那一列。

    **这是本批的核心断言。** 在它之前 `media_assets.color_variant_id` 恒为
    NULL —— 列在、两处判定读它、而没有任何写入路径,于是颜色维整个塌成
    一维:每张图都算通用图。
    """
    asset, _ = _ingest(session, products[0], colour=colours[0])
    session.flush()
    session.refresh(asset)
    assert asset.color_variant_id == colours[0].id


def test_the_public_upload_path_carries_it_too(session, products, colours, storage):
    """接口用的那条路(`asset_service.upload_asset`)也带得下去。

    分开断一次是因为它比 `ingest` 多两层(影子写、旧 `ProductAsset` 表),
    而本批那个洞正是断在中间某一层 —— 每一层单看都对,合起来什么也没发生。
    """
    from tests.pure._helpers import jpeg_bytes

    asset_service.upload_asset(
        session,
        product=products[0],
        data=jpeg_bytes(),
        filename="front.jpg",
        asset_type=AssetType.GARMENT_FRONT,
        storage=storage,
        actor="tester",
        color_variant_id=colours[0].id,
    )
    session.flush()
    media = session.query(MediaAsset).filter(
        MediaAsset.product_id == products[0].id
    ).one()
    assert media.color_variant_id == colours[0].id, (
        "接口那条路没把归属带到影子行 —— 纯层断言全绿而列是空的"
    )


def test_no_colour_means_a_shared_asset_not_a_missing_value(session, products):
    """不传颜色 = 通用图,而不是"待定"。

    §5.3 给通用图定了明确语义(只进共享作用域,不进任何颜色子集),
    所以 NULL 在这一列上是一个**有意义的取值**,不是缺失。
    """
    asset, _ = _ingest(session, products[0], colour=None)
    session.flush()
    assert asset.color_variant_id is None
    prints = fp.fingerprints(fp.views([asset]))
    assert set(prints) == {fp.SHARED}, "通用图进了某个颜色子集"


# ---------------------------------------------------------------- 两道闸


def test_a_colour_from_another_spu_cannot_be_written(session, products):
    """跨 SPU 的颜色 id 在服务层就被拦下。

    §4.3:颜色名在 SPU 内唯一,**跨 SPU 同名是常态**。传错的后果不是报错 ——
    图挂到另一个款的颜色上,于是一个没人动过的款突然一批事实过期。
    """
    other = Spu(
        spu_code=f"SPU-OTHER-{uuid.uuid4().hex[:6]}",
        internal_name="别的款",
        audience="WOMEN",
        base_category="swimwear",
        created_by="test",
    )
    session.add(other)
    session.flush()
    stranger = ColorVariant(
        spu_id=other.id, variant_code="A", working_name="黑", display_name="黑"
    )
    session.add(stranger)
    session.flush()

    with pytest.raises(ValidationError):
        product_service.assert_colour_belongs_to(session, products[0], stranger.id)


def test_a_product_without_an_spu_cannot_use_colour_upload(session):
    """老建档路径建的商品给不出"本商品所在 SPU",一律拒绝。

    放行等于允许把图挂到一个**查不出关系**的颜色上,而那正是上一条那个
    问题的来路。代价是老路径建的商品暂时不能按颜色上传 —— 它们本来也
    没有颜色可选。
    """
    legacy = Product(
        spu="SPU-LEGACY-1",
        sku=f"SPU-LEGACY-1-{uuid.uuid4().hex[:6]}",
        name="老路径建的商品",
        category="swimwear",
        audience="WOMEN",
    )
    session.add(legacy)
    session.flush()
    assert legacy.spu_id is None
    with pytest.raises(ValidationError):
        product_service.assert_colour_belongs_to(session, legacy, uuid.uuid4())


def test_the_foreign_key_refuses_a_colour_that_does_not_exist(session, products):
    """**数据库这一层也拦。** 服务层那道闸是给正常路径的;这一条防的是
    绕过服务层的写入(脚本、迁移、将来某个新路径)。

    与 §9.2 那条"数据库裁决"同一个道理:一道校验靠另一道校验碰巧还在,
    不叫防住了。
    """
    asset = MediaAsset(
        product_id=products[0].id,
        spu=products[0].spu,
        source=MediaSource.MANUAL_UPLOAD.value,
        storage_path="x.jpg",
        mime_type="image/jpeg",
        width=800,
        height=1200,
        bytes=1024,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        status=MediaStatus.READY.value,
        color_variant_id=uuid.uuid4(),  # 不存在的颜色
    )
    session.add(asset)
    with pytest.raises(IntegrityError):
        session.flush()


# ---------------------------------------------------------------- 去重那一路


def test_a_shared_asset_can_later_be_told_which_colour_it_belongs_to(
    session, products, colours
):
    """先传成通用图、再补颜色 —— 这条动线要走得通。

    运营的真实动作顺序常常是"先把图传上来,回头再分颜色"。补不上的话,
    那张图永远算通用图,而该颜色的完整度门禁永远缺图。

    **靠的是去重命中那一路的 `_fill_missing_colour`** —— 纯层验不到它,
    因为它只在 `(product_id, sha256)` 真的撞上时才走到。
    """
    digest = uuid.uuid4().hex + uuid.uuid4().hex
    first, deduped = _ingest(session, products[0], colour=None, digest=digest)
    assert deduped is False
    assert first.color_variant_id is None

    again, deduped = _ingest(session, products[0], colour=colours[0], digest=digest)
    assert deduped is True, "第二次没有命中去重 —— 那这条用例验的不是这件事"
    assert again.id == first.id
    session.flush()
    assert again.color_variant_id == colours[0].id, "补不上归属"


def test_a_repeat_upload_cannot_move_an_image_to_another_colour(
    session, products, colours
):
    """已经定过归属的图,重复上传**不许**把它挪到别的颜色。

    §5.3:改归属 A→B 会让原颜色、新颜色、共享三个指纹同时变化 ——
    三批事实同时过期。让一次"再传一次同一张图"顺手触发它,是最难查的
    一类数据变更:运营做的动作和看到的结果在两个页面上,毫无关联。
    """
    digest = uuid.uuid4().hex + uuid.uuid4().hex
    first, _ = _ingest(session, products[0], colour=colours[0], digest=digest)
    again, deduped = _ingest(session, products[0], colour=colours[1], digest=digest)
    assert deduped is True
    session.flush()
    assert again.color_variant_id == colours[0].id, (
        "**一次重复上传把图挪到了另一个颜色** —— 三批事实会同时过期,"
        "而运营看不出这两件事有关系"
    )


# ---------------------------------------------------------------- 删颜色


def test_deleting_a_colour_does_not_take_the_photos_with_it(
    session, products, colours
):
    """`ondelete=SET NULL`:删掉一个颜色不连坐删素材。

    素材是实物样品的照片,颜色是业务分组 —— 后者没了前者还在。
    连坐删的话,一次"这个颜色不做了"的业务决定会静默销毁一批原始素材,
    而那是找不回来的。

    删完之后那些图变成通用图(归属为 NULL),这与 §5.3 的语义一致。
    """
    asset, _ = _ingest(session, products[0], colour=colours[0])
    session.flush()
    asset_id = asset.id

    # SKU 行先解绑,否则 `fk_products_color_variant_id` 的 RESTRICT 会拦住删除
    for product in products:
        if product.color_variant_id == colours[0].id:
            product.color_variant_id = None
    session.flush()
    session.delete(colours[0])
    session.flush()

    survivor = session.get(MediaAsset, asset_id)
    assert survivor is not None, "删颜色把素材一起删了 —— 原始样品照找不回来"
    session.expire(survivor, ["color_variant_id"])
    assert survivor.color_variant_id is None
