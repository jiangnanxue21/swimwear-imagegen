"""Provider 路由:未配置阻止提交、手动指定、优先级顺序、FALLBACK 显式推迟。"""
from __future__ import annotations

from app.core.enums import RoutingMode
from app.core.errors import ValidationError
from app.providers.base import GenerationMode
from app.providers.errors import NotConfiguredError
from app.providers.registry import resolve
from tests.pure._helpers import expect_raises

VTO = GenerationMode.VIRTUAL_TRY_ON
P2M = GenerationMode.PRODUCT_TO_MODEL


def test_manual_picks_the_requested_provider():
    provider = resolve(mode=RoutingMode.MANUAL, requested="mock", generation_mode=VTO)
    assert provider.provider_name == "mock"


def test_manual_falls_back_to_default_when_not_specified():
    provider = resolve(
        mode=RoutingMode.MANUAL, requested=None, generation_mode=VTO, default_provider="mock"
    )
    assert provider.provider_name == "mock"


def test_manual_blocks_unconfigured_provider_at_submit_time():
    """需求第七章:Provider 未配置时自动阻止任务提交。

    必须在创建任务时就挡下,而不是让任务跑到 SUBMITTING 才失败 ——
    否则用户要等几十秒才知道自己没配 Key。
    """
    err = expect_raises(
        NotConfiguredError, resolve, mode=RoutingMode.MANUAL, requested="fashn", generation_mode=VTO
    )
    assert err.http_status == 409
    assert "未配置" in err.message


def test_manual_rejects_provider_that_cannot_do_the_mode():
    """mock 与 fashn 两种模式都支持,改用只声明 product_to_model 的 fal 来验证。

    fal 现在会先被「尚未实现」挡下,所以这里临时把它标成已实现 ——
    这条测试要验的是**模式检查**,不能让另一道更早的关卡把它遮住。
    未实现该被挡下这件事由 test_manual_rejects_unimplemented_provider 单独守。
    """
    from app.providers import registry
    from app.providers.fal import FalImageGenerationProvider
    from app.providers.registry import PROVIDER_FACTORIES

    original = PROVIDER_FACTORIES["fal"]
    original_implemented = registry.IMPLEMENTED_PROVIDERS
    PROVIDER_FACTORIES["fal"] = lambda: FalImageGenerationProvider(
        api_key="k", base_url="http://example.invalid"
    )
    registry.IMPLEMENTED_PROVIDERS = frozenset({*original_implemented, "fal"})
    try:
        err = expect_raises(
            ValidationError, resolve, mode=RoutingMode.MANUAL, requested="fal", generation_mode=VTO
        )
        assert "不支持" in err.message
    finally:
        registry.IMPLEMENTED_PROVIDERS = original_implemented
        PROVIDER_FACTORIES["fal"] = original


def test_priority_skips_unconfigured_and_lands_on_mock():
    """comfyui/fashn/fal 都没配时,PRIORITY 应当落到 mock —— 系统永远有得用。"""
    provider = resolve(mode=RoutingMode.PRIORITY, requested=None, generation_mode=VTO)
    assert provider.provider_name == "mock"


def test_priority_respects_the_configured_order():
    from app.providers.fashn import FashnImageGenerationProvider
    from app.providers.registry import PROVIDER_FACTORIES

    original = PROVIDER_FACTORIES["fashn"]
    PROVIDER_FACTORIES["fashn"] = lambda: FashnImageGenerationProvider(
        api_key="k", base_url="http://example.invalid"
    )
    try:
        chosen = resolve(
            mode=RoutingMode.PRIORITY,
            requested=None,
            generation_mode=VTO,
            priority_order=("fashn", "mock"),
        )
        assert chosen.provider_name == "fashn"
        # 换个顺序,结果必须跟着变 —— 证明顺序真的生效
        chosen = resolve(
            mode=RoutingMode.PRIORITY,
            requested=None,
            generation_mode=VTO,
            priority_order=("mock", "fashn"),
        )
        assert chosen.provider_name == "mock"
    finally:
        PROVIDER_FACTORIES["fashn"] = original


