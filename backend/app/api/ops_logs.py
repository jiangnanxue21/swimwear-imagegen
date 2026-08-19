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
from datetime import datetime
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
    normalize_level,
    parse_ts,
    resolve_domain,
    routine_group_of,
    seq_sort_key,
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

    ## 这次 `json.dumps` 现在只对入选的 `limit` 条做(a55)

    上一版调用方在循环里无条件 dumps 全窗 5000 条,因为 `q` 匹配需要一行原文。
    现在原文由 `read_ring` 一路带下来(它本来就在手上),`q` 直接匹配那一行;
    这里这次 dumps 只为**剥掉 `seq`** —— stdout 上没有那个键,而这一块的
    承诺是"stdout 上逐字的样子"。
    """
    event = row.get("event")
    domain = row.get("domain") or resolve_domain(event, row.get("logger"))
    fields = {k: v for k, v in row.items() if k not in _PROMOTED}
    raw = json.dumps({k: v for k, v in row.items() if k != "seq"}, ensure_ascii=False)
    return {
        "seq": row.get("seq"),
        "ts": row.get("ts"),
        # 时间戳解析不出来的行不许被静默安放:它排在末尾,并且**说出来**。
        # 兜底一个时间会让它安静地落在某个位置上,而"它到底该排哪"
        # 恰恰是答不出来的那件事。
        "ts_unparsed": parse_ts(row.get("ts")) is None,
        "level": row.get("level"),
        "logger": row.get("logger"),
        # 哪个进程写的。API / worker / beat / script 全部 LPUSH 进同一个键,
        # 在此之前没有任何顶层字段区分它们(`seq` 里藏着 pid,但那是给去重用的)。
        "service": row.get("service"),
        "pid": row.get("pid"),
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
    service: str | None,
    request_id: str | None,
    task_id: str | None,
    needle: str,
    raw: str,
    since: datetime | None,
    until: datetime | None,
    stamp: datetime | None,
) -> bool:
    """一条**未成形**的记录过不过筛。

    ## 筛在 `_shape` 之前,而且这次真的省下了那笔开销(a55 改)

    a54 已经把 `_shape()` 挪到了筛选后面,并加了一条守卫。但那条守卫断言的是
    **`_matches` 的调用行号小于 `_shape` 的** —— 形状对了,而真正贵的那次
    `json.dumps` 还留在调用方的循环里,无条件对全窗 5000 条各做一遍,
    因为 `q` 匹配需要一行原文。**守卫一直是绿的,开销一次没降。**

    现在 `raw` 由 `read_ring` 从 LRANGE 一路带下来(它本来就在手上,
    上一版 `json.loads` 完就丢了),`q` 直接匹配那一行。这正是上一版
    这段文档字符串已经写着、而代码没有做到的事。

    `raw` 与 `_shape` 产出的 `raw` 只差一个 `seq` 键 —— 拿它做子串匹配,
    差别是搜一串十六进制时可能多命中 `seq`,代价可以忽略。

    ## 时间窗

    `stamp` 由调用方解析好传进来,不在这里解析:命中的行随后还要按它排序,
    解析两遍是白费;而解析不出来的行**不被时间窗挡掉** —— 挡掉等于说
    "它不在这段时间里",而真相是"不知道它在哪段时间里"。
    """
    event_of_row = row.get("event")
    if domain and (row.get("domain") or resolve_domain(event_of_row, row.get("logger"))) != domain:
        return False
    if event and event_of_row != event:
        return False
    if not level_at_least(row.get("level"), level):
        return False
    if service and str(row.get("service") or "") != service:
        return False
    if request_id and row.get("request_id") != request_id:
        return False
    if task_id and str(row.get("task_id") or "") != task_id:
        return False
    if stamp is not None:
        if since is not None and stamp < since:
            return False
        if until is not None and stamp > until:
            return False
    if needle and needle not in raw.lower():
        return False
    return True


def _parse_bound(value: str | None, name: str) -> datetime | None:
    """时间窗的一端。**解析不出来就 400,不当没填。**

    当没填的话,调用方会拿到一整窗的结果并以为"那就是那段时间里发生的全部事"
    —— 而这一页反复申明的那句话正是:界面不许说一件没发生的事。一个被
    静默忽略的时间窗说的恰好是这种话。
    """
    if not value or not value.strip():
        return None
    parsed = parse_ts(value.strip())
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail=f"{name} 不是可识别的时间。要 ISO 8601 带时区,例如 2026-08-18T09:30:00+08:00",
        )
    return parsed


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
        # 诊断窗口收不收 2xx/3xx 访问日志。**界面必须把它说出来** ——
        # 窗口可以少装东西,但不许让人把"我没收"读成"没发生"。
        "access_mode": settings.OPS_LOG_RING_ACCESS,
    }


@router.get("/logs")
def list_logs(
    domain: str | None = Query(default=None),
    event: str | None = Query(default=None),
    level: str | None = Query(default=None, description="最低级别,含它自己以上"),
    service: str | None = Query(default=None, description="哪个进程写的:api/worker/beat/script"),
    request_id: str | None = Query(default=None),
    task_id: str | None = Query(default=None),
    q: str | None = Query(default=None),
    since: str | None = Query(default=None, description="ISO 8601,含时区。这个时刻之后"),
    until: str | None = Query(default=None, description="ISO 8601,含时区。这个时刻之前"),
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
            "matched": 0,
            "truncated": False,
            "shown_oldest_ts": None,
            "services_seen": {},
        }

    rows, error = read_ring(settings.REDIS_URL, limit=settings.OPS_LOG_RING_CAP)
    # `held` 是 Redis 里真实的 LLEN,**不是解析成功的条数**。`read_ring` 的
    # 文档承诺坏行"会体现在 held 与实际条数的差里" —— 上一版这里把 held
    # 填成 len(rows),那个差恒为 0,承诺在 `/logs` 上是句空话;而 `/logs/meta`
    # 给的又是真实 LLEN:同名字段,两个端点,两种含义。多付的这一次 LLEN
    # 往返,买的是"held − items 条数 = 坏行数"这条等式真的成立。
    meta = _ring_meta()
    if error:
        # 读不到就明说。**不许画一张空列表** —— 那等于说"这段时间没有日志",
        # 而那是一句没发生的事(硬规则第 4 条)。
        meta["unavailable_reason"] = error
        return {
            "items": [],
            "ring": meta,
            "oldest_ts": None,
            "domain_counts": {},
            "matched": 0,
            "truncated": False,
            "shown_oldest_ts": None,
            "services_seen": {},
        }

    needle = (q or "").lower().strip()
    # 级别打错 = 400,不是"当没填"。与 `_parse_bound` 同一个口径,也与 CLI 对
    # `--domain`/`--event` 的口径同向:打错一个名字要当场说出来。上一版这里
    # 只做 `.upper()`,`level=WARN` 会被 `level_rank` 的未知兜底按 INFO 算 ——
    # 静默变成"不筛",整窗全回来;而那条兜底只属于**日志行自己的级别**
    # (第三方 logger 的自定义级别名),不属于筛选条件。
    try:
        wanted_level = normalize_level(level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    wanted_service = (service or "").strip() or None
    # 时间窗解析失败 = 400,不是"当没填"。当没填的话调用方会拿到一整窗的
    # 结果并以为那就是那段时间里发生的全部事 —— 又一次"界面说了没发生的话"。
    since_at = _parse_bound(since, "since")
    until_at = _parse_bound(until, "until")

    # 域计数**不受 domain 筛选影响**,其余筛选照吃(a54 改)。
    #
    # 上一版是前端按已经筛过的那一屏算的:点进 `gen` 之后,其余十四格全是 0,
    # 恰好在最需要"别处还有没有事"的时候把这个信息拿掉了。它得在服务端算,
    # 因为服务端手上才有全窗。
    counts: dict[str, dict[str, int]] = {}
    services_seen: dict[str, int] = {}
    # 命中的行先全收下来,排完序再截 —— 上一版是"边扫边填到 limit 为止",
    # 而扫描顺序是 LPUSH 到达顺序,于是截出来的既不是最新的 limit 条,
    # 也不是任何一个说得清的集合。
    picked: list[tuple[datetime | None, str, dict[str, Any]]] = []
    for raw, row in rows:
        stamp = parse_ts(row.get("ts"))
        if not _matches(
            row,
            domain=None,
            event=event,
            level=wanted_level,
            service=wanted_service,
            request_id=request_id,
            task_id=task_id,
            needle=needle,
            raw=raw,
            since=since_at,
            until=until_at,
            stamp=stamp,
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
        # 进程分布**不吃 domain 筛选**,与域计数同一个理由:它回答的是
        # "这台机器上现在有哪几个进程在写日志",而那正是"gen 域为什么空着"
        # 的答案所在 —— worker 一条都没有,就是 worker 没在写。
        who = str(row.get("service") or "?")
        services_seen[who] = services_seen.get(who, 0) + 1
        if domain and key != domain:
            continue
        # tie-break 走注册表里的 `seq_sort_key`:数值序,不是字符串序 ——
        # ts 只有秒级精度,同一秒内 "10" < "7" 的字符串比较会把同进程的
        # 第 7~10 条倒过来,而链路横幅正保证着「按时间顺读」。
        picked.append((stamp, seq_sort_key(row.get("seq")), row))

    # **按时间排序,不按到达顺序。**
    #
    # 环形是多个进程 LPUSH 进同一个键的,列表顺序是到达顺序;而链路模式的
    # 横幅上写着「按时间顺读,旧在上」。一条 API 领取、worker 执行、API 回写的
    # 任务链路,展示顺序可能是错的,**而界面正在向你保证它是对的**。
    #
    # 解析不出时间的排在末尾(`_shape` 会给它们打上 `ts_unparsed`):
    # 不静默丢,也不假装它在某个位置。
    picked.sort(key=lambda one: (one[0] is not None, one[0], one[1]), reverse=True)
    matched = len(picked)
    items = [_shape(row) for _, _, row in picked[:limit]]

    # 窗口边界取的是**全窗**最老的一条,不是筛完之后最老的那条:
    # 它回答的是"这个环形能看到多早",与当前筛选无关。
    oldest = rows[-1][1].get("ts") if rows else None
    return {
        "items": items,
        "ring": meta,
        "oldest_ts": oldest,
        # 当前这张列表实际的起点。**和 `oldest_ts` 是两个数,谁也不冒充谁** ——
        # 被 limit 截断时它比 `oldest_ts` 晚得多,而上一版界面只说了后者,
        # 于是那行"更早的不是没发生,是滚出窗口了"在此刻是在误导人。
        "shown_oldest_ts": items[-1]["ts"] if items else None,
        "domain_counts": counts,
        # 截断显形。上一版是 `if len(items) < limit: append`,超出就停,
        # 响应里没有任何字段说明这件事 —— 而 a54 刚把域计数改成按全窗算,
        # 于是左边显示 800、右边显示 200,两个数字互相矛盾且无人解释。
        "matched": matched,
        "truncated": matched > len(items),
        "services_seen": services_seen,
    }


@router.get("/logs/meta")
def logs_meta() -> dict:
    """域与事件注册表。**前端下拉的唯一来源。**

    ## `services_seen` 为什么是"窗口里数出来的",不是一张写死的表

    写死的表会在 worker 没起来的时候依然列出 `worker` —— 而"worker 在不在写
    日志"正是要发现的那件事。`LOG-CONSOLE.md` 第十二章第 1 条留下的自检顺序是
    「跑一个生成任务,看 `gen` / `batch` 域出不出条目」;有了这个数,自检
    第一步变成**打开页面看有没有 `worker`**,不用先跑任务。
    """
    services: dict[str, int] = {}
    if settings.OPS_LOG_RING_ENABLED:
        rows, error = read_ring(settings.REDIS_URL, limit=settings.OPS_LOG_RING_CAP)
        if not error:
            for _raw, row in rows:
                who = str(row.get("service") or "?")
                services[who] = services.get(who, 0) + 1
    return {
        "domains": [{"key": key, "label": label} for key, label in DOMAINS.items()],
        "services_seen": services,
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
