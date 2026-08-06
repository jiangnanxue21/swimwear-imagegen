"""把规范化后的排序参数挂到 `select()` 上。

与 `app.core.sorting` 分开的唯一理由是分层:那边是纯函数(无 SQLAlchemy),
能在没有数据库的纯测试里跑;这里碰 ORM 表达式。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import nulls_last

from app.core.sorting import DESC


def apply_order(
    stmt: Any,
    *,
    columns: Mapping[str, Any],
    sort: str,
    order: str,
    tiebreak: Any,
) -> Any:
    """`ORDER BY <列> <方向>, <唯一列> ASC`。

    ## 兜底列不是装饰

    服务端分页 + 非唯一排序列 = 翻页时行会重复或漏掉。`ORDER BY status LIMIT 20
    OFFSET 20` 里,status 相同的那几十行之间没有任何定序,数据库有权在两次查询中
    给出不同顺序 —— 于是运营翻到第二页,看见第一页刚处理过的那件商品又出现了,
    而中间某一件从头到尾没露过面。这不是理论问题,PostgreSQL 换执行计划时就会发生。

    追加一个唯一列(这里都是主键 `id`)把行序钉死,代价是一次索引内的次级比较。

    ## 空值一律排最后

    可空列(审核项的 `best_score`、素材的 `role`)在 PostgreSQL 里默认 ASC 时
    NULLS LAST、DESC 时 NULLS FIRST。照默认走的话,「按评分从高到低」会让**一批
    根本没评过分的项**霸占列表顶部 —— 而运营点「按分数排」想看的恰恰是分数。
    NULL 的语义是「这一维没有值」,不是「值很大」也不是「值很小」,所以两个方向都
    压到最后,让有值的行先出来。
    """
    column = columns[sort]
    direction = column.desc() if order == DESC else column.asc()
    return stmt.order_by(nulls_last(direction), tiebreak.asc())
