"""识别 run 的终态、取消与幂等键(PRD §4.6 / §9.2)。**纯判定,零三方依赖。**

## 它修的是什么

§4.6 要求识别异步化:接口立即返回 run_id,Celery 执行,支持 cancel,
`idempotency_key` 落唯一约束。那一整件事要五个新列 + 迁移 + Celery 任务,
**要真库**。但它里面有一半是纯判定,而那一半今天就有一个正在漏的消费者:

    「这次识别算成功、部分成功还是失败」——今天由**前端**回答。

`AttributeTab.tsx` 拿 `succeeded_count` / `failed_count` 自己判三档,
那是硬规则 4 明令禁止的形状。而且它判得不对:后端的循环有可能在
**跑完之前**停下(worker 被回收、上限拦截、将来的 cancel),那时
`failed_count` 是 0 而 `succeeded_count` 大于 0 —— 前端那三档里,
这种情况落在「识别完成」上,于是一次**跑了一半**的识别在界面上
显示成功,而缺的那几张图不会有任何人回来补。

后端从来没有派生过这个值:`run_extraction` 跑完之后 `error_code` 是 NULL、
接口返回 200,**全部失败和全部成功在库里长得一模一样**。

## 三个判定,一个共同的失效方式

    terminal_status_for()   一次 run 跑完之后算哪一档
    reuse_verdict()         同意图的第二次请求:返回原 run / 复用结果 / 新建
    idempotency_key()       §9.2 的键本体

三者的失效方式都是**往宽了错**:多算一档 COMPLETED、多判一次可复用、
键少带一个维度 —— 后果分别是「半截数据被当成事实」「换了模型还在用旧结果」
「双击产生两个 run,付两次钱」。所以本模块的每一条兜底都朝
**不放行**那一侧倒,与 `scope_fingerprint.facts_stale` 的「算不出来就算过期」
同一条口径。

## 为什么幂等键必须在第一次调用**之前**算得出来

§9.2 的键含 `model_version`。有两个地方能拿到它:配置(`EXTRACTOR_MODEL_NAME`)
与模型响应(`result.model_name`)。取后者的话,键只有在**付过钱之后**
才算得出来 —— 而幂等键要挡的正是「双击」和「网络重发」,那两件事都发生在
付钱之前。所以本模块拒绝用空的 model/prompt 版本建键(抛 `ValueError`),
逼调用方去配置里取。

这一条不是洁癖:`run_extraction` 今天写的是
`row.model_name = row.model_name or result.model_name` —— 响应回来才知道,
照那个来源建键的实现会**编译得过、测试也绿**,只是每一次双击都买两次。

## 唯一约束不能是全表的(落码时和 §9.2 对不上的一处)

§9.2 说「落 `idempotency_key` 唯一约束,数据库裁决」。按字面做成全表唯一,
一次 FAILED 之后**同样的输入再也建不出第二个 run** —— 输入没变、模型没变、
字段没变,而那正是重试的定义。运营看到的是一个再也识别不了的商品,
而唯一的解法是去改点什么(换个字段范围、传张图)来骗过那条约束。

所以占键的只有「还会被复用的那几档」(`KEY_OCCUPYING_STATUSES`),
索引必须是部分唯一索引,谓词由 `unique_index_predicate()` 生成 ——
它是那条 DDL 的**孪生**,与 `media/evidence_rules.py` 的 CHECK 孪生同一套办法:
两处各写一遍必然漂移,而漂移的表现是「代码说能重试、库说不能」。

## 与指纹模块共用一个编码器

`idempotency_key` 与 `shared_fingerprint` 都要把一串字段拼起来再哈希,
都必须防拼接歧义。这里**不重写第二个编码器**,直接用
`scope_fingerprint.length_prefixed` —— 两个模块对「怎么把一组字段变成
一个稳定字符串」必须是同一个答案,否则将来有人改其中一个的编码,
另一个静静地留在旧版本上,而两边都不会报错。
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.attributes.scope_fingerprint import SHARED, length_prefixed
from app.core.enums import ExtractionRunStatus

S = ExtractionRunStatus

#: 键的版本。组成或编码有任何改动都要动它 —— 不动的话,新算出来的键会和
#: 库里的老键**比较**,而比较的结果是「同意图」判定全体错乱:老 run 认不出来,
#: 于是每一个商品的下一次识别都会新建一个 run 并重新付费。
KEY_VERSION = "k1"

#: 作用域的三种形状(§4.6 `requested_scope`)。**它们是键的一部分**,
#: 所以必须是稳定的字符串,不是内存里的哨兵对象。
SCOPE_SHARED = "SHARED"
SCOPE_ALL = "ALL"
SCOPE_VARIANTS = "VARIANTS"

#: 终态:到了这几档,调度器不再看这一行,也不接受任何转移。
#: `PARTIAL_SUCCESS` 在里面 —— 它是**终态不是中间态**,失败的那几张要重跑
#: 走一个新 run(§13 的「按颜色重试」就是这个意思)。放进非终态的话,
#: 「重试」会变成在同一行上继续写,而那一行的 `input_fingerprint` 是
#: 第一次跑时的快照,于是重试之后的证据挂在一个过期的输入上。
TERMINAL_STATUSES: frozenset[ExtractionRunStatus] = frozenset(
    {S.COMPLETED, S.PARTIAL_SUCCESS, S.FAILED, S.CANCELLED}
)

#: 合法转移表。与 `workflows/state_machine.py` 同一套规矩:转移表是唯一真相源,
#: 非法跳转不静默纠正。
#:
#: `QUEUED -> FAILED` 是一条真实的边:任务还没开始就失败(参数不合法、
#: 素材在排队期间被隔离光了)。少了它,那种 run 只能停在 QUEUED,
#: 而看板会一直说「排队中」。
TRANSITIONS: dict[ExtractionRunStatus, frozenset[ExtractionRunStatus]] = {
    S.QUEUED: frozenset({S.RUNNING, S.CANCELLED, S.FAILED}),
    S.RUNNING: frozenset({S.COMPLETED, S.PARTIAL_SUCCESS, S.FAILED, S.CANCELLED}),
    S.COMPLETED: frozenset(),
    S.PARTIAL_SUCCESS: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}

#: 「这次 run 的证据可以拿去合并成事实吗」。
#:
#: `PARTIAL_SUCCESS` **算数**:成功那几张的证据是真的,而合并层
#: (`extractors/merge.py`)本来就按证据条数与阈值决定采信什么 ——
#: 少几张图的表现是置信度低、进 CANDIDATE 而不是 SUGGESTED,那是它的职责。
#:
#: `CANCELLED` **不算数**,尽管它的证据同样是真的、同样付过钱。
#: 区别在于「为什么停」:部分失败是模型答不上来,取消是**人喊停**,
#: 而人喊停的时候多半正是因为这一次跑错了(选错了图、选错了字段)。
#: 把它算进去等于让一次被叫停的识别照样改写事实。
#:
#: 注意这条只管**合并**,不管删除:被取消的 run 已经产生的证据行照样留着,
#: 那是「这笔钱花在哪了」的唯一记录。
AUTHORITATIVE_STATUSES: frozenset[ExtractionRunStatus] = frozenset(
    {S.COMPLETED, S.PARTIAL_SUCCESS}
)


def is_terminal(status: ExtractionRunStatus | str) -> bool:
    return ExtractionRunStatus(status) in TERMINAL_STATUSES


def can_transition(
    current: ExtractionRunStatus | str, target: ExtractionRunStatus | str
) -> bool:
    return ExtractionRunStatus(target) in TRANSITIONS.get(
        ExtractionRunStatus(current), frozenset()
    )


def can_cancel(status: ExtractionRunStatus | str) -> bool:
    """还能不能取消。终态一律不能 —— 取消一个已经跑完的 run 只会让
    一份已经付过钱、已经合并进事实的结果变成「不算数」,而钱不会回来。"""
    return can_transition(status, S.CANCELLED)


def run_is_authoritative(status: ExtractionRunStatus | str | None) -> bool:
    """这一次 run 的证据能不能进事实合并。见 `AUTHORITATIVE_STATUSES`。"""
    if status is None:
        return False
    return ExtractionRunStatus(status) in AUTHORITATIVE_STATUSES


def terminal_status_for(
    *,
    image_count: int,
    succeeded: int,
    failed: int,
    cancel_requested: bool = False,
) -> ExtractionRunStatus:
    """一次 run 跑完之后落哪一档。**这是本模块唯一今天就接了线的判定。**

    入参故意只有四个数,全部来自已经存在的列 —— 所以它今天就能算,
    不用等 §4.6 的五个新列。

    ## 判定顺序是有讲究的

    1. **输入自相矛盾 → FAILED。** 计数是负的、处理过的比总数还多,
       这些都是 bug 的征兆而不是业务情况。往 COMPLETED 倒的话,
       一个计数写错的 run 会带着「成功」的标签把半截证据送进事实。
    2. **一张图都没有 → FAILED。** 「零张图全部成功」在逻辑上是真的,
       在业务上是一个**没有输入的 run**。今天 `run_extraction` 在这之前
       就抛了,所以这一档是给将来的异步路径留的:那时候素材可能在
       排队期间被隔离光。
    3. **喊了停并且确实停在做完之前 → CANCELLED。**
    4. 剩下的按成败分三档。

    ## 喊得太晚不算取消(第 3 条那半句)

    最后一张图已经跑完之后才收到取消,判定是「跑完了」,不是 CANCELLED。
    理由是钱:那批调用一次不少地付过了,而 CANCELLED 的证据不进合并
    (`AUTHORITATIVE_STATUSES`)—— 判成取消等于**把已经买到手的结果扔掉**,
    换来的只是界面上一个更符合运营预期的词。

    反过来,停在做完之前**必须**是 CANCELLED 而不是 PARTIAL_SUCCESS:
    两者的下一步动作完全不同 —— 部分成功该重试失败的那几张,
    被取消的那次该先问「为什么喊停」。
    """
    processed = succeeded + failed
    if succeeded < 0 or failed < 0 or image_count < 0 or processed > image_count:
        return S.FAILED
    if image_count == 0:
        return S.FAILED
    if cancel_requested and processed < image_count:
        return S.CANCELLED
    if processed < image_count:
        # 没喊停却没跑完:worker 中途没了,或者循环被什么东西提前退出了。
        # 这一档**不能**是 COMPLETED —— 它正是前端那三档今天判错的那一种。
        return S.PARTIAL_SUCCESS if succeeded > 0 else S.FAILED
    if succeeded == 0:
        return S.FAILED
    if failed == 0:
        return S.COMPLETED
    return S.PARTIAL_SUCCESS


# ==========================================================
# §9.2 幂等
# ==========================================================


class ReuseVerdict:
    """同意图的第二次请求该怎么办。三个取值写成常量而不是枚举,
    是为了让本模块保持在「只 import core.enums」这一条依赖上。"""

    #: 那一次还在跑。返回原 run —— 双击、网络重发、worker 重投都走这里
    RETURN_EXISTING = "RETURN_EXISTING"
    #: 那一次跑完了且结果算数。直接复用,不再付一次钱
    REUSE_RESULT = "REUSE_RESULT"
    #: 建新的
    NEW_RUN = "NEW_RUN"


def reuse_verdict(existing_status: ExtractionRunStatus | str | None) -> str:
    """§9.2「同意图」的判定。

        QUEUED / RUNNING   返回原 run
        COMPLETED          复用结果
        PARTIAL_SUCCESS    **新建**。复用等于宣布失败的那几张永远不再重试,
                           而「按颜色重试」是 §13 阶段 3 点名的交付项
        FAILED / CANCELLED 新建。这两档就是要重试的那两档
        没有那一行           新建

    注意 PARTIAL_SUCCESS 判新建带来一个直接后果:它**不占键**,
    见 `KEY_OCCUPYING_STATUSES`。
    """
    if existing_status is None:
        return ReuseVerdict.NEW_RUN
    status = ExtractionRunStatus(existing_status)
    if status in (S.QUEUED, S.RUNNING):
        return ReuseVerdict.RETURN_EXISTING
    if status is S.COMPLETED:
        return ReuseVerdict.REUSE_RESULT
    return ReuseVerdict.NEW_RUN


#: 哪几档**占着**幂等键。**从 `reuse_verdict` 派生,不手写第二张名单** ——
#: 手写的那一版会和判定漂移,而漂移的表现极隐蔽:代码说该新建一个 run,
#: 库说这个键被占了,于是接口报一个和用户动作毫无关系的 409。
KEY_OCCUPYING_STATUSES: frozenset[ExtractionRunStatus] = frozenset(
    s for s in ExtractionRunStatus if reuse_verdict(s) != ReuseVerdict.NEW_RUN
)


def unique_index_predicate(column: str = "status") -> str:
    """部分唯一索引的谓词。**这是那条 DDL 的孪生。**

    §4.6 落 `idempotency_key` 唯一约束时,索引必须写成:

        CREATE UNIQUE INDEX ... ON product_attribute_extractions (idempotency_key)
        WHERE <本函数的返回值>

    全表唯一的后果见模块文档第四节:一次失败之后同样的输入再也建不出
    第二个 run。谓词由 `KEY_OCCUPYING_STATUSES` 生成而不是手打,
    理由与 `media/evidence_rules.py` 的 CHECK 孪生一字不差。
    """
    values = ", ".join(f"'{s.value}'" for s in sorted(KEY_OCCUPYING_STATUSES))
    return f"{column} IN ({values})"


@dataclass(frozen=True)
class Scope:
    """一个作用域的两面:**存进库的样子**与**它覆盖哪几个子集**。

    两面必须一起走。只带 token 的话,「这个作用域包含颜色 A 吗」这个问题
    只能靠在 token 里找子串回答 —— 而 token 是可以被伪造的:一个 id 恰好
    长成 `x1:a` 的颜色,会让 `1:a`(颜色 A 的编码)成为它的子串,
    于是 A 的指纹被算进一个根本不含 A 的键。

    这不是假想:本仓库已经在三个地方栽在「文件里出现过这串字 ≠ 这件事成立」
    上(batch13-3 的 M2/M11、batch14-2 的 N15)。这里是同一个形状换到了
    数据编码上,所以成员判定走 `variant_ids` 这个真实的元组,不碰 token。
    """

    kind: str
    token: str
    variant_ids: tuple[str, ...] = ()


def canonical_scope(
    *, all_variants: bool = False, variant_ids: Iterable[Any] | None = None
) -> Scope:
    """§4.6 的 `requested_scope` 归一成一个稳定字符串。

    三种形状:共享、指定的几个颜色、全部颜色。

    ## ALL 与「把当前全部颜色列出来」不是同一个意图

    两者今天覆盖的颜色集合可能一模一样,但意思不同:ALL 说的是
    「**现在有哪些颜色就跑哪些**」,列表说的是「就这几个」。
    新增一个颜色之后,前者该重新跑而后者不该 —— 所以它们必须编码成
    两个不同的键。归一成同一个的写法会让「加了新颜色之后再点识别」
    命中旧 run 并直接复用,而新颜色一张证据都没有。

    ## 颜色 id 要排序去重

    集合迭代序每个进程都不一样(字符串哈希带随机种子),不排序的表现是
    **同一个请求在两个 worker 上算出两个键** —— 于是唯一约束挡不住任何东西,
    双击照样跑两次、付两次钱。这与 batch14-9 的 H3、batch14-10 的 D1
    是同一个教训的第三次出现。
    """
    ids = [text for text in (_text_or_none(v) for v in (variant_ids or ())) if text]
    if all_variants and ids:
        # 两种意图同时给出来 = 调用方自己也没想清楚。猜一个的代价是猜错时
        # 键是错的,而键错了不报错、只是多付一次钱
        raise ValueError("requested_scope 不能同时是 ALL 和一份颜色清单")
    if all_variants:
        return Scope(kind=SCOPE_ALL, token=length_prefixed(SCOPE_ALL))
    if not ids:
        return Scope(kind=SCOPE_SHARED, token=length_prefixed(SCOPE_SHARED))
    wanted = tuple(sorted(set(ids)))
    token = length_prefixed(SCOPE_VARIANTS) + "".join(
        length_prefixed(i) for i in wanted
    )
    return Scope(kind=SCOPE_VARIANTS, token=token, variant_ids=wanted)


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _scope_fingerprints(
    scope: Scope, fingerprints: Mapping[Any, str]
) -> list[tuple[str, str]]:
    """这个作用域下,键要带上哪几个指纹。

    共享作用域带共享指纹;颜色作用域**只带那几个颜色的指纹,不带共享的**。

    后一半是刻意的,理由和 `scope_fingerprint` 那边「通用素材不进颜色子集」
    完全同源:带上共享指纹的话,补一张通用图会让**每一个颜色**的键都变,
    于是每个颜色都要重跑一次识别 —— D1(作用域过宽)从幂等键这道门回来。

    成员判定读 `scope.variant_ids`,不在 token 里找子串,见 `Scope` 的文档。
    """
    if scope.kind == SCOPE_SHARED:
        wanted: list[Any] = [SHARED]
    elif scope.kind == SCOPE_ALL:
        wanted = sorted(fingerprints, key=lambda k: "" if k is SHARED else str(k))
    else:
        wanted = list(scope.variant_ids)
    out: list[tuple[str, str]] = []
    for key in wanted:
        value = fingerprints.get(key)
        if not value:
            # 指纹缺了就建不出键。返回一个「少一维」的键的话,两个输入不同的
            # run 会算出同一个键,而后果是第二个被当成重复请求直接复用 ——
            # 拿 A 色的结果当 B 色的事实
            raise ValueError(
                f"作用域 {key!r} 没有指纹,建不出幂等键(§9.2 要求键含作用域指纹)"
            )
        out.append(("" if key is SHARED else str(key), value))
    if not out:
        raise ValueError("这个作用域一个指纹都没覆盖到,建不出幂等键")
    return out


def idempotency_key(
    *,
    spu_id: Any,
    scope: Scope,
    fingerprints: Mapping[Any, str],
    model_version: Any,
    prompt_version: Any,
    requested_fields: Iterable[str],
) -> str:
    """§9.2 的幂等键。

    组成(逐条对着 §9.2 原文):

        spu_id                 归属
        requested_scope        作用域本身(`canonical_scope` 的产物)
        作用域相关指纹          共享/颜色,由 `_scope_fingerprints` 挑
        model_version          **配置里的那个**,不是响应报回来的那个
        prompt_version         同上
        requested_fields       排序去重之后的字段清单

    缺 spu_id / 模型版本 / Prompt 版本 / 字段清单一律抛 `ValueError` ——
    见模块文档第三节。少一维的键不会报错,只会在少的那一维变化时
    把两次不同的识别判成同一次。
    """
    spu = _text_or_none(spu_id)
    model = _text_or_none(model_version)
    prompt = _text_or_none(prompt_version)
    fields = sorted({f for f in (_text_or_none(x) for x in requested_fields) if f})
    if not spu:
        raise ValueError("幂等键必须带 spu_id")
    if not model:
        raise ValueError(
            "幂等键必须带 model_version,而且要取配置里的那个 —— "
            "取响应里的话,键只有在付过钱之后才算得出来(见模块文档第三节)"
        )
    if not prompt:
        raise ValueError("幂等键必须带 prompt_version")
    if not fields:
        raise ValueError("幂等键必须带至少一个字段:一个什么都不问的 run 没有意义")
    if not _text_or_none(getattr(scope, "token", None)):
        raise ValueError("幂等键必须带 requested_scope")

    parts = [
        length_prefixed(KEY_VERSION),
        length_prefixed(spu),
        scope.token,
        length_prefixed(model),
        length_prefixed(prompt),
    ]
    for name, digest in _scope_fingerprints(scope, fingerprints):
        parts.append(length_prefixed(name))
        parts.append(length_prefixed(digest))
    for name in fields:
        parts.append(length_prefixed(name))
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


# ==========================================================
# §11 第一行:同一 run 内部分颜色的图全部失败
# ==========================================================
#
# §11 那一行要求三件事:
#
#     run = PARTIAL_SUCCESS       整体判定 —— `terminal_status_for` 已经给了
#     成功颜色的建议正常入队       合并层与 `queue_policy` 的事,这里不管
#     失败颜色明确列出,可单独重试   **本节**
#
# 前两件今天都成立,第三件一件都不成立:跑完之后**没有任何地方记得哪个颜色
# 全军覆没**。`succeeded_count` / `failed_count` 是两个标量,而
# 「3 张图散着失败」与「B 色的 3 张全失败」这两种情况在它们眼里一模一样 ——
# 前者每个颜色都还有证据,后者 B 色一条证据都没有。两者的下一步动作完全不同。
#
# ## 为什么不能事后从证据表反推
#
# 「这个 run 的输入里,哪几张没有留下证据行」看起来能反推出失败清单。它不能:
# `plan_evidence` 在模型只答了目标清单之外的字段时会**返回空计划**,
# 于是一张**调用成功、扣了钱**的图同样一条证据行都没有。
# 两种情况在库里长得一模一样,而它们一个该重试、一个不该。
#
# 所以「明确列出」这件事欠一列,规格写在
# `test_listing_the_failed_scopes_needs_a_column_and_here_is_which_one`。


@dataclass(frozen=True)
class ImageResult:
    """一张图跑完之后,判定需要知道的全部:它属于哪个颜色,成没成功。

    **作用域在这里归一,不指望调用方先归一。**`image_result()` 那条路本来
    就会归一,但它不是唯一的构造路径 —— 直接 `ImageResult(scope=..., ...)`
    同样合法,而一个空串或一串空格会当场变成一个**假颜色**:它进失败清单、
    进重试范围,而运营点下去重试的是一个不存在的作用域。

    这条守卫是变异写完之后跑出来的:第一版把归一留在 `scope_of` 里,
    而穷举用例直接构造 `ImageResult`,于是空串一路走到了失败清单。
    不变式该长在类型上,不是长在「记得走哪个构造函数」上。
    """

    scope: str | None
    succeeded: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _text_or_none(self.scope))
        object.__setattr__(self, "succeeded", bool(self.succeeded))


@dataclass(frozen=True)
class ScopeOutcome:
    """一个作用域在这次 run 里的成绩。"""

    scope: str | None
    total: int
    succeeded: int
    failed: int
    status: ExtractionRunStatus


@dataclass(frozen=True)
class RunOutcome:
    """整个 run 的成绩单。

    `status` **不是**在这里重新算的,它就是共享作用域那一档 ——
    共享作用域覆盖全部图(§6.3「共享字段跨全部证据合并」),
    所以它的成绩就是整个 run 的成绩。另起一套算法的话,
    `models/attribute.py` 那个派生属性和这里会给出两个答案。
    """

    status: ExtractionRunStatus
    scopes: tuple[ScopeOutcome, ...]
    failed_scopes: tuple[str, ...]
    retry_scope: Scope | None


def scope_of(asset: Any) -> str | None:
    """素材行 -> 它属于哪个颜色作用域。

    用 `getattr` 取 `color_variant_id`,与 `evidence_rules.asset_is_extraction_input`
    同一处理。**那一列已经是真列了**(A45-batch14-15 / 迁移 0037,
    `models/media_asset.py` 上有列,`tests/test_a45_batch14_22_colour_attribution_db.py`
    在用它)。这里保留 `getattr` 不是因为列可能缺,是因为本函数按鸭子类型接
    任何"素材行"—— 纯层的假对象、未来的投影对象都要能进来,而属性访问会让
    它们在缺列时 AttributeError。

    原来这里写着「那一列**今天还不存在**」。那句话在 14-15 之后过期了,
    A45-batch17-2 改掉 —— 与 `evidence_rules` 那两个溯源列同型同修法(§3.33):
    **一句过期的注释会让下一个人以为那条路还没通**,于是他不会去验按颜色重试的
    行为,而那正是决定"下一次付多少钱"的东西。

    **不读 `variant_hint`。**§4.8 明写它是「识别建议位」—— 拿它分作用域等于让
    模型猜出来的值决定**重试范围**,而重试范围决定下一次付多少钱。
    14-9 与 14-10 两次拒绝接线是同一条理由,这里是第三次。
    """
    return _text_or_none(getattr(asset, "color_variant_id", None))


def image_result(asset: Any, *, succeeded: bool) -> ImageResult:
    """素材行 + 成败 -> 判定入参。翻译层也放在这里,理由与
    `media/evidence_rules.py` 顶部那条相同:判定可穷举而喂给判定的东西
    不可穷举的话,守卫就只能靠 AST 看「调用出现了没有」。"""
    return ImageResult(scope=scope_of(asset), succeeded=bool(succeeded))


def _tally(rows: list[ImageResult], scope: str | None) -> ScopeOutcome:
    succeeded = sum(1 for r in rows if r.succeeded)
    failed = len(rows) - succeeded
    return ScopeOutcome(
        scope=scope,
        total=len(rows),
        succeeded=succeeded,
        failed=failed,
        status=terminal_status_for(
            image_count=len(rows), succeeded=succeeded, failed=failed
        ),
    )


def scope_lost_everything(outcome: ScopeOutcome) -> bool:
    """这个作用域这一轮是不是**一条证据都没拿到**。

    单独成一个有名字的判定,是变异验证逼出来的:它写在 `outcome_by_scope`
    的循环里时,`total > 0` 那一项**没有任何用例够得着** ——
    颜色是从「确实有图的行」里数出来的,所以循环里的 total 恒大于 0。
    删掉它不会有任何东西变红,而它恰恰是「没有图的颜色不算失败」这条
    规则的载体(§6.2 完整度门禁管那件事,重试识别管不了)。
    与 batch14 的 M9 一样,修法是**把判定挪到能穷举的地方**,
    不是把断言写得更长。

    两个边界各自有代价:

        total == 0 也算失败    运营被送去重试一个没有图的作用域,
                              再得到一次空结果,再花一次钱
        succeeded > 0 也算失败  一个 5 张里成功 4 张的颜色进重试清单,
                              为已经答上来的问题再买一次
    """
    return outcome.total > 0 and outcome.succeeded == 0


def outcome_by_scope(results: Iterable[ImageResult]) -> RunOutcome:
    """按作用域归集这一次 run 的成绩(§11 第一行)。

    ## 作用域的划法与指纹模块**必须**一致

    共享作用域覆盖**全部**图,颜色作用域只覆盖归属到该颜色的子集 ——
    与 `scope_fingerprint.fingerprints()` 一字不差。通用图(没有归属)
    只进共享,不进任何颜色子集:让它进每个颜色的话,「一张通用图失败」
    会把每个颜色都算成有失败,而 D1(作用域过宽)正是这个形状。

    ## 「全军覆没」的判据是**一次都没成功**,不是「有失败」

    写成 `succeeded < total` 的话,一个 5 张图里失败 1 张的颜色也会进重试清单,
    而那个颜色**已经有 4 张图的证据**了。重试它 = 为一个已经答上来的问题
    再付一次钱,而且付的是整个作用域的钱。

    ## 没有图的颜色不在这里出现

    一个颜色一张图都没有,不是「失败」——那是 §6.2 样品完整度的问题
    (`media/sample_completeness.py`),它的下一步是去拍照,不是重试识别。
    本函数只看这次 run **实际跑过**的图,所以那种颜色根本不进结果。
    混进来的后果是运营点「重试这个颜色」,而重试一个没有图的作用域
    只会再得到一次空结果。

    ## 共享作用域永远不进 `failed_scopes`

    共享覆盖全部图,所以它「全军覆没」等价于整个 run 失败 ——
    那时该做的是整体重跑,不是「重试某个作用域」。把它列进去的话,
    重试范围会变成一个既不是全部、也不是某个颜色的东西。
    """
    rows = [r for r in results]
    shared = _tally(rows, SHARED)
    colours = sorted({r.scope for r in rows if r.scope})
    scopes = [shared]
    failed: list[str] = []
    for colour in colours:
        outcome = _tally([r for r in rows if r.scope == colour], colour)
        scopes.append(outcome)
        if scope_lost_everything(outcome):
            failed.append(colour)
    retry = canonical_scope(variant_ids=failed) if failed else None
    return RunOutcome(
        status=shared.status,
        scopes=tuple(scopes),
        failed_scopes=tuple(failed),
        retry_scope=retry,
    )
