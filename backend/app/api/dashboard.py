"""仪表盘接口(需求第十六章)。

    GET /api/dashboard/summary
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import db_session, require_operator
from app.services import dashboard_service

# 整个路由挂 require_operator(读接口也要)。为什么挂在路由器上见 `deps.require_operator`
router = APIRouter(tags=["dashboard"], dependencies=[Depends(require_operator)])


@router.get("/dashboard/summary")
def dashboard_summary(session: Session = Depends(db_session)) -> dict:
    """后台首页的全部数字。一次请求给全,前端不用发十个请求拼。"""
    return dashboard_service.summary(session)
