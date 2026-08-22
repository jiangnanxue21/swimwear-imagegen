"""真实提交的清理预案(方案 v4.1 任务 16 / 4.1 节 H / 10.7 节)。

4.1 节 H 那段话的结论写得很直白:

> 没有这一条的后果不是技术故障,而是测试期间往真实店铺里塞进一批无人认领的
> 商品,而且没人说得清一共有几个。

所以这个模块回答三个问题,顺序不能反:

    列清单   `inventory()`   本轮测试在平台上创建了哪些外部商品
    核对     `verify()`      本地记的这些,平台上是不是真的还在、是不是都下架了
    下架     `plan_delist()` / `run_delist()`

## 为什么"列清单"和"下架"是两个动作

清理是不可逆的:在平台上下架一个商品,撤不回来。所以这里刻意做成
「先看,再做」——`plan_delist()` 返回一份计划,`run_delist()` 才真的排队。
做成一个带 `dry_run=False` 的函数也能实现同样的效果,但那样"看一眼"和
"动手"共用一条代码路径,而参数是最容易在复制粘贴时被改掉的东西。

## 作用域是强制的

`_require_scope()` 会在没有任何过滤条件时直接拒绝。一个不小心跑成全量的
清理脚本,会把生产上所有在架商品下架 —— 这个模块存在的目的是防止事故,
它自己不能是事故的来源。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.channels.registry import is_simulated_channel
from app.core.clock import iso_utc, utc_now
from app.core.enums import PublishOperation, PublishStatus
from app.core.errors import ErrorCode, ValidationError
from app.core.logging import get_logger
from app.models.listing_copy import ListingDraft
from app.models.product import Product
from app.models.publishing import ChannelListing
from app.services import poll_service, publish_service

logger = get_logger(__name__)

#: 平台上「还占着一个位置」的状态。清理要清的就是这一批。
#:
#: `SUBMIT_RESULT_UNKNOWN` 在里面,而这一条是整个集合里最重要的:它表示
#: 我们**不知道**平台上有没有这个商品。清理清单必须把不确定的那些也列出来 ——
#: 4.1 节 H 要的是"数量一致",而一个被漏掉的不确定项恰恰是数量对不上的
#: 最常见来源。宁可让人多核对一行,不可以让一行悄悄不在清单里。
OCCUPYING_STATUSES: frozenset[str] = frozenset(
    {
        PublishStatus.SUBMITTED.value,
        PublishStatus.PLATFORM_REVIEWING.value,
        PublishStatus.PLATFORM_REJECTED.value,
        PublishStatus.LISTED.value,
        PublishStatus.UPDATING.value,
        PublishStatus.UPDATE_FAILED.value,
        PublishStatus.REPUBLISHING.value,
        PublishStatus.PAUSED.value,
        PublishStatus.DELISTING.value,
        PublishStatus.SUBMIT_RESULT_UNKNOWN.value,
    }
)


@dataclass(frozen=True)
class ExternalListing:
    """清单上的一行。**纯数据**,可以直接序列化进报表或接口。"""

    listing_id: UUID
    channel: str
    shop_id: str
    site: str
    spu: str
    sku: str
    #: 平台侧 SPU ID。**可以是空串** —— 一次 CREATE 超时后本地落
    #: `SUBMIT_RESULT_UNKNOWN` 时,商品可能已经在平台上建好了,而我们
    #: 没拿到 ID。那种行恰恰是最需要人去核对的一行(A45-batch18 / P1-3)
    external_spu_id: str
    status: str
    last_platform_status: str | None
    test_batch_tag: str | None
    created_at: datetime | None
    #: 这个外部 ID 是 Simulator 造的。**真实平台上并不存在这个商品**
    simulated: bool
    #: 还占着平台上的位置(见 `OCCUPYING_STATUSES`)
    occupying: bool
    #: 能不能自动下架。False 时 `reason` 说明为什么
    delistable: bool
    reason: str | None = None
    #: **必须由人去平台后台核对的一行**(A45-batch18 / P1-3)。
    #:
    #: 今天只有一种来源:状态是 `SUBMIT_RESULT_UNKNOWN` 且没有外部 ID ——
    #: 提交到达过平台没有,本系统答不出来。自动下架处理不了它
    #: (没有 ID 就构造不出下架报文),但它绝不能从清单上消失:
    #: 4.1 节 H 要的是"数量一致",而一个被漏掉的不确定项正是数量对不上的
    #: 最常见来源。
    needs_reconcile: bool = False
    #: 人拿着去平台后台找这件商品的线索。没有外部 ID 时它是唯一的入口
    locator: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "listing_id": str(self.listing_id),
            "channel": self.channel,
            "shop_id": self.shop_id,
            "site": self.site,
            "spu": self.spu,
            "sku": self.sku,
            "external_spu_id": self.external_spu_id,
            "status": self.status,
            "last_platform_status": self.last_platform_status,
            "test_batch_tag": self.test_batch_tag,
            "created_at": iso_utc(self.created_at),
            "simulated": self.simulated,
            "occupying": self.occupying,
            "delistable": self.delistable,
            "reason": self.reason,
            "needs_reconcile": self.needs_reconcile,
            "locator": dict(self.locator),
        }


@dataclass(frozen=True)
class CleanupReport:
    """一次清单核对的结果(4.1 节 H 的最后一项:数量一致且全部已下架)。"""

    rows: tuple[ExternalListing, ...] = ()
    total: int = 0
    simulated: int = 0
    real: int = 0
    occupying: int = 0
    not_delistable: int = 0
    #: **这次动手真的会排掉几行**(含模拟行)。
    #:
    #: 和 `occupying` 不是一个口径,而这个区别是 A41 补上的:`occupying` /
    #: `not_delistable` 只数真实商品(它们回答的是「平台上还占着几个位置」),
    #: 而 `run_delist` 遍历的是 `plan.rows` —— **模拟行照样排队**
    #: (`include_simulated` 默认 True,而人工测试批次里几乎全是 SIM- 行)。
    #:
    #: 于是 10 模拟 + 2 真实的批次,计划显示「仍占着平台位置:2」,
    #: 实际排出去 12 条。这个模块的整套安全设计就是「强制限定作用域 +
    #: 动手前把数量给人看」(见 `_require_scope`),给人看的数字和实际执行的
    #: 数量不是一个,那道护栏就只剩一半。
    to_queue: int = 0
    to_skip: int = 0
    #: **还没核清的不确定行**(A45-batch18 / P1-3)。
    #:
    #: 状态是"提交结果未知"且没有外部 ID:平台上可能有这件商品,也可能没有,
    #: 本系统答不出来。它们不占 `occupying`(那个数问的是"平台上还占着
    #: 几个位置",而这些行的答案是不知道),但它们必须让 `clean` 变成 False
    unreconciled: int = 0
    #: 核对时逐行问平台的结果分桶(只有 `verify()` 会填)
    probe: Mapping[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """全部清理干净了吗。

        判据是**真实**商品里没有一个还占着位置。模拟的那些不算 ——
        它们从来没有出现在任何平台上,把它们计入"没清干净"会让这个
        布尔量在人工测试期间永远是 False,然后没人再看它。

        ## 不确定的那些也算没清干净(A45-batch18 / P1-3)

        `unreconciled` 是"提交结果未知且没有外部 ID"的行。原来它们
        根本不在清单上,于是一次 CREATE 到达平台、响应在返回 ID 前超时的
        商品会**同时**从 inventory、plan、verify 里消失,而报告说 clean=true。
        平台上留着一个孤儿商品,本系统检索不到、核对不了、下架不掉。

        `clean` 是 4.1 节 H 真正要的那个结论,也是 CLI 的退出码。
        它必须在"我不知道"的时候说 False —— 一个把不确定说成干净的布尔量,
        比没有这个布尔量更危险。
        """
        return self.occupying == 0 and self.unreconciled == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "simulated": self.simulated,
            "real": self.real,
            "occupying": self.occupying,
            "not_delistable": self.not_delistable,
            "to_queue": self.to_queue,
            "to_skip": self.to_skip,
            "unreconciled": self.unreconciled,
            "clean": self.clean,
            "probe": dict(self.probe),
            "rows": [r.as_dict() for r in self.rows],
        }


def _require_scope(
    *,
    test_batch_tag: str | None,
    shop_id: str | None,
    channel: str | None,
    destructive: bool = False,
) -> None:
    """至少给一个过滤条件,否则拒绝。**动手时的门槛比看一眼高。**

    这不是防御性编程,是这个模块的核心约束。一次跑成全量的清理会把生产上
    所有在架商品下架,而那个操作在平台上通常撤不回来。要求显式作用域的代价
    是多打一个参数;不要求的代价是一次手滑等于一次事故。

    ## 为什么"只给渠道"可以看、不可以动(A45-batch18 / P1-4)

    原来三个条件是 `any(...)`,预览和执行共用同一道闸。于是

        cleanup_test_listings delist --channel GENERIC --apply

    会把 GENERIC 渠道下**全部店铺、全部批次**里满足状态条件的真实 listing
    逐行排进 DELIST 队列 —— 包括别人的店铺和根本不属于本轮测试的商品。
    一次参数遗漏(少打一个 `--tag`)就扩大成一次生产批量下架。

    而 REVIEW.md 4.1 节 H 对真实提交的要求本来就是"严格位于专用测试店铺/
    类目并与生产凭证分离"。渠道不是那个作用域:它是一整个平台。

    所以破坏性执行必须由 `test_batch_tag` 或 `shop_id` 之一限定 ——
    这两个都是"这一批 / 这一家"的口径,而 `channel` 是"这个平台"的口径。
    预览不受这一条限制:看一眼整个渠道有哪些行,恰恰是决定要不要动手
    之前该做的事。
    """
    if not any((test_batch_tag, shop_id, channel)):
        raise ValidationError(
            "清理必须限定作用域:至少给 test_batch_tag / shop_id / channel 之一。"
            "不限定的话这次调用会覆盖全部渠道的全部商品。",
            code=ErrorCode.INPUT_INVALID,
            http_status=400,
        )
    if destructive and not any((test_batch_tag, shop_id)):
        raise ValidationError(
            "只给渠道不能执行下架:那会覆盖该渠道下全部店铺、全部批次的商品,"
            "包括不属于本轮测试的。请再给 test_batch_tag(推荐)或 shop_id。"
            "只想看一眼的话,不加 --apply 的预览允许只给渠道。",
            code=ErrorCode.INPUT_INVALID,
            http_status=400,
        )


def inventory(
    session: Session,
    *,
    test_batch_tag: str | None = None,
    shop_id: str | None = None,
    channel: str | None = None,
    include_simulated: bool = True,
) -> CleanupReport:
    """列出「本轮测试创建了哪些外部商品」(4.1 节 H 第二条)。

    ## 两类行,少一类都不成立

        拿到过 external_spu_id      平台上确实有这件商品
        SUBMIT_RESULT_UNKNOWN       **不知道**平台上有没有(A45-batch18 / P1-3)
        且没有 external_spu_id

    第二类原来被 `external_spu_id IS NOT NULL AND != ''` 挡在清单外,
    理由写的是"没有外部 ID 就等于那次提交从没到达平台"。**那句话是错的**,
    而且本仓自己的判定就说它是错的:`workflows/publish_policy` 明确允许
    一次 CREATE 在超时、或在 201/409 没带 ID 时落成 `SUBMIT_RESULT_UNKNOWN`
    (见 `tests/pure/test_publish_policy.py` 的
    `test_a_timed_out_create_is_never_resent_automatically`)。那种情况下
    商品可能已经在平台上建好了,只是我们没拿到它的 ID。

    被漏掉的后果正是 4.1 节 H 那段话描述的结局:那一行从 inventory、
    plan、verify 里**同时**消失,清理报告说 clean=true,而平台上留着一个
    谁也检索不到、核对不了、下架不掉的孤儿商品。

    这类行标 `needs_reconcile=True`、`delistable=False`(没有 ID 就构造不出
    下架报文),并带上 `locator` —— 人拿着店铺 / SPU / 批次 / 时间去平台
    后台找它,那是唯一的入口。

    `include_simulated=False` 是给真实清理用的:拿着一份混着 SIM- 的清单去
    平台后台核对,核不上,然后这份清单就没人信了。**不确定行不受它影响** ——
    没有外部 ID 就无从判断是不是模拟的,而"无从判断"必须留在清单上。
    """
    _require_scope(test_batch_tag=test_batch_tag, shop_id=shop_id, channel=channel)

    stmt = (
        select(ChannelListing, Product)
        .join(Product, Product.id == ChannelListing.product_id)
        .where(
            or_(
                and_(
                    ChannelListing.external_spu_id.is_not(None),
                    ChannelListing.external_spu_id != "",
                ),
                # 没有 ID 但结果未知 —— 这一条正是本次要补的那一类
                ChannelListing.status == PublishStatus.SUBMIT_RESULT_UNKNOWN.value,
            )
        )
        .order_by(ChannelListing.created_at)
    )
    if test_batch_tag:
        stmt = stmt.where(ChannelListing.test_batch_tag == test_batch_tag)
    if shop_id:
        stmt = stmt.where(ChannelListing.shop_id == shop_id)
    if channel:
        stmt = stmt.where(ChannelListing.channel == channel.strip().upper())

    rows: list[ExternalListing] = []
    for listing, product in session.execute(stmt).all():
        has_id = bool((listing.external_spu_id or "").strip())
        # 判据是这一行的渠道现在挂着哪个 transport,不是外部 ID 的前缀
        # (PRD §10.2 第 3 条)。前缀那条判据对**没有 ID 的行**答不了,
        # 而那恰恰是最需要判断的一类
        simulated = is_simulated_channel(listing.channel)
        # `include_simulated=False` 只筛掉**有 ID 且确定是模拟的**那些。
        # 没有 ID 的行一律留下:4.1 节 H 要的是数量一致,而一个被漏掉的
        # 不确定项正是数量对不上的最常见来源
        if simulated and not include_simulated and has_id:
            continue
        rows.append(_row(listing, product, simulated=simulated, has_id=has_id))

    return _report(rows)


def _row(
    listing: ChannelListing, product: Product, *, simulated: bool, has_id: bool = True
) -> ExternalListing:
    occupying = listing.status in OCCUPYING_STATUSES
    delistable, reason = _delistable(listing, occupying=occupying, has_id=has_id)
    needs_reconcile = not has_id
    return ExternalListing(
        listing_id=listing.id,
        channel=listing.channel,
        shop_id=listing.shop_id,
        site=listing.site,
        spu=product.spu,
        sku=product.sku,
        # `str(None)` 会得到字面量 "None" —— 它会出现在界面、接口和运营手里的
        # 清单上,看起来像一个真的平台 ID。这一列的契约是"可以是空串"
        external_spu_id=(listing.external_spu_id or "").strip(),
        status=listing.status,
        last_platform_status=listing.last_platform_status,
        test_batch_tag=listing.test_batch_tag,
        created_at=listing.created_at,
        simulated=simulated,
        occupying=occupying,
        delistable=delistable,
        reason=reason,
        needs_reconcile=needs_reconcile,
        locator=_locator(listing, product) if needs_reconcile else {},
    )


def _locator(listing: ChannelListing, product: Product) -> dict[str, Any]:
    """没有外部 ID 时,人拿什么去平台后台找这件商品(A45-batch18 / P1-3)。

    全部来自库里已有的列,不新增存储:店铺 + SPU 是平台后台搜索框认的两个
    维度,批次号回答"是不是本轮测试的",时间窗回答"往哪一段翻"。

    时间给的是**提交时刻前后各一小时**而不是一个精确点:平台后台的列表
    按它自己的受理时间排,两边差几分钟是常态,而一个精确到秒的时间戳
    会让人以为可以按它精确过滤,然后什么都搜不到。
    """
    created = listing.created_at
    return {
        "shop_id": listing.shop_id,
        "channel": listing.channel,
        "site": listing.site,
        "spu": product.spu,
        "sku": product.sku,
        "test_batch_tag": listing.test_batch_tag,
        "submitted_around": iso_utc(created),
        "search_window_hours": 1,
        "hint": (
            "本地状态是「提交结果未知」且没有平台 SPU ID:这次 CREATE 可能已经"
            "在平台上建成了商品。请按店铺 + SPU 在平台后台搜索,"
            "找到就手工下架并回填 ID,没找到再把本地这行结掉。"
        ),
    }


def _delistable(
    listing: ChannelListing, *, occupying: bool, has_id: bool = True
) -> tuple[bool, str | None]:
    """这一行能不能走自动下架。

    不能的那两种都要在报表里写清楚**为什么**,而不是从清单里消失。4.1 节 H
    的最后一条要求"若平台不支持删除、只支持下架,则在文档中写明残留 SKU 的
    处置方式,不留悬空" —— 一个自动流程处理不了的商品,同样不许悬空。

    没有外部 ID 的那一类是第三种(A45-batch18 / P1-3):下架报文的目标
    由 `external_spu_id` 确定,没有它就构造不出请求。**它不许自动下架,
    但它必须留在清单上**并标成要人核对 —— 那正是这一行存在的全部意义。
    """
    if not has_id:
        return False, (
            "本地状态是「提交结果未知」且没有平台 SPU ID:平台上可能已经有这件"
            "商品,本系统答不出来。自动下架构造不出请求,必须人工到平台后台"
            "按 locator 里的线索核对"
        )
    if not occupying:
        return False, None
    if listing.draft_id is None:
        # `publish_service.enqueue` 要一份草稿才能构造报文。下架报文其实是空的,
        # 但 listing 与草稿的关联同时也是审计链条的一环,缺了它这次下架
        # 就没有来源记录 —— 这种行留给人工处理,并在报表里点名
        return False, "listing 上没有 draft_id,自动下架构造不出请求,需人工在平台后台处理"
    return True, None


def _report(rows: Sequence[ExternalListing], probe: Mapping[str, int] | None = None):
    real = [r for r in rows if not r.simulated]
    return CleanupReport(
        rows=tuple(rows),
        total=len(rows),
        simulated=len(rows) - len(real),
        real=len(real),
        # 不确定行单独数(A45-batch18 / P1-3)。它们不进 `occupying` ——
        # 那个数问的是"平台上还占着几个位置",而这些行的答案是不知道 ——
        # 但它们让 `clean` 变成 False
        unreconciled=sum(1 for r in rows if r.needs_reconcile),
        # 只数真实商品:模拟的那些从来没有出现在任何平台上。
        #
        # **同时排掉不确定行。** 上面那句注释和 `CleanupReport.occupying` 的
        # 字段说明都写着它们不占这个数,而实现原来照数不误:一行
        # `SUBMIT_RESULT_UNKNOWN` 且没有外部 ID 的记录会同时进
        # `unreconciled` 和 `occupying`,于是运营读到的"平台仍占位数量"
        # 被虚增 —— 然后拿着一个可能根本不存在的数字去后台逐条核对。
        #
        # 排掉不等于让它消失:`unreconciled` 单独报、`clean` 照样是 False、
        # `plan_delist` 照样把它留在清单里、`run_delist` 照样把它连同
        # locator 记进 `skipped`。这里改的只是"确定占位"这一个口径。
        occupying=sum(1 for r in real if r.occupying and not r.needs_reconcile),
        # 跟着 `occupying` 一起收窄 —— CLI 把它印成"其中 N 行……",
        # 而"其中"的那个整体正是上面这个数。两个口径不一致时,
        # 屏幕上会出现"仍占着 0 个位置,其中 1 行处理不了"
        not_delistable=sum(
            1 for r in real if r.occupying and not r.delistable and not r.needs_reconcile
        ),
        # 这两个数**含模拟行,也含不确定行**,因为 `run_delist` 就是这么
        # 遍历的:它对每一行 `delistable=False` 的记录都记一笔 `skipped`,
        # 不确定行也不例外。跟着上面两个一起排掉不确定行的话,
        # "若执行:跳过 N 行"会比实际少 —— 而这两个数存在的唯一理由
        # 就是"给人看的数字必须等于实际会执行的数量"。
        # 口径差异见 `CleanupReport.to_queue` 的说明
        to_queue=sum(1 for r in rows if r.occupying and r.delistable),
        to_skip=sum(1 for r in rows if r.occupying and not r.delistable),
        probe=dict(probe or {}),
    )


# ================================================================ 下架


def plan_delist(
    session: Session,
    *,
    test_batch_tag: str | None = None,
    shop_id: str | None = None,
    channel: str | None = None,
    include_simulated: bool = True,
) -> CleanupReport:
    """**只看不做**:这次清理会对哪些商品下手。

    动手之前要看的是 `to_queue` / `to_skip` —— 它们含模拟行,和 `run_delist`
    实际遍历的集合一致。`occupying` / `not_delistable` 回答的是另一个问题
    (「真实平台上还占着几个位置」),两者在人工测试批次里差得很远,
    因为那种批次几乎全是 SIM- 行。口径说明见 `CleanupReport.to_queue`。
    """
    report = inventory(
        session,
        test_batch_tag=test_batch_tag,
        shop_id=shop_id,
        channel=channel,
        include_simulated=include_simulated,
    )
    # 不确定行**留在计划里**(A45-batch18 / P1-3)。它们 `delistable=False`,
    # 所以 `run_delist` 会把它们记进 `skipped` 而不是排队 —— 那正是要的:
    # 动手前那份计划必须让人看见"有几行我处理不了,你得自己去平台后台核"。
    # 从计划里滤掉的话,人看到的是一份干净的清单,而事实不是
    return _report(
        [r for r in report.rows if r.occupying or r.needs_reconcile],
        probe=report.probe,
    )


def run_delist(
    session: Session,
    *,
    actor: str,
    test_batch_tag: str | None = None,
    shop_id: str | None = None,
    channel: str | None = None,
    include_simulated: bool = True,
) -> dict[str, Any]:
    """把计划里那些商品排进 DELIST 队列。**在业务事务内调用,由调用方提交。**

    只 `flush` 不 `commit`,同 `publish_service.enqueue` —— 一次清理是一批
    排队动作,要么整批进队列要么一个都不进。半批进队列的后果是重跑一次
    清理时,前半批因为幂等键相同被复用(没问题),而人会以为这次排了全部。

    真正的下架请求由 `publish_service.run_due` 在事务外发出,这里只排队。
    """
    # **破坏性作用域比预览严**(A45-batch18 / P1-4)。这一句必须排在
    # `plan_delist` 之前:排在后面的话,一次 channel-only 的调用会先把
    # 整个渠道的行读出来,然后才被拒 —— 拒对了,但日志里留下的是一次
    # 看起来"差一点就执行了"的全渠道扫描,而下一个人会照着它去放宽这道闸
    _require_scope(
        test_batch_tag=test_batch_tag,
        shop_id=shop_id,
        channel=channel,
        destructive=True,
    )
    plan = plan_delist(
        session,
        test_batch_tag=test_batch_tag,
        shop_id=shop_id,
        channel=channel,
        include_simulated=include_simulated,
    )

    queued: list[str] = []
    skipped: list[dict[str, Any]] = []
    for row in plan.rows:
        if not row.delistable:
            # 没有外部 ID 的那些也要出现在 skipped 里,并且要带上定位线索 ——
            # 一句"跳过了 3 行"而不说是哪 3 行,等于没说(A45-batch18 / P1-3)
            entry: dict[str, Any] = {
                "external_spu_id": row.external_spu_id,
                "listing_id": str(row.listing_id),
                "reason": row.reason,
            }
            if row.needs_reconcile:
                entry["needs_reconcile"] = True
                entry["locator"] = dict(row.locator)
            skipped.append(entry)
            continue
        listing = session.get(ChannelListing, row.listing_id)
        draft = session.get(ListingDraft, listing.draft_id) if listing else None
        product = session.get(Product, listing.product_id) if listing else None
        if listing is None or draft is None or product is None:
            skipped.append(
                {"external_spu_id": row.external_spu_id, "reason": "草稿或商品行缺失"}
            )
            continue
        publish_service.enqueue(
            session,
            product=product,
            draft=draft,
            shop_id=listing.shop_id,
            actor=actor,
            requested_operation=PublishOperation.DELIST,
            test_batch_tag=listing.test_batch_tag,
        )
        queued.append(row.external_spu_id)

    logger.info(
        "queued a delist for this listing",
        extra={
            "extra_fields": {"event": "ops.delist_queued",
                "test_batch_tag": test_batch_tag,
                "shop_id": shop_id,
                "channel": channel,
                "queued": len(queued),
                "skipped": len(skipped),
                "unreconciled": plan.unreconciled,
                "actor": actor,
            }
        },
    )
    return {"queued": queued, "skipped": skipped, "plan": plan.as_dict()}


# ================================================================ 核对


def verify(
    session_factory: Callable[[], Session],
    *,
    test_batch_tag: str | None = None,
    shop_id: str | None = None,
    channel: str | None = None,
    include_simulated: bool = True,
) -> CleanupReport:
    """人工测试结束时的清单核对(4.1 节 H 最后一条)。

    对清单上的**每一行**显式问一次平台(`poll_service.poll_one`),不看它的
    本地状态到期没有 —— 已经 LISTED 或已经 DELISTED 的行都不在
    `POLLABLE_STATUSES` 里,靠定时轮询一辈子也问不到它们,而它们恰恰是这次
    核对要查的对象。

    问完之后重新读一遍库再统计:如果某个商品我们以为已经下架、平台却说还在,
    `poll_one` 会把本地状态改回去,而这份报表必须反映**问过之后**的事实。
    只用问之前那份快照的话,报表会说"全部已下架",而那正是它要验的东西。
    """
    session = session_factory()
    try:
        before = inventory(
            session,
            test_batch_tag=test_batch_tag,
            shop_id=shop_id,
            channel=channel,
            include_simulated=include_simulated,
        )
        listing_ids = [r.listing_id for r in before.rows]
    finally:
        session.close()

    probe: dict[str, int] = {}
    for listing_id in listing_ids:
        bucket = poll_service.poll_one(session_factory, listing_id)
        probe[bucket] = probe.get(bucket, 0) + 1

    session = session_factory()
    try:
        after = inventory(
            session,
            test_batch_tag=test_batch_tag,
            shop_id=shop_id,
            channel=channel,
            include_simulated=include_simulated,
        )
    finally:
        session.close()

    return _report(after.rows, probe=probe)



# ==========================================================
# C-17:批量导出文件的孤儿对象回收
# ==========================================================


#: 作废的导出对象至少留这么久再删。
#:
#: `regenerate_batch_export()` **刻意不删旧对象**,理由写在那里:
#: "新的万一也有问题,连对照物都没了"。那个理由是有保质期的 ——
#: 重新生成之后的头两周里旧字节确实是唯一的对照物,两周之后它只是垃圾。
#:
#: 所以这里不推翻那个取舍,只给它加一个期限。
SUPERSEDED_EXPORT_RETENTION_DAYS = 14

#: 审计里那条"重新生成"记录的业务动作名。旧路径就记在它的 payload 里
_REGENERATE_ACTION = "regenerate_export_file"


@dataclass(frozen=True)
class OrphanExport:
    """一个已经没人引用的导出对象。"""

    storage_path: str
    batch_id: str | None
    superseded_at: datetime | None
    reason: str | None
    deleted: bool = False
    #: 没删成时的原因。删成了是 None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "storage_path": self.storage_path,
            "batch_id": self.batch_id,
            "superseded_at": iso_utc(self.superseded_at),
            "reason": self.reason,
            "deleted": self.deleted,
            "error": self.error,
        }


def purge_superseded_exports(
    session: Session,
    storage,
    *,
    older_than_days: int = SUPERSEDED_EXPORT_RETENTION_DAYS,
    apply: bool = False,
    now: datetime | None = None,
) -> list[OrphanExport]:
    """回收被 `regenerate_batch_export()` 作废掉的导出对象(C-17)。

    ## 为什么线索在审计里而不是在存储里

    存储层没有 `list()`(见 `services/storage.py` 的抽象),扫不了前缀。
    而每一次重新生成都在审计里留下了 `old_storage_path` —— 那份记录本来
    就是为"万一要捞回来"写的,现在它同时是回收的线索。

    ## 三道闸,少一道都可能删掉在用的文件

    1. **年龄。** 早于 `older_than_days` 的才考虑。
    2. **没人引用。** 任何 `BatchJob.file_path` 还指着它就跳过 ——
       `storage.save()` 按内容哈希落盘,两次生成字节相同就是**同一个对象**,
       此时"旧路径"和"新路径"是同一个字符串,删了等于删掉在用的文件。
       这一条不是防御性冗余,它是必然会命中的那种情况。
    3. **默认干跑。** `apply=False` 时只报告。

    返回值逐条给出决定与结果,调用方(运维脚本 / 定时任务)照着打印即可。
    """
    from app.models.audit_log import AuditLog
    from app.models.batch_job import BatchJob

    moment = now or utc_now()
    cutoff = moment - timedelta(days=older_than_days)

    rows = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == "BatchJob",
            AuditLog.created_at < cutoff,
            AuditLog.payload["action"].astext == _REGENERATE_ACTION,
        )
        .order_by(AuditLog.created_at)
    )

    candidates: dict[str, AuditLog] = {}
    for row in rows:
        path = (row.payload or {}).get("old_storage_path")
        if path:
            # 同一个路径被作废过多次时留最早那条:年龄按最早一次算,
            # 更保守
            candidates.setdefault(str(path), row)

    if not candidates:
        return []

    # 还在用的一律不碰。一条 IN 查询,不逐个查
    live = {
        path
        for path in session.scalars(
            select(BatchJob.file_path).where(BatchJob.file_path.in_(list(candidates)))
        )
        if path
    }

    out: list[OrphanExport] = []
    for path, row in sorted(candidates.items()):
        if path in live:
            continue
        entry = OrphanExport(
            storage_path=path,
            batch_id=None if row.entity_id is None else str(row.entity_id),
            superseded_at=row.created_at,
            reason=(row.payload or {}).get("reason"),
        )
        if not apply:
            out.append(entry)
            continue
        try:
            deleted = storage.delete(path)
        except Exception as exc:  # noqa: BLE001 - 一个删不掉不该让整轮停下
            # A-14 之后 `delete()` 会把 403/限流/5xx 抛出来。抛出来是对的
            # (那正是要看见的),但回收是批量的:记下来,继续下一个
            logger.warning(
                "orphan export delete failed",
                extra={
                    "extra_fields": {
                        "event": "ops.orphan_export_delete_failed",
                        "storage_path": path,
                        "error": str(exc),
                    }
                },
            )
            out.append(replace(entry, deleted=False, error=str(exc)))
            continue
        out.append(replace(entry, deleted=deleted))
    return out

__all__ = [
    "OCCUPYING_STATUSES",
    "CleanupReport",
    "ExternalListing",
    "inventory",
    "plan_delist",
    "run_delist",
    "verify",
    "purge_superseded_exports",
    "OrphanExport",
    "SUPERSEDED_EXPORT_RETENTION_DAYS",
]
