"""A45 batch12-4:batch12-3 回归报告点名的五条。

报告的裁决是"不能关闭 batch12-3",理由分三类:

    REG-01  Provider 返回了图但全部下载失败 -> 被读成"没有候选图" -> 自动重生
            **不需要任何人点击就会重复扣费。**
    REG-02  reaper 把已知外部 ID 的任务压成 SUBMIT_RESULT_UNKNOWN,
            而那个码的唯一出口(强制重试)会清掉 ID 重新 submit。
            **人是被系统逼着走上重复扣费那条路的。**
    REG-03  候选落库异常时 `_abandon_attempt()` 会 commit,把半截候选一起提交,
            续跑之后同一个外部结果存了两遍。
    REG-04  `create_batch` 的保存点套错位置,并发冲突返回 500 而不是回读。
    REG-05  重新投递之后界面仍然显示 STALLED,并开放普通 SUBMIT。

## 这份守卫刻意分成两半

报告对上一轮守卫的批评是准确的:1824 条全绿的同时仍然存在自动重复扣费,
因为那些断言**没有覆盖跨函数组合**。

所以能真跑的就真跑:`publish_view` 和 `state_machine` 零依赖,
REG-02 的分档、REG-05 的派生在这里是**调用真函数**得出的结论。

剩下三条落在 `generation_tasks` / `batch_service` 上,它们要 sqlalchemy,
在纯运行器里 import 不进来 —— 这里只能用 AST 钉住结构。真正的行为验证在
`tests/test_a45_batch12_4_recovery_db.py`,那一批需要真实 PostgreSQL,
跑的是报告要求的"reaper -> retry_task -> worker"整条链路。

**AST 断言的定位是"结构塌了要红",不是"行为对了"。** 两者都需要,
而上一轮的问题正是只有前者。
"""
from __future__ import annotations

import ast

from app.core.enums import (
    PROVIDER_RESULT_PENDING_CODE,
    PUBLISH_OUTBOX_TERMINAL_STATES,
    PublishAttemptStatus,
    PublishOutboxStatus,
    PublishStatus,
    TaskStatus,
)
from app.workflows import publish_view as pv
from app.workflows.state_machine import (
    AWAITING_PROVIDER_RESULT_STATUSES,
    SPENT_STATUSES,
    SUBMIT_IN_DOUBT_STATUSES,
    TRANSITIONS,
    can_transition,
)
from tests.pure._helpers import BACKEND_ROOT

GENERATION_TASKS = BACKEND_ROOT / "app" / "tasks" / "generation_tasks.py"
GENERATION_SERVICE = BACKEND_ROOT / "app" / "services" / "generation_service.py"
BATCH_SERVICE = BACKEND_ROOT / "app" / "workbench" / "batch_service.py"
MIGRATIONS = BACKEND_ROOT / "migrations" / "versions"


