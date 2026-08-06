"""列表排序的纯测试。

排序是这套接口里唯一一类**直接来自 URL、又要拼进 ORDER BY** 的字符串,
所以这里断言的重点不是「排得对不对」,是「非法输入进不去」和
「翻页时行序稳定」这两件会变成线上事故的事。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import ErrorCode, ValidationError
from app.core.sorting import ASC, DESC, normalize_sort, sort_in_memory
from tests.pure._helpers import expect_raises

ALLOWED = ("created_at", "sku", "best_score")


def test_defaults_apply_when_nothing_is_passed():
    assert normalize_sort(None, None, allowed=ALLOWED, default_sort="created_at") == (
        "created_at",
        DESC,
    )


def test_empty_strings_are_treated_as_absent():
    """antd 取消排序时给的是 undefined 的 order 加一个仍在的 columnKey。

    两边都当作「回到默认排序」,前端因此不必在四个页面各写一次清参数的判断。
    """
    assert normalize_sort("", "", allowed=ALLOWED, default_sort="sku", default_order="asc") == (
        "sku",
        ASC,
    )


#: (用例名, URL 里传进来的 order 原文, 期望收敛成的方向)
#:
#: 用例名是必须的,不是装饰。`@pytest.mark.parametrize` 会把每个参数组
#: 报成一个独立用例,循环化之后六个组塌缩成一个函数 —— 不把名字和原文
#: 写进断言消息的话,失败时只能看到「第几行 assert 挂了」,还得回头数
#: 是哪一组输入。那正是参数化本来帮我们省掉的事。
ORDER_ALIAS_CASES: tuple[tuple[str, str, str], ...] = (
    ("小写 asc", "asc", ASC),
    ("antd ascend", "ascend", ASC),
    ("全大写 ASCENDING", "ASCENDING", ASC),
    ("小写 desc", "desc", DESC),
    ("antd descend", "descend", DESC),
    ("首字母大写 Descending", "Descending", DESC),
)


def test_antd_order_aliases_are_accepted():
    """认 antd 的 ascend/descend,省掉前端四处各写一次映射 —— 写四次的会有一次写反。

    大小写混写的三组不是凑数:`ASCENDING` / `Descending` 走的是
    `normalize_sort` 里的 `.lower()`,而这个别名表哪天改成精确匹配时,
    正是这三组会先红。
    """
    for name, raw, expected in ORDER_ALIAS_CASES:
        actual = normalize_sort("sku", raw, allowed=ALLOWED, default_sort="sku")[1]
        assert actual == expected, (
            f"[{name}] order={raw!r} 应收敛成 {expected!r},实际是 {actual!r}"
        )


def test_unknown_sort_field_is_rejected_not_silently_ignored():
    """越界必须报错。

    静默回落到默认排序是更糟的行为:前端参数写错时界面「看起来排了」,
    而实际排的是别的东西,没有任何人会发现。
    """
    exc = expect_raises(
        ValidationError,
        normalize_sort,
        "price; DROP TABLE products",
        None,
        allowed=ALLOWED,
        default_sort="sku",
    )
    assert exc.code is ErrorCode.INPUT_INVALID
    assert exc.http_status == 400
    # 报错信息要能直接指导联调,而不是只说「参数非法」
    assert "sku" in exc.message


def test_unknown_order_is_rejected():
    exc = expect_raises(
        ValidationError,
        normalize_sort,
        "sku",
        "sideways",
        allowed=ALLOWED,
        default_sort="sku",
    )
    assert exc.code is ErrorCode.INPUT_INVALID


def test_default_sort_outside_whitelist_is_a_coding_error():
    """调用方写错默认字段时立刻炸,而不是让每一个请求都 400。

    这里期望的就是 `AssertionError`,而 `expect_raises` 内部也用它来报
    「没抛」和「抛错了类型」。两者不会混淆:命中的那条 `except` 直接
    return,只有真的没抛时才会走到末尾那句 raise。
    """
    expect_raises(
        AssertionError,
        normalize_sort,
        None,
        None,
        allowed=ALLOWED,
        default_sort="nonexistent",
    )


# ---------------------------------------------------------------- 内存排序


@dataclass(frozen=True)
class Row:
    sku: str
    blocking: int
    score: float | None


ROWS = [
    Row("SW-003", 0, 91.0),
    Row("SW-001", 0, None),
    Row("SW-004", 2, 60.5),
    Row("SW-002", 0, 72.0),
]
KEYS = {
    "blocking": lambda r: r.blocking,
    "score": lambda r: r.score,
    "sku": lambda r: r.sku,
}


def _sorted(sort: str, order: str) -> list[str]:
    return [
        r.sku
        for r in sort_in_memory(ROWS, keys=KEYS, sort=sort, order=order, tiebreak=lambda r: r.sku)
    ]


def test_ties_are_broken_deterministically():
    """三行的 blocking 都是 0。没有兜底键的话它们之间的顺序取决于上游 select,

    而上游行序在两次请求之间并不保证一致 —— 于是翻页会看见重复或漏掉的行。
    """
    assert _sorted("blocking", ASC)[:3] == ["SW-001", "SW-002", "SW-003"]
    # 降序时兜底键跟着反转,但仍然是确定的:确定性才是这里要的东西
    assert _sorted("blocking", DESC) == ["SW-004", "SW-003", "SW-002", "SW-001"]


def test_missing_values_sink_to_the_bottom_in_both_directions():
    """「没有分」不是「分很低」,也不是「分很高」。

    按分数倒序时,一批根本没评过分的项霸占列表顶部 —— 而运营点「按分数排」
    想看的恰恰是分数。所以空值两个方向都排最后。
    """
    assert _sorted("score", DESC) == ["SW-003", "SW-002", "SW-004", "SW-001"]
    assert _sorted("score", ASC) == ["SW-004", "SW-002", "SW-003", "SW-001"]


def test_sorting_does_not_mutate_the_input():
    before = list(ROWS)
    sort_in_memory(ROWS, keys=KEYS, sort="sku", order=DESC, tiebreak=lambda r: r.sku)
    assert list(ROWS) == before
