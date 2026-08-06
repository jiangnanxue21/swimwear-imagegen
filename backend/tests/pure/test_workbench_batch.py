"""批量执行判定(阶段 3)。对着任务书 §6.3 的退出条件逐条测。

这批用例的取舍和 test_workbench_flow.py 一致:**不启动数据库、不建批次行**,
只测判定。执行器用一个假函数注入 —— "第 37 件炸了不影响前 36 件"这条
如果要真跑一次 50 件的批次才能验证,它就不会被验证。
"""
from __future__ import annotations

from app.workbench import batch as b
from app.workbench.batch import (
    BatchAction,
    BatchErrorCode,
    BatchItemStatus,
    BatchJobStatus,
    Candidate,
)
from app.workbench.flow import FlowStep, NextActionCode
from tests.pure._helpers import BACKEND_ROOT, expect_raises  # noqa: F401


def candidate(
    sku: str,
    *,
    spu: str = "SPU-1",
    action: NextActionCode = NextActionCode.EXPORT,
    fingerprint: str = "fp",
    step: FlowStep = FlowStep.DRAFT,
    ref: str | None = None,
) -> Candidate:
    return Candidate(
        product_id=f"id-{sku}",
        sku=sku,
        spu=spu,
        next_action=action,
        next_action_label=f"label-{action.value}",
        current_step=step,
        first_blocking_ref=ref,
        fingerprint=fingerprint,
    )


# ---------------------------------------------------------------- 异常码注册表


def test_every_error_code_is_registered_with_a_category_and_advice():
    """§6.3.1 第一条与第三条:预定义的码 + 运营能自己判断下一步。

    漏一个码的后果是界面上显示英文常量,且 `error_meta` 会把它当成
    UNEXPLAINED —— 于是一个本来可解释的失败会污染"不可解释数必须为 0"。
    """
    for code in BatchErrorCode:
        meta = b.ERROR_REGISTRY[code]
        assert meta.label, f"{code} 缺中文标签"
        assert meta.advice, f"{code} 缺下一步建议"
        assert isinstance(meta.category, FlowStep)


def test_only_unexplained_is_unexplainable():
    """"可解释"的判据只有一条,不能有第二个含糊的码混进来。"""
    for code in BatchErrorCode:
        if code is BatchErrorCode.UNEXPLAINED:
            assert not b.is_explainable(code)
        else:
            assert b.is_explainable(code), f"{code} 不该被判成不可解释"


def test_unknown_code_degrades_to_unexplained_not_to_a_crash():
    meta = b.error_meta("SOMETHING_NOBODY_DEFINED")
    assert meta is b.ERROR_REGISTRY[BatchErrorCode.UNEXPLAINED]
    assert not b.is_explainable("SOMETHING_NOBODY_DEFINED")


def test_needs_human_codes_are_never_machine_retryable():
    """重试"草稿缺必填字段"一百次结果相同,唯一效果是让运营以为系统在努力。"""
    for code, meta in b.ERROR_REGISTRY.items():
        if meta.needs_human:
            assert not meta.retryable, f"{code} 既要人工又说可重试"


def test_status_for_maps_the_three_kinds_apart():
    assert b.status_for(BatchErrorCode.SAME_SPU_DEDUPED) is BatchItemStatus.SKIPPED
    assert b.status_for(BatchErrorCode.DRAFT_FIELD_INVALID) is BatchItemStatus.NEEDS_HUMAN
    assert b.status_for(BatchErrorCode.EXTRACTION_FAILED) is BatchItemStatus.FAILED


def test_every_next_action_code_has_a_precheck_mapping():
    """前置不满足时必须给出一个具体的类别,不能落到泛化的 PRECHECK_MISMATCH。

    后端加一个动作码而这里不加映射,那一类"卡住"就会显示成
    "当前不该做这个动作"——技术上正确,但运营看不出该去哪儿。
    """
    for code in NextActionCode:
        assert code in b.PRECHECK_CODE_BY_ACTION, f"{code} 没有对应的异常类别"


