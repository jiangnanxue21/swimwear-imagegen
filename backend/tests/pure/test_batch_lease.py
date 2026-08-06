"""批次条目租约与异常恢复(任务 17 / 18)。

分两部分,分界线就是"能不能不装 sqlalchemy 跑":

    判定    `batch.py` 里的租约常量与 `reclaim_verdict()` —— 真的调用、真的断言
    接线    `batch_service.py` 的领取 / 回收 / 结算 —— 只能 AST 静态钉

第二部分为什么值得写:这一整条链路**只在出事时才走到**(worker 猝死、
消息丢了)。正常跑一百遍批次,领取里有没有 `skip_locked` 看不出任何差别;
差别只在两个 worker 撞上的那一次出现,而那一次没有人在看屏幕。
所以它必须被静态钉住 —— 行为测试要真库,真库测试在本机是 skip 的。
"""
from __future__ import annotations

import ast

from app.workbench import batch as b
from app.workbench.batch import BatchErrorCode, BatchItemStatus
from tests.pure._helpers import BACKEND_ROOT

SERVICE = BACKEND_ROOT / "app" / "workbench" / "batch_service.py"
MODEL = BACKEND_ROOT / "app" / "models" / "batch_job.py"
CELERY = BACKEND_ROOT / "app" / "tasks" / "celery_app.py"
MAINTENANCE = BACKEND_ROOT / "app" / "tasks" / "maintenance_tasks.py"


# ---------------------------------------------------------------- 判定


def test_the_lease_outlives_the_whole_claim_not_just_one_item():
    """回收一件**仍在跑**的条目 = 同一件事第二次调用付费模型。

    回执表挡不住这个:回执是调用之后才写的。所以租约本身是唯一防线,
    模块里那条 assert 是它的看门人 —— 这条测试守的是那条 assert 还在。

    **必须乘 `CLAIM_CHUNK`。** 这条用例原来写的是
    `ITEM_LEASE_SECONDS > LONGEST_LEGAL_ITEM_SECONDS`(单件口径),
    它在 `CLAIM_CHUNK = 10` 时**是绿的**,而那时真实情况是
    1800 秒的租约要盖住 10800 秒的活 —— 第 3 件开跑租约就过期了。
    一次领取共用一个 `lease_until`(`claim_items` 算一次 deadline 盖给整批)
    且执行期间不续期,所以能守住的口径只有整批那一个。
    """
    assert b.ITEM_LEASE_SECONDS > b.CLAIM_CHUNK * b.LONGEST_LEGAL_ITEM_SECONDS


def test_the_claim_size_lives_where_the_invariant_can_see_it():
    """`CLAIM_CHUNK` 必须和那条 assert 在同一个模块里。

    它原来在 `batch_service.py`,而 assert 在这边 —— 于是 assert 看不见它,
    只能退化成单件断言。**这不是风格问题,那正是上面那个缺陷活下来的机制。**
    服务层现在只是转出来,两个名字必须是同一个值;哪天有人在服务层重新写一个
    字面量,这条会红。
    """
    from app.workbench import batch_service as svc

    assert svc.CLAIM_CHUNK == b.CLAIM_CHUNK
    service_src = SERVICE.read_text(encoding="utf-8")
    assert "CLAIM_CHUNK = rules.CLAIM_CHUNK" in service_src, (
        "服务层必须转引判定模块的值,不许自己写一个字面量"
    )


