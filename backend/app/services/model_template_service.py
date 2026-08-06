"""模特模板服务。复用与商品素材相同的哈希去重存储。

PRD v2 起两条新职责:

    §10.5  受众硬约束的候选过滤(`list_templates(for_product_audience=...)`)
    §11.3  授权失效的硬阻断(`assert_usable`,生成任务创建前必经)
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core import audience as audience_rules
from app.core.config import settings
from app.core.enums import (
    AssetType,
    Audience,
    AuditAction,
    ModelLicenseStatus,
)
from app.core.errors import ErrorCode, NotFoundError, ValidationError
from app.core.hashing import hash_bytes
from app.models.model_template import ModelTemplate
from app.services import audit

# 授权判定是**纯逻辑**,拆在 `model_license` 里(与 core/audience.py、
# workbench/audience_rules.py 同一个惯例:规则不依赖 ORM,才测得动)。
# 这里再导出一次,调用方不必知道它搬过家
from app.services.model_license import assert_usable  # noqa: F401
from app.services.storage import ObjectStorage
from app.services.upload_validation import validate_upload


def list_templates(
    session: Session,
    *,
    enabled_only: bool = False,
    audience: str | None = None,
    for_product_audience: str | None = None,
) -> list[ModelTemplate]:
    """模板列表。

    两个受众参数语义不同,别混用:

        audience              管理员按模特受众筛选(精确匹配一个值)
        for_product_audience  给某个商品挑候选(§10.5 硬约束):
                              WOMEN 商品只出 WOMEN 模特、MEN 只出 MEN、
                              UNISEX 两种都出。**不匹配的模特不出现在
                              候选列表里,不是排在后面** —— 理由 §10.5
                              写得很清楚:错配图在纯技术指标上往往是好的,
                              评分器不查穿的人是谁,人工也容易放过,
                              所以必须拦在选择阶段。

    过滤在 SQL 里做而不是取回来再筛:候选列表是运营高频页面,
    模特库会随受众翻倍。
    """
    stmt = select(ModelTemplate).order_by(ModelTemplate.created_at.desc())
    if enabled_only:
        stmt = stmt.where(ModelTemplate.enabled.is_(True))
    if audience:
        stmt = stmt.where(ModelTemplate.audience == Audience(audience.upper()).value)
    if for_product_audience:
        product_audience = audience_rules.coerce(for_product_audience)
        if product_audience is Audience.UNISEX:
            stmt = stmt.where(
                or_(
                    ModelTemplate.audience == Audience.WOMEN.value,
                    ModelTemplate.audience == Audience.MEN.value,
                )
            )
        elif product_audience is not None:
            stmt = stmt.where(ModelTemplate.audience == product_audience.value)
        # None(受众待确认)不过滤:那个商品该先去确认受众(§8.2),
        # 不是在这里替它猜一个候选集
    return list(session.scalars(stmt))


def get_template(session: Session, template_id: UUID) -> ModelTemplate:
    template = session.get(ModelTemplate, template_id)
    if template is None:
        raise NotFoundError("模特模板不存在")
    return template


def create_template(
    session: Session,
    *,
    data: bytes,
    filename: str,
    payload: dict,
    storage: ObjectStorage,
    actor: str,
) -> ModelTemplate:
    validated = validate_upload(
        data,
        asset_type=AssetType.MODEL_REFERENCE,
        max_bytes=settings.max_upload_bytes,
        min_edge_px=settings.MIN_IMAGE_EDGE_PX,
    )
    file_hash = hash_bytes(data)
    stored = storage.save(
        data, file_hash=file_hash, extension=validated.info.extension, prefix="models"
    )

    # ---- 受众与体型词表(§10.4 / §10.5) ----
    #
    # 受众必须显式给。默认 WOMEN 会让男装模特被顺手传成女装 —— 而那个
    # 错误方向恰好是危险的一侧(男装商品的候选列表里冒出女性模特)。
    raw_audience = payload.get("audience")
    try:
        template_audience = audience_rules.coerce(raw_audience)
    except ValueError:
        template_audience = None
    if template_audience is None or template_audience is Audience.UNISEX:
        # UNISEX 模特不存在:那是商品的取值(§10.5),模特本人总有受众
        raise ValidationError(
            "模特必须标注受众(WOMEN 或 MEN);UNISEX 是商品的取值,不是模特的",
            code=ErrorCode.INPUT_INVALID,
        )
    body_type = str(payload.get("body_type", "UNKNOWN") or "UNKNOWN").upper()
    allowed_bodies = audience_rules.body_types_allowed(template_audience)
    if body_type not in allowed_bodies:
        # §10.4:"给男装模特打上 CURVY 不应该是一个可以做到的操作"
        raise ValidationError(
            f"体型 {body_type} 不在{template_audience.value}词表里,"
            f"可选:{', '.join(allowed_bodies)}",
            code=ErrorCode.INPUT_INVALID,
        )

    template = ModelTemplate(
        name=payload.get("name") or filename,
        storage_path=stored.storage_path,
        file_hash=file_hash,
        mime_type=validated.info.mime_type,
        width=validated.info.width,
        height=validated.info.height,
        pose=payload.get("pose", "UNKNOWN"),
        body_type=body_type,
        background=payload.get("background", "studio"),
        tags=payload.get("tags") or [],
        notes=payload.get("notes"),
        # ---- §11 授权字段组。缺省一律"未核实",不替人签字 ----
        audience=template_audience.value,
        source=str(payload.get("source") or ""),
        license_status=str(
            payload.get("license_status") or ModelLicenseStatus.UNVERIFIED.value
        ).upper(),
        commercial_scope=payload.get("commercial_scope"),
        allowed_platforms=payload.get("allowed_platforms") or [],
        allowed_regions=payload.get("allowed_regions") or [],
        # 归一成 naive UTC。表单是 ISO 字符串,直接进 DateTime 列会在
        # SQLite 上 flush 报错、在 assert_usable 里比较时抛 TypeError
        license_start_at=_naive_utc(payload.get("license_start_at")),
        license_expires_at=_naive_utc(payload.get("license_expires_at")),
        allow_ai_dressing=bool(payload.get("allow_ai_dressing", False)),
        allow_derivative_images=bool(payload.get("allow_derivative_images", False)),
        age_verified=bool(payload.get("age_verified", False)),
        prohibited_categories=payload.get("prohibited_categories") or [],
        # 检查没过的图默认**停用**。以前 validate_upload 的结论被整个丢掉,
        # 一张 200×300 的模糊图会照常启用,然后被自动路由选中去做试穿 ——
        # 出来的图必然判 C,而运营完全不知道问题出在模板上。
        # 不直接拒绝是刻意的:检查项里有「建议」性质的warning,人可以自己决定启用。
        enabled=bool(validated.check_passed),
    )
    if not validated.check_passed:
        template.notes = (
            (payload.get("notes") or "") + f"\n[上传检查] {validated.check_message}"
        ).strip()[:2000]
    session.add(template)
    session.flush()

    audit.record(
        session, actor=actor, action=AuditAction.CREATE,
        entity_type="ModelTemplate", entity_id=template.id,
        payload={
            "name": template.name,
            "file_hash": file_hash,
            # 受众与年龄确认进审计:§11 的字段将来被人改动时,
            # "入库时是什么"要能查
            "audience": template.audience,
            "age_verified": template.age_verified,
            "license_status": template.license_status,
        },
    )
    return template




def _naive_utc(value: Any) -> Any:
    """把授权日期归一成**naive UTC**,与库里两个 DateTime 列的约定一致。

    ## 这里曾经有两个会炸的坑

    表单走 `model_dump(mode="json")`,于是 `license_expires_at` 到达这一层时
    是一个 **ISO 字符串**而不是 datetime。直接塞进 `DateTime` 列:

        SQLite   flush 当场 TypeError(只接受 datetime/date)
        Postgres 可能被库侧强转,于是同一份代码两个环境行为不同

    更要命的是 `model_license.assert_usable` 的那句比较:

        now = datetime.now(UTC).replace(tzinfo=None)
        if expires <= now:

    `str <= datetime` 抛 TypeError;而运营填的日期若带时区偏移
    (`2027-01-01T00:00:00+08:00`),存进去是 aware,
    `aware <= naive` 同样抛 TypeError。**两条都不是"判定错了",是崩**,
    而且崩在授权闸口上 —— 那正是这一轮要让它开始生效的东西。

    在此之前这个坑是**潜伏的**:界面上根本没有授权日期的输入框,
    没人填得进来。本轮把输入框加上,它就活了 —— 所以归一化和输入框
    必须同一轮落地。
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValidationError(
                f"授权日期格式不合法:{text!r};请用 ISO 8601",
                code=ErrorCode.INPUT_INVALID,
            ) from None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value
    return value


