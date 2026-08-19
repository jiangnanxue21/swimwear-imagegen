"""提示词模板的读写与版本切换。

和设置页一样,**读写都要过 ``require_admin``**:提示词决定评分口径,
能改它的人等于能改"什么图算合格"。这比改一个超时值的权重大得多。

保存不做内容拦截 —— 已经明确要完全开放全文编辑。但会把体检结果一并返回,
让编辑的人当场看到"你这版没提 JSON""你删光了硬错误代码"之类的提示。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import db_session, require_admin
from app.core.enums import AuditAction
from app.core.errors import NotFoundError
from app.prompts import registry
from app.services import audit, prompt_service

router = APIRouter(tags=["prompts"])


class PromptSaveIn(BaseModel):
    """保存提示词的入参。

    **没有 `updated_by`**(报告 6.11)。它原来在这里,取自请求体 ——
    也就是说任何拿到管理口令的人都能把一次改动署名成别人。而提示词决定
    "什么图算合格",它的变更记录是评分口径出问题时唯一能回溯的东西。
    署名一律用 `require_admin` 验证过的身份,和审计里的 actor 同源。
    """

    content: str = Field(..., max_length=prompt_service.MAX_PROMPT_CHARS * 2)
    note: str | None = Field(None, max_length=500)
    #: 调用方以为的当前生效版本(FE-309)。`None` = 不参与检查,留给脚本与内部调用;
    #: 页面**必须**传 —— 不传的话 A 保存 v5、B 停在 v4 上点保存会直接落 v6,
    #: A 的改动无声消失。这一列不做成必填,是因为把既有脚本一起打断的代价更大
    expected_version: int | None = Field(None, ge=0)


class PromptActivateIn(BaseModel):
    version: int = Field(..., ge=1)
    #: 为什么退回来(FE-310)。保存有 note 而回滚没有 —— 三个月后看到 v3 生效、
    #: v5 存在,没人知道原因,而回滚连内容 diff 都猜不出来
    note: str | None = Field(None, max_length=500)
    expected_version: int | None = Field(None, ge=0)


class PromptResetIn(BaseModel):
    """恢复默认也要理由与乐观锁(FE-310 / AC-35)。

    这个端点原来**连请求体都没有**。三个动作里它是破坏性最强的一个
    (一次把所有版本停用),却是唯一不必解释自己的。
    """

    note: str | None = Field(None, max_length=500)
    expected_version: int | None = Field(None, ge=0)


def _conflict(exc: prompt_service.PromptConflict) -> HTTPException:
    """把版本冲突翻成一条界面说得出口的 409(FE-309)。

    **只回一句「版本冲突」是不够的。** 运营此刻要判断的是"我该放弃还是该合并",
    而那要知道对方是谁、什么时候、把它改成了第几版。少了这三样,唯一可行的
    动作是刷新页面重来一遍 —— 而他刚写的那段就此没了,和静默覆盖只差一个提示框。

    `actual` 为 None 表示对方把它恢复成内置默认了。这一支单独说,不然
    「现在是第 None 版」会原样出现在界面上。
    """
    if exc.actual is None:
        moved = "已被恢复成内置默认"
    else:
        moved = f"已经是第 {exc.actual} 版"
    who = exc.updated_by or "另一位管理员"
    when = f",时间 {exc.updated_at}" if exc.updated_at else ""
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "code": "PROMPT_VERSION_CONFLICT",
                "message": (
                    f"这条提示词{moved}(由 {who} 保存{when})。"
                    "你看到的还是更早的一版 —— 先看看对方改了什么,再决定要不要覆盖。"
                ),
                "expected_version": exc.expected,
                "actual_version": exc.actual,
                "updated_by": exc.updated_by,
            }
        },
    )


@router.get("/prompts")
def list_prompts(session: Session = Depends(db_session)) -> dict:
    """注册表全量(PRD BE-302):8 处提示词的 key、层、可编辑性、消费方。

    「可编辑」= 消费链路会读库,改了会生效;详见 `registry` 模块文档 ——
    对其余 6 处这里如实标 False,而不是让保存成功但毫无效果。
    editable 的项附上当前生效版本号。

    **调用统计(FE-301 的近 7 天列)仍然没有,但推迟的理由已经不是原来那个。**
    原文写的是「待测试留档落地后一并做」—— 而留档 a57 就落了、读接口 a62 也落了,
    这个前提在 a62 就消失了,注释却又躺了六轮(§3.33:过期的理由比没有理由更糟)。

    现在的实情是:数据齐了,做得了 —— `ai_test_runs` 按 `prompt_key` +
    `prompt_template_version` 聚合即可,而两列的语义 a63 才理顺。**没做的原因
    只剩"还没排"**,不是"还差什么"。它连着 FE-301 的那一列一起,记在 PRD §16 的
    S3 那格,别再写成依赖问题。
    """
    surfaces = []
    for surface in registry.PROMPT_SURFACES:
        item = {
            "key": surface.key,
            "label": surface.label,
            "tier": surface.tier.value,
            "editable": surface.editable,
            "ui_reachable": surface.ui_reachable,
            "required_slots": list(surface.required_slots),
            "consumers": list(surface.consumers),
            "has_static_default": surface.default_ref is not None,
        }
        if surface.editable:
            _content, version = prompt_service.get_active_content(session, surface.key)
            item["active_version"] = version  # None = 内置默认生效中
        surfaces.append(item)
    return {"surfaces": surfaces}


@router.get("/prompts/{key}/versions/{version}")
def read_prompt_version(
    key: str,
    version: int,
    session: Session = Depends(db_session),
) -> dict:
    """单版本**只读**正文(PRD BE-303)。看一眼不等于让它生效。"""
    row = prompt_service.get_version(session, key, version)
    if row is None:
        raise NotFoundError(f"提示词 {key} 没有第 {version} 版")
    return {
        "key": key,
        "version": row.version,
        "content": row.content,
        "is_active": row.is_active,
        "note": row.note,
        "updated_by": row.updated_by,
    }


@router.get("/prompts/{key}")
def read_prompt(
    key: str,
    session: Session = Depends(db_session),
    actor: str = Depends(require_admin),
) -> dict:
    if key not in prompt_service.KNOWN_KEYS:
        raise NotFoundError(f"未知的提示词用途:{key}")
    return prompt_service.describe(session, key)


@router.post("/prompts/{key}/preview")
def preview_prompt(
    key: str,
    body: PromptSaveIn,
    actor: str = Depends(require_admin),
) -> dict:
    """存之前先体检一遍,不写库。

    单独给一个接口是因为"保存"和"看看这版有没有问题"是两件事 ——
    没人愿意为了看一眼警告就先把它设为生效版本。
    """
    if key not in prompt_service.KNOWN_KEYS:
        raise NotFoundError(f"未知的提示词用途:{key}")
    warnings = prompt_service.lint_prompt(key, body.content)
    return {
        "key": key,
        "chars": len(body.content),
        "warnings": [{"code": w.code, "message": w.message} for w in warnings],
    }


@router.put("/prompts/{key}")
def save_prompt(
    key: str,
    body: PromptSaveIn,
    session: Session = Depends(db_session),
    actor: str = Depends(require_admin),
) -> dict:
    # 只有消费链路会读库的 key 才许落库(registry.editable 的语义)。对其余
    # 6 处放行的结果是「保存成功、毫无效果」—— 比 PRD 问题 A 那条"后端通、
    # 前端不可达"的死路更糟,因为它连报错都没有。
    if key not in registry.editable_keys():
        surface = registry.surface_of(key)
        raise HTTPException(
            status_code=400,
            detail=(
                f"{key} 当前不可编辑:它的消费链路不读库,保存不会产生任何效果。"
                + ("原因见注册表:" + surface.consumers[-1] if surface and surface.consumers else "")
            ),
        )
    if key not in prompt_service.KNOWN_KEYS:
        raise NotFoundError(f"未知的提示词用途:{key}")

    try:
        row = prompt_service.save_version(
            session,
            key,
            body.content,
            note=body.note,
            updated_by=actor,
            expected_version=body.expected_version,
        )
    except prompt_service.PromptConflict as exc:
        raise _conflict(exc) from None
    # 提示词变更进统一审计(报告 6.11)。原来只有服务层一条 logger.info ——
    # 日志是滚动清理的,而"三个月前谁把评分提示词改成了这样"要能查得到。
    # 不记 content:它可能上万字,而版本表里已经完整存着,这里记版本号即可
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="PromptTemplate",
        entity_id=row.id,
        payload={
            "action": "save",
            "key": key,
            "version": row.version,
            "chars": len(body.content),
            "note": body.note,
        },
    )
    session.commit()
    result = prompt_service.describe(session, key)
    result["saved_version"] = row.version
    return result


@router.post("/prompts/{key}/activate")
def activate_prompt(
    key: str,
    body: PromptActivateIn,
    session: Session = Depends(db_session),
    actor: str = Depends(require_admin),
) -> dict:
    if key not in prompt_service.KNOWN_KEYS:
        raise NotFoundError(f"未知的提示词用途:{key}")
    try:
        row = prompt_service.activate_version(
            session,
            key,
            body.version,
            note=body.note,
            updated_by=actor,
            expected_version=body.expected_version,
        )
    except LookupError as exc:
        raise NotFoundError(str(exc)) from None
    except prompt_service.PromptConflict as exc:
        raise _conflict(exc) from None
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="PromptTemplate",
        entity_id=row.id,
        payload={
            "action": "activate",
            "key": key,
            "version": body.version,
            # 回滚的理由进审计。版本行上那条是给读页面的人看的,
            # 这一条是给三个月后查"谁在什么时候退回去的"的人看的
            "note": body.note,
        },
    )
    session.commit()
    return prompt_service.describe(session, key)


@router.post("/prompts/{key}/reset")
def reset_prompt(
    key: str,
    body: PromptResetIn | None = None,
    session: Session = Depends(db_session),
    actor: str = Depends(require_admin),
) -> dict:
    """回到代码内置的默认提示词(把所有版本停用,历史仍然留着)。"""
    if key not in prompt_service.KNOWN_KEYS:
        raise NotFoundError(f"未知的提示词用途:{key}")
    payload = body or PromptResetIn()
    try:
        prompt_service.reset_to_default(
            session,
            key,
            updated_by=actor,
            note=payload.note,
            expected_version=payload.expected_version,
        )
    except prompt_service.PromptConflict as exc:
        raise _conflict(exc) from None
    # 恢复默认是"把所有版本停用",库里因此**没有**一条新行标记这次事件
    # (报告 6.11)。审计是唯一记着它的地方 —— 否则页面上只会显示
    # "当前用的是内置默认",而不知道是谁、什么时候切回去的
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="PromptTemplate",
        entity_id=None,
        payload={"action": "reset_to_default", "key": key},
    )
    session.commit()
    return prompt_service.describe(session, key)
