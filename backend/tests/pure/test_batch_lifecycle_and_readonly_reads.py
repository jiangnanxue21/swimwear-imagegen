"""A15:回执生命周期、真实用量、只读读取、驳回硬闸门。

对应评审第 2、4、5、6、7、17、18、19 条。

分成两半,理由和 `test_copy_pipeline_config_and_schema.py` 一样:

    纯函数部分   `receipt_route` / `usage_dict` / `resolve_target` 可以穷举。
                 这些函数刻意不读 settings、不碰 session,就是为了能在
                 这里被逐个取值验证
    AST 部分     "GET 不许 commit"、"下载不许调 record_export" 这类约束
                 没有可调用的判定函数,只能钉源码。全部用 AST 找**真实的
                 调用节点**,不用 `"commit" in source` —— 阶段 7 的教训是
                 字符串包含式的断言,把调用删掉只留注释照样绿
"""
from __future__ import annotations

import ast

from app.workbench import batch as b
from app.workbench import platform as pf
from app.workbench.batch import BatchAction, ReceiptRoute, ReceiptStatus
from tests.pure._helpers import BACKEND_ROOT, expect_raises

APP = BACKEND_ROOT / "app"


def _read(path):
    return path.read_text(encoding="utf-8")


def _func(path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(_read(path))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到函数 {name}")


def _call_names(node: ast.AST) -> set[str]:
    """函数体里出现的调用,归一成 'session.commit' / 'record_export' 这种名字。"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            out.add(func.id)
        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                out.add(f"{func.value.id}.{func.attr}")
            out.add(func.attr)
    return out


# ==========================================================
# 第 5 条:回执生命周期
# ==========================================================


def test_no_receipt_means_execute():
    assert b.receipt_route(None, BatchAction.GENERATE_COPY) is ReceiptRoute.EXECUTE


def test_a_succeeded_receipt_is_reused_for_every_action():
    """成功回执在四个动作下都复用。这是"不重复触发付费调用"的本体。"""
    for action in BatchAction:
        assert (
            b.receipt_route(ReceiptStatus.SUCCEEDED.value, action)
            is ReceiptRoute.REUSE_SUCCESS
        ), action


def test_an_unbilled_failure_is_as_if_it_never_happened():
    """没花钱的失败必须能真正重跑 —— FE-304 的重试靠这条成立。"""
    for action in BatchAction:
        assert (
            b.receipt_route(ReceiptStatus.FAILED_UNBILLED.value, action)
            is ReceiptRoute.EXECUTE
        ), action


def test_a_billed_copy_failure_is_revalidated_not_regenerated():
    """第 5 条的核心:文案已经生成、只是校验没过时,重试不许再调模型。

    模型产出连同违规项都在 `listing_copies` 里,重新校验只是再读一遍规则。
    走 EXECUTE 的话,同一个提示词会被再发一次 —— 钱花两遍,而第二次拿到的
    多半是同一段被拒的文本。
    """
    assert (
        b.receipt_route(ReceiptStatus.FAILED_BILLED.value, BatchAction.GENERATE_COPY)
        is ReceiptRoute.REUSE_BILLED_FAILURE
    )


def test_a_billed_extraction_failure_has_nothing_to_revalidate():
    """识别全失败 = 一条证据都没落。没有东西可以"重新校验",只能重跑。

    这条和上一条一起,钉住 `REVALIDATABLE_ACTIONS` 不许被顺手扩大:
    把 EXTRACT 放进去的表现是重试永远返回同一个空结果,而运营看到的是
    "已复用",完全看不出识别其实从没成功过。
    """
    assert (
        b.receipt_route(ReceiptStatus.FAILED_BILLED.value, BatchAction.EXTRACT)
        is ReceiptRoute.EXECUTE_AGAIN_BILLED
    )


def test_an_interrupted_execution_is_never_silently_retried_for_free():
    """IN_FLIGHT = 上次执行中途死掉。**绝不能当成没做过悄悄重来。**

    这条守的东西没变,变的是"不可考"这个前提(A45-batch12-2 / EX-02)。

    迁移 0031 之后钱花没花是**可考**的:`provider_call_at` 由独立事务在付费
    调用发出之前写下,崩溃之后它还在。于是同一个 IN_FLIGHT 分成两支:

        调用已发出   停下等人对账 —— 不再自动重跑,自动重复付费降到 0 次
        调用没发出   可证明没花钱,当成没做过重来是**正确**的

    第二支看起来像这条守卫原来禁止的那件事,区别是"悄悄"两个字:
    那时是分不出来所以赌一把,现在是库里读得到所以知道。
    """
    for action in BatchAction:
        assert (
            b.receipt_route(
                ReceiptStatus.IN_FLIGHT.value, action, call_dispatched=True
            )
            is ReceiptRoute.NEEDS_RECONCILIATION
        ), action
        assert (
            b.receipt_route(
                ReceiptStatus.IN_FLIGHT.value, action, call_dispatched=False
            )
            is ReceiptRoute.EXECUTE
        ), action


def test_the_conservative_default_is_to_assume_the_money_was_spent():
    """不传 `call_dispatched` 时必须按"可能已经花了"处理。

    默认值是这条修复里唯一一个会**静默**出错的地方:反过来设成 False 的话,
    每一条 0031 之前的存量回执、以及任何忘了传这个参数的调用点,都会被判成
    "没做过"直接重跑 —— 而那是一笔谁也对不上的费用。
    """
    assert (
        b.receipt_route(ReceiptStatus.IN_FLIGHT.value, BatchAction.EXTRACT)
        is ReceiptRoute.NEEDS_RECONCILIATION
    )


def test_an_unknown_status_costs_money_rather_than_faking_success():
    """兜底方向:宁可多花一次钱,不可把坏状态当成功复用。"""
    assert b.receipt_route("WAT", BatchAction.EXTRACT) is ReceiptRoute.EXECUTE


def test_every_receipt_status_has_a_label():
    assert set(b.RECEIPT_STATUS_LABELS) == set(ReceiptStatus)


def test_every_receipt_status_routes_somewhere():
    """新加一个状态却忘了给它一条路,表现是它落进兜底的 EXECUTE ——
    也就是"每次重试都重新计费",而且没有任何地方会说一句。"""
    for status in ReceiptStatus:
        for action in BatchAction:
            assert isinstance(b.receipt_route(status.value, action), ReceiptRoute)


# ==========================================================
# 第 4 条:真实用量
# ==========================================================


def test_usage_keeps_attempted_and_billable_apart():
    usage = b.usage_dict(attempted_calls=8, billable_calls=8, unit="image", images=8)
    assert usage["attempted_calls"] == 8
    assert usage["billable_calls"] == 8
    assert usage["unit"] == "image"


def test_a_template_run_reports_zero_not_one():
    """模板生成器没有任何远程调用。原来无条件记 1 次,默认配置下跑一个
    50 件的批次,台账上凭空出现 50 次不存在的费用。"""
    usage = b.usage_dict(attempted_calls=0, billable_calls=0, unit="call")
    assert usage["billable_calls"] == 0


def test_billable_can_not_exceed_attempted():
    """算出"计费次数比发起次数还多"时,错的是调用方,不是台账。
    静默接受它等于让费用台账变成一个没人能核对的数字。"""
    expect_raises(
        ValueError, b.usage_dict, attempted_calls=1, billable_calls=2, unit="call"
    )


def test_usage_rejects_negative_counts():
    expect_raises(
        ValueError, b.usage_dict, attempted_calls=-1, billable_calls=0, unit="call"
    )


def test_extract_executor_no_longer_hardcodes_one_paid_call():
    """第 4 条:识别是逐图调用的,`paid_calls=1` 是个假数字。"""
    source = ast.get_source_segment(
        _read(APP / "workbench" / "batch_service.py"),
        _func(APP / "workbench" / "batch_service.py", "_exec_extract"),
    )
    assert "paid_calls=1" not in source, "识别执行器又把付费次数写死成 1 了"
    assert "paid_calls=attempted" in source, "识别执行器没有按图片数计费"


def test_copy_executor_bills_only_the_llm_generator():
    source = ast.get_source_segment(
        _read(APP / "workbench" / "batch_service.py"),
        _func(APP / "workbench" / "batch_service.py", "_exec_generate_copy"),
    )
    assert "paid_calls=1" not in source, "文案执行器又把付费次数写死成 1 了"
    assert "LLM_GENERATOR" in source, "文案执行器没有区分模板与大模型"


# ==========================================================
# 第 7 条:驳回关闭的硬闸门
# ==========================================================


def test_evidence_closes_a_rejection():
    assert (
        pf.resolve_target(
            current_status="OPEN", gate_satisfied=True, declared_fixed=False
        )
        == pf.RESOLVE_TO_RESOLVED
    )


def test_a_declaration_alone_can_not_reach_resolved():
    """第 7 条的整条命脉。

    原来勾一个框或写一句备注就能把记录变 RESOLVED,于是"已解决"在台账上
    什么也不代表。现在声明只能到中间态,而中间态**仍然阻断**。
    """
    target = pf.resolve_target(
        current_status="OPEN", gate_satisfied=False, declared_fixed=True
    )
    assert target == pf.RESOLVE_TO_FIXED_PENDING
    assert target != pf.RESOLVE_TO_RESOLVED


def test_saying_nothing_is_refused():
    assert (
        pf.resolve_target(
            current_status="OPEN", gate_satisfied=False, declared_fixed=False
        )
        == pf.RESOLVE_REFUSED
    )


def test_a_declared_rejection_stays_put_until_evidence_shows_up():
    """已经声明过修复的记录,再点一次仍然不会仅凭点击变成已解决。"""
    assert (
        pf.resolve_target(
            current_status="FIXED_PENDING_EXPORT",
            gate_satisfied=False,
            declared_fixed=False,
        )
        == pf.RESOLVE_TO_FIXED_PENDING
    )
    assert (
        pf.resolve_target(
            current_status="FIXED_PENDING_EXPORT",
            gate_satisfied=True,
            declared_fixed=False,
        )
        == pf.RESOLVE_TO_RESOLVED
    )


def test_the_intermediate_state_still_blocks():
    """中间态必须算"未解决",否则点一次"我改好了"商品就从阻断列表消失 ——
    那正是原来勾选框的问题,只是换了个状态名。"""
    from app.core.enums import UNRESOLVED_REJECTION_STATUSES

    assert "OPEN" in UNRESOLVED_REJECTION_STATUSES
    assert "FIXED_PENDING_EXPORT" in UNRESOLVED_REJECTION_STATUSES
    assert "RESOLVED" not in UNRESOLVED_REJECTION_STATUSES


def test_open_rejections_query_uses_the_unresolved_set():
    """判据必须是那个集合,不是 `== OPEN` —— 后者会漏掉中间态。"""
    source = ast.get_source_segment(
        _read(APP / "workbench" / "platform_service.py"),
        _func(APP / "workbench" / "platform_service.py", "open_rejections"),
    )
    assert "UNRESOLVED_REJECTION_STATUSES" in source
    assert "== RejectionStatus.OPEN.value" not in source


def test_resolving_no_longer_flips_the_platform_status_by_itself():
    """"关掉最后一条驳回"和"我把新文件传上去了"是两件事。

    自动跳 SUBMITTED 是在替运营声明一件系统根本看不见的事。
    """
    source = ast.get_source_segment(
        _read(APP / "workbench" / "platform_service.py"),
        _func(APP / "workbench" / "platform_service.py", "resolve_rejection"),
    )
    assert "PlatformStatus.SUBMITTED" not in source, "关闭驳回又在自动改平台状态了"


def test_the_rejection_status_column_fits_the_new_state():
    """`FIXED_PENDING_EXPORT` 有 20 个字符。列宽不够时 PostgreSQL 直接拒写,
    而 SQLite 夹具不会 —— 那是典型的"测试全绿、上真库才炸"。"""
    source = _read(APP / "models" / "platform_rejection.py")
    assert "String(32), nullable=False, default=RejectionStatus.OPEN.value" in source
    assert len("FIXED_PENDING_EXPORT") <= 32


# ==========================================================
# 第 6 条:生成一次,下载任意次
# ==========================================================


def test_download_does_not_generate_or_record_an_export():
    """GET 下载不许产出字节,也不许改导出次数。"""
    calls = _call_names(
        _func(APP / "workbench" / "batch_service.py", "read_batch_file")
    )
    assert "record_export" not in calls, "下载路径又在记导出留痕了"
    assert "to_batch_xlsx" not in calls, "下载路径又在现场生成文件了"
    assert "batch_file" not in calls, "下载路径又去调生成函数了"


def test_generation_is_idempotent_on_an_already_generated_batch():
    """已经生成过就返回旧的那一份。重新生成出来的是"现在的内容",
    而驳回要定位的是"运营当时传上去的那一份"。"""
    source = ast.get_source_segment(
        _read(APP / "workbench" / "batch_service.py"),
        _func(APP / "workbench" / "batch_service.py", "ensure_batch_export"),
    )
    assert "if job.file_path:" in source, "生成不再是幂等的了"
    assert source.index("if job.file_path:") < source.index("batch_file("), (
        "幂等检查跑在重新生成之后"
    )


def test_the_download_route_is_a_get_and_generation_is_a_post():
    source = _read(APP / "api" / "workbench_batch.py")
    assert '@router.post("/batches/{batch_id}/file")' in source, "生成不是 POST"
    assert '@router.get("/batches/{batch_id}/file")' in source, "下载不是 GET"


def test_generation_and_download_are_two_different_audit_actions():
    """两者原来共用 "download" 一个词,于是审计里数不出这份文件被下过几次。"""
    from app.workbench.audit_view import PAYLOAD_ACTION_LABELS

    assert "generate_export_file" in PAYLOAD_ACTION_LABELS
    assert "download_export_file" in PAYLOAD_ACTION_LABELS


# ==========================================================
# 第 19 条:读取接口不改业务状态
# ==========================================================

#: 挂在 GET 上、且会走 `collect()` 的那几个接口。
_READ_ONLY_ENDPOINTS = (
    (APP / "api" / "workbench.py", "list_products"),
    (APP / "api" / "workbench.py", "product_flow"),
    (APP / "api" / "workbench.py", "draft_stale_reason"),
    (APP / "api" / "workbench_batch.py", "list_exceptions"),
    (APP / "api" / "workbench_batch.py", "list_spus"),
)


def test_read_endpoints_do_not_commit():
    """页面刷新、浏览器预取、监控探活都不该改变业务状态。"""
    for path, name in _READ_ONLY_ENDPOINTS:
        calls = _call_names(_func(path, name))
        assert "session.commit" not in calls, f"{name} 又在提交事务了"


def test_read_endpoints_ask_for_the_dry_run_judgement():
    """判定照常做(界面上仍然显示"已过期"),只是不落库。

    修法必须是给 `collect` 传 dry_run,**不是**在读接口里跳过判定 ——
    跳过的话列表页会把过期草稿显示成正常的。
    """
    for path, name in _READ_ONLY_ENDPOINTS:
        source = ast.get_source_segment(_read(path), _func(path, name))
        assert "dry_run=True" in source, f"{name} 没有走 dry-run 判定"


def test_staleness_still_has_exactly_one_implementation():
    """P4 的老约束:过期口径只有 `refresh_draft` 一份。
    dry_run 是给它加的一个模式,不是第二份判定。

    ## 断言从字符串包含改成了 AST(任务 19)

    原来钉的是 `"refresh_draft(session, product, row, dry_run=dry_run)" in src`。
    那个写法有两个毛病,任务 19 给 `refresh_draft` 加一个可选参数时同时撞上了:

        一、**参数表一动就红,而不变量根本没被破坏。** 它钉的是调用长什么样,
            不是"有没有委托、有没有透传"。
        二、**反方向拦不住。** 那串字符出现在注释里照样绿。本文件开头
            (以及 `test_transaction_boundaries.py`)写的就是这条教训。

    改成 AST 之后顺手补强了第三条:全仓 `draft_rules.is_stale` 只许在
    `refresh_draft` 里被调用。这才是"只有一份实现"的字面意思 ——
    原来那个字符串断言对"别处又算了一遍"是完全无感的。
    """
    service_path = APP / "workbench" / "service.py"
    fn = _func(service_path, "_draft_facts")

    delegations = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "refresh_draft"
    ]
    assert len(delegations) == 1, (
        f"_draft_facts 里有 {len(delegations)} 处 refresh_draft 调用,应当恰好一处"
    )

    forwarded = [
        kw
        for kw in delegations[0].keywords
        if kw.arg == "dry_run" and isinstance(kw.value, ast.Name) and kw.value.id == "dry_run"
    ]
    assert forwarded, (
        "_draft_facts 没有把 dry_run **原样**转给 refresh_draft —— "
        "写成常量的话,只读接口要么开始写库,要么再也不落库"
    )

    # 只有一份实现:过期判定本体不许在 refresh_draft 之外被调用。
    # 用行号区间而不是节点身份 —— `_func()` 是另一次 ast.parse,
    # 同一段源码解析两遍得到的是两组不同对象,`in` 永远为假
    refresh = _func(service_path, "refresh_draft")
    body = range(refresh.lineno, (refresh.end_lineno or refresh.lineno) + 1)
    strays = [
        node.lineno
        for node in ast.walk(ast.parse(_read(service_path)))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "is_stale"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "draft_rules"
        and node.lineno not in body
    ]
    assert not strays, (
        f"第 {strays} 行在 refresh_draft 之外又算了一遍过期 —— P4 说只许有一份"
    )


def test_someone_still_persists_the_stale_verdict():
    """读接口不写之后,总得有人写 —— 否则"过期草稿禁止导出"就失去了依据。

    这条钉住那个兜底任务的存在。删掉它,过期状态会长期停在旧结论上,
    而这件事不会有任何报错。
    """
    source = _read(APP / "tasks" / "maintenance_tasks.py")
    assert "def refresh_stale_drafts(" in source, "过期草稿的落库兜底任务没了"
    assert "dry_run" not in source, "兜底任务也走了 dry-run,那就没人落库了"

    beat = _read(APP / "tasks" / "celery_app.py")
    assert "maintenance.refresh_stale_drafts" in beat, "兜底任务没挂到 beat 上"


# ==========================================================
# 第 18 条:审计过滤下推数据库
# ==========================================================


def test_audit_query_no_longer_truncates_before_filtering():
    """先 `.limit(2000)` 再在 Python 里按动作过滤的后果是两条:

    超出最近 2000 条的匹配记录**永远查不到**(而按动作查审计的人要的
    恰恰是三个月前那次导出),`total` 和分页也只反映那 2000 条。

    判据找的是**真实的调用节点**,不是文字:上面那句解释为什么改的注释
    里也写着 `.limit(2000)`,按字符串断言会把注释当成回归。
    """
    fn = _func(APP / "api" / "workbench_batch.py", "query_audit")
    truncating = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "limit"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == 2000
    ]
    assert not truncating, "审计查询又在过滤前先截断了"

    source = ast.get_source_segment(_read(APP / "api" / "workbench_batch.py"), fn)
    assert "offset(" in source, "分页没有交给数据库"
    assert "func.count()" in source, "总数没有交给数据库"


def test_audit_query_keeps_the_payload_first_semantics():
    """下推到 SQL 的判据必须和 `audit_view.business_action` 是同一句话:
    payload 里有 action 就用它,否则退回数据库列。"""
    source = ast.get_source_segment(
        _read(APP / "api" / "workbench_batch.py"),
        _func(APP / "api" / "workbench_batch.py", "query_audit"),
    )
    assert 'AuditLog.payload["action"]' in source
    assert "AuditLog.action == action" in source


# ==========================================================
# 第 2 条:批次不再是一个长事务
# ==========================================================


def test_run_batch_commits_per_item():
    """每件一个**事务**,不只是一个保存点。

    请求断在半路时,已完成的条目、回执、审计都已落盘 —— 而不是连同
    已经计费的外部调用一起回滚。
    """
    source = ast.get_source_segment(
        _read(APP / "workbench" / "batch_service.py"),
        _func(APP / "workbench" / "batch_service.py", "run_batch"),
    )
    assert "commit_each" in source, "run_batch 又变回一个长事务了"
    assert "session.commit()" in source, "每件之后没有事务边界"


def test_batch_creation_commits_before_it_runs_anything():
    """批次和它的全部条目要先成为既成事实,再谈执行。"""
    source = ast.get_source_segment(
        _read(APP / "api" / "workbench_batch.py"),
        _func(APP / "api" / "workbench_batch.py", "create_batch"),
    )
    assert source.index("session.commit()") < source.index("bs.run_batch("), (
        "批次还没提交就开始执行了"
    )


def test_every_execution_mode_is_on_the_whitelist():
    assert b.BATCH_EXECUTION_MODES == ("inline", "celery")


def test_only_dispatch_service_sends_the_batch_to_the_queue():
    """全仓单一队列入口的老约束,新代码同样要守。"""
    source = ast.get_source_segment(
        _read(APP / "api" / "workbench_batch.py"),
        _func(APP / "api" / "workbench_batch.py", "_dispatch_batch"),
    )
    assert ".delay(" not in source, "API 又直接投递队列了"
    assert "dispatch_service" in source


# ==========================================================
# 第 17 条:商品删除后批次明细留存
# ==========================================================


def test_batch_items_survive_a_deleted_product():
    """冗余 sku / spu 的注释写着"删后仍可读",而外键原来是 CASCADE ——
    两句话只能留一句。"""
    source = _read(APP / "models" / "batch_job.py")
    assert 'ForeignKey("products.id", ondelete="SET NULL")' in source
    assert 'ForeignKey("products.id", ondelete="CASCADE")' not in source


def test_the_migration_widens_before_it_switches_the_foreign_key():
    """顺序要紧:SET NULL 的外键配上仍然 NOT NULL 的列,删商品会撞非空约束 ——
    一个只在真的删商品时才暴露的坏中间态。"""
    source = _read(
        BACKEND_ROOT
        / "migrations"
        / "versions"
        / "0021_batch_receipt_lifecycle_and_export_file.py"
    )
    upgrade = source[source.index("def upgrade()") : source.index("def downgrade()")]
    assert upgrade.index("nullable=True") < upgrade.index("drop_constraint")


# ==========================================================
# 批次活性:Broker 挂了的时候批次列表不该转到天荒地老
# ==========================================================
#
# 全部**真的调用 `b.liveness()` 看返回值**,没有一条钉源码字符串 ——
# 这批判定有可调用的入口,那就该按取值验,而不是按"这行字还在"验。


def _live(status="QUEUED", *, created=None, started=None, progress=None, now=10_000.0, **kw):
    return b.liveness(
        status=status,
        created_at=created,
        started_at=started,
        last_progress_at=progress,
        now=now,
        **kw,
    )


def test_a_batch_nobody_ever_picked_up_is_reported_stalled():
    """这就是要修的那个:`BATCH_EXECUTION_MODE=celery` 下 Broker 挂着,
    批次建好、条目全 PENDING、`started_at` 永远是 NULL。

    投递失败的 False 只出现在创建那一次的响应里、不落库,所以刷新之后
    前端看到的就是一个 QUEUED 批次 —— 而它的规矩是"非终态就继续轮询"。
    """
    now = 10_000.0
    verdict = _live(created=now - b.BATCH_PICKUP_STALL_SECONDS - 1, now=now)
    assert verdict.state is b.BatchLiveness.STALLED
    assert verdict.stalled_seconds is not None and verdict.stalled_seconds > 0
    assert verdict.advice, "STALLED 必须带一句可执行的话,否则运营只知道坏了不知道做什么"


def test_a_batch_still_within_the_pickup_window_is_not_accused():
    """刚建好的批次不许报卡住 —— 那是每一个批次出生后的正常样子。"""
    now = 10_000.0
    verdict = _live(created=now - 5, now=now)
    assert verdict.state is b.BatchLiveness.LIVE
    assert verdict.advice is None and verdict.stalled_seconds is None


def test_the_pickup_verdict_flips_exactly_at_the_threshold():
    """边界取"到点就算",不是"超过才算"。写死这一条是因为边界差一秒的
    改动不会让任何别的用例变红。"""
    now = 10_000.0
    at = _live(created=now - b.BATCH_PICKUP_STALL_SECONDS, now=now)
    just_under = _live(created=now - b.BATCH_PICKUP_STALL_SECONDS + 1, now=now)
    assert at.state is b.BatchLiveness.STALLED
    assert just_under.state is b.BatchLiveness.LIVE


def test_a_running_batch_that_keeps_finishing_items_is_live():
    """有条目在陆续落地 = worker 活着。大批次跑很久本身不是问题。"""
    now = 10_000.0
    verdict = _live(
        "RUNNING", created=now - 100_000, started=now - 99_000, progress=now - 60, now=now
    )
    assert verdict.state is b.BatchLiveness.LIVE


def test_a_running_batch_whose_items_stopped_landing_is_stalled():
    """worker 中途没了:状态停在 RUNNING,最后一个 finished_at 越来越旧。"""
    now = 10_000.0
    verdict = _live(
        "RUNNING",
        created=now - 100_000,
        started=now - 99_000,
        progress=now - b.BATCH_PROGRESS_STALL_SECONDS - 1,
        now=now,
    )
    assert verdict.state is b.BatchLiveness.STALLED
    assert "重试" in (verdict.advice or ""), "建议要指向一个真的能点的动作"


def test_a_running_batch_with_no_finished_items_falls_back_to_started_at():
    """一件都还没跑完时没有心跳可用,按开跑时刻算 —— 否则第一件就特别慢的
    批次会因为"没有 last_progress_at"而永远算 LIVE。"""
    now = 10_000.0
    fresh = _live("RUNNING", created=now - 100, started=now - 60, progress=None, now=now)
    ancient = _live(
        "RUNNING",
        created=now - 100_000,
        started=now - b.BATCH_PROGRESS_STALL_SECONDS - 1,
        progress=None,
        now=now,
    )
    assert fresh.state is b.BatchLiveness.LIVE
    assert ancient.state is b.BatchLiveness.STALLED


def test_finished_batches_are_settled_no_matter_how_old():
    """跑完的批次不管多久以前跑完的都不是"卡住"。少了这一条,历史批次
    会集体挂上一个红标签 —— 而它们只是老。"""
    now = 10_000.0
    for status in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
        verdict = _live(status, created=0.0, started=0.0, progress=0.0, now=now)
        assert verdict.state is b.BatchLiveness.SETTLED, status
        assert verdict.advice is None


def test_a_finished_status_with_unfinished_items_is_not_settled():
    """**这是 A33 那轮漏掉的组合,也是本判定要修的那个场景的第二步。**

    `reset_items_for_retry()` 只把条目打回 PENDING,不动 `job.status`;
    全仓写 `job.status` 的只有 `run_batch()` 里两处。于是 Broker 挂着时:

        批次跑完(有失败)      status = COMPLETED_WITH_ERRORS
        运营点「重试」          条目回 PENDING,status 不变
        投递失败                没有任何人会去调 run_batch()

    只看 status 会返回 SETTLED —— 轮询完全停止,界面显示「已完成(有失败)」,
    而库里躺着一批永远不会被执行的 PENDING 条目。上一轮的 11 条用例全部围着
    QUEUED / RUNNING 转,一条都没覆盖"终态 status 配未完成条目"。
    """
    now = 10_000.0
    for status in ("COMPLETED", "COMPLETED_WITH_ERRORS"):
        verdict = _live(
            status,
            created=now - 100_000,
            started=now - 99_000,
            progress=now - 98_000,
            now=now,
            unfinished_items=3,
            requeued_at=now - 10,
        )
        assert verdict.state is not b.BatchLiveness.SETTLED, status
        assert verdict.state is b.BatchLiveness.LIVE, status


def test_a_retry_nobody_picked_up_is_reported_stalled():
    """重试之后一直没人认领 = 投递又失败了。必须报出来。

    这一条和"建好没人认领"是同一个成因(Broker 不可用),但它发生在
    运营**已经照着提示点过一次重试**之后 —— 那正是最需要提示的时刻。
    """
    now = 10_000.0
    verdict = _live(
        "COMPLETED_WITH_ERRORS",
        created=now - 500_000,
        started=now - 499_000,
        progress=now - 498_000,
        now=now,
        unfinished_items=2,
        requeued_at=now - b.BATCH_PICKUP_STALL_SECONDS - 1,
    )
    assert verdict.state is b.BatchLiveness.STALLED
    assert verdict.stalled_seconds is not None and verdict.stalled_seconds > 0
    assert "重试" in (verdict.advice or ""), "建议要指向一个真的能点的动作"


def test_the_retry_advice_does_not_say_nobody_ever_picked_it_up():
    """重试路径的建议不能复用「建好之后一直没有人认领」。

    对着一个刚被人点过重试的批次说"建好之后一直没有人认领",读起来像是
    系统根本没收到那次点击 —— 而运营的下一步会变成再建一个批次。
    """
    now = 10_000.0
    retried = _live(
        "COMPLETED_WITH_ERRORS",
        created=now - 500_000,
        now=now,
        unfinished_items=1,
        requeued_at=now - b.BATCH_PICKUP_STALL_SECONDS - 1,
    )
    never = _live(created=now - b.BATCH_PICKUP_STALL_SECONDS - 1, now=now)
    assert retried.advice and never.advice
    assert retried.advice != never.advice
    assert "建好" not in retried.advice


def test_an_old_batch_retried_just_now_is_not_immediately_accused():
    """参照时刻必须是"重排那一刻",不是建批次那一刻。

    按 `created_at` 算的话,一个三天前建的批次今天被重试会**立刻**报
    "停滞了三天" —— 而它刚刚才被点过。误报本身便宜,但一句在每次重试后
    立刻出现的警告会让运营学会忽略这条横幅,包括真的出问题那次。
    """
    now = 10_000.0
    verdict = _live(
        "COMPLETED_WITH_ERRORS",
        created=now - 3 * 86_400,
        started=now - 3 * 86_400,
        progress=now - 3 * 86_400,
        now=now,
        unfinished_items=5,
        requeued_at=now - 5,
    )
    assert verdict.state is b.BatchLiveness.LIVE


def test_the_retry_verdict_flips_exactly_at_the_pickup_threshold():
    """边界和认领那一档同一个口径:到点就算。"""
    now = 10_000.0

    def at(offset: int):
        return _live(
            "COMPLETED_WITH_ERRORS",
            created=now - 500_000,
            now=now,
            unfinished_items=1,
            requeued_at=now - b.BATCH_PICKUP_STALL_SECONDS + offset,
        )

    assert at(0).state is b.BatchLiveness.STALLED
    assert at(1).state is b.BatchLiveness.LIVE


def test_unfinished_statuses_are_exactly_the_complement_of_terminal():
    """"还没跑完"的定义只有一份,从终态反推出来。

    两份清单的分叉方式是安静的:加一个中间状态、只往终态那边看了一眼、
    忘了这边 —— 于是那个状态的条目会被当成"跑完了",终态 `job.status`
    重新单方面决定 SETTLED,而这正是本轮修掉的那个洞。
    """
    assert b.UNFINISHED_STATUSES == {
        b.BatchItemStatus.PENDING,
        b.BatchItemStatus.RUNNING,
    }
    assert not (b.UNFINISHED_STATUSES & b.TERMINAL_STATUSES)
    assert b.UNFINISHED_STATUSES | b.TERMINAL_STATUSES == set(b.BatchItemStatus), (
        "每个条目状态要么算跑完了要么算没跑完,不许有第三类 —— "
        "漏掉的那一个会被 liveness 当成已完成"
    )


def test_a_running_item_also_counts_as_unfinished():
    """判据是"还有没到终态的条目",不是"还有 PENDING"。

    上一次执行中途死掉会在库里留下 RUNNING 的行。只数 PENDING 的话,
    一个 status 终态 + 全是 RUNNING 残留的批次仍然会被判成 SETTLED。
    (调用方那边由 `_UNFINISHED_ITEM_STATUSES` 从终态集合反推,不另抄清单。)
    """
    now = 10_000.0
    verdict = _live(
        "COMPLETED",
        created=now - 100_000,
        now=now,
        unfinished_items=1,
        requeued_at=now - b.BATCH_PICKUP_STALL_SECONDS - 1,
    )
    assert verdict.state is b.BatchLiveness.STALLED


def test_without_the_item_facts_the_old_behaviour_is_kept():
    """不交条目那份事实时退回老行为(SETTLED)。

    默认值 0 的含义是"调用方没说",不是"确认没有未完成条目"。不知道的时候
    不喊"卡住了" —— 和未知 status 那一支同一条纪律:没有依据就不下判断。
    """
    now = 10_000.0
    verdict = _live("COMPLETED_WITH_ERRORS", created=0.0, started=0.0, progress=0.0, now=now)
    assert verdict.state is b.BatchLiveness.SETTLED


def test_the_reference_falls_back_when_there_is_no_requeue_stamp():
    """拿不到重排时刻时逐档退化,而不是直接放弃判定。

    老数据、或者条目 `updated_at` 读不出来时,退到最后落地 -> 开跑 -> 建批次。
    全都取不到才不判(LIVE)。
    """
    now = 10_000.0
    by_progress = _live(
        "COMPLETED_WITH_ERRORS",
        created=now - 500_000,
        started=now - 400_000,
        progress=now - b.BATCH_PICKUP_STALL_SECONDS - 1,
        now=now,
        unfinished_items=1,
    )
    assert by_progress.state is b.BatchLiveness.STALLED

    nothing_known = _live(
        "COMPLETED_WITH_ERRORS", created=None, started=None, progress=None, now=now,
        unfinished_items=1,
    )
    assert nothing_known.state is b.BatchLiveness.LIVE
    assert nothing_known.advice is None


def test_an_unknown_status_is_never_accused():
    """不认识的状态按 LIVE 处理(= 和今天一样继续轮询)。

    报 STALLED 的后果是对着一个没人见过的状态喊"可能没派出去",
    而那句话的下一步是"点重试" —— 建议一个不知道对不对的动作,
    比不给建议糟。
    """
    assert _live("SOMETHING_NEW", created=0.0, now=10_000.0).state is b.BatchLiveness.LIVE


def test_every_liveness_state_has_a_label():
    """标签表漏一个值的表现是 KeyError,而它只在那个状态真的出现时才炸。"""
    for state in b.BatchLiveness:
        assert b.BATCH_LIVENESS_LABELS.get(state), state


def test_the_progress_threshold_clears_the_longest_legal_item():
    """阈值必须大于单件最长合法耗时,否则正在正常跑的大批次会被报成卡住。

    模块里已经有一句 `assert`,但它在 import 时求值 —— 而 import 成功
    这件事不会有人特意去看。这一条让它变成一个会红的测试。
    """
    assert b.BATCH_PROGRESS_STALL_SECONDS > b.LONGEST_LEGAL_ITEM_SECONDS


def test_pickup_and_progress_thresholds_are_independent_knobs():
    """两档的误报代价不同(认领那档没有误杀风险,进度那档有),
    所以它们**不该**是同一个数。写成同一个数的话,下一个人调其中一个
    的时候会连带把另一个也调了。"""
    assert b.BATCH_PICKUP_STALL_SECONDS != b.BATCH_PROGRESS_STALL_SECONDS
    assert b.BATCH_PICKUP_STALL_SECONDS < b.BATCH_PROGRESS_STALL_SECONDS


def test_the_service_really_hands_the_item_facts_to_the_verdict():
    """判定层拿得到条目那份事实 —— 这条钉的是**接线**,不是判定本身。

    上面那批用例全部直接调 `b.liveness()`。它们证明了"给了未完成条目数
    就不会返回 SETTLED",但证明不了 `batch_service` 真的把那个数交上去了 ——
    而 A33 那轮的缺陷恰恰是判定和数据之间的一段接线:同一个响应里
    `counts.pending > 0`,而判定信了另一份事实。

    默认值 0 让漏传 `unfinished_items` 不会报错,只会静默退回旧行为。
    所以这里用 AST 找**真实的关键字实参**,不用字符串包含式断言。
    """
    fn = _func(APP / "workbench" / "batch_service.py", "_liveness_out")
    passed: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", "") != "liveness":
            continue
        passed |= {kw.arg for kw in node.keywords if kw.arg}
    assert passed, "_liveness_out 里找不到对 liveness() 的调用 —— 解析规则可能失效了"
    for name in ("unfinished_items", "requeued_at"):
        assert name in passed, (
            f"_liveness_out 没有把 {name} 交给判定层。它有默认值,"
            "所以漏传不会报错,只会让重试路径重新退回 SETTLED"
        )


def test_job_out_reads_the_items_once_and_shares_the_rows():
    """一次查询,两个用途。

    `_liveness_out` 收 ORM 行而不是 `item_dict()` 的产物。原来收 dict,于是
    要把 `finished_at` 从 ISO 字符串解析回 datetime,而当时那句注释把
    「我没重构」写成了「重构没用」。这一条让那次重构不会被悄悄改回去:
    `job_out` 走 `_items()`,不再走 `_item_dicts()`。
    """
    fn = _func(APP / "workbench" / "batch_service.py", "job_out")
    called = _call_names(fn)
    assert "_items" in called, "job_out 应该查一次 _items(),把行和 dict 一起分发"
    assert "_item_dicts" not in called, (
        "job_out 又去查了第二遍。`_item_dicts` 拿不到 ORM 行,"
        "活性判定就只能从 ISO 字符串解析回 datetime —— 那正是被修掉的写法"
    )
