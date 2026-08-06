"""商品业务逻辑。所有写操作都伴随审计记录。"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import garments
from app.core.enums import Audience, AuditAction, DraftStatus, GarmentType, ProductStatus
from app.core.errors import DuplicateError, ErrorCode, NotFoundError, ValidationError
from app.core.search import ESCAPE_CHAR, like_pattern
from app.core.sorting import normalize_sort
from app.listings import image_set_service, variants
from app.models.listing_copy import ListingDraft
from app.models.product import Product
from app.models.product_asset import ProductAsset
from app.models.spu import Spu
from app.services import audit
from app.services.product_import import ImportResult
from app.services.sorting import apply_order

#: 商品列表允许排序的字段 -> 列。白名单放在这里(而不是 API 层),因为「能不能按
#: 这个字段排」是这张表的性质:有没有索引、值是不是可空、排出来对运营有没有意义。
#:
#: `asset_count` **刻意不在表里**。它不是 products 的列,是另一条 group by 查出来的
#: 计数(`asset_counts`),要按它排就得把那条查询并进主 select。为了一个次要维度
#: 换一次 JOIN + 全表聚合,不值当;等真的有人要按素材数排的时候再说。
SORTABLE = {
    "created_at": Product.created_at,
    "updated_at": Product.updated_at,
    "sku": Product.sku,
    "spu": Product.spu,
    "name": Product.name,
    "status": Product.status,
    "category": Product.category,
}


def get_product(session: Session, product_id: UUID) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise NotFoundError("商品不存在")
    return product


def _sku_exists(session: Session, sku: str) -> bool:
    return session.scalar(select(func.count()).select_from(Product).where(Product.sku == sku)) > 0


def create_product(session: Session, data: dict[str, Any], *, actor: str) -> Product:
    """新建商品。

    先查后插只能挡住「明显重复」,挡不住并发:两个请求同时查到「不存在」,
    第二个会在 flush 时撞唯一约束,以 500 返回。真正的裁判是数据库约束,
    所以这里把 IntegrityError 翻译成 409,查询只作为快速路径保留。
    保存点是必需的 —— 约束冲突会让整个事务进入 aborted 状态,不回滚就没法继续。
    """
    if _sku_exists(session, data["sku"]):
        raise DuplicateError(f"SKU {data['sku']} 已存在")

    # 受众 × 品类组合校验(C-03)。update 那道闸如果不在 create 上重复,
    # 想绕开的人删了重建就行 —— 与授权字段"两个入口一起收"是同一条道理。
    # 受众未确认(None)不拦:那时"该出哪一组"还没有答案
    block = garments.garment_block_reason(data.get("audience"), data.get("garment_type"))
    if block is not None:
        raise ValidationError(block, code=ErrorCode.INPUT_INVALID, http_status=422)

    product = Product(**{k: v for k, v in data.items() if v is not None})
    # 变体身份在这里定下来,**必须在 add 之前**(A44)。
    # 先 add 的话它会在同门兄弟里查到自己,于是「已有同色变体」永远成立,
    # 复用逻辑从第 2 行起就再也不会被执行 —— 而那是它唯一的用处。
    variants.assign_variant_key(session, product)
    savepoint = session.begin_nested()
    try:
        session.add(product)
        session.flush()
        savepoint.commit()
    except IntegrityError:
        savepoint.rollback()
        raise DuplicateError(f"SKU {data['sku']} 已存在") from None
    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="Product",
        entity_id=product.id,
        payload={"sku": product.sku, "spu": product.spu},
    )
    return product


def _is_column(name: str) -> bool:
    """这个键是不是 `products` 表上的真实列。"""
    return Product.__table__.columns.get(name) is not None


def _is_identity_column(name: str) -> bool:
    """这一列是不是「身份」——**存在于表上,但任何接口都不许改**(A44)。

    问模型上的 `info={"identity": True}`,**不维护第二张名单**。
    手写一张名单的话,它会和模型定义分叉,而分叉的表现是某一天有人
    加了一列身份、忘了加进名单,于是它可以从商品编辑接口改掉 ——
    那正是 `variant_key` 存在的理由被抹掉的方式。

    为什么 `_is_column` 挡不住它:`variant_key` **是**一列真列,
    那条检查会放行。身份和普通字段的区别不在\"是不是列\",
    在\"改了之后已经引用它的东西会不会全部指空\"。
    """
    column = Product.__table__.columns.get(name)
    return bool(column is not None and column.info.get("identity"))


def _nullable(column_name: str) -> bool:
    """这一列允不允许存 NULL。**问表,不维护第二张清单。**

    手写一张「可空字段名单」的话,它会和模型定义分叉,而分叉的表现是
    某个字段清不掉、或者某个字段清掉之后撞数据库约束报 500 ——
    两种都不会有人在改模型的时候想起来。
    """
    column = Product.__table__.columns.get(column_name)
    return bool(column is not None and column.nullable)


def update_product(
    session: Session, product_id: UUID, changes: dict[str, Any], *, actor: str
) -> Product:
    product = get_product(session, product_id)
    prior_audience = product.audience

    # ---- A45-batch13-3 / R2:SPU 管理的行,受众不许清空 ----
    #
    # batch13 起受众的权威在 `spus.audience`,且那一列 NOT NULL(§4.2:SPU 层
    # 不存在"待确认受众")。带 `spu_id` 的行把受众清成 NULL,只有两种结局:
    # 不同步权威 —— 权威 WOMEN、九份副本 NULL,静默分叉;同步权威 —— 撞
    # NOT NULL 约束,flush 阶段 500。两条都不该发生,所以在入口拒绝。
    # 旧路径(`spu_id` 为空)不变:那里 NULL 仍然是合法的"待确认受众"。
    if "audience" in changes and changes["audience"] is None and product.spu_id is not None:
        raise ValidationError(
            "这一行属于一个已建档的 SPU,受众的权威在 SPU 上且必填(§4.2)。"
            "SPU 层没有'待确认受众'——要换受众,直接改成新值;"
            "整个款不做了走 SPU 停用,不是把受众清空",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
        )

    applied: dict[str, Any] = {}
    for key, value in changes.items():
        # 不认识的键当场报错,**不要静默跳过**。
        #
        # 以前走的是 `setattr(product, key, value)` —— 一个不是列的键会被安静地
        # 挂到 ORM 对象上,flush 时无视,接口返回 200。于是 schema 加了字段
        # 而这里没跟上时,「保存成功了但值没变」是唯一的症状。
        #
        # 顺带给 `_nullable()` 兜住底:它查的是表,查不到就说明这个键
        # 根本不该走到这里。
        if not _is_column(key):
            raise ValidationError(
                f"{key} 不是可更新的商品字段",
                code=ErrorCode.INPUT_INVALID,
                http_status=422,
            )
        # 身份列在表上但不许改(A44)。
        #
        # 改掉 `variant_key` 的后果不是\"这一列变了\":已确认的颜色属性、
        # 已绑定的图片标签、导出文件的变体列会同时指向一个不存在的变体,
        # 而三者都不报错。这正是这一列被引入所要消灭的那件事,
        # 从编辑接口放进来等于原地绕开它。
        if _is_identity_column(key):
            raise ValidationError(
                f"{key} 是变体身份,创建后不可修改;要改颜色名请改 primary_color",
                code=ErrorCode.INPUT_INVALID,
                http_status=422,
            )
        # **不要在这里跳过 None。**
        #
        # 「未传字段不动」已经由接口层的 `model_dump(exclude_unset=True)` 做完了 ——
        # 走到这里的每一个键都是调用方**显式写在请求体里**的。再跳过 None
        # 等于把「把这个字段清空」静默降级成什么都不做,而接口返回 200,
        # 运营看到"保存成功"、刷新后值还在。
        #
        # 非空列仍然要挡:清空它会撞数据库约束,报 500 不如报一句人话。
        if value is None and not _nullable(key):
            raise ValidationError(
                f"{key} 不允许清空",
                code=ErrorCode.INPUT_INVALID,
                http_status=422,
            )
        if getattr(product, key, None) != value:
            setattr(product, key, value)
            applied[key] = value

    # ---- 受众 × 品类组合校验(A45 独立审查 C-03) ----
    #
    # 前端下拉的收窄只是就近提示,这里才是拦截点:接口直连、批量脚本、
    # 以及"换受众时品类键被 JSON 序列化丢掉"的路径都汇到这一行。
    # 词表在 `core/garments.py`,与前端那份由跨语言契约测试钉住。
    if "audience" in applied or "garment_type" in applied:
        block = garments.garment_block_reason(product.audience, product.garment_type)
        if block is not None:
            if "garment_type" in changes:
                # 调用方**显式**声明了这个组合 —— 拒绝,不替它改(§4.2:
                # 不静默改写)。改成什么由调用方自己重新决定
                raise ValidationError(
                    block, code=ErrorCode.INPUT_INVALID, http_status=422
                )
            # 只改了受众、库里旧品类越界:显式写回 OTHER。这不是静默 ——
            # 它进 applied、进审计的重置清单,界面弹窗也预告过"品类会被
            # 清空待重选"。拒绝的话这次受众修改从接口上根本做不完:
            # 调用方(含旧版前端)未必会带 garment_type 键
            product.garment_type = GarmentType.OTHER.value
            applied["garment_type"] = GarmentType.OTHER.value

    # 受众挂在 SPU 上,不是挂在 SKU 上(§3.4 规则 2)。
    #
    # `products` 是 SKU 级表,而这个接口改的是一行。只改一行的后果与导入侧
    # 那条被拦下的不一致完全相同 —— 草稿是 SPU 级的,它的规则包取决于
    # **查到哪一行**:运营从红色 SKU 进去看到男装草稿,从蓝色 SKU 进去看到
    # 女装草稿,两边写的是同一行草稿。
    #
    # 所以受众要么整个 SPU 一起改,要么不改。这里选择整体改写而不是
    # 「拒绝并要求逐行改」:后者把一个本该原子的操作交给运营手工保持一致,
    # 而中途关掉页面就留下一个半改的 SPU。
    cascade: dict[str, Any] = {}
    if "audience" in applied:
        new_audience = applied["audience"]
        cascade["audience"] = {"from": prior_audience, "to": new_audience}

        # ---- A45-batch13-3 / R2:权威跟着改,不许分叉 ----
        #
        # batch13 把 `spus.audience` 定为受众唯一权威(§4.2),而这个函数
        # 是运营改受众的**标准入口**(带二次确认、兄弟行传播、图片集降级)。
        # 只改九份副本、不动权威的话,SPU 详情页和每一行 SKU 从此各说各话,
        # 而且没有任何诊断;等阶段 1 第二批把读取方切到权威列,这次修改会被
        # **静默撤销** —— §3.17 的同一个形状,只是引信更长。所以副本怎么改,
        # 权威就怎么改,在同一个事务里。上面那道闸保证走到这里 `new_audience`
        # 一定非空(SPU 管理的行清空已经被拒),NOT NULL 撞不上。
        if product.spu_id is not None:
            spu_row = session.get(Spu, product.spu_id)
            if spu_row is not None and spu_row.audience != new_audience:
                # StrEnum 直接落列也行,但权威列存的是取值,不是枚举对象
                spu_row.audience = (
                    new_audience.value
                    if isinstance(new_audience, Audience)
                    else new_audience
                )
                cascade["spu_audience_synced"] = spu_row.spu_code

        reset_skus: list[str] = []
        if applied.get("garment_type") == GarmentType.OTHER.value and (
            "garment_type" not in changes
        ):
            reset_skus.append(product.sku)

        # 兄弟行:字符串命中 ∪ 外键命中(A45-batch13-3 / R2)。
        #
        # 只按 `Product.spu` 字符串查,对带外键的行违反 §4.4(反规范化列
        # 禁止作为查询权威);只按外键查,又会漏掉混合期里同一个 SPU 字符串
        # 底下没有外键的存量/导入行 —— 而"同 SPU 各行受众必须一致"从来是按
        # 业务上的同款说的。数据一致时两个集合相等,不一致时并集才是全部。
        sibling_filter = Product.spu == product.spu
        if product.spu_id is not None:
            sibling_filter = or_(sibling_filter, Product.spu_id == product.spu_id)
        siblings = list(
            session.scalars(
                select(Product).where(sibling_filter, Product.id != product.id)
            )
        )
        changed_siblings = 0
        for sibling in siblings:
            if sibling.audience != new_audience:
                sibling.audience = new_audience
                changed_siblings += 1
            # 兄弟行的品类也要一起过组合校验:只搬受众不看品类,
            # 传播本身就会**制造** MEN + BIKINI_SET(C-03 的第二半)
            if not garments.garment_allowed(new_audience, sibling.garment_type):
                sibling.garment_type = GarmentType.OTHER.value
                reset_skus.append(sibling.sku)
        if changed_siblings:
            # 记进审计:同 SPU 的其它 SKU 也被这一次操作改了,
            # 而运营在界面上只看到自己改了一件商品
            applied["audience_propagated_to"] = changed_siblings
        if reset_skus:
            cascade["garment_type_reset_on"] = sorted(reset_skus)

        # ---- C-04:原受众的已批准图片集降级为待复核(同一事务) ----
        # 界面弹窗承诺"作废已生成图片";在素材层拿到生成溯源列之前,
        # 强制回到人工复核是能兑现的最强版本(细节见 image_set_service)
        downgraded = image_set_service.downgrade_sets_on_audience_change(
            session,
            spu=product.spu,
            actor=actor,
            from_audience=prior_audience,
            to_audience=new_audience,
        )
        if downgraded:
            cascade["image_sets_downgraded"] = len(downgraded)

        # ---- C-05:旧草稿立即 STALE(同一事务) ----
        # 指纹机制(category_id 随受众派生)本会在下一次 refresh_draft 时
        # 发现过期,但只读路径刻意 dry_run 不落库、提交入口只看存储状态 ——
        # 不在这里写,一份 VALIDATED 的旧草稿就仍然可以把旧受众内容排队
        # 提交。ARCHIVED 不动(它已归档,上游怎么变都与它无关)。
        stale_rows = list(
            session.scalars(
                select(ListingDraft).where(
                    ListingDraft.spu == product.spu,
                    ListingDraft.status.notin_(
                        [DraftStatus.ARCHIVED.value, DraftStatus.STALE.value]
                    ),
                )
            )
        )
        for row in stale_rows:
            row.status = DraftStatus.STALE.value
        if stale_rows:
            cascade["drafts_marked_stale"] = len(stale_rows)

    if applied:
        session.flush()
        audit.record(
            session,
            actor=actor,
            action=AuditAction.UPDATE,
            entity_type="Product",
            entity_id=product.id,
            payload={"changes": list(applied), **({"cascade": cascade} if cascade else {})},
        )
    return product


def list_products(
    session: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    category: str | None = None,
    garment_type: str | None = None,
    id_in: set[UUID] | Select | None = None,
    sort: str | None = None,
    order: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[Product], int]:
    """商品列表 + 总数。

    `id_in` 把"成品图是否齐全"这类需要跨表聚合的条件下推到分页之前
    (BLOCK-17)。它接受两种形态:

        Select      子查询,整个筛选留在数据库里 —— 规模无关,首选
        set[UUID]   已经算好的一批 id。只在规模确定有限时用

    传空集合表示"没有任何商品符合",返回空页而不是忽略这个条件 ——
    忽略的话 `only_complete=true` 在没有完整商品时会退化成"导出全部"。
    """
    sort_field, direction = normalize_sort(
        sort, order, allowed=SORTABLE, default_sort="created_at", default_order="desc"
    )
    stmt = select(Product)
    if search:
        # 通配符要转义:`_` 在真实 SKU 里很常见,不转的话搜 `SW-001_BLK`
        # 会命中 `SW-001XBLK`,而搜一个 `%` 直接返回全表(见 core/search.py)
        pattern = like_pattern(search)
        stmt = stmt.where(
            or_(
                Product.sku.ilike(pattern, escape=ESCAPE_CHAR),
                Product.spu.ilike(pattern, escape=ESCAPE_CHAR),
                Product.name.ilike(pattern, escape=ESCAPE_CHAR),
            )
        )
    if status:
        stmt = stmt.where(Product.status == status)
    if category:
        stmt = stmt.where(Product.category == category)
    if garment_type:
        stmt = stmt.where(Product.garment_type == garment_type)
    if id_in is not None:
        if isinstance(id_in, Select):
            stmt = stmt.where(Product.id.in_(id_in))
        else:
            # 空集合要走 `IN ()` 的语义(匹配不到任何行),不能当作"没传"
            stmt = stmt.where(Product.id.in_(id_in) if id_in else false())

    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    ordered = apply_order(
        stmt, columns=SORTABLE, sort=sort_field, order=direction, tiebreak=Product.id
    )
    rows = list(session.scalars(ordered.offset(offset).limit(limit)))
    return rows, total


def asset_counts(session: Session, product_ids: list[UUID]) -> dict[UUID, int]:
    if not product_ids:
        return {}
    stmt = (
        select(ProductAsset.product_id, func.count())
        .where(ProductAsset.product_id.in_(product_ids))
        .group_by(ProductAsset.product_id)
    )
    return {pid: count for pid, count in session.execute(stmt)}


def import_products(session: Session, parsed: ImportResult, *, actor: str) -> dict[str, Any]:
    """落库导入结果。

    已存在的 SKU 视为跳过而非错误 —— 批量导入经常是增量补充,重复执行必须安全(幂等)。
    """
    created_ids: list[UUID] = []
    skipped = 0

    existing = set(
        session.scalars(
            select(Product.sku).where(Product.sku.in_([r["sku"] for r in parsed.rows] or [""]))
        )
    )

    for row in parsed.rows:
        if row["sku"] in existing:
            skipped += 1
            continue
        product = Product(**row)
        # 同 `create_product`:身份在 add 之前定(A44)。
        #
        # 导入一批同 SPU 多颜色多尺码的行时,这一句是「红色 S / 红色 M
        # 落到同一个变体」的全部保证 —— 前一行已经 flush,查得到。
        variants.assign_variant_key(session, product)
        # 每行一个保存点:某一行撞了约束(并发导入同一批 SKU)只让这一行算跳过,
        # 不该把已经导进去的几百行一起回滚掉。
        savepoint = session.begin_nested()
        try:
            session.add(product)
            session.flush()
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()
            skipped += 1
            existing.add(row["sku"])
            continue
        created_ids.append(product.id)
        existing.add(row["sku"])

    audit.record(
        session,
        actor=actor,
        action=AuditAction.IMPORT,
        entity_type="Product",
        payload={"created": len(created_ids), "skipped": skipped, "failed": len(parsed.errors)},
    )

    return {
        "created": len(created_ids),
        "skipped_existing": skipped,
        "failed": len(parsed.errors),
        "errors": [
            {"row_number": e.row_number, "field": e.field, "message": e.message}
            for e in parsed.errors
        ],
        "created_ids": created_ids,
    }


def refresh_status_after_asset_change(session: Session, product: Product) -> None:
    """素材齐备后把 DRAFT 推进到 READY。

    判定标准:至少有一张商品正面图。状态机的完整定义在阶段 3,这里只做这一步最小推进。
    """
    from app.core.enums import AssetType

    if product.status != ProductStatus.DRAFT.value:
        return
    has_front = session.scalar(
        select(func.count())
        .select_from(ProductAsset)
        .where(
            ProductAsset.product_id == product.id,
            ProductAsset.asset_type == AssetType.GARMENT_FRONT.value,
        )
    )
    if has_front:
        product.status = ProductStatus.READY.value