def test_bumping_the_claim_size_without_renewal_is_refused_at_import():
    """把领取批量调大而不加续租,应该**当场炸在导入期**,不是跑起来才出事。

    这条用例把那个不变量重新算一遍:它守的是"乘法真的在判据里",
    而不只是"守卫这一行还在"。乘号被人删掉之后上面那条仍然绿
    (1 × N == N),这条会红。

    ## 这条断言自己改过一次(R1-23)

    原来它要求守卫**必须写成模块级 `assert`** —— 而模块级 assert 在
    `python -O` 下被整条剥离,也就是在最可能开 -O 的生产里根本不存在。
    于是这条门禁在钉住一个**只在开发机上生效**的守卫,而且钉得很紧:
    想改成 `if ... raise` 的人会先看到它红,然后大概率把守卫改回去。

    现在要求的是 `if <判据>: raise` 这个形状,并且**明确禁止 assert**。
    """
    src = (BACKEND_ROOT / "app" / "workbench" / "batch.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    stripped = [
        node
        for node in tree.body
        if isinstance(node, ast.Assert) and "ITEM_LEASE_SECONDS" in ast.dump(node.test)
    ]
    assert not stripped, (
        "租约不变量又写成模块级 assert 了 —— `python -O` 会把它整条删掉,"
        "而那正是生产(R1-23)"
    )

    guards = [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and "CLAIM_CHUNK" in ast.dump(node.test)
        and "ITEM_LEASE_SECONDS" in ast.dump(node.test)
        and any(isinstance(n, ast.Raise) for n in ast.walk(node))
    ]
    assert guards, (
        "租约不变量必须是模块级的 `if ...: raise`,且判据必须同时含 CLAIM_CHUNK"
    )
    assert any(
        isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mult)
        for g in guards
        for n in ast.walk(g.test)
    ), "不变量里必须有乘法 —— 少了它就退回单件口径,守不住整批共用的那个租约"


def test_a_missing_lease_counts_as_expired():
    """0025 之前的 RUNNING 行没有租约列。把 NULL 当成"永不过期"的话,
    那批残骸会被这次迁移永久封存 —— 而它们正是本轮要修的东西。"""
    assert b.lease_expired(lease_until=None, now=0.0) is True


def test_a_live_lease_is_not_expired():
    assert b.lease_expired(lease_until=100.0, now=99.0) is False
    # 边界:到期时刻本身算过期(与 SQL 里的 `<=` 一致,两处必须同向)
    assert b.lease_expired(lease_until=100.0, now=100.0) is True


def test_the_first_few_expiries_go_back_to_the_queue():
    verdict = b.reclaim_verdict(attempts=1)
    assert verdict.action is b.ReclaimAction.REQUEUE
    assert verdict.reason


def test_automatic_requeue_is_capped():
    """一件能打死 worker 的条目会被无限回收,每一轮都可能是一次付费调用,
    而没有人在看。上限把"预算悄悄见底"换成"界面上一条可解释的失败"。"""
    verdict = b.reclaim_verdict(attempts=b.MAX_ITEM_ATTEMPTS)
    assert verdict.action is b.ReclaimAction.GIVE_UP
    # 超过上限同样是放弃,不是绕回去
    assert (
        b.reclaim_verdict(attempts=b.MAX_ITEM_ATTEMPTS + 5).action
        is b.ReclaimAction.GIVE_UP
    )


def test_giving_up_still_lands_on_a_retryable_code():
    """放弃的是**自动**重排,不是这件事本身。写成不可重试的话,
    条目连重试按钮都拿不到 —— 而这一类恰恰是重试就能好的。"""
    meta = b.error_meta(BatchErrorCode.WORKER_LOST)
    assert meta.retryable is True
    assert meta.needs_human is False
    assert b.status_for(BatchErrorCode.WORKER_LOST) is BatchItemStatus.FAILED


def test_worker_lost_is_an_explainable_failure():
    """§6.3.1 的签字门槛是"不可解释失败数为 0"。worker 崩了是一个
    已经被理解的失败模式,不该把那个计数器顶起来。"""
    assert b.is_explainable(BatchErrorCode.WORKER_LOST) is True


def test_a_reaped_item_becomes_retryable_from_the_ui():
    """本轮修的那个缺陷,用 `retryable_rows` 复述一遍。

    回收之前:条目停在 RUNNING,而 `retryable_rows` 要求 status 必须是
    FAILED —— 于是界面提示"点重试",重试接口返回 409。
    回收之后:落 FAILED + WORKER_LOST,重试按钮真的能按。
    """
    stuck = {"id": "i-1", "status": BatchItemStatus.RUNNING.value, "error_code": None}
    assert b.retryable_rows([stuck]) == []

    reaped = {
        "id": "i-1",
        "status": BatchItemStatus.FAILED.value,
        "error_code": BatchErrorCode.WORKER_LOST.value,
    }
    assert b.retryable_rows([reaped]) == ["i-1"]


