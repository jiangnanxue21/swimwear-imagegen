"""领域错误定义。本模块仅使用标准库,任何层都可安全导入。

错误分类对应需求第七章:配置/输入/鉴权/限流/网络超时/Provider服务/内容安全/生成失败/结果下载。
阶段 1-2 只会真正抛出配置类与输入类错误,其余枚举先声明,供阶段 3+ 使用。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    # 配置类
    CONFIG_INVALID = "CONFIG_INVALID"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    # 输入类
    INPUT_INVALID = "INPUT_INVALID"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    IMAGE_UNREADABLE = "IMAGE_UNREADABLE"
    IMAGE_TOO_SMALL = "IMAGE_TOO_SMALL"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    DUPLICATE_RESOURCE = "DUPLICATE_RESOURCE"
    NOT_FOUND = "NOT_FOUND"
    # 阶段 3+ 预留,当前不抛出
    AUTH_FAILED = "AUTH_FAILED"
    #: 身份**有效**,但角色不够(operator 去动 admin 才能动的东西)。
    #:
    #: 和 AUTH_FAILED 分开是为了让前端说对话:两者都是 4xx,但一个的意思是
    #: 「你的口令不对,去核对」,另一个是「你的口令没问题,只是这件事你做不了」。
    #: 混用同一个码时,前端的全局横幅会对着一把**完全正确**的操作口令说
    #: 「后端不认这把口令」,诱导运营反复去改一个本来就对的东西。
    AUTH_FORBIDDEN = "AUTH_FORBIDDEN"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    PROVIDER_SERVICE_ERROR = "PROVIDER_SERVICE_ERROR"
    CONTENT_SAFETY = "CONTENT_SAFETY"
    GENERATION_FAILED = "GENERATION_FAILED"
    RESULT_DOWNLOAD_FAILED = "RESULT_DOWNLOAD_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # ------------------------------------------------------------------
    # 阶段 9-13(M0 契约):素材 / 属性 / 刊登 / 渠道 / 发布
    #
    # 这一批现在**一个都不会被抛出** —— M0 只定契约,实现在 M1 之后。
    # 先声明是为了让契约文档、前端错误提示表和 spec 校验器有同一份来源;
    # 等实现落地时不必再回来改一次枚举,也不会出现两处各写一个字符串。
    # ------------------------------------------------------------------
    # 素材与合规
    MEDIA_CONSENT_DENIED = "MEDIA_CONSENT_DENIED"          # 授权不覆盖本次调用,拒绝且不降级
    MEDIA_QUARANTINED = "MEDIA_QUARANTINED"                # 素材已被隔离,不参与上架
    # 属性识别
    ATTRIBUTE_EXTRACTION_FAILED = "ATTRIBUTE_EXTRACTION_FAILED"
    ATTRIBUTE_NOT_CALIBRATED = "ATTRIBUTE_NOT_CALIBRATED"  # 未校准,不允许自动确认
    # 文案
    COPY_COMPLIANCE_FAILED = "COPY_COMPLIANCE_FAILED"
    COPY_CLAIM_MISMATCH = "COPY_CLAIM_MISMATCH"            # claims 与 facts 不一致
    # 图片集与草稿
    IMAGE_SET_NOT_APPROVED = "IMAGE_SET_NOT_APPROVED"
    DRAFT_STALE = "DRAFT_STALE"                            # 上游变更,草稿过期
    # 渠道规格
    CHANNEL_SPEC_INCOMPLETE = "CHANNEL_SPEC_INCOMPLETE"    # spec 里还有 TODO,生产环境拒绝启动
    CHANNEL_SOURCE_EXPR_INVALID = "CHANNEL_SOURCE_EXPR_INVALID"
    CHANNEL_FIELD_MISSING = "CHANNEL_FIELD_MISSING"
    CHANNEL_TEMPLATE_MISMATCH = "CHANNEL_TEMPLATE_MISMATCH"
    CHANNEL_TEMPLATE_FIDELITY_FAILED = "CHANNEL_TEMPLATE_FIDELITY_FAILED"
    CHANNEL_AUTH_EXPIRED = "CHANNEL_AUTH_EXPIRED"
    CHANNEL_RATE_LIMITED = "CHANNEL_RATE_LIMITED"
    # 发布
    PUBLISH_RESULT_UNKNOWN = "PUBLISH_RESULT_UNKNOWN"      # 提交超时,**不自动重试**
    PUBLISH_DUPLICATE_CREATE = "PUBLISH_DUPLICATE_CREATE"  # 已有 external_spu_id 却试图 CREATE
    PUBLISH_REJECTED = "PUBLISH_REJECTED"
    WEBHOOK_SIGNATURE_INVALID = "WEBHOOK_SIGNATURE_INVALID"

    # ------------------------------------------------------------------
    # 阶段 4(验收收口)
    # ------------------------------------------------------------------
    #: 同一作用域正在被另一个请求执行。**结构化的码,不靠消息片段匹配。**
    #: §7.3 把"重复触发付费模型调用"列为 P0,而 P0 的失败必须能被归类 ——
    #: 如果这条路径落到 `INPUT_INVALID`,批次里就会出现一个
    #: `UNEXPLAINED`,而那个数字必须为 0 才能签字(§6.3.2)。
    CONCURRENT_EXECUTION = "CONCURRENT_EXECUTION"


@dataclass(frozen=True)
class FieldProblem:
    """一条能指到具体输入位置的问题。

    ## 形状不是新发明的,是抄的

    `main.py` 的 `RequestValidationError` 处理器已经在发
    ``{"loc": ..., "msg": ...}``,`StarletteHTTPException` 处理器把结构化
    detail 原样放进同一个 `fields`,前端 `api/client.ts` 读的也是这一个。
    唯一不发它的是 `AppError.to_payload()`。

    所以这里**不许**因为上游异常的属性叫 `field`(单数)就造第二种形状:
    两种形状意味着前端要写两套解析,而漏掉的那一套表现是错误提示变成
    `undefined` —— `http_error_handler` 顶部记的正是这件事。
    """

    #: 出问题的位置。点分路径,与 pydantic 的 `loc` 同源(``color_variants[0].variant_code``)
    loc: str
    #: 给人看的那句话
    msg: str


class AppError(Exception):
    """所有业务错误的基类。

    ``message`` 会返回给客户端,因此禁止在其中拼接数据库异常原文(需求第十九章)。
    ``detail`` 仅进日志。**要让前端看见的字段位置走 ``fields``,不要塞进 ``detail``**
    —— 两者的可见性不同,而混用的表现是"日志里明明有,前端就是拿不到"。
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        http_status: int | None = None,
        detail: dict | None = None,
        fields: list[FieldProblem] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.detail = detail or {}
        self.fields = list(fields or [])

    def to_payload(self) -> dict:
        """返回给客户端的错误体。

        ``fields`` **为空时整个键不出现**,而不是给一个空数组:前端
        `client.ts` 用 ``body?.error?.fields ?? []`` 取值,两种写法它都吃得下,
        但空数组会让"这个错误没有字段位置"和"有位置但一条都没算出来"
        长得一模一样。
        """
        payload: dict = {"error": {"code": str(self.code), "message": self.message}}
        if self.fields:
            payload["error"]["fields"] = [
                {"loc": item.loc, "msg": item.msg} for item in self.fields
            ]
        return payload


