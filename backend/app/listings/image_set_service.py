"""图片集的读写(M5,§7)。

规则在 `image_set_rules.py`(纯函数),这里只负责把库里的行喂进去、
把结论写回来。

## 不可原地修改

批准之后任何改动都走 `derive()` 生成 `version+1` 的新集。
「平台驳回的是哪一版图」因此永远可查,回滚就是 `approve()` 旧版。
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.enums import AuditAction, ImageSetStatus, MediaStatus
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.field_limits import MAX_IMAGE_SET_ITEMS
from app.core.logging import get_logger
from app.db import locks
from app.listings import image_set_rules as rules
from app.listings import variant_key, variants
from app.models.listing_image import ListingImageItem, ListingImageSet
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.models.spu import Spu
from app.services import audit, generation_plan_service

logger = get_logger(__name__)


def _angle_value(raw) -> str | None:
    """把入参角度归一成库里存的那个字符串。

    入参层已经用 `ImageAngle` 枚举卡过一次(拼错的角度停在 422),
    这里处理的是**另一条路**:`create_set` 的 items 是 `dict`,
    调用方可以是接口层(值已是枚举)、也可以是内部调用(值可能是字符串)。

    `StrEnum` 直接 `str()` 得到的是取值本身,所以两条路在这里合流。
    空串归一成 None —— 库里 NULL 与 `''` 都表示"没标注",
    留两种形状会让 `covered_angles` 的集合里多出一个空元素。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _now():
    return utc_now()


def _to_view(session: Session, row: ListingImageSet) -> rules.SetView:
    """把 ORM 行翻译成规则层的只读视图。

    素材的状态与角色可信度**一起带进去** —— 规则要判断"这张图能不能当主图",
    而那个判断的依据在素材上,不在图片项上。让规则层自己去查库就等于
    它不再是纯函数。

    素材**一次取回**,不逐项 `session.get`(任务 19)。原来那个写法是这条读
    路径上最深的一处 N+1:工作台列表对全量商品逐件 `collect()`,每件解析
    图片集,每版图片集再逐张图打一次往返 —— 三层乘起来。改法照搬同仓库
    `workbench/service._set_items` 的形状,那里对同样两张表本来就是批量取的。

    `session.get` 会先命中 identity map,批量 `IN` 不会;所以素材恰好全在
    身份映射里时,这里从 0 次往返变成 1 次。换来的是**次数不再随图片张数
    增长**,而列表页的代价正出在张数上。
    """
    items = list(
        session.scalars(
            select(ListingImageItem).where(ListingImageItem.image_set_id == row.id)
        )
    )
    # 空集直接跳过:`IN ()` 是一次查不出任何东西的往返
    asset_ids = [item.media_asset_id for item in items]
    assets: dict[UUID, MediaAsset] = (
        {
            a.id: a
            for a in session.scalars(
                select(MediaAsset).where(MediaAsset.id.in_(asset_ids))
            )
        }
        if asset_ids
        else {}
    )
    views: list[rules.ItemView] = []
    for item in items:
        # 取不到仍按原来的方式兜底。列上是 NOT NULL + ondelete=RESTRICT,
        # 理论上不会缺;但"理论上不会"不是删掉分支的理由
        asset = assets.get(item.media_asset_id)
        views.append(
            rules.ItemView(
                media_asset_id=str(item.media_asset_id),
                role=item.role,
                sort_order=item.sort_order,
                variant_id=item.variant_id,
                is_primary=item.is_primary,
                enabled=item.enabled,
                asset_status=asset.status if asset else MediaStatus.FAILED.value,
                asset_role_source=asset.role_source if asset else "UNSET",
                asset_role_confidence=(
                    float(asset.role_confidence)
                    if asset and asset.role_confidence is not None
                    else None
                ),
                # §6.5 的两列。**从图片项上取,不从素材上取**:
                # "这张通用图可以混进颜色附图"是一个**编排决定**,
                # 同一张素材在两版图片集里完全可以有两个答案
                shared_opt_in=bool(item.shared_opt_in),
                angle=item.angle,
            )
        )
    return rules.SetView(
        image_set_id=str(row.id),
        spu=row.spu,
        version=row.version,
        status=row.status,
        items=tuple(views),
        channel=row.channel,
        site=row.site,
    )


def list_sets(session: Session, spu: str) -> list[ListingImageSet]:
    return list(
        session.scalars(
            select(ListingImageSet)
            .where(ListingImageSet.spu == spu)
            .order_by(ListingImageSet.version.desc())
        )
    )


def get_set(session: Session, image_set_id: UUID) -> ListingImageSet:
    row = session.get(ListingImageSet, image_set_id)
    if row is None:
        raise NotFoundError("图片集不存在")
    return row


