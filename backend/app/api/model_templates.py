"""模特模板接口。"""
from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from app.api.deps import (
    current_actor,
    db_session,
    has_admin_rights,
    read_within_limit,
    require_operator,
)
from app.core.config import settings
from app.core.enums import AUDIENCE_LABELS
from app.core.errors import AppError, ErrorCode, ValidationError
from app.schemas.asset import (
    LICENSE_ENDORSEMENT_FIELDS,
    ModelTemplateForm,
    ModelTemplateUpdateForm,
)
from app.schemas.generation import ModelTemplateOut
from app.services import model_template_service as mts
from app.services.storage import asset_url, build_storage

# 整个路由挂 require_operator(读接口也要)。为什么挂在路由器上见 `deps.require_operator`
router = APIRouter(
    prefix="/model-templates",
    tags=["model-templates"],
    dependencies=[Depends(require_operator)],
)


def get_storage():
    return build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )


def _assert_may_endorse(request: Request, endorsed: set[str]) -> None:
    """签 §11 合规字段要管理员权限(2026-08-11 评审)。

    `endorsed` 是这次请求里**真的在写**背书字段的那些键。空集合直接放行 ——
    上传一个"全部未核实"的模特模板仍然是 operator 的日常动作,那条动线不动。

    ## 为什么判的是"写了什么"而不是"挂哪个依赖"

    整个路由器挂的是 `require_operator`(上传素材要它)。把 create / patch
    整体换成 `require_admin` 会把「传一张模特图」也变成管理员动作,而那
    既不是 PRD 的意思,也会当场打断现有的素材上传动线 —— 这正是
    `update_template` 的 docstring 当初拒绝只锁一边的理由的另一面。

    所以闸开在**字段**上:描述字段谁都能写,签字字段只有管理员能写。
    两个入口共用这一个函数与
    `schemas/asset.LICENSE_ENDORSEMENT_FIELDS` 一份清单 —— 只锁一边等于没锁,
    而分别写两份判断迟早会分叉成"create 松一格"。

    码用 `AUTH_FORBIDDEN` / 403,不是 401:这把凭据是**对的**,只是权限不够。
    用 AUTH_FAILED 的话前端会点亮"后端不认这把口令"的横幅,
    然后运营去改一把本来就正确的口令(`deps.rejection` 顶部记着那件事)。
    """
    if not endorsed or has_admin_rights(request):
        return
    raise AppError(
        "修改模特的授权与合规字段需要管理员权限(这些字段是合规背书,"
        "生成前的授权闸直接拿它们做判断):" + "、".join(sorted(endorsed)),
        code=ErrorCode.AUTH_FORBIDDEN,
        http_status=403,
    )


def _endorsements_asserted(payload: dict) -> set[str]:
    """这次请求把哪些背书字段写成了**和默认值不同**的值。

    创建路径用它。判"给了值"是不行的:界面上那张表单每次都把十个字段
    连同默认值一起发出来(`ModelTemplatesPage.LicenseFields`),按"给了值"
    判会让每一次正常创建都 403。

    判据是"和 `ModelTemplateForm` 的默认值不同" —— 也就是**真的在断言什么**:
    `UNVERIFIED` / false / 空列表 / None 的含义都是"没人核实过",
    那不是签字,不需要管理员。

    默认值从 `model_fields` 现取,不在这里抄一份字面量:抄下来的那份会在
    有人改动 schema 默认值时静默过期,而过期的方向是**把签字当成默认放行**。
    枚举默认值(`ModelLicenseStatus.UNVERIFIED`)和 `model_dump(mode="json")`
    出来的字符串能直接比,因为本仓的枚举一律是 `StrEnum`。
    """
    defaults = {
        field: ModelTemplateForm.model_fields[field].get_default(
            call_default_factory=True
        )
        for field in LICENSE_ENDORSEMENT_FIELDS
    }
    return {
        field
        for field, default in defaults.items()
        if payload.get(field) != default
    }


def _out(template, storage) -> ModelTemplateOut:
    result = ModelTemplateOut.model_validate(template)
    # 模特参考图是私有素材,不该有永久公开地址
    result.url = asset_url(storage, template.storage_path)
    return result