def _fn(path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到 {name} —— 改名了就把这份守卫一起改")


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
            if isinstance(func.value, ast.Name):
                names.add(f"{func.value.id}.{func.attr}")
    return names


# ==========================================================
# REG-01:"没有候选图"和"候选全部下载失败"必须分开
# ==========================================================


def test_persist_reports_the_provider_count_separately_from_what_it_stored():
    """判据只能是 Provider 返回了几个,不能是我们成功存下几个。

    `_persist_candidates` 原来只返回一个 `stored` 列表,于是这两件事在
    调用方眼里长得一模一样:

        Provider 一张都没返回     stored == []
        返回了三张、全下载失败     stored == []

    而它们的下一步相反 —— 前者该按轮次规则重生(再花一次钱),后者该保留
    external_task_id 重新取结果(不再花钱)。只有一个数的话,调用方**没有
    任何办法**分辨,于是全部下载失败会静静地走进 `_empty_round()`。
    """
    fn = _fn(GENERATION_TASKS, "_persist_candidates")
    returns = fn.returns
    assert returns is not None and getattr(returns, "id", None) == "_PersistOutcome", (
        "`_persist_candidates` 又开始返回一个裸列表了 —— "
        "调用方就分不出「没有候选」和「全部下载失败」"
    )

    # 三个数必须都在,少一个就有一种情况分不出来
    outcome = next(
        node
        for node in ast.walk(ast.parse(GENERATION_TASKS.read_text(encoding="utf-8")))
        if isinstance(node, ast.ClassDef) and node.name == "_PersistOutcome"
    )
    fields = {
        node.target.id
        for node in outcome.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert {"provider_count", "stored", "download_failed"} <= fields, (
        f"`_PersistOutcome` 少了字段:{sorted(fields)}"
    )


def test_an_all_failed_download_round_does_not_reach_empty_round():
    """`_empty_round()` 会转 REGENERATING 并**自动排下一轮生成**。

    这是整条链路上唯一一处不需要人点击就会再花一次钱的地方,所以进它的
    条件必须严格到只剩一种情况:Provider 真的一个候选都没返回。

    这条断言钉的是控制流顺序:全部下载失败那一支必须**先 return**,
    `_empty_round()` 只能在它后面。反过来写的话(先判 `not stored`),
    全部下载失败会被它先接走 —— 那正是 REG-01。
    """
    fn = _fn(GENERATION_TASKS, "_await_and_collect")
    src = ast.unparse(fn)
    assert "all_downloads_failed" in src, (
        "`_await_and_collect` 里没有区分「全部下载失败」的判据了"
    )

    # 定义它的那一句必须读 provider_count —— 读别的都分不出这两种情况
    assigns = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "all_downloads_failed"
            for t in node.targets
        )
    ]
    assert assigns, "找不到 all_downloads_failed 的定义"
    assert "provider_count" in ast.unparse(assigns[0].value), (
        "全部下载失败的判据没有用 Provider 返回数 —— 换成别的判据就分不出"
        "「没有候选图」,而那两条路一条花钱一条不花"
    )

    empty_round_lines = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_empty_round"
    ]
    assert empty_round_lines, "找不到 _empty_round 调用,这份守卫的假设过期了"

    pending_returns = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Return)
        and "PROVIDER_RESULT_PENDING_CODE" in ast.unparse(node)
    ]
    assert any(line < min(empty_round_lines) for line in pending_returns), (
        "全部下载失败没有在 `_empty_round()` **之前**返回 —— "
        "它会掉进重生分支,自动买第二次生成"
    )


def _traced(fn: ast.FunctionDef, value: ast.AST) -> str:
    """把一个表达式连同**它引用的局部变量的赋值**一起摊平成文本。

    ## 为什么要摊平

    这条守卫原来直接对 `billable_units=` 的实参做子串判断,于是它钉住的是
    "那一处写没写 `provider_count` 这几个字",而不是"账是按什么记的"。
    A45-batch14-18 把那个实参换成一个上一行算好的局部变量之后它就红了 ——
    **而口径其实更严了**(厂商报了真数就用真数)。

    这是这个仓库第四次撞上同一个形状(前三次:14-16 的迁移条数、14-4 的
    `import { useUrlSeed }`、14-10 的 `"setAudience" in page`),一般化写在
    `docs/DECISIONS.md` §3.31:**写守卫前先问「如果有人用另一种更好的做法
    达成同一件事,这条会不会红」。**

    摊平只跟一层、且只跟函数内的赋值:够用,而且跟得越深越容易把一段无关
    代码拽进窗口 —— 那会让下面的反向断言变松,方向比漏判更糟。
    """
    text = ast.unparse(value)
    names = {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
    for node in ast.walk(fn):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        assigned = {
            t.id for t in ast.walk(ast.Tuple(elts=list(targets), ctx=ast.Load()))
            if isinstance(t, ast.Name)
        }
        if assigned & names and node.value is not None:
            text += "\n" + ast.unparse(node.value)
    return text


def test_the_ledger_bills_what_the_provider_produced_not_what_we_saved():
    """账按 Provider 说了算,不按我们存下的张数。

    `record_usage` 的默认 `billable_units` 是 `max(candidate_count, 1)`,
    而调用方传的 `candidate_count` 是**成功落库的张数**。于是三张里坏一张
    会被记成两张的账,全部下载失败会被记成一张 —— 钱却是按三张收的。

    报告里"内部费用台账可能只准确记录正常完成的调用"说的就是这个。

    ## 两条合法的记法,不是一条

    A45-batch14-18(任务 9)之后,"按 Provider 说了算"有两条路,而**厂商自述
    优先**:

        厂商报了   `settle_billable_units()` 拿 `x-fashn-credits-used` 的真数
        厂商没报   退回 `provider_count`(它返回了几张图),并标 `inferred`

    所以这条断言取两者的并,不点名任何一种做法 —— 真正不许出现的是第三种:
    拿**我们存下了几张**去记账。那一条写成反向断言。
    """
    fn = _fn(GENERATION_TASKS, "_await_and_collect")
    usage_calls = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and "record_usage" in ast.unparse(node.func)
    ]
    persisted = [
        call
        for call in usage_calls
        if any(kw.arg == "billable_units" for kw in call.keywords)
    ]
    assert persisted, (
        "落库之后那条用量流水没有显式传 billable_units —— "
        "它会退回默认值,按我们存下的张数记账"
    )
    traced = [
        _traced(fn, kw.value)
        for call in persisted
        for kw in call.keywords
        if kw.arg == "billable_units"
    ]
    assert any(
        "provider_count" in text or "settle_billable_units" in text for text in traced
    ), (
        "billable_units 既不是从 Provider 返回数算出来的,也没有走厂商自述 —— "
        "两条路都断了就只剩\"按我们存下几张记账\""
    )
    # 反向:摊平之后的文本**非空**(上面 `assert persisted` 已保证),
    # 所以这一条不会平凡为真。`stored` 是落库成功的那批,拿它记账正是
    # REG-01 报的那件事
    for text in traced:
        assert "stored" not in text, (
            f"billable_units 追到了 `stored`(落库成功的张数):\n{text}\n"
            "三张里坏一张会记成两张的账,而钱是按三张收的"
        )


