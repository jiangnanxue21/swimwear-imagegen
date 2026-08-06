"""付费调用的计价与预算判定。

## 这个模块回答什么

`ProviderUsageRecord` 的注释一直写着「用于仪表盘统计与**将来的**成本核算」——
表里记了 provider、operation、成功与否、候选数,唯独**没有钱**。
于是整个仓库对费用的态度是分裂的:`batch_service.py` 里到处是「已计费的失败」
「费用不明」「宁可多核对不可重复计费」,但没有任何一处能回答「这个月花了多少」。

本模块补的是那个缺口的算术部分:单价 × 计费单位 = 金额,以及金额对预算的判定。
**它不碰数据库、不发请求**,所以能进 `tests/pure/`,在没装依赖的机器上也跑得动。

## 为什么用整数微单位而不是浮点

`0.1 + 0.2 != 0.3`。费用要累加成千上万条流水,浮点误差会在月末汇总时变成
一个对不上的尾数 —— 而对不上的账没人敢用。全程整数,**1 单位货币 = 1_000_000 微**。
显示时才除,且只除这一次。

## 为什么单价要随流水快照下来

厂商调价是常事。如果金额靠「查当前价目表 × 历史用量」实时算,那么调价当天
之前所有月份的账目会集体变化 —— 上个月的报表今天再打开就是另一个数字。
所以 `ProviderUsageRecord` 存的是**当时那次调用的单价与算好的金额**,
本模块只在写入那一刻被调用。

## 预算不是余额

这一点必须写在代码里,因为它决定了界面该怎么说话:我们能知道的是
**本系统发起的调用花了多少**,不是厂商账户里还剩多少。两者会不一致 ——
别的系统共用同一把 Key、厂商侧的赠送额度、失败但仍计费的调用、汇率,
每一样都会让差额变大。所以对外一律叫「预算」,权威数字在厂商控制台。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

#: 1 单位货币 = 多少微单位。整个系统只在这里定义一次。
MICROS_PER_UNIT = 1_000_000

#: 价目表里表示「这个 provider 的所有操作都按这个价」的通配键。
#: 大多数 provider 只有一种计费动作,逐个列出来只会让配置更容易写错。
ANY_OPERATION = "*"


class PricingError(ValueError):
    """价目表配置有问题。**故意不静默降级为 0** —— 单价缺失时算出 0 元,
    看板上就是一条平坦的绿线,而钱照花。宁可让配置错误当场可见。
    """


@dataclass(frozen=True)
class UnitPrice:
    """一次计费动作的单价。"""

    micros: int
    currency: str

    def cost_for(self, units: int) -> int:
        """算这次调用的金额(微单位)。

        `units` 是**计费单位数**,不是候选图数量 —— 两者常常相等,但不总是:
        一次失败的调用可能已经计费(units=1)却一张图都没产出。
        由调用方决定传什么,这里只做乘法。
        """
        if units < 0:
            raise PricingError(f"计费单位不能为负:{units}")
        return self.micros * units


def parse_price_book(raw: str | dict | None) -> dict[str, dict[str, UnitPrice]]:
    """把配置里的价目表解析成 {provider: {operation: UnitPrice}}。

    接受 JSON 字符串(来自环境变量)或已经解析好的 dict。形状:

        {
          "fashn":  {"*": {"micros": 40000, "currency": "USD"}},
          "openai": {"vision_score": {"micros": 1500, "currency": "USD"}}
        }

    空配置返回空表 —— 那是合法状态(还没接真实 provider 时),
    调用方靠 `resolve` 返回 None 来区分「没配价」和「价是 0」。
    """
    if raw is None or raw == "":
        return {}
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise PricingError(f"价目表必须是对象,拿到 {type(data).__name__}")

    book: dict[str, dict[str, UnitPrice]] = {}
    for provider, ops in data.items():
        if not isinstance(ops, dict):
            raise PricingError(f"{provider} 的价目必须是对象")
        entries: dict[str, UnitPrice] = {}
        for operation, spec in ops.items():
            if not isinstance(spec, dict) or "micros" not in spec:
                raise PricingError(f"{provider}.{operation} 缺少 micros")
            micros = spec["micros"]
            if not isinstance(micros, int) or isinstance(micros, bool) or micros < 0:
                raise PricingError(f"{provider}.{operation} 的 micros 必须是非负整数")
            entries[str(operation)] = UnitPrice(
                micros=micros, currency=str(spec.get("currency", "USD")).upper()
            )
        book[str(provider).lower()] = entries
    return book


def resolve_unit_price(
    book: dict[str, dict[str, UnitPrice]], provider: str, operation: str
) -> UnitPrice | None:
    """查单价。先找精确的操作,再退到通配。查不到返回 None。

    返回 None 而不是抛错,是因为「这个 provider 还没配价」在开发期是正常状态
    (mock provider 本来就不花钱)。调用方据此把这条流水记成 `cost_micros=0`
    且 `unit_price_micros=NULL` —— NULL 与 0 的区别是「不知道」与「免费」,
    看板要能把前者单独列出来提醒配价。
    """
    entries = book.get(str(provider).lower())
    if not entries:
        return None
    return entries.get(operation) or entries.get(ANY_OPERATION)


# ---------------------------------------------------------------- 预算判定

#: 预算档位。顺序即严重程度,前端按这个上色。
BUDGET_OK = "OK"
BUDGET_WARN = "WARN"
BUDGET_CRITICAL = "CRITICAL"
BUDGET_EXCEEDED = "EXCEEDED"
BUDGET_UNSET = "UNSET"


@dataclass(frozen=True)
class BudgetStatus:
    """本月花费相对预算的判定结果。"""

    level: str
    spent_micros: int
    budget_micros: int
    used_ratio: float
    #: 按目前速度,预算够撑到本月第几天。None = 没设预算或还没花钱
    projected_exhaustion_day: int | None
    #: 按目前速度推算的整月花费。None 同上
    projected_month_micros: int | None

    @property
    def should_alert(self) -> bool:
        return self.level in (BUDGET_WARN, BUDGET_CRITICAL, BUDGET_EXCEEDED)


def evaluate_budget(
    *,
    spent_micros: int,
    budget_micros: int,
    elapsed_days: float,
    days_in_month: int,
    warn_ratio: float = 0.7,
    critical_ratio: float = 0.9,
) -> BudgetStatus:
    """判定当前花费处于哪一档,并按目前速度外推。

    外推用的是**已过天数的平均日耗**,不是最近一天 —— 单日尖峰(比如跑了一次
    大批量)会让「还能撑几天」在两个方向上剧烈跳动,而一个跳来跳去的数字
    没人会照着做决定。

    `elapsed_days` 传浮点,因为月初第一个小时也要能给出一个数;
    下限钳到一天的一小部分,避免除以零把外推放大到天文数字。
    """
    if budget_micros <= 0:
        return BudgetStatus(
            level=BUDGET_UNSET,
            spent_micros=spent_micros,
            budget_micros=0,
            used_ratio=0.0,
            projected_exhaustion_day=None,
            projected_month_micros=None,
        )

    used_ratio = spent_micros / budget_micros
    if used_ratio >= 1.0:
        level = BUDGET_EXCEEDED
    elif used_ratio >= critical_ratio:
        level = BUDGET_CRITICAL
    elif used_ratio >= warn_ratio:
        level = BUDGET_WARN
    else:
        level = BUDGET_OK

    projected_day: int | None = None
    projected_month: int | None = None
    effective_days = max(float(elapsed_days), 1.0 / 24.0)
    if spent_micros > 0:
        daily = spent_micros / effective_days
        projected_month = int(round(daily * days_in_month))
        day = budget_micros / daily
        # 超过月末就不必报了 —— 「第 47 天用完」对一个 31 天的月份没有意义,
        # 那种情况要说的是「按这个速度花不完」,由前端读 None 决定文案。
        #
        # 已经超支时同样返回 None:预算都花完了,再说「第 26 天用完」是句
        # 指向未来的话,而事情已经发生了。EXCEEDED 那一档要说的是别的。
        #
        # `ceil` 而不是 `int(...) + 1`:后者在 day 正好是整数时会多报一天
        # (10.0 -> 第 11 天),而「第几天用完」这种数字没人会去验算
        if level == BUDGET_EXCEEDED:
            projected_day = None
        elif day <= days_in_month:
            projected_day = max(1, math.ceil(day))

    return BudgetStatus(
        level=level,
        spent_micros=spent_micros,
        budget_micros=budget_micros,
        used_ratio=round(used_ratio, 4),
        projected_exhaustion_day=projected_day,
        projected_month_micros=projected_month,
    )


def format_money(micros: int, currency: str = "USD") -> str:
    """给人看的金额。**只在最外层调用一次**,内部一律传微单位。

    保留两位小数:再多的位数对运营没有意义,而四舍五入到整数会让
    「这次调用花了 0.04」显示成 0,看起来像没花钱。
    """
    return f"{micros / MICROS_PER_UNIT:.2f} {currency.upper()}"


@dataclass(frozen=True)
class CurrencyTotal:
    currency: str
    cost_micros: int
    cost_display: str
    #: 是不是预算判定用的那个币种。**主币种也在结果里** ——
    #: 只给"其他币种"的话,消费方要自己判断哪个是主的才能把两边对上,
    #: 而那个判断在这里已经做过一次了
    is_main: bool
    #: 这个币种下发生过几次调用。
    calls: int = 0
    #: 其中没有配价、因而**没有计入金额**的有几次(A34)。
    #:
    #: 少了这一列,一个只有未配价调用的币种在页面上会彻底消失:它的
    #: `cost_micros` 是 0,而"另有 X 未计入"那条提示按金额非零过滤。
    #: 顶层的 `unpriced_calls` 提示确实会亮,但它不说是哪个币种 ——
    #: 于是运营看到"有 3 次调用没算进钱里",却找不到那 3 次在哪。
    #: **0 花费和未知花费在界面上长得一模一样,这一列是把它们分开的依据。**
    unpriced_calls: int = 0


def aggregate_by_currency(
    rows: list[dict[str, object]], main_currency: str
) -> list[CurrencyTotal]:
    """按币种把花费汇总起来。**不折算、不跨币种求和。**

    ## 这个函数解决的是"那个数偏小,而且没人说它偏小"

    `/spend` 上的「本月已花费」和预算进度条只统计主币种,因为多币种混算
    需要汇率,而汇率是又一个"看起来精确、实际是估的"数字。这个取舍本身
    是对的,但在此之前界面上**没有任何地方**说那个数不是全部 ——
    顶层只有一个 `daily_currencies` 币种代码清单,回答不了"少了多少"。

    ## 为什么求和落在这里而不是前端

    `format_money` 在这个模块里。前端自己拼金额字符串就是第二份货币格式化,
    而两份格式化分叉的表现是同一笔钱在两个位置上写法不同 ——
    那比不显示更让人不敢信。

    `rows` 只要求有 `currency` 与 `cost_micros` 两个键(`by_provider` 的形状),
    刻意不收窄成某个具体类型:这个模块不该认识 provider 台账的全部字段。
    `calls` 与 `unpriced_calls` 缺席时按 0 算 —— 老调用方传两键的行仍然可用。

    ## 为什么连调用次数一起数(A34)

    只数金额的话,**一个只有未配价调用的币种会从结果里消失**:它的
    `cost_micros` 是 0,而消费方按金额非零过滤。那笔钱不是 0,是未知,
    而"未知"恰恰是最该被看见的那一类。
    """
    main = main_currency.upper()
    totals: dict[str, int] = {}
    calls: dict[str, int] = {}
    unpriced: dict[str, int] = {}
    for row in rows:
        code = str(row.get("currency") or main).upper()
        totals[code] = totals.get(code, 0) + int(row.get("cost_micros") or 0)
        calls[code] = calls.get(code, 0) + int(row.get("calls") or 0)
        unpriced[code] = unpriced.get(code, 0) + int(row.get("unpriced_calls") or 0)
    return [
        CurrencyTotal(
            currency=code,
            cost_micros=value,
            cost_display=format_money(value, code),
            is_main=code == main,
            calls=calls.get(code, 0),
            unpriced_calls=unpriced.get(code, 0),
        )
        for code, value in sorted(totals.items())
    ]
