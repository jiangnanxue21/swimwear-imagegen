"""渠道注册表:一个渠道名 -> 谁来构造报文、谁来把它发出去(任务 14)。

## 为什么映射层和传输层是分开注册的

首期这两件事**不由同一个模块承担**,而这不是过渡期的将就:

    映射(map_fields / validate / build_request)   `generic/`   —— 我们自己的字段定义
    传输(submit / poll)                          `simulator.py` —— 平台行为的模拟

4.1 节 B 写明,拿不到 SHEIN 官方文档与凭证之前不允许宣称 SHEIN 已完成,
退而求其次是实现严格的 Simulator。于是现在的真实状态就是:报文是真的按我们的
字段规格构造出来的,发送端是模拟的。把两者绑成一个「渠道对象」会让这个事实
被一个名字盖住 —— 而 4.1 节 F 要求前端状态条如实显示「真实渠道 / Simulator」,
那条显示需要一个真的能追溯到来源的判据,不是一个常量。

`describe_channels()` 返回的 `is_simulator` 就是那个判据:它来自这张表里
传输层实际是谁,不是手写的 True/False(硬规矩 4,`describe_extractors()`
曾经硬编码 `configured: true` 的教训)。

## 契约三

本模块在 `app.channels` 下,因此不许 import `app.models` / `app.db` / `app.services`。
它只做「名字 -> 函数」的查表,数据由调用方传进来。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.channels import generic, simulator
from app.channels.base import PreparedRequest, PublishOperationRef
from app.listings.contracts import MappedListing


@dataclass(frozen=True)
class ChannelResponse:
    """传输层的一次响应。**保留状态码与响应头。**

    和 `base.SubmitResult` 的分工值得写清楚,否则下一个人会以为二者重复:

        SubmitResult      给业务看的结论:接受了没有、外部 ID 是什么
        ChannelResponse   给**重试策略**看的原始事实:状态码、Retry-After

    退避与重试的判定需要后者(`workflows/publish_policy.py`),而把状态码揉进
    SubmitResult 会让真实 Adapter 的返回类型被重试策略的需要污染 ——
    `simulator.SimResponse` 的文档里已经论证过同一件事。

    `timed_out` 单列一个字段而不是用一个假的状态码(比如 599):超时是
    「没有响应」,不是「响应是某个码」。用假状态码表示它,分类函数里就会
    出现一条对着 599 的分支,而那个数字不存在于任何 HTTP 规范里。
    """

    status_code: int = 0
    body: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    #: True = 请求发出去了但没等到响应。平台可能已经收到,见 7.4 节
    timed_out: bool = False
    #: 超时/连接失败时的简短说明。**只放异常类型,不放原文** ——
    #: 客户端库的异常原文里常常整条带着 URL 与凭证参数
    error: str = ""


#: 报文构造器。键是渠道名,与 `ChannelListing.channel` 落库的值一致。
_BUILDERS: Mapping[str, Callable[[MappedListing, PublishOperationRef], PreparedRequest]] = {
    generic.CHANNEL: generic.build_request,
}


def _simulator_submit(req: PreparedRequest, options: Mapping[str, Any]) -> ChannelResponse:
    """把 Simulator 的 `SimResponse` 转成传输层响应。

    这里**不**走 `simulator.submit()`:那个函数返回 `SubmitResult`,状态码
    和 Retry-After 在转换中丢掉了,而重试策略正需要它们。两条路径共用底下
    同一个 `simulate_submit`,所以行为不会分叉。
    """
    body = dict(req.body)
    header = body.get("header") if isinstance(body.get("header"), dict) else {}
    resp = simulator.simulate_submit(
        spu=str(header.get("spu") or ""),
        operation=body.get("operation") or "CREATE",
        idempotency_key=req.idempotency_key or "",
        body=body,
        scenario=options.get("scenario") if options else None,
    )
    return ChannelResponse(
        status_code=resp.status_code,
        body=dict(resp.body),
        headers=dict(resp.headers),
    )


def _simulator_poll(external_spu_id: str, options: Mapping[str, Any]) -> ChannelResponse:
    # `now` 可注入,理由同 `simulate_submit`:异步审核按**经过时间**推进,
    # 而一个依赖"真的等 60 秒"的测试没人会跑第二遍 —— 于是那条分支
    # (提交成功 ≠ 上架成功)就永远处在"写过但没跑过"的状态
    resp = simulator.simulate_poll(
        external_spu_id=external_spu_id, now=(options or {}).get("now")
    )
    return ChannelResponse(status_code=resp.status_code, body=dict(resp.body))


@dataclass(frozen=True)
class Transport:
    """一个渠道的发送端。"""

    name: str
    #: 是不是模拟器。**来自这张表,不是手填的常量** —— 见模块顶部
    is_simulator: bool
    submit: Callable[[PreparedRequest, Mapping[str, Any]], ChannelResponse]
    poll: Callable[[str, Mapping[str, Any]], ChannelResponse]


SIMULATOR = Transport(
    name=simulator.CHANNEL,
    is_simulator=True,
    submit=_simulator_submit,
    poll=_simulator_poll,
)

#: 渠道 -> 发送端。真实渠道进这张表的前提是官方文档到手且 spec 里不再有 TODO
#: (`FieldSpec.is_todo` + `CHANNEL_SPEC_INCOMPLETE` 启动拒绝,4.1 节 B)。
#: 在那之前 GENERIC 的发送端就是 Simulator,而这件事必须能被查询到、
#: 能显示在前端状态条上,不能只写在文档里。
_TRANSPORTS: Mapping[str, Transport] = {
    generic.CHANNEL: SIMULATOR,
}


class UnknownChannelError(ValueError):
    """渠道名不在注册表里。

    不降级成「用默认渠道」:一个拼错的渠道名会让商品被发到另一个店铺,
    而那种错误在平台上是不可撤销的。
    """


def known_channels() -> list[str]:
    return sorted(_BUILDERS)


def _resolve(channel: str) -> str:
    name = (channel or "").strip().upper()
    if name not in _BUILDERS:
        raise UnknownChannelError(
            f"未知渠道 {channel!r};已注册:{', '.join(known_channels()) or '(空)'}"
        )
    return name


def build_request(
    channel: str, mapped: MappedListing, op: PublishOperationRef
) -> PreparedRequest:
    return _BUILDERS[_resolve(channel)](mapped, op)


def transport_for(channel: str) -> Transport:
    return _TRANSPORTS[_resolve(channel)]


def describe_channels() -> list[dict[str, Any]]:
    """给 `GET /api/...` 与前端状态条用(4.1 节 F)。

    每个字段都能追溯到真实来源:`is_simulator` 来自注册表里挂的传输层是谁,
    `spec_complete` 来自 spec 文件里还有没有 TODO。**不填常量。**

    读 spec 会碰文件系统,读坏了不该让状态条整个打不开 —— 所以单个渠道的
    异常收敛成 `spec_complete: None`(未知),而不是抛出去。None 与 False
    要分开:「读不出来」和「读出来了、有 TODO」是两个不同的运维动作。
    """
    out: list[dict[str, Any]] = []
    for name in known_channels():
        transport = _TRANSPORTS.get(name)
        try:
            spec = generic.field_spec() if name == generic.CHANNEL else None
            spec_complete: bool | None = (not spec.todos()) if spec else None
        except Exception:  # noqa: BLE001 - 见 docstring 最后一段
            spec_complete = None
        out.append(
            {
                "channel": name,
                "transport": transport.name if transport else None,
                "is_simulator": bool(transport and transport.is_simulator),
                "spec_complete": spec_complete,
            }
        )
    return out
