"""Provider 错误分类。需求第七章:不同错误类型采用不同的重试与切换策略。

关键判断在两个布尔上:
- ``retriable``  同一个 Provider 再试一次有没有意义;
- ``switchable`` 换一个 Provider 有没有意义。

举例:限流是 retriable 也 switchable;输入错误两者皆否(同样的图换谁都错);
内容安全两者皆否且必须人工看,自动重试只会烧钱。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import AppError, ErrorCode


@dataclass(frozen=True)
class RetryPolicy:
    retriable: bool
    switchable: bool
    max_retries: int
    backoff_seconds: float
    #: 出现即停止自动流程、转人工
    requires_human: bool = False


class ProviderError(AppError):
    """所有 Provider 错误的基类。"""

    code = ErrorCode.PROVIDER_SERVICE_ERROR
    http_status = 502
    policy = RetryPolicy(retriable=True, switchable=True, max_retries=2, backoff_seconds=2.0)

    def __init__(self, message: str, *, provider: str = "", detail: dict | None = None) -> None:
        super().__init__(message, detail={"provider": provider, **(detail or {})})
        self.provider = provider


class NotConfiguredError(ProviderError):
    """Provider 缺少必要配置。绝不重试,也不该被路由选中。"""

    code = ErrorCode.PROVIDER_NOT_CONFIGURED
    http_status = 409
    policy = RetryPolicy(retriable=False, switchable=True, max_retries=0, backoff_seconds=0)


class ProviderInputError(ProviderError):
    """请求参数或素材不被 Provider 接受。换 Provider 通常也一样错。"""

    code = ErrorCode.INPUT_INVALID
    http_status = 422
    policy = RetryPolicy(retriable=False, switchable=False, max_retries=0, backoff_seconds=0)


class ProviderAuthError(ProviderError):
    """鉴权失败。重试无意义,必须人工换 Key。"""

    code = ErrorCode.AUTH_FAILED
    http_status = 502
    policy = RetryPolicy(
        retriable=False, switchable=True, max_retries=0, backoff_seconds=0, requires_human=True
    )


class ProviderRateLimitError(ProviderError):
    """限流。退避后重试,或立刻换一家。"""

    code = ErrorCode.RATE_LIMITED
    http_status = 429
    policy = RetryPolicy(retriable=True, switchable=True, max_retries=3, backoff_seconds=10.0)


class ProviderQuotaError(ProviderError):
    """账户余额或配额用尽。

    和限流的区别很实在:限流等一会儿就好,配额用尽等多久都不会好 ——
    必须有人去充值。所以它 retriable=False 且 requires_human=True,
    否则退避重试只会把这一轮的时间白白烧掉。
    """

    code = ErrorCode.QUOTA_EXHAUSTED
    http_status = 402
    policy = RetryPolicy(
        retriable=False, switchable=True, max_retries=0, backoff_seconds=0, requires_human=True
    )


class ProviderTimeoutError(ProviderError):
    """网络超时。

    注意:超时不代表对方没收到请求。恢复流程必须先查外部任务状态再决定是否重提,
    否则会重复计费并产生孤儿任务(需求第九章)。
    """

    code = ErrorCode.NETWORK_TIMEOUT
    http_status = 504
    policy = RetryPolicy(retriable=True, switchable=True, max_retries=2, backoff_seconds=5.0)


class ProviderContentSafetyError(ProviderError):
    """内容安全拦截。自动重试只会重复触发,直接转人工。"""

    code = ErrorCode.CONTENT_SAFETY
    http_status = 422
    policy = RetryPolicy(
        retriable=False, switchable=False, max_retries=0, backoff_seconds=0, requires_human=True
    )


class ProviderGenerationError(ProviderError):
    """Provider 明确报告生成失败。换 seed 或换 Provider 都有机会。"""

    code = ErrorCode.GENERATION_FAILED
    policy = RetryPolicy(retriable=True, switchable=True, max_retries=2, backoff_seconds=1.0)


class ResultDownloadError(ProviderError):
    """结果地址拿到了但下不下来。多为临时网络问题,重试优先于换家。"""

    code = ErrorCode.RESULT_DOWNLOAD_FAILED
    policy = RetryPolicy(retriable=True, switchable=False, max_retries=3, backoff_seconds=2.0)


#: 错误码 -> 异常类,供从持久化的 error_code 还原策略
ERROR_BY_CODE: dict[ErrorCode, type[ProviderError]] = {
    cls.code: cls
    for cls in (
        NotConfiguredError,
        ProviderInputError,
        ProviderAuthError,
        ProviderRateLimitError,
        ProviderQuotaError,
        ProviderTimeoutError,
        ProviderContentSafetyError,
        ProviderGenerationError,
        ResultDownloadError,
        ProviderError,
    )
}


def policy_for(code: ErrorCode | str) -> RetryPolicy:
    """按错误码取重试策略。未知错误按最保守的基类策略处理。"""
    try:
        return ERROR_BY_CODE.get(ErrorCode(code), ProviderError).policy
    except ValueError:
        return ProviderError.policy