def test_a_requeued_item_keeps_the_batch_out_of_a_settled_state():
    """回收把一件放回 PENDING,批次就不再是"已完成" —— 结算必须可撤销。"""
    counts = b.count_rows(
        [
            {"id": "a", "status": BatchItemStatus.SUCCEEDED.value},
            {"id": "b", "status": BatchItemStatus.PENDING.value},
        ]
    )
    assert b.job_status(counts) is b.BatchJobStatus.RUNNING


# ---------------------------------------------------------------- 接线(AST)


def _tree(path=SERVICE) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _fn(name: str, path=SERVICE) -> ast.FunctionDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到函数 {name}")


def _call_names(node: ast.AST) -> list[str]:
    out: list[str] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            out.append(func.id)
        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                out.append(f"{func.value.id}.{func.attr}")
            else:
                out.append(func.attr)
    return out


def test_claiming_uses_skip_locked():
    """没有它,两个 worker 捞到同一批 PENDING、同时置 RUNNING、同时开跑。
    最后靠 advisory lock 挡下来 —— 挡是挡住了,但一半条目落成
    `CONCURRENT_IN_FLIGHT`,运营看到的是"跑了一半失败了"。

    比 `with_for_update` 这个名字还要多比一层关键字:漏掉 `skip_locked=True`
    的话行为退化成排队等同一批,横向扩容变得毫无意义,而且**不报错**。
    """
    claim = _fn("claim_items")
    for sub in ast.walk(claim):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "with_for_update"
        ):
            kwargs = {kw.arg for kw in sub.keywords}
            assert "skip_locked" in kwargs, "领取必须带 skip_locked=True"
            return
    raise AssertionError("claim_items 里没有 with_for_update —— 领取不再是原子的")


def test_run_batch_goes_through_the_claim():
    """`run_batch` 一旦改回自己 SELECT 一遍 PENDING,租约就成了一个
    写进库却没人写的列,而回收器会把正在跑的条目当成残骸回收。"""
    assert "claim_items" in _call_names(_fn("run_batch"))


def _own_level_calls(fn: ast.FunctionDef) -> list[tuple[int, str]]:
    """`fn` **自己那一层**的调用,不含嵌套函数体里的。

    这一步不是洁癖:`run_batch` 里的 `persist()` 定义在 `run_plan` 调用
    之前,而它体内有一次 `session.commit()`。按行号排的话那次提交排在
    "领取之后、执行之前",于是把领取后的提交整段删掉,顺序断言**照样绿**。
    实测过:删掉之后 22/22 全过。嵌套函数体是另一个执行时机,不能算进
    这一层的语句顺序。
    """
    out: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            if isinstance(child, ast.Call):
                name = (
                    child.func.id
                    if isinstance(child.func, ast.Name)
                    else getattr(child.func, "attr", "")
                )
                out.append((child.lineno, name))
            visit(child)

    visit(fn)
    return sorted(out)


def test_the_lease_is_committed_before_the_work_starts():
    """租约不落库等于没有:别的 worker 看不见内存里的对象。

    判据是顺序 —— `claim_items` 之后、`run_plan` 之前必须有一次提交,
    而且是 `run_batch` 自己那一层的提交(见 `_own_level_calls`)。
    """
    order = [name for _, name in _own_level_calls(_fn("run_batch"))]

    assert "claim_items" in order, "run_batch 不再领取"
    assert "run_plan" in order, "run_batch 不再执行"
    claimed = order.index("claim_items")
    ran = order.index("run_plan")
    commits = [i for i, name in enumerate(order) if name == "commit"]
    assert any(claimed < i < ran for i in commits), (
        "领取与执行之间没有提交 —— 租约不会落库,"
        "另一个 worker 会把同一批再领一遍"
    )


