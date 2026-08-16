"""日志脱敏(M2)。

规矩很短:**请求摘要里不出现 API Key、图片字节、base64、完整请求体。**

可以出现的是:URL、模型名、图片张数、内联张数、HTTP 状态码、
响应 ID、token 用量、finish reason。

为什么要单独一个模块:这条规矩在评分层已经守住了,但识别层(M3)会
写自己的日志。规矩写在注释里靠人记,写成函数才能被复用和被测试。
"""
from __future__ import annotations

import re
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit, urlunsplit

#: 摘要里允许出现的 body 字段。白名单,不是黑名单 ——
#: 黑名单漏一个就是把 base64 图片写进日志,而那种日志通常还会被采集到别处。
SAFE_BODY_KEYS: frozenset[str] = frozenset({"model", "max_output_tokens", "temperature"})

_SECRET_KEY = re.compile(
    r"(api[_-]?key|secret|password|(?:^|[_-])token(?:$|[_-])|authorization|credential|cookie)",
    re.IGNORECASE,
)
_DATA_URL = re.compile(r"^data:([^;,]+)(?:;[^,]*)?;base64,(.*)$", re.DOTALL)
_EMBEDDED_DATA_URL = re.compile(
    r"data:([^;,\s]+)(?:;[^,\s]*)?;base64,([A-Za-z0-9+/=_-]+)", re.IGNORECASE
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
MAX_LOG_STRING_CHARS = 12_000
MAX_LOG_ITEMS = 100

#: 截断标记。**必须显形** —— 界面要能画出"这里被截了",不能让人误以为
#: 看到的是全文。做成常量是因为旁挂库要反过来认它,好列出 `truncated` 字段名;
#: 两处各写一份字面量的话,改了措辞的那天,列表会安静地变空。
TRUNCATION_MARK = "…[truncated {omitted} chars]"
_TRUNCATION_SUFFIX = "…[truncated "


def _safe_url(value: str) -> str:
    """保留可排障的地址，同时去掉签名查询串和 fragment。"""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value[:MAX_LOG_STRING_CHARS]
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return value[:MAX_LOG_STRING_CHARS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def safe_payload_for_log(value: Any, *, max_string_chars: int | None = None) -> Any:
    """保留请求/响应 JSON 的结构，但移除密钥、图片正文和签名 URL。

    这和 :func:`safe_request_summary` 的用途不同：摘要默认一直记；完整结构只在
    ``LLM_LOG_PAYLOADS=true`` 时记。图片 base64 即使打开完整日志也永不落盘，
    只留下 MIME、编码字符数和摘要，足够核对到底发送的是哪一份数据。

    ## ``max_string_chars``:两个去向,同一套脱敏

    诊断旁挂库(`llm/payload_store.py`)要的字符串上限比归档面宽:系统提示词
    本身就有几千字,截在 12k 会把"输出要求"那一段切掉 —— 而那段正是排查
    格式问题要看的。

    宽的那一档做成**参数**而不是第二个函数,是刻意的:图片正文、密钥键、
    签名 URL 三条规矩对两个去向完全一样,而复制一份脱敏逻辑出去意味着
    以后每加一条规矩都要记得改两处。**新开一个去向,最容易漏的就是在新去向上
    把老规矩忘了**,所以这里只让"截多长"可配,别的一个字不让改。

    不传时逐字维持原行为(``MAX_LOG_STRING_CHARS``),归档面因此没有任何变化。
    """
    limit = MAX_LOG_STRING_CHARS if max_string_chars is None else max(1, int(max_string_chars))
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_LOG_ITEMS:
                result["__truncated_fields__"] = len(value) - MAX_LOG_ITEMS
                break
            result[str(key)] = (
                "***"
                if _SECRET_KEY.search(str(key))
                else safe_payload_for_log(item, max_string_chars=limit)
            )
        return result
    if isinstance(value, (list, tuple)):
        items = [
            safe_payload_for_log(item, max_string_chars=limit) for item in value[:MAX_LOG_ITEMS]
        ]
        if len(value) > MAX_LOG_ITEMS:
            items.append({"__truncated_items__": len(value) - MAX_LOG_ITEMS})
        return items
    if isinstance(value, str):
        match = _DATA_URL.match(value)
        if match:
            encoded = match.group(2)
            return {
                "redacted": "inline_image",
                "mime_type": match.group(1),
                "base64_chars": len(encoded),
                "sha256_16": sha256(encoded.encode("ascii", errors="ignore")).hexdigest()[:16],
            }
        # 非 JSON 错误页可能把请求原文嵌进一整段 HTML/文本；不能只检查字符串开头。
        value = _EMBEDDED_DATA_URL.sub(
            lambda found: (
                f"[inline_image mime={found.group(1)} "
                f"base64_chars={len(found.group(2))}]"
            ),
            value,
        )
        value = _BEARER_SECRET.sub("Bearer ***", value)
        if value.startswith(("http://", "https://")):
            return _safe_url(value)
        if len(value) > limit:
            omitted = len(value) - limit
            return value[:limit] + TRUNCATION_MARK.format(omitted=omitted)
    return value


def was_truncated(value: Any) -> bool:
    """这个值是不是被 :func:`safe_payload_for_log` 截过。

    判据只有一条,而且和产出截断的那一行共用同一个常量。旁挂库靠它列出
    `truncated`,界面靠 `truncated` 告诉人"你看到的不是全文"。
    """
    return isinstance(value, str) and _TRUNCATION_SUFFIX in value


def safe_request_summary(
    *, url: str, body: dict[str, Any], image_count: int, inline_count: int
) -> dict[str, Any]:
    """可以进日志的请求摘要。

    **不含** ``messages`` / ``input`` —— 那里面是 base64 图片。
    一张 8MB 的图变成 base64 是 11MB 的字符串,进日志之后既撑爆采集,
    又把一份本该受授权约束的图片复制到了一个没人管的地方。
    """
    # url 必须过 `_safe_url`。自建端点的 base_url 里带签名或 token 查询串是
    # 常见做法(`?api-key=`、`?sig=`),而这条摘要是**默认一直记**的 ——
    # 不像完整 payload 要 LLM_LOG_PAYLOADS=true 才开。同一个函数里
    # `safe_payload_for_log` 早就对 URL 做了这件事,只有这里漏了。
    summary: dict[str, Any] = {"url": _safe_url(url), "image_count": image_count,
                               "inline_images": inline_count}
    for key in SAFE_BODY_KEYS:
        if key in body:
            summary[key] = body[key]
    return summary