def list_items(session: Session, image_set_id: UUID) -> list[ListingImageItem]:
    """一版图片集里的全部项,按编排顺序。

    排序在这里而不是在接口层:`sort_order` 就是运营拖出来的那个顺序,
    读的地方每多一处自己排序,就多一个「界面上的顺序和导出的顺序不一样」
    的可能。
    """
    return list(
        session.scalars(
            select(ListingImageItem)
            .where(ListingImageItem.image_set_id == image_set_id)
            .order_by(ListingImageItem.sort_order)
        )
    )


def resolve_for_publish(
    session: Session, *, spu: str, channel: str | None, site: str | None
) -> ListingImageSet | None:
    """发布时该用哪一版。**只返回已批准的。**"""
    rows = list_sets(session, spu)
    views = [_to_view(session, r) for r in rows]
    chosen = rules.resolve_set(views, channel=channel, site=site)
    if chosen is None:
        return None
    return next((r for r in rows if str(r.id) == chosen.image_set_id), None)


#: 同一批 items 里不允许重复的组合。**在进库之前查。**
#:
#: 不查的话,这两类重复要到 `flush()` 撞唯一索引时才失败,而那时候抛出来的是
#: 一个 `IntegrityError` —— 接口层把它变成 500,用户看到的是"服务器错误",
#: 而实际上是他自己提交的表单里有两行排在同一个位置。
def _reject_duplicates(items: list[dict]) -> None:
    orders: dict[int, int] = {}
    assets: dict[tuple[str, str], int] = {}
    for index, item in enumerate(items):
        order = int(item.get("sort_order", index))
        if order < 0:
            raise ValidationError(
                f"第 {index + 1} 项的排序号 {order} 是负数",
                code=ErrorCode.INPUT_INVALID,
            )
        if order in orders:
            raise ValidationError(
                f"第 {orders[order] + 1} 项和第 {index + 1} 项的排序号都是 {order}",
                code=ErrorCode.INPUT_INVALID,
                http_status=409,
            )
        orders[order] = index

        # variant_id 为 NULL 时数据库那条唯一约束是不生效的
        # (PostgreSQL 把 NULL 当成互不相等),所以这里用 '' 归一,
        # 和 0016 那条表达式索引口径一致
        key = (str(item["media_asset_id"]), str(item.get("variant_id") or ""))
        if key in assets:
            raise ValidationError(
                f"第 {assets[key] + 1} 项和第 {index + 1} 项是同一张素材的同一个变体",
                code=ErrorCode.INPUT_INVALID,
                http_status=409,
            )
        assets[key] = index


def _assert_spu_exists(session: Session, spu: str) -> None:
    """SPU 必须在 `products` 里真实存在(BLOCK-01)。

    不查的话,一个拼错的 SPU 会成功建出一版图片集,而它永远不会被任何
    商品解析到 —— 界面上它长得和正常图片集一模一样,运营会以为图已经编好了。
    孤儿图片集也让「这个 SPU 有几版图」这个问题多出一批不属于任何商品的答案。
    """
    exists = session.scalar(select(Product.id).where(Product.spu == spu).limit(1))
    if exists is None:
        raise ValidationError(
            f"SPU {spu} 在商品库里不存在,不能为它建图片集",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
        )


def _assert_items_belong_to_spu(session: Session, *, spu: str, items: list[dict]) -> None:
    """图片集里的每一张素材都必须属于这个 SPU(BLOCK-01)。

    ## 为什么这是一条硬约束,不是一条校验规则

    素材的授权、隔离状态、角色可信度**全部按商品算**(见 `MediaAsset` 的
    去重键说明)。引用另一个 SPU 的素材意味着:那张图被隔离时,降级逻辑
    (`downgrade_sets_using`)确实会找到这一版并降级 —— 但运营在本 SPU 的
    素材列表里根本看不到那张图,于是「这一版为什么突然变成待复核」没有
    任何可见依据。更直接的后果是把 A 商品的图发到 B 商品的详情页。

    ## 一次查询,不逐张 `session.get`

    详情页那边的 N+1 已经在报告里了(6.7),这条新增的校验不该再添一个。

    ## 为什么用 `MediaAsset.spu` 与商品的 spu 双口径

    `MediaAsset.spu` 是冗余列、可空(见模型注释),存量数据里有一批没回填。
    只认它的话,老素材会被误判成"不属于任何 SPU"而全部拒绝;只认商品的
    spu 的话,又漏掉了「素材挂在 A 商品但 spu 列写着 B」这种真正的脏数据。
    两者任一对上即认可,两者都对不上才拒绝 —— 拒绝的是确实指向别处的素材。
    """
    ids = [UUID(str(item["media_asset_id"])) for item in items]
    rows = session.execute(
        select(
            MediaAsset.id,
            MediaAsset.spu,
            MediaAsset.status,
            Product.spu,
        )
        .join(Product, Product.id == MediaAsset.product_id, isouter=True)
        .where(MediaAsset.id.in_(ids))
    ).all()
    found = {row[0]: row for row in rows}

    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise ValidationError(
            f"素材不存在:{', '.join(missing[:5])}",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
            detail={"missing_media_asset_ids": missing},
        )

    foreign: list[str] = []
    deleted: list[str] = []
    for asset_id, asset_spu, status, product_spu in rows:
        if spu not in (asset_spu, product_spu):
            foreign.append(str(asset_id))
        elif status == MediaStatus.DELETED.value:
            deleted.append(str(asset_id))

    if foreign:
        raise ValidationError(
            f"这些素材不属于 SPU {spu},不能放进它的图片集:"
            + ", ".join(foreign[:5]),
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
            detail={"foreign_media_asset_ids": foreign},
        )
    if deleted:
        # 已删除的素材和"暂时不可用"不是一回事:隔离可以放行,删除不能撤销。
        # 让它进集只会在批准时撞 UNUSABLE_ASSET,而那条消息说的是"状态不对",
        # 不会说"这张图已经没了,换一张"
        raise ValidationError(
            "这些素材已删除,不能放进图片集:" + ", ".join(deleted[:5]),
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
            detail={"deleted_media_asset_ids": deleted},
        )


