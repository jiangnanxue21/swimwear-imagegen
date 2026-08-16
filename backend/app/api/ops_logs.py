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
    LEVEL_ORDER,
    ROUND_SUMMARY_EVENT,
    ROUTINE_GROUPS,
    folds_away,
    label_of,
    level_at_least,
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


def _shape(row: dict[str, Any], *, raw: str | None = None) -> dict[str, Any]:
    """一条日志给前端的形状。**判定全在这里,前端只展示。**

    `raw` 是这条记录在 stdout 上逐字的样子。它存在是一次态度表达:
    分类法是索引,不是转述 —— 控制台把事件码和中文标签摆在前面是为了让人
    快速定位,一旦定位到了,原文必须零成本可得,否则这套分类就变成了一层遮挡。
    """
    event = row.get("event")
    domain = row.get("domain") or resolve_domain(event, row.get("logger"))
    fields = {k: v for k, v in row.items() if k not in _PROMOTED}
    if raw is None:
        raw = json.dumps({k: v for k, v in row.items() if k != "seq"}, ensure_ascii=False)
    return {
        "seq": row.get("seq"),
        "ts": row.get("ts"),
        "level": row.get("level"),
        "logger": row.get("logger"),
        "domain": domain,
        "domain_label": DOMAINS.get(domain, domain),
        "event": event,
        "event_label": label_of(event),
        # ERROR 与 CRITICAL 永不折叠:routine 标记对它们不生效。判定放在这里
        # 而不是前端,是因为"什么时候可以藏一条日志"是一条业务规则,而前端
        # 藏错了没有任何人会发现。**判据只有 `folds_away` 这一份** —— 上一版
        # 这里写的是 `level != "ERROR"`,而 CLI 写的是 `< 40`,两边对 CRITICAL
        # 的结论正好相反。
        "routine": folds_away(event, row.get("level")),
        "routine_group": routine_group_of(event),
        # 链路模式里这条事件当段头(`── 第 1 轮 · … ──`)。给布尔而不是让前端
        # 认事件码:哪条事件是一轮的里程碑属于分类法,而前端不持有分类表。
        "round_summary": event == ROUND_SUMMARY_EVENT,
        "message": row.get("message"),
        "request_id": row.get("request_id"),
        "fields": fields,
        "raw": raw,
    }