def test_paid_actions_are_exactly_the_model_calling_ones():
    """付费动作清单错了,重试守卫就守错了对象。"""
    assert b.PAID_ACTIONS == {BatchAction.EXTRACT, BatchAction.GENERATE_COPY}


# ---------------------------------------------------------------- 计划


def test_plan_counts_executable_and_blocked_separately():
    """FE-301 的验收结果:执行前显示可执行 / 不可执行数量。"""
    plan = b.plan(
        BatchAction.EXPORT,
        [
            candidate("A", spu="S1", action=NextActionCode.EXPORT),
            candidate("B", spu="S2", action=NextActionCode.EXPORT),
            candidate(
                "C",
                spu="S3",
                action=NextActionCode.CONFIRM_ATTRIBUTES,
                step=FlowStep.ATTRIBUTE,
            ),
        ],
    )
    assert plan.executable_count == 2
    assert plan.blocked_count == 1
    assert plan.by_code == {BatchErrorCode.NO_CONFIRMED_ATTRIBUTES.value: 1}


def test_blocked_reason_names_the_products_own_next_step():
    """"不可执行"必须说清是被什么卡住的,否则运营只能逐个点开详情。"""
    plan = b.plan(
        BatchAction.EXPORT,
        [
            candidate(
                "A",
                action=NextActionCode.UPLOAD_MATERIAL,
                step=FlowStep.MATERIAL,
                ref="PRODUCT_FRONT",
            )
        ],
    )
    (item,) = plan.blocked
    assert item.code is BatchErrorCode.NO_USABLE_MATERIAL
    assert "补充素材" in item.reason or "label-UPLOAD_MATERIAL" in item.reason
    # 跳转目标是卡点所在的步骤,不是批量动作所在的步骤
    assert item.target_step is FlowStep.MATERIAL
    assert item.target_ref == "PRODUCT_FRONT"


def test_same_spu_is_executed_once_for_spu_scoped_actions():
    """同 SPU 的三个 SKU 批量生成文案:调一次模型,不是三次。

    这条就是 §6.3 最后一条"不重复触发付费模型调用"在计划阶段的形态。
    """
    plan = b.plan(
        BatchAction.GENERATE_COPY,
        [
            candidate("A-S", spu="SAME", action=NextActionCode.GENERATE_COPY),
            candidate("A-M", spu="SAME", action=NextActionCode.GENERATE_COPY),
            candidate("A-L", spu="SAME", action=NextActionCode.GENERATE_COPY),
        ],
    )
    assert plan.executable_count == 1
    assert plan.blocked_count == 2
    assert set(plan.by_code) == {BatchErrorCode.SAME_SPU_DEDUPED.value}


def test_extraction_is_per_sku_so_the_same_spu_is_not_deduped():
    """属性识别是逐图跑的,而素材挂在 SKU 上 —— 去重会漏掉两个 SKU。"""
    plan = b.plan(
        BatchAction.EXTRACT,
        [
            candidate("A-S", spu="SAME", action=NextActionCode.RUN_EXTRACTION),
            candidate("A-M", spu="SAME", action=NextActionCode.RUN_EXTRACTION),
        ],
    )
    assert plan.executable_count == 2


def test_precheck_runs_before_dedupe():
    """先判前置再去重。反了的话,留下的那件恰好不可执行时整个 SPU 什么也不做。"""
    plan = b.plan(
        BatchAction.GENERATE_COPY,
        [
            # 第一件卡在属性上
            candidate(
                "A-S",
                spu="SAME",
                action=NextActionCode.CONFIRM_ATTRIBUTES,
                step=FlowStep.ATTRIBUTE,
            ),
            # 第二件可以做 —— 它必须被留下来执行
            candidate("A-M", spu="SAME", action=NextActionCode.GENERATE_COPY),
        ],
    )
    assert plan.executable_count == 1
    assert plan.executable[0].sku == "A-M"


