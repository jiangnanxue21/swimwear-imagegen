"""一次**失败的**提交该记几个计费单位。**纯判定,零三方依赖。**

## 它补的是哪个洞(A45-batch18 / P2-2)

`product-to-model` 每次 POST 只能提交 1 张(`max_per_call=1`),所以 4 张
候选 = 4 次独立提交。中途失败时 `fashn.submit()` 会把已受理的 prediction ID
挂在异常上(`_with_partial_ids`,那一段修的是"凭证不能丢"),但编排层
`tasks/generation_tasks.py` 记流水时**不传 `billable_units`**,于是走
`record_usage` 的默认值 `max(candidate_count, 1)` —— 而失败分支的
`candidate_count` 是 0,所以恒为 1。两个方向同时错:

    四次提交、前三次成功第四次失败    最多 4 次已发出的调用,台账记 1
    素材下载 / 输入校验在第一个 POST 之前失败    一个请求都没发,台账仍记 1

前者少记已经花掉的钱,后者记了一笔从来没花过的钱。

## 判据是"发出去了几次",不是"受理成功几个"

第四次失败时,那个请求**已经到了 FASHN 那里**,很可能已经计费(它的
失败可能是响应超时,而不是被拒)。按已受理的 3 个记账,少记的正是
最可能出问题、最需要人去核对的那一次。所以取 `attempted`。

这与识别链路 `extractors/call_accounting.settle_extraction_units` 是同一条
口径,也和 `record_usage` 文档里那句"失败的调用往往也计费"一致 ——
区别只是那句话原来没有数字支撑它。

## 三档,不是两档

    0        确认没发出去(preflight)。**这是一个结论**,不是缺省
    N ≥ 1    确认发出去了 N 个请求
    没人报    不知道 —— 退回 1,也就是这次修改之前的行为

第三档必须留:`submit()` 之外抛出的异常(`_require_config` 未配置、
`validate_request` 不过、缺模特图)不经过 `_with_partial_ids`,detail 里
没有这个键。把"不知道"当成 0 会让一整类真实调用从账上消失,
而那个方向的错误没有人会去发现 —— 与 `providers/base.ProviderUsage`
对 `units is None` 的处理是同一条规矩。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.enums import UnitsSource
from app.providers.fashn import ATTEMPTED_CALLS_KEY, PARTIAL_IDS_KEY


def attempted_calls(detail: Mapping[str, Any] | None) -> int | None:
    """异常上报的"已发出请求数"。`None` = 没报(见模块文档第三档)。

    负数与非整数按"没报"处理:一个坏值让流水记成负单位,比缺一列更糟。
    """
    if not detail:
        return None
    raw = detail.get(ATTEMPTED_CALLS_KEY)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw >= 0 else None


def accepted_ids(detail: Mapping[str, Any] | None) -> int:
    """已经拿到 prediction ID 的那几次。用于报错文案与排查,**不用于记账**。

    记账用 `attempted`,理由见模块文档"判据是发出去了几次"那一段。
    """
    raw = (detail or {}).get(PARTIAL_IDS_KEY)
    return len(raw) if isinstance(raw, (list, tuple)) else 0


def failed_submit_units(detail: Mapping[str, Any] | None) -> tuple[int, str]:
    """一次失败的提交记几个计费单位,以及**这个数是谁说的**。

    >>> failed_submit_units({"attempted_calls": 4, "partial_external_ids": ["a", "b", "c"]})
    (4, 'inferred')
    >>> failed_submit_units({"attempted_calls": 0})
    (0, 'inferred')
    >>> failed_submit_units({})
    (1, 'inferred')

    来源恒为 `inferred`:这是我们数出来的请求次数,不是 FASHN 报的额度。
    厂商自述(`x-fashn-credits-used`)只在成功收尾那条路径上拿得到 ——
    失败时那个头要么没有、要么语义未验证(是本次还是累计,至今没连过
    真端点确认过)。如实标成推算值,对账的人才知道这一行对不上该去查谁。
    """
    attempted = attempted_calls(detail)
    if attempted is None:
        return 1, UnitsSource.INFERRED.value
    return attempted, UnitsSource.INFERRED.value
