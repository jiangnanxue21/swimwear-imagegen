"""批量执行判定(阶段 3,任务书 §6)。**零数据库依赖纯函数。**

阶段 3 的退出条件(§6.3)有四条是"判定"而不是"界面":

    A 批次 20 件完整成功率 ≥95%          -> 计数要按批次分开算
    成功或**可解释失败** ≥95%,不可解释 = 0 -> "可解释"必须是程序判定,不是事后争论
    单件失败不导致整个批量任务失败        -> 执行循环的结构保证
    失败项可单独重试且不重复触发付费调用   -> 幂等键 + 回执

这四条全部落在本文件,和 flow.py 同样的理由:它们需要被穷举测试,
而只要能查库就没人会写那个穷举测试。`batch_service.py` 负责查库与真正
执行动作,本文件负责"该不该执行、算不算成功、能不能重试"。

## 前置条件不另发明一套规则

批量动作的可执行性**完全由 flow.py 已经算出的「唯一下一步」决定**
(`ACTION_PRECONDITION`)。这不是省事:如果这里再写一遍"属性确认了吗、
图片集批准了吗",那么列表页显示"下一步:确认属性"而批量导出却说
"可以执行"的情况迟早出现,而这正是任务书 §3.4 明令禁止的两个数字。

于是"不可执行"的理由天然可解释:**当前唯一下一步是 X,而你要做 Y**。
运营看到这句话就知道该去哪儿——§6.3.1 的第三个条件(运营无需开发协助
即可判断下一步动作)因此是结构保证的。

## 一个作用域只做一次

图片集与草稿按 SPU 存,文案按 (SPU, 颜色) 存(见 service.py 的
`_current_*` 与迁移 0051)。选中同一 SPU 下 3 个同色 SKU 去批量生成文案,
朴素实现会调三次文案模型、写三个版本、最后两次覆盖前一次——
**付费三次,结果一份**。

所以计划阶段按 `scope_key` 去重:同作用域在一个批次里只留一件执行,
其余标记 `SAME_SPU_DEDUPED` 跳过。跳过不是失败,它在界面上单独一列。

**去重键必须和存储的槽位一样细。** 文案原来按 SPU 去重,而它的槽位
自 0051 起带颜色 —— 三颜色 SPU 的批量生成只给第一个颜色出稿,另外两个
被跳过,界面上读起来完全正常。粗一档漏做、细一档白花钱,两侧都不报错,
所以这两个粒度是**同一个决定**,改一处必须改另一处(§3.54)。
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.core import vocab as _vocab
from app.workbench.flow import FlowStep, NextActionCode


class BatchAction(StrEnum):
    """四个批量动作(FE-301:批量识别、生成文案、校验草稿、导出)。

    刻意**没有**"批量批准":批准是人对内容负责的动作,批量批准等于
    把审核这一步变成一次点击。§6.3 要的是批量把商品推到"等人批准",
    不是替人批准。
    """

    EXTRACT = "EXTRACT"                # 批量属性识别(付费:BE-201)
    GENERATE_COPY = "GENERATE_COPY"    # 批量生成文案(付费:BE-202)
    VALIDATE_DRAFT = "VALIDATE_DRAFT"  # 批量生成/重建草稿并校验
    EXPORT = "EXPORT"                  # 批量导出上架文件


BATCH_ACTION_LABELS: Mapping[BatchAction, str] = {
    BatchAction.EXTRACT: "批量属性识别",
    BatchAction.GENERATE_COPY: "批量生成文案",
    BatchAction.VALIDATE_DRAFT: "批量校验草稿",
    BatchAction.EXPORT: "批量导出",
}

#: 会触发按次计费模型调用的动作。§7.3 的 P0 示例"重复触发付费模型调用"
#: 只可能出现在这两个上,重试守卫因此对它们最严格。
PAID_ACTIONS: frozenset[BatchAction] = frozenset(
    {BatchAction.EXTRACT, BatchAction.GENERATE_COPY}
)

#: 动作作用在哪一级对象上。同一批次里,同一作用域只执行一次。
#:
#: 属性识别是逐图跑的,而素材挂在 SKU 上,所以它按 SKU;
#: 图片集 / 草稿两张表以 spu 为 scope(见 service.py),所以按 SPU。
#:
#: **文案按 (SPU, 颜色) —— 不是按 SPU**(迁移 0051 / AC-11 第二句,
#: A45-batch24)。`listing_copies` 的槽位从 0051 起带颜色维,而文案内容
#: 本来就是颜色相关的(标题模板第一段是颜色)。仍按 SPU 去重的话,
#: 一个三颜色 SPU 的批量生成只会给**第一个**颜色出稿,另外两个颜色
#: 被记成 `SAME_SPU_DEDUPED` 跳过 —— 界面显示"已跳过 2 件、成功 1 件",
#: 读起来完全正常,而那两个颜色在导出时才发现没有文案。
#: 这是"去重键比存储粗一档"的反向形态,与 §3.54 记的那笔是同一件事。
ACTION_SCOPE: Mapping[BatchAction, str] = {
    BatchAction.EXTRACT: "product",
    BatchAction.GENERATE_COPY: "spu_colour",
    BatchAction.VALIDATE_DRAFT: "spu",
    BatchAction.EXPORT: "spu",
}

#: 动作归到哪个流程步骤。只在**与具体动作有关、但与具体码无关**的失败上用到
#: (目前是 `CONCURRENT_IN_FLIGHT`):同一个码在批量识别时该落属性、
#: 在批量导出时该落草稿,而 `ErrorMeta.category` 是按码定死的,表达不了这件事。
#: FE-303 的异常列表按步骤分组,归错组等于让运营在属性那一栏里找导出的问题。
ACTION_STEP: Mapping[BatchAction, FlowStep] = {
    BatchAction.EXTRACT: FlowStep.ATTRIBUTE,
    BatchAction.GENERATE_COPY: FlowStep.COPY,
    BatchAction.VALIDATE_DRAFT: FlowStep.DRAFT,
    BatchAction.EXPORT: FlowStep.DRAFT,
}


class BatchItemStatus(StrEnum):
    """一个批次条目的状态(FE-302 的五个计数就是它的分布)。

    `NEEDS_HUMAN` 与 `FAILED` 必须分开:前者是系统正确地停下来等人做决定
    (草稿字段缺必填、属性有冲突),重试一百次结果都一样;后者是这一次
    执行没成功,重试有意义。混在一起的后果是运营对着一堆"失败"反复点重试,
    而真正该做的是去补一个字段。
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    SKIPPED = "SKIPPED"


BATCH_ITEM_STATUS_LABELS: Mapping[BatchItemStatus, str] = {
    BatchItemStatus.PENDING: "待处理",
    BatchItemStatus.RUNNING: "处理中",
    BatchItemStatus.SUCCEEDED: "成功",
    BatchItemStatus.FAILED: "失败",
    BatchItemStatus.NEEDS_HUMAN: "需人工",
    BatchItemStatus.SKIPPED: "已跳过",
}

#: 终态。批次跑完之后条目只可能落在这几个上。
TERMINAL_STATUSES: frozenset[BatchItemStatus] = frozenset(
    {
        BatchItemStatus.SUCCEEDED,
        BatchItemStatus.FAILED,
        BatchItemStatus.NEEDS_HUMAN,
        BatchItemStatus.SKIPPED,
    }
)

#: 还没跑完的条目状态。**从终态反推**,不另抄一份清单(A34)。
#:
#: `liveness()` 拿它数"还有多少件没做",而那个数决定终态 `job.status`
#: 还算不算数。抄一份的后果是将来加一个中间状态时,这里会把它当成
#: "已经跑完了",于是重试路径上的判定又会退回 SETTLED。
UNFINISHED_STATUSES: frozenset[BatchItemStatus] = frozenset(
    s for s in BatchItemStatus if s not in TERMINAL_STATUSES
)


class BatchJobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"          # 全部条目到终态,且无失败
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"  # 有失败但批次跑完了


BATCH_JOB_STATUS_LABELS: Mapping[BatchJobStatus, str] = {
    BatchJobStatus.QUEUED: "排队中",
    BatchJobStatus.RUNNING: "执行中",
    BatchJobStatus.COMPLETED: "已完成",
    BatchJobStatus.COMPLETED_WITH_ERRORS: "完成(有失败)",
}


# ==========================================================
# 异常码:§6.3.1 「可解释失败」的判定基础
# ==========================================================


class BatchErrorCode(StrEnum):
    """预定义异常码。

    §6.3.1 第一条要求"映射到预定义的异常码,而非泛化的『处理失败』或
    『系统错误』"。于是这里**没有**一个叫 `FAILED` 的通用码,只有
    `UNEXPLAINED` —— 而它存在的意义恰恰相反:它是一个必须为 0 的计数器。

    退出条件写的是"不可解释失败数为 0 时方可签字"。让系统自己数这个数,
    比在验收会上逐条争论"这算不算可解释"可靠得多。任何走到 `UNEXPLAINED`
    的失败都是一个"该加异常码但没加"的缺口,它会在界面上以红字出现。
    """

    # ---- 前置不满足(不是失败,是还没到时候)----
    PRECHECK_MISMATCH = "PRECHECK_MISMATCH"
    SAME_SPU_DEDUPED = "SAME_SPU_DEDUPED"
    ALREADY_EXPORTED = "ALREADY_EXPORTED"

    # ---- 素材 ----
    #: 这一行 SKU 说不出自己属于哪个 SPU / 哪个颜色(§4.4,阶段 6 的建档步)。
    #:
    #: **不复用 `NO_USABLE_MATERIAL`**:那个类别会把运营带到素材页去传图,
    #: 而归属没挂好之前传的图会绑到错的作用域上 —— 传得越多错得越多。
    OWNERSHIP_MISSING = "OWNERSHIP_MISSING"
    #: 这个颜色还没有生效的生成方案(阶段 6 的方案步)。
    #:
    #: 与图片集类别分开:图片集没批准是"图有了没过审",没有方案是"还没决定
    #: 用哪个模特、什么参数" —— 前者去审图,后者去选方案,两个页面。
    NO_ACTIVE_PLAN = "NO_ACTIVE_PLAN"
    NO_USABLE_MATERIAL = "NO_USABLE_MATERIAL"

    #: 素材角色未经人工确认,§6.2 的完整度门禁不认(A45-batch14-10)。
    #:
    #: 它自成一档而不是并进 `NO_USABLE_MATERIAL`:两者的下一步不同 ——
    #: 那个码的建议是「补图或放行隔离素材」,而这条动线上两个动作都是错的。
    #: 补进来的新图默认同样没确认,运营会再撞一次;放行隔离素材则完全
    #: 答非所问,这条路径上被隔离的可能是 0 条。
    #: 合成一条会把运营送去做一件解决不了问题的事。
    ASSET_ROLE_UNCONFIRMED = "ASSET_ROLE_UNCONFIRMED"

    # ---- 受众(PRD v2 §8.2 / §9.4)----
    #: 它自成一档而不是并进 NO_CONFIRMED_ATTRIBUTES:两者的下一步不同 ——
    #: 受众未确认要去商品资料里选受众,属性未确认要去属性页逐项确认。
    #: 合成一条会把运营送去错的那一页
    AUDIENCE_UNCONFIRMED = "AUDIENCE_UNCONFIRMED"

    # ---- 属性 ----
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    ATTRIBUTE_CONFLICT = "ATTRIBUTE_CONFLICT"
    NO_CONFIRMED_ATTRIBUTES = "NO_CONFIRMED_ATTRIBUTES"

    #: 识别跑过了,但结论按 §6.4 的 fail-closed 一律不采信(未校准 /
    #: 置信度低于下限),确认队列是空的(A45-batch14-11,PRD §11)。
    #:
    #: 它自成一档而不是并进 `NO_CONFIRMED_ATTRIBUTES`:那个码的建议是
    #: 「到属性标签页逐项确认」,而这条动线上**没有任何东西可确认**。
    #: 也不能并进 `EXTRACTION_FAILED` —— 识别没有失败,它成功地告诉了我们
    #: 「这批结论不够格」,而 `retryable=True` 会让批次自动再花一次钱。
    ATTRIBUTE_NOT_CREDIBLE = "ATTRIBUTE_NOT_CREDIBLE"

    # ---- 图片集 ----
    IMAGE_SET_NOT_APPROVED = "IMAGE_SET_NOT_APPROVED"

    # ---- 文案 ----
    COPY_GENERATION_FAILED = "COPY_GENERATION_FAILED"
    COPY_RULE_VIOLATION = "COPY_RULE_VIOLATION"
    COPY_NOT_APPROVED = "COPY_NOT_APPROVED"

    # ---- 草稿与导出 ----
    DRAFT_PRECONDITION = "DRAFT_PRECONDITION"
    DRAFT_FIELD_INVALID = "DRAFT_FIELD_INVALID"
    DRAFT_STALE = "DRAFT_STALE"
    DRAFT_NOT_EXPORTABLE = "DRAFT_NOT_EXPORTABLE"
    SPEC_VERSION_MIXED = "SPEC_VERSION_MIXED"
    EXPORT_WRITE_FAILED = "EXPORT_WRITE_FAILED"

    # ---- 并发(阶段 4)----
    #: 同一作用域正在被另一个请求执行,本次**没有调用模型**就退出了。
    #: 这个码存在的唯一理由是 §7.3 的 P0:宁可报一个可解释的失败让人重试,
    #: 也不能为了让这一件"成功"而多调一次付费模型。
    CONCURRENT_IN_FLIGHT = "CONCURRENT_IN_FLIGHT"

    # ---- 执行中断(任务 17 / 18)----
    #: 领走这一件的 worker 没有回来,租约过期后被回收,且已经用完重试次数。
    #: 它与 `CONCURRENT_IN_FLIGHT` 的区别是**谁不见了**:那个是有人正在跑,
    #: 这个是没人在跑了。分成两个码而不是复用,因为运营的下一步不同 ——
    #: 前者等一等再点,后者要去看 worker 还活着没有。
    WORKER_LOST = "WORKER_LOST"

    #: 开跑前发现执行权已经不在自己手上(A43 / BLOCK-02)。**没有花钱。**
    #:
    #: 与上面两个码的区别仍然是"谁不见了":`CONCURRENT_IN_FLIGHT` 是别人正在跑,
    #: `WORKER_LOST` 是没人在跑了,而这一个是**这一件已经改由别人跑了**。
    #: 它几乎总是成对出现:另一个 worker 手里有同一件的正常结果。
    #: 单独一个码,是为了让"续租失败"和"worker 死了"在异常中心里分得开 ——
    #: 前者说明租约时长配短了,后者说明进程有问题,要看的地方完全不同。
    LEASE_LOST = "LEASE_LOST"

    #: 上一次付费执行没落地,且**已经自动重跑过**仍然没落地。
    #: 到这里就不再自动付费了 —— 停下来等人去 Provider 后台对账。
    #:
    #: 与 `WORKER_LOST` 的区别是**钱**:那个说的是"自动重排用完了,重试即可",
    #: 这个说的是"再点一次可能是第三次付费"。两者共用一个码的话,
    #: 界面上的「重试」按钮会长得一模一样,而其中一个会花钱。
    BILLED_RESULT_UNKNOWN = "BILLED_RESULT_UNKNOWN"

    # ---- 兜底。必须为 0 ----
    UNEXPLAINED = "UNEXPLAINED"