def test_already_exported_is_skipped_not_failed():
    plan = b.plan(
        BatchAction.EXPORT, [candidate("A", action=NextActionCode.DONE)]
    )
    (item,) = plan.blocked
    assert item.code is BatchErrorCode.ALREADY_EXPORTED
    assert b.status_for(item.code) is BatchItemStatus.SKIPPED


def test_include_exported_lets_a_done_product_through():
    """平台那边上传失败要重导,这是真实需求 —— 但必须显式勾选。"""
    default = b.plan(BatchAction.EXPORT, [candidate("A", action=NextActionCode.DONE)])
    assert default.executable_count == 0
    assert default.blocked[0].code is BatchErrorCode.ALREADY_EXPORTED

    opted_in = b.plan(
        BatchAction.EXPORT,
        [candidate("A", action=NextActionCode.DONE)],
        include_exported=True,
    )
    assert opted_in.executable_count == 1, "勾了「包含已导出」却还是不执行"


def test_generate_copy_refuses_to_overwrite_human_edited_copy():
    """已有文案待批准时批量生成必须让路 —— 那是人的工作成果。"""
    for action in (NextActionCode.APPROVE_COPY, NextActionCode.FIX_COPY):
        plan = b.plan(
            BatchAction.GENERATE_COPY,
            [candidate("A", action=action, step=FlowStep.COPY)],
        )
        assert plan.executable_count == 0, f"{action} 竟然允许批量覆盖文案"


def test_validate_draft_accepts_build_fix_and_refresh():
    """重建草稿是幂等的且保留手填字段,没必要让运营分三个按钮点。"""
    for action in (
        NextActionCode.BUILD_DRAFT,
        NextActionCode.FIX_DRAFT,
        NextActionCode.REFRESH_DRAFT,
    ):
        plan = b.plan(
            BatchAction.VALIDATE_DRAFT, [candidate("A", action=action)]
        )
        assert plan.executable_count == 1, f"{action} 应当允许重建草稿"


# ---------------------------------------------------------------- 幂等键


def test_same_inputs_give_the_same_idempotency_key():
    a = candidate("A", fingerprint="media:1|media:2")
    b_ = candidate("A", fingerprint="media:1|media:2")
    assert b.idempotency_key(BatchAction.EXTRACT, a) == b.idempotency_key(
        BatchAction.EXTRACT, b_
    )


def test_changed_upstream_changes_the_key():
    """幂等不等于"永远只做一次":上游变了就该重新执行。"""
    before = candidate("A", fingerprint="media:1")
    after = candidate("A", fingerprint="media:1|media:2")
    assert b.idempotency_key(BatchAction.EXTRACT, before) != b.idempotency_key(
        BatchAction.EXTRACT, after
    )


def test_different_actions_never_share_a_key():
    same = candidate("A", fingerprint="fp")
    keys = {b.idempotency_key(a, same) for a in BatchAction}
    assert len(keys) == len(list(BatchAction))


def test_spu_scoped_key_is_shared_across_skus_of_one_spu():
    """同 SPU 的两个 SKU 算出同一个键 —— 于是跨批次也只会真的执行一次。"""
    left = candidate("A-S", spu="SAME", fingerprint="fp")
    right = candidate("A-M", spu="SAME", fingerprint="fp")
    assert b.idempotency_key(BatchAction.GENERATE_COPY, left) == b.idempotency_key(
        BatchAction.GENERATE_COPY, right
    )
    # 而按 SKU 的动作必须不同
    assert b.idempotency_key(BatchAction.EXTRACT, left) != b.idempotency_key(
        BatchAction.EXTRACT, right
    )


# ---------------------------------------------------------------- 执行循环


def _plan_of(n: int, action: BatchAction = BatchAction.EXTRACT) -> b.BatchPlan:
    return b.plan(
        action,
        [
            candidate(f"S{i}", spu=f"SPU{i}", action=NextActionCode.RUN_EXTRACTION)
            for i in range(n)
        ],
    )


