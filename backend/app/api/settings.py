"""后台设置接口(阶段 8)。

    GET    /api/settings          分组、字段、当前值与来源
    PUT    /api/settings          保存一批修改
    POST   /api/settings/reset    删掉指定项的覆盖,回到环境变量/默认值

三个接口**都**要过 ``require_admin``。读接口一开始是开放的,但它返回的东西
——每把密钥的末四位、各家的接口地址、内网下载白名单、加密是否可用——
凑在一起就是一份攻击面清单,末四位对运营是\"认出是不是我填的那把\",
对外人是纯泄露。既然这一页只给运维看,读也一并关上。

无论读写,**明文密钥都不会出现在响应里** —— 只给末位打码串。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import db_session, require_admin
from app.schemas.setting import (
    SettingsOut,
    SettingsReset,
    SettingsUpdate,
    SettingsWriteResult,
)
from app.services import settings_service

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=SettingsOut)
def read_settings(
    session: Session = Depends(db_session),
    actor: str = Depends(require_admin),
) -> dict:
    return settings_service.describe(session)


@router.put("/settings", response_model=SettingsWriteResult)
def update_settings(
    payload: SettingsUpdate,
    session: Session = Depends(db_session),
    actor: str = Depends(require_admin),
) -> dict:
    updated = settings_service.apply(session, payload.values, actor)
    session.flush()
    # 提交必须在拼这条消息**之前**。`_write_message` 要回答的正是
    # 「这次改动存住了没有」,而在自动提交那版里它是在提交发生之前
    # 就把「已保存」写进响应的 —— 提交真的失败时,运营看到的是一句
    # 已经发出去的谎
    session.commit()
    return {
        "updated": updated,
        "message": _write_message(updated),
    }


@router.post("/settings/reset", response_model=SettingsWriteResult)
def reset_settings(
    payload: SettingsReset,
    session: Session = Depends(db_session),
    actor: str = Depends(require_admin),
) -> dict:
    removed = settings_service.reset(session, payload.keys, actor)
    session.flush()
    session.commit()
    if not removed:
        return {"updated": [], "message": "这些项本来就没有后台覆盖,无需恢复"}
    return {
        "updated": removed,
        "message": f"已恢复 {len(removed)} 项为环境变量/默认值",
    }


def _write_message(updated: list[str]) -> str:
    """写完之后运营最想知道的是\"什么时候生效\",所以直说。"""
    if not updated:
        return "没有检测到改动"
    return f"已保存 {len(updated)} 项;后台立即生效,生成 worker 最迟十几秒内跟上"
