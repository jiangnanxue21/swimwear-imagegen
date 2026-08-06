"""列表排序参数的规范化。纯函数,只依赖标准库与 `app.core.errors`。

## 为什么排序值得单独一层

四个服务端分页的列表(商品 / 生成任务 / 图片审核 / 素材库)都需要排序。走查里
「500 个 SKU 时按更新时间、按 SKU 排序是刚需」这条成立,而上一轮**刻意没做**,
理由写在 `WorkbenchListPage.tsx` 里:客户端 sorter 只排当前 20 行,运营以为排的是
500 行 —— 那不是修好了,是把「没有排序」换成「有排序但结果是错的」。

所以排序必须下沉到 SQL。而一旦下沉,`sort` 就成了这套接口里**唯一一个直接来自
URL、又要拼进 ORDER BY 的字符串**。拼错的后果不是排得不对,是注入。这个模块只做
一件事:把 `(sort, order)` 收敛到调用方给出的白名单里。

## 三个刻意的决定

**越界报错,不静默回落。** `sort=priceee` 不会安静地退回默认排序 —— 那样前端参数
写错时界面看起来「排了」,而实际排的是别的东西,没人会发现。这里直接 400,
错误信息里带上可用值,前端联调时一眼看到。

**`ascend` / `descend` 是合法别名。** 唯一的客户端是 antd Table,它的 `sorter` 事件
给的就是这两个词。让后端认这两个词,前端就不必在四个页面各写一次映射;写四次的
东西迟早会有一次写反。这不是静默回落 —— 别名是明确列出来的。

**空字符串等同于没传。** axios 会把 `undefined` 的参数丢掉,但页面上「取消排序」这个
动作在 antd 里给的是 `undefined` 的 `order` 加一个仍然存在的 `columnKey`。两边都
当作「回到默认排序」处理,省掉前端一处清参数的判断。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, TypeVar

from app.core.errors import ErrorCode, ValidationError

#: 规范化之后只会出现这两个值。SQL 拼接与内存排序都以它为准。
ASC = "asc"
DESC = "desc"

#: antd Table 的 `sorter` 事件给的是 ascend/descend。列在这里,不在四个页面里。
_ORDER_ALIASES: Mapping[str, str] = {
    "asc": ASC,
    "ascend": ASC,
    "ascending": ASC,
    "desc": DESC,
    "descend": DESC,
    "descending": DESC,
}


def normalize_sort(
    sort: str | None,
    order: str | None,
    *,
    allowed: Iterable[str],
    default_sort: str,
    default_order: str = DESC,
) -> tuple[str, str]:
    """把一对来自 URL 的排序参数收敛成 `(字段, asc|desc)`。

    `allowed` 是这张表**允许**排序的字段名集合,由各自的 service 提供 ——
    白名单在数据模型旁边,而不是在这里,因为能不能按某个字段排是那张表的事
    (有没有索引、是不是可空、排出来对运营有没有意义)。

    参数越界抛 `ValidationError(INPUT_INVALID)`,由全局异常处理器转 400。
    """
    allowed_set = tuple(dict.fromkeys(allowed))  # 去重且保序,报错信息里好读
    if default_sort not in allowed_set:
        # 这是调用方的编码错误,不是用户输入错误 —— 立即炸,别等到线上才发现
        # 默认排序字段根本不在白名单里(那时它会表现为「所有请求都 400」)。
        raise AssertionError(f"默认排序字段 {default_sort!r} 不在白名单 {allowed_set} 中")

    field = (sort or "").strip()
    if not field:
        field = default_sort
    elif field not in allowed_set:
        raise ValidationError(
            f"不支持按 {field} 排序。可用字段:{', '.join(allowed_set)}",
            code=ErrorCode.INPUT_INVALID,
            http_status=400,
            detail={"field": "sort", "allowed": list(allowed_set)},
        )

    raw_order = (order or "").strip().lower()
    if not raw_order:
        direction = _ORDER_ALIASES.get(default_order.lower(), DESC)
    elif raw_order in _ORDER_ALIASES:
        direction = _ORDER_ALIASES[raw_order]
    else:
        raise ValidationError(
            f"排序方向只能是 asc 或 desc,收到 {raw_order}",
            code=ErrorCode.INPUT_INVALID,
            http_status=400,
            detail={"field": "order", "allowed": [ASC, DESC]},
        )

    return field, direction


T = TypeVar("T")


def sort_in_memory(
    rows: Sequence[T],
    *,
    keys: Mapping[str, Callable[[T], Any]],
    sort: str,
    order: str,
    tiebreak: Callable[[T], Any],
) -> list[T]:
    """内存排序。给工作台列表用 —— 它的筛选在判定结果上做,分页也在内存里。

    `tiebreak` 不是可选的。排序列有重复值时(比如按「阻断数」排,几十件都是 0),
    Python 的 sort 虽然稳定,但上游 `select` 的行序在两次请求之间并不保证一致,
    于是翻页会看见重复或漏掉的行。给一个唯一的兜底键,这个失败模式就不存在了。

    键函数返回 `None` 的行排到最后 —— 理由与 SQL 侧的 `nulls_last` 相同,
    见 `app.services.sorting.apply_order`。
    """
    key_fn = keys[sort]
    reverse = order == DESC

    def composite(row: T) -> tuple:
        value = key_fn(row)
        # (是否为空, 值) —— 空值那一档恒为 1,升序时自然落到最后;降序时整个元组
        # 反转会把它顶到最前,所以降序单独把标志位取反,保持「空值永远在最后」。
        missing = value is None
        flag = (not missing) if reverse else missing
        return (flag, _comparable(value), tiebreak(row))

    return sorted(rows, key=composite, reverse=reverse)


def _comparable(value: Any) -> Any:
    """把 None 换成一个能参与比较的占位值。

    真正决定位置的是外层元组的第一位(是否为空),这里只需要保证同一列里
    `None` 与字符串 / 数字放在一起时 `sorted` 不抛 TypeError。
    """
    if value is None:
        return ""
    return value