def test_one_exploding_item_does_not_stop_the_batch():
    """§6.3 第三条。**这条测试是那个退出条件的唯一自动化证据。**"""
    plan = _plan_of(5)
    seen: list[str] = []

    def execute(item):
        seen.append(item.sku)
        if item.sku == "S2":
            raise RuntimeError("boom")
        return b.ExecResult(ok=True, paid_calls=1)

    outcomes = b.run_plan(plan, execute)
    assert seen == ["S0", "S1", "S2", "S3", "S4"], "循环在失败那一件之后停了"
    counts = b.count(outcomes)
    assert counts.succeeded == 4
    assert counts.failed == 1


def test_an_unmapped_exception_is_counted_as_unexplained():
    """裸 RuntimeError 没有异常码,必须落进那个必须为 0 的计数器里。"""
    plan = _plan_of(1)

    def execute(item):
        raise RuntimeError("数据库连接被重置")

    counts = b.count(b.run_plan(plan, execute))
    assert counts.failed == 1
    assert counts.unexplained == 1
    assert counts.explainable_rate == 0.0


def test_a_mapped_failure_is_explainable():
    plan = _plan_of(1)

    def execute(item):
        return b.ExecResult(
            ok=False, code=BatchErrorCode.EXTRACTION_FAILED, message="provider 超时"
        )

    counts = b.count(b.run_plan(plan, execute))
    assert counts.failed == 1
    assert counts.unexplained == 0
    assert counts.explainable_rate == 1.0


def test_needs_human_counts_as_a_correct_outcome_not_a_failure():
    """V1.1 特意澄清的口径:异常件的正确终态是被阻断,不是导出成功。"""
    plan = _plan_of(2)
    calls = iter([True, False])

    def execute(item):
        if next(calls):
            return b.ExecResult(ok=True)
        return b.ExecResult(
            ok=False,
            code=BatchErrorCode.DRAFT_FIELD_INVALID,
            message="price 缺失",
            target_ref="price",
        )

    outcomes = b.run_plan(plan, execute)
    counts = b.count(outcomes)
    assert counts.succeeded == 1
    assert counts.needs_human == 1
    assert counts.failed == 0
    # 成功率只看真正被尝试的件数,需人工不算失败但也不算成功
    assert counts.success_rate == 0.5
    assert counts.explainable_rate == 1.0


def test_failed_item_keeps_the_field_level_jump_target():
    """§6.3.1 第二条:一键跳到具体字段。ref 丢了就只能跳到标签页。"""
    plan = _plan_of(1)

    def execute(item):
        return b.ExecResult(
            ok=False,
            code=BatchErrorCode.DRAFT_FIELD_INVALID,
            message="price 缺失",
            target_step=FlowStep.DRAFT,
            target_ref="price",
        )

    (outcome,) = b.run_plan(plan, execute)
    assert outcome.target_ref == "price"
    assert outcome.target_step is FlowStep.DRAFT


def test_blocked_items_are_not_executed_at_all():
    """不可执行的条目不该进执行器 —— 进去了就是一次白花的付费调用。"""
    plan = b.plan(
        BatchAction.EXPORT,
        [
            candidate("A", spu="S1", action=NextActionCode.EXPORT),
            candidate("B", spu="S2", action=NextActionCode.UPLOAD_MATERIAL),
        ],
    )
    executed: list[str] = []

    def execute(item):
        executed.append(item.sku)
        return b.ExecResult(ok=True)

    outcomes = b.run_plan(plan, execute)
    assert executed == ["A"]
    counts = b.count(outcomes)
    assert counts.succeeded == 1
    assert counts.needs_human == 1  # 缺素材要人处理
    assert counts.paid_calls == 0


def test_on_outcome_is_called_once_per_item_in_order():
    """service 靠这个钩子逐件落库。少调一次就有一条记录永远停在 PENDING。"""
    plan = _plan_of(3)
    order: list[str] = []
    outcomes = b.run_plan(
        plan,
        lambda item: b.ExecResult(ok=True),
        on_outcome=lambda o: order.append(o.item.sku),
    )
    assert order == ["S0", "S1", "S2"]
    assert len(outcomes) == 3


