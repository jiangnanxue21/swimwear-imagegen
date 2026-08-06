"""要**落库或回给前端**的异常说明,先过这里(BLOCK-15)。

## 问题长什么样

候选图下载失败时写的是:

    row.error_message = f"{type(exc).__name__}: {exc}"

而 httpx 的异常原文带着完整 URL:

    HTTPStatusError: Client error '403 Forbidden' for url
    'https://fal.media/files/x.png?X-Amz-Credential=AKIA...&X-Amz-Signature=9f3c...'

这条字符串进了 `generation_candidates.error_message`,而那一列会经
`GET /api/generation-tasks/{id}` 原样回到浏览器,再经仪表盘的
"最近失败任务"显示给任何能登录的人。签名 URL 在有效期内是**免鉴权可读**的,
等于把一张未过审的图连同一把临时钥匙一起贴了出去。进了库之后再想清掉要改数据。

## 规矩

**留结构,去取值。** 异常类型、HTTP 状态码、库自己的错误码都不含凭据,
留着才有排查价值;URL、查询串、Authorization、token、key 一律不留。

需要完整原文时看日志 —— 那里有异常和堆栈,而日志是滚动清理的,
留存期比业务表短得多,这正是两者该有的区别。

`services/dispatch_service.describe_failure()` 解决的是同一类问题的一个
特例(Broker URL 里的密码),它只保留类型名。这里要更进一步:候选图下载
的失败原因**必须**能区分 403 / 404 / 超时,否则运营无从判断是签名过期
还是 Provider 根本没出图。所以这里保留状态码,只把取值抹掉。
"""
from __future__ import annotations

import re

#: URL —— 连同查询串一起吃掉。只抹查询串是不够的:路径段里也可能是令牌
#: (`/v1/files/eyJhbGciOi.../raw`),而那种形状肉眼不一定认得出来
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")

#: `key=value` 形态的敏感取值。URL 已经整条抹掉了,这一条管的是
#: 异常原文里零散出现的那些(某些 SDK 会把请求参数拼进消息)
_SECRET_KV = re.compile(
    r"(?i)\b("
    r"x-amz-[a-z-]+|signature|sig|token|access[_-]?key|secret[_-]?key|api[_-]?key"
    r"|apikey|authorization|auth|password|passwd|pwd|credential[s]?|bearer"
    r")\b\s*[:=]\s*\S+"
)

#: 长串 base64 / 十六进制:多半是令牌或图片字节。20 位以下不动,
#: 否则会把正常的错误码和 UUID 片段也一起打码
_LONG_OPAQUE = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")

#: HTTP 状态码 —— 这个要留下来,单独识别一次
_STATUS = re.compile(r"\b([1-5]\d{2})\b")

REDACTED = "[已脱敏]"


def scrub(text: str, *, limit: int = 500) -> str:
    """把一段自由文本里的凭据、URL 与长令牌抹掉。

    只做替换,不判断"这段文本安不安全" —— 白名单式的判断在异常原文上
    做不到,因为原文形状由第三方库决定,而它们随版本变。
    """
    if not text:
        return ""
    cleaned = _URL.sub(REDACTED, text)
    cleaned = _SECRET_KV.sub(lambda m: f"{m.group(1)}={REDACTED}", cleaned)
    cleaned = _LONG_OPAQUE.sub(REDACTED, cleaned)
    return cleaned[:limit]


def describe_exception(exc: BaseException, *, limit: int = 500) -> str:
    """一次异常的**可落库**说明。

    形如 ``HTTPStatusError(403)`` / ``ReadTimeout`` / ``OSError(ENOSPC)``。

    保留三样东西,因为它们决定运营下一步做什么,而且都不含凭据:

        异常类型      是网络问题还是磁盘问题
        HTTP 状态码   403 是签名过期(可重试),404 是图没了(重试无用)
        库内错误码    某些客户端库给的稳定标识
    """
    name = type(exc).__name__

    code = getattr(exc, "code", None)
    if isinstance(code, (int, str)) and str(code).strip():
        return f"{name}({scrub(str(code), limit=32)})"

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    if isinstance(status, int):
        return f"{name}({status})"

    # 没有结构化字段时,从原文里只捞一个状态码出来 —— 原文本身不留
    match = _STATUS.search(str(exc))
    if match:
        return f"{name}({match.group(1)})"
    return name[:limit]


def safe_error_message(exc: BaseException, *, limit: int = 500) -> str:
    """要落库的失败说明。**自家异常留文案,第三方异常只留结构。**

    区别对待是有理由的:`AppError` 的 message 是我们自己写的中文,
    「候选图超过下载上限」这句话直接决定运营下一步做什么,丢掉它换成
    `ResultDownloadError(PROVIDER_DOWNLOAD_FAILED)` 是净损失。但它也可能
    嵌了第三方原文(`ResultDownloadError(f"拒绝下载:{exc}")`),所以照样过 `scrub`。

    第三方异常的原文形状由库的版本决定,今天不含凭据不代表下个小版本也不含 ——
    对它只保留类型和状态码。
    """
    from app.core.errors import AppError

    if isinstance(exc, AppError):
        cleaned = scrub(exc.message, limit=limit).strip()
        if cleaned:
            return cleaned
    return describe_exception(exc, limit=limit)
