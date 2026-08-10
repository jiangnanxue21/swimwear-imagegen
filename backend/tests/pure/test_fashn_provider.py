"""FASHN 适配器契约测试(阶段 5)。

这些用例**不发任何网络请求**:请求映射、错误分类、状态映射、批次拆分都是纯函数,
素材一律用 data-URI 传入,于是 `build_inputs` 也能离线跑。

用例里的每一个字段名和错误代码都对照官方 skill 文档
(`docs/vendor/fashn-skill/reference.md`)写死 —— 这正是它们的价值:
将来有人图省事把 `product_image` 改成 `garment_image`,这里立刻变红。
"""
from __future__ import annotations

import asyncio
import ipaddress
import sys
import tempfile
import types
from pathlib import Path

from app.core import net_safety
from app.core.errors import ErrorCode
from app.providers.base import (
    ExternalTaskStatus,
    GenerationMode,
    GenerationRequest,
    provider_input_reference,
)
from app.providers.errors import (
    ProviderAuthError,
    ProviderContentSafetyError,
    ProviderError,
    ProviderGenerationError,
    ProviderInputError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from app.providers.fashn import (
    ASPECT_RATIOS,
    DEFAULT_BASE_URL,
    PRODUCT_TO_MODEL,
    STATUS_MAP,
    TRYON_MAX,
    TRYON_V16,
    FashnImageGenerationProvider,
    aggregate_status,
    batch_sizes,
    data_uri,
    error_from_api,
    error_from_runtime,
    model_for,
    nearest_aspect_ratio,
)
from tests.pure._helpers import expect_raises  # noqa: F401  (供扩展用)

#: 1x1 像素的假图,只是为了走通 data-URI 分支,不会被解码
TINY = data_uri(b"\xff\xd8\xff\xd9", "image/jpeg")


def run(coro):
    return asyncio.run(coro)


def _provider() -> FashnImageGenerationProvider:
    return FashnImageGenerationProvider(api_key="test-key")


def _req(**overrides) -> GenerationRequest:
    payload = {
        "mode": GenerationMode.VIRTUAL_TRY_ON,
        "garment_image_url": TINY,
        "model_image_url": TINY,
        "candidate_count": 4,
        "seed": 12345,
    }
    payload.update(overrides)
    return GenerationRequest(**payload)


def _with_setting(name: str, value: str):
    """临时改一个配置项,返回恢复函数。

    **只改 os.environ 是不够的。** ``provider_setting()`` 的读取顺序是
    后台覆盖 -> Settings -> os.environ,而 Settings 是一个 lru_cache 的单例:
    装了 pydantic 的环境里它早就实例化好了,后改的环境变量根本进不去。
    于是这个 helper 在 CI(装了依赖)上失效,在纯测试环境(没装 pydantic,
    走 environ 回落分支)上却是绿的 —— 一个只在「真环境」里红的测试,
    最容易被当成「环境问题」放过去。

    现在直接打补丁到 ``provider_setting`` 本身:它是全系统唯一的读配置入口,
    补它一个就覆盖了所有分支,也不必关心底下到底装没装 pydantic。
    """
    from app.providers import _config

    original = _config.provider_setting
    overrides = {name: value}

    def patched(key: str, default: str = "") -> str:
        if key in overrides:
            return overrides[key]
        return original(key, default)

    # fashn.py 在 import 时就把这个名字绑进了自己的模块命名空间,两处都要换
    from app.providers import fashn

    _config.provider_setting = patched
    fashn.provider_setting = patched

    def restore() -> None:
        _config.provider_setting = original
        fashn.provider_setting = original

    return restore


# ---------- 配置 ----------

def test_base_url_defaults_to_the_official_endpoint():
    assert FashnImageGenerationProvider(api_key="k").base_url == DEFAULT_BASE_URL


def test_only_the_api_key_is_required():
    """端点在文档里是确定的,不该因为没填 BASE_URL 就显示未配置。"""
    assert FashnImageGenerationProvider(api_key="k", base_url="").is_configured() is True
    assert FashnImageGenerationProvider(api_key="", base_url="").is_configured() is False


def test_official_result_cdn_is_added_to_the_download_allowlist():
    provider = _provider()
    assert (
        provider.result_download_allowed_hosts(
            "https://cdn.fashn.ai/generated/result.png", "internal.example"
        )
        == "internal.example,cdn.fashn.ai"
    )


def test_result_cdn_trust_is_an_exact_hostname_boundary():
    provider = _provider()
    configured = "internal.example"
    assert (
        provider.result_download_allowed_hosts(
            "https://cdn.fashn.ai.evil.example/result.png", configured
        )
        == configured
    )
    assert (
        provider.result_download_allowed_hosts(
            "https://sub.cdn.fashn.ai/result.png", configured
        )
        == configured
    )


def test_official_result_cdn_survives_proxy_fake_ip_without_opening_the_range():
    """只放 FASHN 的精确域名;同一个 fake-IP 上的其他域名仍被拦截。"""
    provider = _provider()
    official_url = "https://cdn.fashn.ai/generated/result.png"
    allowed = provider.result_download_allowed_hosts(official_url)
    original = net_safety._resolve
    net_safety._resolve = lambda _host: [ipaddress.ip_address("198.18.0.145")]
    try:
        assert net_safety.check_download_url(official_url, allowed_hosts=allowed)
        expect_raises(
            net_safety.UnsafeDownloadURL,
            net_safety.check_download_url,
            "https://untrusted.example/result.png",
        )
    finally:
        net_safety._resolve = original


def test_capabilities_reflect_the_documented_features():
    caps = _provider().capabilities()
    assert caps.supports(GenerationMode.VIRTUAL_TRY_ON)
    assert caps.supports(GenerationMode.PRODUCT_TO_MODEL)
    assert caps.supports_seed is True
    assert caps.supports_webhook is True, "文档明确支持 /run?webhook_url="
    assert caps.supports_cancel is False, "文档里没有取消端点,不许假装支持"


# ---------- 批次拆分 ----------

def test_batch_sizes_splits_correctly():
    """同一个纯函数的输入输出表。"""
    #  想要几张, 每次最多几张 -> 各次提交的数量
    cases = [
        (4, 4, [4],          "模型支持一次多张,一次调用就够"),
        (4, 1, [1, 1, 1, 1], "product-to-model 没有 num_images,只能提交四次"),
        (6, 4, [4, 2],       "除不尽时最后一次是余数"),
        (0, 4, [],           "一张都不要就不该发请求"),
    ]
    for total, per_call, expected, why in cases:
        assert batch_sizes(total, per_call) == expected, f"batch_sizes({total}, {per_call}): {why}"


# ---------- 请求映射 ----------

def test_virtual_try_on_uses_tryon_max_field_names():
    inputs = run(_provider().build_inputs(_req(), TRYON_MAX, count=4, seed=7))
    assert set(inputs) >= {"model_image", "product_image", "num_images"}
    assert inputs["num_images"] == 4
    assert "garment_image" not in inputs, "garment_image 是 v1.6 的字段名"


def test_tryon_v16_uses_garment_image_and_num_samples():
    inputs = run(_provider().build_inputs(_req(), TRYON_V16, count=2, seed=7))
    assert "garment_image" in inputs
    assert inputs["num_samples"] == 2
    assert "product_image" not in inputs


def test_tryon_v16_does_not_receive_prompt_or_resolution():
    """v1.6 的参数表里没有这两项,多发只会换来 BadRequest。"""
    restore = _with_setting("FASHN_RESOLUTION", "2k")
    try:
        inputs = run(
            _provider().build_inputs(_req(prompt="影棚灯光"), TRYON_V16, count=1, seed=1)
        )
    finally:
        restore()
    assert "prompt" not in inputs
    assert "resolution" not in inputs


def test_product_to_model_never_sends_a_candidate_count_field():
    """文档的 product-to-model 参数表没有 num_images,不许凭空造一个字段名。"""
    inputs = run(
        _provider().build_inputs(
            _req(mode=GenerationMode.PRODUCT_TO_MODEL), PRODUCT_TO_MODEL, count=4, seed=1
        )
    )
    assert "num_images" not in inputs
    assert "num_samples" not in inputs
    assert PRODUCT_TO_MODEL.max_per_call == 1


def test_product_to_model_passes_model_image_to_enable_tryon_mode():
    inputs = run(
        _provider().build_inputs(
            _req(mode=GenerationMode.PRODUCT_TO_MODEL), PRODUCT_TO_MODEL, count=1, seed=1
        )
    )
    assert inputs["product_image"] == TINY
    assert inputs["model_image"] == TINY


def test_seed_is_wrapped_into_the_documented_range():
    """文档:seed 取值 0 到 2^32-1。"""
    inputs = run(_provider().build_inputs(_req(), TRYON_MAX, count=1, seed=2**33 + 5))
    assert 0 <= inputs["seed"] < 2**32


def test_seed_is_omitted_when_not_requested():
    inputs = run(_provider().build_inputs(_req(seed=None), TRYON_MAX, count=1, seed=None))
    assert "seed" not in inputs


def test_output_format_is_whitelisted():
    restore = _with_setting("FASHN_OUTPUT_FORMAT", "webp")
    try:
        inputs = run(_provider().build_inputs(_req(), TRYON_MAX, count=1, seed=1))
    finally:
        restore()
    assert "output_format" not in inputs, "文档只允许 png / jpeg"


def test_generation_mode_is_validated_against_the_model():
    """performance 是 v1.6 的档位,tryon-max 只有 balanced / quality。"""
    restore = _with_setting("FASHN_GENERATION_MODE", "performance")
    try:
        max_inputs = run(_provider().build_inputs(_req(), TRYON_MAX, count=1, seed=1))
        v16_inputs = run(_provider().build_inputs(_req(), TRYON_V16, count=1, seed=1))
    finally:
        restore()
    assert "generation_mode" not in max_inputs
    assert v16_inputs["mode"] == "performance"


def test_base64_results_are_never_requested():
    """结果要尽快落到自有存储(需求第十九章),base64 响应体巨大且 60 分钟过期。"""
    inputs = run(_provider().build_inputs(_req(), TRYON_MAX, count=1, seed=1))
    assert "return_base64" not in inputs


# ---------- 画幅 ----------

def test_aspect_ratio_snaps_to_an_allowed_value():
    assert nearest_aspect_ratio(768, 1024) == "3:4"
    assert nearest_aspect_ratio(1000, 1000) == "1:1"
    assert nearest_aspect_ratio(1920, 1080) == "16:9"


def test_every_snapped_ratio_is_in_the_documented_enum():
    for width, height in ((768, 1024), (1000, 1000), (1920, 1080), (900, 1600), (1200, 800)):
        assert nearest_aspect_ratio(width, height) in ASPECT_RATIOS


def test_aspect_ratio_is_omitted_without_a_requested_size():
    assert nearest_aspect_ratio(None, None) is None
    inputs = run(_provider().build_inputs(_req(), TRYON_MAX, count=1, seed=1))
    assert "aspect_ratio" not in inputs


# ---------- 素材 ----------

def test_data_uri_carries_the_required_prefix():
    """文档:base64 必须带 data:image/...;base64, 前缀,少了会被判 ImageLoadError。"""
    assert data_uri(b"x", "image/png").startswith("data:image/png;base64,")


def test_existing_data_uri_is_passed_through_untouched():
    inputs = run(_provider().build_inputs(_req(), TRYON_MAX, count=1, seed=1))
    assert inputs["product_image"] == TINY


def test_local_fashn_input_uses_storage_path_without_building_a_loopback_url():
    """任务编排与 FASHN 的接缝:本地素材不能绕成 localhost 再下载。"""
    reference = provider_input_reference(
        _provider(),
        storage_backend="local",
        storage_path="uploads/ab/product.png",
        # 本地分支若误建 URL,这条 lambda 会让测试当场失败。
        external_url=lambda: (_ for _ in ()).throw(AssertionError("不该构造 localhost URL")),
    )
    assert reference == "uploads/ab/product.png"


def test_s3_fashn_input_keeps_the_signed_url():
    reference = provider_input_reference(
        _provider(),
        storage_backend="s3",
        storage_path="uploads/ab/product.png",
        external_url=lambda: "https://storage.example/signed-product.png",
    )
    assert reference == "https://storage.example/signed-product.png"


def test_fashn_reads_a_local_storage_path_and_inlines_it():
    """上一条证明编排会传路径;这一条证明 Provider 真能消费那条路径。"""
    payload = b"\x89PNG\r\n\x1a\nlocal-image"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        relative = Path("uploads") / "product.png"
        (root / relative).parent.mkdir(parents=True)
        (root / relative).write_bytes(payload)

        fake_config = types.ModuleType("app.core.config")
        fake_config.settings = types.SimpleNamespace(storage_dir=root)
        missing = object()
        previous = sys.modules.get("app.core.config", missing)
        sys.modules["app.core.config"] = fake_config
        try:
            provider = FashnImageGenerationProvider(api_key="test-key", base_url="")
            prepared = run(provider._prepare_image(relative.as_posix()))
        finally:
            if previous is missing:
                sys.modules.pop("app.core.config", None)
            else:
                sys.modules["app.core.config"] = previous

    assert prepared == data_uri(payload, "image/png")


def test_empty_asset_url_is_rejected():
    provider = _provider()
    expect_raises(ProviderInputError, run, provider._prepare_image(""))


# ---------- 错误分类 ----------

def test_documented_api_errors_map_to_the_right_categories():
    cases = [
        (400, "BadRequest", ProviderInputError),
        (401, "UnauthorizedAccess", ProviderAuthError),
        (429, "RateLimitExceeded", ProviderRateLimitError),
        (429, "ConcurrencyLimitExceeded", ProviderRateLimitError),
        (429, "OutOfCredits", ProviderQuotaError),
        (500, "InternalServerError", ProviderError),
    ]
    for status_code, code, expected in cases:
        err = error_from_api(status_code, {"error": code, "message": "x"})
        assert isinstance(err, expected), f"{code} 应映射到 {expected.__name__}"


def test_unknown_api_error_falls_back_by_http_status():
    err = error_from_api(429, {"error": "SomethingNew", "message": "x"})
    assert isinstance(err, ProviderRateLimitError)


def test_out_of_credits_is_never_auto_retried():
    """限流等一会儿就好,余额用尽等多久都不会好 —— 必须有人去充值。"""
    err = error_from_api(429, {"error": "OutOfCredits", "message": "x"})
    assert err.policy.retriable is False
    assert err.policy.requires_human is True
    assert err.code is ErrorCode.QUOTA_EXHAUSTED


def test_documented_runtime_errors_map_to_the_right_categories():
    cases = [
        ("ImageLoadError", ProviderInputError),
        ("ContentModerationError", ProviderContentSafetyError),
        ("PoseError", ProviderInputError),
        ("InputValidationError", ProviderInputError),
        ("ThirdPartyError", ProviderGenerationError),
        ("3rdPartyProviderError", ProviderGenerationError),
        ("PipelineError", ProviderGenerationError),
        ("PollingTimeout", ProviderTimeoutError),
    ]
    for name, expected in cases:
        err = error_from_runtime({"name": name, "message": "x"})
        assert isinstance(err, expected), f"{name} 应映射到 {expected.__name__}"


def test_content_moderation_goes_straight_to_a_human():
    err = error_from_runtime({"name": "ContentModerationError", "message": "x"})
    assert err.policy.retriable is False
    assert err.policy.requires_human is True


def test_pipeline_error_is_retriable_because_failures_are_not_charged():
    err = error_from_runtime({"name": "PipelineError", "message": "x"})
    assert err.policy.retriable is True


def test_unknown_runtime_error_is_treated_as_a_generation_failure():
    err = error_from_runtime({"name": "BrandNewError", "message": "x"})
    assert isinstance(err, ProviderGenerationError)


def test_error_messages_never_contain_the_api_key():
    err = error_from_api(401, {"error": "UnauthorizedAccess", "message": "bad key"})
    assert "test-key" not in str(err.message)


# ---------- 状态 ----------

def test_status_map_covers_every_documented_status():
    documented = {"starting", "in_queue", "processing", "completed", "failed",
                  "canceled", "time_out"}
    assert documented <= set(STATUS_MAP)


def test_terminal_statuses_are_mapped_correctly():
    assert STATUS_MAP["completed"] is ExternalTaskStatus.SUCCEEDED
    assert STATUS_MAP["failed"] is ExternalTaskStatus.FAILED
    assert STATUS_MAP["in_queue"] is ExternalTaskStatus.PENDING
    assert STATUS_MAP["processing"] is ExternalTaskStatus.RUNNING


def test_partial_success_still_counts_as_success():
    """4 张里坏了 1 张,不该让另外 3 张陪葬。"""
    statuses = [ExternalTaskStatus.SUCCEEDED, ExternalTaskStatus.FAILED]
    assert aggregate_status(statuses) is ExternalTaskStatus.SUCCEEDED


def test_any_unfinished_prediction_keeps_the_batch_running():
    statuses = [ExternalTaskStatus.SUCCEEDED, ExternalTaskStatus.RUNNING]
    assert aggregate_status(statuses) is ExternalTaskStatus.RUNNING


def test_all_failed_is_a_failed_batch():
    assert aggregate_status([ExternalTaskStatus.FAILED] * 3) is ExternalTaskStatus.FAILED


def test_empty_batch_is_unknown():
    assert aggregate_status([]) is ExternalTaskStatus.UNKNOWN


def test_composite_external_id_round_trips():
    ids = ["a1", "b2", "c3"]
    joined = ",".join(ids)
    assert FashnImageGenerationProvider.split_external_id(joined) == ids
    assert FashnImageGenerationProvider.split_external_id("") == []


# ---------- 模型选择 ----------

def test_product_to_model_mode_selects_the_product_to_model_endpoint():
    assert model_for(GenerationMode.PRODUCT_TO_MODEL).model_name == "product-to-model"


def test_try_on_defaults_to_the_flagship_model():
    assert model_for(GenerationMode.VIRTUAL_TRY_ON).model_name == "tryon-max"


def test_try_on_model_is_switchable_by_configuration():
    restore = _with_setting("FASHN_TRYON_MODEL", "tryon-v1.6")
    try:
        assert model_for(GenerationMode.VIRTUAL_TRY_ON).model_name == "tryon-v1.6"
    finally:
        restore()


def test_unknown_try_on_model_falls_back_to_the_flagship():
    restore = _with_setting("FASHN_TRYON_MODEL", "tryon-v99")
    try:
        assert model_for(GenerationMode.VIRTUAL_TRY_ON).model_name == "tryon-max"
    finally:
        restore()


# ---------- 未配置 ----------

def test_submit_without_a_key_never_touches_the_network():
    from app.providers.errors import NotConfiguredError

    provider = FashnImageGenerationProvider(api_key="", base_url="")
    err = expect_raises(NotConfiguredError, run, provider.submit(_req()))
    assert err.http_status == 409
    assert "FASHN_API_KEY" in err.message


def test_try_on_without_a_model_image_is_rejected_before_submitting():
    provider = _provider()
    err = expect_raises(ProviderInputError, run, provider.submit(_req(model_image_url=None)))
    assert "模特图" in err.message
