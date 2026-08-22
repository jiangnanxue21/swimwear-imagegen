"""A45 batch12-2:独立异常场景审查(EX-01 ~ EX-06)的守卫。

六条修复的共同主题只有一个词:**钱**。每一条都是"系统在某个崩溃点上会
自动地、无人值守地再花一次钱",或者"人被逼到只能用一条会重复上架的路"。

    EX-01  Provider 已受理但外部 ID 落库失败 -> 判成普通卡死 -> 一键重试再买一次
    EX-02  批量付费调用崩在返回之后 -> 回收器自动重跑 -> 无人值守的重复计费
    EX-03  查状态/下载失败 -> 重试清掉外部 ID 回队列 -> 同一轮生成买第二次
    EX-04  上架 UNKNOWN / DEAD 没有出口 -> 运营改草稿绕过 -> 平台上多一件商品
    EX-05  候选文件坏了 -> 续跑读同一个坏文件 -> FAILED/FORMATTING 死循环
    EX-06  创建批次没有请求级幂等 -> 双击建出两个批次

## 这份守卫为什么大量用源码断言

`tests/pure/` 里没有 sqlalchemy、没有 pydantic、没有数据库(见
`run_pure_tests.py` 的自检)。而这六条修复里有四条的判定发生在**查询条件**
和**事务边界**上 —— 那些东西没法用纯函数验证。

能穷举的部分(`receipt_route`、状态机、指纹)照常直接调。剩下的用 AST 钉住
"这段代码有没有在做那件事":它挡不住实现写错,但挡得住**实现被悄悄删掉**,
而回归里更常见的恰恰是后者 —— 一次重构顺手把一个看起来多余的条件去掉,
测试全绿,而重复扣费在三个月后的账单上出现。

真正的行为验证是那六组故障注入测试,它们需要真 PostgreSQL,跑在 CI 上。
(列出它们的那份批次评审文档已删 —— 口径与结论去处见
`docs/DECISIONS.md` §3.107。)
"""
from __future__ import annotations

import ast

from app.core.enums import (
    PROVIDER_RESULT_PENDING_CODE,
    SOURCE_ASSET_BROKEN_CODES,
    SOURCE_ASSET_CORRUPT_CODE,
    SOURCE_ASSET_MISSING_CODE,
    SUBMIT_UNKNOWN_CODE,
    TaskStatus,
)
from app.workbench.batch import (
    BatchAction,
    ReceiptRoute,
    ReceiptStatus,
    batch_request_fingerprint,
    receipt_route,
)
from app.workflows.state_machine import (
    AWAITING_PROVIDER_RESULT_STATUSES,
    SPENT_STATUSES,
    SUBMIT_IN_DOUBT_STATUSES,
    can_transition,
)
from tests.pure._helpers import BACKEND_ROOT

GENERATION_SERVICE = BACKEND_ROOT / "app" / "services" / "generation_service.py"
GENERATION_TASKS = BACKEND_ROOT / "app" / "tasks" / "generation_tasks.py"
BATCH_SERVICE = BACKEND_ROOT / "app" / "workbench" / "batch_service.py"
OUTPUT_SERVICE = BACKEND_ROOT / "app" / "services" / "output_service.py"
PUBLISH_API = BACKEND_ROOT / "app" / "api" / "publish.py"
PUBLISH_SERVICE = BACKEND_ROOT / "app" / "services" / "publish_service.py"
BATCH_API = BACKEND_ROOT / "app" / "api" / "workbench_batch.py"
MIGRATIONS = BACKEND_ROOT / "migrations" / "versions"