def test_reused_results_report_zero_paid_calls():
    plan = _plan_of(1)

    def execute(item):
        return b.ExecResult(ok=True, reused=True, paid_calls=0, message="复用")

    counts = b.count(b.run_plan(plan, execute))
    assert counts.paid_calls == 0
    assert counts.reused == 1


# ---------------------------------------------------------------- 汇总与重试


def test_job_status_never_says_the_whole_batch_failed():
    """§6.3 第三条的另一半:批次不因单件失败而变成失败状态。"""
    counts = b.BatchCounts(total=10, succeeded=8, failed=2)
    assert b.job_status(counts) is BatchJobStatus.COMPLETED_WITH_ERRORS
    assert b.job_status(b.BatchCounts(total=10, succeeded=10)) is BatchJobStatus.COMPLETED
    assert b.job_status(b.BatchCounts(total=10, pending=3, succeeded=7)) is BatchJobStatus.RUNNING


def test_success_rate_is_none_when_nothing_was_attempted():
    """全部被跳过时返回 None 而不是 0 —— 0% 会被读成"全失败"。"""
    assert b.BatchCounts(total=5, skipped=5).success_rate is None
    assert b.BatchCounts(total=5, skipped=5).explainable_rate is None


def test_skipped_items_do_not_dilute_the_rates():
    """A 批次 20 件里 5 件同 SPU 被跳过,成功率不该因此变成 75%。"""
    counts = b.BatchCounts(total=20, succeeded=15, skipped=5)
    assert counts.attempted == 15
    assert counts.success_rate == 1.0


def test_count_rows_reads_the_shape_the_database_stores():
    rows = [
        {"status": "SUCCEEDED", "paid_calls": 1},
        {"status": "FAILED", "error_code": "EXTRACTION_FAILED"},
        {"status": "FAILED", "error_code": "UNEXPLAINED"},
        {"status": "NEEDS_HUMAN", "error_code": "DRAFT_FIELD_INVALID"},
        {"status": "SKIPPED", "error_code": "SAME_SPU_DEDUPED", "reused": False},
    ]
    counts = b.count_rows(rows)
    assert (counts.succeeded, counts.failed, counts.needs_human, counts.skipped) == (
        1,
        2,
        1,
        1,
    )
    assert counts.unexplained == 1
    assert counts.paid_calls == 1


def test_retryable_rows_leaves_success_and_human_items_alone():
    """FE-304:重试只挑该重试的。成功项被重试就是重复付费。"""
    rows = [
        {"id": "1", "status": "SUCCEEDED"},
        {"id": "2", "status": "FAILED", "error_code": "EXTRACTION_FAILED"},
        {"id": "3", "status": "NEEDS_HUMAN", "error_code": "DRAFT_FIELD_INVALID"},
        {"id": "4", "status": "FAILED", "error_code": "DRAFT_STALE"},
        {"id": "5", "status": "SKIPPED", "error_code": "SAME_SPU_DEDUPED"},
    ]
    assert b.retryable_rows(rows) == ["2", "4"]


# ---------------------------------------------------------------- 异常分类


def test_classify_reads_the_message_fragments_service_actually_raises():
    """`MESSAGE_HINTS` 里的片段必须真的出现在 service 的 raise 处。

    改一句提示语而忘了这里,分类会静默退化成 UNEXPLAINED —— 而那个数字
    是签字门槛,退化了没人会立刻发现。所以用一次源码 grep 钉住它。
    """
    sources = "".join(
        (BACKEND_ROOT / "app" / "workbench" / name).read_text(encoding="utf-8")
        for name in ("service.py", "batch_service.py")
    )
    sources += (BACKEND_ROOT / "app" / "attributes" / "service.py").read_text(
        encoding="utf-8"
    )
    for fragment, _code in b.MESSAGE_HINTS:
        assert fragment in sources, f"没有任何 raise 会产生片段 {fragment!r},该删了"