def _resolve_variant_refs(session: Session, *, spu: str, items: list[dict]) -> list[dict]:
    """把入参里的变体标签翻译成稳定 key(A44)。翻不动的当场拒绝。

    ## 为什么写入侧一定要有这一层

    变体的**身份**是 `products.variant_key`,分配那一刻取的是当时的颜色名;
    界面上给人看的是**当前**颜色名。改过一次名的 SPU,这两个字符串不同。

    不翻译的话,人照着界面打出来的标签会落进 `unknown_tags`,而同一版里
    早先绑的旧标签还是好的 —— 表现是「越是最近绑的图越对不上」,
    没有人会往「三个月前改过颜色名」那边想。

    ## 为什么翻不动是 422 而不是记一条 warning

    这是**人**在界面上提交的一次绑定,提交者就在屏幕前。让他改一个字,
    比让一张图带着一个谁也不认识的标签进库、在批准时报「缺变体图」好。
    (识别那条机器路径的取舍相反,见 `attributes/service.py` 的说明。)

    `None` 原样放行:通用图是合法的,而且今天绝大多数图都是通用图。
    """
    if not any((item.get("variant_id") or "").strip() for item in items):
        return items

    directory = variants.variant_directory(session, spu)
    known = "、".join(ref.label for ref in directory) or "(该 SPU 没有变体)"

    out: list[dict] = []
    for item in items:
        raw = (item.get("variant_id") or "").strip()
        if not raw:
            out.append(item)
            continue
        key = variant_key.resolve_ref(raw, directory)
        if key is None:
            raise ValidationError(
                f"变体标签「{raw}」在 SPU {spu} 下找不到对应的变体。当前变体:{known}",
                code=ErrorCode.INPUT_INVALID,
                http_status=422,
                detail={"variant_id": raw, "known_variants": [r.label for r in directory]},
            )
        out.append({**item, "variant_id": key})
    return out



#: 版本号撞车会命中的那两条约束。表约束与表达式索引各一条 ——
#: 前者对 channel/site 为 NULL 的行不生效(NULL 互不相等),
#: 真正挡住通用集重复的是后者,所以两条都要认。
_VERSION_COLLISION_CONSTRAINTS = (
    "uq_listing_image_sets_version",
    "uq_listing_image_sets_version_coalesced",
)


def _is_version_collision(exc: IntegrityError) -> bool:
    """这次完整性冲突是不是"版本号被别人抢先占了"(A-37)。

    先读驱动给的结构化约束名(psycopg 会在 `diag` 上给出),读不到再退回
    字符串匹配 —— 后者是兜底,不是判据本体:约束名出现在异常文本里
    是 PostgreSQL 的习惯,不是契约。

    两条路都拿不到名字时返回 True(按版本冲突处理)。方向是刻意选的:
    多重试几次只是几毫秒,而把真正的版本冲突当成入参错误抛出去,
    会让一次正常的并发派生变成一条运营看不懂的 500。
    """
    name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    if name:
        return name in _VERSION_COLLISION_CONSTRAINTS
    text = str(getattr(exc, "orig", exc))
    if any(known in text for known in _VERSION_COLLISION_CONSTRAINTS):
        return True
    # 认得出是别的约束 -> 明确不是版本冲突
    return "uq_listing_image" not in text and "ix_listing_image" not in text


