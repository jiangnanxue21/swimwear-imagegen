"""运行日志控制台的读接口(`docs/LOG-CONSOLE.md` §4.3 / §6.4)。

    GET /api/ops/logs         事件流(带筛选)
    GET /api/ops/logs/meta    域与事件注册表 + 环形窗口元信息
    GET /api/ops/llm/{id}     一次模型调用的完整请求与响应

## 与审计页的分工,一句话说死

    审计(/audit)    谁在什么时候改了什么 —— 合规,入库,长留
    运行(/ops/logs) 系统怎么跑的 —— 排障,环形,短留

不合并。合并的结果是运营在合规页里看见租约让位,谁都不舒服。

## 为什么走管理员闸

运行日志里有请求路径、外部 ID、商品与任务标识,以及模型调用的完整往返。
这不是"运营每天要看的东西"(那是花费台账那一类),是出事时管理员才该
打开的一扇窗。三个端点同一道闸,别开小口。

## 前端不持有任何一张分类表(硬规则第 4 条)

每一条返回都自带 `domain_label` / `event_label` / `routine` / `routine_group`,
下拉取值来自 `/meta`。`AuditLogPage` 前端持有两张翻译表是"拉到数据之前
就要能列出取值"的历史妥协,这一页不重复它 —— 因为 `/meta` 恰好就是
"拉到数据之前"能拿到的那份东西。

## 筛选在内存里做

环形 cap 才 5000,`limit` 上限 1000。一次 LRANGE 拉回来在 API 进程里过一遍
就够了,不值得更聪明的做法 —— 而更聪明的做法(Redis 侧索引)会让写路径
从一次往返变成三次,那条路径在每一次付费调用上。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import require_admin
from app.core.config import settings
from app.core.log_events import (
    DOMAINS,
    EVENTS,
    ROUTINE_GROUPS,
    label_of,
    resolve_domain,
    routine_group_of,
)
from app.core.log_ring import STATS, read_ring, ring_length
from app.llm import payload_store

router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(require_admin)])

#: 一次最多拉多少。cap 才 5000,给 1000 已经够一次"把这一段全看完"。
MAX_LIMIT = 1000

#: 不进 `fields` 的键 —— 它们已经各自有了顶层位置。
_PROMOTED = {"ts", "level", "logger", "domain", "event", "message", "request_id", "seq"}


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """一条日志给前端的形状。**判定全在这里,前端只展示。**

    `raw` 是这条记录在 stdout 上逐字的样子。它存在是一次态度表达:
    分类法是索引,不是转述 —— 控制台把事件码和中文标签摆在前面是为了让人
    快速定位,一旦定位到了,原文必须零成本可得,否则这套分类就变成了一层遮挡。
    """
    event = row.get("event")
    domain = row.get("domain") or resolve_domain(event, row.get("logger"))
    fields = {k: v for k, v in row.items() if k not in _PROMOTED}
    archive_line = {k: v for k, v in row.items() if k != "seq"}
    return {
        "seq": row.get("seq"),
        "ts": row.get("ts"),
        "level": row.get("level"),
        "logger": row.get("logger"),
        "domain": domain,
        "domain_label": DOMAINS.get(domain, domain),
        "event": event,
        "event_label": label_of(event),
        # ERROR 永不折叠:routine 标记对 ERROR 级不生效。判定放在这里而不是
        # 前端,是因为"什么时候可以藏一条日志"是一条业务规则,而前端藏错了
        # 没有任何人会发现。
        "routine": bool(routine_group_of(event)) and str(row.get("level")) != "ERROR",
        "routine_group": routine_group_of(event),
        "message": row.get("message"),
        "request_id": row.get("request_id"),
        "fields": fields,
        "raw": json.dumps(archive_line, ensure_ascii=False),
    }


def _ring_meta(held_hint: int | None = None) -> dict[str, Any]:
    """环形窗口的边界。**必须说出来。**

    查不到早于 `oldest_ts` 的记录不是"没发生",是"滚出窗口了"。少了这一段,
    运营会把一个空列表读成"那段时间系统很安静"。
    """
    stats = STATS.snapshot()
    held = ring_length(settings.REDIS_URL) if held_hint is None else held_hint
    return {
        "cap": settings.OPS_LOG_RING_CAP,
        "held": held,
        "enabled": settings.OPS_LOG_RING_ENABLED,
        "dropped_since_boot": stats["dropped_since_boot"],
        "last_error": stats["last_error"],
    }


@router.get("/logs")
def list_logs(
    domain: str | None = Query(default=None),
    event: str | None = Query(default=None),
    level: str | None = Query(default=None),
    request_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=MAX_LIMIT),
) -> dict:
    """事件流,新在前。

    `q` 对 message 与扁平化后的字段值做子串匹配;`request_id` / `task_id`
    精确匹配 —— 它们是链路视角的入口,模糊匹配会把两条不同的链路混在一起,
    而那正是这一页最不该出错的地方。
    """
    rows, error = read_ring(settings.REDIS_URL, limit=settings.OPS_LOG_RING_CAP)
    meta = _ring_meta(held_hint=len(rows) if not error else None)
    if error:
        # 读不到就明说。**不许画一张空列表** —— 那等于说"这段时间没有日志",
        # 而那是一句没发生的事(硬规则第 4 条)。
        meta["unavailable_reason"] = error
        return {"items": [], "ring": meta, "oldest_ts": None}

    wanted_level = (level or "").upper().strip()
    needle = (q or "").lower().strip()
    items: list[dict[str, Any]] = []
    for row in rows:
        shaped = _shape(row)
        if domain and shaped["domain"] != domain:
            continue
        if event and shaped["event"] != event:
            continue
        if wanted_level and str(shaped["level"] or "").upper() != wanted_level:
            continue
        if request_id and shaped["request_id"] != request_id:
            continue
        if task_id and str(shaped["fields"].get("task_id") or "") != task_id:
            continue
        if needle and needle not in shaped["raw"].lower():
            continue
        items.append(shaped)
        if len(items) >= limit:
            break

    # 窗口边界取的是**全窗**最老的一条,不是筛完之后最老的那条:
    # 它回答的是"这个环形能看到多早",与当前筛选无关。
    oldest = rows[-1].get("ts") if rows else None
    return {"items": items, "ring": meta, "oldest_ts": oldest}


@router.get("/logs/meta")
def logs_meta() -> dict:
    """域与事件注册表。**前端下拉的唯一来源。**"""
    return {
        "domains": [{"key": key, "label": label} for key, label in DOMAINS.items()],
        "events": [
            {
                "key": e.key,
                "label": e.label,
                "domain": e.domain,
                "routine": e.routine,
                "routine_group": ROUTINE_GROUPS.get(e.routine_group or "", None),
            }
            for e in EVENTS.values()
        ],
        "routine_groups": [{"key": k, "label": v} for k, v in ROUTINE_GROUPS.items()],
        "levels": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        "ring": _ring_meta(),
        "payload_capture": {
            "enabled": settings.OPS_LLM_PAYLOAD_CAPTURE,
            "ttl_seconds": settings.OPS_LLM_PAYLOAD_TTL_SECONDS,
        },
    }


@router.get("/llm/{llm_call_id}")
def llm_payload(llm_call_id: str) -> dict:
    """一次模型调用的完整请求与响应(脱敏后)。

    ## 404 有两种,必须分开说

        没开捕获   `OPS_LLM_PAYLOAD_CAPTURE=false` —— 去改配置
        已过期     超过 TTL 或 Redis 重启 —— 这次查不到了,下次能查到

    一个空面板说不清是哪一种,而它们的下一步完全相反。
    """
    if not settings.OPS_LLM_PAYLOAD_CAPTURE:
        raise HTTPException(
            status_code=404,
            detail=(
                "载荷捕获没有开启(OPS_LLM_PAYLOAD_CAPTURE=false)。"
                "这不是「这次没记到」,是这台机器一直没在记 —— "
                "打开它之后,新的调用才会有留痕"
            ),
        )
    found = payload_store.load(llm_call_id)
    if found is None:
        hours = max(1, settings.OPS_LLM_PAYLOAD_TTL_SECONDS // 3600)
        raise HTTPException(
            status_code=404,
            detail=(
                f"载荷已过期或未捕获。旁挂库只保留 {hours} 小时,"
                "Redis 重启也会清空 —— 更早的调用请查 stdout 采集端"
            ),
        )
    found["ttl_seconds"] = settings.OPS_LLM_PAYLOAD_TTL_SECONDS
    return found
