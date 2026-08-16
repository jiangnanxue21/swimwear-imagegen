"""素材上传与查询。

去重语义分两层:
1. 物理层 —— 落盘路径由 sha256 推导,相同内容全局只存一份;
2. 记录层 —— 同一商品下 **(file_hash, asset_type)** 只保留一条记录。

第 2 条的**角色必须参与**,和 `ProductAsset` 上的唯一约束一致。
同一张图既可能是「正面图」也可能是「抠图」,只按 (商品, 哈希) 去重的话:

    先传成正面图,再传成抠图 -> 静默返回那条正面图记录
                            -> 抠图那条压根没建
                            -> 抠图要求的 alpha 通道校验一次都没跑

而调用方拿到 `deduplicated=True`,界面上显示「已存在,已复用」——
看起来一切正常。这个缺陷不报错,要到生成任务取不到抠图时才暴露。
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import AssetType, AuditAction
from app.core.hashing import hash_bytes
from app.core.paths import sanitize_filename
from app.models.product import Product
from app.models.product_asset import ProductAsset
from app.services import audit, product_service
from app.services.storage import ObjectStorage
from app.services.upload_validation import validate_upload


def list_assets(session: Session, product_id: UUID) -> list[ProductAsset]:
    return list(
        session.scalars(
            select(ProductAsset)
            .where(ProductAsset.product_id == product_id)
            .order_by(ProductAsset.created_at)
        )
    )


def _find_same(
    session: Session, product_id: UUID, file_hash: str, asset_type: AssetType
) -> ProductAsset | None:
    """按去重键查已有素材。**键必须和唯一约束逐字一致。**

    先查后插和冲突恢复共用这一个函数,而不是各写一遍 where 子句:
    两处写法一旦分叉,冲突恢复会查不到那条刚刚赢了的记录,
    于是把一个本该返回「已存在」的请求变成 500。
    """
    return session.scalar(
        select(ProductAsset).where(
            ProductAsset.product_id == product_id,
            ProductAsset.file_hash == file_hash,
            ProductAsset.asset_type == asset_type.value,
        )
    )


def _shadow(
    session: Session,
    product: Product,
    asset: ProductAsset,
    *,
    color_variant_id: UUID | None,
) -> None:
    """影子写(迁移 A,§4.4)。**同事务,不自己 commit。**

    分两个事务写的话,业务提交成功而影子写失败时两边会永久不一致,而且没有
    任何东西会发现 —— 对账要到迁移 B 才上线。所以它失败就让整笔上传失败:
    迁移期的正确性比可用性重要,一个静默失败的影子写会产出一个"看起来成功"
    的迁移,而它要到切换读路径那天才暴露。

    抽成函数是因为**两条路径都要走它**:新建那条,和去重命中那条。
    命中那条以前直接 return,理由见 `upload_asset` 里那段注释。
    """
    from app.media import service as media_service

    media_service.shadow_from_product_asset(
        session, product, asset, color_variant_id=color_variant_id
    )


def upload_asset(
    session: Session,
    *,
    product: Product,
    data: bytes,
    filename: str,
    asset_type: AssetType,
    storage: ObjectStorage,
    actor: str,
    color_variant_id: UUID | None = None,
) -> tuple[ProductAsset, bool]:
    """返回 (素材记录, 是否命中去重)。

    ## `color_variant_id`:这张图是哪个颜色的样品(§4.8)

    A45-batch14-22 起这条路收得下归属。在此之前 `media_assets.color_variant_id`
    **没有任何写入路径** —— 列在、两处判定读它、而全仓恒为 NULL,
    于是颜色维整个塌成一维:每张图都算通用图。

    `None` 是通用图,不是"待定"。两者要区分的话得再加一列,而 §5.3 已经
    给通用图定了明确语义(只进共享作用域),再加一档只会让判定多一个分支
    而没有对应的业务动作。
    """
    validated = validate_upload(
        data,
        asset_type=asset_type,
        max_bytes=settings.max_upload_bytes,
        min_edge_px=settings.MIN_IMAGE_EDGE_PX,
    )
    file_hash = hash_bytes(data)

    existing = _find_same(session, product.id, file_hash, asset_type)
    if existing is not None:
        # 同一商品、同一文件、**同一角色**重复上传:返回已有记录,不新增、不覆盖。
        #
        # **但影子写照跑一遍。** 这里原来直接 return,于是"删掉再传回来"是一个
        # 死胡同:`ProductAsset` 这条老记录从来不软删,所以第二次上传永远命中
        # 这一支;而素材侧那条 `MediaAsset` 是 DELETED 的,谁也不会去动它。
        # 运营看到「已存在,沿用已有素材」,素材列表里一张图都不多,
        # 而同一张图从此再也传不上来 —— 提示说的是成功,发生的是什么都没发生。
        #
        # `shadow_from_product_asset` 在正常重复上传下是幂等的(`ingest` 命中
        # 去重、不覆盖任何字段),所以这一跳的代价只有一次查询;
        # 而它换来的是那条已删除素材被这次上传复活。
        _shadow(session, product, existing, color_variant_id=color_variant_id)
        return existing, True

    stored = storage.save(
        validated.data,
        file_hash=file_hash,
        extension=validated.info.extension,
        prefix="uploads",
    )

    asset = ProductAsset(
        product_id=product.id,
        asset_type=asset_type.value,
        file_hash=file_hash,
        mime_type=validated.info.mime_type,
        file_size=len(data),
        width=validated.info.width,
        height=validated.info.height,
        storage_path=stored.storage_path,
        original_filename=sanitize_filename(filename),
        check_passed=validated.check_passed,
        check_message=validated.check_message,
    )
    # 先查后插不是原子的:两个请求同时传同一商品、同一文件、同一角色时,
    # 两边都可能在上面那次查询里看不到对方,于是都走到这里,
    # 其中一个撞 `uq_product_assets_product_id_file_hash` —— 而 IntegrityError
    # 会一路冒到 `unhandled_handler`,运营看到的是「服务内部错误」,
    # 而实际发生的事情是「这张图已经传上去了」。两者该说的话完全不同。
    #
    # 保存点是必须的:不开保存点的话,冲突会让**整个外层事务**进入
    # 需要回滚的状态,后面的影子写、状态刷新、审计全部做不成。
    try:
        with session.begin_nested():
            session.add(asset)
            session.flush()
    except IntegrityError:
        # 回到保存点,对方那条已经提交或在同一事务里可见了,直接复用。
        # 查不到才是真异常 —— 那说明撞的不是我们以为的那条约束。
        winner = _find_same(session, product.id, file_hash, asset_type)
        if winner is None:
            raise
        return winner, True

    # 影子写(迁移 A,§4.4)。**同事务**,不自己 commit。
    #
    # 分两个事务写的话,业务提交成功而影子写失败时两边会永久不一致,
    # 而且没有任何东西会发现 —— 对账要到迁移 B 才上线。所以它失败就让
    # 整笔上传失败:迁移期的正确性比可用性重要,一个静默失败的影子写
    # 会产出一个"看起来成功"的迁移,而它要到切换读路径那天才暴露。
    _shadow(session, product, asset, color_variant_id=color_variant_id)

    product_service.refresh_status_after_asset_change(session, product)

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPLOAD,
        entity_type="ProductAsset",
        entity_id=asset.id,
        payload={
            "product_id": str(product.id),
            "asset_type": asset_type.value,
            "file_hash": file_hash,
            "deduplicated_on_disk": stored.deduplicated,
        },
    )
    return asset, False
