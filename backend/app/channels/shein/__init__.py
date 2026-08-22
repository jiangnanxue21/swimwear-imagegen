"""SHEIN 渠道适配器。

## 它在注册表里,但**运行时不可用**

M0 只有 spec 骨架(`spec/common.yaml`),全部字段为 TODO。在拿到官方文档并存档到
`docs/vendor/shein-openapi/` 之前,这里不实现任何会发出真实请求的代码(硬规矩 1)。

从本批起它**进注册表**,而不是不存在。两者的差别是运营看到的东西:

    不在注册表  ->  `UnknownChannelError`,状态页上一个字都没有,
                    而 PRD §2.2 要求"Simulator 自动、真实 SHEIN 尚未接"必须分开写
    在注册表    ->  状态页如实显示 SHEIN 有 builder、没有 transport、spec 未完成,
                    任何构造报文的尝试抛 `ChannelNotReady` 并说明缺什么

一个查不到的渠道和一个说不清的渠道,在界面上长得一样;而前者会让人以为"还没开始做",
后者会让人以为"做好了"。注册表这一侧的正确答案是把事实摆出来。

## 已经落地的部分

`signing` / `decode` / `callback` / `capabilities` / `preflight` 五个模块是纯的:
输入全是参数,不依赖店铺凭据、叶子类目或响应形状。它们可以被测试,但拼不出一次
真实提交 —— 缺的是映射层,而映射层要等 `platform_category_id` 与响应字段取证。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.channels.base import PreparedRequest, PublishOperationRef
from app.core.errors import ErrorCode
from app.listings.contracts import MappedListing

#: 渠道代号。与 `ChannelListing.channel` 落库的值一致。
CHANNEL = "SHEIN"

SPEC_DIR = Path(__file__).resolve().parent / "spec"
#: 骨架 spec 的文件名。字段全部以 `TODO_` 开头,见该文件顶部。
SPEC_FILE = SPEC_DIR / "common.yaml"


class ChannelNotReady(RuntimeError):
    """渠道已注册但还不能用。

    不复用 `UnknownChannelError`:那个的含义是"渠道名拼错了",处置是改调用方;
    这个的含义是"这个渠道确实存在,但缺件",处置是去把缺的那件补上。用同一个
    异常会让两种完全不同的运维动作收到同一句话。
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.code = ErrorCode.CHANNEL_SPEC_INCOMPLETE.value


def spec_todo_keys() -> tuple[str, ...]:
    """spec 里还带 `TODO_` 前缀的字段名。

    只读文件、按前缀数,不走 `load_spec_file()` 的完整校验:那条路对带 TODO 的
    spec 会抛 `CHANNEL_SPEC_INCOMPLETE`,而状态页要的是"有几个没确认",
    不是"能不能加载"。读坏了让异常抛出去,由调用方决定是显示"未知"还是失败。
    """
    import yaml

    data = yaml.safe_load(SPEC_FILE.read_text(encoding="utf-8")) or {}
    keys: list[str] = []
    for section in ("header_fields", "row_fields"):
        for item in data.get(section) or ():
            key = str((item or {}).get("key") or "")
            if key.startswith("TODO_"):
                keys.append(key)
    for name in ("manual_fields", "sites"):
        for item in data.get(name) or ():
            if str(item).startswith("TODO_"):
                keys.append(str(item))
    return tuple(keys)


def spec_complete() -> bool:
    """spec 里还有没有未确认字段。"""
    return not spec_todo_keys()


def build_request(mapped: MappedListing, op: PublishOperationRef) -> PreparedRequest:
    """**拒绝构造报文。**

    映射层要等三件事:`platform_category_id` 与 `product_type_id` 的店铺级选择、
    动态填写标准快照、以及 `publishOrEdit` 的响应字段形状(PRD §7.1、§7.4)。
    在那之前构造出来的报文只能是猜的,而猜出来的报文发出去会创建一个真实商品。

    抛错而不是返回一个空报文:空报文会一路走到 transport,那里今天没有 SHEIN 的
    发送端,于是失败发生在更远的地方,错误信息也说不清缺的是什么。
    """
    from app.channels.shein import readiness

    reasons = readiness.blocking_reasons()
    raise ChannelNotReady(
        f"SHEIN 渠道尚未接通(当前档位 {readiness.mode()},共 {len(reasons)} 条阻断):"
        + ";".join(reasons[:3])
        + ("…" if len(reasons) > 3 else "")
    )


def describe() -> dict[str, Any]:
    """给状态页的一行事实。**不填常量。**

    `ready` 原来是一个字面 `False`,`blocked_on` 是一句手写的话 —— 两者都对,
    但它们对的方式是"有人当时记得写对",而不是从真实来源推出来的(硬规则 4)。
    取证推进时那句话不会跟着变,而它变不变没有任何东西会说。

    现在两格都由 `readiness` 算:`mode` 三档、`blocking_reasons` 逐条可追溯到
    读它的那个模块。**这里不再自己判断任何事**,只是把闸的答案摊平进这一行。
    """
    from app.channels.shein import readiness

    todos = spec_todo_keys()
    gate = readiness.describe()
    return {
        "channel": CHANNEL,
        "spec_file": str(SPEC_FILE.name),
        "todo_field_count": len(todos),
        "ready": gate["real_write_allowed"],
        "mode": gate["mode"],
        "blocking_reasons": gate["blocking_reasons"],
        "sources_unverified": gate["sources_unverified"],
        "sources_stale": gate["sources_stale"],
    }
