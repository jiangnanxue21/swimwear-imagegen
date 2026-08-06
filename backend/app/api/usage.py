"""付费调用花费台账接口(A23)。

    GET /api/usage/spend

一次给全:本月花费、按 provider 拆分、按天曲线、预算判定与外推。
前端不用发五个请求拼一页 —— 那样每个请求各自的加载态会让页面抖三下。

挂 `require_operator` 而不是 `require_admin`:花费是运营每天要看的东西,
「这个月还能不能跑大批量」直接影响他今天怎么排活。改预算才是管理员的事,
而那个改的是配置,走 `.env` / 设置页,不在这个接口里。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import db_session, require_operator
from app.services import spend

# 挂在路由器上而不是逐个路由上 —— 逐个挂的失败模式太安静:
# 新加一个 @router.get 忘了写依赖,不报错,它只会对全世界开放
router = APIRouter(tags=["usage"], dependencies=[Depends(require_operator)])


@router.get("/usage/spend")
def usage_spend(session: Session = Depends(db_session)) -> dict:
    """本月付费调用台账。

    注意返回里的 `disclaimer` 字段要原样显示:这是**本系统的台账**,
    不是厂商账户余额,两者必然有差额。
    """
    return spend.summary(session)