def create_set(
    session: Session,
    *,
    spu: str,
    items: list[dict],
    channel: str | None = None,
    site: str | None = None,
    actor: str = "system",
    derived_from: UUID | None = None,
) -> ListingImageSet:
    """建一版新图片集(DRAFT)。

    版本号按 `(spu, channel, site)` 分组算 —— 渠道专用集和默认集
    各自有独立的版本序列,否则改一次默认集会让所有渠道的版本号跳号。

    整段"算版本号 -> 插入"在 advisory lock 之内(评审第 6 条):
    并发建集会算出同一个版本号,后提交的撞
    `uq_listing_image_sets_version_coalesced`,用户拿到一个 500。
    """
    if len(items) > MAX_IMAGE_SET_ITEMS:
        raise ValidationError(
            f"一版图片集最多 {MAX_IMAGE_SET_ITEMS} 项,收到 {len(items)} 项",
            code=ErrorCode.INPUT_INVALID,
            http_status=422,
        )
    # 变体标签先翻译成稳定 key,**在去重之前**(A44)。
    #
    # 顺序有讲究:去重的键是 `(media_asset_id, variant_id or "")`。
    # 先去重再翻译的话,同一张图打了「红色」和它的 key(改名前叫「正红」)
    # 两个标签会被当成两项放行,翻译完变成同一项,而唯一约束在库里 ——
    # 报出来是 500,不是那句能读的 422。
    items = _resolve_variant_refs(session, spu=spu, items=items)
    _reject_duplicates(items)
    # 归属校验放在拿锁**之前**:它只读、可能查一批素材,没有理由把
    # 同 scope 的建集串行化窗口拉长
    _assert_spu_exists(session, spu)
    _assert_items_belong_to_spu(session, spu=spu, items=items)
    locks.advisory_xact_lock(session, "listing_image_set", spu, channel, site)

    row: ListingImageSet | None = None
    last_error: IntegrityError | None = None
    for _ in range(locks.MAX_LOCK_RETRIES):
        try:
            with session.begin_nested():
                same_scope = [
                    r
                    for r in list_sets(session, spu)
                    if r.channel == channel and r.site == site
                ]
                version = rules.next_version(
                    [
                        rules.SetView(str(r.id), r.spu, r.version, r.status)
                        for r in same_scope
                    ]
                )
                row = ListingImageSet(
                    spu=spu,
                    channel=channel,
                    site=site,
                    version=version,
                    status=ImageSetStatus.DRAFT.value,
                    derived_from=derived_from,
                )
                session.add(row)
                session.flush()

                for index, item in enumerate(items):
                    session.add(
                        ListingImageItem(
                            image_set_id=row.id,
                            media_asset_id=UUID(str(item["media_asset_id"])),
                            role=item["role"],
                            sort_order=int(item.get("sort_order", index)),
                            variant_id=item.get("variant_id"),
                            is_primary=bool(item.get("is_primary", False)),
                            enabled=bool(item.get("enabled", True)),
                            derivative_purpose=item.get("derivative_purpose"),
                            # §6.5 的两列。**没有这两个 kwarg 时它们恒为默认值**,
                            # 而 `_to_view` 与 `_section_6_5` 都在读它们 ——
                            # 后果不是「少了个功能」:`angle` 恒 NULL 会让配了方案的
                            # 颜色永远覆盖不了必要角度,而门禁报出来的是「缺正面图」。
                            # 运营会去补图,补多少张都没用。
                            shared_opt_in=bool(item.get("shared_opt_in", False)),
                            angle=_angle_value(item.get("angle")),
                        )
                    )
                session.flush()
            break
        except IntegrityError as exc:
            # A-37:**只有版本号撞车才重试。**
            #
            # 原来这里接住的是所有 IntegrityError,于是任何一种完整性冲突都被
            # 塞进"版本冲突"这个重试环里 —— 比如同一素材的同一变体重复
            # (`uq_listing_image_items_variant`)、或者同 scope 已经有一个
            # APPROVED(`uq_listing_image_sets_one_approved`)。这两种重试
            # **一万次也不会成功**,最后以「图片集版本连续 N 次冲突,请稍后重试」
            # 报出来,把排查方向指向并发,而真实原因是入参。
            #
            # 判据是约束名。拿不到名字时(不同驱动、或者被包成别的异常)
            # 按"可能是版本冲突"处理并重试 —— 保守方向:多试几次的代价是
            # 几毫秒,认错方向的代价是运营照着错误提示等一下午。
            if not _is_version_collision(exc):
                raise
            last_error = exc
            row = None
            logger.warning(
                "image set version collided, retrying",
                extra={"extra_fields": {"spu": spu, "channel": channel, "site": site}},
            )
    if row is None:
        raise ValidationError(
            f"图片集版本连续 {locks.MAX_LOCK_RETRIES} 次冲突,请稍后重试",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        ) from last_error

    audit.record(
        session, actor=actor, action=AuditAction.CREATE,
        entity_type="ListingImageSet", entity_id=row.id,
        payload={"spu": spu, "version": row.version, "items": len(items)},
    )
    return row


