"""Provider 注册表与路由(需求第七章)。

两条硬约束:
1. **未配置的 Provider 不得被选中**,任务在创建阶段就被挡下,而不是跑到一半才失败;
2. **切换只能由明确规则触发**,不存在"模型自由决定用哪家"。路由是纯函数。

阶段 3 支持 MANUAL 与 PRIORITY。FALLBACK 需要基于失败错误类型做动态切换,
依赖阶段 4 的评分与错误策略,故在此显式拒绝而不是假装支持。
"""
from __future__ import annotations

from collections.abc import Callable

from app.core.enums import RoutingMode
from app.core.errors import ErrorCode, ValidationError
from app.providers.base import GenerationMode, ImageGenerationProvider
from app.providers.comfyui import ComfyUIImageGenerationProvider
from app.providers.errors import NotConfiguredError
from app.providers.fal import FalImageGenerationProvider
from app.providers.fashn import FashnImageGenerationProvider
from app.providers.mock import MockImageGenerationProvider

#: 名称 -> 构造函数。延迟构造,避免导入期就读配置。
PROVIDER_FACTORIES: dict[str, Callable[[], ImageGenerationProvider]] = {
    "mock": MockImageGenerationProvider,
    "fashn": FashnImageGenerationProvider,
    "fal": FalImageGenerationProvider,
    "comfyui": ComfyUIImageGenerationProvider,
}

#: 请求映射已按官方文档落地、可以真跑的 Provider。
#: 其余仍是骨架 —— 前端据此显示"未实现",避免运营选中一个只会抛 NotImplementedError 的家。
IMPLEMENTED_PROVIDERS: frozenset[str] = frozenset({"mock", "fashn"})

#: PRIORITY 模式的默认顺序。真实 Provider 优先,Mock 兜底保证永远有得用。
DEFAULT_PRIORITY_ORDER: tuple[str, ...] = ("comfyui", "fashn", "fal", "mock")


def available_names() -> list[str]:
    return list(PROVIDER_FACTORIES)


def get_provider(name: str) -> ImageGenerationProvider:
    factory = PROVIDER_FACTORIES.get((name or "").strip().lower())
    if factory is None:
        raise ValidationError(
            f"未知 Provider:{name};可选:{', '.join(available_names())}",
            code=ErrorCode.INPUT_INVALID,
        )
    return factory()


def describe_all() -> list[dict]:
    """供 GET /api/providers 使用。

    只读配置,不发网络请求 —— 外部服务挂掉不应该让 Provider 页面打不开。
    绝不返回 API Key 本身(需求第十四章)。

    ## `is_simulator` 与 `active` 这两列是补的(A42),缺了它们是一个真缺陷

    `core/environment.build_facet()` 读的是 `implemented` / `configured` /
    `is_simulator` 三列,而这里原来只报前两列。`row.get("is_simulator")`
    取到 `None` -> `bool(None)` 是 False -> Mock 的 `is_configured()` 恒为
    True -> **判定输出 REAL**,状态条据此对运营说「真的在调外部出图服务,
    会产生费用」。默认 Mock 环境下这句话每一个字都是反的。

    这个洞能活下来是因为两侧各自都有测试:`core/environment` 的判定被穷举
    钉死(8 种组合),这里的 `describe_all()` 也有形状测试,**没有一条跨过
    中间那道缝**去检查"注册表真的报得出判定要读的那三列"。补在
    `tests/pure/test_environment.py` 的「注册表接缝」一节。
    """
    # 走 provider_setting 而不是 get_settings():后台设置页改的 DEFAULT_PROVIDER
    # 写在数据库覆盖层里,不重启就该生效(见 docs/SETTINGS.md)。评分器注册表
    # 那一列用的是同一个读法 —— 两处必须同源,否则状态条会说 A 而任务跑的是 B。
    from app.providers._config import provider_setting

    active = (provider_setting("DEFAULT_PROVIDER", "mock") or "mock").strip().lower()
    result = []
    for name in available_names():
        provider = get_provider(name)
        caps = provider.capabilities()
        result.append(
            {
                "name": provider.provider_name,
                "display_name": provider.display_name or provider.provider_name,
                "configured": provider.is_configured(),
                "implemented": name in IMPLEMENTED_PROVIDERS,
                # 问实现类自己,不查名单 —— 理由在 providers/base.py 那一行上
                "is_simulator": bool(provider.is_simulator),
                "active": name == active,
                "capabilities": {
                    "supported_modes": sorted(m.value for m in caps.supported_modes),
                    "supports_multiple_candidates": caps.supports_multiple_candidates,
                    "supports_seed": caps.supports_seed,
                    "supports_cancel": caps.supports_cancel,
                    "supports_webhook": caps.supports_webhook,
                    "max_candidates_per_call": caps.max_candidates_per_call,
                },
            }
        )
    return result