def _fn(path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到 {name} —— 改名了就把这份守卫一起改")


def _src(path, name: str) -> str:
    return ast.dump(_fn(path, name))


def _calls(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


# ==========================================================
# EX-01:外部 ID 落库失败的任务不许掉进"可一键重试"那一档
# ==========================================================


def test_the_reaper_splits_by_status_not_by_whether_the_id_landed():
    """病灶就在那个 `AND external_task_id IS NOT NULL` 上。

    「Provider 受理了」和「外部 ID 落库了」中间隔着一次 commit:

        _checkpoint(task, "submitting")   状态 = SUBMITTING,已落库
        provider.submit(request)          对方受理,可能已经开始计费
        task.external_task_id = 外部 ID
        session.commit()                  <-- 崩在这里

    崩在最后一句的行长成「SUBMITTING + ID 为空」。带 ID 条件分批的话它会掉进
    `WORKER_STALLED`,而那个码的含义是"钱没花出去,直接重试即可"——
    于是运营点一次重试就再买一次,第一次那批图永远没人认领。

    现在两批**只按状态划分**。这条断言钉的是"划分依据里不许再出现 ID"。
    """
    fn = _fn(GENERATION_SERVICE, "_reap_batch")
    src = ast.dump(fn)
    assert "SPENT_STATUSES" in src, "回收分批不再按状态划分了?"

    # `external_task_id` 仍然允许出现 —— 它现在只用来分「有 ID 可查」和
    # 「连 ID 都没有」两种措辞。但它不许再和 spent_only 绑在一起决定**分档**
    reap = _fn(GENERATION_SERVICE, "reap_stalled")
    dump = ast.dump(reap)
    assert dump.count("SUBMIT_UNKNOWN_CODE") >= 2, (
        "两种「结果未知」必须都落 SUBMIT_RESULT_UNKNOWN —— "
        "外部 ID 没落库的那一批漏掉的话,EX-01 原样复发"
    )


def test_every_spent_status_is_covered_by_exactly_one_reaper_bucket():
    """`SPENT_STATUSES` 是"请求可能已经发出去"的清单,回收器必须整个用它。

    只挑其中几个的话,漏掉的那个状态上的卡死任务会走普通重试 —— 而这份
    清单存在的全部理由就是那些状态下重试等于再买一次。

    ## A45-batch12-3 / REG-02:整个用它,但**不再落同一个码**

    这条原来叫 `..._the_reaper_treats_as_unknown`,而那个名字断言的是
    「整份清单一律 `SUBMIT_RESULT_UNKNOWN`」—— 那正是 REG-02 报的缺陷:
    `PROVIDER_RUNNING` / `DOWNLOADING` 且外部 ID 还在的那一批被压成"提交
    结果未知",而它的唯一出口(强制重试)会清掉 ID 重新 submit。

    现在这份清单拆成两档,两档**不相交、并起来等于原清单** —— 这条断言
    钉的就是这个:少一个状态会掉进 `WORKER_STALLED`,多算一次就是重复计费。
    """
    assert TaskStatus.SUBMITTING.value in SPENT_STATUSES
    assert TaskStatus.PROVIDER_RUNNING.value in SPENT_STATUSES
    assert TaskStatus.DOWNLOADING.value in SPENT_STATUSES
    assert not (SUBMIT_IN_DOUBT_STATUSES & AWAITING_PROVIDER_RESULT_STATUSES), (
        "两档重叠:同一个状态会被两批 UPDATE 各认领一次"
    )
    assert SUBMIT_IN_DOUBT_STATUSES | AWAITING_PROVIDER_RESULT_STATUSES == SPENT_STATUSES, (
        "两档并起来不等于 SPENT_STATUSES —— 漏掉的那个状态会掉进 WORKER_STALLED,"
        "而那个码的含义是「钱没花出去,直接重试即可」"
    )


def test_a_resumed_task_without_an_external_id_is_not_resubmitted():
    """续跑分支自己也要守住 EX-01 的那个形状。

    停在 PROVIDER_RUNNING 却没有外部 ID 的任务,**不许往下走**:往下走会
    重新 submit,而这条任务恰恰属于"可能已经受理过"的那一类。
    """
    src = GENERATION_TASKS.read_text(encoding="utf-8")
    run = _fn(GENERATION_TASKS, "_run")
    dump = ast.dump(run)
    assert "SUBMIT_UNKNOWN_CODE" in dump, (
        "PROVIDER_RUNNING 续跑分支缺少「没有外部 ID 就交对账」的兜底 —— "
        "少了它,这条路会变成一个新的重复提交入口"
    )
    assert "_await_and_collect" in src


# ==========================================================
# EX-02:批量付费动作不许自动重复付费
# ==========================================================


def test_a_dispatched_call_never_gets_an_automatic_paid_retry():
    """EX-02 的正题。**一次都不许**,不是"最多一次"。

    这条与 `test_a45_batch12_fixes` 里那条是同一件事的两个方向:那边守的是
    "上限存在",这边守的是"发出去过就没有上限可言"。
    """
    for action in BatchAction:
        for executions in (1, 2, 5):
            assert (
                receipt_route(
                    ReceiptStatus.IN_FLIGHT.value,
                    action,
                    executions=executions,
                    call_dispatched=True,
                )
                is ReceiptRoute.NEEDS_RECONCILIATION
            ), (action, executions)


def test_a_call_that_never_left_is_free_to_rerun():
    """反方向:可证明没花钱的那一支必须免费重跑,而且不占额度。

    不放过它的代价是每一次 pod 驱逐都要人工介入 —— 那种代价下这道闸迟早
    会被整个关掉,而那才是最坏的结局。
    """
    assert (
        receipt_route(
            ReceiptStatus.IN_FLIGHT.value,
            BatchAction.GENERATE_COPY,
            executions=5,
            call_dispatched=False,
        )
        is ReceiptRoute.EXECUTE
    )


def test_the_marker_is_written_in_its_own_transaction():
    """标记写在业务事务里等于没写 —— 崩溃时它会跟着一起回滚。

    与 `_claim_in_flight` 完全同一条理由。两个函数必须都自己开会话。
    """
    fn = _fn(BATCH_SERVICE, "_mark_provider_call")
    assert "SessionLocal" in ast.dump(fn), (
        "付费调用标记没有用独立会话 —— 它的全部意义就是崩溃之后还读得到"
    )
    assert "commit" in _calls(fn)


def test_the_marker_is_written_before_the_paid_executors():
    """标记必须在付费执行器**之前**落。之后落的话它永远等于没有。

    A45-batch12-3:这条原来只断言两个名字都出现在 `_calls(_execute)` 里,
    而那**验证不了顺序** —— 把 `_mark_provider_call` 挪到 `_exec_extract`
    后面,它照样绿,可标记从此永远等于没写。docstring 说的是顺序,断言
    也得说顺序:按行号比,标记的第一次调用必须在两个执行器之前。
    """
    fn = _fn(BATCH_SERVICE, "_execute")

    def _first_line(name: str) -> int:
        lines = [
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        ]
        assert lines, f"`_execute` 里找不到对 {name} 的调用"
        return min(lines)

    marker = _first_line("_mark_provider_call")
    for executor in ("_exec_extract", "_exec_generate_copy"):
        assert marker < _first_line(executor), (
            f"调用标记落在 {executor} 之后 —— 崩在付费调用里的时候它还没写下,"
            "而那正是唯一需要它的时刻"
        )


def test_a_marker_that_did_not_land_blocks_the_paid_call():
    """标记写不下去就不许调付费模型(A45-batch12-3)。

    `_mark_provider_call` 的返回值原来是丢掉的,而 `_execute` 传给
    `receipt_route` 的 `call_dispatched` 是**显式**算的
    (`receipt.provider_call_at is not None`)—— 所以"标记没写进去、
    但调用发出去了、然后崩溃"会被下一次回收判成「可证明没花钱」,
    自动重跑一次。默认值 True 保护不了这条路,它只保护不传参的调用方。

    这里钉三件事:返回值进了判断、失败分支不往下走、而且**不留一条
    IN_FLIGHT** 去冒充对账工单。
    """
    fn = _fn(BATCH_SERVICE, "_execute")
    guard = next(
        (
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.If)
            and "_mark_provider_call" in ast.unparse(node.test)
        ),
        None,
    )
    assert guard is not None, (
        "`_mark_provider_call` 的返回值没有进任何判断 —— 它又变回一句日志了"
    )
    assert isinstance(guard.test, ast.UnaryOp) and isinstance(
        guard.test.op, ast.Not
    ), "判断方向反了:要挡的是**失败**那一支"
    body = ast.unparse(guard.body)
    assert "return" in body, "标记失败之后没有停下,还会继续往下调付费模型"
    assert "_settle_unbilled_failure" in body, (
        "没把回执落成\"确认没花钱\" —— 那条 IN_FLIGHT 会被对账脚本捞成"
        "一条假工单,而我们明明知道这次没调用"
    )


def test_reclaiming_a_receipt_clears_the_previous_call_marker():
    """重新认领必须把上一轮的标记清成 NULL。

    留着的话新一次执行还没走到调用就已经带着标记,崩溃时被判成"可能已计费"
    —— 一个凭空的人工工单,而且它会重复发生。

    A45-batch12-3:这条原来只断言 `"provider_call_at" in ast.dump(fn)`。
    那**分不出实现和它的反面** —— 把 `"provider_call_at": None` 改成
    `utc_now()`,字符串照样在,测试照样绿,而那正是上面这段话讲的 bug。
    所以直接去 upsert 的 `set_` 字典里取那个值,断言它是 `None` 字面量。
    """
    fn = _fn(BATCH_SERVICE, "_claim_in_flight")
    values: list[ast.expr] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "provider_call_at":
                values.append(value)
    assert values, "认领没有重置调用标记 —— upsert 的 set_ 里根本没提到它"
    for value in values:
        assert isinstance(value, ast.Constant) and value.value is None, (
            "认领把调用标记写成了一个非 NULL 的值:新一次执行还没调用就带着标记,"
            f"崩溃时会被判成\"可能已计费\"(实际写的是 {ast.unparse(value)})"
        )


def test_the_reconciliation_escape_hatch_can_still_see_what_is_stuck():
    """闸门必须有出口,而出口的取数条件必须跟着闸门一起改。

    `resolve_billed_unknown.py` 原来只按 `executions >= MAX` 取数,那是
    "自动付费额度用完了"的口径。现在 IN_FLIGHT 只要调用发出过就当场停下,
    `executions` 还是 1 —— 照旧条件取数的话这个出口对新卡住的那一批
    **一条都列不出来**,而它们正是现在唯一会卡住的那一批。
    """
    src = (BACKEND_ROOT / "tools" / "resolve_billed_unknown.py").read_text(
        encoding="utf-8"
    )
    assert "provider_call_at" in src, (
        "对账出口没有按新口径取数 —— 闸门装上了,出口对不上号"
    )


# ==========================================================
# EX-03:外部任务还在时,重试不许重新提交
# ==========================================================


def test_the_resume_gate_requires_both_the_code_and_the_id():
    """两个条件缺一不可。

    少了错误码,`CONTENT_SAFETY` 这种"外部已有结论"的失败会被拖进续跑,
    一遍遍去问一个不会变的答案;少了 ID 检查,这条边就退化成"从 FAILED
    随便跳回等结果"。
    """
    dump = _src(GENERATION_SERVICE, "can_resume_provider_results")
    assert "PROVIDER_RESULT_PENDING_CODE" in dump
    assert "external_task_id" in dump


def test_the_provider_resume_branch_keeps_the_external_id():
    """续跑分支**不许**出现清 ID 的那一句。

    `external_task_id = None` 是这条链路上唯一一个会把已付费任务变成孤儿的
    动作。通用重试分支里它是对的(那条路本来就要重新生成),续跑分支里
    出现一次就等于 EX-03 原样复发。
    """
    fn = _fn(GENERATION_SERVICE, "retry_task")
    branch = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "can_resume_provider_results" not in test:
            continue
        branch = "\n".join(ast.unparse(stmt) for stmt in node.body)
        break
    assert branch is not None, "retry_task 里没有 provider_results 续跑分支"
    assert "provider_results" in branch
    assert "external_task_id" not in branch, (
        "续跑分支碰了 external_task_id —— 这条链路上唯一一个会把已付费任务"
        "变成孤儿的动作就是清掉它"
    )
    assert "PROVIDER_RUNNING" in branch


def test_only_retrieval_failures_are_treated_as_resumable():
    """判据是错误码不是阶段 —— 同样发生在 status 阶段的两类,恢复路径相反。"""
    src = GENERATION_TASKS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assign = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_RESULT_RETRIEVABLE_CODES"
    )
    dump = ast.dump(assign)
    for allowed in ("NETWORK_TIMEOUT", "RATE_LIMITED", "PROVIDER_SERVICE_ERROR"):
        assert allowed in dump, f"{allowed} 该算作可续跑的取结果失败"
    for forbidden in ("CONTENT_SAFETY", "GENERATION_FAILED"):
        assert forbidden not in dump, (
            f"{forbidden} 是外部任务自己的结论,续跑只会一遍遍问同一个答案"
        )