def derive(
    session: Session, image_set_id: UUID, *, items: list[dict], actor: str
) -> ListingImageSet:
    """从一版派生出新一版。**这是修改已批准图片集的唯一方式。**

    原地改的话,「平台驳回的是哪一版图」就没法回答 —— 而驳回回流
    第一件要做的事正是定位到具体那一版的具体那一项。
    """
    source = get_set(session, image_set_id)
    return create_set(
        session,
        spu=source.spu,
        items=items,
        channel=source.channel,
        site=source.site,
        actor=actor,
        derived_from=source.id,
    )


def _required_angles(session: Session, spu: str) -> dict[str, list[str]]:
    """每个颜色当前生效方案要求的角度(§6.5「必要角度按 angles_json 验收」)。

    ## 为什么解析不出 SPU 时返回空

    `listing_image_sets.spu` 至今是**字符串**(阶段 1 的归属外键只落到了
    `products` 与 `media_assets`)。翻译不出 `spus.id` 时返回空字典 ——
    也就是"这一版不查角度",而不是"每个颜色都缺全部角度"。

    后者会让阶段 4 上线当天所有图片集批不了,而根因是一个还没搬完的外键,
    与图片本身无关。这条口径与 `validate_set` 的 `variant_ids` 传空
    (不做颜色级检查)是同一条:**少一个维度的检查必须能被显式表达。**

    欠账记在 `docs/STATUS.md`:`listing_image_sets.spu` 切外键那天,
    这个函数的兜底分支应当被删掉。
    """
    spu_row = session.scalar(select(Spu).where(Spu.spu_code == spu))
    if spu_row is None:
        return {}
    return generation_plan_service.required_angles_for(session, spu_row.id)


def validate(session: Session, image_set_id: UUID) -> list[rules.Violation]:
    """校验一版图片集。**带上这个 SPU 真实需要覆盖的变体**(BLOCK-02)。

    `image_set_rules.validate_set` 的 `variant_ids` 一直是默认空集,
    于是 `MISSING_IMAGE_FOR_VARIANT` 这条规则从写下来那天起就没有触发过 ——
    规则、测试、错误码、前端展示全都在,只差没人把变体传进去。
    多色 SPU 少一个颜色的图,在批准这一步是查不出来的,要等平台驳回。

    变体口径来自 `listings.variants`,与草稿的 SKU 展开同一份定义。
    """
    row = get_set(session, image_set_id)
    return rules.validate_set(
        _to_view(session, row),
        variant_ids=variants.required_variant_ids(session, row.spu),
        required_angles=_required_angles(session, row.spu),
    )


def variant_coverage(session: Session, row: ListingImageSet) -> dict:
    """这一版图片集对该 SPU 变体的覆盖情况。**诊断,不阻断。**

    ## 为什么它不能是一条阻断规则

    `validate_set` 里的 `MISSING_IMAGE_FOR_VARIANT` 有一条放行:
    只要集里存在通用图(`variant_id IS NULL`),所有变体都算被覆盖。
    而实际系统里 **`listing_image_items.variant_id` 从来没有被写过非空值** ——
    唯一的写入方是接口入参,唯一的调用方(工作台图片集页)把它硬编码成 null,
    界面上也没有任何设置变体的入口。

    也就是说:把真实变体传进 `validate_set` 是必要的,但**不足以**让那条规则
    在今天生效。要真正生效,得先有人给图打上变体标签。

    改成硬阻断呢?那会让每一个多色 SPU 立刻无法批准 —— 因为它们的图全是通用图。
    那不是修复,是停产。

    ## 于是这里做的事

    把缺口**算出来并暴露出去**,让它在详情页和审计里可见,而不是继续
    以"校验通过"的形式隐身。三个字段各回答一个问题:

        required        这个 SPU 有哪几个变体(来自商品行)
        tagged          这一版图实际用了哪些变体标签
        unknown_tags    用了但不属于该 SPU 的标签 —— 词表对不上的信号

    `unknown_tags` 尤其要紧:图片项的变体标签是自由文本。A44 之前
    `required` 来自 `primary_color or sku`,两套词表各自演化的话,"覆盖"
    这件事算出来是假的,而没有这个字段就没人会发现。

    ## A44 之后这个字段的含义变窄了

    写入侧现在会把标签翻译成稳定 key(`_resolve_variant_refs`),翻不动直接 422。
    于是 `unknown_tags` 不再由「改了颜色名」产生 —— 它剩下的成因只有一种:
    **打完标签之后,那一行商品被删了或换了 SPU**。这是真异常,值得看。

    新增两个字段:

        labels      key → 当前颜色名。**界面要显示的是它,不是 key** ——
                    key 是分配那一刻的颜色名,改过名之后直接显示会让运营
                    看到自己刚改掉的旧名字,然后再改一次。
        drift       稳定 key 换来的两种新异常(改名、重名撞车)+ 没分到 key 的行。
                    见 `variant_key.drift()`。
    """
    required = variants.required_variant_ids(session, row.spu)
    directory = variants.variant_directory(session, row.spu)
    used = {i.variant_id for i in list_items(session, row.id) if i.enabled}
    tagged = sorted(v for v in used if v)
    return {
        "required": required,
        "labels": {ref.key: ref.label for ref in directory},
        "drift": variants.drift_report(session, row.spu),
        "tagged": tagged,
        "has_generic_images": None in used,
        # 只在真的做了变体绑定时才谈"缺哪个" —— 全是通用图时缺的不是某个变体,
        # 而是变体绑定这件事本身,那由 `variant_binding_missing` 表达
        "missing": [v for v in required if v not in tagged] if tagged else [],
        "unknown_tags": [v for v in tagged if v not in required],
        # 多变体 SPU 却一个变体标签都没有:平台需要按颜色取图,而这一版
        # 给不出"红色的主图是哪张"
        "variant_binding_missing": len(required) > 1 and not tagged,
    }