def resolve(
    *,
    mode: RoutingMode | str,
    requested: str | None,
    generation_mode: GenerationMode,
    default_provider: str = "mock",
    priority_order: tuple[str, ...] = DEFAULT_PRIORITY_ORDER,
) -> ImageGenerationProvider:
    """选出本次任务要用的 Provider。纯函数,不产生副作用。"""
    routing = RoutingMode(mode)

    if routing is RoutingMode.FALLBACK:
        raise ValidationError(
            "FALLBACK 路由将在阶段 5 实现;当前请用 MANUAL 或 PRIORITY",
            code=ErrorCode.CONFIG_INVALID,
        )

    if routing is RoutingMode.MANUAL:
        provider = get_provider(requested or default_provider)
        _assert_usable(provider, generation_mode)
        return provider

    # PRIORITY:按顺序挑第一个「已实现 + 已配置 + 支持该生成模式」的。
    # 三个条件缺一不可 —— 少了「已实现」,自动路由会挑中一个只会抛 NotImplementedError 的家。
    tried: list[str] = []
    for name in priority_order:
        if name not in PROVIDER_FACTORIES or name not in IMPLEMENTED_PROVIDERS:
            continue
        provider = get_provider(name)
        tried.append(name)
        if provider.is_configured() and provider.capabilities().supports(generation_mode):
            return provider

    raise NotConfiguredError(
        f"没有可用的 Provider 支持 {generation_mode.value};已尝试:{', '.join(tried)}",
        provider="",
        detail={"tried": tried},
    )


def _assert_usable(provider: ImageGenerationProvider, generation_mode: GenerationMode) -> None:
    # 「已实现」要比「已配置」先查:填了 Key 的 fal.ai 一样跑不了,
    # 这时候说「未配置」会把人引到错误的方向上去。
    if provider.provider_name not in IMPLEMENTED_PROVIDERS:
        raise ValidationError(
            f"{provider.display_name or provider.provider_name} 的请求映射尚未按官方文档实现,"
            "暂时不能提交任务;请改用其它 Provider",
            code=ErrorCode.CONFIG_INVALID,
        )
    if not provider.is_configured():
        raise NotConfiguredError(
            f"{provider.display_name or provider.provider_name} 未配置,无法提交任务",
            provider=provider.provider_name,
        )
    if not provider.capabilities().supports(generation_mode):
        raise ValidationError(
            f"{provider.provider_name} 不支持 {generation_mode.value}",
            code=ErrorCode.INPUT_INVALID,
        )


def next_configured_provider(
    current: str,
    generation_mode: GenerationMode,
    *,
    order: tuple[str, ...] = DEFAULT_PRIORITY_ORDER,
    exclude: tuple[str, ...] = (),
) -> ImageGenerationProvider | None:
    """找一个可以替换当前 Provider 的备选。找不到返回 None。

    阶段 4 的 C/D 档修复策略里有"必要时切换 Provider"(需求第十二章)。
    这里提供**机制**,触发条件由规则给出 —— 大模型在这件事上没有任何裁量权
    (需求第七章)。

    今天只有 Mock 是完整实现的,所以除非另外配置了 FASHN / fal / ComfyUI,
    这个函数会诚实地返回 None,调用方保持原 Provider 继续重试。
    这与 FALLBACK **路由模式**(阶段 5)不是一回事:那个要处理的是
    "调用失败后按错误类型动态切换",范围大得多。
    """
    skip = {current.strip().lower(), *(e.strip().lower() for e in exclude)}
    for name in order:
        if name in skip or name not in PROVIDER_FACTORIES:
            continue
        if name not in IMPLEMENTED_PROVIDERS:
            continue
        provider = get_provider(name)
        if provider.is_configured() and provider.capabilities().supports(generation_mode):
            return provider
    return None