def test_settlement_writes_the_finish_time_in_both_directions():
    """结算是可撤销的:回收把一件放回 PENDING 时,批次从"已完成"退回
    "执行中",而旧的完成时间如果留着,列表页会显示"10:05 完成"却带着
    一个转圈的状态。所以 `finished_at` 必须有被写回 None 的那一支。"""
    settle = _fn("settle")
    assigns = [
        node
        for node in ast.walk(settle)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == "finished_at"
            for t in node.targets
        )
    ]
    assert assigns, "settle 不写 finished_at"
    assert any(
        isinstance(node.value, ast.Constant) and node.value.value is None
        for node in assigns
    ), "settle 只会盖上完成时间,不会撤销 —— 回收之后批次状态会自相矛盾"


def test_run_batch_settles_instead_of_stamping_the_status_itself():
    """结算口径只有一份。`run_batch` 自己写 `job.status` 的话,
    回收器那条路径就会有第二份,而两份迟早不一致。"""
    assert "settle" in _call_names(_fn("run_batch"))


def test_the_reaper_scans_across_batches():
    """死掉的 worker 不会告诉我们它领的是哪个批次,所以回收必须是全表的。
    一旦有人给它加上 `batch_id ==` 的条件,残骸就只能靠人逐个批次去捞。"""
    reap = _fn("reap_expired_leases")
    for node in ast.walk(reap):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "batch_id"
        ):
            raise AssertionError("回收器按批次过滤了 —— 它应当是跨批次全表扫描")


def test_the_reaper_lets_the_rules_decide():
    """回收器自己不判定,它只执行 `reclaim_verdict` 的结果。
    判定一旦搬进 service,"几次之后放弃"就再也没有穷举测试。"""
    assert "rules.reclaim_verdict" in _call_names(_fn("reap_expired_leases"))


def test_the_reaper_re_settles_every_batch_it_touched():
    """回收把条目从 RUNNING 挪走,批次整体状态因此可能变化。
    不重算的话批次那一行与它自己的条目对不上,而列表页读的是那一行。"""
    assert "settle" in _call_names(_fn("reap_expired_leases"))


def test_redispatch_goes_through_the_dispatch_service():
    """全仓只允许 `dispatch_service` 碰队列。多一个入口就多一条绕过登记的
    路径,而这种绕过只会表现为"偶尔有任务莫名其妙不跑"。"""
    names = _call_names(_fn("redispatch_stalled_batches"))
    assert "dispatch_service.send_batch" in names
    assert not any(name.endswith(".delay") for name in names)


def test_redispatch_reuses_the_pickup_threshold_from_the_rules():
    """界面上说"可能没派出去"和后台真的重投,必须用同一个阈值。
    两个数会让运营先看到提示却等不到自愈,或者反过来。"""
    source = SERVICE.read_text(encoding="utf-8")
    assert "rules.BATCH_PICKUP_STALL_SECONDS" in source


def test_the_reaper_is_on_the_beat_schedule():
    """写好了没挂到 beat 上,等于回收器不存在 —— 而它的失效模式恰恰是
    **没人盯着**:批次卡住的那天没有人会想起来手工跑一次脚本。"""
    assert "maintenance.reap_batch_leases" in CELERY.read_text(encoding="utf-8")
    assert "maintenance.reap_batch_leases" in MAINTENANCE.read_text(encoding="utf-8")


def test_the_reaper_task_reaps_before_it_redispatches():
    """顺序不能靠调度保证:先重投的话,它看到的是一个"没有 PENDING 条目"
    的批次(残骸还停在 RUNNING),于是不投;等回收器把条目放回去时,
    重投已经跑完了 —— 那些条目要再等一整拍。"""
    names = _call_names(_fn("reap_batch_leases", MAINTENANCE))
    assert "bs.reap_expired_leases" in names
    assert "bs.redispatch_stalled_batches" in names
    assert names.index("bs.reap_expired_leases") < names.index(
        "bs.redispatch_stalled_batches"
    )


