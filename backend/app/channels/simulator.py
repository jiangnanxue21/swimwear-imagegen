"""渠道 API Simulator(方案 v4.1 任务 12 / 4.1 节 B)。

## 它是什么,以及**它不是什么**

方案 4.1 节 B 写得很直白:拿不到 SHEIN 官方文档和凭证之前,不允许宣称
SHEIN 已完成,退而求其次是「实现严格的 Channel API Simulator」。

**Simulator 只是人工测试准备,不等于正式渠道完成。** 它模拟的是一个平台
*可能*的行为形状,不是任何真实平台的契约。它存在的意义只有一个:让
「创建 → 幂等 → 限流 → 驳回 → 重试」这条链路在真实凭证到位之前就能被跑通、
被测试、被前端接上,而不是等文档到了再从零开始搭。

所以它的配置必须与真实渠道明确区分,前端状态条要能显示当前连的是哪一个
(4.1 节 F)。这条纪律在 `ChannelContext.channel` 上体现:Simulator 的
channel 名是 `SIMULATOR`,永远不会和真实渠道混。

## 九种行为(4.1 节 B 逐条)

    创建 / 更新 / 幂等冲突 / 限流 / 鉴权失败 / 字段拒绝 / 异步审核 / 发布成功 / 平台驳回

## 为什么做成**无状态**的

第一版的自然写法是在模块里放一个 dict 记住"哪个键提交过、哪个商品在审核中"。
那样有三个问题,而且都要等到接上真实 worker 才暴露:

    多进程     Celery worker 和 API 是不同进程,worker 里创建的商品
               API 进程 poll 不到 —— 表现是"提交成功了但状态永远查不到"
    重启丢失   调试时重启一次后端,所有在审核中的商品集体消失
    测试污染   用例之间互相看得见对方造的数据,顺序一变就红

改法是把状态**编码进 external_spu_id 本身**:

    SIM-<场景>-<创建时刻的 epoch 秒>-<8 位指纹>

`poll()` 解开这个 ID 就知道"这是哪种场景、创建了多久",于是异步审核可以
按真实经过的时间推进,而不需要任何人记住什么。代价是外部 ID 变长且有结构 ——
对一个 Simulator 来说完全可以接受,而且这个结构本身在排查时很有用。

幂等冲突同理:平台拒绝重复键时会告诉你"这个键已经创建过资源 X",
而 X 可以由键本身确定性地算出来,不需要记。

## 场景怎么选

两条途径,都刻意做成**可预测**的:

    显式        ChannelContext 里带 scenario(配置或前端选),优先级最高
    按 SPU 前缀  SPU 以 `SIM-RATELIMIT-` 之类开头时走对应场景

第二条是给人工测试用的:测试者想验限流重试,建一个叫 `SIM-RATELIMIT-001`
的商品就行,不必去改配置再重启。**不用随机数** —— 一个随机失败的 Simulator
会让"这次红是代码问题还是模拟器抽到了失败"变成一个每天都要回答的问题。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.channels.base import (
    ChannelContext,
    ChannelListingRef,
    PreparedRequest,
    SubmitResult,
)
from app.core.enums import PublishOperation, PublishStatus, RejectCategory

#: Simulator 的渠道名。与真实渠道永不重名 —— 4.1 节 F 要求前端状态条
#: 能显示"当前连的是真实渠道还是 Simulator",而这个名字就是那个判据。
CHANNEL = "SIMULATOR"

#: 外部 ID 的前缀。看到它就知道这个商品不在任何真实平台上。
#: 4.1 节 H 的清理预案要"一次性列出本轮测试创建了哪些外部商品",
#: 这个前缀让"哪些是模拟的"变成一次字符串前缀匹配。
EXTERNAL_ID_PREFIX = "SIM"

#: 异步审核要多久出结果(秒)。60 秒是刻意选的:比一次页面刷新长
#: (否则"审核中"这个状态在界面上一闪而过,前端那条分支等于没测过),
#: 又短到人工测试时不必去泡杯咖啡。
REVIEW_SECONDS = 60

#: 限流场景返回的重试等待秒数。真实平台的 Retry-After 通常是几秒到几分钟,
#: 取 3 秒让退避逻辑能在一次人工测试里被观察到。
RATE_LIMIT_RETRY_AFTER = 3


class SimScenario(StrEnum):
    """九种行为。取值即 SPU 前缀里用的关键字(`SIM-<关键字>-...`)。"""

    CREATE_OK = "CREATE_OK"
    UPDATE_OK = "UPDATE_OK"
    IDEMPOTENT_CONFLICT = "IDEMPOTENT_CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAILED = "AUTH_FAILED"
    FIELD_REJECTED = "FIELD_REJECTED"
    ASYNC_REVIEW = "ASYNC_REVIEW"
    PUBLISHED = "PUBLISHED"
    PLATFORM_REJECTED = "PLATFORM_REJECTED"


#: SPU 前缀 -> 场景。人工测试建一个叫 `SIM-RATELIMIT-001` 的商品即可触发限流。
#: 关键字比枚举名短,因为它要被人手打进商品编号里。
SPU_TRIGGERS: dict[str, SimScenario] = {
    "SIM-CONFLICT": SimScenario.IDEMPOTENT_CONFLICT,
    "SIM-RATELIMIT": SimScenario.RATE_LIMITED,
    "SIM-AUTH": SimScenario.AUTH_FAILED,
    "SIM-FIELD": SimScenario.FIELD_REJECTED,
    "SIM-REVIEW": SimScenario.ASYNC_REVIEW,
    "SIM-REJECT": SimScenario.PLATFORM_REJECTED,
    "SIM-LIVE": SimScenario.PUBLISHED,
}


class SimulatorError(ValueError):
    """Simulator 自身用法错误(不是模拟出来的平台错误)。

    两者必须分开:模拟的平台错误是**正常返回**,调用方要按业务处理;
    这个异常表示调用方把 Simulator 用错了,是代码缺陷。
    """


@dataclass(frozen=True)
class SimFieldError:
    """字段拒绝里的一条。形状对齐 `FieldViolation`,方便回流到修复队列。"""

    field: str
    code: str
    message: str


@dataclass(frozen=True)
class SimResponse:
    """一次模拟调用的结果。**刻意长得像 HTTP 响应**。

    不直接返回 `SubmitResult`,是因为调用方需要看到状态码和 Retry-After
    才能决定退避与重试 —— 而把这些揉进 SubmitResult 会让真实 Adapter
    的返回类型被 Simulator 的需要污染。转换在 Adapter 层做。
    """

    status_code: int
    body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    #: 映射到本地状态机的建议状态。None = 这次调用没有推进状态
    publish_status: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def retriable(self) -> bool:
        """能不能原样重发。

        429 与 5xx 可以;401 / 422 不行 —— 凭证和字段不改,重发一万次
        还是同一个结果,而每一次都在给平台的风控加分。
        409(幂等冲突)也不可重试,但它**不是失败**:它说明上一次其实成功了。
        """
        return self.status_code == 429 or self.status_code >= 500


def pick_scenario(
    *,
    spu: str,
    operation: PublishOperation | str,
    override: SimScenario | str | None = None,
) -> SimScenario:
    """决定这次调用走哪个场景。**确定性的,没有随机。**

    优先级:显式 override > SPU 前缀 > 按操作类型的默认。

    默认值按操作分:创建走 CREATE_OK,更新走 UPDATE_OK。也就是说
    一个普通商品走完整条链路是**成功路径**,九种行为里的八种要显式触发 ——
    这个方向是对的:默认失败的 Simulator 会让所有人第一次接入就卡住,
    然后开始怀疑自己的代码。
    """
    if override:
        try:
            return SimScenario(str(override).upper())
        except ValueError as exc:
            raise SimulatorError(
                f"未知场景 {override!r}。可用:{', '.join(s.value for s in SimScenario)}"
            ) from exc

    upper = (spu or "").upper()
    for prefix, scenario in SPU_TRIGGERS.items():
        if upper.startswith(prefix):
            return scenario

    op = PublishOperation(str(operation).upper())
    if op in (PublishOperation.UPDATE, PublishOperation.REPUBLISH):
        return SimScenario.UPDATE_OK
    if op is PublishOperation.DELIST:
        # 下架在 Simulator 里一律成功。4.1 节 H 要求 DELIST 路径打通,
        # 而一个会拒绝下架的模拟器会让清理预案演练不下去
        return SimScenario.CREATE_OK
    return SimScenario.CREATE_OK


def make_external_id(*, scenario: SimScenario, spu: str, now: datetime) -> str:
    """造一个把场景与创建时刻编进去的外部 ID。

    格式:`SIM-<场景>-<epoch 秒>-<8 位指纹>`。

    `poll()` 靠解开它来判断"这个商品是哪种场景、创建多久了",于是整个
    Simulator 不需要任何共享存储 —— 见模块顶部「为什么做成无状态的」。
    """
    digest = hashlib.sha256(f"{scenario}:{spu}".encode()).hexdigest()[:8]
    return f"{EXTERNAL_ID_PREFIX}-{scenario.value}-{int(now.timestamp())}-{digest}"


def is_simulated_external_id(external_id: str | None) -> bool:
    """这个外部 ID 是 Simulator 造的吗(4.1 节 H / 任务 16)。

    清理预案要回答的第一个问题是「本轮测试在**真实平台**上创建了哪些商品」,
    而模拟出来的那些一个都不该出现在那份清单里 —— 混进去的话,运营会拿着
    一份大部分是假的清单去平台后台核对,核不上,然后这份清单就没人信了。

    只看前缀,不做完整解析:`parse_external_id` 对结构不对的 ID 会抛错,
    而这里的问题是「是不是模拟的」,一个结构损坏的 SIM ID 仍然是模拟的。
    用解析来回答会让一个坏数据变成一次异常,而调用点是一份报表。
    """
    return (external_id or "").strip().upper().startswith(f"{EXTERNAL_ID_PREFIX}-")


def parse_external_id(external_id: str) -> tuple[SimScenario, datetime]:
    """把外部 ID 解回 (场景, 创建时刻)。

    解不开就抛 —— 一个解不开的 SIM ID 意味着有人把真实平台的 ID
    传进了 Simulator,或者反过来。这两种混淆都必须当场停住,
    不能降级成"当作默认场景处理":那会让一次配置错误表现为
    "平台状态莫名其妙变成了 LISTED"。
    """
    parts = (external_id or "").split("-")
    if len(parts) < 4 or parts[0] != EXTERNAL_ID_PREFIX:
        raise SimulatorError(f"不是 Simulator 的外部 ID:{external_id!r}")
    # 场景名自身含下划线不含短横,但保险起见按位置取中间段
    scenario_raw = "-".join(parts[1:-2])
    try:
        scenario = SimScenario(scenario_raw)
        created = datetime.fromtimestamp(int(parts[-2]), tz=UTC)
    except (ValueError, OSError, OverflowError) as exc:
        raise SimulatorError(f"外部 ID 结构不对:{external_id!r}") from exc
    return scenario, created


# ================================================================ 提交


def simulate_submit(
    *,
    spu: str,
    operation: PublishOperation | str,
    idempotency_key: str,
    body: dict[str, Any] | None = None,
    scenario: SimScenario | str | None = None,
    now: datetime | None = None,
) -> SimResponse:
    """模拟一次提交。九种行为里的前六种在这里发生。

    `now` 可注入 —— 异步审核要按经过时间推进,一个依赖"真的等 60 秒"的
    测试没人会跑第二遍。
    """
    now = now or datetime.now(UTC)
    picked = pick_scenario(spu=spu, operation=operation, override=scenario)
    op = PublishOperation(str(operation).upper())

    if picked is SimScenario.AUTH_FAILED:
        # 401 不带任何业务信息。真实平台在鉴权失败时通常也不会告诉你
        # 商品有没有问题 —— 它根本没看到商品
        return SimResponse(
            status_code=401,
            body={"error": "invalid_credentials", "message": "签名校验未通过"},
            publish_status=PublishStatus.SUBMIT_FAILED.value,
        )

    if picked is SimScenario.RATE_LIMITED:
        return SimResponse(
            status_code=429,
            body={"error": "rate_limited", "message": "请求过于频繁"},
            headers={"Retry-After": str(RATE_LIMIT_RETRY_AFTER)},
            # 限流**不改本地状态**:这次没送到,商品还在排队,
            # 把它标成 SUBMIT_FAILED 会让运营以为要人工处理
            publish_status=None,
        )

    if picked is SimScenario.FIELD_REJECTED:
        errors = _field_errors(body or {})
        return SimResponse(
            status_code=422,
            body={
                "error": "validation_failed",
                "message": "字段校验未通过",
                "field_errors": [
                    {"field": e.field, "code": e.code, "message": e.message} for e in errors
                ],
            },
            publish_status=PublishStatus.VALIDATION_FAILED.value,
        )

    if picked is SimScenario.IDEMPOTENT_CONFLICT:
        # 409 是**好消息**:上一次其实成功了,平台把既有资源还给你。
        # 所以这里必须带上 external_spu_id,否则调用方只能重试或人工介入,
        # 而两者都是错的 —— 正确动作是把这个 ID 记下来然后继续
        existing = _id_from_key(idempotency_key, now)
        return SimResponse(
            status_code=409,
            body={
                "error": "idempotency_conflict",
                "message": "该幂等键已创建过商品",
                "external_spu_id": existing,
                "idempotency_key": idempotency_key,
            },
            publish_status=PublishStatus.SUBMITTED.value,
        )

    external_id = make_external_id(scenario=picked, spu=spu, now=now)

    if picked in (SimScenario.ASYNC_REVIEW, SimScenario.PLATFORM_REJECTED,
                  SimScenario.PUBLISHED):
        # 202:平台收下了,但结果要等。这条分支是最需要被真的跑一遍的 ——
        # 「提交成功」不等于「上架成功」,而把两者混同是这类集成最常见的错
        return SimResponse(
            status_code=202,
            body={
                "external_spu_id": external_id,
                "platform_status": "REVIEWING",
                "message": "已受理,审核中",
            },
            publish_status=PublishStatus.PLATFORM_REVIEWING.value,
        )

    # CREATE_OK / UPDATE_OK:同步成功
    return SimResponse(
        status_code=200 if op is not PublishOperation.CREATE else 201,
        body={
            "external_spu_id": external_id,
            "external_sku_ids": _sku_ids(external_id, body or {}),
            "platform_status": "LISTED",
            "operation": op.value,
        },
        publish_status=PublishStatus.LISTED.value,
    )


# ================================================================ 轮询


def simulate_poll(*, external_spu_id: str, now: datetime | None = None) -> SimResponse:
    """模拟一次状态轮询。异步审核、发布成功、平台驳回三种在这里收敛。

    状态完全由 `external_spu_id` 里编码的场景与创建时刻决定,
    所以同一个 ID 在任何进程、任何时刻查都得到一致的答案。
    """
    now = now or datetime.now(UTC)
    scenario, created = parse_external_id(external_spu_id)
    elapsed = (now - created).total_seconds()

    if scenario not in (SimScenario.ASYNC_REVIEW, SimScenario.PLATFORM_REJECTED,
                        SimScenario.PUBLISHED):
        # 同步创建的商品,查出来就是已上架
        return SimResponse(
            status_code=200,
            body={"external_spu_id": external_spu_id, "platform_status": "LISTED"},
            publish_status=PublishStatus.LISTED.value,
        )

    if elapsed < REVIEW_SECONDS:
        return SimResponse(
            status_code=200,
            body={
                "external_spu_id": external_spu_id,
                "platform_status": "REVIEWING",
                "seconds_remaining": int(REVIEW_SECONDS - elapsed),
            },
            publish_status=PublishStatus.PLATFORM_REVIEWING.value,
        )

    if scenario is SimScenario.PLATFORM_REJECTED:
        return SimResponse(
            status_code=200,
            body={
                "external_spu_id": external_spu_id,
                "platform_status": "REJECTED",
                # 驳回类目对齐 `RejectCategory`,这样驳回回流能直接分诊到
                # "该去哪个标签页改",而不是丢给运营一句平台原话
                "reject_category": RejectCategory.IMAGE.value,
                "reject_code": "IMG_BACKGROUND_NOT_WHITE",
                "reject_message": "主图背景不是纯白",
            },
            publish_status=PublishStatus.PLATFORM_REJECTED.value,
        )

    return SimResponse(
        status_code=200,
        body={"external_spu_id": external_spu_id, "platform_status": "LISTED"},
        publish_status=PublishStatus.LISTED.value,
    )


# ================================================================ 内部


def _id_from_key(idempotency_key: str, now: datetime) -> str:
    """由幂等键确定性地算出"上次创建的那个资源 ID"。

    真实平台是查库查出来的;这里算出来。区别对调用方不可见,而这正是
    无状态设计成立的关键:同一个键永远指向同一个外部 ID。
    """
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:8]
    # 冲突返回的是**之前**创建的资源,时间戳往回推一个审核周期,
    # 这样紧接着的一次 poll 会返回 LISTED 而不是"还在审核"
    earlier = int(now.timestamp()) - REVIEW_SECONDS - 1
    return f"{EXTERNAL_ID_PREFIX}-{SimScenario.CREATE_OK.value}-{earlier}-{digest}"


def _field_errors(body: dict[str, Any]) -> list[SimFieldError]:
    """造一批字段错误。**优先挑报文里真实存在的字段**。

    随便返回一个 `field: "foo"` 的话,驳回回流那条链路会因为找不到
    对应字段而静默丢弃 —— 而那正是这个场景要验的东西。
    """
    header = body.get("header") if isinstance(body.get("header"), dict) else {}
    candidates = [k for k in header if k][:2]
    if not candidates:
        candidates = ["title"]
    return [
        SimFieldError(
            field=key,
            code="FIELD_TOO_LONG" if i == 0 else "FIELD_NOT_ALLOWED",
            message=f"字段 {key} 未通过平台校验",
        )
        for i, key in enumerate(candidates)
    ]


def _sku_ids(external_spu_id: str, body: dict[str, Any]) -> dict[str, str]:
    """给每个 SKU 行分配一个平台 SKU ID。

    形状对齐 `ChannelListing.external_sku_ids`({本地 sku: 平台 id}),
    这样落库时不需要再转一次结构。
    """
    rows = body.get("rows")
    if not isinstance(rows, list):
        return {}
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sku = str(row.get("sku") or row.get("SKU") or "").strip()
        if sku:
            out[sku] = f"{external_spu_id}-{hashlib.sha256(sku.encode()).hexdigest()[:6]}"
    return out


# ================================================================ Adapter 接线
#
# 上面的 simulate_* 是纯函数,返回长得像 HTTP 响应的东西。下面两个把它们
# 转成 `ChannelAdapter` 协议要的 `SubmitResult` / 状态字符串。
#
# 分成两层是刻意的:纯函数那层可以穷举九种行为做快照测试,而这一层只做
# 结构转换。把两者揉在一起的话,想验"限流时 Retry-After 是多少"就得先
# 造一个 ChannelContext。


def submit(req: PreparedRequest, ctx: ChannelContext) -> SubmitResult:
    """`ChannelAdapter.submit` 的 Simulator 实现。

    干跑(`ctx.dry_run`)在这里**直接返回**,不进模拟逻辑:干跑的定义是
    "构造了报文但不发出去",而 Simulator 再假,它也是"发出去"的那一端。
    把干跑也交给它模拟,就没有任何一条路径在验证"我们真的没发"。
    """
    if ctx.dry_run:
        return SubmitResult(
            accepted=True,
            external_spu_id=None,
            platform_status=PublishStatus.DRY_RUN_COMPLETED.value,
            message="干跑:报文已构造,未发送",
        )

    body = dict(req.body)
    # SPU 取自 `req.spu`,不从 body 里猜。理由见 `base.PreparedRequest.spu`
    spu = str(req.spu or "")
    operation = body.get("operation") or PublishOperation.CREATE.value

    resp = simulate_submit(
        spu=spu,
        operation=operation,
        idempotency_key=req.idempotency_key or "",
        body=body,
        scenario=ctx.options.get("scenario") if ctx.options else None,
    )

    # 409 幂等冲突算**接受**:平台告诉我们上一次成功了,外部 ID 在返回里。
    # 判成失败的话,重试逻辑会再发一次,而那一次仍然是 409 —— 死循环,
    # 且每一轮都在给平台的风控加分
    accepted = resp.ok or resp.status_code == 409
    return SubmitResult(
        accepted=accepted,
        external_spu_id=resp.body.get("external_spu_id"),
        platform_status=resp.publish_status,
        message=resp.body.get("message") or resp.body.get("error"),
    )


def poll(listing: ChannelListingRef, ctx: ChannelContext) -> str:
    """`ChannelAdapter.poll` 的 Simulator 实现。返回本地状态机的状态字符串。"""
    if not listing.external_spu_id:
        # 还没拿到外部 ID 就来轮询,说明上游把"提交了"和"提交成功了"
        # 混同了。返回一个状态会掩盖这个 bug,所以直接抛
        raise SimulatorError("没有 external_spu_id,无从轮询 —— 上一次提交并未成功")
    return simulate_poll(external_spu_id=listing.external_spu_id, now=None).publish_status or ""
