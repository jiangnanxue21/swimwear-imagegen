"""提示词版本 = 内容派生值(PRD §11.3 / BE-305)。

## 为什么是内容哈希

`prompt_version` 此前有四套口径,其中三套「改了措辞版本号不动」:识别绑
schema 版本、文案靠人记得改手写常量、出图是函数默认参数 `"v1"`。
`copy_generator.py` 的注释记录了这个坑已经发生过一次 —— 落库的版本号
一动不动,「这一版为什么和上一版不一样」就没法回答,幂等指纹也认不出
提示词变过。内容哈希把"版本变不变"从人的记性里拿出来。

`prompt_templates.version`(自增 int)保留,与它并存:前者回答「这是
第几版」,后者回答「这次调用用的到底是哪段文本」。

## 只接受已定型的最终字符串(AC-30b)

拼装内容里任何不稳定成分 —— dict 迭代顺序、时间戳、浮点格式化 ——
都会让同一份提示词每次哈希不同,于是历史记录变成一堆孤儿版本。
**那比「版本号不动」更糟,因为它看起来是在正常工作。** 所以这个函数
对非 str 输入直接 TypeError:序列化的责任在调用方,而且必须是稳定的;
收 dict 等于替调用方把这个责任藏起来。
"""
from __future__ import annotations

import hashlib

#: 12 位十六进制。与 `payload_store` 的 sha256_16 同族但刻意不同长,
#: 一眼分得出「这是提示词版本」还是「这是载荷摘要」。
_DIGEST_CHARS = 12


def content_version(content: str) -> str:
    """一段提示词文本的版本号:``sha256(content)[:12]``。

    同一份输入恒等(AC-30b);改一个字必变(AC-30)。
    """
    if not isinstance(content, str):
        raise TypeError(
            f"content_version 只接受已定型的最终字符串,收到 {type(content).__name__} —— "
            "dict/列表请先做**稳定**序列化再传进来(不稳定的序列化会造出一堆孤儿版本)"
        )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


#: 落库列宽(`evaluation_attempts.prompt_version` 等四张表统一 String(32))
_COLUMN_CHARS = 32


def version_text(value: object) -> str | None:
    """落库用的提示词版本文本(迁移 0055 / BE-307)。

    评分链路原来写的是 `value if isinstance(value, int) else None`。那句话在
    列还是 Integer 的时候是对的,**而它同时是一个静默丢弃器**:任何非 int 的
    版本值(内容哈希、`"template-1"` 这类)都会被换成 NULL,不报错、不告警。
    评分链路一旦接上 BE-305 的内容哈希,落库的版本就会集体变空,而
    「这批图当时按哪一版评的」正是提示词留档要回答的唯一问题。

    所以类型归一之后改成**强制转文本**,而不是把 isinstance 放宽:放宽会让
    int 和 str 两种形状同时存在于一列里,聚合时又要各写一次转换。

    放在这里而不是 `evaluation_service`:那个模块要 sqlalchemy 才 import 得动,
    于是守卫在离线环境里只能跳过 —— 而这个函数恰恰是**为了修一个静默错误**
    才存在的,它的守卫不该是全仓最先失去覆盖的那一条。
    """
    if value is None:
        return None
    text = str(value).strip()
    return text[:_COLUMN_CHARS] if text else None