@router.get("", response_model=list[ModelTemplateOut])
def list_templates(
    session: Session = Depends(db_session),
    enabled_only: bool = Query(False),
    audience: str | None = Query(
        None, description="按模特受众精确筛选(管理端用):WOMEN / MEN"
    ),
    for_product_audience: str | None = Query(
        None,
        description=(
            "给某个受众的商品挑候选(§10.5 硬约束):WOMEN 只出 WOMEN、"
            "MEN 只出 MEN、UNISEX 两种都出。不匹配的模特**不出现在列表里**"
        ),
    ),
    storage=Depends(get_storage),
) -> list[ModelTemplateOut]:
    try:
        rows = mts.list_templates(
            session,
            enabled_only=enabled_only,
            audience=audience,
            for_product_audience=for_product_audience,
        )
    except ValueError as exc:
        # 受众取值不认识。这里必须报错而不是"忽略这个筛选返回全部"——
        # 后者会让一个拼错参数的前端拿到女装模特去配男装商品(§10.5)
        #
        # A69:**原来这里拼的是 `{exc}`**,而它是 `StrEnum` 的原生英文报错 ——
        # 运营看到的整句是「受众取值不合法:'KIDS' is not a valid Audience」。
        # 后半句一个字都不是给运营写的,而且它是 Python 的措辞,不是这个系统的。
        #
        # 中文名 `AUDIENCE_LABELS` 早就在 `core/enums.py` 里,只是没走到报错
        # 这条路上来。列出可选值而不是只说"不合法":只说不合法等于让人挨个试。
        raise ValidationError(
            f"没有「{audience}」这种受众;可选:"
            + "、".join(AUDIENCE_LABELS.values()),
            code=ErrorCode.INPUT_INVALID,
        ) from None
    return [_out(t, storage) for t in rows]


@router.post(
    "",
    response_model=ModelTemplateOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator)],
)
async def create_template(
    request: Request,
    session: Session = Depends(db_session),
    file: UploadFile = File(...),
    name: str = Form(...),
    #: **必填,无默认**(§10.5)。默认值会让男装模特被顺手传成女装 ——
    #: 那个错误方向恰好是危险的一侧
    audience: str = Form(..., description="WOMEN 或 MEN;UNISEX 是商品的取值"),
    pose: str = Form("UNKNOWN"),
    body_type: str = Form("UNKNOWN"),
    background: str = Form("studio"),
    tags: str = Form("[]", description='JSON 数组,如 ["亚洲","长发"]'),
    notes: str | None = Form(None),
    # ---- §11 授权字段组 ----
    source: str = Form("", description="素材来源:签约拍摄 / 素材库采购 / 自有"),
    license_status: str = Form("UNVERIFIED"),
    commercial_scope: str | None = Form(None),
    allowed_platforms: str = Form("[]", description="JSON 数组"),
    allowed_regions: str = Form("[]", description="JSON 数组"),
    license_start_at: str | None = Form(None, description="ISO 8601"),
    license_expires_at: str | None = Form(None, description="ISO 8601"),
    allow_ai_dressing: bool = Form(False),
    allow_derivative_images: bool = Form(False),
    age_verified: bool = Form(
        False, description="§11.1 已确认成年。默认 false = 没人核实过"
    ),
    prohibited_categories: str = Form("[]", description="JSON 数组"),
    storage=Depends(get_storage),
) -> ModelTemplateOut:
    # C-11:与素材上传共用同一个分块读取,超限尽早停下
    limit = settings.max_upload_bytes
    data = await read_within_limit(file, limit)

    def _json_list(raw: str, field: str) -> list:
        try:
            parsed = json.loads(raw) if raw else []
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 JSON 数组") from exc
        if not isinstance(parsed, list):
            raise ValidationError(f"{field} 必须是 JSON 数组")
        return parsed

    parsed_tags = _json_list(tags, "tags")
    parsed_platforms = _json_list(allowed_platforms, "allowed_platforms")
    parsed_regions = _json_list(allowed_regions, "allowed_regions")
    parsed_prohibited = _json_list(prohibited_categories, "prohibited_categories")

    def _blank_to_none(raw: str | None) -> str | None:
        """空白的日期表单值归一成 None(= 未核实),不喂给 pydantic。

        multipart 表单没有 null:浏览器端"没填日期"发出来是 **空字符串**,
        而 `ModelTemplateForm` 的两个日期列是 `datetime | None` —— pydantic v2
        对 `''` 两个分支都不认,整个创建请求因此 422,报错还指向
        license_start_at。也就是说**不填授权日期就建不了任何模特模板**,
        而"未填"恰恰是新模板的默认状态(§11:缺省一律"未核实")。
        归一放在路由层而不是前端:表单可以来自任何客户端(curl -F 同样发 '')。
        """
        if raw is None:
            return None
        text = raw.strip()
        return text or None

    license_start_at = _blank_to_none(license_start_at)
    license_expires_at = _blank_to_none(license_expires_at)

    # 表单字段和 JSON body 一样来自浏览器,一样要过校验。
    # 直接写库的后果是 name 超长撞 VARCHAR(128) 变成 500、pose 变成界面上一个
    # 谁也不认识的标签。
    try:
        form = ModelTemplateForm(
            name=name, audience=audience, pose=pose, body_type=body_type,
            background=background, tags=parsed_tags, notes=notes,
            source=source, license_status=license_status,
            commercial_scope=commercial_scope,
            allowed_platforms=parsed_platforms,
            allowed_regions=parsed_regions,
            license_start_at=license_start_at,
            license_expires_at=license_expires_at,
            allow_ai_dressing=allow_ai_dressing,
            allow_derivative_images=allow_derivative_images,
            age_verified=age_verified,
            prohibited_categories=parsed_prohibited,
        )
    except PydanticValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(x) for x in first.get("loc", ())) or "表单"
        raise ValidationError(
            f"{field}: {first.get('msg', '取值不合法')}", code=ErrorCode.INPUT_INVALID
        ) from None

    body = form.model_dump(mode="json")
    # 合规背书要管理员。**放在写库之前**:403 之后不该有一个已经落盘的文件
    _assert_may_endorse(request, _endorsements_asserted(body))

    template = mts.create_template(
        session,
        data=data,
        filename=file.filename or "model.jpg",
        payload=body,
        storage=storage,
        actor=current_actor(request),
    )
    session.commit()
    return _out(template, storage)


