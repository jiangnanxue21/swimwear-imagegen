"""付费调用的花费台账(A23)。

## 它回答什么

    这个月花了多少?         按 provider 拆开
    照这个速度会不会超预算?  外推整月花费与耗尽日
    哪些调用没算进钱里?      没配价的那部分,单独列出来

数字全部来自 `provider_usage_records` 的真实行,**没有一处是估的**。
这条约束来自评审 6.2 节的扩展要求:「后端返回的每一个状态字段,都必须能
追溯到一个真实来源,不允许为了让接口形状完整而填常量」。

## 一处必须说清楚的边界

这里算的是**本系统发起的调用**花了多少,不是厂商账户里还剩多少。两者会不一致:

    别的系统共用同一把 Key      我们看不见那部分
    厂商侧的赠送额度 / 阶梯价    我们按配置的固定单价算
    失败但仍计费的调用          取决于厂商在哪一步开始计费
    汇率                        多币种混算时按各自币种分开列,不折算

所以接口字段一律叫 budget 不叫 balance,前端文案也不许出现"余额"。
权威数字在厂商控制台 —— 这一层是用来**提前发现异常**的,不是用来对账的。
"""
from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.clock import iso_utc
from app.core.config import settings
from app.core.logging import get_logger
from app.core.pricing import (
    MICROS_PER_UNIT,
    aggregate_by_currency,
    evaluate_budget,
    format_money,
    parse_price_book,
)
from app.models.generation import GenerationTask, ProviderUsageRecord