# ==========================================================
# REG-02:reaper 按状态 + 外部 ID 分档
# ==========================================================


def test_the_spent_statuses_split_into_two_disjoint_buckets():
    """一个状态只能属于一档,而两档并起来必须盖满原清单。

    重叠的话同一行会被两批 UPDATE 各认领一次;漏掉的话那个状态会掉进
    `WORKER_STALLED`,而那个码的含义是"钱没花出去,直接重试即可"。
    两个方向都通向重复扣费。
    """
    assert not (SUBMIT_IN_DOUBT_STATUSES & AWAITING_PROVIDER_RESULT_STATUSES)
    assert SUBMIT_IN_DOUBT_STATUSES | AWAITING_PROVIDER_RESULT_STATUSES == SPENT_STATUSES


def test_awaiting_result_means_submit_already_returned():
    """"已经确定受理过"这一档,成员必须真的在 submit 之后。

    判据是状态机:`SUBMITTING` 的出边里有 `PROVIDER_RUNNING`,也就是说
    进了 `PROVIDER_RUNNING` / `DOWNLOADING` 一定意味着 `provider.submit()`
    成功返回过。放一个 submit 之前的状态进这一档,它会被判成"可安全续取",
    而那条路根本没有 external_task_id 可用。
    """
    assert AWAITING_PROVIDER_RESULT_STATUSES == {
        TaskStatus.PROVIDER_RUNNING.value,
        TaskStatus.DOWNLOADING.value,
    }
    assert SUBMIT_IN_DOUBT_STATUSES == {TaskStatus.SUBMITTING.value}
    assert can_transition(TaskStatus.SUBMITTING, TaskStatus.PROVIDER_RUNNING)
    # 续跑那条边必须还在,否则 PROVIDER_RESULT_PENDING 无处可去
    assert TaskStatus.PROVIDER_RUNNING in TRANSITIONS[TaskStatus.FAILED]


