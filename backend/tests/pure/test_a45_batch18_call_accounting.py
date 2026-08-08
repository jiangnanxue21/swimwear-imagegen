"""付费调用的**次数**必须进台账(A45-batch18 / P1-2 + P2-2)。

## 这一组守的是哪两个洞

两个洞形状完全一样,所以放在同一个文件里 —— 分开写会让下一个人以为
它们是两件事,然后只修一边:

    识别   `llm/transport` 对超时/429/5xx 自动重试,一次业务调用最多发出
           `1 + EXTRACTOR_MODEL_MAX_RETRIES` 个请求;台账固定记 1
    生成   `fashn.submit` 把候选拆成多次 POST,中途失败时前几次已被受理;
           台账走默认值 `max(candidate_count, 1)`,失败分支恒为 1

两个方向同时错:**已经发出去的请求少记**,以及**一个请求都没发出去的
preflight 失败凭空记一笔**。叠在一起之后 `/spend` 与供应商账单再也无法
逐条核对,而 §10.2 第 5 条准入要的正是"用量记录与 Provider 后台账单
条数一致"。

## 为什么判定值得单独放进纯模块

失败模式全部是**边界**:0 次 / 1 次 / 重试满 / 没人报。这些分支只有在
零依赖模块里才穷举得起来 —— 留在服务层的话,覆盖"preflight 失败记 0"
就要起一个数据库加一个假的供应商,而那种测试没人会为一个比较运算去写。
与 `extractors/call_budget.py` 是同一条路子。

## AST 那几条守的是接线,不是算术

算术全对而没有人调用它,是这个仓库付过两次代价的那种失败
(`describe_extractors` 硬编码、`record_cost` 零调用)。所以下面有几条
直接读源码:确认 `_record_extraction_usage` 真的不再写死 1、
确认失败分支真的传了 `billable_units`。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.core.enums import UnitsSource
from app.extractors import call_accounting as extraction
from app.providers import call_accounting as generation
from app.providers.fashn import ATTEMPTED_CALLS_KEY, PARTIAL_IDS_KEY

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ATTR_SERVICE = BACKEND_ROOT / "app" / "attributes" / "service.py"
GEN_TASKS = BACKEND_ROOT / "app" / "tasks" / "generation_tasks.py"
TRANSPORT = BACKEND_ROOT / "app" / "llm" / "transport.py"
VISION = BACKEND_ROOT / "app" / "extractors" / "vision.py"


# ================================================================ 识别侧判定


def test_a_retried_call_bills_every_round_trip():
    """三次往返 = 三个计费单位。**这一条是整组的理由。**

    厂商在收到请求那一刻开始计费,超时不退钱 —— 所以"最后成没成功"
    不是判据,"发出去了几次"才是。
    """
    units, source = extraction.settle_extraction_units(reported_attempts=3)
    assert units == 3
    assert source == UnitsSource.INFERRED.value


def test_a_preflight_failure_bills_nothing():
    """一个字节都没出去的失败不该记一笔消费。

    Schema 构造、图片准备、未配置 —— 这三类都发生在第一次往返之前。
    原来它们和"供应商拒绝了我们"记成同一个数,于是台账里凭空多出
    一批从来没花过的钱,而那批数字看起来和真的一模一样。
    """
    assert extraction.settle_extraction_units(reported_attempts=0)[0] == 0


def test_an_extractor_that_does_not_report_falls_back_to_one_not_zero():
    """**"不知道"绝不能写成"没花钱"。**

    抽取器是可插拔的(`extractors/registry.py`)。一个第三方 billable
    抽取器没接上次数上报时,把它记成 0 会让一整类真实调用从账上消失,
    而那个方向的错误没有人会去发现。退回 1 是这次修改之前的行为,
    也是安全的那一侧 —— 与 `providers/base.ProviderUsage` 对
    `units is None` 的处理同一条规矩。
    """
    assert extraction.settle_extraction_units(reported_attempts=None)[0] == 1


def test_a_broken_attempt_value_is_treated_as_not_reported():
    """坏值退回"没报",不退回 0。

    负数、字符串、布尔都可能从一个写错的抽取器里冒出来。让流水记成
    负单位比缺一列更糟:金额会**减少**,而月度汇总不会报任何错。

    布尔单独点名:Python 里 `isinstance(True, int)` 为真,不显式挡掉的话
    `provider_attempts=True` 会被当成 1 次往返记进账。
    """
    for bad in (-1, "3", 3.0, True, None):
        assert extraction.attempts_from({extraction.ATTEMPTS_KEY: bad}) in (None,), bad


def test_zero_is_read_back_as_zero_not_as_missing():
    """0 是一个结论(确认没发出去),不是缺省。两者合并会让 preflight 记 1。"""
    assert extraction.attempts_from({extraction.ATTEMPTS_KEY: 0}) == 0
    assert extraction.attempts_from({}) is None


def test_the_message_tells_the_operator_whether_money_moved():
    """运营看到"识别失败"时唯一想得到的动作是再点一次,而每次都在花钱。

    告诉他这次发出去几个请求,他才有第二个选项:去供应商后台核对。
    """
    assert "未发出" in extraction.describe_attempts(0)
    assert "2" in extraction.describe_attempts(2)
    assert "未知" in extraction.describe_attempts(None)


# ================================================================ 生成侧判定


def test_a_partial_batch_bills_every_post_that_left_the_process():
    """四次提交、第四次失败:记 4,不是 3。

    第四个请求**已经到了 FASHN 那里**(它的失败可能是响应超时而不是被拒),
    很可能已经计费。按已受理的 3 个记账,少记的恰恰是最可能出问题、
    最需要人去核对的那一次。
    """
    detail = {ATTEMPTED_CALLS_KEY: 4, PARTIAL_IDS_KEY: ["p1", "p2", "p3"]}
    units, source = generation.failed_submit_units(detail)
    assert units == 4
    assert source == UnitsSource.INFERRED.value
    # 受理成功那个数仍然读得出来 —— 报错文案要用它,记账不用
    assert generation.accepted_ids(detail) == 3


def test_a_failure_before_the_first_post_bills_nothing():
    """素材下载、输入校验在第一个 POST 之前失败:一个请求都没发。

    原来这一路走 `record_usage` 的默认值 `max(candidate_count, 1)`,
    而失败分支 candidate_count 是 0,所以恒为 1 —— 一笔从没花过的钱。
    """
    assert generation.failed_submit_units({ATTEMPTED_CALLS_KEY: 0})[0] == 0


def test_an_exception_from_outside_submit_falls_back_to_one():
    """`_require_config` / `validate_request` / 缺模特图 抛出的异常
    不经过 `_with_partial_ids`,detail 里没有这个键。

    退回 1(修改前的行为),不退回 0:把"不知道"当成"没花钱"会让一整类
    真实调用从账上消失。
    """
    assert generation.failed_submit_units(None)[0] == 1
    assert generation.failed_submit_units({})[0] == 1
    assert generation.failed_submit_units({PARTIAL_IDS_KEY: ["p1"]})[0] == 1


def test_units_source_is_never_provider_on_a_failed_submit():
    """失败时**不许**冒充厂商自述。

    `x-fashn-credits-used` 只在成功收尾那条路径上拿得到;失败时那个头
    要么没有,要么语义至今没有连过真端点确认(是本次还是累计)。
    标成 provider 会让对账的人拿着一个估算值去质问厂商。
    """
    for detail in ({ATTEMPTED_CALLS_KEY: 2}, {ATTEMPTED_CALLS_KEY: 0}, {}):
        assert generation.failed_submit_units(detail)[1] == UnitsSource.INFERRED.value


# ================================================================ 接线


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function(path: Path, name: str) -> str:
    src = _source(path)
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    return ast.get_source_segment(src, fn) or ""


def _call_in(path: Path, fn_name: str, called: str) -> ast.Call:
    """按 AST 找函数体里那次调用。**不切源码字符串。**"""
    src = _source(path)
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name
    )
    return next(
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and ast.unparse(n.func).split(".")[-1] == called
    )


def test_extraction_usage_no_longer_hardcodes_one_unit():
    """**这一条守的是修复本身还在。**

    `billable_units=1` 是原来那一行。它删掉之前,上面全部判定都可以是绿的
    而账仍然错 —— 算术和接线是两件事,这个仓库为它付过两次代价。

    ## 判据打在实参上,不打在源码字符串上

    第一版是 `assert "billable_units=1" not in body`,而 `body` 是
    `ast.get_source_segment()` 切出来的**整个函数,含 docstring** ——
    那段 docstring 里逐字写着 `billable_units=1` 来解释被修掉的旧缺陷。
    于是这条守卫在**修复完全正确**的代码上稳定报红:它查的是"这几个字
    出现过没有",而要问的是"那个实参今天是不是一个字面量"。

    §3.26 第四节那句话的又一次:按字符串在源码里找东西,找到的从来不保证
    是你以为的那个位置。这次的失效方向是**假红** —— 而假红比假绿更贵:
    它会让人开始怀疑整套守卫,然后加豁免、然后关掉。
    """
    call = _call_in(ATTR_SERVICE, "_record_extraction_usage", "record_usage")
    passed = {kw.arg: kw.value for kw in call.keywords}

    assert "billable_units" in passed, "流水里没有计费单位这一项"
    assert not isinstance(passed["billable_units"], ast.Constant), (
        "`billable_units` 又是一个字面量了 —— 写死就是这次要修的那个 bug。"
        f"现在传的是 {ast.unparse(passed['billable_units'])}"
    )
    assert "provider_attempts" in passed, "次数要单独落列,供对账用"

    body = _function(ATTR_SERVICE, "_record_extraction_usage")
    assert "settle_extraction_units" in body, "计费单位必须由纯判定给出"


def test_both_extraction_branches_pass_the_attempt_count():
    """成功和失败**两条**分支都要传次数。

    只接一边的话,一次三下往返之后成功的调用记 3,而一次三下往返之后
    失败的调用仍然记 1 —— 而后者恰恰是更可能重复计费的那一种。
    """
    body = _function(ATTR_SERVICE, "run_extraction")
    assert body.count("reported_attempts=") == 2, (
        "成功分支和失败分支各一次;少一处就是半条修复"
    )


def test_the_transport_reports_every_round_trip_before_it_makes_it():
    """回调必须在 `round_trip` **之前**触发。

    放在之后就变成"成功了几次",而厂商在收到请求那一刻就开始计费。
    """
    body = _function(TRANSPORT, "send")
    assert "on_attempt" in body
    assert body.index("on_attempt(attempt + 1)") < body.index("await round_trip"), (
        "计的是发出去了几次,不是成功了几次"
    )


def test_every_exit_from_the_extractor_carries_the_attempt_count():
    """成功走 `meta`,失败走异常 detail —— 两条路都要带。

    失败路径尤其不能少:服务层看到的是一个异常,而"这个异常发生在第几次
    往返之后"只有传输层知道。
    """
    wrapper = _function(VISION, "extract_async")
    assert "_with_attempts" in wrapper, "失败路径必须挂上次数"
    inner = _function(VISION, "_extract_once")
    assert "ATTEMPTS_KEY] = attempts.count" in inner, "成功路径必须写进 meta"


def test_a_failed_submit_no_longer_takes_the_default_unit():
    """失败分支必须显式传 `billable_units`。

    不传就是 `max(candidate_count, 1)`,而失败时 candidate_count 是 0 ——
    也就是恒为 1,不论这次发了 0 个还是 4 个请求。
    """
    src = _source(GEN_TASKS)
    start = src.index("failed_units, units_source = ")
    window = src[start : start + 900]
    assert "billable_units=failed_units" in window
    assert "units_source=units_source" in window
    assert "provider_attempts=" in window


def test_fashn_counts_the_post_before_it_sends_it():
    """`attempted += 1` 必须排在 `_request` 之前、`build_inputs` 之后。

    排在 `_request` 之后的话,失败的那一次数不进去 —— 而那一次恰恰是
    最可能已经到达 FASHN、最可能已经计费的一次。
    排在 `build_inputs` 之前的话,素材下载失败会被记成一次已发出的请求。
    """
    body = _function(BACKEND_ROOT / "app" / "providers" / "fashn.py", "submit")
    build_at = body.index("await self.build_inputs(")
    count_at = body.index("attempted += 1")
    send_at = body.index("body, _ = await self._request(")
    assert build_at < count_at < send_at