def test_priority_reports_what_it_tried_when_nothing_matches():
    """报告里只会出现**真的被试过**的家。

    fal 尚未实现,自动路由根本不会碰它 —— 报告里出现一个从没被调用过的名字,
    只会把排查的人往「fal 是不是配错了」的方向带。
    """
    err = expect_raises(
        NotConfiguredError,
        resolve,
        mode=RoutingMode.PRIORITY,
        requested=None,
        generation_mode=VTO,
        priority_order=("fashn", "fal"),
    )
    assert set(err.detail["tried"]) == {"fashn"}


def test_fallback_is_explicitly_deferred_not_silently_broken():
    """FALLBACK 依赖阶段 4 的错误策略,现在必须明确报错而不是假装支持。"""
    err = expect_raises(
        ValidationError, resolve, mode=RoutingMode.FALLBACK, requested=None, generation_mode=VTO
    )
    assert "阶段 5" in err.message


def test_routing_is_a_pure_function():
    """同样输入必须给同样结果 —— 路由不允许有随机性或隐藏状态。"""
    names = {
        resolve(mode=RoutingMode.PRIORITY, requested=None, generation_mode=VTO).provider_name
        for _ in range(5)
    }
    assert len(names) == 1


# ---------------------------------------------------------------- 未实现的 Provider


def test_manual_rejects_unimplemented_provider():
    """填了 Key 的 fal 一样跑不了:请求映射还没按官方文档落地。

    以前这里只查「已配置」和「支持该模式」,于是任务能创建、能排队,
    到 worker 才抛 NotImplementedError —— 用户等了十几秒拿到一个看不懂的失败。
    """
    from app.providers.fal import FalImageGenerationProvider
    from app.providers.registry import PROVIDER_FACTORIES

    original = PROVIDER_FACTORIES["fal"]
    PROVIDER_FACTORIES["fal"] = lambda: FalImageGenerationProvider(
        api_key="k", base_url="http://example.invalid"
    )
    try:
        err = expect_raises(
            ValidationError, resolve, mode=RoutingMode.MANUAL, requested="fal",
            generation_mode=P2M,
        )
        assert "尚未" in err.message or "未实现" in err.message
    finally:
        PROVIDER_FACTORIES["fal"] = original


def test_priority_never_selects_an_unimplemented_provider():
    from app.providers.comfyui import ComfyUIImageGenerationProvider
    from app.providers.registry import PROVIDER_FACTORIES

    original = PROVIDER_FACTORIES["comfyui"]
    PROVIDER_FACTORIES["comfyui"] = lambda: ComfyUIImageGenerationProvider(
        base_url="http://example.invalid"
    )
    try:
        chosen = resolve(
            mode=RoutingMode.PRIORITY, requested=None, generation_mode=VTO,
            priority_order=("comfyui", "mock"),
        )
        assert chosen.provider_name == "mock", "自动路由挑中了一个跑不了的 Provider"
    finally:
        PROVIDER_FACTORIES["comfyui"] = original


def test_provider_switch_never_lands_on_an_unimplemented_provider():
    """C/D 档修复策略会要求「换一家」,换过去必须是能跑的。"""
    from app.providers.comfyui import ComfyUIImageGenerationProvider
    from app.providers.registry import PROVIDER_FACTORIES, next_configured_provider

    original = PROVIDER_FACTORIES["comfyui"]
    PROVIDER_FACTORIES["comfyui"] = lambda: ComfyUIImageGenerationProvider(
        base_url="http://example.invalid"
    )
    try:
        alternative = next_configured_provider("mock", VTO, order=("comfyui",))
        assert alternative is None, "切换到了一个只会抛 NotImplementedError 的 Provider"
    finally:
        PROVIDER_FACTORIES["comfyui"] = original