def test_the_lease_columns_are_indexed_for_the_reaper():
    """回收器每分钟扫一次全表。没有索引时它会随着历史批次一起变慢,
    而变慢的表现是"回收越来越晚",不是报错。"""
    source = MODEL.read_text(encoding="utf-8")
    assert "ix_batch_job_items_lease" in source
    assert "lease_until" in source and "lease_owner" in source


# ---------------------------------------------------------------- A43:fencing token
#
# BLOCK-02 的判定部分。**接线部分只能静态钉** —— 条件更新要真库两个 session
# 才验得出来,而那条路径在本机是 skip 的(见本文件开头那段分界线)。


def test_ownership_is_decided_by_the_token_not_by_the_clock():
    """一件跑超时但**还没被抢走**的条目,仍然有权把自己的结果落库。

    直觉上 `lease_still_held()` 该再加一条"租约没过期"。加了就错:
    真正的交接发生在回收器把 `lease_token` 吊销的那一刻,不是时钟走过
    某一秒的那一刻。把到期时刻写进归属判定,后果是一件"跑超了但没人来抢"
    的条目连自己的结果都落不了库 —— 钱已经花掉,结果却被自己丢掉。

    这条用例钉的就是"时间不参与归属":函数签名里根本没有 now / lease_until。
    """
    import inspect

    params = set(inspect.signature(b.lease_still_held).parameters)
    assert "now" not in params and "lease_until" not in params, (
        "归属由令牌决定,不由时钟决定 —— 时间参数一旦出现在这里,"
        "跑超时的 worker 会丢掉自己已经花了钱的结果"
    )
    assert b.lease_still_held(
        status=BatchItemStatus.RUNNING.value,
        lease_owner="w-A",
        lease_token="tok-1",
        owner="w-A",
        token="tok-1",
    )


def test_a_revoked_token_loses_the_row_even_for_the_original_owner():
    """回收器清空令牌之后,原 owner 的落库必须被判成失效。

    这是 A42 那条"已知缺口"用例的正面版本:owner 名字仍然对得上
    (worker 没换机器),差别只有令牌。**只比 owner 是不够的** ——
    同一个 worker 进程完全可能把同一件重新领一次。
    """
    assert not b.lease_still_held(
        status=BatchItemStatus.RUNNING.value,
        lease_owner=None,
        lease_token=None,
        owner="w-A",
        token="tok-1",
    )
    # 换了新令牌(被 worker B 领走),旧令牌同样落不进来
    assert not b.lease_still_held(
        status=BatchItemStatus.RUNNING.value,
        lease_owner="w-B",
        lease_token="tok-2",
        owner="w-A",
        token="tok-1",
    )


def test_null_tokens_do_not_match_each_other():
    """两边都是 None 时**不能**判成持有。

    Python 里 `None == None` 为真,SQL 里 `NULL = NULL` 为 NULL。
    纯判定跟着 SQL 走,否则"本机测试绿、真库下不一致"。
    0026 之前的存量行正好是这个形状。
    """
    assert not b.lease_still_held(
        status=BatchItemStatus.RUNNING.value,
        lease_owner=None,
        lease_token=None,
        owner="",
        token="",
    )


def test_a_row_that_left_running_is_no_longer_mine():
    """人工重试打回 PENDING、回收器放弃打成 FAILED —— 两条路都不经过"别人领走"。"""
    for status in (
        BatchItemStatus.PENDING.value,
        BatchItemStatus.FAILED.value,
        BatchItemStatus.SUCCEEDED.value,
    ):
        assert not b.lease_still_held(
            status=status,
            lease_owner="w-A",
            lease_token="tok-1",
            owner="w-A",
            token="tok-1",
        )


