"""错误分类与重试策略。需求第七章:不同错误类型采用不同的重试和切换策略。"""
from __future__ import annotations

from app.core.errors import ErrorCode
from app.providers.errors import (
    NotConfiguredError,
    ProviderAuthError,
    ProviderContentSafetyError,
    ProviderError,
    ProviderGenerationError,
    ProviderInputError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ResultDownloadError,
    policy_for,
)
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401

ALL_ERRORS = [
    NotConfiguredError, ProviderInputError, ProviderAuthError, ProviderRateLimitError,
    ProviderTimeoutError, ProviderContentSafetyError, ProviderGenerationError, ResultDownloadError,
]


def test_every_error_type_has_a_distinct_code():
    codes = [cls.code for cls in ALL_ERRORS]
    assert len(set(codes)) == len(codes)


def test_every_error_declares_a_policy():
    for cls in ALL_ERRORS:
        policy = cls.policy
        assert isinstance(policy.retriable, bool)
        assert policy.max_retries >= 0
        assert policy.backoff_seconds >= 0


def test_retry_policy_table():
    """错误码 -> 策略字段的映射表。

    每条策略背后都有具体理由,写在下面的 why 里:
      - 输入错误:同一张图换谁都错,切换只是浪费配额
      - 限流:退避后重试,也值得换一家
      - 鉴权失败 / 内容安全:自动重试只会重复触发并烧掉配额,必须转人工
      - 结果下载失败:地址已经拿到,换 Provider 要整轮重生,先重试更划算
      - 未配置:换一家可能配好了,但重试同一家没有意义

    (原先是十来个函数、每个只有一两条断言;合并成一张表之后,加一种错误
    只需要加一行,而且忘了加会立刻在 test_every_policy_is_in_the_table 红。)
    """
    #     错误类型                      retriable switchable requires_human  why
    table = [
        (ProviderInputError,            False, False, None,  "换谁都错"),
        (ProviderRateLimitError,        True,  True,  None,  "退避后重试,也可换家"),
        (ProviderAuthError,             False, None,  True,  "Key 不对,人来改"),
        (ProviderContentSafetyError,    False, False, True,  "重试只会重复被拦"),
        (ProviderTimeoutError,          True,  True,  False, "瞬时故障,重试或换家"),
        (ProviderGenerationError,       True,  True,  False, "生成失败,重试或换家"),
        (ResultDownloadError,           True,  False, None,  "先重试,别整轮重生"),
        (NotConfiguredError,            False, True,  None,  "换一家,别重试同一家"),
    ]
    for cls, retriable, switchable, requires_human, why in table:
        policy = cls.policy
        assert policy.retriable is retriable, f"{cls.__name__}: {why}"
        if switchable is not None:
            assert policy.switchable is switchable, f"{cls.__name__}: {why}"
        if requires_human is not None:
            assert policy.requires_human is requires_human, f"{cls.__name__}: {why}"

    # 声明不可重试却配了重试次数,是最容易被忽略的配置矛盾
    for cls in ALL_ERRORS:
        if not cls.policy.retriable:
            assert cls.policy.max_retries == 0, f"{cls.__name__} 策略自相矛盾"

    # 数量级约定:限流要退够久,下载失败要重试够多次
    assert ProviderRateLimitError.policy.backoff_seconds >= 5
    assert ResultDownloadError.policy.max_retries >= 3


def test_every_policy_is_in_the_table():
    """新增错误类型必须同时补进上面的策略表,否则它的策略没人看过。"""
    import inspect as _inspect

    source = _inspect.getsource(test_retry_policy_table)
    missing = [cls.__name__ for cls in ALL_ERRORS if cls.__name__ not in source]
    assert not missing, f"这些错误类型没有进策略表: {missing}"


def test_policy_lookup_by_code():
    assert policy_for(ErrorCode.RATE_LIMITED) is ProviderRateLimitError.policy
    assert policy_for("NETWORK_TIMEOUT") is ProviderTimeoutError.policy


def test_unknown_code_falls_back_to_conservative_policy():
    assert policy_for("SOMETHING_WE_NEVER_SAW") is ProviderError.policy


def test_error_carries_provider_name_for_diagnosis():
    err = ProviderTimeoutError("超时了", provider="comfyui")
    assert err.detail["provider"] == "comfyui"
    assert err.to_payload()["error"]["code"] == "NETWORK_TIMEOUT"


def test_error_payload_does_not_leak_internals():
    err = ProviderGenerationError("生成失败", provider="mock", detail={"trace": "secret-internal"})
    payload = err.to_payload()
    assert set(payload["error"]) == {"code", "message"}