def test_classify_prefers_a_specific_code_over_the_fallback():
    class FakeError(Exception):
        code = "INPUT_INVALID"
        message = "草稿已过期,禁止导出:已确认属性 primary_color 变了。请重新生成草稿"

    code, message = b.classify_exception(FakeError())
    assert code is BatchErrorCode.DRAFT_STALE
    assert "primary_color" in message


def test_classify_uses_the_error_code_when_the_message_says_nothing():
    class FakeError(Exception):
        code = "ATTRIBUTE_EXTRACTION_FAILED"
        message = "识别没成"

    code, _ = b.classify_exception(FakeError())
    assert code is BatchErrorCode.EXTRACTION_FAILED


def test_classify_falls_back_to_unexplained_and_keeps_the_message():
    code, message = b.classify_exception(RuntimeError("连接被重置"))
    assert code is BatchErrorCode.UNEXPLAINED
    assert message == "连接被重置"


def test_action_labels_cover_every_action_and_status():
    """界面上任何一个动作或状态都不该显示英文常量。"""
    assert set(b.BATCH_ACTION_LABELS) == set(BatchAction)
    assert set(b.BATCH_ITEM_STATUS_LABELS) == set(BatchItemStatus)
    assert set(b.BATCH_JOB_STATUS_LABELS) == set(BatchJobStatus)
    assert set(b.ACTION_SCOPE) == set(BatchAction)
    assert set(b.ACTION_PRECONDITION) == set(BatchAction)


# ---------------------------------------------------------------- 并发付费守卫(阶段 4)


def test_action_step_covers_every_action():
    """`CONCURRENT_IN_FLIGHT` 的归类靠这张表。缺一项就会 KeyError 在执行路径上。"""
    assert set(b.ACTION_STEP) == set(BatchAction)
    for action, step in b.ACTION_STEP.items():
        assert isinstance(step, FlowStep), f"{action} 的步骤不是 FlowStep"


def test_concurrency_failure_is_explainable_and_retryable():
    """§6.3.2 两条门槛同时压在这个码上。

    不可解释 -> 签字过不去;不可重试 -> 那一件永远不会被做。
    两者缺一,这个守卫就从"防重复付费"变成"制造一件永远做不成的商品"。
    """
    meta = b.ERROR_REGISTRY[BatchErrorCode.CONCURRENT_IN_FLIGHT]
    assert b.is_explainable(BatchErrorCode.CONCURRENT_IN_FLIGHT)
    assert meta.retryable, "拿不到锁的那一件必须能重试"
    assert not meta.needs_human, "这不需要人做决定,只需要重跑一次"
    assert not meta.skip, "跳过项不进分母也拿不到重试按钮,这件事就静悄悄地不做了"
    assert b.status_for(BatchErrorCode.CONCURRENT_IN_FLIGHT) is BatchItemStatus.FAILED


def test_concurrency_error_code_maps_structurally_not_by_message():
    """走异常路径时也要落同一个码。

    消息片段匹配靠的是提示语原文,改一个字就退化成 UNEXPLAINED。
    这个码有自己的 ErrorCode,所以钉住结构化那条路。
    """
    class FakeError(Exception):
        code = "CONCURRENT_EXECUTION"
        message = "同一商品正被另一个请求执行"

    code, _ = b.classify_exception(FakeError())
    assert code is BatchErrorCode.CONCURRENT_IN_FLIGHT


