"""计价与预算判定。零依赖,任何机器上都跑得动。"""
from __future__ import annotations

from app.core.pricing import (
    BUDGET_CRITICAL,
    BUDGET_EXCEEDED,
    BUDGET_OK,
    BUDGET_UNSET,
    BUDGET_WARN,
    MICROS_PER_UNIT,
    PricingError,
    UnitPrice,
    aggregate_by_currency,
    evaluate_budget,
    format_money,
    parse_price_book,
    resolve_unit_price,
)
from tests.pure._helpers import expect_raises

BOOK_JSON = """
{
  "fashn":  {"*": {"micros": 40000, "currency": "USD"}},
  "openai": {"vision_score": {"micros": 1500, "currency": "USD"},
             "*":            {"micros": 500,  "currency": "USD"}}
}
"""


# ---------------------------------------------------------------- 价目表


def test_price_book_parses_and_lowercases_provider():
    book = parse_price_book(BOOK_JSON)
    assert set(book) == {"fashn", "openai"}
    assert resolve_unit_price(book, "FASHN", "submit").micros == 40000


def test_exact_operation_wins_over_wildcard():
    book = parse_price_book(BOOK_JSON)
    assert resolve_unit_price(book, "openai", "vision_score").micros == 1500
    assert resolve_unit_price(book, "openai", "anything_else").micros == 500


def test_unknown_provider_returns_none_not_zero():
    """None 表示「没配价」,0 表示「免费」。两者混同的后果是看板上
    一条平坦的绿线,而钱照花。
    """
    book = parse_price_book(BOOK_JSON)
    assert resolve_unit_price(book, "comfyui", "submit") is None


def test_empty_config_is_legal():
    assert parse_price_book(None) == {}
    assert parse_price_book("") == {}


def test_malformed_price_book_fails_loudly():
    for bad in (
        '{"fashn": 3}',
        '{"fashn": {"*": {}}}',
        '{"fashn": {"*": {"micros": -1}}}',
        '{"fashn": {"*": {"micros": 1.5}}}',
        '{"fashn": {"*": {"micros": true}}}',
        "[1, 2]",
    ):
        expect_raises(PricingError, parse_price_book, bad)


def test_cost_is_integer_arithmetic_end_to_end():
    """浮点会在月末汇总时留下一个对不上的尾数。全程整数。"""
    price = UnitPrice(micros=333_333, currency="USD")
    total = sum(price.cost_for(1) for _ in range(3))
    assert total == 999_999
    assert isinstance(total, int)


def test_negative_units_are_refused():
    expect_raises(PricingError, UnitPrice(micros=100, currency="USD").cost_for, -1)


# ---------------------------------------------------------------- 预算档位


def _status(spent: float, budget: float = 100.0, **kw):
    return evaluate_budget(
        spent_micros=int(spent * MICROS_PER_UNIT),
        budget_micros=int(budget * MICROS_PER_UNIT),
        elapsed_days=kw.pop("elapsed_days", 10.0),
        days_in_month=kw.pop("days_in_month", 30),
        **kw,
    )


def test_budget_levels_step_up_at_the_configured_ratios():
    assert _status(50).level == BUDGET_OK
    assert _status(70).level == BUDGET_WARN
    assert _status(90).level == BUDGET_CRITICAL
    assert _status(100).level == BUDGET_EXCEEDED
    assert _status(140).level == BUDGET_EXCEEDED


def test_no_budget_configured_is_its_own_level():
    """没设预算不是「没超支」。界面要能说「你还没设预算」,
    而不是显示一条 0% 的绿条让人以为一切正常。
    """
    s = evaluate_budget(
        spent_micros=99 * MICROS_PER_UNIT, budget_micros=0,
        elapsed_days=10.0, days_in_month=30,
    )
    assert s.level == BUDGET_UNSET
    assert s.projected_month_micros is None
    assert not s.should_alert


def test_projection_uses_average_daily_burn():
    """10 天花了 30,一个月 30 天 → 外推整月 90,预算 100 撑得住。"""
    s = _status(30, budget=100, elapsed_days=10.0, days_in_month=30)
    assert s.projected_month_micros == 90 * MICROS_PER_UNIT
    assert s.projected_exhaustion_day is None, "花不完就不该报耗尽日"


def test_projection_reports_the_day_the_budget_runs_out():
    """10 天花了 50 → 日耗 5 → **第 20 天**正好把 100 花完。

    这里原来断言 21,和上面这句 docstring 自相矛盾 —— 老实现写的是
    `int(day) + 1`,在 day 恰好是整数时会多报一天。整除是最常见的情形
    (预算和日耗都是人定的整数),也就是说那个 off-by-one 平时一直在生效。

    「第几天用完」这种数字没人会去验算,所以它错了也不会有人发现,
    而运营会按晚一天的预期安排采买。
    """
    s = _status(50, budget=100, elapsed_days=10.0, days_in_month=30)
    assert s.projected_exhaustion_day == 20


