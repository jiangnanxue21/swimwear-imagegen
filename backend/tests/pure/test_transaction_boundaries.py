"""事务边界与派发 Outbox(评审第 1、3 条)。

这两条是上一轮**明确搁置**的,理由是"没有真库没法验"。这个文件试图把其中
可以静态验证的部分钉死 —— 不能替代真实 PostgreSQL + Redis 上的并发测试,
但能拦住最容易发生的退化:有人为了"少一次提交"把 checkpoint 删掉。

写法上刻意全部用 AST 找**真实的调用节点**,不用 `"commit" in source`。
阶段 7 有过教训:字符串包含式的断言,把调用删掉只留 import 照样绿。
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT

APP_DIR = BACKEND_ROOT / "app"
PIPELINE = APP_DIR / "tasks" / "generation_tasks.py"


def _fn(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _pipeline_tree() -> ast.AST:
    return ast.parse(PIPELINE.read_text(encoding="utf-8"))


def _call_name(node: ast.AST) -> str | None:
    """把一个调用节点归一成可比较的名字,如 'session.commit' / '_checkpoint'。"""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        return func.attr
    return None


#: 会走出进程的调用。持有任务行锁时不许出现这些 —— 锁会跨越整个网络往返。
_EXTERNAL_CALLS = {
    "provider.submit",
    "provider.get_status",
    "provider.fetch_results",
    "_persist_candidates",
    "evaluation_service.evaluate_round",
    "output_service.build_outputs",
}

#: 能结束事务的调用。_checkpoint 与 _check_cancelled 内部都是 session.commit()。
_COMMITTING = {"session.commit", "_checkpoint", "_check_cancelled", "_claim"}


def _ordered_calls(fn: ast.FunctionDef) -> list[tuple[int, str]]:
    """按行号排出函数体里的所有调用。只用于不关心分支的断言。"""
    calls: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        name = _call_name(node)
        if name:
            calls.append((node.lineno, name))
    return sorted(calls)


def _direct_calls(stmt: ast.stmt) -> list[tuple[int, str]]:
    """只取这条语句自身的调用,**不下钻到子代码块**。

    下钻会把 if/else 两个互斥分支当成顺序执行,得出"先转移后调用"这种
    根本不存在的路径 —— 那分析的是源码顺序,不是控制流。
    """
    blocks = {"body", "orelse", "handlers", "finalbody"}
    calls: list[tuple[int, str]] = []

    def walk(node: ast.AST, top: bool) -> None:
        name = _call_name(node)
        if name:
            calls.append((node.lineno, name))
        for field, value in ast.iter_fields(node):
            if top and field in blocks:
                continue
            for item in (value if isinstance(value, list) else [value]):
                if isinstance(item, ast.AST):
                    walk(item, False)

    walk(stmt, True)
    return sorted(calls)


def _exits(stmts: list[ast.stmt]) -> bool:
    """这个代码块是否一定不会流到后面。"""
    return bool(stmts) and isinstance(
        stmts[-1], (ast.Return, ast.Raise, ast.Continue, ast.Break)
    )


def _scan_block(
    stmts: list[ast.stmt], dirty_since: int | None, problems: list[str], label: str
) -> int | None:
    """沿控制流扫一个代码块。

    ``dirty_since``:最近一次"写了任务行但还没提交"的行号,None 表示没有未提交的写。
    返回离开这个块时的状态,供后续语句继续用。
    """
    for stmt in stmts:
        for lineno, name in _direct_calls(stmt):
            if name in _COMMITTING:
                dirty_since = None
            elif name == "gs.transition":
                dirty_since = lineno
            elif name in _EXTERNAL_CALLS and dirty_since is not None:
                problems.append(
                    f"{label}: 第 {dirty_since} 行的 transition 拿到行锁后,"
                    f"第 {lineno} 行就调用了 {name},中间没有提交"
                )

        branches: list[list[ast.stmt]] = []
        for field in ("body", "orelse", "finalbody"):
            block = getattr(stmt, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                branches.append(block)
        for handler in getattr(stmt, "handlers", []) or []:
            branches.append(handler.body)

        if branches:
            outcomes = [_scan_block(b, dirty_since, problems, label) for b in branches]
            flowing = [
                out for b, out in zip(branches, outcomes, strict=True) if not _exits(b)
            ]
            # 会流到后面的分支里,只要有一个仍持锁,后续就必须当作持锁
            dirty_since = next((d for d in flowing if d is not None), None)
    return dirty_since


def test_no_external_call_happens_while_holding_the_task_row_lock():
    """#1 的核心不变量,沿控制流验证(不是按源码行号)。

    每次 ``gs.transition()`` 都是一条 UPDATE,拿到 generation_tasks 那一行的写锁,
    直到提交才放。锁没放掉就去做外部调用,锁就会跨越整个网络往返 ——
    轮询最长 300 秒、每张图评分 90 秒。后果不是"慢",是取消功能整体失效:

        worker 持锁 -> 取消接口的 UPDATE 阻塞 -> 取消事务提交不了
                    -> worker 的 refresh 永远读不到 cancel_requested
                    -> worker 继续持锁

    这是个闭环,不会自己解开。
    """
    tree = _pipeline_tree()
    problems: list[str] = []
    for fname in ("_run", "_apply_decision", "_format_outputs"):
        _scan_block(_fn(tree, fname).body, None, problems, fname)
    assert not problems, "行锁跨越了外部调用 -> " + "; ".join(problems)


def test_cancel_check_commits_before_it_reads():
    """`_check_cancelled` 必须先结束事务再 refresh。

    顺序反了就是个闭环死结:worker 持锁 -> 取消接口阻塞 -> 取消提交不了
    -> refresh 永远读不到 -> worker 继续持锁。
    """
    fn = _fn(_pipeline_tree(), "_check_cancelled")
    calls = _ordered_calls(fn)
    commits = [ln for ln, n in calls if n in _COMMITTING]
    refreshes = [ln for ln, n in calls if n == "session.refresh"]
    assert commits, "_check_cancelled 没有结束事务,读到的只会是自己事务里的旧值"
    assert refreshes, "_check_cancelled 应当 refresh 任务行"
    assert min(commits) < min(refreshes), "必须先提交再读,否则看不见别人写的取消标记"


def test_attempt_is_committed_before_money_is_spent():
    """submit 之前必须提交:attempt 是"我们发过一次请求"的唯一证据。

    只 flush 不 commit 的话,worker 在 submit 期间被 kill,这条记录随事务消失 ——
    对方可能已经受理并计费,我们这边查无此事。
    """
    calls = _ordered_calls(_fn(_pipeline_tree(), "_run"))
    submit_line = next(ln for ln, n in calls if n == "provider.submit")
    attempt_line = next(ln for ln, n in calls if n == "_new_attempt")
    commits = [ln for ln, n in calls if n in _COMMITTING]
    assert any(attempt_line < ln < submit_line for ln in commits), (
        "创建 attempt 之后、调用 submit 之前必须有一次提交"
    )


def test_pipeline_never_calls_delay_directly():
    """#3:流水线不许自己 .delay(),必须经过 Outbox。

    直接 delay 就绕开了"意图和业务数据同事务"这个保证,
    Broker 抖一下任务就永远停在 REGENERATING。
    """
    tree = _pipeline_tree()
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "delay"
    ]
    assert not offenders, f"流水线里仍有直接 .delay() 调用,行号: {offenders}"


def test_only_dispatch_service_talks_to_the_queue():
    """全仓只允许 dispatch_service 调用 .delay()。

    多一个入口就多一条绕过 Outbox 的路径,而这种绕过在日志里是看不出来的 ——
    它只会表现为"偶尔有任务莫名其妙不跑"。
    """
    allowed = {"dispatch_service.py"}
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        if "__pycache__" in str(path) or path.name in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "delay"
            ):
                offenders.append(f"{path.relative_to(APP_DIR)}:{node.lineno}")
    assert not offenders, "只有 dispatch_service 能投递队列 -> " + ", ".join(offenders)


def test_enqueue_does_not_commit():
    """Outbox 的全部意义在于"和业务数据同一个事务"。

    `enqueue()` 里一旦出现 commit,意图就被拆成了独立事务,
    等于退回到"先提交后派发",这张表也就白建了。
    """
    tree = ast.parse(
        (APP_DIR / "services" / "dispatch_service.py").read_text(encoding="utf-8")
    )
    fn = _fn(tree, "enqueue")
    commits = [n for _, n in _ordered_calls(fn) if n == "session.commit"]
    assert not commits, "enqueue() 不能提交 —— 必须由调用方连同业务数据一起提交"
    assert any(n == "session.flush" for _, n in _ordered_calls(fn)), "应当 flush 以拿到主键"


def test_every_dispatch_site_enqueues_before_it_commits():
    """四条派发路径都必须"先登记意图,再提交,最后才投递"。"""
    sites = {
        "app/api/generation_tasks.py": ("create_task", "retry_task"),
        "app/api/reviews.py": ("regenerate_review",),
    }
    problems: list[str] = []
    for rel, fnames in sites.items():
        tree = ast.parse((BACKEND_ROOT / rel).read_text(encoding="utf-8"))
        for fname in fnames:
            calls = _ordered_calls(_fn(tree, fname))
            enq = [ln for ln, n in calls if n == "dispatch_service.enqueue"]
            com = [ln for ln, n in calls if n == "session.commit"]
            deliver = [
                ln for ln, n in calls
                if n in ("dispatch_service.deliver_pending", "_dispatch")
            ]
            if not enq:
                problems.append(f"{rel}:{fname} 没有登记派发意图")
                continue
            if not com or min(enq) > min(com):
                problems.append(f"{rel}:{fname} 在提交之后才登记意图,窗口仍在")
            if deliver and min(deliver) < min(com):
                problems.append(
                    f"{rel}:{fname} 在提交之前就投递了,worker 可能读到不存在的任务"
                )
    assert not problems, "; ".join(problems)


# ---------------------------------------------------------------- 退避

def test_backoff_grows_and_is_capped():
    from app.workflows.dispatch_policy import MAX_BACKOFF_SECONDS, backoff_seconds

    assert backoff_seconds(0) == 0
    seq = [backoff_seconds(i) for i in range(1, 12)]
    assert seq == sorted(seq), "退避必须单调不减"
    assert all(s <= MAX_BACKOFF_SECONDS for s in seq), "退避必须封顶"
    assert backoff_seconds(1) < backoff_seconds(4), "应当是指数增长,不是固定间隔"
    # 封顶之后不再增长,否则 Broker 长时间不可用会把重试推到无限远
    assert backoff_seconds(50) == MAX_BACKOFF_SECONDS


def test_giving_up_is_bounded():
    """必须存在放弃上限。无限重试会让真正的告警淹没在日志里。"""
    from app.workflows.dispatch_policy import MAX_DISPATCH_ATTEMPTS

    assert 1 < MAX_DISPATCH_ATTEMPTS <= 50


# ---------------------------------------------------------------- 卡死回收

def test_every_stallable_status_can_legally_reach_failed():
    """回收器把卡死任务落成 FAILED。这条边必须在状态机里真实存在,
    否则回收会在 assert_can 上抛错,任务反而卡得更死。
    """
    from app.core.enums import TaskStatus
    from app.workflows import state_machine as sm

    for status in sm.STALLABLE_STATUSES:
        assert sm.can_transition(status, TaskStatus.FAILED), (
            f"{status} 不能转 FAILED,回收器会失败"
        )


def test_stallable_statuses_exclude_states_that_wait_on_humans_or_queues():
    """等人、等队列的状态不算卡死。

    把 MANUAL_REVIEW 算进去会把待审任务全部误杀;把 QUEUED 算进去则会在
    队列积压时误杀正常排队的任务 —— 而"消息没发出去"现在由 Outbox 负责,
    不需要靠状态去猜。
    """
    from app.core.enums import TaskStatus
    from app.workflows import state_machine as sm

    for forbidden in (
        TaskStatus.QUEUED.value,
        TaskStatus.MANUAL_REVIEW.value,
        TaskStatus.CREATED.value,
        TaskStatus.REGENERATING.value,
    ):
        assert forbidden not in sm.STALLABLE_STATUSES, f"{forbidden} 不该被当成卡死"


def test_stall_threshold_exceeds_the_longest_legal_pause():
    """回收阈值必须大于一轮里最长的合法停顿,否则会误杀正在跑的任务。

    误杀的代价很实在:任务被落成 FAILED,人去重试,又花一次 Provider 的钱。
    """
    from app.workflows.dispatch_policy import (
        LONGEST_LEGAL_PAUSE_SECONDS,
        STALL_THRESHOLD_SECONDS,
    )

    assert STALL_THRESHOLD_SECONDS > LONGEST_LEGAL_PAUSE_SECONDS, (
        f"回收阈值 {STALL_THRESHOLD_SECONDS}s 小于最长合法停顿 "
        f"{LONGEST_LEGAL_PAUSE_SECONDS}s,会误杀正常任务"
    )
    # 余量至少一倍:配置调大轮询或评分超时之后,不该立刻就顶到阈值
    assert STALL_THRESHOLD_SECONDS >= 2 * LONGEST_LEGAL_PAUSE_SECONDS


def test_reaper_requires_reconciliation_when_the_provider_may_have_charged_us():
    """已经拿到 external_task_id 的卡死任务,必须打成"结果未知"。

    打成普通失败的话,界面上一键重试 = 再买一次。这里复用评审第 8 条
    那套护栏(SUBMIT_RESULT_UNKNOWN + force),不新造一套。
    """
    from app.core.enums import SUBMIT_UNKNOWN_CODE, WORKER_STALLED_CODE, TaskStatus
    from app.workflows import state_machine as sm

    # 只有真正把请求发出去了的阶段才需要对账
    assert TaskStatus.SUBMITTING.value in sm.SPENT_STATUSES
    assert TaskStatus.PROVIDER_RUNNING.value in sm.SPENT_STATUSES
    # 还没提交的阶段不该拦着人重试
    assert TaskStatus.PREPROCESSING.value not in sm.SPENT_STATUSES
    # 出图阶段卡死不涉及新的 Provider 消费,重试走 resume_formatting
    assert TaskStatus.FORMATTING.value not in sm.SPENT_STATUSES
    assert SUBMIT_UNKNOWN_CODE != WORKER_STALLED_CODE


def test_retry_still_blocks_reaped_tasks_that_need_reconciliation():
    """回收器打的错误码必须真的能被重试接口拦住 —— 光定义常量不算数。"""
    import ast as _ast

    src = (APP_DIR / "services" / "generation_service.py").read_text(encoding="utf-8")
    fn = _fn(_ast.parse(src), "retry_task")
    guarded = any(
        isinstance(node, _ast.Attribute) and node.attr == "SUBMIT_UNKNOWN_CODE"
        or isinstance(node, _ast.Name) and node.id == "SUBMIT_UNKNOWN_CODE"
        for node in _ast.walk(fn)
    )
    assert guarded, "retry_task 必须检查 SUBMIT_UNKNOWN_CODE,否则回收后一键重试就会重复计费"


# ---------------------------------------------------------------- 取消

def test_cancelled_signal_reports_the_status_it_actually_saw():
    """被别人抢先落成终态时,如实回报那个状态。

    一律报 CANCELLED 会让 MANUALLY_REJECTED 的任务在日志里显示成"被取消",
    排查时对不上账。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_pipe_for_test", PIPELINE)
    # 直接 import 会拉起整条依赖链(sqlalchemy 等),这里只验签名与默认值
    assert spec is not None
    tree = _pipeline_tree()
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "_Cancelled"
    )
    init = _fn(cls, "__init__")
    args = [a.arg for a in init.args.args]
    assert "observed_status" in args, "_Cancelled 必须带上观察到的实际状态"