@router.patch(
    "/{template_id}",
    response_model=ModelTemplateOut,
    dependencies=[Depends(require_operator)],
)
def update_template(
    template_id: UUID,
    payload: ModelTemplateUpdateForm,
    request: Request,
    session: Session = Depends(db_session),
    storage=Depends(get_storage),
) -> ModelTemplateOut:
    """改模特模板,主要是 §11 的授权字段组。

    ## 为什么这条路由必须存在

    §11 的十二个字段后端一直能收、能存,`model_license.assert_usable`
    也真的读其中四个 —— 但在此之前**没有任何写入路径**能把
    `license_status` 从 `UNVERIFIED` 改成 `LICENSED`。而 `assert_usable`
    对非 `LICENSED` 提前 return,于是那道授权闸口在真人操作的路径上
    **一次都不会触发**。§0.3 第一条底线("模特必须是已确认成年人,
    且授权覆盖本次用途")的后半句因此无法执行。

    ## 权限:描述字段 operator,合规背书字段 admin(2026-08-11 评审)

    这里签的确实是合规的字("已确认成年"、"授权覆盖本次用途"),第一版
    因此挂了 `require_admin`,后来又被改回 operator —— 理由是**那个门是假的**:
    `create` 是 operator,而它同样接收全部授权字段。同一份背书,建的时候
    不用口令、改的时候要,那么想绕开的人重建一条就行。一道能被日常动作
    绕过的闸比没有闸更糟,因为它看起来像已经管住了。

    那一版同时写下了正确解法:「授权字段该不该收归管理员」必须**两个入口
    一起回答**。这一轮就是那次回答 —— 但闸不开在整个端点上,而开在**字段组**上:

        name / pose / tags / notes / source ...   operator,不动
        §11 的十个背书字段                        admin,两个入口同一份清单

    整个端点收归 admin 是不行的:那会把"传一张模特图"也变成管理员动作,
    打断现有的素材上传动线 —— 而那正是上一版拒绝只锁 patch 的另一半理由。
    清单与判定在 `schemas/asset.LICENSE_ENDORSEMENT_FIELDS` 与
    `_assert_may_endorse()` 各**一处**。
    """
    body = payload.model_dump(mode="json", exclude_unset=True)
    if not body:
        raise ValidationError(
            "没有要修改的字段", code=ErrorCode.INPUT_INVALID
        )
    # PATCH 判的是"这次改了它没有",不是"值和默认一样不一样":`exclude_unset`
    # 已经把"没传"筛掉了,而 PATCH 里显式传一个默认值(把 LICENSED 改回
    # UNVERIFIED、把已确认的年龄抹掉)**本身就是一次背书变更**,
    # 恰恰是最需要管理员的那一种
    _assert_may_endorse(request, LICENSE_ENDORSEMENT_FIELDS & set(body))
    template = mts.update_template(
        session, template_id, body, actor=current_actor(request)
    )
    session.commit()
    return _out(template, storage)


@router.post(
    "/{template_id}/enable",
    response_model=ModelTemplateOut,
    dependencies=[Depends(require_operator)],
)
def enable_template(
    template_id: UUID, request: Request,
    session: Session = Depends(db_session), storage=Depends(get_storage),
) -> ModelTemplateOut:
    template = mts.set_enabled(session, template_id, True, actor=current_actor(request))
    session.commit()
    return _out(template, storage)


@router.post(
    "/{template_id}/disable",
    response_model=ModelTemplateOut,
    dependencies=[Depends(require_operator)],
)
def disable_template(
    template_id: UUID, request: Request,
    session: Session = Depends(db_session), storage=Depends(get_storage),
) -> ModelTemplateOut:
    template = mts.set_enabled(session, template_id, False, actor=current_actor(request))
    session.commit()
    return _out(template, storage)