def test_the_reaper_gives_a_resumable_code_to_tasks_that_still_have_their_id():
    """REG-02 的核心:有 ID 的等结果任务不许落 `SUBMIT_RESULT_UNKNOWN`。

    那个码在 `retry_task()` 里的唯一出口是强制重试,而强制重试对它走的是
    通用分支:清 `external_task_id`、回 `QUEUED`、重新 `submit()`。
    外部那笔生成还在跑,我们主动把唯一能取回它的凭证删掉了。
    """
    reap = _fn(GENERATION_SERVICE, "reap_stalled")
    batches = [
        node
        for node in ast.walk(reap)
        if isinstance(node, ast.Call) and "_reap_batch" in ast.unparse(node.func)
    ]
    assert len(batches) >= 4, (
        f"回收只分了 {len(batches)} 批 —— 两个维度(状态 × 有没有 ID)至少要四批"
    )

    def kw(call: ast.Call, name: str) -> str:
        for item in call.keywords:
            if item.arg == name:
                return ast.unparse(item.value)
        return ""

    resumable = [
        call
        for call in batches
        if "AWAITING_PROVIDER_RESULT_STATUSES" in kw(call, "statuses")
        and kw(call, "external_id") == "True"
    ]
    assert len(resumable) == 1, (
        "找不到「等结果 + 有外部 ID」那一批 —— 它正是 REG-02 要救的那一批"
    )
    assert "PROVIDER_RESULT_PENDING_CODE" in kw(resumable[0], "error_code"), (
        "有外部 ID 的等结果任务仍然落 SUBMIT_RESULT_UNKNOWN —— "
        "强制重试会清掉 ID 重新 submit,同一轮生成买第二次"
    )

    # 反过来:没有 ID 的仍然要走对账,那一批是真的查无此 ID
    blind = [
        call
        for call in batches
        if "AWAITING_PROVIDER_RESULT_STATUSES" in kw(call, "statuses")
        and kw(call, "external_id") == "False"
    ]
    assert len(blind) == 1 and "SUBMIT_UNKNOWN_CODE" in kw(blind[0], "error_code"), (
        "停在等结果却没有外部 ID 的那一批被判成可续跑了 —— 它没有 ID 可续"
    )


def test_the_resume_gate_still_only_accepts_a_task_that_has_its_id():
    """闸门本身不许放宽:码对了但 ID 没了,续跑无从谈起。

    reaper 现在会主动写这个码,进来的量比以前大 —— 闸门要是只看码,
    一条"码对、ID 空"的任务会被转进 `PROVIDER_RUNNING`,而 `_run` 在那里
    找不到 ID 只能再落一次 FAILED。任务在两个状态之间来回,永远出不去。
    """
    fn = _fn(GENERATION_SERVICE, "can_resume_provider_results")
    src = ast.unparse(fn)
    assert "external_task_id" in src, "续跑闸门不再检查 external_task_id 了"
    doc = ast.get_docstring(fn) or ""
    assert "reap_stalled" in doc, (
        "闸门的 docstring 没有提 reaper 这条进入路径 —— "
        "下一个人看到 reaper 写这个码会以为是误用"
    )


# ==========================================================
# REG-03:候选落库不许提交半截数据
# ==========================================================


def test_persisting_candidates_happens_inside_a_savepoint():
    """`_abandon_attempt()` 内部会 `session.commit()`,而那是会话级的。

    上一轮这里的注释写着「这次的 `session.add()` 全部随外层 rollback 掉了」——
    那句话在 `_abandon_attempt()` 存在的前提下不成立:已经 flush 过的候选行
    会被它一起提交。库里于是留下"任务失败、候选存了一半"。

    保存点把这一整段变成可回滚的单元。
    """
    fn = _fn(GENERATION_TASKS, "_await_and_collect")
    nested = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and "begin_nested" in ast.unparse(node.func)
    ]
    assert nested, "候选落库没有套保存点 —— 异常时会提交半截候选"

    # 保存点必须真的包住 `_persist_candidates`,而不是套在别处
    guarded = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Try)
        and "_persist_candidates" in ast.unparse(node.body)
        and "savepoint" in ast.unparse(node)
    ]
    assert guarded, "保存点没有包住 `_persist_candidates` 那一段"
    assert all(
        "_rollback_savepoint" in ast.unparse(handler)
        for try_node in guarded
        for handler in try_node.handlers
    ), "有异常分支没有回滚保存点 —— 那一支仍然会把半截候选交出去"


