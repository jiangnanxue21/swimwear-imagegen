"""模型载荷旁挂库(`docs/LOG-CONSOLE.md` §6)。

## 为什么载荷不和事件走同一条路

事件流要**短、快、能全量扫**;载荷要**完整、有独立寿命、按需取**。塞进同一条路
就只能二选一:要么载荷压垮事件流(5000 条的环形装几十条就满),要么事件流
挤掉载荷 —— 后者正是今天的默认关。

所以两个去向、两个开关、两套寿命:

    事件      环形缓冲 + stdout     无开关          一直记      环形 5000 条
    载荷诊断  ops:llm:{call_id}     OPS_LLM_PAYLOAD_CAPTURE  默认开   TTL 24h
    载荷归档  stdout 日志行         LLM_LOG_PAYLOADS         默认关   采集方定

**第三行逐字维持现状。** 那条决策的理由(归档面不该躺着一份受授权约束的
商品数据)现在依然成立,本模块不碰它。新增的是第二行:一个有 TTL、有管理员闸、
不出本机 Redis 的诊断窗口。风险面不同,所以默认值可以不同。

## 为什么非要有它

「打开开关重跑一遍」对模型问题基本无效 —— 值得排查的失败恰恰是**不可复现**
的那些:偶发的格式跑偏、偶发的限流、某张图触发的拒答。等你开了开关,它不来了。
所以留痕必须是默认行为,而"默认留"能成立的前提就是它不进归档面。

还有一处更隐蔽的:响应侧真正需要原文的时刻,恰恰是响应**不是 JSON** 的时刻 ——
上游返回网关的 HTML 错误页、返回一段散文、返回被截断的 JSON。那时摘要字段里
只剩 `http_status` 和 `content_type`,`finish_reason` 和 `usage` 全是空。

## 三条边界

1. **复用 `safe_payload_for_log`,一行不改。** 图片永远只留 MIME + 字符数 +
   sha256_16,连旁挂库也不存正文。那条规矩不是"归档面才需要的谨慎"。
   `tests/pure/test_a53_log_console.py` 用 AST 钉着这一点:写进旁挂库的实参
   必须是那个函数的返回值,不许把 `request.body` 直接递进来。
2. **字符串上限单独放宽**(`OPS_PAYLOAD_STRING_CHARS`),理由见 `redaction.py`。
3. **截断必须显形。** `truncated` 列出被截的字段路径,界面照着画。

## 为什么是 hash 而不是 SETEX

设计文档写的是 SETEX。改成 `HSET + EXPIRE` 是因为一次调用要写多次:一次请求
加最多 N 次尝试。SETEX 意味着每次尝试都要"读回来、改一改、整个写回去" ——
三次重试就是三次读 + 三次写,而其中每一次都在付费调用的热路径上。
hash 是同一件事的一次往返版本,TTL 语义完全一样。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.core.log_ring import RingStats, get_client, note_failure
from app.llm.redaction import safe_payload_for_log, was_truncated

logger = logging.getLogger(__name__)

#: 键前缀。`{call_id}` 就是日志里那个 `llm_call_id` —— 界面上点一下
#: `call c41f8a09d2e3` 芯片,取的就是这个键。
KEY_PREFIX = "ops:llm:"

#: 旁挂库自己的账(a54 补)。
#:
#: 上一版 `_write` 的 `except` 直接 `return`:既不记数也不进冷却期。
#: 环形那边有 `dropped_since_boot`,口径写着"不赌,但也不瞎";旁挂库这边是
#: 纯瞎 —— `/api/ops/llm/{id}` 报 404 的时候,分不清是"过期了"还是
#: "从来就没写进去过",而这两者的下一步完全不同。
STATS = RingStats()

#: `llm_call_id` 的缺省值(`transport._llm_call_id` 的 default)。
#:
#: 它不是一个 id,是"这条路径没有 id"。上一版 `_write` 只挡空串,于是
#: `evaluators/vision.py` 的连接自检(它直接调 `_send_once`,绕过
#: `_send_with_retries`)会把响应写进 `ops:llm:-` 的 `attempt:0`,
#: 每点一次"测试连接"覆盖一次,还顺手给这个垃圾键续一次 TTL。
NO_CALL_ID = "-"

_FIELD_REQUEST = "request"
_FIELD_META = "meta"
_ATTEMPT_PREFIX = "attempt:"


def key_for(call_id: str) -> str:
    return f"{KEY_PREFIX}{call_id}"


def _settings() -> Any | None:
    try:
        from app.core.config import settings

        return settings
    except Exception:  # noqa: BLE001 - 裁剪环境仍要能导入传输层
        return None


def capture_enabled() -> bool:
    settings = _settings()
    return bool(settings is not None and settings.OPS_LLM_PAYLOAD_CAPTURE)


def has_call_id(call_id: str | None) -> bool:
    """这条路径有没有真的 call id。`"-"` 不是 id,是「没有」。"""
    return bool(call_id) and call_id != NO_CALL_ID


def _string_limit() -> int:
    settings = _settings()
    return int(getattr(settings, "OPS_PAYLOAD_STRING_CHARS", 40_000) or 40_000)


def _max_bytes() -> int:
    settings = _settings()
    return int(getattr(settings, "OPS_LLM_PAYLOAD_MAX_BYTES", 262_144) or 262_144)


def _ttl() -> int:
    settings = _settings()
    return int(getattr(settings, "OPS_LLM_PAYLOAD_TTL_SECONDS", 86_400) or 86_400)


def redact(value: Any) -> Any:
    """旁挂库的**唯一**入口脱敏。宽一档的字符串上限在这里生效。"""
    return safe_payload_for_log(value, max_string_chars=_string_limit())


def truncated_paths(value: Any, prefix: str = "") -> list[str]:
    """哪些字段被截断了。深度遍历,路径写成 `request.system` 这样。"""
    found: list[str] = []
    if was_truncated(value):
        return [prefix or "<root>"]
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(truncated_paths(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(truncated_paths(item, f"{prefix}[{index}]"))
    return found


def _fit(payload: dict[str, Any]) -> dict[str, Any]:
    """把一条记录压进 `OPS_LLM_PAYLOAD_MAX_BYTES`,**并把砍过的路径记进 `truncated`**。

    超了就从**最长的字符串**开始砍,而不是整条丢掉 —— 丢掉的那条恰恰是
    "响应特别大"的那一次,而那多半就是要查的那一次。

    ## 为什么 `truncated` 要在这里补(a54 修)

    上一版的顺序是:`redact()` -> `truncated_paths()` -> `_write()` -> `_fit()`。
    也就是说 `_fit` 砍掉的那些字符串,是在 `truncated` 算完**之后**才发生的,
    一条都没进那张表。而 256KB 这一档恰恰砍得最狠 —— 界面于是在最该说
    「你看到的不是全文」的那一次保持沉默,而设计 §6.2 的第三条边界写的是
    **截断必须显形**。
    """
    limit = _max_bytes()
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= limit:
        return payload

    # 找出所有字符串叶子,按长度倒序砍到一半,直到装得下。
    def leaves(node: Any, path: tuple[Any, ...]) -> list[tuple[tuple[Any, ...], int]]:
        if isinstance(node, str):
            return [(path, len(node))]
        if isinstance(node, dict):
            return [x for k, v in node.items() for x in leaves(v, (*path, k))]
        if isinstance(node, list):
            return [x for i, v in enumerate(node) for x in leaves(v, (*path, i))]
        return []

    def cut(node: Any, path: tuple[Any, ...]) -> None:
        target = node
        for step in path[:-1]:
            target = target[step]
        text = target[path[-1]]
        keep = max(200, len(text) // 2)
        from app.llm.redaction import TRUNCATION_MARK

        target[path[-1]] = text[:keep] + TRUNCATION_MARK.format(omitted=len(text) - keep)

    def label(path: tuple[Any, ...]) -> str:
        out = ""
        for step in path:
            if isinstance(step, int):
                out = f"{out}[{step}]"
            else:
                out = f"{out}.{step}" if out else str(step)
        return out or "<root>"

    cut_here: list[str] = []
    for _ in range(40):
        ranked = sorted(leaves(payload, ()), key=lambda item: -item[1])
        if not ranked or ranked[0][1] <= 200:
            break
        cut(payload, ranked[0][0])
        cut_here.append(label(ranked[0][0]))
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= limit:
            break
    if cut_here:
        listed = payload.get("truncated")
        listed = list(listed) if isinstance(listed, list) else []
        # `truncated` 自己也是一个字符串叶子的容器,砍到它头上没有意义,滤掉。
        payload["truncated"] = listed + [
            one for one in cut_here if one not in listed and not one.startswith("truncated")
        ]
    return payload


def _write(call_id: str, field: str, value: dict[str, Any]) -> None:
    """写一条字段。**fire-and-forget** —— 与 RingHandler 同款(§4.2)。

    任何异常静默吞掉:排障留痕失败不该让一次真的付费调用跟着失败。
    这里不打日志,理由和 RingHandler 一样 —— 这条路径在日志系统内部,
    打日志会绕回来。
    """
    if not has_call_id(call_id) or not capture_enabled():
        return
    settings = _settings()
    if settings is None:
        return
    try:
        client = get_client(settings.REDIS_URL)  # 与环形共用那个带冷却期的客户端
        if client is None:
            STATS.drop()
            return
        key = key_for(call_id)
        pipe = client.pipeline(transaction=False)
        pipe.hset(key, field, json.dumps(_fit(value), ensure_ascii=False))
        pipe.expire(key, _ttl())
        pipe.execute()
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        # 静默对**业务**,不对运维:计数进 STATS,并让共享客户端进冷却期 ——
        # 少了后者,Redis 挂掉时每一次付费调用都要白付 0.2 秒。
        note_failure(exc)
        STATS.drop(exc)
        return
    STATS.ok()


def capture_request(
    call_id: str,
    *,
    provider: str,
    model: str,
    endpoint: str,
    headers: dict[str, str],
    body: Any,
    request_sha256_16: str,
    request_body_bytes: int,
    images: list[dict[str, Any]] | None = None,
) -> None:
    """留一次调用发出去的那一份。

    ``request_sha256_16`` 是可核对性的锚点(§6.3):它是 `canonical_json_bytes()`
    算出来的、**真正发出去的那串字节**的摘要。界面把它和脱敏视图并排显示,
    配一行小字「你看到的是脱敏视图,哈希对应的是发出去的原始字节」——
    于是"为什么图片是一行 sha 而不是图"有答案,而不是让人怀疑控制台在藏东西。
    """
    if not capture_enabled() or not has_call_id(call_id):
        return
    payload = {
        "endpoint": redact(endpoint),
        "headers": redact(headers),
        "body": redact(body),
        "images": images or [],
        "sha256_16": request_sha256_16,
        "body_bytes": request_body_bytes,
    }
    payload["truncated"] = truncated_paths(payload)
    _write(call_id, _FIELD_REQUEST, payload)
    _write(
        call_id,
        _FIELD_META,
        {"provider": provider, "model": model, "ttl_seconds": _ttl()},
    )


def capture_attempt(
    call_id: str,
    *,
    attempt: int,
    http_status: int,
    duration_ms: int | None,
    content_type: str | None,
    upstream_request_id: str | None,
    body: Any,
) -> None:
    """留第 N 次尝试收回来的那一份。

    **多次尝试各存各的,不覆盖。** 「重试后成功了但结果不一样」「第一次是 429
    第二次是 200」这类问题,只有把两次尝试摆在同一个切换器里才看得出来 ——
    覆盖式写入会让第一次的现场消失,而第一次恰恰是出问题的那一次。
    """
    if not capture_enabled() or not has_call_id(call_id):
        return
    payload = {
        "attempt": attempt,
        "http_status": http_status,
        "duration_ms": duration_ms,
        "content_type": content_type,
        "upstream_request_id": upstream_request_id,
        "body": redact(body),
    }
    payload["truncated"] = truncated_paths(payload)
    _write(call_id, f"{_ATTEMPT_PREFIX}{attempt}", payload)


def load(call_id: str) -> dict[str, Any] | None:
    """取一次调用的完整留痕。取不到返回 None。

    **"没开捕获"和"已过期"是两种情况**,界面要分开说 —— 这里只回答
    "有没有",由调用方(`api/ops_logs.py`)根据开关状态选措辞。
    一个空面板说不清是哪一种,而它们的下一步完全不同。
    """
    settings = _settings()
    if settings is None or not has_call_id(call_id):
        return None
    try:
        client = get_client(settings.REDIS_URL)
        if client is None:
            return None
        raw = client.hgetall(key_for(call_id))
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None

    meta: dict[str, Any] = {}
    request: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []
    for field, value in raw.items():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            continue
        if field == _FIELD_META:
            meta = parsed
        elif field == _FIELD_REQUEST:
            request = parsed
        elif str(field).startswith(_ATTEMPT_PREFIX):
            attempts.append(parsed)

    attempts.sort(key=lambda a: a.get("attempt") or 0)
    truncated = list((request or {}).get("truncated") or [])
    for one in attempts:
        truncated.extend(f"attempts[{one.get('attempt')}].{p}" for p in one.get("truncated") or [])
    return {
        "llm_call_id": call_id,
        "provider": meta.get("provider"),
        "model": meta.get("model"),
        "request": request,
        "attempts": attempts,
        "truncated": truncated,
    }
