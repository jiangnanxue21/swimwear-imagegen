"""模糊搜索的 LIKE 模式构造。纯函数,只依赖标准库。

## 要修的那件事

四个列表接口都这么拼搜索条件::

    like = f"%{search.strip()}%"
    stmt.where(or_(Product.spu.ilike(like), Product.sku.ilike(like), ...))

参数是绑定的,所以这**不是** SQL 注入。但 ``%`` 和 ``_`` 是 LIKE 自己的元字符,
拼进去之后它们照常生效:

    搜 ``SW-001_BLK``   ``_`` 匹配任意一个字符 -> 命中 ``SW-001XBLK``
    搜 ``%``            整串变成 ``%%%`` -> **返回全表**
    搜 ``100%纯棉``     ``%`` 匹配任意长度 -> 命中一堆不含"100"的行

失败方式是安静的:运营看到的是一张"搜出来的东西不对"的列表,而不是一条错误。
SKU / SPU 在 schema 上只有长度约束(`models/product.py`),下划线在真实
SKU 里很常见,所以第一条不是理论问题。

## 为什么必须显式给 escape

PostgreSQL 的 ``LIKE`` **默认转义符就是反斜杠**,但这一条属于实现细节:
SQL 标准里不带 ``ESCAPE`` 子句时转义符是未定义的,MySQL 的 ``NO_BACKSLASH_ESCAPES``
就会改掉它。`like_pattern()` 返回的串按 ``ESCAPE_CHAR`` 转义,调用方必须把它
一起传给 ``ilike(..., escape=ESCAPE_CHAR)`` —— 两者是一对,不要只用一半。
"""
from __future__ import annotations

#: LIKE 的转义符。用 ``\`` 是因为它与 PostgreSQL 的默认值一致,
#: 万一某处漏传 ``escape=`` 也不会当场把语义反过来。
ESCAPE_CHAR = "\\"

#: 需要转义的字符。**顺序有意义**:转义符自己必须第一个换,
#: 否则后面换出来的反斜杠会被再转义一次,``_`` 变成 ``\\_``(= 字面反斜杠 + 通配)。
_SPECIALS = (ESCAPE_CHAR, "%", "_")


def escape_like(value: str) -> str:
    """把用户输入里的 LIKE 元字符转义成字面量。"""
    out = value or ""
    for ch in _SPECIALS:
        out = out.replace(ch, ESCAPE_CHAR + ch)
    return out


def like_pattern(value: str | None) -> str:
    """用户输入 -> ``%已转义%``。配合 ``ilike(pattern, escape=ESCAPE_CHAR)`` 用。

    先 ``strip()`` 再转义:两边的空格是手滑,而不是搜索意图。
    """
    return f"%{escape_like((value or '').strip())}%"