#: PATCH 允许改的列。白名单而不是黑名单:将来给 ModelTemplate 加列时,
#: 默认是**不可通过接口改**,要改得有人显式加进来。反过来(黑名单)的话,
#: 新加的 storage_path / file_hash 一类的列会默认可写。
UPDATABLE_FIELDS: frozenset[str] = frozenset(
    {
        "name", "pose", "background", "tags", "notes",
        "source", "license_status", "commercial_scope",
        "allowed_platforms", "allowed_regions",
        "license_start_at", "license_expires_at",
        "allow_ai_dressing", "allow_derivative_images",
        "age_verified", "prohibited_categories", "disabled_reason",
    }
)

#: 改动进审计 payload 的字段。**授权与年龄必须逐次留痕**(§11):
#: "谁在什么时候把这个模特标成已授权/已确认成年"是这套记录唯一的意义 ——
#: 只存最终值的话,出问题时答不上是谁签的字。
_AUDITED_FIELDS: tuple[str, ...] = (
    "license_status", "age_verified", "allow_ai_dressing",
    "allow_derivative_images", "license_expires_at", "disabled_reason",
)


def _nullable(column_name: str) -> bool:
    """这一列允不允许存 NULL。**问表,不维护第二张清单**(同 product_service)。

    手写"可空字段名单"会和模型定义分叉,分叉的表现是某个字段清不掉、
    或者清掉之后撞数据库约束报 500 —— 两种都不会有人在改模型时想起来。
    """
    column = ModelTemplate.__table__.columns.get(column_name)
    return bool(column is not None and column.nullable)


