"""日志脱敏(M2)。

规矩很短:**请求摘要里不出现 API Key、图片字节、base64、完整请求体。**

可以出现的是:URL、模型名、图片张数、内联张数、HTTP 状态码、
响应 ID、token 用量、finish reason。

为什么要单独一个模块:这条规矩在评分层已经守住了,但识别层(M3)会
写自己的日志。规矩写在注释里靠人记,写成函数才能被复用和被测试。
"""
from __future__ import annotations

from typing import Any

#: 摘要里允许出现的 body 字段。白名单,不是黑名单 ——
#: 黑名单漏一个就是把 base64 图片写进日志,而那种日志通常还会被采集到别处。
SAFE_BODY_KEYS: frozenset[str] = frozenset({"model", "max_output_tokens", "temperature"})


def safe_request_summary(
    *, url: str, body: dict[str, Any], image_count: int, inline_count: int
) -> dict[str, Any]:
    """可以进日志的请求摘要。

    **不含** ``messages`` / ``input`` —— 那里面是 base64 图片。
    一张 8MB 的图变成 base64 是 11MB 的字符串,进日志之后既撑爆采集,
    又把一份本该受授权约束的图片复制到了一个没人管的地方。
    """
    summary: dict[str, Any] = {"url": url, "image_count": image_count,
                               "inline_images": inline_count}
    for key in SAFE_BODY_KEYS:
        if key in body:
            summary[key] = body[key]
    return summary