def test_the_resume_edge_exists_and_the_dangerous_ones_do_not():
    assert can_transition(TaskStatus.FAILED, TaskStatus.PROVIDER_RUNNING)
    assert not can_transition(TaskStatus.FAILED, TaskStatus.SUBMITTING), (
        "FAILED -> SUBMITTING 会重新 submit —— 同一轮生成买第二次"
    )


# ==========================================================
# EX-04:上架的两种"停住"都必须有出口
# ==========================================================


def test_both_recovery_endpoints_exist():
    """`allowed_actions` 里出现一个没有端点的动作 = 前端渲染一个点了报 404 的
    按钮。这两条动作是本轮新加的,所以端点必须同时在。
    """
    src = PUBLISH_API.read_text(encoding="utf-8")
    assert '"/listings/{listing_id}/reconcile"' in src
    assert '"/listings/{listing_id}/redeliver"' in src


def test_reconcile_is_wired_to_the_service_function_that_already_existed():
    """EX-04 的病灶不是"没实现",是**实现了却没接线**。

    `reconcile_unknown()` 从任务 18 起就写好了,而全仓唯一的调用点是一条
    数据库测试。这条断言钉住它现在真的挂在一个端点上。
    """
    assert "reconcile_unknown" in _calls(_fn(PUBLISH_API, "reconcile"))
    assert "redeliver_dead" in _calls(_fn(PUBLISH_API, "redeliver"))


