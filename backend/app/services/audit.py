"""审计写入。需求第十九章:写操作必须记录审计日志。"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.enums import AuditAction
from app.core.json_safe import json_safe
from app.core.logging import redact, request_id_var
from app.models.audit_log import AuditLog


def record(
    session: Session,
    *,
    actor: str,
    action: AuditAction,
    entity_type: str,
    entity_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditLog:
    """写一条审计。payload 先脱敏、再 JSON 安全化,然后才落库。

    JSON 安全化(`core/json_safe.py`)不是防御性装饰:JSONB 列的默认序列化器
    就是 `json.dumps`,一个 `datetime` 混进来,整笔业务事务在 commit 时回滚。
    A45 batch10 的授权到期日 PATCH 就是第一条这样的路径 —— 修在这里,
    之后任何调用方把日期 / UUID / 枚举放进 payload 都不再是一次事故。
    """
    entry = AuditLog(
        actor=actor or "system",
        action=action.value,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=request_id_var.get(),
        payload=json_safe(redact(payload or {})),  # type: ignore[arg-type]
    )
    session.add(entry)
    return entry
