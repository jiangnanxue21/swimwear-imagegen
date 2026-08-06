"""Provider 接口契约:能力声明、未配置行为、Mock 完整生命周期。"""
from __future__ import annotations

import asyncio

from app.providers.base import (
    ExternalTaskStatus,
    GenerationMode,
    GenerationRequest,
    ImageGenerationProvider,
)
from app.providers.comfyui import ComfyUIConfig, ComfyUIImageGenerationProvider
from app.providers.errors import (
    NotConfiguredError,
    ProviderContentSafetyError,
    ProviderGenerationError,
    ProviderInputError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from app.providers.fal import FalImageGenerationProvider
from app.providers.fashn import FashnImageGenerationProvider
from app.providers.mock import MockImageGenerationProvider
from app.providers.registry import PROVIDER_FACTORIES, describe_all, get_provider
from tests.pure._helpers import expect_raises

ALL_PROVIDERS = [
    MockImageGenerationProvider,
    FashnImageGenerationProvider,
    FalImageGenerationProvider,
    ComfyUIImageGenerationProvider,
]


def _req(**kwargs) -> GenerationRequest:
    base = dict(
        mode=GenerationMode.VIRTUAL_TRY_ON,
        garment_image_url="http://x/garment.jpg",
        model_image_url="http://x/model.jpg",
        candidate_count=4,
        seed=1234,
    )
    base.update(kwargs)
    return GenerationRequest(**base)


def run(coro):
    return asyncio.run(coro)


# ---------- 契约 ----------

def test_all_providers_implement_the_interface():
    for cls in ALL_PROVIDERS:
        assert issubclass(cls, ImageGenerationProvider)
        assert cls.provider_name and cls.provider_name != "abstract"


def test_all_providers_can_be_constructed_without_credentials():
    """需求第十七章:没有任何 Key 时应用必须能正常启动。

    构造 Provider 是启动路径的一部分,不能因为缺 Key 就炸。
    """
    for cls in ALL_PROVIDERS:
        provider = cls()
        assert isinstance(provider.capabilities().supported_modes, set)
        assert isinstance(provider.is_configured(), bool)


def test_capabilities_are_self_consistent():
    for cls in ALL_PROVIDERS:
        caps = cls().capabilities()
        assert caps.supported_modes, f"{cls.provider_name} 未声明任何生成模式"
        if not caps.supports_multiple_candidates:
            assert caps.max_candidates_per_call == 1
        else:
            assert caps.max_candidates_per_call >= 1


def test_test_connection_never_raises_even_when_unconfigured():
    """Provider 页面必须永远打得开,哪怕全都没配。"""
    for cls in ALL_PROVIDERS:
        result = run(cls().test_connection())
        assert set(result) >= {"provider", "configured", "message"}
        assert isinstance(result["configured"], bool)


# ---------- 未配置 ----------

def test_unconfigured_third_party_providers_report_not_configured():
    for cls in (FashnImageGenerationProvider, FalImageGenerationProvider):
        assert cls().is_configured() is False


def test_unconfigured_submit_raises_not_configured_not_crash():
    for cls in (FashnImageGenerationProvider, FalImageGenerationProvider):
        err = expect_raises(NotConfiguredError, run, cls().submit(_req()))
        assert err.http_status == 409
        assert cls.provider_name in str(err.detail.get("provider", ""))


def test_comfyui_without_node_mapping_is_not_configured():
    provider = ComfyUIImageGenerationProvider(ComfyUIConfig(base_url="http://comfyui:8188"))
    assert provider.is_configured() is False
    err = expect_raises(NotConfiguredError, run, provider.submit(_req()))
    assert "节点映射" in err.message


def test_comfyui_reports_exactly_which_slots_are_missing():
    config = ComfyUIConfig(
        base_url="http://comfyui:8188",
        nodes={"garment_image": "12", "model_image": "13"},
    )
    assert set(config.missing_slots) == {"positive_prompt", "negative_prompt", "seed", "output"}


def test_comfyui_with_full_mapping_is_configured():
    config = ComfyUIConfig(
        base_url="http://comfyui:8188",
        nodes={
            s: str(i)
            for i, s in enumerate(
                [
                    "garment_image", "model_image", "positive_prompt",
                    "negative_prompt", "seed", "output",
                ],
                10,
            )
        },
    )
    provider = ComfyUIImageGenerationProvider(config)
    assert provider.is_configured() is True
    # 配置齐了但实现还没写,应当是 NotImplementedError 而不是静默成功
    expect_raises(NotImplementedError, run, provider.submit(_req()))


def test_comfyui_config_load_survives_missing_file():
    """没接 ComfyUI 的部署必须能正常启动。"""
    config = ComfyUIConfig.load("/nonexistent/path/config.yaml")
    assert config.missing_slots  # 空配置 -> 全部缺失,但没抛错


# ---------- Mock 生命周期 ----------

def test_mock_is_always_configured():
    assert MockImageGenerationProvider().is_configured() is True


def test_mock_full_lifecycle_produces_requested_candidates():
    provider = MockImageGenerationProvider()
    external_id = run(provider.submit(_req(candidate_count=4)))
    assert external_id.startswith("mock-")
    assert run(provider.get_status(external_id)) is ExternalTaskStatus.SUCCEEDED

    candidates = run(provider.fetch_results(external_id))
    assert len(candidates) == 4
    for index, candidate in enumerate(candidates):
        assert candidate.metadata["index"] == index
        assert len(candidate.metadata["inline_bytes"]) > 1000


def test_mock_output_is_deterministic_for_same_seed():
    """同 seed 必须同图,否则幂等与重放没法验证。"""
    a = MockImageGenerationProvider()
    b = MockImageGenerationProvider()
    first = run(a.fetch_results(run(a.submit(_req(seed=777)))))
    second = run(b.fetch_results(run(b.submit(_req(seed=777)))))
    assert [c.metadata["inline_bytes"] for c in first] == [
        c.metadata["inline_bytes"] for c in second
    ]


def test_mock_image_bytes_do_not_depend_on_wall_clock():
    """回归用例:曾把含时间戳的 external_id 画进像素,导致同 seed 出图不一致。

    两次提交的 external_id 必然不同,但图像字节必须完全相同。
    """
    provider = MockImageGenerationProvider()
    first_id = run(provider.submit(_req(seed=999)))
    second_id = run(provider.submit(_req(seed=999)))
    assert first_id != second_id, "external_id 本身应当唯一"
    first = run(provider.fetch_results(first_id))
    second = run(provider.fetch_results(second_id))
    assert first[0].metadata["inline_bytes"] == second[0].metadata["inline_bytes"]
    assert first[0].external_id != second[0].external_id


def test_mock_different_seeds_produce_different_images():
    provider = MockImageGenerationProvider()
    a = run(provider.fetch_results(run(provider.submit(_req(seed=1)))))
    b = run(provider.fetch_results(run(provider.submit(_req(seed=2)))))
    assert a[0].metadata["inline_bytes"] != b[0].metadata["inline_bytes"]


def test_mock_candidates_within_one_round_differ():
    """一轮 4 张必须互不相同,否则多候选毫无意义。"""
    provider = MockImageGenerationProvider()
    candidates = run(provider.fetch_results(run(provider.submit(_req(seed=5)))))
    assert len({c.metadata["inline_bytes"] for c in candidates}) == 4


def test_mock_respects_custom_dimensions():
    provider = MockImageGenerationProvider()
    candidates = run(provider.fetch_results(run(provider.submit(_req(width=512, height=512)))))
    assert candidates[0].metadata["width"] == 512


def test_mock_can_be_cancelled():
    provider = MockImageGenerationProvider()
    external_id = run(provider.submit(_req()))
    assert run(provider.cancel(external_id)) is True
    assert run(provider.get_status(external_id)) is ExternalTaskStatus.CANCELLED
    assert run(provider.fetch_results(external_id)) == []


def test_mock_cancel_unknown_task_returns_false():
    assert run(MockImageGenerationProvider().cancel("does-not-exist")) is False


def test_unknown_external_id_reports_unknown_status():
    """查不到的外部 ID 不能当成成功,否则会拉到空结果还以为跑完了。"""
    assert run(MockImageGenerationProvider().get_status("nope")) is ExternalTaskStatus.UNKNOWN


# ---------- Mock 失败分支 ----------

def test_mock_simulates_generation_failure():
    provider = MockImageGenerationProvider()
    external_id = run(provider.submit(_req(options={"mock_outcome": "fail"})))
    assert run(provider.get_status(external_id)) is ExternalTaskStatus.FAILED
    expect_raises(ProviderGenerationError, run, provider.fetch_results(external_id))


def test_mock_simulates_timeout_stays_running():
    provider = MockImageGenerationProvider()
    external_id = run(provider.submit(_req(options={"mock_outcome": "timeout"})))
    assert run(provider.get_status(external_id)) is ExternalTaskStatus.RUNNING
    expect_raises(ProviderTimeoutError, run, provider.fetch_results(external_id))


def test_mock_simulates_rate_limit_at_submit():
    provider = MockImageGenerationProvider()
    expect_raises(
        ProviderRateLimitError, run, provider.submit(_req(options={"mock_outcome": "rate_limit"}))
    )


def test_mock_simulates_content_safety_block():
    provider = MockImageGenerationProvider()
    expect_raises(
        ProviderContentSafetyError, run, provider.submit(_req(options={"mock_outcome": "safety"}))
    )


def test_mock_simulates_empty_result():
    """提交成功但没返回图 —— 编排层应据此触发重生。"""
    provider = MockImageGenerationProvider()
    external_id = run(provider.submit(_req(options={"mock_outcome": "empty"})))
    assert run(provider.fetch_results(external_id)) == []


# ---------- 请求校验 ----------

def test_virtual_try_on_requires_model_image():
    provider = MockImageGenerationProvider()
    err = expect_raises(
        ProviderInputError, run, provider.submit(_req(model_image_url=None))
    )
    assert "模特图" in err.message


def test_product_to_model_does_not_require_model_image():
    provider = MockImageGenerationProvider()
    external_id = run(
        provider.submit(_req(mode=GenerationMode.PRODUCT_TO_MODEL, model_image_url=None))
    )
    assert external_id


def test_missing_garment_image_is_rejected():
    provider = MockImageGenerationProvider()
    expect_raises(ProviderInputError, run, provider.submit(_req(garment_image_url="")))


def test_unsupported_mode_is_rejected():
    """fal 只声明支持 product_to_model,请求试穿应在校验期就被拦下。

    阶段 5 之前这条用的是 FASHN;FASHN 按官方文档接入后两种模式都支持了,
    于是改用仍是骨架的 fal —— 用例要验的是"能力声明会被执行",不是某一家的能力。
    """
    provider = FalImageGenerationProvider()
    provider.api_key, provider.base_url = "test-key", "http://example.invalid"
    err = expect_raises(
        ProviderInputError,
        run,
        provider.submit(_req(mode=GenerationMode.VIRTUAL_TRY_ON, candidate_count=1)),
    )
    assert "virtual_try_on" in err.message


def test_multi_candidate_rejected_when_unsupported():
    provider = FalImageGenerationProvider()
    provider.api_key, provider.base_url = "test-key", "http://example.invalid"
    err = expect_raises(
        ProviderInputError,
        run,
        provider.submit(_req(mode=GenerationMode.PRODUCT_TO_MODEL, candidate_count=4)),
    )
    assert "1 张" in err.message


def test_zero_candidates_is_rejected():
    expect_raises(
        ProviderInputError, run, MockImageGenerationProvider().submit(_req(candidate_count=0))
    )


# ---------- 注册表 ----------

def test_registry_exposes_all_four_providers():
    assert set(PROVIDER_FACTORIES) == {"mock", "fashn", "fal", "comfyui"}


def test_describe_all_never_leaks_secrets():
    """需求第十四章:前端不得显示完整 API Key。"""
    import json

    blob = json.dumps(describe_all(), ensure_ascii=False).lower()
    for banned in ("api_key", "apikey", "secret", "token", "password"):
        assert banned not in blob


def test_describe_all_marks_implemented_providers():
    """前端据此显示"未实现"。请求映射没按官方文档落地的,一律不能标已实现。"""
    by_name = {p["name"]: p for p in describe_all()}
    for name in ("mock", "fashn"):
        assert by_name[name]["implemented"] is True, name
    for name in ("fal", "comfyui"):
        assert by_name[name]["implemented"] is False, name


def test_get_provider_is_case_insensitive():
    assert get_provider("MOCK").provider_name == "mock"


def test_get_unknown_provider_lists_valid_options():
    from app.core.errors import ValidationError

    err = expect_raises(ValidationError, get_provider, "midjourney")
    assert "mock" in err.message
