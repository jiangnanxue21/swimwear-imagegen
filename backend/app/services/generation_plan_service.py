"""生成方案的服务层(PRD v3.1 §4.7 / §6.4)。

**判定一律不在这里。**指纹怎么算、哪个颜色当前生效的是哪一份、预算过不过,
全在 `app/workflows/generation_plan.py`(零依赖、可穷举)。这一层只做四件事:

    取数     把库里的行翻成 `PlanView`
    校验     调既有的授权闸与受众筛选,不自己写一套
    落库     写规范化之后的角度与算出来的指纹
    留痕     audit

## 唯一写入点

`plan_fingerprint` 列带 `info={"projection": True}`,与 `products` 上那八个
投影列同一套规矩:**只有这里能写它**。路由层或脚本自己拼一个的表现是
——创建任务时用 A、判过期时用 B,于是每次创建任务都会顺带把自己的
图片集判成过期。一个只会多花钱、不会报错的洞。

## ACTIVE 不原地改,DRAFT 可以原地改

ACTIVE 与 DRAFT 各有一个部分唯一槽。保存时更新同层 DRAFT;启用时先归档
旧 ACTIVE,再把 DRAFT 置 ACTIVE,两步在**同一个事务**里。这样启用后的方案
仍能由一份新 DRAFT 替换,而 ACTIVE 的指纹历史不会被覆盖。

并发首次保存靠 DRAFT 唯一索引裁决。冲突必须在保存点里捕获并回读赢家;
没有保存点时一次正常双击会把整个外层事务打成不可用。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_attribute

from app.core import audience as audience_rules
from app.core.enums import AuditAction, GenerationPlanStatus
from app.core.errors import DuplicateError, ErrorCode, NotFoundError, ValidationError
from app.models.generation_plan import GenerationPlan
from app.models.spu import ColorVariant, Spu
from app.providers import registry
from app.services import audit, model_template_service
from app.services.model_license import assert_usable
from app.workflows import generation_plan as gp


def to_view(row: GenerationPlan) -> gp.PlanView:
    """库行 -> 判定入参。**翻译只有这一处。**

    与 `scope_fingerprint.AssetView`、`media/evidence_rules` 那两处同一条
    理由:判定可穷举,而喂给判定的东西如果在每个调用点各翻一遍,
    守卫就只能靠 AST 看"调用出现了没有"。
    """
    return gp.PlanView(
        plan_id=str(row.id),
        color_variant_id=str(row.color_variant_id) if row.color_variant_id else None,
        model_template_id=str(row.model_template_id) if row.model_template_id else None,
        provider=row.provider or "",
        scene=row.scene or "",
        pose=row.pose or "",
        angles=gp.normalize_angles(row.angles_json),
        budget_cap=row.budget_cap,
        status=row.status,
    )


def list_plans(session: Session, spu_id: UUID) -> list[GenerationPlan]:
    """一个 SPU 的全部方案(含归档)。

    含归档是刻意的:界面要能回答"这批图是按哪份方案出的",而那份多半
    已经被归档了。过滤放在展示层,不放在取数 —— 取数少给一类,
    界面就永远没法把那类显示出来。
    """
    return list(
        session.scalars(
            select(GenerationPlan)
            .where(GenerationPlan.spu_id == spu_id)
            .order_by(GenerationPlan.color_variant_id, GenerationPlan.created_at)
        )
    )


def get_plan(session: Session, plan_id: UUID) -> GenerationPlan:
    row = session.get(GenerationPlan, plan_id)
    if row is None:
        raise NotFoundError("生成方案不存在")
    return row


def resolve_for(
    session: Session, *, spu_id: UUID, color_variant_id: UUID | None
) -> GenerationPlan | None:
    """某个颜色**当前生效**的方案。颜色覆盖优先,回落 SPU 默认。

    判定在 `gp.resolve_plan`,这里只负责取数 —— 写成一句 SQL
    (`ORDER BY color_variant_id NULLS LAST LIMIT 1`)会让"颜色覆盖优先"
    这条规则活在 SQL 里,而 SQL 在这台机器上一次都执行不了。
    """
    rows = list_plans(session, spu_id)
    wanted = str(color_variant_id) if color_variant_id else None
    chosen = gp.resolve_plan([to_view(r) for r in rows], wanted)
    if chosen is None:
        return None
    return next(r for r in rows if str(r.id) == chosen.plan_id)


def _assert_scope(session: Session, *, spu_id: UUID, color_variant_id: UUID | None) -> None:
    if session.get(Spu, spu_id) is None:
        raise NotFoundError("SPU 不存在")
    if color_variant_id is None:
        return
    variant = session.get(ColorVariant, color_variant_id)
    if variant is None:
        raise NotFoundError("颜色变体不存在")
    if variant.spu_id != spu_id:
        # 挡的是"给 A 款的颜色配了 B 款的方案"。外键各自成立,所以数据库
        # 一声不吭 —— 而那份方案永远解析不到,表现是"配了方案却没生效"
        raise ValidationError(
            "颜色变体不属于这个 SPU", code=ErrorCode.INPUT_INVALID
        )


def _assert_plan_usable(view: gp.PlanView, *, raw_angle_count: int) -> None:
    """方案本身能不能用。判定在 `gp.plan_problems`,这里只把环境事实递进去。

    `usable_providers` 从注册表现取(2026-08-11 评审):它是"这个部署装了
    什么、配了什么 Key",不是一个可穷举的判定,所以不能写进纯模块 ——
    理由与 `plan_problems` 那个参数上的说明是同一条。
    """
    problems = gp.plan_problems(
        view,
        raw_angle_count=raw_angle_count,
        usable_providers=registry.selectable_names(),
    )
    if problems:
        raise ValidationError(
            ";".join(p.message for p in problems), code=ErrorCode.INPUT_INVALID
        )


def _assert_model_template_usable(
    session: Session, *, spu_id: UUID, model_template_id: UUID | None
) -> None:
    """这份方案选的模特,拿去给这个 SPU 出图会不会被拒(2026-08-11 评审)。

    ## 改之前只查了四分之一

    `save_plan` 只做 `assert_usable(get_template(...))` —— 也就是只查授权
    状态与到期。三样没查:

        template.enabled                  已停用的模特照样能存进方案
        受众匹配(§10.5 / §12.4)          女装 SPU 能配 MEN 模特
        禁用品类(assert_usable 的第四条)  没传 category_id,那一条恒不触发

    而 `activate()` **一次都不验**。于是这条动线是真实可走的:

        女装 SPU -> 选到 MEN 模特 -> 保存成功 -> 启用成功 -> 旧的正确方案被归档
        -> 真正创建任务时才被 `generation_service` 拒 -> 这个颜色出不了图,
           而那份能出图的方案已经不在 ACTIVE 了

    也就是说**坏方案能成为正式 ACTIVE 方案,并且顶掉一份好的**。所以
    `activate()` 必须重验,而且要**先验证再归档**(见 `activate`)。

    ## 受众与品类都从 SPU 取,不从某一行 SKU 取

    方案是 SPU + 颜色作用域的,没有唯一的"那一行商品"。`Spu.audience`
    非空、`Spu.base_category` 也非空,而规则包键的派生只有
    `core/audience.category_code_for` 一处(§23.5)—— 这里递的就是那一处,
    与 `channels/generic.category_id_for` 是同一个函数,不是另写一份。

    派生不出规则包时(受众与品类码里编入的前缀冲突)**只跳过"禁用品类"
    那一条**,其余三条照判 —— 与 `generation_service` 里那段同一个取舍:
    不因为派生不出就整个放行。
    """
    if model_template_id is None:
        return
    template = model_template_service.get_template(session, model_template_id)
    if not template.enabled:
        raise ValidationError(
            "选中的模特模板已停用,不能写进生成方案", code=ErrorCode.INPUT_INVALID
        )

    spu = session.get(Spu, spu_id)
    spu_audience = getattr(spu, "audience", None)
    block = audience_rules.generation_block_reason(spu_audience, template.audience)
    if block is not None:
        raise ValidationError(block, code=ErrorCode.INPUT_INVALID)

    category_id = None
    try:
        category_id = audience_rules.category_code_for(
            spu_audience, getattr(spu, "base_category", "") or ""
        )
    except Exception:  # noqa: BLE001 - 见 docstring 最后一段
        category_id = None
    assert_usable(template, category_id=category_id)


def _draft_for_scope(
    session: Session,
    *,
    spu_id: UUID,
    color_variant_id: UUID | None,
    lock: bool = False,
) -> GenerationPlan | None:
    """同作用域正在编辑的唯一 DRAFT。锁只用于顺序编辑已有草稿。"""
    scope_filter = (
        GenerationPlan.color_variant_id.is_(None)
        if color_variant_id is None
        else GenerationPlan.color_variant_id == color_variant_id
    )
    statement = select(GenerationPlan).where(
        GenerationPlan.spu_id == spu_id,
        scope_filter,
        GenerationPlan.status == GenerationPlanStatus.DRAFT.value,
    )
    if lock:
        statement = statement.with_for_update()
    return session.scalars(statement).one_or_none()


def _update_draft(target: GenerationPlan, source: GenerationPlan) -> None:
    """DRAFT 尚未被生成任务引用,可在启用前保留身份并更新内容。"""
    target.model_template_id = source.model_template_id
    target.provider = source.provider
    target.scene = source.scene
    target.pose = source.pose
    target.angles_json = source.angles_json
    target.budget_cap = source.budget_cap
    target.plan_fingerprint = source.plan_fingerprint
    # 列写入审计对 `obj.note = ...` 刻意按名字宽认,会把这一句同时算成
    # `ListingImageSet.note` 的写入点,让那笔真实欠账从台账里消失。
    # 走 SQLAlchemy 的正式属性 API,既保留脏标记/事件,又不制造假写入者。
    set_attribute(target, GenerationPlan.note.key, source.note)
    target.row_version += 1


def save_plan(
    session: Session,
    *,
    spu_id: UUID,
    color_variant_id: UUID | None = None,
    model_template_id: UUID | None = None,
    provider: str = "",
    scene: str = "",
    pose: str = "",
    angles: Any = None,
    budget_cap: Decimal | None = None,
    note: str | None = None,
    actor: str = "system",
) -> GenerationPlan:
    """保存同作用域的唯一 DRAFT。**不直接生效**。"""
    _assert_scope(session, spu_id=spu_id, color_variant_id=color_variant_id)

    # §6.4:「ModelTemplate 可用(现有授权闸 + 受众筛选 §10.5 + 生成前阻断)」。
    # 走既有的判定,不在这里重写一遍 —— 方案不是绕过授权的第二条路,
    # 那正是 §6.4 保留 v3.0 那条约束的原因。
    #
    # **在这里就挡,而不是等创建任务那一刻**:方案是运营会反复看的对象,
    # 一份挂着已吊销模特的方案摆在界面上,每个人都会以为它能用。
    #
    # 2026-08-11:这里原来只调 `assert_usable`,漏了 enabled / 受众 / 禁用品类
    # 三样。三样都在 `_assert_model_template_usable` 里,`activate()` 也调它。
    _assert_model_template_usable(
        session, spu_id=spu_id, model_template_id=model_template_id
    )

    normalized = gp.normalize_angles(angles)
    raw_count = len(angles) if isinstance(angles, (list, tuple, dict)) else 0

    row = GenerationPlan(
        spu_id=spu_id,
        color_variant_id=color_variant_id,
        model_template_id=model_template_id,
        provider=(provider or "").strip(),
        scene=(scene or "").strip(),
        pose=(pose or "").strip(),
        angles_json=gp.serialize_angles(normalized),
        budget_cap=budget_cap,
        note=note,
        status=GenerationPlanStatus.DRAFT.value,
        created_by=actor,
    )
    view = to_view(row)
    _assert_plan_usable(view, raw_angle_count=raw_count)
    # **唯一写入点。**见模块文档第二段
    row.plan_fingerprint = gp.fingerprint_of(view)

    existing = _draft_for_scope(
        session,
        spu_id=spu_id,
        color_variant_id=color_variant_id,
        lock=True,
    )
    if existing is not None:
        # 相同请求直接复用,连 row_version 与审计都不多写一次。
        if (
            existing.plan_fingerprint == row.plan_fingerprint
            and existing.note == row.note
        ):
            return existing
        _update_draft(existing, row)
        session.flush()
        audit.record(
            session,
            actor=actor,
            action=AuditAction.UPDATE,
            entity_type="GenerationPlan",
            entity_id=existing.id,
            payload={
                "status": GenerationPlanStatus.DRAFT.value,
                "plan_fingerprint": existing.plan_fingerprint,
                "angles": existing.angles_json,
            },
        )
        return existing

    try:
        # `add` 必须在保存点里面:`begin_nested()` 会先 flush 已有 pending,
        # 放在外面会让唯一冲突发生在 try 覆盖不到的位置。
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        winner = _draft_for_scope(
            session,
            spu_id=spu_id,
            color_variant_id=color_variant_id,
        )
        if winner is None:
            raise
        if (
            winner.plan_fingerprint != row.plan_fingerprint
            or winner.note != row.note
        ):
            raise DuplicateError(
                "生成方案草稿已被另一个请求修改,请刷新后重试"
            ) from None
        return winner

    audit.record(
        session,
        actor=actor,
        action=AuditAction.CREATE,
        entity_type="GenerationPlan",
        entity_id=row.id,
        payload={
            "spu_id": str(spu_id),
            "color_variant_id": str(color_variant_id) if color_variant_id else None,
            "plan_fingerprint": row.plan_fingerprint,
            "angles": row.angles_json,
        },
    )
    return row


def activate(session: Session, plan_id: UUID, *, actor: str = "system") -> GenerationPlan:
    """启用一份方案:**先重验**,再归档同层的旧方案,最后把这一份置 ACTIVE。

    两步必须在同一个事务里,理由见模块文档最后一段。

    ## 为什么 activate 必须自己再验一遍(2026-08-11 评审)

    保存与启用之间可以隔任意长时间,而这中间会变的东西不少:模特被停用、
    授权到期或吊销、FASHN 的 Key 被撤掉、SPU 的受众被改过。改之前这里
    **一次都不验** —— 于是一份保存时合法的草稿可以在这些前提全部失效之后
    被启用,而运营看到的是"启用成功"。

    ## 顺序:验证在归档之前

    `activate()` 会把同作用域的旧 ACTIVE 归档。验证放在归档之后(哪怕
    同一个事务会回滚)也是错的写法 —— 一次异常路径上的顺序依赖,
    读代码的人无法从这里看出它安全。放在最前面,失败时什么都没动过。

    这一条正是评审那句"activate 应先验证,再 archive 旧方案"。
    """
    row = get_plan(session, plan_id)
    if row.status == GenerationPlanStatus.ARCHIVED.value:
        raise DuplicateError("已归档的方案不能重新启用,请复制一份新的")

    # 重验:模特(enabled / 受众 / 授权 / 禁用品类)与方案本身(角度、张数、
    # Provider 可用性)。两者都可能在保存之后失效
    _assert_model_template_usable(
        session, spu_id=row.spu_id, model_template_id=row.model_template_id
    )
    _assert_plan_usable(to_view(row), raw_angle_count=len(row.angles_json or []))

    scope_filter = (
        GenerationPlan.color_variant_id.is_(None)
        if row.color_variant_id is None
        else GenerationPlan.color_variant_id == row.color_variant_id
    )
    previous = session.scalars(
        select(GenerationPlan)
        .where(
            GenerationPlan.spu_id == row.spu_id,
            scope_filter,
            GenerationPlan.status == GenerationPlanStatus.ACTIVE.value,
        )
        .with_for_update()
    ).all()
    archived: list[GenerationPlan] = []
    for old in previous:
        if old.id == row.id:
            continue
        old.status = GenerationPlanStatus.ARCHIVED.value
        archived.append(old)

    # 两次 flush 仍在同一事务里。先释放 ACTIVE 唯一槽,再让 DRAFT 占进去;
    # 合成一次 flush 时 ORM 的 UPDATE 顺序不承诺先旧后新。
    if archived:
        session.flush()

    row.status = GenerationPlanStatus.ACTIVE.value
    row.row_version += 1
    session.flush()

    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="GenerationPlan",
        entity_id=row.id,
        payload={
            "status": row.status,
            "plan_fingerprint": row.plan_fingerprint,
            # 谁被顶下去了要留痕:§8.1「更换生成方案」那一行的触发时机
            # 就是这一刻,而"哪些颜色因此要重出图"要能追溯
            "archived": [str(o.id) for o in archived],
        },
    )
    return row


def stale_image_scopes(
    session: Session,
    *,
    spu_id: UUID,
    before: list[gp.PlanView],
    after: list[gp.PlanView] | None = None,
) -> frozenset[str]:
    """改完方案之后,哪些颜色的图片集过期了(§8.1 第 7 行)。

    入参是 `PlanView` 而不是库行,**这一点是必须的**:`activate()` 会原地
    改 ORM 行的 status,取完库行再改的话,调用方手里的 `before` 会跟着变,
    于是"变了哪些作用域"永远算出空集 —— 一个不报错、只让图片集该过期
    而没过期的洞。`PlanView` 是冻结的 dataclass,拿不到这种反噬。

    颜色清单**由库里的 ACTIVE 颜色给出**,不由调用方传:传清单的写法有个
    具体的坏处 —— 清单漏了一个颜色时,那个颜色的图片集永远不会被判过期,
    而"漏了一个"不会有任何征兆(与 `scope_fingerprint.fingerprints` 同源)。
    """
    variants = [
        str(v.id)
        for v in session.scalars(
            select(ColorVariant).where(ColorVariant.spu_id == spu_id)
        )
    ]
    current = after if after is not None else [to_view(r) for r in list_plans(session, spu_id)]
    return gp.image_set_stale_scopes(
        gp.effective_fingerprints(before, variants),
        gp.effective_fingerprints(current, variants),
    )


def required_angles_for(session: Session, spu_id: UUID) -> dict[str, list[str]]:
    """每个颜色当前生效方案要求的角度。喂给 §6.5 的图片集验收。

    没有生效方案的颜色**不出现在结果里**,而不是给一个空集合:
    空集合会被读成"这个颜色不要求任何角度",于是没配方案等于免检 ——
    而 §6.4 的前置校验本来就要求先有方案才能创建任务。
    """
    plans = [to_view(r) for r in list_plans(session, spu_id)]
    out: dict[str, list[str]] = {}
    for variant in session.scalars(
        select(ColorVariant).where(ColorVariant.spu_id == spu_id)
    ):
        chosen = gp.resolve_plan(plans, str(variant.id))
        if chosen is not None and chosen.angles:
            out[str(variant.id)] = sorted(gp.required_angles(chosen.angles))
    return out