def test_redelivery_reuses_the_original_attempt_and_key():
    """两条动作的**全部**安全性来自"不算新键"。

    换一把键再发一次才是重新创建,而那正是运营在没有出口时会被逼着去做的
    事(改草稿 -> 新指纹 -> 新键 -> 平台上多一件商品)。
    """
    fn = _fn(PUBLISH_SERVICE, "redeliver_dead")
    dump = ast.dump(fn)
    assert "idempotency_key" not in dump or "reused_idempotency_key" in dump, (
        "重新投递不该自己算幂等键"
    )
    assert "DEAD" in dump, "重新投递没有限定只放行 DEAD"
    assert "with_for_update" in dump, (
        "没有行锁 —— 两个运营同时点会让投递计数被清两次"
    )


def test_both_recovery_actions_demand_a_written_reason():
    """由人承担"再对外发一次请求"的动作,必须留下依据。

    与 `retry_task(force=True)` 完全同一条理由:填不出结论说明核对没做。
    """
    src = PUBLISH_API.read_text(encoding="utf-8")
    assert "class RecoverIn" in src
    assert "min_length=1" in src, "说明可以为空的话,审计里会留下一片空白"
    assert "note" in ast.dump(_fn(PUBLISH_SERVICE, "redeliver_dead"))


# ==========================================================
# EX-05:坏文件不许让重试变成死循环
# ==========================================================