def test_abandoning_an_attempt_drops_rows_it_was_never_meant_to_commit():
    """第二道防线:收尾函数自己拒绝提交遗留的业务行。

    保存点在调用方,漏一条路径就失效。而这条函数是所有异常路径的**汇合点**,
    在这里拦一次的成本是几行代码,漏掉的成本是一批事后无法和正常数据区分的
    半截记录。
    """
    fn = _fn(GENERATION_TASKS, "_abandon_attempt")
    assert "_drop_stray_pending_rows" in _calls(fn), (
        "`_abandon_attempt` 没有清理遗留待写行就直接 commit 了"
    )
    dropper = _fn(GENERATION_TASKS, "_drop_stray_pending_rows")
    src = ast.unparse(dropper)
    assert "session.new" in src, "没有检查 `session.new`,漏掉的正是待 INSERT 的新行"
    assert "expunge" in src, "发现了遗留行却没有摘掉它"


def test_persisting_is_idempotent_across_a_resume():
    """同一个 attempt 会被落库两遍,而且这是**设计内**的。

    两条续跑路径(全部下载失败、reaper 回收 DOWNLOADING)都会重新
    `fetch_results()` 再走一次落库。无条件 `add()` 的话,每续跑一次就多一整套
    候选行 —— 候选数、排序、评分输入全部失真,而且没有任何报错。
    """
    fn = _fn(GENERATION_TASKS, "_persist_candidates")
    src = ast.unparse(fn)
    assert "existing" in src and "candidate_index" in src, (
        "落库没有按 candidate_index 认领已有行 —— 续跑一次多一套候选"
    )
    adds = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and "session.add" in ast.unparse(node.func)
    ]
    assert adds, "找不到 session.add,这份守卫的假设过期了"
    # 每一处 add 都必须在"这一行还不存在"的分支里
    assert all(
        any(
            isinstance(parent, ast.If) and "is None" in ast.unparse(parent.test)
            for parent in ast.walk(fn)
            if isinstance(parent, ast.If) and add.lineno in _line_span(parent)
        )
        for add in adds
    ), "有一处 session.add 不在「这一行还不存在」的分支里,续跑会插重复行"


def _line_span(node: ast.AST) -> range:
    return range(getattr(node, "lineno", 0), getattr(node, "end_lineno", 0) + 1)


def test_the_dedup_key_exists_in_the_schema_too():
    """应用层认领 + 数据库唯一约束,两层都要。

    只有应用层的话,下一次有人写新的落库路径、忘了认领那一步,后果是一批
    看不出来的重复候选;有约束的话,后果是一次 IntegrityError —— 后者能被
    测试发现,前者不能。
    """
    names = {p.name for p in MIGRATIONS.glob("*.py")}
    assert any(n.startswith("0033") for n in names), "0033 迁移不在链上"
    text = next(MIGRATIONS.glob("0033*.py")).read_text(encoding="utf-8")
    assert "generation_candidates" in text
    for column in ("attempt_id", "round_number", "candidate_index"):
        assert column in text, f"唯一键里少了 {column}"
    # 存量重复不许被脚本静默删掉:那些行上可能挂着 evaluations(CASCADE)
    assert "raise" in text, (
        "迁移碰到存量重复时没有报错 —— 要么静默跳过(约束没加上却没人知道),"
        "要么静默删除(连带删掉判断哪条有效的依据)"
    )


# ==========================================================
# REG-04:create_batch 的保存点必须包住 add
# ==========================================================


