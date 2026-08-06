"""提示词模板的读写与版本切换。

和设置页一样,**读写都要过 ``require_admin``**:提示词决定评分口径,
能改它的人等于能改"什么图算合格"。这比改一个超时值的权重大得多。

保存不做内容拦截 —— 已经明确要完全开放全文编辑。但会把体检结果一并返回,
让编辑的人当场看到"你这版没提 JSON""你删光了硬错误代码"之类的提示。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import db_session, require_admin
from app.core.enums import AuditAction
from app.core.errors import NotFoundError
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


class PromptActivateIn(BaseModel):
    version: int = Field(..., ge=1)


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
    if key not in prompt_service.KNOWN_KEYS:
        raise NotFoundError(f"未知的提示词用途:{key}")

    row = prompt_service.save_version(
        session,
        key,
        body.content,
        note=body.note,
        updated_by=actor,
    )
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
        row = prompt_service.activate_version(session, key, body.version)
    except LookupError as exc:
        raise NotFoundError(str(exc)) from None
    audit.record(
        session,
        actor=actor,
        action=AuditAction.UPDATE,
        entity_type="PromptTemplate",
        entity_id=row.id,
        payload={"action": "activate", "key": key, "version": body.version},
    )
    session.commit()
    return prompt_service.describe(session, key)


@router.post("/prompts/{key}/reset")
def reset_prompt(
    key: str,
    session: Session = Depends(db_session),
    actor: str = Depends(require_admin),
) -> dict:
    """回到代码内置的默认提示词(把所有版本停用,历史仍然留着)。"""
    if key not in prompt_service.KNOWN_KEYS:
        raise NotFoundError(f"未知的提示词用途:{key}")
    prompt_service.reset_to_default(session, key, updated_by=actor)
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