def update_template(
    session: Session, template_id: UUID, payload: dict, *, actor: str
) -> ModelTemplate:
    """改模特模板(§11 授权字段组)。**PATCH 语义:不传的键不动。**

    调用方传的是 `model_dump(exclude_unset=True)` —— 用 `exclude_unset`
    而不是 `exclude_none`:`None` 对 `commercial_scope` / `license_expires_at`
    这类字段是一个**合法取值**(清空),而"没传"才是不改。两者混同的话,
    运营永远清不掉一个填错的到期日。
    """
    template = get_template(session, template_id)

    changes: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in UPDATABLE_FIELDS:
            # 白名单外的键直接拒绝而不是静默忽略:静默忽略会让调用方
            # 以为自己改成功了(接口返回 200,值没变)
            raise ValidationError(
                f"字段 {key} 不允许修改", code=ErrorCode.INPUT_INVALID
            )
        # 显式 null 只对**可空列**是"清空"。非空列(name / tags /
        # license_status / age_verified …)收到 null 会一路走到数据库撞
        # NOT NULL 约束,报 500 —— 与 `product_service.update_product`
        # 同一条规矩:问表、不维护第二张清单,在接口层换成一句人话。
        if value is None and not _nullable(key):
            raise ValidationError(
                f"{key} 不允许清空", code=ErrorCode.INPUT_INVALID
            )
        if key in ("license_start_at", "license_expires_at"):
            value = _naive_utc(value)
        elif hasattr(value, "value") and not isinstance(value, (str, bool, int)):
            # mode="json" 之后枚举已经是字符串;这条只兜 mode="python" 的调用方
            value = value.value
        before = getattr(template, key, None)
        if before == value:
            continue
        setattr(template, key, value)
        changes[key] = value

    if not changes:
        return template

    # ---- 授权时间窗:**合并后**校验(A45 独立审查 C-07) ----
    #
    # 创建表单有 `_license_window_is_ordered`,PATCH schema 却没有;
    # 而且单边 PATCH(只改开始或只改结束)光看请求体根本判不了 ——
    # 必须与库里另一端合并之后再问一次。此刻 setattr 已经把改动落在
    # 实体上,template 身上就是合并结果;抛错由请求级会话回滚。
    start, end = template.license_start_at, template.license_expires_at
    if start is not None and end is not None and end <= start:
        raise ValidationError(
            "授权到期时间必须晚于开始时间",
            code=ErrorCode.INPUT_INVALID,
        )

    session.flush()

    # 审计 payload 只带授权相关的字段值 + 改了哪些键。
    # 不把整行快照写进去:notes 可以是 2000 字,而审计表要能被人读
    audited = {k: v for k, v in changes.items() if k in _AUDITED_FIELDS}
    audit.record(
        session, actor=actor, action=AuditAction.UPDATE,
        entity_type="ModelTemplate", entity_id=template.id,
        payload={"changed": sorted(changes), **audited},
    )
    return template


def set_enabled(session: Session, template_id: UUID, enabled: bool, *, actor: str) -> ModelTemplate:
    template = get_template(session, template_id)
    template.enabled = enabled
    session.flush()
    audit.record(
        session, actor=actor, action=AuditAction.UPDATE,
        entity_type="ModelTemplate", entity_id=template.id, payload={"enabled": enabled},
    )
    return template
