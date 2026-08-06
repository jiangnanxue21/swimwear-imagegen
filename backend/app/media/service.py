"""统一素材服务(M1,§4)。

四条来源、一个入口:

    人工上传    asset_service.upload_asset()      -> shadow_from_product_asset()
    外链导入    (M1 之后)                          -> ingest()
    供应商推送  (M1 之后)                          -> ingest()
    AI 生成     generation_tasks._persist_candidates -> shadow_from_candidate()
    成品图      output_service.build_outputs()     -> shadow_from_output_asset()

## 迁移期的三步走在这里的落点

本模块同时服务迁移 A 与迁移 B(§4.4):

    shadow_*()      迁移 A。业务写入时**同事务**追加一条素材,读路径不变
    reconcile_*()   迁移 B。双读比对,不一致落 media_migration_mismatches

同事务是关键。分两个事务写的话,业务提交成功而影子写失败时两边永久不一致,
而且没有任何东西会发现 —— 对账要到迁移 B 才上线。所以影子写**不自己 commit**,
由调用方那个业务事务一起提交;它失败就让整笔业务失败。

> 这一条看起来激进(为了一条影子记录让上传失败?),但迁移期的正确性
> 比可用性重要:影子写静默失败的后果是一个看起来成功的迁移,
> 而它要到切换读路径那天才暴露,那时候已经没有回滚点了。
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.clock import utc_now
from app.core.enums import MediaRole, MediaSource, MediaStatus, RoleSource
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.sorting import normalize_sort
from app.media import evidence_rules, provenance_conflict
from app.media.mapping import (
    LEGACY_CANDIDATE,
    LEGACY_PRODUCT_ASSET,
    PARITY_FIELDS,
    normalize_for_parity,
    role_for_legacy_asset_type,
)
from app.models.media_asset import (
    MediaAsset,
    MediaDerivative,
    MediaMigrationMismatch,
)
from app.models.product import Product
from app.services.sorting import apply_order

logger = get_logger(__name__)


# 映射表与比较规则在 `app/media/mapping.py` —— 那个模块**零依赖**,
# 因此这次迁移里最容易错的部分(旧的什么对应新的什么、两个值算不算一样)
# 能在不带数据库的测试里穷举。这里只负责用它们读写库。



# ------------------------------------------------------------------ 入库


def ingest(
    session: Session,
    *,
    product: Product,
    storage_path: str,
    sha256: str,
    mime_type: str,
    width: int,
    height: int,
    byte_size: int,
    source: MediaSource,
    role: MediaRole | None = None,
    role_source: RoleSource = RoleSource.UNSET,
    role_confidence: float | None = None,
    variant_hint: str | None = None,
    status: MediaStatus = MediaStatus.READY,
    quality: dict[str, Any] | None = None,
    processor: str | None = None,
    processing_region: str | None = None,
    consent_id: UUID | None = None,
    retention_policy: str | None = None,
    legacy_kind: str | None = None,
    legacy_id: UUID | None = None,
    #: §4.8 溯源列(A45-batch14-16 / 迁移 0038)。**候选落盘时必须写** ——
    #: 三个判定(证据分层、AC-22 拦截、补角色闸)都读它们,而在它们之前
    #: 只有 `legacy_kind` 那条过渡记号真的会命中
    generation_task_id: UUID | None = None,
    generation_candidate_id: UUID | None = None,
) -> tuple[MediaAsset, bool]:
    """把一张图收敛成素材。返回 (素材, 是否命中去重)。

    **去重键是 (product_id, sha256)。** 供应商重复推同一张图不会产生第二条
    素材,也不会重复触发识别 —— 后者才是真正的成本:识别是按图计费的。

    命中去重时**不覆盖**已有记录的任何字段。角色可能是人工改过的,
    状态可能是人工隔离的,让一次重复推送把它们冲掉是最难查的一类数据丢失。
    唯一的例外见下面 `_fill_missing_role`。

    ## 溯源冲突按 SPU 找,不按去重键找(§11 / AC-22)

    去重键是 `(product_id, sha256)`,而 §11 写的是**同 SPU**。差出来的
    那一块正是最常见的走法:一个卖三色的款,运营把 A 色生成好的图下载下来,
    当样品传给 B 色的 SKU —— `product_id` 不同,去重不命中,于是新建一条
    `source=MANUAL_UPLOAD` 的行,它派生成 `PRODUCT_EVIDENCE`,
    **直接进识别输入,每张一次真实付费调用**,投的票还是一张 AI 图看出来的。

    判定在 `media/provenance_conflict`(零依赖、可穷举),这里只负责取数
    与落库,与 `evidence_rules` / `sample_completeness` 同一套分工。
    """
    same_digest = _same_digest_in_spu(session, product, sha256)
    existing = next((a for a in same_digest if a.product_id == product.id), None)
    conflict = provenance_conflict.verdict(
        source=source, role=role, same_digest_in_spu=same_digest
    )

    if existing is not None:
        _fill_missing_role(existing, role=role, role_source=role_source,
                           role_confidence=role_confidence)
        if conflict is provenance_conflict.Verdict.SOURCE_CONFLICT:
            # **去重命中这一路不改状态。** 命中的那条很可能就是生成链路
            # 自己的候选行(`candidate.media_asset_id` 指着它),隔离它等于
            # 把一张合法候选图从图片集里拿掉 —— 修一个洞,砸一条正常动线。
            #
            # 这一路的防线是上面那次 `_fill_missing_role`:它已经拒绝给
            # 带溯源的行补角色,于是 §6.2 门禁不会认下这张图。
            # 留一条日志,因为这件事发生了就该有人知道。
            logger.warning(
                "media.ingest.provenance_conflict.deduped",
                extra={
                    "product_id": str(product.id),
                    "media_asset_id": str(existing.id),
                    "sha256": sha256,
                    "incoming_source": source.value,
                },
            )
        return existing, True

    if conflict is provenance_conflict.Verdict.SOURCE_CONFLICT:
        # §11:「拒绝并提示来源冲突,**落隔离待人工放行**」——两件事都要。
        # 不抛异常:影子写和业务写在同一个事务里(见模块顶部),抛出去会把
        # 这次上传整笔回滚,那条"待人工放行"的记录也一起没了,
        # 运营手上只剩一句报错。放行通道 `release()` 认的正是隔离态。
        logger.warning(
            "media.ingest.provenance_conflict.quarantined",
            extra={
                "product_id": str(product.id),
                "spu": product.spu,
                "sha256": sha256,
                "incoming_source": source.value,
            },
        )
        status = MediaStatus.QUARANTINED

    asset = MediaAsset(
        product_id=product.id,
        spu=product.spu,
        source=source.value,
        role=role.value if role else None,
        role_source=role_source.value,
        role_confidence=role_confidence,
        variant_hint=variant_hint,
        storage_path=storage_path,
        mime_type=mime_type,
        width=width,
        height=height,
        bytes=byte_size,
        sha256=sha256,
        status=status.value,
        quality_json=quality,
        processor=processor,
        processing_region=processing_region,
        consent_id=consent_id,
        retention_policy=retention_policy,
        legacy_kind=legacy_kind,
        legacy_id=legacy_id,
        generation_task_id=generation_task_id,
        generation_candidate_id=generation_candidate_id,
    )
    if conflict is provenance_conflict.Verdict.SOURCE_CONFLICT:
        # 理由文案取自判定模块的常量,不在这里手写一句 —— 手写的那份
        # 和素材页上解释这条规则的地方会各自漂移,而运营读到的是这一句。
        asset.quarantine_reason = provenance_conflict.QUARANTINE_REASON[:2000]
    session.add(asset)
    session.flush()
    return asset, False


def _same_digest_in_spu(
    session: Session, product: Product, sha256: str
) -> list[MediaAsset]:
    """同 SPU、同 sha256 的已有素材行。**溯源冲突的取数入口。**

    包含本商品自己的那一条(去重命中的那条),调用方从中挑出来 ——
    分成两次查询的话,「去重看到的」和「冲突判定看到的」会在并发下
    是两个不同的快照,而这两个结论必须来自同一次观察。

    `spu` 为空时退回按商品找。**不能写成 `MediaAsset.spu == None`** ——
    SQLAlchemy 会渲染成 `spu IS NULL`,于是全库所有没有 SPU 的素材
    (存量数据、导入中途的行)会被当成同一个 SPU 的兄弟,
    一次上传能被一件毫不相干的商品的历史 AI 图拦下来。
    """
    where = (
        MediaAsset.spu == product.spu
        if product.spu
        else MediaAsset.product_id == product.id
    )
    return list(session.scalars(select(MediaAsset).where(where, MediaAsset.sha256 == sha256)))


def _fill_missing_role(
    asset: MediaAsset,
    *,
    role: MediaRole | None,
    role_source: RoleSource,
    role_confidence: float | None,
) -> None:
    """只在角色还没定过时补一次。

    唯一允许在去重命中时写入的字段。理由:一条 `role_source=UNSET` 的素材
    对下游是不可用的(图片集要按角色取图),而补上它不会覆盖任何人的决定。

    人工定过的、规则定过的、甚至模型定过的,一律不动。

    ## 带溯源的行不许补(§11 / AC-22)

    上面那句「补上它不会覆盖任何人的决定」对人工上传的行成立,
    对 AI 影子行**不成立**:AI 行的角色留空是一个决定,
    `shadow_from_candidate()` 的注释写着「在这里猜一个等于用
    `role_source=RULE` 掩盖一次没有依据的推断」。

    少了这一条会怎样:运营把生成好的图下载下来、当样品传回同一件商品,
    去重命中的正是那条 `role=None` + `role_source=UNSET` 的 AI 行,
    于是一条 `source=AI_GENERATED` 的记录拿到了 `role_source=HUMAN` ——
    §6.2 的完整度门禁问的正是「角色是不是人定的」,而它被答错了。
    今天 §5.1 那道白名单还拦得住(`evidence_class` 由 `source` 派生),
    但**一道门禁靠另一道门禁碰巧还在,不叫防住了**。
    """
    if role is None:
        return
    if not provenance_conflict.may_fill_role(asset):
        return
    if asset.role is not None or asset.role_source != RoleSource.UNSET.value:
        return
    asset.role = role.value
    asset.role_source = role_source.value
    asset.role_confidence = role_confidence


# ------------------------------------------------------------------ 影子写


def shadow_from_product_asset(session: Session, product: Product, asset) -> MediaAsset:
    """人工上传 / 外链导入(迁移 A)。

    `check_passed=False` 的素材落成 `QUARANTINED` 而不是 `READY`:
    旧模型里那是一个「记了一笔但照用不误」的标记,新模型里它必须真的
    挡住下游 —— 上架用图不能是一张没通过检查的图。
    """
    status = MediaStatus.READY if asset.check_passed else MediaStatus.QUARANTINED
    role = role_for_legacy_asset_type(asset.asset_type)
    media, deduped = ingest(
        session,
        product=product,
        storage_path=asset.storage_path,
        sha256=asset.file_hash,
        mime_type=asset.mime_type,
        width=asset.width,
        height=asset.height,
        byte_size=asset.file_size,
        source=MediaSource.MANUAL_UPLOAD,
        role=role,
        # 旧枚举是人工在上传时选的,所以角色来源是 HUMAN。
        # 但 GARMENT_CUTOUT -> FLAT_LAY、MODEL_REFERENCE -> MODEL_FRONT
        # 这两条映射是有损的,素材库页面应当提示复核
        role_source=RoleSource.HUMAN,
        status=status,
        legacy_kind=LEGACY_PRODUCT_ASSET,
        legacy_id=asset.id,
    )
    if not deduped and status is MediaStatus.QUARANTINED and not media.quarantine_reason:
        media.quarantine_reason = (asset.check_message or "上传检查未通过")[:2000]
    # `not media.quarantine_reason` 这个条件是 §11 加的:`ingest()` 可能已经
    # 因为溯源冲突写过一句更要紧的理由(这张图和一张 AI 产出字节相同),
    # 而"上传检查未通过"会把它盖掉 —— 运营看到的是一条查不出来路的隔离。
    return media


def shadow_from_candidate(session: Session, product: Product, candidate) -> MediaAsset | None:
    """AI 生成候选图(迁移 A)。

    下载失败的候选没有字节也没有路径,不产生素材 —— 它记录的是
    「这一轮向 Provider 要过图但没拿到」,那不是一张图。
    """
    if not candidate.storage_path or not candidate.file_hash:
        return None
    media, _ = ingest(
        session,
        product=product,
        storage_path=candidate.storage_path,
        sha256=candidate.file_hash,
        mime_type=candidate.mime_type or "image/jpeg",
        width=candidate.width or 0,
        height=candidate.height or 0,
        byte_size=candidate.file_size or 0,
        source=MediaSource.AI_GENERATED,
        # 角色**留空**。生成图是正面还是背面取决于任务的 mode 与提示词,
        # 在这里猜一个等于用 role_source=RULE 掩盖一次没有依据的推断。
        # 留 UNSET,由 M3 的角色识别或人工来定
        role=None,
        role_source=RoleSource.UNSET,
        status=MediaStatus.READY,
        legacy_kind=LEGACY_CANDIDATE,
        legacy_id=candidate.id,
        # §4.8「候选落盘写入」。`legacy_kind` 那条过渡记号**保留** ——
        # 存量行只有它,删掉等于让历史上所有 AI 影子行一次性失去溯源。
        # 两条并存不是重复:一条是迁移期的记号,一条是真外键,
        # 而判定认的是「任一命中即算带溯源」(`provenance_conflict`)
        generation_task_id=candidate.task_id,
        generation_candidate_id=candidate.id,
    )
    candidate.media_asset_id = media.id
    return media


def shadow_from_output_asset(
    session: Session, output, *, source_media_asset_id: UUID | None = None
) -> MediaDerivative | None:
    """成品图(迁移 A)。

    **成品图落成派生版本,不是新素材**(§4.3)。它是某张候选图的
    多尺寸转换结果,把它当独立素材会让同一张图在素材库里出现五遍,
    去重、授权和保留策略全部失效。

    找不到源素材时返回 None 并告警:那说明候选图的影子写漏了,
    是一个需要被发现的问题,不该静默造一条孤立素材把它盖住。
    """
    if source_media_asset_id is None:
        source_media_asset_id = _resolve_candidate_media(session, output.candidate_id)
    if source_media_asset_id is None:
        logger.warning(
            "output asset has no source media; skipping shadow write",
            extra={
                "extra_fields": {
                    "output_id": str(output.id),
                    "candidate_id": str(output.candidate_id or ""),
                }
            },
        )
        return None

    existing = session.scalar(
        select(MediaDerivative).where(
            MediaDerivative.source_media_asset_id == source_media_asset_id,
            MediaDerivative.purpose == output.purpose,
            MediaDerivative.transform_version == _transform_version(output),
        )
    )
    if existing is not None:
        return existing

    row = MediaDerivative(
        source_media_asset_id=source_media_asset_id,
        purpose=output.purpose,
        width=output.width or 0,
        height=output.height or 0,
        bytes=output.file_size or 0,
        storage_path=output.storage_path,
        transform_version=_transform_version(output),
        sha256=output.file_hash,
    )
    session.add(row)
    session.flush()
    return row


def _transform_version(output) -> str:
    """派生版本的算法版本号。

    旧的 `OutputAsset` 没有这个概念,用 `generation` 代数顶上 ——
    它至少能保证「重算一次出的新图」和旧图不撞唯一键。
    真正的变换版本要等 M5 的图片集把裁切规则外置之后才有意义。
    """
    return f"legacy-gen{output.generation or 1}"


def _resolve_candidate_media(session: Session, candidate_id) -> UUID | None:
    if candidate_id is None:
        return None
    from app.models.generation import GenerationCandidate

    candidate = session.get(GenerationCandidate, candidate_id)
    return candidate.media_asset_id if candidate is not None else None


# ------------------------------------------------------------------ 查询


#: 素材库允许排序的字段。
#:
#: `bytes` / `width` / `height` 在这里不是凑数的:素材库最常见的两个体力活是「找出
#: 分辨率不够的那几张」和「找出体积异常大的那几张」,以前只能靠翻页用眼睛比。
#:
#: `role` 可空(AI 生成图刻意留 UNSET,理由见 media.ts),空值排最后。
SORTABLE = {
    "created_at": MediaAsset.created_at,
    "updated_at": MediaAsset.updated_at,
    "bytes": MediaAsset.bytes,
    "width": MediaAsset.width,
    "height": MediaAsset.height,
    "status": MediaAsset.status,
    "source": MediaAsset.source,
    "role": MediaAsset.role,
    "spu": MediaAsset.spu,
}


def list_assets(
    session: Session,
    *,
    product_id: UUID | None = None,
    spu: str | None = None,
    source: str | None = None,
    role: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[MediaAsset], int]:
    """素材库列表。四个筛选维度对应素材库页面的四个下拉。"""
    sort_field, direction = normalize_sort(
        sort, order, allowed=SORTABLE, default_sort="created_at", default_order="desc"
    )
    stmt = select(MediaAsset)
    if product_id:
        stmt = stmt.where(MediaAsset.product_id == product_id)
    if spu:
        stmt = stmt.where(MediaAsset.spu == spu)
    if source:
        stmt = stmt.where(MediaAsset.source == source)
    if role:
        stmt = stmt.where(MediaAsset.role == role)
    if status:
        stmt = stmt.where(MediaAsset.status == status)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    ordered = apply_order(
        stmt, columns=SORTABLE, sort=sort_field, order=direction, tiebreak=MediaAsset.id
    )
    rows = list(session.scalars(ordered.offset(offset).limit(limit)))
    return rows, total


def get_asset(session: Session, media_id: UUID) -> MediaAsset:
    asset = session.get(MediaAsset, media_id)
    if asset is None:
        raise NotFoundError("素材不存在")
    return asset


def usable_assets(session: Session, product_id: UUID) -> list[MediaAsset]:
    """可用于识别与上架的素材。

    这是「上架链路的入口」那条规矩的实现(§4.5):
    入口是「商品存在至少一条 READY 且未隔离的素材」,
    **不查询、不依赖、不展示 GenerationTask 的状态**。
    """
    return list(
        session.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.product_id == product_id,
                MediaAsset.status == MediaStatus.READY.value,
            )
            .order_by(MediaAsset.created_at)
        )
    )


# ------------------------------------------------------------------ §5.1 白名单取数


#: `evidence_assets_for(scope=...)` 的「共享作用域」。和
#: `attributes/scope_fingerprint.SHARED` 是同一个约定(None = 不属于任何颜色),
#: 两处各写一个字面量 None 的话,哪天有人把其中一处改成 `""` 不会有任何征兆。
SHARED_SCOPE: None = None

#: SQL 那道粗筛允许出现的条件。**这张表是本模块最要紧的一段。**
#:
#: 每一条都必须满足:`asset_is_extraction_input()` 判 True 的行,一定满足它。
#: 也就是粗筛的结果必然是最终结果的**超集** —— 粗筛只负责少读几行,
#: 不负责判定。逐条的依据:
#:
#:     status = READY                 判定里 `status_is_ready` 是与项
#:     generation_task_id IS NULL     溯源列非空必然派生成 GENERATED_RESULT
#:     generation_candidate_id NULL   同上
#:     source NOT IN (AI_GENERATED,   前者派生 GENERATED_RESULT,
#:                    PLATFORM_SYNC)  后者派生 CHANNEL_DERIVATIVE
#:
#: 后三条在 `tests/pure/test_a45_batch14_7_evidence_class.py` 里被穷举证过
#: (`violates_ai_check` 那一组)。**往这张表里加条件之前先去那边补证明** ——
#: 加一条判定不蕴含的条件,表现是识别输入静静少了几张图,而
#: 「本该识别却没识别」这件事没有任何地方会报。
_COARSE_FILTER_COLUMNS = frozenset(
    {"status", "generation_task_id", "generation_candidate_id", "source"}
)


def evidence_assets_for(
    session: Session,
    product_id: UUID,
    *,
    scope: UUID | str | None = SHARED_SCOPE,
    scope_is_explicit: bool = False,
    imported_url_trusted: bool = False,
) -> list[MediaAsset]:
    """§5.1 的证据白名单取数入口。**识别输入只许从这里拿。**

    PRD 阶段 2 的交付原文是「白名单查询助手成为唯一取数入口」。在此之前
    `run_extraction` 走的是 `usable_assets()` 再在 Python 里过一道 ——
    判定是对的(A45-batch14-7 接的),但**取数不是收口的**:

        任何一个新写的调用点只要照着 `usable_assets()` 抄一行,
        就能把 AI 图喂进付费抽取器,而没有任何地方会红

    §5.1 要消灭的正是这个形状。所以这个函数存在的意义不在于它比旧写法快,
    在于**它让"忘记过滤"这件事不再抄得出来**。

    ## 判定仍然只有一处,这里没有第二个

    SQL 只做粗筛(`_COARSE_FILTER_COLUMNS`,每一条都被判定蕴含),
    最终一律由 `asset_is_extraction_input()` 说了算。

    这个分工是刻意的。把四条白名单条件全写成 WHERE 子句,等于给
    `evidence_class` 造第二个判定点 —— 而 `media/evidence_rules.py` 顶部
    整整一段在说这件事:两个判定点漂移时没有人会发现,而漂移的后果是
    一张 AI 图被当成商品证据穿透整个白名单。

    代价是多读几行再丢掉。可接受:粗筛已经把最大的两类(AI 图、渠道回抓图)
    挡在库里了,剩下的差额是「模特参考图」那种少量。

    ## `scope`:按颜色取证据

    ``scope=SHARED_SCOPE``(默认)= 不限颜色,取这个商品的全部证据 ——
    也就是今天 `run_extraction` 的行为。传具体颜色时取**那个颜色的 + 共享的**:
    共享素材(``color_variant_id IS NULL``)对每一个颜色都算数,
    这与 `scope_fingerprint.fingerprints()` 的分组口径一致。

    `scope_is_explicit` 用来区分「没传」和「传了 None」—— 两者语义相反:
    前者是"全都要",后者是"只要共享的那些"。**默认值同为 None 的参数
    必须有这样一个伴随标记**,否则调用方无法表达后者,而它恰恰是
    §6.5 混排规则将来要用的那一个。
    """
    stmt = select(MediaAsset).where(
        MediaAsset.product_id == product_id,
        MediaAsset.status == MediaStatus.READY.value,
        # 溯源列非空 = 这张图来自我们自己的生成流水线。`shadow_from_candidate`
        # 每跑完一个生成任务就写一条,而 AI 图对属性证据的贡献必然是零
        MediaAsset.generation_task_id.is_(None),
        MediaAsset.generation_candidate_id.is_(None),
        MediaAsset.source.not_in(
            (MediaSource.AI_GENERATED.value, MediaSource.PLATFORM_SYNC.value)
        ),
    )
    if scope_is_explicit:
        if scope is SHARED_SCOPE:
            stmt = stmt.where(MediaAsset.color_variant_id.is_(None))
        else:
            stmt = stmt.where(
                or_(
                    MediaAsset.color_variant_id == scope,
                    MediaAsset.color_variant_id.is_(None),
                )
            )
    rows = list(session.scalars(stmt.order_by(MediaAsset.created_at)))
    # 判定在这里,不在 WHERE 里 —— 见上面那一段
    return [
        asset
        for asset in rows
        if evidence_rules.asset_is_extraction_input(
            asset, imported_url_trusted=imported_url_trusted
        )
    ]


def usable_asset_count(session: Session, product_id: UUID) -> int:
    """`usable_assets()` 的条数,**不把行读出来**。

    存在的理由是一条很窄的需要:`run_extraction` 在证据为空时要把话说准 ——
    「素材库里有 12 条,但一条都不能当证据」和「一条素材都没有」是两种不同的
    处境,运营的下一步动作也相反(前者去查这些图是哪来的,后者去传图)。

    为此单开一个计数函数,而不是让识别路径去调 `usable_assets()`,是因为
    §5.1 的口径是「白名单查询助手成为**唯一取数入口**」。识别路径上只要
    出现过一个装着未过滤素材行的变量,下一个人就可能顺手拿它去喂抽取器 ——
    而那一步不会有任何地方报错。**拿不到行,就不会有人用错行。**

    `tests/pure/test_a45_batch14_19_evidence_query.py` 钉着这一条:
    `run_extraction` 里不许出现 `usable_assets`。
    """
    return int(
        session.scalar(
            select(func.count())
            .select_from(MediaAsset)
            .where(
                MediaAsset.product_id == product_id,
                MediaAsset.status == MediaStatus.READY.value,
            )
        )
        or 0
    )


# ------------------------------------------------------------------ 人工操作


def set_role(
    session: Session,
    media_id: UUID,
    *,
    role: MediaRole,
    actor: str,
) -> MediaAsset:
    """人工指定角色。**永远覆盖模型和规则的判断。**

    ## 状态守卫(报告 6.8)

    原来这里对任何状态的素材都放行,包括已删除和已隔离的。两者都有具体后果:

        已删除    这条记录代表"这张图没了"。给它改角色是在编辑一个不存在的
                  对象,而界面上它看起来改成功了
        已隔离    隔离的判据往往就是这张图不该出现在它现在这个位置。把角色
                  改成"主图"再放行,`can_be_primary` 会因为 role_source 变成
                  HUMAN 而直接放过 —— 人工角色不看置信度,那是设计如此。
                  于是隔离这道闸被绕开了,而绕开它的动作看起来只是"改个角色"

    隔离素材要改角色,先放行(`POST /api/media/{id}/release`)——
    放行是一个显式动作,会留下"谁放的、为什么",而改角色不会。
    """
    asset = get_asset(session, media_id)
    if asset.status == MediaStatus.DELETED.value:
        raise ValidationError(
            "已删除的素材不能修改角色", code=ErrorCode.INPUT_INVALID, http_status=409
        )
    if asset.status == MediaStatus.QUARANTINED.value:
        raise ValidationError(
            "已隔离的素材不能修改角色;确认无误请先放行,放行会留下记录",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    asset.role = role.value
    asset.role_source = RoleSource.HUMAN.value
    # 人工定的角色没有「把握程度」这回事,清掉模型留下的数字。
    # 留着它会让 can_be_primary 之类的判断读到一个已经无意义的值
    asset.role_confidence = None
    _audit(session, actor, asset, "set_role", {"role": role.value})
    return asset


def quarantine(
    session: Session, media_id: UUID, *, reason: str, actor: str
) -> MediaAsset:
    """隔离一条素材。不删除,可放行。

    图片合规预检(水印、logo、露出)有假阳性,删掉就找不回来了。
    隔离的素材不参与上架,但保留在库里。

    §7.3 的配套动作(引用它的 APPROVED 图片集自动降级为 PENDING_REVIEW)
    在 M5 图片集落地之后接上 —— 现在还没有图片集这张表。
    """
    asset = get_asset(session, media_id)
    if asset.status == MediaStatus.DELETED.value:
        raise ValidationError("已删除的素材不能隔离", code=ErrorCode.INPUT_INVALID)
    asset.status = MediaStatus.QUARANTINED.value
    asset.quarantine_reason = (reason or "")[:2000] or None
    session.flush()

    # 引用这张图的**已批准**图片集自动降级为待复核(§7.3)。
    #
    # 不降级的话,一张已经被判定为不合规的图会继续被发布 ——
    # 而隔离动作在界面上看起来是成功的。这条在 M1 文档里记的是
    # "等 M5 图片集落地后接上",现在接上了。
    from app.listings import image_set_service

    # A-39:把操作者传进去。隔离是人点的,自动降级是它的后果 ——
    # 审计里记成 system 会让运营查不到"这版图为什么突然待复核"
    downgraded = image_set_service.downgrade_sets_using(
        session, asset.id, actor=actor
    )

    _audit(
        session, actor, asset, "quarantine",
        {"reason": reason[:200], "downgraded_image_sets": len(downgraded)},
    )
    return asset


def release(session: Session, media_id: UUID, *, actor: str) -> MediaAsset:
    """放行一条被隔离的素材。"""
    asset = get_asset(session, media_id)
    if asset.status != MediaStatus.QUARANTINED.value:
        raise ValidationError(
            f"只有被隔离的素材可以放行,当前状态 {asset.status}",
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
        )
    asset.status = MediaStatus.READY.value
    asset.quarantine_reason = None
    _audit(session, actor, asset, "release", {})
    return asset


def _audit(session: Session, actor: str, asset: MediaAsset, action: str, payload: dict) -> None:
    from app.core.enums import AuditAction
    from app.services import audit

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="MediaAsset",
        entity_id=asset.id,
        payload={"action": action, **payload},
    )


# ------------------------------------------------------------------ 双读对账


# PARITY_FIELDS 原来在这里重复定义了一遍(取值与 `app.media.mapping` 相同),
# 而顶部已经导入了它。理由同 vision.py 那一处:静默覆盖,改上游的人看不见。
# 说明性注释在 mapping.py 的定义处,那里才是这张表该被读到的地方。


def reconcile_product_asset(session: Session, asset) -> list[MediaMigrationMismatch]:
    """双读对账:一条 `ProductAsset` 与它的影子素材(迁移 B)。

    **不一致时返回旧表的值给调用方**,这里只负责记录。这一步的目的是
    *发现*不一致,不是修复它 —— 让新表的值生效意味着一个还没被验证的
    迁移直接影响线上行为。
    """
    media = session.scalar(
        select(MediaAsset).where(
            MediaAsset.legacy_kind == LEGACY_PRODUCT_ASSET,
            MediaAsset.legacy_id == asset.id,
        )
    )
    if media is None:
        return [
            _mismatch(
                session,
                LEGACY_PRODUCT_ASSET,
                asset.id,
                None,
                "__missing__",
                "存在",
                "缺失",
                "影子写漏了这条,或回填还没跑到",
            )
        ]

    legacy = {
        "storage_path": asset.storage_path,
        "sha256": asset.file_hash,
        "mime_type": asset.mime_type,
        "width": asset.width,
        "height": asset.height,
        "bytes": asset.file_size,
    }
    return _compare(session, LEGACY_PRODUCT_ASSET, asset.id, media, legacy)


def reconcile_candidate(session: Session, candidate) -> list[MediaMigrationMismatch]:
    """双读对账:一条候选图与它的素材。"""
    if not candidate.storage_path:
        return []
    media = (
        session.get(MediaAsset, candidate.media_asset_id)
        if candidate.media_asset_id
        else None
    )
    if media is None:
        return [
            _mismatch(
                session, LEGACY_CANDIDATE, candidate.id, None,
                "__missing__", "存在", "缺失",
                "候选图没有关联素材,影子写漏了或回填未覆盖",
            )
        ]
    legacy = {
        "storage_path": candidate.storage_path,
        "sha256": candidate.file_hash,
        "mime_type": candidate.mime_type or "image/jpeg",
        "width": candidate.width or 0,
        "height": candidate.height or 0,
        "bytes": candidate.file_size or 0,
    }
    return _compare(session, LEGACY_CANDIDATE, candidate.id, media, legacy)


def _compare(
    session: Session,
    kind: str,
    legacy_id: UUID,
    media: MediaAsset,
    legacy: dict[str, Any],
) -> list[MediaMigrationMismatch]:
    found: list[MediaMigrationMismatch] = []
    for name in PARITY_FIELDS:
        left, right = legacy.get(name), getattr(media, name, None)
        if normalize_for_parity(left) != normalize_for_parity(right):
            found.append(
                _mismatch(session, kind, legacy_id, media.id, name, left, right, None)
            )
    return found


def _mismatch(
    session: Session,
    kind: str,
    legacy_id: UUID | None,
    media_id: UUID | None,
    field_name: str,
    legacy_value: Any,
    media_value: Any,
    note: str | None,
) -> MediaMigrationMismatch:
    row = MediaMigrationMismatch(
        legacy_kind=kind,
        legacy_id=legacy_id,
        media_asset_id=media_id,
        field_name=field_name,
        legacy_value=str(legacy_value)[:500] if legacy_value is not None else None,
        media_value=str(media_value)[:500] if media_value is not None else None,
        note=note,
    )
    session.add(row)
    logger.warning(
        "media migration mismatch",
        extra={
            "extra_fields": {
                "legacy_kind": kind,
                "legacy_id": str(legacy_id or ""),
                "field": field_name,
            }
        },
    )
    return row


def mismatch_summary(session: Session, *, days: int = 7) -> dict[str, int]:
    """最近 N 天的不一致统计。

    迁移 B 的出口条件是「一周无 mismatch」,这个函数就是那个判据的查询。
    只打日志不落表的话,到了该切换的那天没有任何人能拿出证据。
    """
    from datetime import timedelta

    cutoff = utc_now() - timedelta(days=max(1, days))
    rows = session.execute(
        select(MediaMigrationMismatch.legacy_kind, func.count())
        .where(MediaMigrationMismatch.created_at >= cutoff)
        .group_by(MediaMigrationMismatch.legacy_kind)
    ).all()
    result = {kind: int(count) for kind, count in rows}
    result["total"] = sum(result.values())
    return result


def coverage(session: Session) -> dict[str, int]:
    """回填覆盖率。给运维判断「回填跑完了没有」。"""
    from app.models.generation import GenerationCandidate
    from app.models.product_asset import ProductAsset

    def _count(model, *where) -> int:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return session.scalar(stmt) or 0

    return {
        "product_assets": _count(ProductAsset),
        "product_assets_shadowed": _count(
            MediaAsset, MediaAsset.legacy_kind == LEGACY_PRODUCT_ASSET
        ),
        "candidates_with_image": _count(
            GenerationCandidate, GenerationCandidate.storage_path.is_not(None)
        ),
        "candidates_linked": _count(
            GenerationCandidate, GenerationCandidate.media_asset_id.is_not(None)
        ),
        "media_assets": _count(MediaAsset),
    }