def test_the_insert_is_inside_the_savepoint_not_before_it():
    """`begin_nested()` 会**先 flush 当前 pending 对象,再发 SAVEPOINT**。

    于是 `session.add(job)` 放在保存点外面时,唯一键冲突在 `begin_nested()`
    这一句就抛出来了 —— 而它在 `try` 之外,`except IntegrityError` 盖不住。
    第二个并发请求收到 500,而不是回读到第一个批次。

    数据库那条唯一约束仍然拦住了第二条 BatchJob,所以这不是重复建批次;
    但"双击之后一个 500"和 EX-06 想要的"双击之后两次都拿到同一个批次"
    差着一整条用户路径:运营看到 500 会再点一次。
    """
    fn = _fn(BATCH_SERVICE, "create_batch")
    withs = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.With) and "begin_nested" in ast.unparse(node.items)
    ]
    assert withs, "create_batch 里找不到保存点了"
    assert any("session.add" in ast.unparse(node.body) for node in withs), (
        "`session.add(job)` 在保存点外面 —— `begin_nested()` 的预 flush 会先撞上"
        "唯一约束,而那一句不在 try 里,冲突直接变成 500"
    )

    # 保存点之外不许再有一处提前 add:留着的话预 flush 照样会撞
    tries = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Try) and "begin_nested" in ast.unparse(node)
    ]
    assert tries, "保存点没有配 try/except,冲突无处捕获"
    assert any(
        any(
            isinstance(h.type, ast.Name) and h.type.id == "IntegrityError"
            for h in node.handlers
        )
        for node in tries
    ), "保存点外层不再捕获 IntegrityError,并发冲突会冒到接口层"


# ==========================================================
# REG-05:活跃 Outbox 优先于历史失败 attempt
# ==========================================================


def test_a_pending_outbox_outranks_a_terminal_attempt():
    """**调用真函数**。`redeliver_dead()` 之后的四行事实原样喂进去。"""
    view = pv.build_view(
        pv.PublishFacts(
            listing_status=PublishStatus.SUBMITTING.value,
            draft_submittable=True,
            attempt_status=PublishAttemptStatus.FAILED.value,
            outbox_status=PublishOutboxStatus.PENDING.value,
        )
    )
    assert view.display_status is pv.PublishDisplayStatus.QUEUED
    assert view.next_action is pv.NextActionCode.WAIT
    assert pv.ActionCode.SUBMIT not in view.allowed_actions, (
        "重新投递之后还开着普通提交 —— 那条路会算出一把新幂等键,"
        "而那才是真的会在平台上多出一件商品的操作"
    )


def test_a_dead_outbox_still_reads_as_stalled():
    """修 REG-05 不能顺手把本模块存在的理由修掉。

    `STALLED` 是这个枚举里唯一一个状态机中不存在的取值,它的全部意义就是
    "listing 说在途、而 outbox 说不会再投了"。
    """
    view = pv.build_view(
        pv.PublishFacts(
            listing_status=PublishStatus.SUBMITTING.value,
            draft_submittable=True,
            attempt_status=PublishAttemptStatus.IN_FLIGHT.value,
            outbox_status=PublishOutboxStatus.DEAD.value,
        )
    )
    assert view.display_status is pv.PublishDisplayStatus.STALLED
    assert pv.ActionCode.REDELIVER in view.allowed_actions


def test_active_and_terminal_outbox_states_do_not_overlap():
    """"还会自动往前走"和"调度器不再看它"必须是互斥的两档。

    重叠的话 `_stalled()` 的第一条判断会把一个终态 outbox 读成活跃的,
    于是一条真的死掉的链路显示成"已排队",运营会一直等。
    """
    assert not (pv._ACTIVE_OUTBOX & PUBLISH_OUTBOX_TERMINAL_STATES)
    assert pv._ACTIVE_OUTBOX | PUBLISH_OUTBOX_TERMINAL_STATES == {
        s.value for s in PublishOutboxStatus
    }, "有 outbox 状态两档都不属于 —— 它在 `_stalled()` 里会掉进 attempt 那条分支"


def test_the_pending_code_is_still_distinct_from_submit_unknown():
    """两个码的恢复路径相反,合并就是 REG-02 复发。"""
    assert PROVIDER_RESULT_PENDING_CODE != "SUBMIT_RESULT_UNKNOWN"


# ==========================================================
# 本轮复核(batch12-4 自审):三条
# ==========================================================