def test_the_lock_is_taken_before_the_receipt_is_read():
    """静态纪律:`_execute` 里 advisory lock 必须在查回执**之前**。

    先查后锁的话,查与锁之间还是一个窗口,这个守卫等于没加 —— 而它加没加
    在功能测试里看不出来:两个请求真的撞上才会表现出来,那种测试要真库真并发。
    所以用一次 AST 顺序检查钉住它。

    同时钉住"锁的键是幂等键":锁在 scope_key 上会让不同上游指纹的两件
    互相阻塞,锁在 product_id 上则挡不住同 SPU 的跨 SKU 重复。
    """
    import ast

    source = (BACKEND_ROOT / "app" / "workbench" / "batch_service.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_execute"
    )

    lock_line = receipt_line = None
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name == "try_advisory_xact_lock" and lock_line is None:
            lock_line = node.lineno
            args = [a for a in node.args if isinstance(a, ast.Attribute)]
            assert any(
                a.attr == "idempotency_key" for a in args
            ), "锁的键必须是幂等键本身,不是 scope_key 也不是 product_id"
        elif name == "_receipt" and receipt_line is None:
            receipt_line = node.lineno

    assert lock_line is not None, "_execute 里没有 advisory lock,§7.3 的 P0 是开着的"
    assert receipt_line is not None, "_execute 里没有查回执"
    assert lock_line < receipt_line, "锁必须在查回执之前,否则中间仍有竞态窗口"


# 这里原来还有一条 `test_the_receipt_writer_no_longer_claims_the_race_is_unhandled`,
# 断言 `batch_service.py` 里不再出现"那是 BE-206 之后的事"这句注释。已删。
#
# 它钉的是一句**中文注释的字面量**:注释删掉的那天起它就永远绿,
# 再也不可能因为任何行为回归变红 —— 一条零信息量的用例,却要每次全量跑。
# `HANDOVER.md` 门禁那节自己写过这个批评:「源码字符串匹配钉的是
# 『这行字还在』,不是『这个行为还对』」。
#
# 删它是安全的:那句注释早已不在 `batch_service.py` 里(删除前确认过),
# 所以它是一条**已经完成使命、只剩空转**的一次性守卫。
#
# 注意它和下面那条守的**不是同一件事**:它守的是"注释别再说谎",
# 下面那条 `test_the_lock_is_taken_before_the_receipt_is_read` 守的是
# advisory lock 与查回执的先后(AST 比行号)。前者没有替代品 ——
# 注释与代码是否一致本来就不该靠字符串黑名单来守,那只挡得住已知的那一句。
# 后者才是这段代码真正的防线,它还在。


# ==========================================================
# 历史批次文件是否还能传平台(OPS-REVIEW P4)
# ==========================================================


def _row(**over):
    base = {
        "product_id": "p-1",
        "sku": "SW-001-BLK-S",
        "spu": "SW-001",
        "exported_fingerprint": "f" * 32,
        "current_fingerprint": "f" * 32,
        "draft_exists": True,
        "draft_stale": False,
    }
    base.update(over)
    return base


def test_every_file_state_has_a_label_and_an_advice():
    """漏一个,横幅下面那张表就会显示英文常量,而那张表是运营唯一的行动依据。"""
    for state in b.FileState:
        assert state in b.FILE_STATE_LABELS, f"{state} 缺中文标签"
        assert state in b.FILE_STATE_ADVICE, f"{state} 缺处理建议"


def test_unchanged_draft_means_the_file_is_still_good():
    assert b.file_state(
        exported_fingerprint="a" * 32,
        current_fingerprint="a" * 32,
        draft_exists=True,
        draft_stale=False,
    ) is b.FileState.FRESH


def test_rebuilt_draft_makes_the_old_file_superseded():
    assert b.file_state(
        exported_fingerprint="a" * 32,
        current_fingerprint="b" * 32,
        draft_exists=True,
        draft_stale=False,
    ) is b.FileState.SUPERSEDED


def test_missing_draft_wins_over_everything():
    assert b.file_state(
        exported_fingerprint="a" * 32,
        current_fingerprint=None,
        draft_exists=False,
        draft_stale=True,
    ) is b.FileState.DRAFT_MISSING