def approve(session: Session, image_set_id: UUID, *, actor: str) -> ListingImageSet:
    """批准一版。**校验不过就不批准。**

    把校验放在批准这一步而不是发布那一步:发布时才发现主图缺失,
    人已经在等平台回执了;批准时发现,他手边正好就是那批图。
    """
    row = get_set(session, image_set_id)

    # 同 scope 的批准必须串行(评审第 7 条)。
    #
    # 没有锁的话,两个并发审批各自读到"当前已批准的是 v1",各自把 v1 归档、
    # 把自己那一版置为 APPROVED,两个事务都提交成功 —— 库里留下两个
    # APPROVED,而 resolve 按版本号取最高的那一版。结果是"回滚到旧版"
    # 这个动作有一半概率不生效,且没有任何地方能看出为什么。
    #
    # 数据库那条部分唯一索引(0016)是第二道:锁只覆盖走这条代码路径的进程
    locks.advisory_xact_lock(
        session, "listing_image_set_approval", row.spu, row.channel, row.site
    )
    # 拿到锁之后重读一次:等锁期间别人可能已经把这一版归档或批准了
    session.refresh(row)

    # 变体覆盖必须在**批准**这一步查(BLOCK-02)。发布时才发现少一个颜色,
    # 那批图已经在等平台回执了;批准时发现,运营手边正好就是那批图
    problems = rules.validate_set(
        _to_view(session, row),
        variant_ids=variants.required_variant_ids(session, row.spu),
        required_angles=_required_angles(session, row.spu),
    )
    if problems:
        raise ValidationError(
            "图片集有问题,不能批准:"
            + ";".join(f"{p.code.value} {p.message}" for p in problems[:3]),
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
            detail={"violations": [p.code.value for p in problems]},
        )

    # 同 scope 下已批准的旧版归档。不归档的话 resolve 会按版本号取最高的,
    # 而"回滚到旧版"就变成了"再建一个更高版本" —— 那不是回滚
    for other in list_sets(session, row.spu):
        if (
            other.id != row.id
            and other.channel == row.channel
            and other.site == row.site
            and other.status == ImageSetStatus.APPROVED.value
        ):
            other.status = ImageSetStatus.ARCHIVED.value

    # A10:批准一版被退回过的集是允许的,但退回痕迹要一起清掉,并且在审计里
    # 留下"这一版曾被退回、原因是什么、还是批了"。
    #
    # 为什么不硬挡:退回的下一步由原因决定(改属性 / 补素材 / 重排图),
    # 而那几条路走完往往会 `derive` 出新版本,根本走不到这里。真的走到这里
    # 只有一种情况——**运营认为上次退回判断错了**。系统看不见图,没有资格
    # 替他否决;但也不能让这次翻案无声无息,所以它进审计。
    was_rejected = row.status == ImageSetStatus.REJECTED.value
    prior_reason = row.reject_reason
    # 回滚会走到这里重新批准同一行,`approved_by` / `approved_at` 会被这次
    # 操作覆盖 —— 于是"这一版最早是谁批的、什么时候批的"在库里消失了,
    # 而那正是驳回回流要回答的第一个问题。列上只能存一个值(它表示的是
    # "当前这次生效的批准"),所以把被覆盖的那一份写进审计:审计是追加的,
    # 覆盖多少次都还在
    prior_approved_by = row.approved_by
    prior_approved_at = row.approved_at
    row.status = ImageSetStatus.APPROVED.value
    row.approved_by = actor
    row.approved_at = _now()
    row.reject_reason = None
    row.reject_note = None
    row.rejected_by = None
    row.rejected_at = None
    session.flush()

    payload: dict = {"action": "approve", "version": row.version}
    # 变体覆盖缺口进审计(BLOCK-02 的现状)。规则今天拦不住它 —— 图片项的
    # 变体标签从来没被写过非空值,所以 MISSING_IMAGE_FOR_VARIANT 恒不触发。
    # 至少让"这一版是在没有变体绑定的情况下被批准的"留下记录:
    # 等前端补上变体编排入口,这批审计就是需要回头补图的清单
    coverage = variant_coverage(session, row)
    if coverage["variant_binding_missing"] or coverage["missing"] or coverage["unknown_tags"]:
        payload["variant_coverage"] = coverage
        logger.warning(
            "image set approved with a variant coverage gap",
            extra={
                "extra_fields": {
                    "image_set_id": str(row.id),
                    "spu": row.spu,
                    "required_variants": coverage["required"],
                    "tagged_variants": coverage["tagged"],
                    "unknown_tags": coverage["unknown_tags"],
                }
            },
        )
    if was_rejected:
        payload["overrode_rejection"] = prior_reason
    if prior_approved_by is not None:
        payload["previous_approved_by"] = prior_approved_by
        payload["previous_approved_at"] = (
            prior_approved_at.isoformat() if prior_approved_at else None
        )
    audit.record(
        session, actor=actor, action=AuditAction.UPDATE,
        entity_type="ListingImageSet", entity_id=row.id,
        payload=payload,
    )
    return row