def test_a_successful_attempt_clears_the_previous_rounds_failure_columns():
    """同一条 attempt 被跑第二遍时,上一遍的失败字段必须被清掉。

    REG-01 / REG-02 / REG-03 各开了一条"同一条 attempt 再走一遍"的续跑路径,
    而 `_finish_attempt` 是按"一条 attempt 只收尾一次"写的:它只在
    `error is not None` 时写那三列,成功时不碰。于是续跑成功之后库里是

        status              SUCCEEDED
        error_code          PROVIDER_RESULT_PENDING
        error_message       ……全部下载失败
        regeneration_reason DOWNLOAD_FAILED

    三列都经 `AttemptOut` 原样回到界面。这一轮修复的全部目的就是让运营能
    相信界面上那句话,而这条记录说的是两件互相矛盾的事。
    """
    fn = _fn(GENERATION_TASKS, "_finish_attempt")
    src = ast.unparse(fn)
    assert "AttemptStatus.SUCCEEDED" in src, (
        "`_finish_attempt` 里没有成功分支了 —— 续跑成功会留着上一遍的失败字段"
    )
    for column in ("error_code", "error_message", "regeneration_reason"):
        assert f"attempt.{column} = None" in src, (
            f"`_finish_attempt` 成功时没有清 `{column}`"
        )

    # 清空必须发生在 `if error is not None` 之前:反过来的话失败路径刚写下的
    # 三列会被紧接着抹掉,一次失败就彻底没有原因了
    body = [ast.unparse(node) for node in fn.body]
    clears = next(i for i, line in enumerate(body) if "attempt.error_code = None" in line)
    writes = next(i for i, line in enumerate(body) if line.startswith("if error is not None"))
    assert clears < writes, (
        "清空写在了失败赋值后面 —— 那会把失败原因一起抹掉"
    )


def test_a_shrinking_result_set_does_not_leave_orphan_rows():
    """续跑拿回来的候选变少时,尾巴上那几行要被清掉。

    `_persist_candidates` 按 `candidate_index` 认领已有行,但只遍历**这一遍**
    的候选。上一遍 3 行、这一遍 2 个的话,index 2 那行既不改写也不进 `stored`,
    以 `DOWNLOAD_FAILED` 的样子永远留在库里 —— 界面上是一张查不出所以然的
    失败候选。分批提交的 provider 在部分子任务失效时就是这个形状。
    """
    fn = _fn(GENERATION_TASKS, "_persist_candidates")
    assert "_drop_shrunk_tail" in _calls(fn), (
        "`_persist_candidates` 不再清理变短的尾巴了"
    )

    trim = _fn(GENERATION_TASKS, "_drop_shrunk_tail")
    src = ast.unparse(trim)
    assert "CandidateStatus.DOWNLOADED" in src, (
        "清尾巴时不再区分已下载的行 —— `evaluations` 是 ON DELETE CASCADE,"
        "删一张真图会连带删掉判断它有效与否的依据"
    )
    assert "session.delete" in src


def test_the_recovery_db_tests_inject_the_crash_outside_the_per_image_try():
    """REG-03 的真库用例必须打在影子写上,不能打在 `_load_bytes` 上。

    `_load_bytes` 的调用点在 `_persist_candidates` 每张图那个
    `try / except Exception` **里面**:在那里抛异常会被就地吞掉、把该行标成
    `DOWNLOAD_FAILED`,根本冒不出函数,保存点一次都不会回滚。那样测到的是
    "单张下载失败不拖垮整轮",不是 REG-03 要的"崩在候选已经 flush 之后"。

    这条守卫的意义是:那两条用例要等有 PostgreSQL 的环境才跑得了,
    而一个打错位置的注入点在那之前**没有任何征兆** —— 它会在真库上红,
    红的却是用例自己的前提,不是被测代码。
    """
    path = BACKEND_ROOT / "tests" / "test_a45_batch12_4_recovery_db.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in (
        "test_a_crash_mid_persist_leaves_zero_orphaned_candidates",
        "test_resuming_after_a_persist_crash_does_not_duplicate_candidates",
    ):
        fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        # 跳过 docstring:那两条用例的说明里**特意**点了 `_load_bytes` 的名字,
        # 讲的正是为什么不能打在那儿。查的是代码,不是解释
        body = fn.body[1:] if ast.get_docstring(fn) else fn.body
        src = "\n".join(ast.unparse(node) for node in body)
        assert "shadow_from_candidate" in src, f"{name} 没有打在影子写上"
        assert "_load_bytes" not in src, (
            f"{name} 又打回 `_load_bytes` 了 —— 那个异常会被逐张 try 吞掉,"
            "保存点不会回滚,这条用例测不到 REG-03"
        )