# ================================================================ HTTP 边界(§7.8)
#
# 上面那一段守的是**流水线**里的事务边界(评审第 1、3 条)。这一段守的是
# **请求**的事务边界,也就是 §7.8 那四行建议里的第一行:
#
#     API/use case 拥有业务事务
#
# 以及它对应的第一条禁止项:「请求级自动 commit 与 Service commit 混用」。
#
# ## 为什么必须是门禁,不能靠集成测试
#
# `tests/conftest.py` 的 `client` 夹具把 `db_session` 覆盖成一个**不提交**的
# session(整个用例一个事务,结束回滚)。同一个 session 里读得到未提交的写,
# 于是「真的提交了」和「只是 flush 了」在 API 测试里**完全等价**。
# 也就是说:一个写端点漏掉 commit,现有测试一条都不会红。
#
# 这不是夹具写错了 —— 用例之间要隔离,就只能这么写。代价是提交这件事
# 落在了测试的射程之外,只能从源码形状上钉。

API_DIR = APP_DIR / "api"

#: 会写库的会话方法。
_SESSION_WRITES = {"commit", "flush", "add", "add_all", "delete", "merge"}

#: HTTP 里语义上"会改东西"的方法。
_WRITE_METHODS = {"post", "put", "patch", "delete"}

#: 用了写方法、但语义上是**读**的端点。
#:
#: 白名单放行的只有"不提交"这一件事。它们**一处会话写都不许有** ——
#: 由 `test_read_only_write_method_endpoints_really_do_not_write` 反向钉住。
#: 哪天 `preview_import` 真开始写库,漏掉的提交会被那条用例当场抓住,
#: 而不是变成一个悄悄不落库的接口。
_READ_ONLY_WRITE_METHOD_ENDPOINTS = {
    ("workbench_batch", "preview_import"),
    # 阶段 4 的 §7.5「上游变化前展示失效影响」:让运营在**点下去之前**
    # 看见启用这份方案会让哪些颜色的图片集过期。
    #
    # 它是 POST 只是因为入参是整份方案(`angles` 是数组,塞不进 query
    # string)—— 与 `preview_import` 完全同一个理由。同样地,它一处会话写
    # 都不许有,由下面那条反向用例钉着。
    ("generation_plans", "preview_activation"),
}