def test_the_source_asset_is_actually_decoded_not_just_counted():
    """只看存在与长度会把一段 HTML 错误页当成可用图片。

    那正是 EX-05 里那个不收敛循环的开头:`exists()` 说有、长度也不为零,
    然后在 `render_outputs` 里炸成一个通用 INTERNAL_ERROR,而通用码挡不住
    重试。必须真的解一次。
    """
    calls = _calls(_fn(OUTPUT_SERVICE, "source_asset_problem"))
    assert "exists" in calls
    assert "read" in calls
    assert "probe_image" in calls, "没有真的解码 —— 坏文件会被放过去"


def test_formatting_verifies_the_asset_before_resuming():
    assert "source_asset_problem" in _calls(_fn(GENERATION_TASKS, "_format_outputs"))


def test_a_broken_asset_blocks_the_plain_retry():
    """两条往下的路都是错的,所以必须挡在 `retry_task` 里。

        续跑 FORMATTING   读同一个坏文件,FAILED/FORMATTING 死循环
        回 QUEUED 重生     再花一次 Provider 的钱,而且换了 seed

    挡住之后由人显式选:换一张候选(免费),或者带 force + 说明重新生成。
    """
    dump = _src(GENERATION_SERVICE, "retry_task")
    assert "SOURCE_ASSET_BROKEN_CODES" in dump
    assert SOURCE_ASSET_MISSING_CODE in SOURCE_ASSET_BROKEN_CODES
    assert SOURCE_ASSET_CORRUPT_CODE in SOURCE_ASSET_BROKEN_CODES