class ValidationError(AppError):
    code = ErrorCode.INPUT_INVALID
    http_status = 422


class NotFoundError(AppError):
    code = ErrorCode.NOT_FOUND
    http_status = 404


class DuplicateError(AppError):
    code = ErrorCode.DUPLICATE_RESOURCE
    http_status = 409


class ConfigError(AppError):
    code = ErrorCode.CONFIG_INVALID
    http_status = 500


class NotConfiguredError(AppError):
    """Provider 未配置。阶段 3 起由 Provider 层抛出。"""

    code = ErrorCode.PROVIDER_NOT_CONFIGURED
    http_status = 409


class ConcurrentTransition(AppError):
    """状态转移在写库那一刻被别人抢先了。

    单独一类,是因为它**不是错误**,而是一个正常且必须被尊重的结果。
    典型场景:worker 在等 Provider 回包的十几秒里,用户点了取消。取消已经
    落库,worker 手里的 ORM 对象却还是旧状态,它接着写下去就会把 CANCELLED
    覆盖回 SUBMITTING —— 取消看起来生效了,任务却继续跑、继续花钱。

    带条件的 UPDATE 会在这种情况下改到 0 行,于是抛这个异常。调用方该做的是
    **停下来**,不是重试:数据库已经告诉你,世界和你以为的不一样了。
    """

    code = ErrorCode.INPUT_INVALID
    http_status = 409


class ManualReviewRequired(AppError):
    """自动流程走不下去,必须转人工审核 —— 而且**不允许重新生成图片**。

    为什么要单独一类:这些情况有一个共同点 —— 问题出在\"我们判断不了\",
    不在\"图片不好\"。而流水线在\"图片不好\"时的标准动作是重生一轮,
    那要再花一次 Provider 的钱。把两者混在一起的代价非常具体:
    评分器一直返回不了结果,系统就会一轮接一轮地重新生成图片去\"修\"
    一个根本不在图片上的问题,直到轮次耗尽。

    候选图已经生成并入库了,人可以直接在审核页上看图决定 —— 这条路是通的,
    比烧钱重生便宜得多,也比让任务失败有用得多。

    ``retry_scoring`` 为 True 时表示这是一次**短暂**故障(限流、超时),
    编排层应该重新排一次评分,而不是立刻转人工。
    """

    code = ErrorCode.CONFIG_INVALID
    http_status = 409

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        http_status: int | None = None,
        detail: dict | None = None,
        retry_scoring: bool = False,
    ) -> None:
        super().__init__(message, code=code, http_status=http_status, detail=detail)
        self.retry_scoring = retry_scoring


class EvaluatorUnavailableError(ManualReviewRequired):
    """配置的评分器用不了,且当前环境不允许退回 Mock。

    单独成类是因为它的处理方式和别的配置错误不同:**不让任务失败,转人工审核**。
    没有评分器时人还能看图,自动发布一张没被真正评过的图才是最坏结果。
    """


class ReferenceImagesMissing(ManualReviewRequired):
    """没有合格的商品参考图,无法判断\"生成的还是不是这件衣服\"。

    以前这里会退回\"该任务的全部素材\",于是模特参考图、背景图都可能成为
    商品一致性的比对基准 —— 评分器会认认真真地拿模特照片当商品去比,
    给出的分数看着正常,却毫无意义。宁可转人工。
    """

    code = ErrorCode.INPUT_INVALID


class EvaluationScoringFailed(ManualReviewRequired):
    """整轮候选图都没能评出结果。

    和\"这轮图都是 D 档\"必须分开:后者说明图不行,该重生;前者说明我们没看成,
    重生一次只会再拿到一批同样评不了的图,还要再付一次生成费用。
    """

    code = ErrorCode.INTERNAL_ERROR
