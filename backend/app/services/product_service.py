"""商品业务逻辑。所有写操作都伴随审计记录。"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Select, false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import garments
from app.core.enums import (
    PUBLISH_TERMINAL_STATES,
    Audience,
    AuditAction,
    DraftStatus,
    GarmentType,
    ProductStatus,
    PublishStatus,
)
from app.core.errors import DuplicateError, ErrorCode, NotFoundError, ValidationError
from app.core.search import ESCAPE_CHAR, like_pattern
from app.core.sorting import normalize_sort
from app.listings import image_set_service, variants
from app.models.listing_copy import ListingDraft
from app.models.product import Product
from app.models.product_asset import ProductAsset
from app.models.publishing import ChannelListing
from app.models.spu import ColorVariant, Spu
from app.services import audit
from app.services.product_import import ImportResult, RowError
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


def assert_colour_belongs_to(
    session: Session, product: Product, color_variant_id: UUID
) -> None:
    """这个颜色确实是这件商品所在 SPU 的颜色(§4.3)。

    ## 为什么必须查,而不是信任前端传上来的 id

    §4.3 那条约束写得很清楚:颜色名在 SPU 内唯一,**跨 SPU 同名是常态**
    (几乎每个款都有黑色)。所以光看一个 UUID 分不出它属于谁 —— 而传错的
    后果不是报错:那张图会挂到**另一个款**的颜色上,于是

        它进不了本商品那个颜色的完整度门禁(表现:传了图还说缺图)
        它却会让**另一个 SPU** 的颜色事实过期(表现:一个没人动过的款
        突然一批字段回到待确认)

    两个现象都不指向"上传时选错了颜色",而且发生在两个不同的页面上。

    ## 存量商品没有 spu_id 时一律拒绝

    `create_product` 与 CSV 导入现在都会解析并写入 `products.spu_id`；这里的
    NULL 分支只为升级前已经存在、尚未回填归属的存量行保留。那些行给不出
    "本商品所在 SPU"这个答案,因此没有依据判断一个颜色属不属于它。

    拒绝而不是放行:放行等于允许把图挂到一个查不出关系的颜色上,
    而那正是上面第二条的来路。存量行应先补归属,不能借上传动作猜归属。
    """
    if product.spu_id is None:
        raise ValidationError(
            # A69:原来这里写「走三步建档(POST /spus)」。前半句「三步建档」
            # 是运营认识的,后半句括号里那个不是 —— 而括号会让人以为
            # 那才是准确说法。删掉它,前半句本来就够指路
            "这件商品还没有挂到款上,不能按颜色上传素材;"
            "从「新建款式」走完三步建出来的商品才有颜色可选",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    variant = session.get(ColorVariant, color_variant_id)
    if variant is None or variant.spu_id != product.spu_id:
        raise ValidationError(
            "这个颜色不属于本商品所在的 SPU。颜色名在 SPU 内唯一,"
            "跨 SPU 同名是常态 —— 请从本商品的颜色列表里选",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
        )


def colours_for(session: Session, product: Product) -> list[ColorVariant]:
    """这件商品**可以选**的颜色(§4.3)。按 `sort_order` 排。

    ## 为什么是一个函数,而不是让前端自己走 product -> spu -> colours

    这份清单是 `assert_colour_belongs_to` 那道校验的**补集的补集**:
    界面能选到的,必须恰好是服务端会接受的。让前端自己拼这条链路的话,
    两份口径立刻分叉,而分叉的表现不对称 ——

        界面多列了一个颜色    运营选中、上传、拿到一个 422,他不知道为什么
        界面少列了一个颜色    那个颜色**永远传不了图**,于是它永远缺正面图,
                              而完整度门禁会一直说缺图,没有任何提示指向原因

    后一种没有任何人会报成 bug。所以两边共用同一个取数,而不是共用一句约定。

    ## 没有 SPU 时返回空表,不抛错

    与 `assert_colour_belongs_to` 的 409 方向一致但语气不同:那里是
    「你**做**了一件做不了的事」,这里是「你能选的有哪些」——
    答案是"一个也没有",那是一句完整的话。抛错的话,一件老路径商品的
    素材页会整页报错,而它本来只是不能按颜色上传而已。
    """
    if product.spu_id is None:
        return []
    return list(
        session.scalars(
            select(ColorVariant)
            .where(ColorVariant.spu_id == product.spu_id)
            .order_by(ColorVariant.sort_order, ColorVariant.variant_code)
        )
    )


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
    # **§4.2 那条 NULL 兼容缝在这里关掉(A45-batch14-26)。**
    #
    # 在此之前这个函数从不写 `spu_id`,于是走这条路建出来的商品:
    #   - `spu_id` 为空 → 它不属于任何 SPU
    #   - `audience` 可空 → **绕过了 §4.2「受众必填」**
    # 而受众填错的后果 `create_spu` 里写着:模特、提示词、槽位表、检查项、
    # 尺码表、平台类目全跟着错,每一步单看都是"正常完成"。
    #
    # 关的方式是**解析**,不是**新建**。这里不调 `create_spu`:那个函数会按
    # 尺码模板展开一整套 SKU,从"建一个商品"里长出十几行商品来 ——
    # 而且它会成为第二条建档路径,`seed_sample_data` 当初刻意不写第二条,
    # 理由在那边(§"seed 走 spu_service.create_spu(),不另写建档路径")。
    #
    # 查不到就拒绝,错误信息指向"先建 SPU"。**代价写明**:走 `POST /api/products`
    # 这条路建商品从此要求 SPU 先存在。这是真实的行为变更,不是兼容性疏漏 ——
    # 放行的代价是每一条走老路径的商品继续绕过受众必填,而那批商品
    # 正是将来 `spu_id` 收 NOT NULL 时挡在路上的那批。
    #
    # CSV 导入不调用本函数,但现在遵守同一份身份契约:`import_products` 通过
    # `_resolve_import_identity` 要求 SPU 先存在,多颜色 SPU 还必须明确给出
    # `variant_code`;找不到身份的行进入 `errors`,不会自动造最简 SPU。
    # 两条入口必须同时写 `spu_id` / `color_variant_id` / SPU 权威受众与品类,
    # 否则同一份商品会因为入口不同而得到两套下游事实。
    spu_code = (data.get("spu") or "").strip()
    if not spu_code:
        raise ValidationError(
            "SPU 编码必填:商品必须挂在一个 SPU 下,受众、尺码表、平台类目都由它决定",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
        )
    spu_row = session.scalar(select(Spu).where(Spu.spu_code == spu_code))
    if spu_row is None:
        raise ValidationError(
            # A69:原来这句写的是「请先用 POST /spus 建档」。运营界面上没有
            # 「POST /spus」这个东西,对应的是款式列表页的「新建款式」——
            # 报错要指的是**他点得到的那个动作**,不是我们内部怎么实现的
            f"款号 {spu_code} 还没有建过:请先在款式列表里新建这个款,再往它下面加 SKU。"
            "受众只能在款上填(§4.2),跳过建款直接建 SKU 会把这一步绕过去",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
        )

    # 受众的权威是 `spus.audience`(batch13 / §4.2),`products.audience` 是它的
    # 反规范化副本。**从 SPU 抄,不用入参那一份** —— 两者不一致时信 SPU,
    # 否则调用方可以用一个入参把商品行的受众改成和它 SPU 不同的值,
    # 而 `category_code_for` 读的是商品行那一份。
    data = dict(data)
    data["audience"] = spu_row.audience

    # 受众 × 品类组合校验(C-03)。update 那道闸如果不在 create 上重复,
    # 想绕开的人删了重建就行。**现在受众恒非空**(上面从 SPU 抄的),
    # 所以这道闸从"未确认时不拦"变成永远真的在判 —— 那正是这条缝的意义
    block = garments.garment_block_reason(data.get("audience"), data.get("garment_type"))
    if block is not None:
        raise ValidationError(block, code=ErrorCode.INPUT_INVALID, http_status=422)

    product = Product(**{k: v for k, v in data.items() if v is not None})
    # 归属外键。`spu` 字符串留着是反规范化读列(§4.4),两者由这里一起写死,
    # 不给"码写了、外键没写"留缝
    product.spu_id = spu_row.id
    product.spu = spu_row.spu_code
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


#: 商品可以归档所要求的「平台侧已经了结」。
#:
#: 比 `PUBLISH_TERMINAL_STATES` 多一个 `DELISTED`:那两个终态说的是
#: **这次发布任务**结束了(取消 / 归档),而下架说的是**商品不在平台上了** ——
#: 后者才是这道闸真正要问的事。一件已经下架的商品当然可以在本地归档。
_PUBLISH_SETTLED: frozenset[str] = PUBLISH_TERMINAL_STATES | {PublishStatus.DELISTED.value}


def live_listings_for(
    session: Session, product_ids: Sequence[UUID]
) -> list[ChannelListing]:
    """这批商品在平台上还挂着的发布记录。**「还挂着」只有这一个定义。**

    公开出去是因为 SPU 停用要问同一个问题(`spu_service.disable_spu`):
    这个款底下有没有 SKU 还在平台上。在那边自己写一遍 `notin_(...)` 的话,
    `_PUBLISH_SETTLED` 就有了两个读者而只有一个会跟着改 —— 而它已经被
    改过一次(DELISTED 是后补进去的,见上面那段注释)。

    一次 `IN` 查询,不按行循环。空列表直接回空:`IN ()` 在部分方言下
    是语法错误,而这个函数的调用方拿到的常常是"这个 SPU 底下一行都没有"。
    """
    ids = list(product_ids)
    if not ids:
        return []
    return list(
        session.scalars(
            select(ChannelListing).where(
                ChannelListing.product_id.in_(ids),
                ChannelListing.status.notin_(sorted(_PUBLISH_SETTLED)),
            )
        )
    )


def _live_listings(session: Session, product_id: UUID) -> list[ChannelListing]:
    """这件商品在平台上还挂着的发布记录。"""
    return live_listings_for(session, [product_id])


def archive_product(
    session: Session, product_id: UUID, *, reason: str, actor: str
) -> Product:
    """归档一件商品。**这是本系统里「删除商品」的全部含义。**

    ## 为什么不是 DELETE

    `products.id` 被九张表引着,而两个方向都不能接受:

        channel_listings          ondelete=RESTRICT   平台上还挂着的商品删不掉,
                                                      硬删会撞 IntegrityError -> 500
        media_assets / attributes / ondelete=CASCADE   删得掉,但会连带清空
        generation_tasks /                            这件商品的素材、任务、属性、
        output_assets / evaluations                   评估 —— 整条证据链没了,
                                                      而运营点的那个按钮只写着「删除」

    `publishing.py` 那条 RESTRICT 的注释把话说在前面了:平台上还挂着的商品,
    不能因为本地删了就悄悄失去它的来源记录。所以这里做的是**状态迁移**:
    行还在,证据链还在,只是不再出现在生产动线里(见 `api/workbench.py`
    的列表默认过滤)。

    ## 平台闸

    还挂在平台上的商品拒绝归档,并把渠道和站点说出来 —— 只说
    「不能归档」的话,运营下一步不知道该去哪个后台下架。

    幂等:已经归档的再归档一次直接返回,不重复写审计。运营连点两下、
    或者请求超时后重试,不该拿到一个 409。
    """
    product = get_product(session, product_id)
    if product.status == ProductStatus.ARCHIVED.value:
        return product

    live = _live_listings(session, product.id)
    if live:
        where = ", ".join(sorted({f"{r.channel}/{r.site}" for r in live})[:5])
        raise ValidationError(
            f"这件商品在平台上还挂着({where}),先在平台下架再归档",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
            detail={"live_listing_ids": [str(r.id) for r in live]},
        )

    previous = product.status
    product.status = ProductStatus.ARCHIVED.value
    session.flush()
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="Product",
        entity_id=product.id,
        payload={
            "action": "archive",
            # 理由进审计而不是新开一列:归档是低频动作,而「谁、什么时候、
            # 为什么」这三样审计表本来就答得出来。为它加一列要一次迁移,
            # 换来的只是把同一句话再存一遍
            "reason": (reason or "")[:500],
            "from_status": previous,
            "sku": product.sku,
        },
    )
    return product


def restore_product(session: Session, product_id: UUID, *, actor: str) -> Product:
    """把归档的商品放回生产动线。

    **恢复到 DRAFT,不恢复到归档前那一档。** 归档前是 `COMPLETED` 的商品
    在归档期间上游可能变过(素材被隔离、方案换代),直接还原成
    「已完成」会让它带着一个没人复核过的结论回到列表里。DRAFT 是
    `refresh_status_after_asset_change()` 的起点,回到那里之后由既有规则
    重新算一遍它现在到底走到哪 —— 那比记住一个可能已经过期的旧答案诚实。
    """
    product = get_product(session, product_id)
    if product.status != ProductStatus.ARCHIVED.value:
        raise ValidationError(
            f"只有已归档的商品可以恢复,当前状态 {product.status}",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    product.status = ProductStatus.DRAFT.value
    session.flush()
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="Product",
        entity_id=product.id,
        payload={"action": "restore", "to_status": product.status, "sku": product.sku},
    )
    refresh_status_after_asset_change(session, product)
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


def _import_identity_maps(
    session: Session, parsed: ImportResult
) -> tuple[dict[str, Spu], dict[tuple[UUID, str], ColorVariant]]:
    """一次取齐导入预览与提交共用的 SPU / 颜色事实。"""
    spu_codes = {str(row["spu"]).strip() for row in parsed.rows}
    spus = {
        row.spu_code: row
        for row in session.scalars(select(Spu).where(Spu.spu_code.in_(spu_codes)))
    }
    variants_by_key = {
        (variant.spu_id, variant.variant_code): variant
        for variant in session.scalars(
            select(ColorVariant).where(
                ColorVariant.spu_id.in_([row.id for row in spus.values()])
            )
        )
    }
    return spus, variants_by_key


def _resolve_import_identity(
    row: dict[str, Any],
    row_number: int,
    *,
    spus: dict[str, Spu],
    variants_by_key: dict[tuple[UUID, str], ColorVariant],
) -> tuple[Spu | None, ColorVariant | None, RowError | None]:
    spu = spus.get(str(row["spu"]).strip())
    if spu is None:
        return None, None, RowError(
            row_number, "spu", f"SPU {row['spu']} 不存在，请先建档"
        )

    variant_code = str(row.get("variant_code") or "").strip().upper()
    candidates = [
        variant
        for (owner_id, _code), variant in variants_by_key.items()
        if owner_id == spu.id
    ]
    variant = (
        variants_by_key.get((spu.id, variant_code))
        if variant_code
        else candidates[0]
        if len(candidates) == 1
        else None
    )
    if variant is None:
        message = (
            f"SPU {spu.spu_code} 下不存在颜色 {variant_code}"
            if variant_code
            else f"SPU {spu.spu_code} 有多个颜色，请明确填写 variant_code"
        )
        return None, None, RowError(row_number, "variant_code", message)

    block = garments.garment_block_reason(spu.audience, row.get("garment_type"))
    if block is not None:
        return None, None, RowError(row_number, "garment_type", block)
    return spu, variant, None


def import_validation_errors(
    session: Session,
    parsed: ImportResult,
    *,
    existing_skus: set[str] | None = None,
) -> list[RowError]:
    """只读校验需要数据库事实的导入问题，供预览与提交共享。"""
    existing = existing_skus or set()
    spus, variants_by_key = _import_identity_maps(session, parsed)
    errors: list[RowError] = []
    for position, row in enumerate(parsed.rows):
        if row["sku"] in existing:
            continue
        row_number = (
            parsed.row_numbers[position]
            if position < len(parsed.row_numbers)
            else position + 1
        )
        _spu, _variant, problem = _resolve_import_identity(
            row,
            row_number,
            spus=spus,
            variants_by_key=variants_by_key,
        )
        if problem is not None:
            errors.append(problem)
    return errors


def import_products(session: Session, parsed: ImportResult, *, actor: str) -> dict[str, Any]:
    """落库导入结果。

    已存在的 SKU 视为跳过而非错误 —— 批量导入经常是增量补充,重复执行必须安全(幂等)。

    CSV 导入现在要求 SPU 先存在；多颜色 SPU 要求明确给出 `variant_code`，
    单颜色 SPU 可以无歧义地补上唯一颜色。
    不自动造最简 SPU：受众、类目和颜色身份都属于建档决策，导入器猜一个值会让
    错误沿模特、提示词、尺码表和渠道类目一路传播。找不到 SPU 或颜色的行进入
    `errors`，其余正确行仍在各自保存点里落库。
    """
    created_ids: list[UUID] = []
    skipped = 0

    existing = set(
        session.scalars(
            select(Product.sku).where(Product.sku.in_([r["sku"] for r in parsed.rows] or [""]))
        )
    )

    errors = list(parsed.errors)
    spus, variants_by_key = _import_identity_maps(session, parsed)

    for position, row in enumerate(parsed.rows):
        row_number = (
            parsed.row_numbers[position]
            if position < len(parsed.row_numbers)
            else position + 1
        )
        if row["sku"] in existing:
            skipped += 1
            continue
        spu, variant, problem = _resolve_import_identity(
            row,
            row_number,
            spus=spus,
            variants_by_key=variants_by_key,
        )
        if problem is not None:
            errors.append(problem)
            continue
        if spu is None or variant is None:  # pragma: no cover - helper contract
            raise RuntimeError("导入身份解析返回了不完整结果")
        product_data = {
            key: value
            for key, value in row.items()
            if key not in {"variant_code", "audience", "category"}
        }
        product_data.update(
            spu=spu.spu_code,
            spu_id=spu.id,
            color_variant_id=variant.id,
            audience=spu.audience,
            category=spu.base_category,
        )
        product = Product(**product_data)
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
        payload={"created": len(created_ids), "skipped": skipped, "failed": len(errors)},
    )

    return {
        "created": len(created_ids),
        "skipped_existing": skipped,
        "failed": len(errors),
        "errors": [
            {"row_number": e.row_number, "field": e.field, "message": e.message}
            for e in errors
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