@dataclass(frozen=True)
class ErrorMeta:
    """一个异常码的全部人类可读信息。

    `advice` 不是装饰:§6.3.1 第三条要求"运营无需开发协助即可判断下一步动作",
    这一列就是那句话。写不出 advice 的码说明这个失败还没被真正理解,
    不该被当成"可解释"。
    """

    label: str
    category: FlowStep
    advice: str
    #: 机器重试有意义吗。`NEEDS_HUMAN` 类一律 False —— 重试一百次结果相同
    retryable: bool
    #: True = 需人工介入(状态落 NEEDS_HUMAN);False = 系统失败(FAILED)或跳过
    needs_human: bool
    #: 跳过类:既不算成功也不算失败,单独一列(SKIPPED)
    skip: bool = False


ERROR_REGISTRY: Mapping[BatchErrorCode, ErrorMeta] = {
    BatchErrorCode.PRECHECK_MISMATCH: ErrorMeta(
        label="当前不该做这个动作",
        category=FlowStep.MATERIAL,  # 实际类别按条目的 next_action 覆盖,见 plan()
        advice="按商品自己的「唯一下一步」处理;这次批量动作跳过了它",
        retryable=False,
        needs_human=False,
        skip=True,
    ),
    BatchErrorCode.SAME_SPU_DEDUPED: ErrorMeta(
        label="同作用域已有一件在执行",
        category=FlowStep.DRAFT,
        advice=(
            "图片集/草稿按 SPU 共享,文案按 SPU + 颜色共享;"
            "同一作用域执行一次即覆盖它下面的全部 SKU"
        ),
        retryable=False,
        needs_human=False,
        skip=True,
    ),
    BatchErrorCode.ALREADY_EXPORTED: ErrorMeta(
        label="已导出且无过期",
        category=FlowStep.DRAFT,
        advice="无需再导;确需重导时勾选「包含已导出」",
        retryable=False,
        needs_human=False,
        skip=True,
    ),
    BatchErrorCode.OWNERSHIP_MISSING: ErrorMeta(
        label="没挂到 SPU / 颜色上",
        category=FlowStep.SETUP,
        advice="在 SPU 页把这个 SKU 挂回它所属的颜色,再重试这一批",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.NO_ACTIVE_PLAN: ErrorMeta(
        label="还没选生成方案",
        category=FlowStep.PLAN,
        advice="在方案页给这个颜色(或整个 SPU)选一次模特与参数",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.NO_USABLE_MATERIAL: ErrorMeta(

        label="没有可用素材",
        category=FlowStep.MATERIAL,
        advice="到素材标签页补图,或放行被隔离的素材",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.ASSET_ROLE_UNCONFIRMED: ErrorMeta(
        label="素材角色未确认",
        category=FlowStep.MATERIAL,
        advice="到素材标签页确认这几张图的角色;门禁只认人工确认过的角色,模型猜的不算",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.EXTRACTION_FAILED: ErrorMeta(
        label="属性识别失败",
        category=FlowStep.ATTRIBUTE,
        advice="可以单独重试;连续失败请查 Provider 状态",
        retryable=True,
        needs_human=False,
    ),
    BatchErrorCode.ATTRIBUTE_CONFLICT: ErrorMeta(
        label="属性有冲突",
        category=FlowStep.ATTRIBUTE,
        advice="到属性标签页逐项裁决冲突;系统不替人选",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.ATTRIBUTE_NOT_CREDIBLE: ErrorMeta(
        label="属性证据不采信",
        category=FlowStep.ATTRIBUTE,
        advice="识别已跑过,但这些字段未校准或置信度不足,系统不采信;请人工直接填写,重跑识别不会有新结论",
        # **retryable=False 是这一条的要点。** 识别没有失败,重试等于
        # 再花一次钱换回同一批不采信的证据
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.AUDIENCE_UNCONFIRMED: ErrorMeta(
        label="受众未确认",
        category=FlowStep.ATTRIBUTE,
        advice="先在商品资料里确认受众:它决定模特候选集、检查项与导出模板",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.NO_CONFIRMED_ATTRIBUTES: ErrorMeta(
        label="没有已确认属性",
        category=FlowStep.ATTRIBUTE,
        advice="先确认属性再生成文案,否则卖点声明无从溯源",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.IMAGE_SET_NOT_APPROVED: ErrorMeta(
        label="图片集未批准",
        category=FlowStep.IMAGE_SET,
        advice="到图片集标签页编排并批准",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.COPY_GENERATION_FAILED: ErrorMeta(
        label="文案生成失败",
        category=FlowStep.COPY,
        advice="可以单独重试;连续失败请查文案模型配置",
        retryable=True,
        needs_human=False,
    ),
    BatchErrorCode.COPY_RULE_VIOLATION: ErrorMeta(
        label="文案校验不通过",
        category=FlowStep.COPY,
        advice="到文案标签页看具体是字数、禁词还是无依据声明",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.COPY_NOT_APPROVED: ErrorMeta(
        label="文案未批准",
        category=FlowStep.COPY,
        advice="到文案标签页复核并批准",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.DRAFT_PRECONDITION: ErrorMeta(
        label="草稿前置条件不满足",
        category=FlowStep.DRAFT,
        advice="缺已批准的图片集或文案;先补上游",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.DRAFT_FIELD_INVALID: ErrorMeta(
        label="草稿字段不合格",
        category=FlowStep.DRAFT,
        advice="按提示里的字段名到草稿标签页补填或改短",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.DRAFT_STALE: ErrorMeta(
        label="草稿已过期",
        category=FlowStep.DRAFT,
        advice="重新生成草稿(批量校验草稿即可),手填字段不会丢",
        retryable=True,
        needs_human=False,
    ),
    BatchErrorCode.DRAFT_NOT_EXPORTABLE: ErrorMeta(
        label="草稿状态不允许导出",
        category=FlowStep.DRAFT,
        advice="先让草稿校验通过,再导出",
        retryable=False,
        needs_human=True,
    ),
    BatchErrorCode.SPEC_VERSION_MIXED: ErrorMeta(
        label="批次内导出模板版本不一致",
        category=FlowStep.DRAFT,
        advice="模板换版后先整批重新生成草稿,再合并导出",
        retryable=True,
        needs_human=False,
    ),
    BatchErrorCode.EXPORT_WRITE_FAILED: ErrorMeta(
        label="导出文件写入失败",
        category=FlowStep.DRAFT,
        advice="可以单独重试;连续失败请查服务日志",
        retryable=True,
        needs_human=False,
    ),
    BatchErrorCode.CONCURRENT_IN_FLIGHT: ErrorMeta(
        label="同一商品正被另一个请求执行",
        # 实际类别按动作覆盖(`ACTION_STEP`),这里只是兜底值
        category=FlowStep.DRAFT,
        advice="等那一批跑完后单独重试即可;本次没有调用付费模型,不会重复计费",
        retryable=True,
        needs_human=False,
        # 刻意**不是** skip:跳过项不进成功率分母,也拿不到重试按钮,
        # 于是这件该做的事会静悄悄地不做。它是一次真实的执行失败,
        # 应当被看见、被重试。
    ),
    BatchErrorCode.BILLED_RESULT_UNKNOWN: ErrorMeta(
        label="费用不明,需人工对账",
        # 与 CONCURRENT_IN_FLIGHT / WORKER_LOST 一样按动作覆盖(`ACTION_STEP`)
        category=FlowStep.DRAFT,
        advice=(
            "这一件的外部调用可能已经成功并计费,但结果没能落库,且自动重跑过一次"
            "仍然没落地。请先到模型服务商后台核对这条作用域是否已经产生过结果;"
            "确认没有之后,用 `python -m tools.resolve_billed_unknown --apply` "
            "清掉回执再重跑。**不要直接点重试** —— 那会是第三次付费"
        ),
        # 不可自动重试:这正是这个码存在的全部意义
        retryable=False,
        # 需人工:状态落 NEEDS_HUMAN,不会被回收器或重试按钮捡走
        needs_human=True,
    ),
    BatchErrorCode.WORKER_LOST: ErrorMeta(
        label="执行中断,已用完自动重试",
        # 与 CONCURRENT_IN_FLIGHT 同样按动作覆盖(`ACTION_STEP`)
        category=FlowStep.DRAFT,
        advice=(
            "领走它的 worker 中途退出了,自动重排已达上限。"
            "先确认 worker 进程还在跑,再点「重试」——已成功的条目不会重跑"
        ),
        # 可重试:回收器放弃的是**自动**重排,不是这件事本身。
        # 写 False 的话它连重试按钮都拿不到,而这一类恰恰是重试就能好的
        retryable=True,
        needs_human=False,
    ),
    BatchErrorCode.LEASE_LOST: ErrorMeta(
        label="执行权已交接,本次未执行",
        # 与上面两个同样按动作覆盖(`ACTION_STEP`)
        category=FlowStep.DRAFT,
        advice=(
            "这一件在本 worker 开跑之前已被回收或被人工重试重置,"
            "改由另一次执行负责。**本次没有调用付费模型。**"
            "通常不需要处理;频繁出现说明单件耗时接近租约时长,请检查配置"
        ),
        retryable=True,
        needs_human=False,
    ),
    BatchErrorCode.UNEXPLAINED: ErrorMeta(
        label="未归类的失败",
        category=FlowStep.DRAFT,
        advice="这是一个异常码缺口,请连同批次号报给开发",
        retryable=True,
        needs_human=False,
    ),
}


def is_explainable(code: BatchErrorCode | str | None) -> bool:
    """§6.3.1:失败是否"可解释"。

    两种情形都算不可解释:显式的 `UNEXPLAINED`,**以及没在注册表里登记的码**。
    后者容易被漏掉 —— 有人在执行器里 `code="WHATEVER"` 写了个新字符串,
    `error_meta` 会把它降级成 UNEXPLAINED 显示,但如果这里只比对
    `!= "UNEXPLAINED"`,它就会被算成"可解释",于是签字门槛上的那个
    "不可解释数 = 0" 被绕过去了。判据必须是"在注册表里且不是兜底码"。

    `None` 表示没有失败,可解释。
    """
    if code is None:
        return True
    try:
        parsed = BatchErrorCode(str(code))
    except ValueError:
        return False
    return parsed is not BatchErrorCode.UNEXPLAINED


def error_meta(code: BatchErrorCode | str) -> ErrorMeta:
    try:
        return ERROR_REGISTRY[BatchErrorCode(str(code))]
    except (ValueError, KeyError):
        return ERROR_REGISTRY[BatchErrorCode.UNEXPLAINED]


def status_for(code: BatchErrorCode) -> BatchItemStatus:
    """异常码 -> 条目终态。三分法在这一个函数里,不散落在执行循环各处。"""
    meta = error_meta(code)
    if meta.skip:
        return BatchItemStatus.SKIPPED
    if meta.needs_human:
        return BatchItemStatus.NEEDS_HUMAN
    return BatchItemStatus.FAILED


# ==========================================================
# 前置条件:直接引用 flow.py 的「唯一下一步」
# ==========================================================

#: 动作 -> 允许执行它的下一步动作码集合。
#:
#: `VALIDATE_DRAFT` 收下 REFRESH_DRAFT 与 FIX_DRAFT 是刻意的:重建草稿
#: 是幂等的且保留手填字段,让运营为了"过期"和"字段错"分别点两个按钮
#: 没有意义。`GENERATE_COPY` 反过来只收 GENERATE_COPY —— 已经有人编辑过
#: 的文案不能被批量覆盖,那是人的工作成果。
ACTION_PRECONDITION: Mapping[BatchAction, frozenset[NextActionCode]] = {
    BatchAction.EXTRACT: frozenset({NextActionCode.RUN_EXTRACTION}),
    BatchAction.GENERATE_COPY: frozenset({NextActionCode.GENERATE_COPY}),
    BatchAction.VALIDATE_DRAFT: frozenset(
        {
            NextActionCode.BUILD_DRAFT,
            NextActionCode.FIX_DRAFT,
            NextActionCode.REFRESH_DRAFT,
        }
    ),
    BatchAction.EXPORT: frozenset({NextActionCode.EXPORT}),
}

#: 下一步动作码 -> 该动作若被"卡住"时归到哪一类异常。
#:
#: 前置不满足时,类别取**当前唯一下一步所在的步骤**,而不是批量动作所在的步骤:
#: 运营要去的地方是卡点,不是他刚才点的那个按钮。
PRECHECK_CODE_BY_ACTION: Mapping[NextActionCode, BatchErrorCode] = {
    NextActionCode.COMPLETE_SETUP: BatchErrorCode.OWNERSHIP_MISSING,
    NextActionCode.CHOOSE_PLAN: BatchErrorCode.NO_ACTIVE_PLAN,
    NextActionCode.UPLOAD_MATERIAL: BatchErrorCode.NO_USABLE_MATERIAL,

    NextActionCode.RELEASE_QUARANTINE: BatchErrorCode.NO_USABLE_MATERIAL,
    NextActionCode.CONFIRM_ASSET_ROLE: BatchErrorCode.ASSET_ROLE_UNCONFIRMED,
    NextActionCode.CONFIRM_AUDIENCE: BatchErrorCode.AUDIENCE_UNCONFIRMED,
    NextActionCode.RESOLVE_CONFLICT: BatchErrorCode.ATTRIBUTE_CONFLICT,
    NextActionCode.CONFIRM_ATTRIBUTES: BatchErrorCode.NO_CONFIRMED_ATTRIBUTES,
    NextActionCode.FILL_ATTRIBUTES: BatchErrorCode.ATTRIBUTE_NOT_CREDIBLE,
    NextActionCode.RUN_EXTRACTION: BatchErrorCode.NO_CONFIRMED_ATTRIBUTES,
    NextActionCode.BUILD_IMAGE_SET: BatchErrorCode.IMAGE_SET_NOT_APPROVED,
    NextActionCode.FIX_IMAGE_SET: BatchErrorCode.IMAGE_SET_NOT_APPROVED,
    NextActionCode.APPROVE_IMAGE_SET: BatchErrorCode.IMAGE_SET_NOT_APPROVED,
    NextActionCode.GENERATE_COPY: BatchErrorCode.COPY_NOT_APPROVED,
    NextActionCode.FIX_COPY: BatchErrorCode.COPY_RULE_VIOLATION,
    NextActionCode.APPROVE_COPY: BatchErrorCode.COPY_NOT_APPROVED,
    NextActionCode.BUILD_DRAFT: BatchErrorCode.DRAFT_PRECONDITION,
    NextActionCode.FIX_DRAFT: BatchErrorCode.DRAFT_FIELD_INVALID,
    NextActionCode.REFRESH_DRAFT: BatchErrorCode.DRAFT_STALE,
    NextActionCode.EXPORT: BatchErrorCode.DRAFT_NOT_EXPORTABLE,
    NextActionCode.RESOLVE_REJECTION: BatchErrorCode.DRAFT_NOT_EXPORTABLE,
    NextActionCode.DONE: BatchErrorCode.ALREADY_EXPORTED,
}


@dataclass(frozen=True)
class Candidate:
    """一件被选中的商品,以及它当前的判定结论。

    只带 flow.py 已经算出来的东西。**这里不放素材数、属性数、字段数** ——
    一旦放了,就会有人在这里重新判断一次"够不够导出"。
    """

    product_id: str
    sku: str
    spu: str
    next_action: NextActionCode
    next_action_label: str
    current_step: FlowStep
    blocking_count: int = 0
    pending_count: int = 0
    #: 首个阻断问题的定位信息(字段名 / 校验码 / 素材角色),用于一键跳转
    first_blocking_ref: str | None = None
    #: 决定输出的上游指纹。同一指纹重复执行 = 白花钱,见 `idempotency_key`
    fingerprint: str = ""
    #: 这一行商品属于哪个颜色(变体身份串)。文案的作用域键要用它。
    #:
    #: 默认空串**只服务于旧的构造形状**(测试里手搓 Candidate 的地方),
    #: 生产接线由 `batch_service.candidates()` 填 —— 那一处有守卫。
    #: 恒空的后果很具体:三个颜色算出同一个作用域键,批量生成只出一稿。
    variant_id: str = ""



@dataclass(frozen=True)
class PlannedItem:
    product_id: str
    sku: str
    spu: str
    scope_key: str
    executable: bool
    #: 不可执行时必填。可执行时为 None
    code: BatchErrorCode | None = None
    reason: str = ""
    target_step: FlowStep | None = None
    target_ref: str | None = None
    idempotency_key: str = ""


@dataclass(frozen=True)
class BatchPlan:
    """FE-301 的验收结果:「执行前显示可执行/不可执行数量」。

    `by_code` 让"37 件里 12 件不可执行"能被继续追问一句"为什么" ——
    界面上一行一个理由,而不是一个总数。
    """

    action: BatchAction
    items: tuple[PlannedItem, ...]

    @property
    def executable(self) -> tuple[PlannedItem, ...]:
        return tuple(i for i in self.items if i.executable)

    @property
    def blocked(self) -> tuple[PlannedItem, ...]:
        return tuple(i for i in self.items if not i.executable)

    @property
    def executable_count(self) -> int:
        return len(self.executable)

    @property
    def blocked_count(self) -> int:
        return len(self.blocked)

    @property
    def by_code(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.blocked:
            key = str(item.code or BatchErrorCode.UNEXPLAINED)
            counts[key] = counts.get(key, 0) + 1
        return counts


def scope_key(action: BatchAction, candidate: Candidate) -> str:
    """动作的作用域键。同作用域的多件商品靠它去重(见模块注释)。

    三档:`spu` / `spu_colour` / `product`。文案走中间那档 ——
    键必须和 `listing_copies` 的槽位一样细,不然同 SPU 的其它颜色会被
    当成重复跳过(迁移 0051)。
    """
    scope = ACTION_SCOPE[action]
    if scope == "spu":
        return f"spu:{candidate.spu}"
    if scope == "spu_colour":
        return f"spu:{candidate.spu}|colour:{candidate.variant_id}"
    return f"product:{candidate.product_id}"


def _deduped_reason(action: BatchAction, candidate: Candidate) -> str:
    """跳过理由要说出**是哪个作用域**重了,不是笼统一句"同 SPU"。

    文案按颜色分槽之后,"同 SPU 已有一件在执行"会让运营以为另一个颜色
    也被覆盖了 —— 而它没有,它是另一件在执行。理由文案是运营判断
    「跳过的这几件要不要另开一批」的唯一依据,含糊等于让他自己去猜。
    """
    if ACTION_SCOPE[action] == "spu_colour":
        return (
            f"同 SPU({candidate.spu})的同一颜色"
            f"({candidate.variant_id or '未分配'})已有一件在本批次执行"
        )
    return f"同 SPU({candidate.spu})已有一件在本批次执行"


def idempotency_key(action: BatchAction, candidate: Candidate) -> str:

    """幂等键 = 动作 + 作用域 + 上游指纹。

    判据和 `workflows/idempotency.py` 一致:**改了它,结果会不一样吗?**
    会,才进哈希。于是"同一商品同一动作、上游没变"必然算出同一个键,
    重试时凭它命中回执、跳过付费调用(§6.3 最后一条)。

    指纹为空时(前置就不满足,不会执行)仍然给一个稳定键,方便留痕。
    """
    payload = f"{action.value}|{scope_key(action, candidate)}|{candidate.fingerprint}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:48]
    return f"b3-{digest}"


def batch_request_fingerprint(
    action: BatchAction,
    product_ids: Sequence[str],
    *,
    include_exported: bool,
    label: str | None,
) -> str:
    """「创建批次」这条 HTTP 命令的入参指纹(A45-batch12-2 / EX-06)。

    判据同 `idempotency_key`:**改了它,这次请求要做的事会不一样吗?**
    会,才进哈希。四项各自的理由:

        action             不同动作显然是不同的事
        product_ids        选中的商品集合就是这次请求的正文
        include_exported   它会改变计划出来的条目集合
        label              不改变要做的事,但**改变批次的身份** ——
                           运营靠它在批次中心里认出哪个是哪个。同键换名
                           重发一次,他要的是"改个名",而这里会答"没变化",
                           两者不一致就会让他以为改名没生效。归进指纹之后
                           那种情况报 409,他至少知道要换一把键

    ## 商品顺序为什么要排序

    `[A, B]` 与 `[B, A]` 是同一次请求:计划阶段本来就要去重、按 scope 收敛,
    顺序不影响任何结果。不排序的话,一个把商品存在 Set 里的前端会因为遍历
    顺序不稳定而算出两个指纹,于是同一次重发被判成 409 —— 而那个 409
    没有任何人能看懂。

    去重同理:同一件商品在入参里出现两次不改变结果。
    """
    ids = ",".join(sorted({str(pid) for pid in product_ids}))
    payload = (
        f"{action.value}|{ids}|{int(bool(include_exported))}|{(label or '').strip()}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:48]


def plan(
    action: BatchAction,
    candidates: Sequence[Candidate],
    *,
    include_exported: bool = False,
) -> BatchPlan:
    """算出批次计划。**不执行任何动作,不写任何库。**

    两道筛:先按「唯一下一步」判前置(理由见模块注释),再按 scope 去重。
    顺序不能反 —— 先去重会让"留下的那一件恰好不可执行"而其余被跳过,
    整个 SPU 就什么也没做,而界面上显示的是"1 件不可执行、2 件已跳过"。
    """
    allowed = set(ACTION_PRECONDITION[action])
    if action is BatchAction.EXPORT and include_exported:
        # 平台侧上传失败要重导,这是真实需求。但它必须显式勾选 ——
        # 默认放行会让每次批量导出都把上次已经导过的几十件重新算一遍导出次数,
        # 而"导出次数"是审计里用来回答"这份文件是第几次导的"那个数字
        allowed.add(NextActionCode.DONE)
    seen_scopes: set[str] = set()
    items: list[PlannedItem] = []

    for candidate in candidates:
        key = scope_key(action, candidate)
        idem = idempotency_key(action, candidate)

        if candidate.next_action not in allowed:
            # 已导出且这次是导出动作:这不是"卡住",是"不用做"
            if (
                action is BatchAction.EXPORT
                and candidate.next_action is NextActionCode.DONE
            ):
                code = BatchErrorCode.ALREADY_EXPORTED
                reason = "草稿已导出且未过期,本次跳过"
            else:
                code = PRECHECK_CODE_BY_ACTION.get(
                    candidate.next_action, BatchErrorCode.PRECHECK_MISMATCH
                )
                reason = (
                    f"当前唯一下一步是「{candidate.next_action_label}」,"
                    f"不能执行{BATCH_ACTION_LABELS[action]}"
                )
            items.append(
                PlannedItem(
                    product_id=candidate.product_id,
                    sku=candidate.sku,
                    spu=candidate.spu,
                    scope_key=key,
                    executable=False,
                    code=code,
                    reason=reason,
                    # 类别取卡点所在步骤,不取批量动作所在步骤
                    target_step=candidate.current_step,
                    target_ref=candidate.first_blocking_ref,
                    idempotency_key=idem,
                )
            )
            continue

        if key in seen_scopes:
            items.append(
                PlannedItem(
                    product_id=candidate.product_id,
                    sku=candidate.sku,
                    spu=candidate.spu,
                    scope_key=key,
                    executable=False,
                    code=BatchErrorCode.SAME_SPU_DEDUPED,
                    reason=_deduped_reason(action, candidate),

                    target_step=candidate.current_step,
                    idempotency_key=idem,
                )
            )
            continue

        seen_scopes.add(key)
        items.append(
            PlannedItem(
                product_id=candidate.product_id,
                sku=candidate.sku,
                spu=candidate.spu,
                scope_key=key,
                executable=True,
                idempotency_key=idem,
            )
        )

    # 导出动作要求整批同一个模板版本,否则合并出来的文件半份新半份旧。
    # 这条检查放在计划阶段,免得跑了一半才发现(§4.5 第 5 行:模板换版全部草稿过期)
    return BatchPlan(action=action, items=tuple(items))


# ==========================================================
# 执行:单件失败不终止批次
# ==========================================================


@dataclass(frozen=True)
class ExecResult:
    """执行器对单件的回答。

    执行器**不决定**条目落什么状态,只回答"成功了没、为什么没成"。
    状态由 `status_for` 从异常码算出 —— 三分法只有一份实现。
    """

    ok: bool
    code: BatchErrorCode | None = None
    message: str = ""
    target_step: FlowStep | None = None
    target_ref: str | None = None
    #: 这次真的调了几次付费模型。回执命中而跳过调用时为 0
    paid_calls: int = 0
    #: 命中回执、复用了上次结果
    reused: bool = False
    payload: Mapping[str, object] = field(default_factory=dict)


# ==========================================================
# 回执生命周期与真实用量(评审第 4、5 条)
# ==========================================================


class ReceiptStatus(StrEnum):
    """一条幂等回执的生命周期(评审第 5 条)。

    原来的回执只有"存在 / 不存在"两个状态,而"存在"被硬编码成"做成了"。
    于是「模型已经生成了内容、但合规校验把它拒了」这种**钱已经花了**的失败
    没有任何落点:不写回执,普通重试就再调一次模型、再花一次钱。

        IN_FLIGHT        已经开始执行,可能已经计费,结果未知。
                         正常路径会在同一事务里推进到终态;留在库里的
                         IN_FLIGHT 意味着上一次执行中途死掉,钱花没花不可考
        SUCCEEDED        做成了。命中即复用,不再调用模型
        FAILED_BILLED    付费调用已发生,但结果没通过后处理(如文案合规校验)。
                         **原始结果必须随回执保存**,普通重试只重新校验,
                         不再触发付费调用
        FAILED_UNBILLED  失败且确认没有产生费用。等价于"没做过",重试照常执行
    """

    IN_FLIGHT = "IN_FLIGHT"
    SUCCEEDED = "SUCCEEDED"
    FAILED_BILLED = "FAILED_BILLED"
    FAILED_UNBILLED = "FAILED_UNBILLED"


RECEIPT_STATUS_LABELS: Mapping[ReceiptStatus, str] = {
    ReceiptStatus.IN_FLIGHT: "执行中断,费用不明",
    ReceiptStatus.SUCCEEDED: "已完成",
    ReceiptStatus.FAILED_BILLED: "已付费但未通过",
    ReceiptStatus.FAILED_UNBILLED: "失败(未计费)",
}


class ReceiptRoute(StrEnum):
    """命中回执后该怎么走。**唯一一份分派实现**,service 层不许自己 if。"""

    #: 没有回执 / FAILED_UNBILLED:等价于"没做过",正常执行(会计费)
    EXECUTE = "EXECUTE"
    #: SUCCEEDED:复用上次结果,不调用模型
    REUSE_SUCCESS = "REUSE_SUCCESS"
    #: FAILED_BILLED 且原始结果可以离线重校验:复用失败结论,不再计费。
    #: 目前只有文案生成属于这类 —— 模型产出是确定内容,校验它不要钱
    REUSE_BILLED_FAILURE = "REUSE_BILLED_FAILURE"
    #: 上次执行中途死掉(IN_FLIGHT)或付费失败但没有可复用的原始结果
    #: (识别全失败:没有任何证据落库)。重试必须重新执行,**费用会累计**,
    #: 台账上 paid_calls > 1 的异常标记就是给这种情况看的
    EXECUTE_AGAIN_BILLED = "EXECUTE_AGAIN_BILLED"
    #: 已经自动重跑过一次仍然没落地,**不再自动付费重跑**,交给人对账。
    #: 见 `MAX_BILLED_EXECUTIONS`
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


#: 一条作用域允许**自动**发生几次付费执行。
#:
#: ## 为什么必须有这个上限
#:
#: `EXECUTE_AGAIN_BILLED` 的处境是「上一次可能已经花了钱,但我们不知道」——
#: 和生成链路的 `SUBMIT_RESULT_UNKNOWN` 是同一种不确定性。但两边原来的处理
#: 完全相反:
#:
#:     生成链路   挡住自动重试,`force` 必填人工对账结论,并进审计
#:     批量链路   自动重跑,只写一条 warning 日志
#:
#: 而批量这一侧的重跑是**回收器发起的**(beat 60 秒一拍),全程没有人在回路里。
#: 配合 `MAX_ITEM_ATTEMPTS = 3`,一次「外部调用已成功、本地提交前进程死亡」
#: 最多会自动付费 3 次,而运营只能在事后从台账的 `executions > 1` 里发现。
#: 那个信号出现的时候钱已经花完了。
#:
#: ## 为什么是 2 而不是 1
#:
#: IN_FLIGHT 有两种成因,而标记本身**曾经**分不出来:
#:
#:     崩在调用**之前**   没花钱,重跑是免费且正确的
#:     崩在调用**之后**   花了钱,重跑就是重复计费
#:
#: 取 1 等于每一次 pod 驱逐、每一次库抖动都要人工介入,50 件的批次会因为
#: 一次基础设施抖动全部停下 —— 那种代价下这道闸迟早会被关掉。
#: 取 2 放过最常见的那一种,而**同一个点连续崩两次**已经不像偶发。
#:
#: ## A45-batch12-2 / EX-02:上面这段推理的前提已经不成立了
#:
#: 那个折中默认了"分不出来"。现在分得出来了 —— 迁移 0031 的
#: `provider_call_at` 由 `_mark_provider_call()` 紧挨着付费调用之前写入,
#: **IN_FLIGHT 因此不再走这个上限**:发出去过就直接
#: `NEEDS_RECONCILIATION`,没发出去就免费重跑且不扣额度。
#: 也就是说 EX-02 那笔"无人值守的自动重复付费"是 0 次,不是 1 次。
#:
#: 这个数字**仍然有用**,但适用范围缩小到一条:
#: `FAILED_BILLED` 且动作不可离线复校(目前只有 `EXTRACT`)——
#: 那是一次**已知**付费的失败,而且一条证据都没落库,除了重跑没有别的路。
#: 那条路上仍然分不出"重跑会不会有用",所以仍然靠次数兜底。
#:
#: 上限只约束**付费**执行。校验与导出不调外部模型,不走这条路。
MAX_BILLED_EXECUTIONS = 2


#: 付费失败后,原始结果足以离线重校验的动作。
#:
#: GENERATE_COPY 在这里:模型生成的文案连同违规项都已落库(listing_copies),
#: 重新校验只是再读一遍规则,不要钱。EXTRACT **不在**:全失败意味着一条
#: 证据都没有,没有东西可以"重新校验",重跑是唯一的路。
REVALIDATABLE_ACTIONS: frozenset[BatchAction] = frozenset({BatchAction.GENERATE_COPY})


def receipt_route(
    status: str | None,
    action: BatchAction,
    *,
    executions: int = 1,
    call_dispatched: bool = True,
) -> ReceiptRoute:
    """按回执状态决定这次执行怎么走。

    入参 `status` 是库里那一列的原文;None 表示没有回执。
    未知字符串按 EXECUTE 处理并由调用方记日志 —— 兜底方向是"多花一次钱",
    而不是"把一个坏状态当成功复用"。

    `executions` 是这条作用域**真的开跑过几次**(`batch_action_receipts.executions`,
    由 `_claim_in_flight` 每次认领 +1)。它原来只是台账上的一个观测信号;
    现在同时是闸门:任何会重新计费的走法,超过 `MAX_BILLED_EXECUTIONS`
    就改判 `NEEDS_RECONCILIATION`,停下来等人对账。理由见那个常量。

    默认 1 而不是 0:老回执"存在即至少跑过一次"(迁移 0028 的 server_default
    同一条理由),默认成 0 会让缺列的历史行凭空多出一次自动付费额度。

    ## `call_dispatched`(A45-batch12-2 / EX-02)

    这一次执行**有没有真的把请求发出去** —— 库里的
    `batch_action_receipts.provider_call_at` 是否有值。它补上的正是
    `MAX_BILLED_EXECUTIONS` 那段注释里承认分不出来的那件事。

    默认 True:缺这一列的存量行、以及任何拿不准的调用方,一律按
    "可能已发出"处理。默认成 False 会让一条其实已经计费的回执被判成
    "没做过"直接重跑,那正是这个参数要挡的事。

    ## 这个默认值保护不了真正的调用方(A45-batch12-3)

    别把它当成兜底。`batch_service._execute` 是**显式传**的
    (`receipt.provider_call_at is not None`),所以默认值在那条路上从不生效:
    标记因为任何原因没落库,这里拿到的就是 False,判 `EXECUTE`。

    "没有标记 = 没有调用"这句话的成立,靠的不是这个默认值,而是
    `_execute` 那一侧的约定 —— **标记写不下去就不许调用付费模型**。
    改动那一侧之前,先回来读一遍这一段。
    """
    if status is None:
        return ReceiptRoute.EXECUTE
    try:
        parsed = ReceiptStatus(str(status))
    except ValueError:
        return ReceiptRoute.EXECUTE
    if parsed is ReceiptStatus.SUCCEEDED:
        return ReceiptRoute.REUSE_SUCCESS
    if parsed is ReceiptStatus.FAILED_UNBILLED:
        # 明确没花过钱,不受付费上限约束
        return ReceiptRoute.EXECUTE
    if parsed is ReceiptStatus.FAILED_BILLED:
        if action in REVALIDATABLE_ACTIONS:
            # 离线复校验不花钱,同样不受上限约束
            return ReceiptRoute.REUSE_BILLED_FAILURE
        return _billed_again_or_stop(executions)

    # ---- IN_FLIGHT:上一次执行中途死掉。锁在手里说明那个进程已经不在了 ----
    #
    # A45-batch12-2 / EX-02。这里原来无条件走 `_billed_again_or_stop()`,
    # 于是 `executions == 1` 的第一次回收会判 `EXECUTE_AGAIN_BILLED` ——
    # **一次由回收器发起、没有任何人决定过的付费重跑。**
    #
    # 现在先问一句"请求到底发出去没有":
    if not call_dispatched:
        # 回执落了、调用还没发。可以证明这一次没花钱,于是它等价于"没做过"。
        # **不消耗付费额度** —— `executions` 记的是"开跑过几次",而这一次
        # 压根没跑到花钱那一步,拿它去扣自动付费的额度是把两个数混成一个。
        return ReceiptRoute.EXECUTE
    # 请求已经发出去过。**不再自动重跑**,交给人对账。
    #
    # 不走 `_billed_again_or_stop()`:那个函数的语义是"还剩几次额度",
    # 而这里的结论与剩几次无关 —— 第一次就该停。留着额度判断会让
    # `MAX_BILLED_EXECUTIONS` 看起来仍然在管这条路,而它已经不管了。
    return ReceiptRoute.NEEDS_RECONCILIATION


def _billed_again_or_stop(executions: int) -> ReceiptRoute:
    """还能不能再自动付费跑一次。

    单独抽出来是因为两个调用点必须给出同一个答案 —— 写成两个 if 的话,
    以后只改其中一处的人不会有任何提示。
    """
    if executions >= MAX_BILLED_EXECUTIONS:
        return ReceiptRoute.NEEDS_RECONCILIATION
    return ReceiptRoute.EXECUTE_AGAIN_BILLED


def usage_dict(
    *,
    attempted_calls: int,
    billable_calls: int,
    unit: str,
    tokens: int | None = None,
    images: int | None = None,
) -> dict[str, object]:
    """统一的用量结构(评审第 4 条)。**执行器只许通过它上报。**

    `attempted` 与 `billable` 分开:识别是逐图调用,失败的那几张同样
    发起过请求、可能已经计费 —— 记 attempted 让费用台账宁可多算不少算;
    模板文案没有任何远程调用,两个数都是 0,而不是硬编码的 1。
    """
    if attempted_calls < 0 or billable_calls < 0:
        raise ValueError("用量不能为负数")
    if billable_calls > attempted_calls:
        raise ValueError("计费次数不能超过发起次数")
    out: dict[str, object] = {
        "attempted_calls": attempted_calls,
        "billable_calls": billable_calls,
        "unit": unit,
    }
    if tokens is not None:
        out["tokens"] = tokens
    if images is not None:
        out["images"] = images
    return out


#: 定义搬到了 `app/core/vocab.py`(v4.1 Phase 0:core 不该反向依赖 workbench,
#: import-linter 把这条抓了出来)。这里重新导出,老的
#: `from app.workbench.batch import BATCH_EXECUTION_MODES` 照旧可用。
BATCH_EXECUTION_MODES = _vocab.BATCH_EXECUTION_MODES


@dataclass(frozen=True)
class ItemOutcome:
    item: PlannedItem
    status: BatchItemStatus
    code: BatchErrorCode | None
    message: str
    target_step: FlowStep | None
    target_ref: str | None
    paid_calls: int
    reused: bool
    payload: Mapping[str, object] = field(default_factory=dict)

    @property
    def explainable(self) -> bool:
        return is_explainable(self.code)



class PaidCallFailure(Exception):
    """付费调用之后才失败。**带着这次已经花掉的次数抛出来**(A-22)。

    执行器是唯一知道"厂商侧计没计费"的地方:响应解析失败、写回执失败、
    重试到放弃 —— 这些都发生在钱已经花掉之后。`run_plan` 的异常分支
    接不到这个数字时只能记 0,而少记的那一笔不会让任何数字对不上,
    也就永远不会有人发现。

    用法::

        raise PaidCallFailure("响应解析失败", paid_calls=n) from exc

    不强制所有执行器都用它:改造是渐进的,没用它的路径行为与之前一致。
    """

    def __init__(self, message: str = "", *, paid_calls: int = 0) -> None:
        super().__init__(message)
        self.paid_calls = int(paid_calls or 0)


def paid_calls_of(exc: BaseException) -> int:
    """这次异常带出来的已付费次数。带不出来时是 0(已知的少算)。

    读属性而不是判类型:包过一层的异常(比如被 `raise ... from`
    重新包装)只要把属性透传出来就仍然算数,而 `isinstance` 会漏掉它们。
    """
    value = getattr(exc, "paid_calls", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        # 属性存在但不是个数 —— 这种情况下装作没看见比抛第二个异常好:
        # 我们正处在"某件商品已经失败了"的路径上
        return 0

def run_plan(
    plan_result: BatchPlan,
    execute: Callable[[PlannedItem], ExecResult],
    *,
    on_outcome: Callable[[ItemOutcome], None] | None = None,
) -> tuple[ItemOutcome, ...]:
    """逐件执行。**任何一件抛异常都不会中断循环**(§6.3 第三条)。

    这是本文件里唯一带控制流的函数,也是那条退出条件唯一的实现处。
    执行器自己不需要写 try —— 它只管做事、抛错;把 50 件跑完是这里的职责。

    `on_outcome` 给调用方一个落库钩子:service 在每件之后提交保存点,
    于是"第 37 件炸了"不会回滚前 36 件的结果。
    """
    outcomes: list[ItemOutcome] = []
    for item in plan_result.items:
        if not item.executable:
            outcome = ItemOutcome(
                item=item,
                status=status_for(item.code or BatchErrorCode.PRECHECK_MISMATCH),
                code=item.code,
                message=item.reason,
                target_step=item.target_step,
                target_ref=item.target_ref,
                paid_calls=0,
                reused=False,
            )
        else:
            try:
                result = execute(item)
            except Exception as exc:  # noqa: BLE001 - 单件失败不终止批次
                code, message = classify_exception(exc)
                outcome = ItemOutcome(
                    item=item,
                    status=status_for(code),
                    code=code,
                    message=message,
                    target_step=error_meta(code).category,
                    target_ref=None,
                    # A-22:异常路径原来硬编码 0,而**付费调用之后**抛出的异常
                    # (响应解析失败、落库失败、超时重试后放弃)照样花了钱。
                    # 台账少记这一笔,与同仓"宁可多算不少算"的方向正好相反 ——
                    # 少算的那笔永远不会有人发现,因为没有任何数字对不上。
                    #
                    # 猜不出来就不猜:执行器**自己知道**花没花钱,所以约定由它
                    # 在异常上带出来(`PaidCallFailure`,或任何带 `paid_calls`
                    # 属性的异常)。带不出来的仍然记 0 —— 那是一个已知的少算,
                    # 写在这里让下一个人看得见,而不是让它继续无声。
                    paid_calls=paid_calls_of(exc),
                    reused=False,
                )
            else:
                if result.ok:
                    outcome = ItemOutcome(
                        item=item,
                        status=BatchItemStatus.SUCCEEDED,
                        code=None,
                        message=result.message,
                        target_step=None,
                        target_ref=None,
                        paid_calls=result.paid_calls,
                        reused=result.reused,
                        payload=result.payload,
                    )
                else:
                    code = result.code or BatchErrorCode.UNEXPLAINED
                    outcome = ItemOutcome(
                        item=item,
                        status=status_for(code),
                        code=code,
                        message=result.message or error_meta(code).label,
                        target_step=result.target_step or error_meta(code).category,
                        target_ref=result.target_ref,
                        paid_calls=result.paid_calls,
                        reused=result.reused,
                        payload=result.payload,
                    )
        outcomes.append(outcome)
        if on_outcome is not None:
            on_outcome(outcome)
    return tuple(outcomes)


#: 后端错误码 -> 批次异常码。
#:
#: 键是 `app.core.errors.ErrorCode` 的字符串值。这里用字符串而不是导入枚举,
#: 是为了让本文件在只有 stdlib 的环境里也能被纯测试加载。
ERROR_CODE_MAP: Mapping[str, BatchErrorCode] = {
    "MEDIA_QUARANTINED": BatchErrorCode.NO_USABLE_MATERIAL,
    "ATTRIBUTE_EXTRACTION_FAILED": BatchErrorCode.EXTRACTION_FAILED,
    "COPY_COMPLIANCE_FAILED": BatchErrorCode.COPY_RULE_VIOLATION,
    "COPY_CLAIM_MISMATCH": BatchErrorCode.COPY_RULE_VIOLATION,
    "CHANNEL_FIELD_MISSING": BatchErrorCode.DRAFT_FIELD_INVALID,
    "CHANNEL_SPEC_INCOMPLETE": BatchErrorCode.SPEC_VERSION_MIXED,
    "NOT_FOUND": BatchErrorCode.DRAFT_PRECONDITION,
    # 正常路径下 `_execute` 直接返回 ExecResult 而不抛异常,这一行是给
    # 别的调用方(将来的 celery task、重试端点)兜的:同一个语义在
    # 两条路径上必须落同一个码,否则签字用的那个计数会因为走法不同而变。
    "CONCURRENT_EXECUTION": BatchErrorCode.CONCURRENT_IN_FLIGHT,
    # A43 / BLOCK-02。`LeaseLostError.code` 就是这个字符串 —— 走结构化
    # 那一支,不依赖消息文案。上面 MESSAGE_HINTS 里那一条是兜底:
    # 消息在两个地方各写一遍时,总有一天会有人只改其中一处
    "LEASE_LOST": BatchErrorCode.LEASE_LOST,
}

#: 消息片段 -> 异常码。
#:
#: 后端的 `ValidationError` 大量共用 `INPUT_INVALID`,单看 code 分不出
#: "没有可用素材"和"草稿前置不满足"。按消息片段兜一层不优雅,但它换来的是
#: **每一条失败都有归类**;真正的解法是给这些 raise 点各自的 ErrorCode,
#: 那是一次会改到很多文件的重构,不该塞进阶段 3。
#:
#: 片段必须来自后端 raise 处的原文。`tests/pure/test_workbench_batch.py`
#: 用这份表反查 service.py,片段消失时测试失败 —— 否则改一句提示语,
#: 这里就会静默退化成 `UNEXPLAINED`。
class LeaseLostError(Exception):
    """开跑前发现执行权已经交接(A43 / BLOCK-02)。**抛它的地方还没花钱。**

    定义在判定模块而不是 service:`classify_exception()` 要认识它,
    而那个函数在这里。异常类型与它对应的错误码写在同一个文件里,
    是"改一个必须看见另一个"的最便宜的实现方式。
    """

    code = BatchErrorCode.LEASE_LOST.value


MESSAGE_HINTS: tuple[tuple[str, BatchErrorCode], ...] = (
    ("这一件的执行权已经不在本 worker 手上", BatchErrorCode.LEASE_LOST),
    ("没有可用于识别的素材", BatchErrorCode.NO_USABLE_MATERIAL),
    ("还没有任何已确认的属性", BatchErrorCode.NO_CONFIRMED_ATTRIBUTES),
    ("草稿前置条件不满足", BatchErrorCode.DRAFT_PRECONDITION),
    ("文案语言绑定有误", BatchErrorCode.DRAFT_PRECONDITION),
    ("草稿已过期", BatchErrorCode.DRAFT_STALE),
    ("不允许导出", BatchErrorCode.DRAFT_NOT_EXPORTABLE),
    ("还没有生成上架草稿", BatchErrorCode.DRAFT_PRECONDITION),
    ("无法只重生个别字段", BatchErrorCode.COPY_GENERATION_FAILED),
)


def classify_exception(exc: BaseException) -> tuple[BatchErrorCode, str]:
    """把异常翻译成预定义异常码 + 给运营看的一句话。

    先看 `code`(结构化,可靠),再看消息片段(兜底,见 `MESSAGE_HINTS`),
    都不命中才落 `UNEXPLAINED` —— 而那个数字必须为 0 才能签字,
    所以它是一个会被人追着修的缺口,不是一个安静的兜底分支。

    ## C-15:这段注释和实现曾经是反的

    实现原来**先**扫消息片段、后看 `code`,与上面这句话正好相反。
    交叉查过当时的 9 个 `ERROR_CODE_MAP` 键与 9 条 `MESSAGE_HINTS` 的抛出点,
    结论是"今天没有任何一条会被误分类" —— 所以它是潜在风险不是现行缺陷,
    评审据此把它从 P0/P1 区下调到 P2。

    但"今天不冲突"是一个会过期的结论:下一个人加一条 hint 时,他读的是
    这段注释,而注释说 `code` 优先。于是他会假设自己的 hint 只在没有 `code`
    时才生效,而实际上它会盖掉一个结构化的错误码 —— **两处都不报错**。

    改的是实现,不是注释:结构化的 `code` 本来就该赢,消息片段是兜底。
    """
    message = str(getattr(exc, "message", None) or exc) or exc.__class__.__name__

    raw_code = getattr(exc, "code", None)
    if raw_code is not None:
        mapped = ERROR_CODE_MAP.get(str(raw_code))
        if mapped is not None:
            return mapped, message

    for fragment, code in MESSAGE_HINTS:
        if fragment in message:
            return code, message

    return BatchErrorCode.UNEXPLAINED, message


# ==========================================================
# 汇总:FE-302 的五个计数与 §6.3 的两条比率
# ==========================================================


@dataclass(frozen=True)
class BatchCounts:
    total: int = 0
    pending: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    needs_human: int = 0
    skipped: int = 0
    #: 不可解释失败。**§6.3 要求它为 0 才能签字**,所以它是一等公民
    unexplained: int = 0
    paid_calls: int = 0
    reused: int = 0
    #: 状态列上出现了枚举里没有的值(R1-15)。**恒应为 0。**
    #:
    #: 非 0 意味着 schema 变过或者库被手工改过。它计入 `total` 但不进任何
    #: 一个业务桶 —— 于是"各桶之和 != total"这件事在数字上是看得见的,
    #: 而不是变成一个没人解释得了的差额。
    unknown_status: int = 0

    @property
    def attempted(self) -> int:
        """真正被执行的件数。跳过的不算分母 —— 它们没被尝试过。"""
        return self.succeeded + self.failed + self.needs_human

    @property
    def success_rate(self) -> float | None:
        """完整成功率(A 批次 ≥95% 用它)。分母为 0 时返回 None,不返回 0。"""
        if self.attempted == 0:
            return None
        return self.succeeded / self.attempted

    @property
    def explainable_rate(self) -> float | None:
        """成功或可解释失败的比例(50 件全量 ≥95% 用它)。

        `needs_human` 计入分子:系统正确地停下来等人处理,是可解释的正常结果,
        不是失败。这一条是 §6.3.1 的直接推论,也是 V1.1 特意澄清的口径
        (异常件的正确终态是被阻断,不是导出成功)。
        """
        if self.attempted == 0:
            return None
        good = self.succeeded + self.needs_human + (self.failed - self.unexplained)
        return good / self.attempted


def count(outcomes: Iterable[ItemOutcome]) -> BatchCounts:
    """把执行结果汇成 FE-302 要显示的那一行数字。"""
    totals = {s: 0 for s in BatchItemStatus}
    unexplained = paid = reused = 0
    for outcome in outcomes:
        totals[outcome.status] += 1
        if outcome.status is BatchItemStatus.FAILED and not outcome.explainable:
            unexplained += 1
        paid += outcome.paid_calls
        if outcome.reused:
            reused += 1
    return BatchCounts(
        total=sum(totals.values()),
        pending=totals[BatchItemStatus.PENDING],
        running=totals[BatchItemStatus.RUNNING],
        succeeded=totals[BatchItemStatus.SUCCEEDED],
        failed=totals[BatchItemStatus.FAILED],
        needs_human=totals[BatchItemStatus.NEEDS_HUMAN],
        skipped=totals[BatchItemStatus.SKIPPED],
        unexplained=unexplained,
        paid_calls=paid,
        reused=reused,
    )


def count_rows(rows: Iterable[Mapping[str, object]]) -> BatchCounts:
    """同上,但输入是库里的行(dict 形状)。批次详情接口用它。

    单独一个函数而不是让调用方拼 `ItemOutcome`:落库的行没有 `PlannedItem`,
    硬造一个只为了复用计数会让"库里到底存了什么"变得难追。
    """
    totals = {s.value: 0 for s in BatchItemStatus}
    unexplained = paid = reused = unknown = 0
    for row in rows:
        status = str(row.get("status") or BatchItemStatus.PENDING.value)
        if status in totals:
            totals[status] += 1
        else:
            # R1-15:**不许静默丢掉。** 原来这里只有 `if status in totals`,
            # 认不出来的状态既不进任何一个桶,也不进 `total`(它是各桶之和)。
            # 后果是条目页列出 50 行、而汇总说 48 条 —— 两个数字对不上,
            # 而 §3.4 说这种时候必须有人知道。
            #
            # 触发需要 schema 变更或库损坏,所以它是"不该发生"而不是
            # "不会发生";不该发生的事发生时,要的是一个刺眼的数字,
            # 不是一个安静的差额。
            unknown += 1
        code = row.get("error_code")
        if status == BatchItemStatus.FAILED.value and not is_explainable(
            None if code is None else str(code)
        ):
            unexplained += 1
        paid += int(row.get("paid_calls") or 0)
        if row.get("reused"):
            reused += 1
    return BatchCounts(
        # `total` 数的是**行数**,不是各桶之和 —— 认不出的状态也是一行
        total=sum(totals.values()) + unknown,
        pending=totals[BatchItemStatus.PENDING.value],
        running=totals[BatchItemStatus.RUNNING.value],
        succeeded=totals[BatchItemStatus.SUCCEEDED.value],
        failed=totals[BatchItemStatus.FAILED.value],
        needs_human=totals[BatchItemStatus.NEEDS_HUMAN.value],
        skipped=totals[BatchItemStatus.SKIPPED.value],
        unexplained=unexplained,
        paid_calls=paid,
        reused=reused,
        unknown_status=unknown,
    )


def job_status(counts: BatchCounts) -> BatchJobStatus:
    """批次整体状态。**有失败也算"跑完了"**,不叫"批次失败"。

    §6.3 第三条:单件失败不导致整个批量任务失败。把批次标成 FAILED
    会让运营以为要整批重跑,而正确的动作是去处理那几件。
    """
    if counts.pending or counts.running:
        return BatchJobStatus.RUNNING
    if counts.failed:
        return BatchJobStatus.COMPLETED_WITH_ERRORS
    return BatchJobStatus.COMPLETED


# ---------------------------------------------------------------- 批次活性
#
# 解决的问题:**Broker 挂着的时候批次列表永远在转圈**。
#
# `BATCH_EXECUTION_MODE=celery` 下建批次会走 `dispatch_service.send_batch`,
# 它投递失败时返回 False —— 但那个 False 只出现在**创建那一次的响应里**,
# 不落库。刷新页面、或者别人打开这一页,那条信息就没了:看到的是一个
# 状态 QUEUED 的批次,而前端的规矩是"非终态就继续轮询",于是它转到天荒地老。
#
# ## 为什么不加一张 outbox
#
# `send_batch` 顶部已经写明批次**故意不写 TaskDispatch**:它的条目本身
# 就是意图记录,那批 PENDING 行在业务事务里落库,投递失败也丢不了。
# 再叠一层 outbox 是把同一件事记两遍。
#
# **这一条到 A35 仍然成立,任务 18 也没有推翻它** —— 缺的从来不是"意图存
# 不存得住",是"有没有人重新投递"。补的是那一半:
# `batch_service.redispatch_stalled_batches()` 定期把"还有 PENDING 却很久
# 没动"的批次重投一次,阈值就取下面那个 `BATCH_PICKUP_STALL_SECONDS`。
# 所以下面这句"点重试"今天是**兜底**而不是唯一出路(理由见
# `docs/DECISIONS.md` §3.12)。
#
# 于是库里可用的证据是这几列:批次的 `status` / `created_at` / `started_at`,
# 加上条目的 `finished_at`(心跳)、`status` 与 `updated_at`(重排时刻)。
# 全部**是真实来源**(硬规则第 4 条要的就是这个),阈值是配置,
# 不是为了把接口形状填满而写的常量。
#
# 条目那三列是 A34 补上的:只看批次那三列的话,「重试之后没派出去」这条
# 动线上批次的 status 停在终态,而判定会据此说"已结束"。详见 `liveness()`
# 的「两份 status,信条目那一份」。
#
# ## 阈值为什么比生成任务那边短得多
#
# `dispatch_policy.STALL_THRESHOLD_SECONDS` 是 1800 秒,并且带一句
# "宁可晚回收,不可误杀" —— 因为那边判错要**重跑一次 Provider 调用**,
# 真的花钱。这边不一样:判错的全部后果是多显示一句"可能没派出去,
# 点重试试试",而重试本身是幂等的(条目按 `idempotency_key` 去重,
# 已成功的不重跑)。误报便宜,漏报贵,所以取短的一侧。
#
# 文案也据此写成"可能没派出去",不写"已失败" —— 队列积压时它确实
# 只是在排队,而一句猜错的"已失败"会让运营去建第二个批次。

#: 批次建好之后多久还没有人认领,就该提醒有人去看。
#:
#: 这一档**不存在误杀风险**:`started_at` 还是 NULL 意味着没有任何 worker
#: 碰过它,不存在"正在跑的东西被判死"。最坏情况是队列真的积压了五分钟,
#: 而那本身也值得看一眼 —— 所以即使是积压导致的提醒也不算白报。
BATCH_PICKUP_STALL_SECONDS = 300

#: 一件 EXTRACT 的**单件预算**默认值。三项都取自识别链路自己的配置默认值
#: (`core/config.Settings`),不是估的 —— 原来这里的注释说"四张图是估的",
#: 而全仓其实早就有那个常量了(A45-batch18 / P2-1)。
#:
#: 原来这三个数取的是 **VISION_MODEL_\*(评分器)** 的配置,而批次里跑的
#: EXTRACT 读的是 **EXTRACTOR_MODEL_\***。两组键从一开始就是独立的
#: (`.env.example` 里那句"即便填同一个端点也要各填一遍"),于是这条不变量
#: 一直在拿另一条链路的参数守本条链路:
#:
#:     算的是   VISION_MODEL_TIMEOUT_SECONDS 90 × 3 × 4 张   = 1080 秒
#:     实际是   EXTRACTOR_MODEL_TIMEOUT_SECONDS 60 × 3 × 12 张 = 2160 秒
#:
#: 而 `ITEM_LEASE_SECONDS` 当时是 1800 —— 也就是说"租约必须长于单件最长
#: 合法耗时"这条不变量**在默认配置下根本不成立**,而下面那个 `if` 因为
#: 用了错的被除数,一次都没有红过。一件带 12 张图、每张接近超时并发生重试的
#: 合法 EXTRACT,会在 30 分钟后被回收器判过期、被另一个 worker 抢走,
#: 旧 worker 的结果随后被 fencing 丢弃 —— 钱花了,结果不算数。
DEFAULT_EXTRACT_TIMEOUT_SECONDS = 60      # EXTRACTOR_MODEL_TIMEOUT_SECONDS
DEFAULT_EXTRACT_MAX_RETRIES = 2           # EXTRACTOR_MODEL_MAX_RETRIES
DEFAULT_EXTRACT_MAX_IMAGES = 12           # EXTRACTOR_MAX_IMAGES_PER_RUN


def longest_legal_item_seconds(
    *,
    timeout_seconds: float = DEFAULT_EXTRACT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_EXTRACT_MAX_RETRIES,
    max_images: int = DEFAULT_EXTRACT_MAX_IMAGES,
) -> int:
    """单件最慢的**合法**耗时。**纯函数,三个入参都是真实配置项。**

    做成函数而不是只留一个常量,是因为这三项运维在设置页上能改
    (`core/settings_schema.py` 里 `EXTRACTOR_MODEL_TIMEOUT_SECONDS` /
    `EXTRACTOR_MODEL_MAX_RETRIES` / `EXTRACTOR_MAX_IMAGES_PER_RUN` 都在)。
    改完之后这条不变量还成不成立,得有一个地方能当场算 ——
    `batch_service.assert_lease_covers_configured_budget()` 在启动时调它。

    `1 + max_retries` 是传输层的往返次数(`llm/transport.MultimodalClient.send`
    的循环条件是 `attempt <= max_retries`),不是重试次数本身。
    这个 off-by-one 正是原来那版把 90 写成"一次视觉调用"却乘 3 的来源。
    """
    per_image = float(timeout_seconds) * (1 + max(0, int(max_retries)))
    return int(per_image * max(1, int(max_images)))


#: 默认配置下的单件预算:60 × 3 × 12 = 2160 秒。
LONGEST_LEGAL_ITEM_SECONDS = longest_legal_item_seconds()

#: 已经开跑的批次多久没有任何条目落地,就算 worker 中途没了。
#:
#: 这一档比上一档保守得多,因为它**确实可能误报**:一件跑得慢的付费调用
#: 和一个死掉的 worker,在库里长得一模一样(都是"很久没有新的 finished_at")。
#: 好在误报的全部后果是一句提示 + 轮询变慢,不改任何状态、不花任何钱 ——
#: 所以宁可晚报,也不必像 dispatch_policy 那边一样怕到底。
#:
#: A45-batch18 / P2-1:从 1800 抬到 2700。1800 小于修正后的
#: `LONGEST_LEGAL_ITEM_SECONDS`(2160),下面那条 `if` 会当场拒绝启动 ——
#: 那正是它的意义。取 2160 的 1.25 倍而不是两倍:这一档只是"报一句话",
#: 误报便宜(见上面那段权衡),所以仍然取偏短的一侧。
BATCH_PROGRESS_STALL_SECONDS = 2700

# **不能用 `assert`。** 模块级 assert 在 `python -O` 下被整条剥离 ——
# 而这类"配置常量互相矛盾"的不变量,恰恰是要在**生产**(最可能开 -O 的地方)
# 拦住的那一类。原来三处都是 assert,也就是三条只在开发机上生效的守卫。
if BATCH_PROGRESS_STALL_SECONDS <= LONGEST_LEGAL_ITEM_SECONDS:  # pragma: no cover
    raise RuntimeError(
        "进度停滞阈值必须大于单件最长合法耗时,否则正在正常跑的大批次会被报成卡住"
    )


class BatchLiveness(StrEnum):
    """这个批次还会不会自己往前走。**前端的轮询节拍读它,不自己推。**"""

    SETTLED = "SETTLED"   # 跑完了,不会再变
    LIVE = "LIVE"         # 在跑或刚建,继续轮询
    STALLED = "STALLED"   # 该动而长时间没动,需要人处理


BATCH_LIVENESS_LABELS: Mapping[BatchLiveness, str] = {
    BatchLiveness.SETTLED: "已结束",
    BatchLiveness.LIVE: "进行中",
    BatchLiveness.STALLED: "可能没派出去",
}


@dataclass(frozen=True)
class LivenessVerdict:
    state: BatchLiveness
    label: str
    #: 一句可执行的话。SETTLED / LIVE 时为 None —— 没话说的时候不硬凑一句
    advice: str | None
    #: 停滞了多久(秒)。非 STALLED 时为 None
    stalled_seconds: int | None


def _verdict(
    state: BatchLiveness, advice: str | None = None, stalled_seconds: int | None = None
) -> LivenessVerdict:
    """标签只从 `BATCH_LIVENESS_LABELS` 取一次。三处各写一遍的话,
    改文案时漏掉的那一处会安静地说旧话。"""
    return LivenessVerdict(state, BATCH_LIVENESS_LABELS[state], advice, stalled_seconds)


_NEVER_PICKED_UP_ADVICE = (
    "建好之后一直没有人认领。多半是消息没投出去(Broker 不可用),也可能是队列在排队。"
    "点「重试」会重新投递一次,已经成功的条目不会重跑"
)

_WORKER_GONE_ADVICE = (
    "已经开跑,但很久没有新的条目完成。多半是 worker 中途退出了。"
    "点「重试」会把没跑完的重新排队,已经成功的条目不会重跑"
)

#: 重试之后没人接手。**这是运营看到第一次停滞提示、照着提示点了「重试」
#: 之后必然走到的下一步**,所以它不能复用上面那句「建好之后一直没有人认领」
#: —— 那句话对着一个刚被人点过的批次说,读起来像是系统没收到那次点击。
_RETRY_NOT_PICKED_UP_ADVICE = (
    "重试已经把条目打回待处理,但之后一直没有人认领。多半是消息没投出去(Broker 不可用)。"
    "再点一次「重试」会重新投递,已经成功的条目不会重跑"
)


def liveness(
    *,
    status: str,
    created_at: float | None,
    started_at: float | None,
    last_progress_at: float | None,
    now: float,
    unfinished_items: int = 0,
    requeued_at: float | None = None,
    pickup_stall_seconds: int = BATCH_PICKUP_STALL_SECONDS,
    progress_stall_seconds: int = BATCH_PROGRESS_STALL_SECONDS,
) -> LivenessVerdict:
    """批次活性判定。时间一律传 epoch 秒,这个模块不认识 datetime 也不认识时区。

    `last_progress_at` 是"最后一个条目落地的时刻"——批次自己没有心跳列,
    但它的条目有 `finished_at`,那就是心跳。一个都还没落地时传 None,
    此时按 `started_at` 算(刚开跑本来就还没有条目完成)。

    ## 两份 status,信条目那一份(A34)

    `status` 是**存在 `batch_jobs.status` 里那个值**,而 `unfinished_items`
    是条目现在真实的样子。两者可以不一致,而且有一条**必然**走到不一致的动线:

        批次跑完(有失败)      job.status = COMPLETED_WITH_ERRORS
        运营点「重试」          条目回到 PENDING,job.status 不变
                               (`reset_items_for_retry` 只动条目)
        Broker 挂着,投递失败    没有任何人会去调 `run_batch()` 改 status

    此时只看 `status` 会返回 SETTLED —— 轮询彻底停止,界面显示「已完成
    (有失败)」,而库里躺着一批永远不会被执行的 PENDING 条目。
    **这恰好落在运营看到第一次停滞提示、照着提示点重试之后的那一步上。**

    所以终态 status 只有在**条目也确实没有未完成的**时候才算 SETTLED。
    调用方把两份事实一起交上来,判定层就不必再去猜哪一份是新的。

    `requeued_at` 是"这些未完成条目最近一次被放回待处理的时刻"。它必须是
    一个比 `created_at` 新的参照:一个三天前建的批次今天被重试,按 `created_at`
    算会立刻报"停滞了三天",而它其实刚被点过。库里对应的真实列是条目的
    `updated_at`(重置那次 UPDATE 会推进它),不是新造的字段。
    """
    try:
        job = BatchJobStatus(status)
    except ValueError:
        # 不认识的状态不猜。报 LIVE 的后果是继续轮询(和今天一样),
        # 报 STALLED 的后果是对着一个没人见过的状态喊"没派出去"
        return _verdict(BatchLiveness.LIVE)

    if job in {BatchJobStatus.COMPLETED, BatchJobStatus.COMPLETED_WITH_ERRORS}:
        if unfinished_items <= 0:
            return _verdict(BatchLiveness.SETTLED)
        # 存的 status 说跑完了,条目说还有没跑的。默认值 0 表示调用方
        # 没有交条目这一份事实,那就退回老行为(SETTLED)——
        # 不知道的时候不喊"卡住了",和未知 status 那一支同一条纪律
        reference = requeued_at
        if reference is None:
            # 退化顺序:重排时刻 > 最后落地 > 开跑 > 建批次。越靠前越新,
            # 取不到才退一档 —— 全取不到时不猜(LIVE),理由同上
            reference = last_progress_at
        if reference is None:
            reference = started_at
        if reference is None:
            reference = created_at
        if reference is None:
            return _verdict(BatchLiveness.LIVE)
        waited = int(now - reference)
        if waited >= pickup_stall_seconds:
            return _verdict(
                BatchLiveness.STALLED, _RETRY_NOT_PICKED_UP_ADVICE, waited
            )
        return _verdict(BatchLiveness.LIVE)

    if job is BatchJobStatus.QUEUED and started_at is None:
        # 还没有任何人认领过它
        if created_at is None:
            return _verdict(BatchLiveness.LIVE)
        waited = int(now - created_at)
        if waited >= pickup_stall_seconds:
            return _verdict(BatchLiveness.STALLED, _NEVER_PICKED_UP_ADVICE, waited)
        return _verdict(BatchLiveness.LIVE)

    # 开跑之后:心跳是最后一个条目落地的时刻,没有条目落地就按开跑时刻算
    heartbeat = last_progress_at if last_progress_at is not None else started_at
    if heartbeat is None:
        return _verdict(BatchLiveness.LIVE)
    idle = int(now - heartbeat)
    if idle >= progress_stall_seconds:
        return _verdict(BatchLiveness.STALLED, _WORKER_GONE_ADVICE, idle)
    return _verdict(BatchLiveness.LIVE)


# ---------------------------------------------------------------- 条目租约与回收
#
# 解决的问题:**worker 中途退出后,它领走的那几件永远停在 RUNNING。**
#
# 上面那套 `liveness()` 只是把这件事**说出来**(界面显示"可能没派出去,
# 点重试试试"),而 **A35 之前**没有任何东西把它修好:
#
#     run_batch()            只捞 PENDING,停在 RUNNING 的那几件它看不见
#     reset_items_for_retry  只挑 `retryable_rows()`,而那个函数要求 status
#                            必须是 FAILED —— RUNNING 不在里面
#
# 于是运营照着提示点"重试",得到的是 409「没有可重试的条目」。提示给了一个
# 按不动的按钮,这比没有提示更糟。任务 17(原子领取 + 租约)与任务 18
# (异常恢复)合起来才是那句提示的兑现。
#
# **上面那两行是历史,不是现状**:`run_batch` 今天经 `claim_items` 领取,
# 而领取的条件里已经包含"RUNNING 且租约过期";`reset_items_for_retry`
# 仍然只挑 FAILED,但被回收器放弃的条目会落成 FAILED + `WORKER_LOST`,
# 所以它拿得到重试按钮。留着这两行是因为它们解释了这一整节为什么存在 ——
# 删掉之后,下一个人看到的就只是"有个租约机制",不知道它换掉的是什么。
#
# ## 为什么租约必须比**一次领取**跑完的最长合法耗时长
#
# 回收一件**仍在跑**的条目 = 同一件事第二次调用付费模型。回执表
# (`batch_action_receipts`)挡得住"已经跑完的不重跑",挡不住"正在跑的
# 又来一次" —— 回执是调用**之后**才写的。所以这里唯一的防线就是租约本身
# 足够长,下面那条 assert 是它的看门人。
#
# **原来这里写的是"比单件最长合法耗时长",那个口径守不住。** 一次领取盖的是
# 一个共用的 `lease_until`,而这一批是顺序跑的,所以要比的是整批的耗时。
# 口径写窄了的后果不是少守一点,是这条防线在默认配置下**根本没有生效** ——
# 详见下面 `CLAIM_CHUNK` 那一段。
#
# 这与 `dispatch_policy.STALL_THRESHOLD_SECONDS` 是同一种权衡,方向也一致:
# 判错要花钱,所以宁可晚回收。注意它和上面 `BATCH_PROGRESS_STALL_SECONDS`
# 的取向**相反** —— 那个是"报一句话",误报便宜所以取短;这个是"改状态、
# 可能再花一次钱",误判贵所以取长。两个数字看起来都是 1800,但它们
# 不是同一个数,改动其中一个不要顺手改另一个。

#: 一次领取的租约时长(秒)。领走之后这么久没有落地,就认为 worker 没了。
#:
#: A45-batch18 / P2-1:从 1800 抬到 3600。1800 小于修正后的单件预算
#: (2160),也就是说"租约必须长于单件最长合法耗时"这条不变量原来
#: **在默认配置下不成立** —— 只是因为被除数取错了链路的参数,
#: 下面那条 `if` 一次都没有红过。
#:
#: 取 3600 而不是刚好 2160 + 一点:这一档的误判要花钱(回收一件仍在跑的
#: 条目 = 同一件事第二次调用付费模型),所以按上面那段权衡取长的一侧,
#: 留出接近一倍的余量。代价只是"worker 真的死了之后,这一件多等半小时
#: 才被回收",而那期间界面已经由 `BATCH_PROGRESS_STALL_SECONDS` 报了 STALLED。
ITEM_LEASE_SECONDS = 3600

#: 剩余租约低于这个比例时就该续租(A43 / BLOCK-02)。
#:
#: 取 0.5 而不是"快到期再续":续租本身是一次数据库往返,而它失败的那一刻
#: (租约已被回收器吊销)是**唯一**能在花钱之前停下来的时刻。留一半余量,
#: 是为了让"发现自己已经不是 owner 了"这件事发生在下一次付费调用之前,
#: 而不是发生在它之后。
LEASE_RENEW_RATIO = 0.5


def should_renew_lease(
    *, lease_until: float | None, now: float, lease_seconds: int = ITEM_LEASE_SECONDS
) -> bool:
    """还该不该续租。时间一律 epoch 秒,本模块不认识 datetime。

    A-41:默认值原来写死成字面量 `1800`,而 `ITEM_LEASE_SECONDS` 就在
    本模块上方十行处。调租约时长时改了常量、漏了这里,两个数字会分叉 ——
    表现是续租判据按旧时长算,而租约按新时长发,**且两边都不报错**。

    `None` 返回 True:没有租约列可读时按"该续"处理 —— 与 `lease_expired()`
    把 None 判成过期是同一个方向。多续一次是一条 UPDATE,漏续一次是
    一次可能被重复计费的执行。
    """
    if lease_until is None:
        return True
    return (lease_until - now) < lease_seconds * LEASE_RENEW_RATIO


def lease_still_held(
    *,
    status: str | None,
    lease_owner: str | None,
    lease_token: str | None,
    owner: str,
    token: str,
) -> bool:
    """这一行现在还归我跑吗(A43 / BLOCK-02)。**纯判定,四个条件缺一不可。**

    ## 为什么 `lease_until` 不在这四个条件里

    直觉上应该再加一条"租约还没过期"。**不能加。** 一个还没被回收器碰过、
    只是超时了的条目,它的执行权仍然在原 worker 手上 —— 真正的交接发生在
    回收器把 `lease_token` 吊销的那一刻,不是时钟走过某一秒的那一刻。
    把到期时刻写进这个判定,会让一件"跑超了但没人来抢"的条目连自己的
    结果都落不了库:钱已经花掉,结果却被自己丢掉。

    时间只决定**谁可以来抢**(`lease_expired`),归属由**令牌**决定。
    这是 fencing token 与单纯的超时租约之间的全部区别。

    ## 为什么 status 也要查

    人工重试(`reset_items_for_retry`)把行打回 PENDING 并吊销令牌,
    回收器放弃时把它打成 FAILED。两条路径都不经过"另一个 worker 领取",
    所以光比令牌不够 —— 令牌是 NULL 时 `None == None` 在 Python 里为真,
    而在 SQL 里为 NULL。两边行为不一致的判定不能留。
    """
    if status != BatchItemStatus.RUNNING.value:
        return False
    if not owner or not token:
        return False
    if lease_owner is None or lease_token is None:
        return False
    return lease_owner == owner and lease_token == token


#: 一次领几件。**这个数参与上面那条租约不变量,所以它必须住在这里。**
#:
#: 原来它在 `batch_service.py` 里,值是 10,旁边写着「10 件按单件最长合法耗时
#: 算是半小时的活,与 `ITEM_LEASE_SECONDS` 同量级 —— 再大就会出现『最后一件
#: 还没开跑,第一件的租约先到期了』」。**那句话算错了,而且错了 6 倍:**
#:
#:     10 × LONGEST_LEGAL_ITEM_SECONDS = 10 × 1080 = 10800 秒 = 180 分钟
#:     ITEM_LEASE_SECONDS              =                1800 秒 =  30 分钟
#:
#: 也就是说注释里那句「再大就会出现」描述的失效,在 10 这个值上**已经发生了**。
#: 一整批共用一个 `lease_until`(见 `batch_service.claim_items`:deadline 算一次,
#: 盖给全部 10 行),执行期间**从不续期**,而 `run_batch` 是顺序跑的 ——
#: 于是第 3 件开跑时租约就到期了,第 3～10 件全程在一个过期租约下执行。
#: `reap_expired_leases` 每 60 秒全表扫一遍,它不认识"owner 还活着"这件事
#: (系统里没有活体名单,`lease_owner` 明写只用于排查、不参与判定),
#: 于是它会从一个**正在正常工作**的 worker 手里把条目抢走放回 PENDING。
#: 后果是重叠执行、`attempts` 虚增、`CONCURRENT_IN_FLIGHT` 假失败、
#: 以及 `_apply_outcome` 无条件覆写 status 造成的状态互踩。
#:
#: 首期取 1:一件一领,租约与执行一一对应,上面那条不变量退化成原来那条
#: 单件断言,不需要引入续租。代价是每件多一次 `SELECT ... FOR UPDATE`,
#: 而那一次往返和一次付费出图调用比可以忽略。
#:
#: 要把它调大,**必须先有续租**(执行每一件之前把 `lease_until` 往后推),
#: 否则这条 assert 会当场拦住 —— 那正是它存在的意义。
#:
#: ## A43:续租有了,这个数仍然是 1
#:
#: `batch_service.renew_lease()` 落地之后,上面那句"必须先有续租"的前提
#: 已经满足。**但这个数不跟着改。** 理由是这条 assert 守的不是续租有没有,
#: 是**这一次领取的全部条目顺序跑完**会不会超过一个租约周期 —— 续租只能
#: 保证"正在跑的那一件"不被抢走,保证不了排在它后面、还没开跑的那 9 件。
#: 要调大它,得先让续租覆盖整批(领取时按 chunk 大小算 deadline,或者
#: 每跑完一件就给剩下的续一次),那是另一件事,不是本轮做的事。
CLAIM_CHUNK = 1

# 同上:这一条守的是"会不会回收正在跑的条目",而回收的代价是重复付费。
# 用 assert 的话它在 `python -O` 下不存在,而那正是生产。
if ITEM_LEASE_SECONDS <= CLAIM_CHUNK * LONGEST_LEGAL_ITEM_SECONDS:  # pragma: no cover
    raise RuntimeError(
        "租约必须长于**这一次领取的全部条目**顺序跑完的最长合法耗时,"
        "否则会回收正在跑的条目 —— 回执挡不住重复调用,因为回执是调用之后才写的。"
        "注意乘 CLAIM_CHUNK:整批共用一个 lease_until 且执行期间不续期,"
        "所以单件断言守不住,原来那一版就是这么漏过去的"
    )

#: 同一条目最多自动执行几次(首次 + 自动重排)。超过之后落 `WORKER_LOST`,
#: 由人决定要不要再点重试。
#:
#: 上限存在的理由是钱:一件能把 worker 打死的条目(超大图 OOM)会被无限
#: 回收,每一轮都可能是一次真金白银的调用,而**没有人在看**。有了上限,
#: 同一个故障的表现从"预算悄悄见底"变成"界面上一条可解释的失败"。
#:
#: ## 人工重试之后会发生什么(A41 补,别再重新推一遍)
#:
#: `reset_items_for_retry()` **刻意不重置** `attempts` —— 那一列是台账,
#: 界面上显示成"执行 N 次 / 付费 M 次",清零等于把已经花掉的钱从账上抹掉。
#: 于是一件已经落进 `WORKER_LOST`(attempts=3)的条目,人点一次重试就是
#: attempts=4,worker 再死一次会**立刻**回到 `WORKER_LOST`,而不是再自动排两次。
#:
#: 这是对的,不是缺陷:上限管的是**无人值守**时的花费,而人点重试的那一刻
#: 就是有人在看了 —— 一次点击换一次尝试,下一次要不要再花,仍然由他决定。
MAX_ITEM_ATTEMPTS = 3


class ReclaimAction(StrEnum):
    """租约过期的条目该怎么处理。"""

    REQUEUE = "REQUEUE"   # 放回 PENDING,等下一个 worker 领
    GIVE_UP = "GIVE_UP"   # 落 FAILED + WORKER_LOST,交给人


@dataclass(frozen=True)
class ReclaimVerdict:
    action: ReclaimAction
    attempts: int
    #: 一句给日志和界面看的话。两个分支都有 —— 回收是个会改状态的动作,
    #: 事后总有人要问"这件为什么又跑了一遍"
    reason: str


def lease_expired(*, lease_until: float | None, now: float) -> bool:
    """租约到期了吗。时间一律 epoch 秒,本模块不认识 datetime。

    **`None` 算过期。** 这一条与 `publish_service.claim_due` 里
    `next_attempt_at IS NULL` 那条是同一个坑的两面:可空列参与比较时,
    SQL 里 `NULL <= now` 求值为 NULL 而不是真。那边漏掉 NULL 的后果是
    一行永远领不到;这边如果把 NULL 当成"永不过期",后果是 0025 之前
    创建的、正卡在 RUNNING 的存量行**永远回收不了** —— 而那批行正是
    这一整节代码要修的东西。

    没有租约列可读时按"没人在跑"处理,是这两个方向里安全的那个:
    多回收一次的代价由 `MAX_ITEM_ATTEMPTS` 兜住,漏回收没有兜底。
    """
    if lease_until is None:
        return True
    return lease_until <= now


def reclaim_verdict(
    *, attempts: int, max_attempts: int = MAX_ITEM_ATTEMPTS
) -> ReclaimVerdict:
    """一件租约过期的条目:再排一次,还是交给人。

    `attempts` 传库里那一列的当前值(已经包含刚刚断掉的那一次)。

    人工重试不清 `attempts`(理由见 `MAX_ITEM_ATTEMPTS`),所以这个值**可以
    大于上限**。原来的文案在那种情况下会写出"已执行 4 次仍未落地(上限 3)",
    读起来像是自动重排失控了 —— 而事实相反:那 4 次里有一次是人自己点的。
    超过上限时改说"这一次",不再报那个会引起误会的比较。
    """
    if attempts >= max_attempts:
        reason = (
            f"已执行 {attempts} 次仍未落地(自动重排上限 {max_attempts}),不再自动重排"
            if attempts == max_attempts
            else f"这一次执行同样没有落地(已累计 {attempts} 次),不再自动重排"
        )
        return ReclaimVerdict(ReclaimAction.GIVE_UP, attempts, reason)
    return ReclaimVerdict(
        ReclaimAction.REQUEUE,
        attempts,
        f"第 {attempts} 次执行的租约已过期,放回队列",
    )


def retryable_rows(rows: Iterable[Mapping[str, object]]) -> list[str]:
    """哪些条目值得重试(FE-304)。

    只挑 `FAILED` 且异常码本身可重试的。`NEEDS_HUMAN` 一律不重试 ——
    重试"草稿缺必填字段"一百次,结果一百次相同,唯一效果是让运营
    以为系统在努力。`SUCCEEDED` 也不重试:那正是"不重复触发付费调用"。
    """
    out: list[str] = []
    for row in rows:
        if str(row.get("status")) != BatchItemStatus.FAILED.value:
            continue
        code = row.get("error_code")
        if code is None:
            continue
        if error_meta(str(code)).retryable:
            out.append(str(row.get("id")))
    return out


# ==========================================================
# 历史批次文件是否还能传平台(OPS-REVIEW P4)
# ==========================================================
#
# 运营的真实疑问只有一句:**"上周导的那个压缩包,现在还能传平台吗?"**
#
# 批次页原本只回答"那次跑成功了几件"。跑成功是**过去式**,而文件要传的是
# 现在的平台。中间隔了一周:有人改了属性、有人派生了图片集新版、模板换过版。
# 于是那个压缩包里可能有一半是旧内容 —— 传上去要么被平台驳回(回到 P1),
# 要么静默上架了错的内容,而后者只有等客诉才会被发现。
#
# 判定放在纯函数里的理由同 flow.py:它要被穷举测试,而只要能查库就没人会写
# 那个穷举测试。`batch_service.file_audit` 负责查库、按 SPU 去重,并且**过期与否
# 一律问 `service.refresh_draft`** —— 不在这里凭指纹自己推断"是不是过期了"。
# 两处各判一次的话,批次页会说"3 件过期"而草稿页说"5 件过期",
# 而 §3.4 明令禁止的正是这两个数字。


class FileState(StrEnum):
    """历史批次里某一件商品的导出文件,现在还算不算数。

    顺序即优先级(见 `file_state`):一件商品可能同时满足多条,
    但界面上只显示一条 —— 显示最"堵路"的那条,因为它决定了下一步动作。
    """

    DRAFT_MISSING = "DRAFT_MISSING"      # 草稿行没了(商品删了、SPU 改了)
    UPSTREAM_STALE = "UPSTREAM_STALE"    # 草稿现在是过期状态:重导会被闸拦住
    SUPERSEDED = "SUPERSEDED"            # 草稿其后重建过:文件里是旧内容
    UNKNOWN = "UNKNOWN"                  # 那次导出没留指纹,无从比对
    FRESH = "FRESH"                      # 与当前草稿一致,可以传


FILE_STATE_LABELS: Mapping[FileState, str] = {
    FileState.DRAFT_MISSING: "草稿已不存在",
    FileState.UPSTREAM_STALE: "草稿已过期",
    FileState.SUPERSEDED: "已被新版取代",
    FileState.UNKNOWN: "无法比对",
    FileState.FRESH: "仍然有效",
}

#: 每种状态该做什么。**必须是一句能直接执行的话** ——
#: "已被新版取代"本身不告诉运营该干什么,而运营此刻手里正拿着一个
#: 不知道该不该传的压缩包。
FILE_STATE_ADVICE: Mapping[FileState, str] = {
    FileState.DRAFT_MISSING: "这件商品的草稿已不存在,压缩包里它那几行不要再传;确认商品是否已下线",
    FileState.UPSTREAM_STALE: "先到草稿页重新生成草稿(上游变过),然后重新导出",
    FileState.SUPERSEDED: "当前草稿是新的,直接重新导出一次即可,不必重新生成",
    FileState.UNKNOWN: "这次导出没留下指纹(旧版本记录),无法确认;保守起见重新导一次",
    FileState.FRESH: "内容与当前一致,可以继续用",
}

#: 会让「重新下载这个批次」被后端拒绝的状态。
#:
#: `batch_service.batch_file` 合并文件时对每一件重跑 `export_gate`,
#: 而那道闸对过期草稿一律 409。所以这两种状态下点下载只会拿到一个错误提示 ——
#: 页面提前把话说了,运营就不用靠撞一次错来学这件事。
BLOCKS_REDOWNLOAD: frozenset[FileState] = frozenset(
    {FileState.DRAFT_MISSING, FileState.UPSTREAM_STALE}
)


def file_state(
    *,
    exported_fingerprint: str | None,
    current_fingerprint: str | None,
    draft_exists: bool,
    draft_stale: bool,
) -> FileState:
    """一件商品的导出文件现在算不算数。

    `draft_stale` 由调用方从 `service.refresh_draft` 取,不在这里推断 ——
    过期的判定口径只有一处(理由见本节开头)。

    优先级:草稿没了 > 草稿过期 > 被新版取代 > 无法比对 > 有效。
    过期排在"被取代"前面,因为两者都成立时(草稿重建过、又再次过期)
    该做的事是**先重新生成草稿**;先告诉运营"重新导一次就行"会让他撞上导出闸。
    """
    if not draft_exists:
        return FileState.DRAFT_MISSING
    if draft_stale:
        return FileState.UPSTREAM_STALE
    exported = (exported_fingerprint or "").strip()
    current = (current_fingerprint or "").strip()
    if not exported or not current:
        return FileState.UNKNOWN
    if exported != current:
        return FileState.SUPERSEDED
    return FileState.FRESH


@dataclass(frozen=True)
class FileStateEntry:
    """一件商品一条。`product_id` 用来一键跳到它的草稿页。"""

    product_id: str
    sku: str
    spu: str
    state: FileState
    exported_fingerprint: str | None
    current_fingerprint: str | None


@dataclass(frozen=True)
class FileAudit:
    """整个批次的结论。界面上就是一条横幅 + 一张可展开的表。"""

    entries: tuple[FileStateEntry, ...] = ()

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def by_state(self) -> dict[FileState, tuple[FileStateEntry, ...]]:
        out: dict[FileState, list[FileStateEntry]] = {}
        for entry in self.entries:
            out.setdefault(entry.state, []).append(entry)
        return {k: tuple(v) for k, v in out.items()}

    @property
    def stale_count(self) -> int:
        """不该再传平台的件数。UNKNOWN 不计入 —— 它是"不确定",不是"确定失效"。"""
        return sum(1 for e in self.entries if e.state not in (FileState.FRESH, FileState.UNKNOWN))

    @property
    def unknown_count(self) -> int:
        return sum(1 for e in self.entries if e.state is FileState.UNKNOWN)

    @property
    def blocks_redownload(self) -> bool:
        return any(e.state in BLOCKS_REDOWNLOAD for e in self.entries)

    @property
    def usable(self) -> bool:
        """整包是否还能原样传平台。有一件失效就不是"可以传"。"""
        return self.total > 0 and self.stale_count == 0 and self.unknown_count == 0


def audit_file_states(rows: Iterable[Mapping[str, object]]) -> FileAudit:
    """把 service 查出来的事实行摊成结论。

    每行:{product_id, sku, spu, exported_fingerprint, current_fingerprint,
          draft_exists, draft_stale}
    """
    entries: list[FileStateEntry] = []
    for row in rows:
        entries.append(
            FileStateEntry(
                product_id=str(row.get("product_id") or ""),
                sku=str(row.get("sku") or ""),
                spu=str(row.get("spu") or ""),
                state=file_state(
                    exported_fingerprint=(
                        None
                        if row.get("exported_fingerprint") is None
                        else str(row.get("exported_fingerprint"))
                    ),
                    current_fingerprint=(
                        None
                        if row.get("current_fingerprint") is None
                        else str(row.get("current_fingerprint"))
                    ),
                    draft_exists=bool(row.get("draft_exists")),
                    draft_stale=bool(row.get("draft_stale")),
                ),
                exported_fingerprint=(
                    None
                    if row.get("exported_fingerprint") is None
                    else str(row.get("exported_fingerprint"))
                ),
                current_fingerprint=(
                    None
                    if row.get("current_fingerprint") is None
                    else str(row.get("current_fingerprint"))
                ),
            )
        )
    return FileAudit(entries=tuple(entries))


def file_audit_banner(audit: FileAudit) -> str | None:
    """横幅上的那一句话。没什么可说时返回 None —— 一条"一切正常"的横幅
    出现三次之后就没人再读横幅了,包括真的出问题那次。
    """
    if audit.total == 0:
        return None
    if audit.stale_count == 0 and audit.unknown_count == 0:
        return None
    parts: list[str] = []
    if audit.stale_count:
        parts.append(f"其中 {audit.stale_count} 件已过期,别再传平台")
    if audit.unknown_count:
        parts.append(f"{audit.unknown_count} 件无法比对(旧记录没留指纹)")
    tail = (
        ";这个批次已不能整包重新下载,先按下面的提示逐件处理"
        if audit.blocks_redownload
        else ";重新导出一次即可拿到最新内容"
    )
    return "、".join(parts) + tail


# ---------------------------------------------------------------- 产出方式指纹


#: 允许的文案生成器名。**`core/config.py` 与 `listings/copy_generator.py`
#: 共用这一份**,不各写一份字符串:两边漂移的表现是新生成器在启动期被拒,
#: 而报错信息说的是「只能是 template 或 llm」—— 指着一份已经过期的白名单。
#:
#: 同上,定义在 `app/core/vocab.py`,这里重新导出。
COPY_GENERATORS = _vocab.COPY_GENERATORS
TEXT_API_STYLES = _vocab.TEXT_API_STYLES


def generator_fingerprint(
    *,
    generator: str,
    prompt_version: str,
    rules_version: str,
    model: str | None = None,
    api_style: str | None = None,
) -> str:
    """「除了上游事实之外,还有什么会改变文案产出」的指纹(评审第 3 条)。

    **纯函数,配置由调用方传入。** 和 `channels/generic.map_fields` 同一个
    理由:一旦它能自己去读 settings,就没法在纯测试里穷举「换了这个会不会
    让指纹变」,而那恰恰是这个函数唯一要保证的事。

    ## 它解决的问题

    原来批次幂等指纹只看上游数据:素材 id、属性版本、图片集与文案状态。
    于是换 Qwen 型号、换提示词、换输出 Schema、换禁用词规则之后,指纹
    一个字不变 —— 旧结果被当成新配置的产物复用,而运营看到的是「已复用」,
    没有任何地方提示配置换过。「不重复触发付费调用」这条本意是省钱,
    在这里变成了拿旧结果冒充新配置的产物。

    ## 什么不进指纹

    **API Key 不进。** 它不改变输出,而指纹会进日志和接口响应 ——
    换一次密钥就让全批重跑一遍,也纯属浪费。

    时间戳不进(同 `_fingerprint` 的原注释):每次都变,幂等就永远不命中。
    """
    parts = [f"gen:{(generator or '?').strip()}"]
    if (generator or "").strip() == "llm":
        # 模型名和 api 形状都会改变输出。空值折成显式的 '?' 而不是空串:
        # 空串会和「配置为空」撞在一起,两种情况在指纹里就分不开了
        parts.append(f"model:{(model or '?').strip() or '?'}")
        parts.append(f"style:{(api_style or '?').strip() or '?'}")
    parts.append(f"prompt:{prompt_version}")
    parts.append(f"rules:{rules_version}")
    return "|".join(parts)
