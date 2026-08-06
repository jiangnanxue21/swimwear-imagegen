"""下载响应头的构造。**全仓只有这一份。**

## 要修的那件事

导出接口把 SKU / SPU 直接拼进 `Content-Disposition`::

    headers={"Content-Disposition": f'attachment; filename="{sku}-images.csv"'}

而 HTTP 头在 Starlette 里按 **latin-1** 编码。SKU/SPU 在 schema 上只有长度约束
(`product.py` 的 `min_length` / `max_length`),**没有任何 ASCII 限制** ——
一个中文 SKU 会让这一行抛 `UnicodeEncodeError`,整个导出接口返回 500,
而错误信息里完全看不出是文件名的问题。

四个地方犯同一个错(`api/exports.py` 一处、`api/workbench.py` 一处、
`api/workbench_batch.py` 两处),所以修法不是在四个地方各写一遍编码,
而是让它们共用这一个函数 —— 第五个导出接口加进来时不必重新想一遍。

## 为什么要两个 filename

RFC 6266 / RFC 5987 的做法是同时给两份:

    filename="..."            纯 ASCII 回退名。老客户端只认这个
    filename*=UTF-8''...      百分号编码的原名。现代浏览器优先用它

只给后者的话,少数老客户端会存成一个没有扩展名的文件;
只给前者等于中文名彻底丢掉。两份都给,两边都不吃亏。

`X-Export-Filename` 是我们自己的头,前端优先读它(理由见 `api/workbench.ts`:
`Content-Disposition` 里的中文与引号解析起来更容易出错)。
它同样受 latin-1 限制,所以放的是**已经百分号编码**的那一份,
前端 `decodeURIComponent` 一次即可。
"""
from __future__ import annotations

from urllib.parse import quote

#: 回退文件名。原名一个 ASCII 字符都不剩时用它 —— 比给一个空文件名好,
#: 空文件名会让浏览器存成 "download" 之类它自己编的名字,而那个名字
#: 和这次导出没有任何关系。
FALLBACK_STEM = "export"


def ascii_fallback(filename: str, *, default: str = FALLBACK_STEM) -> str:
    """把文件名压成纯 ASCII。

    非 ASCII 字符整体丢掉而不是逐个转写:转写(比如拼音)需要一张表,
    而那张表会在某个语言上出错,出错的表现是文件名变成一串看不懂的字母。
    丢掉之后至少扩展名和编号还在,运营认得出是哪一份。

    引号和反斜杠必须去掉:它们会截断 `filename="..."` 这个带引号的值,
    往后拼的东西就跑到语法外面去了。
    """
    stem = "".join(
        ch for ch in (filename or "") if 32 <= ord(ch) < 127 and ch not in '"\\'
    ).strip()
    if not stem or stem.lstrip(".") == "":
        # 只剩扩展名(比如原名整个是中文)时保住扩展名
        _, _, ext = (filename or "").rpartition(".")
        return f"{default}.{ext}" if ext and ext.isascii() else default
    if stem.startswith("."):
        # 「全中文.xlsx」剥掉非 ASCII 之后是 ".xlsx" —— 那在 unix 上是个隐藏文件,
        # 在 Windows 上直接是非法名。补回一个词干,不要让回退名比原名更难用
        return f"{default}{stem}"
    return stem


def download_headers(filename: str, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造一次文件下载的响应头。`filename` 可以含任意字符。

    >>> download_headers("泳装-SW-001-images.csv")["X-Export-Filename"]
    '%E6%B3%B3%E8%A3%85-SW-001-images.csv'
    """
    safe = ascii_fallback(filename)
    encoded = quote(filename or safe, safe="")
    headers = {
        "Content-Disposition": (
            f'attachment; filename="{safe}"; filename*=UTF-8\'\'{encoded}'
        ),
        # 已编码。前端 decodeURIComponent 一次拿回原名
        "X-Export-Filename": encoded,
    }
    if extra:
        headers.update(extra)
    return headers
