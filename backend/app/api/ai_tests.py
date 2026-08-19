"""AI 测试留档的读接口(PRD §12.5 / BE-311)。

## 为什么是管理员闸

留档里带着模型的完整输出与提示词版本。它和设置页共用 `require_admin` ——
PRD §14.4 那条待决(要不要独立权限位)本轮仍然不做,当前口径写在那里。

## 判定不在接口层

过滤参数的归一在 `workflows/ai_test_query.py`(零依赖,可穷举);这里只做
取数、调判定、持有事务。与 `api/publish.py` 那条同一个理由:覆盖
「某个参数组合下不该放行」如果要起一个 FastAPI 加一个库,那种测试没人会写。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import db_session, require_admin
from app.core.clock import iso_utc
from app.models.ai_test_run import AiTestRun
from app.workflows import ai_test_query

router = APIRouter(tags=["ai-tests"])


@router.get("/ai-tests/runs")
def list_ai_test_runs(
    kind: str | None = Query(None),
    prompt_key: str | None = Query(None),
    prompt_version: str | None = Query(None),
    prompt_template_version: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    limit: int | None = Query(None),
    offset: int | None = Query(None),
    session: Session = Depends(db_session),
    actor: str = Depends(require_admin),
) -> dict[str, Any]:
    """按类型 / 提示词 / 版本 / 时间窗查留档。

    **不返回总数。** 数一张带全文的表要再扫一遍,而这一页的用途是"最近跑了什么"
    与"这一版跑过哪些",两者都不需要总数。给 `has_more` —— 它由多取一条判出来,
    代价是一行,而"还有没有下一页"正是翻页唯一要回答的问题。
    """
    try:
        query = ai_test_query.build_query(
            kind=kind,
            prompt_key=prompt_key,
            prompt_version=prompt_version,
            prompt_template_version=prompt_template_version,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
    except ai_test_query.FilterRejected as exc:
        # 400 而不是静默返回全量。认不出的取值静默变成"不筛"时,运营会把
        # 那一屏读成「这段时间只有这些」—— §3.88 修过同一个形状
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INPUT_INVALID", "message": str(exc)}},
        ) from None

    stmt = select(AiTestRun)
    if query.kind:
        stmt = stmt.where(AiTestRun.kind == query.kind)
    if query.prompt_key:
        stmt = stmt.where(AiTestRun.prompt_key == query.prompt_key)
    if query.prompt_version:
        stmt = stmt.where(AiTestRun.prompt_version == query.prompt_version)
    if query.prompt_template_version is not None:
        # FE-314 的互链走这一列。评分链路落序号、文案落哈希,两者形状不同 ——
        # 按 `prompt_version` 查评分那两份必然对不上(§3.96)
        stmt = stmt.where(
            AiTestRun.prompt_template_version == query.prompt_template_version
        )
    if query.since:
        stmt = stmt.where(AiTestRun.created_at >= query.since)
    if query.until:
        stmt = stmt.where(AiTestRun.created_at <= query.until)

    # 多取一条判 has_more。总数要再扫一遍全表,而这一页不需要总数
    rows = list(
        session.scalars(
            stmt.order_by(AiTestRun.created_at.desc())
            .offset(query.offset)
            .limit(query.limit + 1)
        )
    )
    has_more = len(rows) > query.limit
    return {
        "runs": [
            ai_test_query.serialize_run(row, iso=iso_utc) for row in rows[: query.limit]
        ],
        "has_more": has_more,
        # 生效的每页条数如实报出来:调用方传了 100000 而后端收到 100,
        # 不说的话它会以为自己拿到了全部
        "limit": query.limit,
        "offset": query.offset,
        "max_limit": ai_test_query.MAX_PAGE_SIZE,
    }