def _matches(
    row: dict[str, Any],
    *,
    domain: str | None,
    event: str | None,
    level: str | None,
    request_id: str | None,
    task_id: str | None,
    needle: str,
    raw: str,
) -> bool:
    """一条**未成形**的记录过不过筛。

    ## 为什么筛在 `_shape` 之前(a54 改)

    上一版是先 `_shape()` 再筛,而 `_shape` 里有一次 `json.dumps` 生成 `raw`。
    也就是说:无论 `limit` 是多少,每次请求都要对**全窗 5000 条**做一遍
    loads + dumps,而跟随模式是 3 秒一拍。一个开着的标签页 ≈ 每秒
    1.7MB 的编解码,全部花在最终会被丢掉的行上。

    现在先在原始 dict 上判,只有入选的那 `limit` 条才成形。`q` 直接匹配
    LRANGE 拿回来的那一行原文 —— 它和 `_shape` 产出的 `raw` 只差一个 `seq` 键。
    """
    event_of_row = row.get("event")
    if domain and (row.get("domain") or resolve_domain(event_of_row, row.get("logger"))) != domain:
        return False
    if event and event_of_row != event:
        return False
    if not level_at_least(row.get("level"), level):
        return False
    if request_id and row.get("request_id") != request_id:
        return False
    if task_id and str(row.get("task_id") or "") != task_id:
        return False
    if needle and needle not in raw.lower():
        return False
    return True


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
    level: str | None = Query(default=None, description="最低级别,含它自己以上"),
    request_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=MAX_LIMIT),
) -> dict:
    """事件流,新在前。

    `q` 对整行原文做子串匹配;`request_id` / `task_id` 精确匹配 —— 它们是
    链路视角的入口,模糊匹配会把两条不同的链路混在一起,而那正是这一页
    最不该出错的地方。

    `level` 是**最低级别**,不是精确值。与 `tools/watch_logs.py --level`
    同一个语义:一个人在终端学会的过滤,换到页面上不用重学。上一版是精确
    匹配,于是运营选 WARNING 想找问题,ERROR 被过滤掉了 —— §1.4 那个病
    换了个地方复发。
    """
    if not settings.OPS_LOG_RING_ENABLED:
        # **关掉的环形不许画成一张空列表。**
        #
        # 上一版这里没有分支:handler 没挂,但 `/logs` 照样去读 Redis,读到空,
        # 于是界面显示"这个筛选组合下没有日志" —— 而前端那条「环形读不到」
        # 的提示只在有 `unavailable_reason` 时才渲染,`!ring.enabled` 那半句
        # 嵌在它里面,永远到不了。空列表的意思是"这段时间没发生",
        # 那正是这一页反复申明不许说的那句话。
        return {
            "items": [],
            "ring": {**_ring_meta(held_hint=0), "unavailable_reason": "ring_disabled"},
            "oldest_ts": None,
            "domain_counts": {},
        }

    rows, error = read_ring(settings.REDIS_URL, limit=settings.OPS_LOG_RING_CAP)
    meta = _ring_meta(held_hint=len(rows) if not error else None)
    if error:
        # 读不到就明说。**不许画一张空列表** —— 那等于说"这段时间没有日志",
        # 而那是一句没发生的事(硬规则第 4 条)。
        meta["unavailable_reason"] = error
        return {"items": [], "ring": meta, "oldest_ts": None, "domain_counts": {}}

    needle = (q or "").lower().strip()
    wanted_level = (level or "").upper().strip() or None

    # 域计数**不受 domain 筛选影响**,其余筛选照吃(a54 改)。
    #
    # 上一版是前端按已经筛过的那一屏算的:点进 `gen` 之后,其余十四格全是 0,
    # 恰好在最需要"别处还有没有事"的时候把这个信息拿掉了。它得在服务端算,
    # 因为服务端手上才有全窗。
    counts: dict[str, dict[str, int]] = {}
    items: list[dict[str, Any]] = []
    for row in rows:
        raw = json.dumps({k: v for k, v in row.items() if k != "seq"}, ensure_ascii=False)
        if not _matches(
            row,
            domain=None,
            event=event,
            level=wanted_level,
            request_id=request_id,
            task_id=task_id,
            needle=needle,
            raw=raw,
        ):
            continue
        key = row.get("domain") or resolve_domain(row.get("event"), row.get("logger"))
        seen = counts.setdefault(str(key), {"total": 0, "warn": 0, "error": 0})
        seen["total"] += 1
        level_name = str(row.get("level") or "").upper()
        if level_name == "WARNING":
            seen["warn"] += 1
        elif level_name in ("ERROR", "CRITICAL"):
            seen["error"] += 1
        if domain and key != domain:
            continue
        if len(items) < limit:
            items.append(_shape(row, raw=raw))

    # 窗口边界取的是**全窗**最老的一条,不是筛完之后最老的那条:
    # 它回答的是"这个环形能看到多早",与当前筛选无关。
    oldest = rows[-1].get("ts") if rows else None
    return {"items": items, "ring": meta, "oldest_ts": oldest, "domain_counts": counts}


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
        # 级别按序给,前端照序画 —— 它是"最低级别"下拉,乱序就没法读。
        "levels": [name for name, _ in sorted(LEVEL_ORDER.items(), key=lambda kv: kv[1])],
        "ring": _ring_meta(),
        "payload_capture": {
            "enabled": settings.OPS_LLM_PAYLOAD_CAPTURE,
            "ttl_seconds": settings.OPS_LLM_PAYLOAD_TTL_SECONDS,
            # 旁挂库自己的账。`/api/ops/llm/{id}` 报 404 时,这两个数回答的是
            # "是过期了,还是根本没写进去过" —— 上一版一个都没有。
            "dropped_since_boot": payload_store.STATS.snapshot()["dropped_since_boot"],
            "last_error": payload_store.STATS.snapshot()["last_error"],
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
