"""一次业务识别 = 几次**网络尝试**,以及这一笔该记几个计费单位。

**纯判定,零三方依赖。** 放在这里而不是服务层里,理由与 `call_budget.py`
一模一样:它的失败模式全部是边界(0 次 / 1 次 / 重试满 / 没人报),
而这些分支只有在零依赖模块里才穷举得起来。

## 它补的是哪个洞(A45-batch18 / P1-2)

传输层 `llm/transport.MultimodalClient.send()` 对超时、429、5xx 自动重试,
`EXTRACTOR_MODEL_MAX_RETRIES=2` 意味着**一次业务调用最多发出 3 个请求**。
业务层 `attributes/service._record_extraction_usage()` 却在最终成功或失败
之后才记一条流水,并且固定写 `billable_units=1`。两个方向都错:

    供应商已受理、客户端超时后重发      同一张图可能被计费 2～3 次,台账记 1
    Schema 构造 / 图片准备阶段失败      一个请求都没发出去,台账仍记 1

第一种是**少记已经花掉的钱**,第二种是**记了一笔从来没花过的钱**。
两者叠在一起,`/spend` 与供应商账单再也无法逐条核对 —— 而 §10.2 第 5 条
准入要的正是"用量记录与 Provider 后台账单条数一致"。

## 判据是"发出去了几次",不是"最后成不成功"

厂商在**收到请求那一刻**开始计费,超时和解析失败都不退钱(这条口径
`_record_extraction_usage` 的文档字符串里早就写了,只是当时没有数字支撑它)。
所以计费单位取**网络尝试次数**:

    0 次   preflight 失败(未配置 / 目标为空 / Schema 拒绝 / 图片准备失败)
           —— 一个字节都没出去,记 0
    N 次   传输层实际发起了 N 个往返,不论最后一次是成功还是失败

## 为什么"没人报"要退回 1 而不是 0

抽取器是可插拔的(`extractors/registry.py`)。一个第三方 billable 抽取器
没有接上尝试计数时,它的 detail 里没有这个键 —— 此时**不能**当成 0:
把"不知道"记成"没花钱"会让一整类调用从账上消失,而那个方向的错误
没有人会去发现。退回 1 是今天的行为,也是安全的那一侧。

判定与 `providers/base.settle_billable_units()` 同型:第二个返回值回答
"这个数是谁说的"。识别链路今天没有任何厂商自述用量,所以恒为 `inferred` ——
但来源列必须如实写,不能让一张估算表冒充和账单同源(§10.2 第 5 条)。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.enums import UnitsSource

#: 网络尝试次数在异常 detail / 结果 meta 里的键名。
#:
#: 写成常量而不是各处敲字符串:拼错一个字母的表现是"计数看起来接上了,
#: 其实每一次都走了退回分支",而且不报错 —— 与 `fashn.PARTIAL_IDS_KEY`
#: 同一个理由。
ATTEMPTS_KEY = "provider_attempts"


def attempts_from(source: Mapping[str, Any] | None) -> int | None:
    """从异常 detail 或结果 meta 里读出网络尝试次数。

    `None` = **这个抽取器没有报**,和"报了 0"是两件事,别合并:
    前者要退回保守推算(见 `settle_extraction_units`),后者是一次
    preflight 失败,真的一个请求都没发。这与 `providers/base.ProviderUsage`
    对 `units is None` 的处理是同一条规矩。

    负数与非整数按"没报"处理:一个坏值让流水记成负单位,比缺一列更糟。
    """
    if not source:
        return None
    raw = source.get(ATTEMPTS_KEY)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw >= 0 else None


def settle_extraction_units(*, reported_attempts: int | None) -> tuple[int, str]:
    """这一笔识别记几个计费单位,以及**这个数是谁说的**。

    >>> settle_extraction_units(reported_attempts=3)
    (3, 'inferred')
    >>> settle_extraction_units(reported_attempts=0)
    (0, 'inferred')
    >>> settle_extraction_units(reported_attempts=None)
    (1, 'inferred')

    来源恒为 `inferred`:识别链路今天没有任何厂商自述用量的通道
    (Responses / Chat Completions 的响应体里有 token 数,那是 token 不是
    计费调用数,两者的换算随厂商与档位变)。如实标成推算值,
    是为了让对账的人第一眼就知道这一行对不上该去查谁。
    """
    if reported_attempts is None:
        return 1, UnitsSource.INFERRED.value
    return max(int(reported_attempts), 0), UnitsSource.INFERRED.value


def describe_attempts(reported_attempts: int | None) -> str:
    """给日志与报错用的一句人话。**说清"发出去了几次"**。

    运营看到"识别失败"时唯一想得到的动作是再点一次,而每一次都在花钱。
    告诉他这次已经发出去几个请求,他才有第二个选项(去供应商后台核对)。
    """
    if reported_attempts is None:
        return "网络尝试次数未知(该抽取器没有上报)"
    if reported_attempts == 0:
        return "请求未发出(在调用供应商之前就失败了,本次不计费)"
    return f"已向供应商发出 {reported_attempts} 个请求(含传输层重试)"