def test_renewal_happens_with_half_the_lease_still_left():
    """续租不能等到快到期才做。

    续租失败的那一刻是链路上**唯一**一个"已经知道自己出局、但还没花钱"
    的时刻。留一半余量,是为了让这个发现落在下一次付费调用之前。
    """
    lease = b.ITEM_LEASE_SECONDS
    assert b.should_renew_lease(lease_until=1000.0 + lease * 0.4, now=1000.0, lease_seconds=lease)
    assert not b.should_renew_lease(
        lease_until=1000.0 + lease * 0.9, now=1000.0, lease_seconds=lease
    )
    # 没有租约列可读时按"该续"处理 —— 与 lease_expired 把 None 判成过期同向
    assert b.should_renew_lease(lease_until=None, now=1000.0, lease_seconds=lease)


def test_lease_lost_is_its_own_error_code_and_costs_nothing():
    """`LEASE_LOST` 不能复用 `WORKER_LOST` 或 `CONCURRENT_IN_FLIGHT`。

    三个码的区别是"谁不见了":别人正在跑 / 没人在跑了 / 这一件改由别人跑了。
    运营要看的地方完全不同 —— 续租失败说明租约时长配短了,worker 丢失
    说明进程有问题。合成一个码,异常中心就再也分不出这两件事。
    """
    meta = b.error_meta(BatchErrorCode.LEASE_LOST)
    assert meta.retryable, "改由别人跑了不是一个需要人来处理的失败"
    assert not meta.needs_human
    assert "没有调用付费模型" in meta.advice, (
        "这个码的全部价值在于它宣告本次没花钱;advice 里必须说出来"
    )
    assert b.classify_exception(b.LeaseLostError("x")) [0] is BatchErrorCode.LEASE_LOST


def test_the_write_back_path_carries_a_where_clause():
    """落库必须是条件更新。**这条守的是 BLOCK-02 不被改回去。**

    `_apply_outcome` 原来是一个纯赋值函数,谁调谁写。静态钉住四个条件
    同时出现在 `apply_outcome` 里 —— 少任何一个,旧 worker 就能覆盖新 owner。
    """
    src = SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "apply_outcome"
    )
    body = ast.get_source_segment(src, fn) or ""
    for needle in (
        "BatchJobItem.status == BatchItemStatus.RUNNING.value",
        "BatchJobItem.lease_owner == owner",
        "BatchJobItem.lease_token == token",
    ):
        assert needle in body, f"落库的 WHERE 缺少条件:{needle}"
    assert "result.rowcount == 1" in body, (
        "影响行数必须被检查 —— 不检查等于没有条件更新"
    )
    assert "_record_stale_outcome" in body, (
        "落不进去的结果必须留下记录:那次执行真的花了钱"
    )


def test_the_reaper_revokes_the_token_when_it_takes_a_row_back():
    """回收 = 吊销令牌。清 owner / until 只是让它看起来没人持有。"""
    src = SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for name in ("reap_expired_leases", "reset_items_for_retry"):
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == name
        )
        body = ast.get_source_segment(src, fn) or ""
        assert "lease_token = None" in body, (
            f"{name} 必须吊销令牌,否则原 worker 跑完仍能覆盖新 owner 的结果"
        )


def test_execution_renews_before_it_spends_money():
    """`run_batch` 的执行回调里,续租必须在 `_execute` 之前。"""
    src = SERVICE.read_text(encoding="utf-8")
    start = src.index("def run_batch(")
    body = src[start : src.index("\ndef settle(", start)]
    renew_at = body.index("renew_lease(")
    exec_at = body.index("_execute(")
    assert renew_at < exec_at, (
        "续租要落在付费调用之前 —— 那是唯一一个还能免费停下来的时刻"
    )


def test_the_model_has_a_token_column_and_a_migration_for_it():
    """列 + 迁移必须同时存在。少了迁移,部署时是一个 UndefinedColumn。"""
    assert "lease_token" in MODEL.read_text(encoding="utf-8")
    migrations = (BACKEND_ROOT / "migrations" / "versions").glob("*.py")
    assert any(
        "lease_token" in m.read_text(encoding="utf-8") for m in migrations
    ), "加了列没加迁移"