def test_a_non_integer_exhaustion_day_rounds_up_not_down():
    """日耗 6、预算 100 → 16.67 天。报 16 等于说"那天还够用",而那天会断。"""
    s = _status(60, budget=100, elapsed_days=10.0, days_in_month=30)
    assert s.projected_exhaustion_day == 17


def test_projection_survives_the_first_hour_of_the_month():
    """elapsed_days 接近 0 时不能除出天文数字,也不能崩。"""
    s = _status(1, budget=100, elapsed_days=0.0, days_in_month=31)
    assert s.projected_month_micros is not None
    assert s.projected_month_micros > 0


def test_zero_spend_gives_no_projection():
    s = _status(0, budget=100)
    assert s.projected_month_micros is None
    assert s.projected_exhaustion_day is None
    assert s.level == BUDGET_OK


def test_thresholds_are_configurable():
    s = _status(60, budget=100, warn_ratio=0.5, critical_ratio=0.55)
    assert s.level == BUDGET_CRITICAL


def test_should_alert_covers_every_level_above_ok():
    assert not _status(10).should_alert
    assert _status(75).should_alert
    assert _status(95).should_alert
    assert _status(120).should_alert


# ---------------------------------------------------------------- 显示


def test_money_keeps_two_decimals():
    """四舍五入到整数会让「这次花了 0.04」显示成 0,看起来像没花钱。"""
    assert format_money(40_000, "usd") == "0.04 USD"
    assert format_money(1_234_567) == "1.23 USD"
    assert format_money(0) == "0.00 USD"


# ==========================================================
# 按币种汇总:让「本月已花费」那个偏小的数说出自己偏小
# ==========================================================


def _row(provider: str, currency: str, micros: int) -> dict[str, object]:
    return {"provider": provider, "currency": currency, "cost_micros": micros}


def test_currencies_are_summed_within_themselves_and_never_across():
    """两个币种各自求和,**绝不相加**。

    micros 是"某个币种的百万分之一单位",不同币种相加得到的数没有单位,
    但它长得像钱。这一条是那个约束的可执行版本:如果哪天有人图省事
    把它们加起来,这里会红。
    """
    totals = aggregate_by_currency(
        [
            _row("fashn", "USD", 1_500_000),
            _row("openai", "USD", 500_000),
            _row("aliyun", "CNY", 3_000_000),
        ],
        main_currency="CNY",
    )
    by_code = {t.currency: t for t in totals}
    assert by_code["USD"].cost_micros == 2_000_000
    assert by_code["CNY"].cost_micros == 3_000_000
    assert len(totals) == 2, "两个币种就是两行,不许折算成一行"


def test_the_main_currency_is_included_and_flagged():
    """主币种也在结果里,并且带标记。

    只返回"其他币种"的话,消费方要自己判断哪个是主的才能把两边对上 ——
    而那正是前端不该做的那种判断。
    """
    totals = aggregate_by_currency(
        [_row("fashn", "USD", 10), _row("aliyun", "CNY", 20)], main_currency="CNY"
    )
    main = [t for t in totals if t.is_main]
    assert [t.currency for t in main] == ["CNY"]
    assert [t.currency for t in totals if not t.is_main] == ["USD"]


def test_currency_codes_are_upper_cased_before_grouping():
    """`usd` 和 `USD` 是同一个币种。不归一的话它们会变成两行,
    而页面会显示"另有 1.00 USD、2.00 usd 未计入"。"""
    totals = aggregate_by_currency(
        [_row("a", "usd", 1_000_000), _row("b", "USD", 2_000_000)], main_currency="cny"
    )
    assert [t.currency for t in totals] == ["USD"]
    assert totals[0].cost_micros == 3_000_000
    assert totals[0].is_main is False

    # `main_currency` 那一侧**也要归一**。少了这一步时 `is_main` 全是 False,
    # 于是页面会说"另有 3.00 CNY 没有计入已花费"—— 而那 3 块钱正是已花费本身。
    # 上面那组用例查不出这条:code 是 USD、main 是 cny,归不归一都不相等
    lower_main = aggregate_by_currency([_row("a", "CNY", 3_000_000)], main_currency="cny")
    assert lower_main[0].is_main is True, "main_currency 没归一,'cny' 永远匹配不上 'CNY'"