def test_stale_wins_over_superseded_because_the_draft_must_be_rebuilt_first():
    """两者同时成立时,先说"重新生成草稿"。

    反过来(先说"重新导一次就行")会让运营点导出、撞上 §4.5.1 的导出闸,
    再回来看这张表 —— 一次本可避免的错误。
    """
    state = b.file_state(
        exported_fingerprint="a" * 32,
        current_fingerprint="b" * 32,
        draft_exists=True,
        draft_stale=True,
    )
    assert state is b.FileState.UPSTREAM_STALE
    assert "重新生成草稿" in b.FILE_STATE_ADVICE[state]


def test_missing_fingerprint_on_either_side_is_unknown_not_a_guess():
    """老批次没留指纹。判成"有效"是撒谎,判成"过期"是虚报,只能说不知道。"""
    assert b.file_state(
        exported_fingerprint=None,
        current_fingerprint="a" * 32,
        draft_exists=True,
        draft_stale=False,
    ) is b.FileState.UNKNOWN
    assert b.file_state(
        exported_fingerprint="a" * 32,
        current_fingerprint=None,
        draft_exists=True,
        draft_stale=False,
    ) is b.FileState.UNKNOWN
    assert b.file_state(
        exported_fingerprint="",
        current_fingerprint="  ",
        draft_exists=True,
        draft_stale=False,
    ) is b.FileState.UNKNOWN


def test_unknown_is_not_counted_as_stale():
    audit = b.audit_file_states([_row(exported_fingerprint=None)])
    assert audit.stale_count == 0
    assert audit.unknown_count == 1
    assert not audit.usable, "不确定的包不能宣布可用"


def test_audit_counts_split_by_state():
    audit = b.audit_file_states(
        [
            _row(product_id="p-1"),
            _row(product_id="p-2", current_fingerprint="b" * 32),
            _row(product_id="p-3", draft_stale=True),
            _row(product_id="p-4", draft_exists=False, current_fingerprint=None),
        ]
    )
    assert audit.total == 4
    assert audit.stale_count == 3
    by_state = audit.by_state
    assert len(by_state[b.FileState.FRESH]) == 1
    assert len(by_state[b.FileState.SUPERSEDED]) == 1
    assert len(by_state[b.FileState.UPSTREAM_STALE]) == 1
    assert len(by_state[b.FileState.DRAFT_MISSING]) == 1


def test_all_fresh_batch_is_usable_and_says_nothing():
    audit = b.audit_file_states([_row(product_id="p-1"), _row(product_id="p-2")])
    assert audit.usable
    assert not audit.blocks_redownload
    assert b.file_audit_banner(audit) is None, "一切正常时不该有横幅"


def test_empty_batch_says_nothing():
    audit = b.audit_file_states([])
    assert audit.total == 0
    assert not audit.usable
    assert b.file_audit_banner(audit) is None


def test_banner_names_the_count_and_tells_them_not_to_upload():
    audit = b.audit_file_states(
        [_row(product_id="p-1"), _row(product_id="p-2", current_fingerprint="b" * 32)]
    )
    banner = b.file_audit_banner(audit)
    assert banner is not None
    assert "1 件" in banner and "别再传平台" in banner


def test_banner_warns_when_the_batch_can_no_longer_be_redownloaded():
    """过期草稿会让 batch_file 的导出闸 409。横幅要在点击前说这件事。"""
    audit = b.audit_file_states([_row(draft_stale=True)])
    assert audit.blocks_redownload
    assert "不能整包重新下载" in (b.file_audit_banner(audit) or "")


def test_superseded_only_batch_can_still_be_redownloaded():
    audit = b.audit_file_states([_row(current_fingerprint="b" * 32)])
    assert not audit.blocks_redownload
    assert "重新导出一次即可" in (b.file_audit_banner(audit) or "")


def test_blocking_states_are_exactly_the_ones_the_export_gate_rejects():
    """导出闸拒的是"草稿不存在"与"草稿过期",别把"被新版取代"也算进去 ——
    那种情况下重新下载是完全可行的,拦住它等于凭空少一条出路。
    """
    assert b.BLOCKS_REDOWNLOAD == frozenset(
        {b.FileState.DRAFT_MISSING, b.FileState.UPSTREAM_STALE}
    )