def reject(
    session: Session,
    image_set_id: UUID,
    *,
    reason: str,
    note: str | None,
    actor: str,
) -> ListingImageSet:
    """快审退回一版图片集(A10)。

    ## 退回和"不批准"不是同一件事

    不批准是什么都不做:那一版停在 PENDING_REVIEW,下一步仍然是"等待批准",
    于是它立刻回到快审队列排在原处。退回要**改变判定结果** ——
    状态置 REJECTED,判定层据此把下一步换成由原因推出的那一个
    (`flow.REJECT_REASON_ACTIONS`),这件商品因此离开待批准队列。

    ## 已批准的版本不能退回

    那一版可能已经被草稿引用、已经导出、甚至已经上传平台。把它退回去,
    草稿指向的图片集会变成"被退回的",而平台上挂着的还是那批图 ——
    库里的结论和平台上的事实对不上。已发出去的问题走平台驳回台账
    (`workbench/platform_service.py`),那条路径会记录驳回的是哪一版、
    对应哪次导出;这条路径不承担那件事。
    """
    from app.workbench import reject as reject_rules

    row = get_set(session, image_set_id)

    if row.status == ImageSetStatus.APPROVED.value:
        raise ValidationError(
            "这一版已经批准,不能退回;要改内容请派生新版本,"
            "已上传平台的问题走平台驳回",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    if row.status == ImageSetStatus.ARCHIVED.value:
        raise ValidationError(
            "已归档的版本不能退回",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )

    try:
        checked = reject_rules.validate_reject("image_set", reason, note)
    except ValueError as exc:
        raise ValidationError(
            str(exc), code=ErrorCode.INPUT_INVALID, http_status=422
        ) from exc

    row.status = ImageSetStatus.REJECTED.value
    row.reject_reason = checked.reason.value
    row.reject_note = checked.note
    row.rejected_by = actor
    row.rejected_at = _now()
    # A-38:两个方向必须对称。`approve()` 会清退回列(589-592 行),而这里
    # 原来不清批准列 —— 于是同一行同时留着"批准于 x 由 y"和"已退回",
    # 界面上一版图既是批准过的又是被退回的,而**哪一个是当前结论**要靠
    # 读 status 才能知道。回滚会重新批准同一行,所以这两组列必然会同时
    # 有值,不是理论情况。
    #
    # 历史不会因此丢:这次退回连同它覆盖掉的批准记录都进了下面的审计。
    prior_approved_by = row.approved_by
    prior_approved_at = row.approved_at
    row.approved_by = None
    row.approved_at = None
    session.flush()

    audit.record(
        session, actor=actor, action=AuditAction.UPDATE,
        entity_type="ListingImageSet", entity_id=row.id,
        payload={
            "action": "reject",
            "version": row.version,
            "reason": checked.reason.value,
            "note": checked.note,
            # 被这次退回清掉的批准记录。清列是为了让"当前结论"只有一个,
            # 而不是为了把它忘掉 —— 追溯要靠这里
            **(
                {
                    "cleared_approved_by": prior_approved_by,
                    "cleared_approved_at": (
                        prior_approved_at.isoformat() if prior_approved_at else None
                    ),
                }
                if prior_approved_by is not None or prior_approved_at is not None
                else {}
            ),
        },
    )
    logger.info(
        "image set rejected in quick review",
        extra={
            "extra_fields": {
                "image_set_id": str(row.id),
                "spu": row.spu,
                "reason": checked.reason.value,
            }
        },
    )
    return row


def rollback_to(session: Session, image_set_id: UUID, *, actor: str) -> ListingImageSet:
    """回滚:把旧版重新置为 APPROVED。

    回滚不是"建一个新版本把内容改回去" —— 那样版本历史里会出现两条
    内容相同的记录,而"我们回滚过"这件事没有任何地方记着。
    """
    row = get_set(session, image_set_id)
    if row.status not in (ImageSetStatus.ARCHIVED.value, ImageSetStatus.APPROVED.value):
        raise ValidationError(
            f"只有已归档或已批准的版本可以回滚,当前状态 {row.status}",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    return approve(session, image_set_id, actor=actor)


def downgrade_sets_on_audience_change(
    session: Session,
    *,
    spu: str,
    actor: str,
    from_audience: str | None,
    to_audience: str | None,
) -> list[UUID]:
    """商品受众变更时,把该 SPU 的已批准图片集降级为待复核(A45 独立审查 C-04)。

    由 `product_service.update_product` 在**同一事务**内调用。界面上那个危险
    弹窗一直承诺"这会作废已生成的图片(它们用的是原受众的模特)",而在此
    之前服务层只改 `Product.audience` —— 承诺没有任何实现,原受众的图片集
    继续以 APPROVED 身份走进新草稿。

    降级到 PENDING_REVIEW 而不是直接作废:MediaAsset 目前没有生成溯源列
    (docs/DECISIONS.md §3.21),分不出哪张图用了哪个受众的模特;强制回到
    人工复核是当前数据形状下能兑现的最强承诺 —— `resolve_for_publish` 只认
    APPROVED,降级即刻切断"旧受众图片进新草稿"这条路。与
    `downgrade_sets_using`(素材隔离)同一形状:改状态 + 记审计,系统自动
    路径也必须回答得了"是谁退回的"(A-39)。
    """
    affected = session.scalars(
        select(ListingImageSet).where(
            ListingImageSet.spu == spu,
            ListingImageSet.status == ImageSetStatus.APPROVED.value,
        )
    )
    downgraded: list[UUID] = []
    for row in affected:
        row.status = ImageSetStatus.PENDING_REVIEW.value
        downgraded.append(row.id)
        audit.record(
            session, actor=actor, action=AuditAction.UPDATE,
            entity_type="ListingImageSet", entity_id=row.id,
            payload={
                "action": "downgrade_on_audience_change",
                "version": row.version,
                "from_status": ImageSetStatus.APPROVED.value,
                "to_status": ImageSetStatus.PENDING_REVIEW.value,
                "spu": spu,
                "audience_from": from_audience,
                "audience_to": to_audience,
            },
        )
        logger.warning(
            "image set downgraded because the product audience changed",
            extra={
                "extra_fields": {
                    "image_set_id": str(row.id),
                    "spu": spu,
                    "audience_from": from_audience,
                    "audience_to": to_audience,
                }
            },
        )
    if downgraded:
        session.flush()
    return downgraded


def downgrade_sets_using(
    session: Session, media_asset_id: UUID, *, actor: str = "system"
) -> list[UUID]:
    """某条素材被隔离时,把引用它的已批准集降级为待复核(§7.3)。

    由 `media/service.quarantine()` 调用。不降级的话,一张已经被判定为
    不合规的图会继续被发布,而隔离动作看起来是成功的。

    ## A-39:降级要进审计,不能只写日志

    原来这里只有一条 `logger.warning`。日志运营看不到,于是这件事在界面上
    的表现是:一版**已经批准过**的图片集突然回到待复核,没有任何解释 ——
    运营第一反应是"谁把它退回了",而审计里查不到任何人做过这件事。
    `reject()` / `approve()` 都记审计,唯独这条系统自动路径不记。

    `actor` 默认 `"system"`:调用方能给出真实操作者时应当传进来
    (隔离是人点的),给不出时这条仍然要落 —— 记成 system 也远好过不记。
    """
    affected = session.scalars(
        select(ListingImageSet)
        .join(ListingImageItem, ListingImageItem.image_set_id == ListingImageSet.id)
        .where(
            ListingImageItem.media_asset_id == media_asset_id,
            ListingImageItem.enabled.is_(True),
            ListingImageSet.status == ImageSetStatus.APPROVED.value,
        )
        .distinct()
    )
    downgraded: list[UUID] = []
    for row in affected:
        if not rules.needs_downgrade(_to_view(session, row)):
            continue
        row.status = ImageSetStatus.PENDING_REVIEW.value
        downgraded.append(row.id)
        audit.record(
            session, actor=actor, action=AuditAction.UPDATE,
            entity_type="ListingImageSet", entity_id=row.id,
            payload={
                "action": "downgrade_on_quarantine",
                "version": row.version,
                "from_status": ImageSetStatus.APPROVED.value,
                "to_status": ImageSetStatus.PENDING_REVIEW.value,
                "media_asset_id": str(media_asset_id),
            },
        )
        logger.warning(
            "image set downgraded because an asset was quarantined",
            extra={
                "extra_fields": {
                    "image_set_id": str(row.id),
                    "spu": row.spu,
                    "media_asset_id": str(media_asset_id),
                }
            },
        )
    if downgraded:
        session.flush()
    return downgraded