#: 允许在 GET 里提交的端点。**目前只有一个,而且它写的不是业务状态。**
#:
#: `download_batch_file` 记的是"谁在什么时候下载了哪份文件"这条只增不改的
#: 审计流水 —— §6.3 第 6 条明确要求生成与下载分别记录事件。它不动
#: `export_count`、不动草稿状态、不动平台状态,所以与评审第 19 条
#: (读接口不许改业务状态)不冲突。
#:
#: 往这个集合里加名字之前先想清楚:浏览器会预取 GET,监控会探活 GET,
#: 运营刷新一次页面就是一次 GET。
_GET_ENDPOINTS_ALLOWED_TO_COMMIT = {
    ("workbench_batch", "download_batch_file"),
}


def _api_modules() -> dict[str, ast.Module]:
    mods: dict[str, ast.Module] = {}
    for path in sorted(API_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        mods[path.stem] = ast.parse(path.read_text(encoding="utf-8"))
    return mods


def _route_methods(fn: ast.AST) -> set[str]:
    """这个函数挂了哪些 HTTP 方法。没挂路由装饰器就是空集。"""
    methods: set[str] = set()
    for deco in getattr(fn, "decorator_list", []):
        if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
            methods.add(deco.func.attr)
    return methods & (_WRITE_METHODS | {"get"})


def _takes_session(fn: ast.AST) -> bool:
    args = list(fn.args.args) + list(fn.args.kwonlyargs)
    return any(arg.arg == "session" for arg in args)


def _session_write_calls(fn: ast.AST) -> list[tuple[int, str]]:
    """函数体里直接打在 `session` 上的写调用。

    只数直接调用,不走调用图:事务边界的**归属**就该看得见地写在端点里。
    委托给某个 service 去提交,正是 §7.8 要拆开的那种混用。
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "session"
            and func.attr in _SESSION_WRITES
        ):
            hits.append((node.lineno, func.attr))
    return sorted(hits)


def _routes() -> list[tuple[str, ast.AST, set[str]]]:
    found: list[tuple[str, ast.AST, set[str]]] = []
    for mod, tree in _api_modules().items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            methods = _route_methods(node)
            if methods:
                found.append((mod, node, methods))
    assert found, "一个路由都没解析到 —— 装饰器识别坏了,不是接口没了"
    return found


def test_the_request_scoped_dependency_does_not_commit_for_everyone():
    """`get_session()` 不许替所有请求提交。

    这一行是 §7.8 第一条禁止项本身。它在的时候,事务边界有两个主人:
    接口层写着 38 处显式 commit(发布链路还刻意分三段),依赖层又在每个
    请求结束时无条件再提交一次 —— 包括那些一行 commit 都没写的端点。

    害处不在"多提交一次"(那无害),在于:

        读路径   任何一处误写都会被提交掉。评审第 19 条把五个 GET 改成只读、
                 a38 又给 `collect()` 加了形状门禁,靠的都是"写要挡在
                 `dry_run` 后面";而这一行在下面兜着,让那些努力只差一次
                 手滑就白做
        写路径   "有没有真的落库"不再由接口负责,而测试又验不了(见本节开头)

    `rollback` 必须留着 —— 它和提交不是一回事,它保证的是"请求中途抛错
    不许留下半截写入"。所以这条用例同时正向要求它还在。
    """
    tree = ast.parse((APP_DIR / "db" / "session.py").read_text(encoding="utf-8"))
    fn = _fn(tree, "get_session")
    calls = {name for _, name in _ordered_calls(fn)}

    assert "session.commit" not in calls, (
        "`get_session()` 又开始替所有请求提交了 —— §7.8 第一条禁止项。"
        "要落库请在接口函数里自己 commit"
    )
    assert "session.rollback" in calls, (
        "`get_session()` 的回滚没了。它不是和提交一起摘掉的那一半:"
        "没有它,请求中途抛错会留下半截写入"
    )


def test_every_write_endpoint_commits_its_own_transaction():
    """POST/PUT/PATCH/DELETE 端点必须自己提交。

    摘掉请求级自动 commit 之后,漏一个 commit 的后果是**接口返回 200
    但什么都没存下来**,而且不会有任何报错 —— 没有异常、没有日志,
    响应体还照常带着新建对象的 id。

    这条用例是那件事唯一的防线(理由见本节开头:集成测试结构上验不了)。
    所以它宁可误伤:拿了 `session` 的写端点一律要求显式 commit,
    真的只读就进白名单,并接受下一条用例的反向检查。
    """
    offenders: list[str] = []
    for mod, fn, methods in _routes():
        if not (methods & _WRITE_METHODS) or not _takes_session(fn):
            continue
        if (mod, fn.name) in _READ_ONLY_WRITE_METHOD_ENDPOINTS:
            continue
        commits = [line for line, attr in _session_write_calls(fn) if attr == "commit"]
        if not commits:
            offenders.append(f"{mod}.{fn.name}({'/'.join(sorted(methods))})")
    assert not offenders, (
        "这些写端点没有自己提交,靠的是已经摘掉的请求级自动 commit -> "
        + "; ".join(sorted(offenders))
    )


def test_read_only_write_method_endpoints_really_do_not_write():
    """白名单里的"其实是读"的端点,必须真的一处会话写都没有。

    白名单本身是个危险物:它放行的是"不提交",而看的人容易读成
    "这个端点不用管"。所以这里反过来要求它们连 `add` / `flush` 都不许有 ——
    一旦某天真写了库,缺的那次提交会在这里当场变红,而不是变成一个
    悄悄不落库的接口。
    """
    checked = 0
    problems: list[str] = []
    for mod, fn, _methods in _routes():
        if (mod, fn.name) not in _READ_ONLY_WRITE_METHOD_ENDPOINTS:
            continue
        checked += 1
        writes = _session_write_calls(fn)
        if writes:
            problems.append(
                f"{mod}.{fn.name} 第 "
                + "、".join(f"{line} 行 session.{attr}()" for line, attr in writes)
            )
    assert checked == len(_READ_ONLY_WRITE_METHOD_ENDPOINTS), (
        "白名单里有名字没对上任何路由函数 —— 端点改名之后白名单会静默失效,"
        f"对上了 {checked}/{len(_READ_ONLY_WRITE_METHOD_ENDPOINTS)}"
    )
    assert not problems, (
        "白名单端点开始写库了,那它就不该在白名单里(要么补 commit,"
        "要么把写挪走)-> " + "; ".join(sorted(problems))
    )


def test_no_get_endpoint_commits_business_state():
    """GET 端点不许提交,署名豁免的那一个除外。

    浏览器会预取 GET、监控会探活 GET、运营刷新一次页面就是一次 GET。
    读接口一旦能提交,这三件事都会变成"改数据"。

    这条与 a38 的 `test_workbench_query_budget.py` 是**两个锚点**,
    不重复:那边从 `collect()` 往下数写调用点的循环深度与 `dry_run` 守卫,
    这边从 HTTP 端点往下看谁结束事务。中间那段(service 里的 flush)
    两边都不管 —— flush 不结束事务,回滚仍然撤得掉。
    """
    offenders: list[str] = []
    for mod, fn, methods in _routes():
        if "get" not in methods:
            continue
        if (mod, fn.name) in _GET_ENDPOINTS_ALLOWED_TO_COMMIT:
            continue
        commits = [line for line, attr in _session_write_calls(fn) if attr == "commit"]
        if commits:
            offenders.append(f"{mod}.{fn.name} 第 {commits} 行")
    assert not offenders, (
        "GET 端点提交了事务 —— 预取和探活会因此改数据 -> " + "; ".join(sorted(offenders))
    )


# ---------------------------------------------------------------- 兜底任务的返回形状


MAINTENANCE = APP_DIR / "tasks" / "maintenance_tasks.py"


def _dict_keys(node: ast.AST) -> frozenset[str] | None:
    """把一个 `return` 的表达式折成键集合。折不出来返回 None。"""
    if isinstance(node, ast.Dict):
        keys: set[str] = set()
        for key in node.keys:
            if key is None:  # `**other`
                return None
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                return None
            keys.add(key.value)
        return frozenset(keys)
    return None


def test_maintenance_tasks_do_not_change_their_return_shape_on_failure():
    """兜底任务的异常分支,不许自己拼一个新的返回字典。

    §7.8 最后一条禁止项是「异常被吞掉后 Celery 显示成功」。这四个任务
    **刻意**吞异常 —— 一拍失败不该让 beat 停摆 —— 所以它们靠返回里的
    `error` 位说话。既然如此,那个返回就必须始终是同一种形状。

    原来 `reap_batch_leases` 的异常分支写着 `{"scanned": 0, "requeued": 0,
    "gave_up": 0, "error": True}`,两处都错:

        少键     成功路径还有 `batches` 与三个 `redispatch_*`。读这个返回
                 做看板的一侧,恰恰在最需要它稳定的时候拿到另一种形状
        假的零   回收跑在重投**之前而且自己提交过**。重投炸掉时"这一拍
                 回收了几件"是一个已经落库的事实,写 0 是把它抹掉

    判据取"形状从哪来",而不是逐个比键名:异常分支要么复用成功路径那个
    构造器,要么就得写一个字面量 —— 而写字面量正是上面两个错误的来源,
    因为它是**手抄**的,成功路径加一个键它不会跟着变。
    """
    tree = ast.parse(MAINTENANCE.read_text(encoding="utf-8"))

    tasks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(deco, ast.Call)
            and isinstance(deco.func, ast.Attribute)
            and deco.func.attr == "task"
            for deco in node.decorator_list
        )
    ]
    assert len(tasks) >= 4, f"兜底任务只解析到 {len(tasks)} 个,装饰器识别坏了"

    problems: list[str] = []
    for task in tasks:
        for handler in [n for n in ast.walk(task) if isinstance(n, ast.ExceptHandler)]:
            for ret in [n for n in ast.walk(handler) if isinstance(n, ast.Return)]:
                if ret.value is None:
                    continue
                keys = _dict_keys(ret.value)
                if keys is None:
                    continue  # 复用了构造器或做了 `**` 展开:正是想要的写法
                success_keys = {
                    k
                    for other in ast.walk(task)
                    if isinstance(other, ast.Return)
                    and other is not ret
                    and (k := _dict_keys(other.value)) is not None
                }
                if not any(keys - {"error"} == sk for sk in success_keys):
                    problems.append(
                        f"{task.name} 第 {ret.lineno} 行的异常分支手抄了一份返回字典 "
                        f"{sorted(keys)},和成功路径对不上"
                    )
    assert not problems, "; ".join(problems)