def test_a_forced_retry_on_a_broken_asset_regenerates_rather_than_resumes():
    """强制重试必须走重新生成 —— 续跑读的正是那个坏文件。

    这一条挡的是"显而易见的错改法":让 force 直接放行到
    `can_resume_formatting()`。那样 force 会变成一个什么都改变不了的按钮,
    而运营会一直点它。
    """
    fn = _fn(GENERATION_SERVICE, "retry_task")
    src = ast.unparse(fn)
    broken_pos = src.find("SOURCE_ASSET_BROKEN_CODES:")
    formatting_pos = src.find("can_resume_formatting")
    assert broken_pos != -1 and formatting_pos != -1
    assert broken_pos < formatting_pos, (
        "坏文件分支必须排在 can_resume_formatting 之前 —— "
        "排在后面的话它永远走不到,因为那个判定对坏文件同样返回 True"
    )


# ==========================================================
# EX-06:创建批次这条命令本身的幂等
# ==========================================================


def test_the_fingerprint_ignores_order_and_duplicates():
    """`[A, B]` 与 `[B, A]` 是同一次请求。

    不归一的话,一个把商品存在 Set 里的前端会因为遍历顺序不稳定算出两个
    指纹,于是同一次重发被判成 409 —— 而那个 409 没有人能看懂。
    """
    a = batch_request_fingerprint(
        BatchAction.EXTRACT, ["p2", "p1", "p1"], include_exported=False, label=None
    )
    b = batch_request_fingerprint(
        BatchAction.EXTRACT, ["p1", "p2"], include_exported=False, label=None
    )
    assert a == b


def test_the_fingerprint_moves_when_the_request_means_something_else():
    """判据同 `idempotency_key`:改了它,这次请求要做的事会不一样吗。"""
    base = dict(product_ids=["p1"], include_exported=False, label=None)
    ref = batch_request_fingerprint(BatchAction.EXTRACT, **base)
    assert batch_request_fingerprint(BatchAction.EXPORT, **base) != ref
    assert (
        batch_request_fingerprint(
            BatchAction.EXTRACT, ["p1", "p2"], include_exported=False, label=None
        )
        != ref
    )
    assert (
        batch_request_fingerprint(
            BatchAction.EXTRACT, ["p1"], include_exported=True, label=None
        )
        != ref
    )
    # label 不改变要做的事,但改变批次的**身份** —— 运营靠它认出哪个是哪个
    assert (
        batch_request_fingerprint(
            BatchAction.EXTRACT, ["p1"], include_exported=False, label="第二批"
        )
        != ref
    )