def test_a_missing_currency_falls_back_to_the_main_one():
    """币种为空的历史行按主币种算 —— 那是它写入时的语境。
    单独开一个空币种分组的话,页面会显示"另有 X 未计入",而那笔钱
    其实已经算进去了。"""
    totals = aggregate_by_currency(
        [_row("a", "", 1_000_000), {"provider": "b", "cost_micros": 2_000_000}],
        main_currency="CNY",
    )
    assert len(totals) == 1
    assert totals[0].currency == "CNY" and totals[0].cost_micros == 3_000_000


def test_display_strings_come_from_format_money():
    """金额字符串在这一侧生成。前端拿到的是成品,不用自己拼 ——
    两份货币格式化分叉的表现是同一笔钱在两个位置上写法不同。"""
    totals = aggregate_by_currency([_row("a", "USD", 1_234_567)], main_currency="CNY")
    assert totals[0].cost_display == format_money(1_234_567, "USD")
    assert totals[0].cost_display == "1.23 USD"


def test_no_rows_means_no_currencies_not_a_zero_row():
    """本月一次调用都没有时返回空列表,而不是一行 0。
    一行"0.00 CNY 未计入"会让页面亮起一条毫无意义的警告。"""
    assert aggregate_by_currency([], main_currency="CNY") == []


def test_the_order_is_stable_so_the_banner_does_not_reshuffle():
    """按币种代码排序。不定序的话页面上那句"另有 X、Y 未计入"
    每次刷新词序都变,读起来像数据在跳。"""
    rows = [_row("a", "USD", 1), _row("b", "CNY", 2), _row("c", "BRL", 3)]
    assert [t.currency for t in aggregate_by_currency(rows, "CNY")] == ["BRL", "CNY", "USD"]
    assert [t.currency for t in aggregate_by_currency(rows[::-1], "CNY")] == ["BRL", "CNY", "USD"]


# ---------------------------------------------------------------- 币种下的调用次数(A34)


def test_a_currency_with_only_unpriced_calls_does_not_disappear():
    """**只有未配价调用的币种必须仍然出现在结果里,并且能被认出来。**

    在此之前 `by_currency` 只带金额,而页面按"金额 > 0"过滤"另有 X 未计入"
    那条提示。于是一个巴西站跑了三次、单价还没配的月份:BRL 那一行金额是 0,
    被过滤掉,页面上一个字都不提它。顶层的 `unpriced_calls` 提示会亮,
    但它不说是哪个币种 —— 运营看到"有 3 次调用没算进钱里",找不到那 3 次。

    0 花费和未知花费在界面上长得一模一样,这一列就是把它们分开的依据。
    """
    totals = aggregate_by_currency(
        [
            {"provider": "aliyun", "currency": "CNY", "cost_micros": 5_000_000,
             "calls": 10, "unpriced_calls": 0},
            {"provider": "fashn", "currency": "BRL", "cost_micros": 0,
             "calls": 3, "unpriced_calls": 3},
        ],
        main_currency="CNY",
    )
    brl = [t for t in totals if t.currency == "BRL"]
    assert brl, "只有未配价调用的币种被丢掉了"
    assert brl[0].cost_micros == 0
    assert brl[0].calls == 3
    assert brl[0].unpriced_calls == 3, (
        "金额是 0 但调用真的发生过。没有这个数,消费方分不出"
        "「这个月没在这个币种上花钱」和「花了多少不知道」"
    )


def test_call_counts_are_summed_per_currency_across_providers():
    """次数和金额一样按币种各自求和,不跨币种相加。"""
    totals = aggregate_by_currency(
        [
            {"provider": "a", "currency": "USD", "cost_micros": 1, "calls": 2,
             "unpriced_calls": 1},
            {"provider": "b", "currency": "USD", "cost_micros": 1, "calls": 5,
             "unpriced_calls": 0},
            {"provider": "c", "currency": "CNY", "cost_micros": 1, "calls": 7,
             "unpriced_calls": 7},
        ],
        main_currency="CNY",
    )
    by_code = {t.currency: t for t in totals}
    assert (by_code["USD"].calls, by_code["USD"].unpriced_calls) == (7, 1)
    assert (by_code["CNY"].calls, by_code["CNY"].unpriced_calls) == (7, 7)


def test_rows_without_call_counts_still_aggregate():
    """老形状的行(只有 currency + cost_micros)不许炸,次数按 0 算。

    这个函数刻意只要求两个键(见它自己的说明),新加的两列不能反过来
    变成必填 —— 否则它就认识 provider 台账的全部字段了。
    """
    totals = aggregate_by_currency([_row("a", "USD", 1_000_000)], main_currency="CNY")
    assert totals[0].calls == 0 and totals[0].unpriced_calls == 0
    assert totals[0].cost_micros == 1_000_000