logger = get_logger(__name__)


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def summary(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """看板要的全部数字,一次给全。

    `now` 可注入,测试才能不依赖"今天是几号"—— 月初和月末的外推结果差别很大,
    一个只在月中通过的测试等于没测。
    """
    now = now or datetime.now(UTC)
    start = _month_start(now)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    elapsed_days = (now - start).total_seconds() / 86400.0

    rows = session.execute(
        select(
            ProviderUsageRecord.provider,
            ProviderUsageRecord.currency,
            func.sum(ProviderUsageRecord.cost_micros),
            func.count(ProviderUsageRecord.id),
            func.sum(ProviderUsageRecord.billable_units),
            # 没配价的调用数。它不是零花费,是**花费未知** ——
            # 单独数出来,否则一条平坦的绿线会掩盖一整个 provider 的开销
            func.count(ProviderUsageRecord.id).filter(
                ProviderUsageRecord.unit_price_micros.is_(None)
            ),
        )
        .where(ProviderUsageRecord.created_at >= start)
        .group_by(ProviderUsageRecord.provider, ProviderUsageRecord.currency)
        .order_by(func.sum(ProviderUsageRecord.cost_micros).desc())
    ).all()

    by_provider = [
        {
            "provider": provider,
            "currency": currency,
            "cost_micros": int(cost or 0),
            "cost_display": format_money(int(cost or 0), currency),
            "calls": int(calls or 0),
            "billable_units": int(units or 0),
            "unpriced_calls": int(unpriced or 0),
        }
        for provider, currency, cost, calls, units, unpriced in rows
    ]

    # 预算判定只对主币种做。多币种混算需要汇率,而汇率是又一个"看起来精确、
    # 实际是估的"数字 —— 宁可让别的币种单独列一行,也不折算
    main_currency = settings.SPEND_CURRENCY.upper()
    spent = sum(r["cost_micros"] for r in by_provider if r["currency"] == main_currency)

    # 按币种汇总(BLOCK-19 的后半段)。判定在 `core/pricing`,理由见那边 ——
    # 简单说是它能在 tests/pure 里被真的调用一次看返回值,而这里不能
    # (本模块顶层 import 了 SQLAlchemy)。
    by_currency = [
        {
            "currency": total.currency,
            "cost_micros": total.cost_micros,
            "cost_display": total.cost_display,
            "is_main": total.is_main,
            # 调用次数一起给出去。只给金额的话,一个只有未配价调用的币种
            # 在页面上会消失 —— 它的金额是 0,而那个 0 的意思是"未知"
            "calls": total.calls,
            "unpriced_calls": total.unpriced_calls,
        }
        for total in aggregate_by_currency(by_provider, main_currency)
    ]

    budget = evaluate_budget(
        spent_micros=spent,
        budget_micros=int(settings.SPEND_MONTHLY_BUDGET_MICROS or 0),
        elapsed_days=elapsed_days,
        days_in_month=days_in_month,
        warn_ratio=float(settings.SPEND_WARN_RATIO),
        critical_ratio=float(settings.SPEND_CRITICAL_RATIO),
    )

    return {
        "period": {
            "month": start.strftime("%Y-%m"),
            "started_at": iso_utc(start),
            "elapsed_days": round(elapsed_days, 2),
            "days_in_month": days_in_month,
        },
        "currency": main_currency,
        "spent_micros": spent,
        "spent_display": format_money(spent, main_currency),
        "budget": {
            "level": budget.level,
            "budget_micros": budget.budget_micros,
            "budget_display": format_money(budget.budget_micros, main_currency),
            "used_ratio": budget.used_ratio,
            "should_alert": budget.should_alert,
            "projected_month_micros": budget.projected_month_micros,
            "projected_month_display": (
                format_money(budget.projected_month_micros, main_currency)
                if budget.projected_month_micros is not None
                else None
            ),
            "projected_exhaustion_day": budget.projected_exhaustion_day,
        },
        "by_provider": by_provider,
        # 按币种汇总。**主币种那一行也在里面**,前端据此判断"这个数是不是全部"
        "by_currency": by_currency,
        "daily": _daily(session, start, now, main_currency=main_currency),
        # 曲线只画主币种。有别的币种时明确说出来,否则一条只反映 CNY 的线
        # 会被读成"这个月的全部开销"(BLOCK-19)
        "daily_currencies": sorted(
            {r["currency"] for r in by_provider if r["currency"]}
        ),
        # 看板据此提示"有 N 次调用没算进钱里,去配价目表"。
        # 这一条是本页最容易被忽略、也最容易让人低估开销的地方
        "unpriced_calls": sum(r["unpriced_calls"] for r in by_provider),
        "pricing_version": settings.PRICING_VERSION,
        # 价目表坏了的话这一页会显示「本月花费 0」,而那和「真的没花钱」
        # 在界面上一模一样。把原因摆出来,别让配置错误伪装成一份报表
        "price_book_error": price_book_error() or None,
        # 前端把它原样显示在页脚。写在后端而不是前端硬编码,
        # 是为了让"这不是厂商余额"这句话和产生数字的地方待在一起
        "disclaimer": (
            "以上为本系统发起的付费调用台账,按配置的价目表计算,"
            "不等于厂商账户余额。共用同一把 Key 的其他系统、厂商赠送额度、"
            "以及失败但仍计费的调用都不在其中。请以厂商控制台为准。"
        ),
    }


def _daily(
    session: Session, start: datetime, now: datetime, *, main_currency: str
) -> list[dict[str, Any]]:
    """按天的花费曲线。缺的日子补 0 —— 让曲线有洞会被读成"那天没数据",
    而实际是"那天没花钱",两者在判断趋势时含义完全相反。

    ## 必须按币种分组(BLOCK-19)

    原来这里只 `group_by(day)`,把 USD 和 CNY 的 micros 直接相加。
    micros 是"某个币种的百万分之一单位",不同币种相加得到的数**没有单位**,
    也就没有任何含义 —— 但它长得像钱,而且会被画成一条平滑的曲线。
    上面 `by_provider` 已经按币种拆开并写着"宁可让别的币种单独列一行,
    也不折算",这条曲线正好破坏了那条约束。

    折算同样不做:汇率是又一个"看起来精确、实际是估的"数字,而这一页的
    用途是提前发现异常,不是对账。

    `cost_micros` 保留为**主币种**的值(与 `spent_micros`、预算判定同源),
    另给 `by_currency` 让前端能画出多条线或明确提示"还有别的币种"。
    """
    # 分桶与下面补零的游标必须用**同一个时区**。
    #
    # `date_trunc` 直接作用在 timestamptz 上是按**会话时区**切天的,而补零游标
    # 是按 UTC 递增的。两者不一致时 `found` 的键一个都命中不了 ——
    # 表现是曲线上每天都是 0,而这个函数的注释恰好在说「让曲线有洞会被读成
    # 那天没数据」。时区不一致会让**每一天**都变成那种误读,比有洞更糟。
    #
    # `timezone('UTC', ts)` 把 timestamptz 转成 UTC 墙上时间再切,与游标同源。
    day = func.date_trunc("day", func.timezone("UTC", ProviderUsageRecord.created_at))
    rows = session.execute(
        select(day, ProviderUsageRecord.currency, func.sum(ProviderUsageRecord.cost_micros))
        .where(ProviderUsageRecord.created_at >= start)
        .group_by(day, ProviderUsageRecord.currency)
        .order_by(day)
    ).all()

    found: dict[str, dict[str, int]] = {}
    for bucket, currency, cost in rows:
        if bucket is None:
            continue
        key = bucket.date().isoformat()
        found.setdefault(key, {})[(currency or main_currency).upper()] = int(cost or 0)

    out: list[dict[str, Any]] = []
    cursor = start
    while cursor.date() <= now.date():
        key = cursor.date().isoformat()
        per_currency = found.get(key, {})
        out.append(
            {
                "date": key,
                "currency": main_currency,
                # 主币种那条线。补 0 的语义和以前一样:那天没花钱
                "cost_micros": per_currency.get(main_currency, 0),
                # 全部币种,原样给出,不求和也不折算
                "by_currency": [
                    {"currency": code, "cost_micros": value}
                    for code, value in sorted(per_currency.items())
                ],
            }
        )
        cursor += timedelta(days=1)
    return out


def price_book_error(raw: str | None = None) -> str:
    """价目表能不能解析。能则返回空串,不能则返回给人看的原因。

    给两个地方用:启动时打一条 error 日志,以及 `/api/usage/spend` 上的
    `price_book_error` 字段 —— 后者才是关键。配置写错时看板会显示
    「本月花费 0」,而那和「这个月真的没花钱」在界面上长得一模一样。
    把原因摆在页面上,才不至于让一个 JSON 拼写错误伪装成一份漂亮的报表。
    """
    try:
        _cached_price_book(settings.PROVIDER_PRICE_BOOK if raw is None else raw)
        return ""
    except Exception as exc:  # noqa: BLE001 - 原因要给人看,不分类
        return f"{type(exc).__name__}: {exc}"


@lru_cache(maxsize=8)
def _warn_once(raw: str, reason: str) -> None:
    """同一份坏配置只吼一次。

    `record_cost` 现在每次 Provider 调用都走一遍,不去重的话一次批量执行
    会刷出上百条一模一样的 error —— 而刷屏的日志和没有日志一样没人看。
    """
    logger.error(
        "price book is unparseable; calls will be recorded as unpriced",
        extra={"extra_fields": {"reason": reason}},
    )


def _price_book_or_empty(raw: str) -> dict[str, Any]:
    """解析价目表,**解析不了就当作没配价,绝不往上抛**。

    ## 为什么这里必须吞

    `PricingError` 的文档说「故意不静默降级为 0 —— 宁可让配置错误当场可见」。
    那句话对**启动校验**是对的,对**写入热路径**是错的:接线之后
    `record_usage()` 有五个调用点,其中三个在 `except` 块里 ——

        generation_tasks 的 ProviderError 处理    价目表异常会**盖掉原始失败原因**
        _abandon_attempt                          收尾动作自己炸掉,正好毁掉它要修的东西
        evaluation_service 的每张候选图           一次评分失败带崩整轮

    也就是说 `.env` 里一个 JSON 拼写错误 = 全站生成瘫痪。这个代价和
    「费用暂时算不出来」完全不成比例。

    ## 降级到哪里

    降级成**没配价**(`unit_price_micros=NULL`),不是降级成 0。两者的区别正是
    `pricing.py` 反复强调的那一条:NULL 是「不知道」,0 是「免费」。看板本来就
    把「未计入金额的调用数」单独做成一条 Alert,所以降级之后钱花在哪里仍然
    看得见 —— 加上 `price_book_error` 字段,页面还会直接说出是配置坏了。
    """
    try:
        return _cached_price_book(raw)
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        _warn_once(raw, f"{type(exc).__name__}: {exc}")
        return {}


def price_book() -> dict[str, Any]:
    """当前生效的价目表。**读它的人只能有这一个入口。**

    加这个公开函数是因为 6-5 的费用预估要用同一份价目表(`workbench/
    cost_rollup`),而它不该去调私有的 `_price_book_or_empty`,更不该自己
    `parse_price_book(settings.PROVIDER_PRICE_BOOK)` —— 后者会绕开
    「解析不了就当没配价、绝不往上抛」那条规矩(理由见 `_price_book_or_empty`),
    于是 `.env` 里一个 JSON 拼写错误会让向导整页打不开。
    """
    return _price_book_or_empty(settings.PROVIDER_PRICE_BOOK)


@lru_cache(maxsize=8)
def _cached_price_book(raw: str) -> dict[str, Any]:
    """解析价目表,按原始字符串缓存。

    `record_cost` 现在**每一次 Provider 调用都会走一遍**(接线之前它一次都没被
    调用过,所以这个开销以前不存在)。价目表是一段 JSON,每条流水重新解析一次
    既浪费又会让一个配置错误在每次调用时各抛一遍。

    键用原始字符串而不是无参缓存:`PROVIDER_PRICE_BOOK` 只从环境变量来
    (它不在 `settings_schema` 里,后台改不了),但万一将来可以改,
    换一个值就是换一个缓存键,不会拿旧价算新账。
    """
    return parse_price_book(raw)


def spent_for_generation_plan(session: Session, plan_id) -> Decimal | None:
    """这份方案迄今花掉多少(§4.7 `budget_cap` 的比较对象)。

    ## 为什么按方案汇总而不是按 SPU

    `budget_cap` 是**方案的**上限(§4.7 原文:"本方案累计费用上限")。
    按 SPU 汇总会让"给红色单独配一份预算更高的方案"这件事失去意义 ——
    它花的钱会记在同一个池子里。

    ## 为什么可能返回 None

    fail-closed 的前提是"读不到"能被表达出来(见
    `workflows.generation_plan.budget_verdict`)。查询本身出错时返回 None,
    调用方会拒绝创建任务 —— 而**返回 0 会变成"还没花钱,随便花"**,
    那是这条链路上最贵的一种默认值。

    金额一律走 `cost_micros`(整数微单位),与 `summary()` 同源。
    在这里换回 Decimal 是因为 `budget_cap` 是 Numeric,两侧不同型的
    表现是边界上"已花 9.999999999 < 上限 10"这种放行。
    """
    if plan_id is None:
        return None
    try:
        micros = session.scalar(
            select(func.coalesce(func.sum(ProviderUsageRecord.cost_micros), 0))
            .select_from(ProviderUsageRecord)
            .join(GenerationTask, GenerationTask.id == ProviderUsageRecord.task_id)
            .where(GenerationTask.generation_plan_id == plan_id)
        )
    except SQLAlchemyError:  # pragma: no cover - 读不到就 fail-closed
        return None
    if micros is None:
        return None
    return Decimal(int(micros)) / Decimal(MICROS_PER_UNIT)


def record_cost(
    record: ProviderUsageRecord,
    *,
    provider: str,
    operation: str,
    billable_units: int,
    price_book: dict[str, Any] | None = None,
) -> ProviderUsageRecord:
    """把费用写进一条流水。**在创建流水的那一刻调用,不要事后补算。**

    事后补算等于"按今天的价目表 × 历史用量",厂商一调价,上个月的报表
    就变成另一个数字。单价必须随流水快照下来。

    没配价时留 `unit_price_micros=None` 且 `cost_micros=0` —— 那是"未知",
    不是"免费",看板会把这类调用单独计数并提示去配价。
    """
    from app.core.pricing import resolve_unit_price

    book = price_book if price_book is not None else _price_book_or_empty(
        settings.PROVIDER_PRICE_BOOK
    )
    price = resolve_unit_price(book, provider, operation)

    record.billable_units = max(int(billable_units), 0)
    record.pricing_version = settings.PRICING_VERSION
    if price is None:
        record.unit_price_micros = None
        record.cost_micros = 0
        record.currency = settings.SPEND_CURRENCY.upper()
    else:
        record.unit_price_micros = price.micros
        record.cost_micros = price.cost_for(record.billable_units)
        record.currency = price.currency
    return record


#: 给别处引用,避免又一处硬编码 1_000_000
UNIT = MICROS_PER_UNIT