def test_the_same_key_with_different_input_is_a_conflict_not_a_silent_reuse():
    """静默返回原批次比多建一个更危险:界面上一切正常,只是有一批商品
    从来没被处理过,而客户端以为做了。
    """
    dump = _src(BATCH_SERVICE, "_same_request_or_conflict")
    assert "request_fingerprint" in dump
    assert "DUPLICATE_RESOURCE" in dump


def test_the_create_path_reads_back_the_winner_instead_of_erroring():
    """"先查有没有"是 check-then-act,并发下两个请求会双双查空。

    裁决只能由唯一约束做出,而 Python 这一层的职责是认输之后回读 ——
    与 `generation_service` 处理生成幂等键冲突是同一个套路。
    """
    fn = _fn(BATCH_SERVICE, "create_batch")
    dump = ast.dump(fn)
    assert "IntegrityError" in dump, "并发冲突没有被回读,双击会直接报 500"
    assert "begin_nested" in dump, (
        "没有保存点 —— 一次 IntegrityError 会把会话打成不可用,回读根本发不出去"
    )


def test_a_reused_request_does_not_run_the_batch_again():
    """重发**什么都不做**。往下走去执行正是这条幂等要挡的事。"""
    fn = _fn(BATCH_API, "create_batch")
    assert "reused_request" in ast.dump(fn), (
        "接口没有识别复用 —— 双击的第二次会把同一个批次再跑一遍"
    )


def test_the_key_comes_from_the_http_header():
    """它描述的是这次**传输**,不是这次请求要做的事。放进 body 会让它
    看起来像一个业务参数。
    """
    assert "Idempotency-Key" in BATCH_API.read_text(encoding="utf-8")


# ==========================================================
# 迁移链
# ==========================================================


def test_both_new_migrations_are_on_the_chain():
    """两条迁移各自带一列,漏挂一条的表现是模型有属性而库里没有那一列 ——
    在真库上是启动即炸,在这里是静默通过。
    """
    names = {p.name for p in MIGRATIONS.glob("*.py")}
    assert any(n.startswith("0031") for n in names)
    assert any(n.startswith("0032") for n in names)

    revisions = {}
    for path in MIGRATIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        rev = down = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.AnnAssign) or not isinstance(
                node.target, ast.Name
            ):
                continue
            if node.target.id == "revision" and isinstance(node.value, ast.Constant):
                rev = node.value.value
            if node.target.id == "down_revision" and isinstance(
                node.value, ast.Constant
            ):
                down = node.value.value
        if rev:
            revisions[rev] = down
    assert revisions.get("0031") == "0030"
    assert revisions.get("0032") == "0031"
    # 单一 head:没有第二条迁移也声称自己接在 0030 或 0031 后面
    children = [r for r, d in revisions.items() if d in {"0030", "0031"}]
    assert sorted(children) == ["0031", "0032"], f"迁移链分叉了:{children}"


def test_the_new_error_codes_are_distinct_from_the_old_ones():
    """新码不许和 `SUBMIT_RESULT_UNKNOWN` 撞。

    两者的恢复路径相反:那个要人去对账,这个要机器接着取结果。
    共用一个码的话,界面上的按钮会长得一模一样,而其中一个会花钱。
    """
    assert PROVIDER_RESULT_PENDING_CODE != SUBMIT_UNKNOWN_CODE
    assert PROVIDER_RESULT_PENDING_CODE not in SOURCE_ASSET_BROKEN_CODES
