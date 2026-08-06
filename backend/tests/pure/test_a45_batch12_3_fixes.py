"""A45 batch12-3:对 batch12-2 那一轮的四条回头修的守卫。

batch12-2 修的六条(EX-01 ~ EX-06)方向都对,但有四处**论证或收尾没走完**,
而它们各自的失败方向都指回同一件事:钱。

    1  EX-02 的"0 次"只在标记写成功时成立。`_mark_provider_call` 的返回值
       被丢掉了,而 `_execute` 传给 `receipt_route` 的 `call_dispatched` 是
       显式算的 —— 标记写失败之后的付费调用会留下一条"没有标记的 IN_FLIGHT",
       下一次回收把它读成「可证明没花钱」,自动重跑。而且那一支刻意不消耗
       付费额度,于是这条路连 `MAX_BILLED_EXECUTIONS`(=2)都不再兜底,
       只剩 `MAX_ITEM_ATTEMPTS`(=3)。**没有兜住,是退了一档。**

    2  两条新守卫的断言比它们自己的 docstring 弱一个量级,分不出实现和
       实现的反面。已就地补强(见 `test_a45_batch12_2_fixes.py`),
       这里只钉住"不许退回字符串包含"。

    3  EX-06 在产品里没有入口:后端认 `Idempotency-Key`,而前端一个字都没发。
       0032 的 docstring 点名的那个场景(运营双击 -> 两个 BatchJob)
       在界面上照样能复现。

    4  EX-03 停在下载那一步之前。`_persist_candidates` 崩了仍然裸 `raise`,
       外层落 `INTERNAL_ERROR`,`retry_task` 掉进最后那个 `else` 清 ID 回
       QUEUED —— 而那时 `fetch_results` 已经返回,钱花了、图也生成了。

这份守卫沿用上一轮的做法:能穷举的直接调,剩下的用 AST 钉结构。真正的
行为验证仍然在 `docs/REVIEW-A45-BATCH12-3.md` 里那张 CI 表上。
"""
from __future__ import annotations

import ast

from app.core.enums import PROVIDER_RESULT_PENDING_CODE
from app.workbench.batch import (
    MAX_BILLED_EXECUTIONS,
    BatchAction,
    ReceiptRoute,
    ReceiptStatus,
    receipt_route,
)
from tests.pure._helpers import BACKEND_ROOT

BATCH = BACKEND_ROOT / "app" / "workbench" / "batch.py"
BATCH_SERVICE = BACKEND_ROOT / "app" / "workbench" / "batch_service.py"
GENERATION_TASKS = BACKEND_ROOT / "app" / "tasks" / "generation_tasks.py"
GUARDS_12_2 = BACKEND_ROOT / "tests" / "pure" / "test_a45_batch12_2_fixes.py"
FRONTEND = BACKEND_ROOT.parent / "frontend" / "src"


def _fn(path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到 {name} —— 改名了就把这份守卫一起改")


# ==========================================================
# 1:标记写不下去的时候,不许调付费模型
# ==========================================================


def test_the_marker_write_reports_a_missing_row_as_failure():
    """UPDATE 匹配到 0 行和 UPDATE 抛错是同一个结果:标记没落下。

    原来只看有没有抛异常,`rowcount == 0` 照样返回 True。正常路径上
    `_claim_in_flight` 保证了那一行存在,所以这不是当下的 bug;它是
    **把两件事混成一个返回值**,而这个文件别处(`usage_dict` 的
    attempted / billable、`_settle_unbilled_failure` 的 paid_calls)
    正在反复拆的就是这种混淆。
    """
    fn = _fn(BATCH_SERVICE, "_mark_provider_call")
    dump = ast.dump(fn)
    assert "rowcount" in dump, (
        "`_mark_provider_call` 没看 rowcount —— 一次没匹配到行的 UPDATE "
        "会被当成\"标记写好了\",而调用方正准备照着它去花钱"
    )


def test_the_ceiling_still_covers_the_only_path_it_still_owns():
    """`MAX_BILLED_EXECUTIONS` 的适用范围收窄了,但没有被架空。

    IN_FLIGHT 不再走它(有标记就停、没标记就免费重跑),剩下的一条是
    `FAILED_BILLED` + 不可离线复校的动作。这条穷举钉住那一条还在。
    """
    assert (
        receipt_route(
            ReceiptStatus.FAILED_BILLED.value,
            BatchAction.EXTRACT,
            executions=MAX_BILLED_EXECUTIONS,
        )
        is ReceiptRoute.NEEDS_RECONCILIATION
    ), "已知付费的失败烧完额度之后必须停下"
    assert (
        receipt_route(
            ReceiptStatus.FAILED_BILLED.value,
            BatchAction.EXTRACT,
            executions=1,
        )
        is ReceiptRoute.EXECUTE_AGAIN_BILLED
    ), "还有额度时不该提前转人工 —— 那一档的代价是每次抖动都开工单"


def test_an_unmarked_in_flight_receipt_is_not_bounded_by_the_ceiling():
    """免费重跑那一支**刻意不消耗额度**,这条钉住它确实不受上限约束。

    它同时解释了为什么第 1 条必须修:一旦"没有标记"不再等于"没有调用",
    这一支就是一条没有上限的自动付费路径。
    """
    for executions in (1, MAX_BILLED_EXECUTIONS, MAX_BILLED_EXECUTIONS + 5):
        assert (
            receipt_route(
                ReceiptStatus.IN_FLIGHT.value,
                BatchAction.EXTRACT,
                executions=executions,
                call_dispatched=False,
            )
            is ReceiptRoute.EXECUTE
        ), (
            "可证明没花钱的重跑被付费额度锁住了 —— 那个额度记的是"
            "\"开跑过几次\",不是\"花过几次钱\""
        )


def test_the_route_docstring_still_says_the_default_protects_nobody():
    """`call_dispatched` 的默认值 True 保护不了真正的调用方。

    `_execute` 是显式传的,所以默认值在那条路上从不生效。这句话必须留在
    `receipt_route` 的 docstring 里 —— 它被删掉之后,下一个人会照着
    "有默认值兜着"重新把 `_mark_provider_call` 的返回值丢掉一次。
    """
    fn = _fn(BATCH, "receipt_route")
    doc = ast.get_docstring(fn) or ""
    assert "_execute" in doc and "显式" in doc, (
        "receipt_route 的 docstring 不再说明默认值保护不了显式调用方 —— "
        "那正是上一轮把返回值丢掉的理由"
    )


# ==========================================================
# 2:补强过的两条守卫不许退回字符串包含
# ==========================================================


def test_the_marker_guards_assert_behaviour_not_spelling():
    """上一轮那两条守卫已经补强,这条挡住它们被改回去。

        顺序守卫    要比行号,不是查两个名字都在
        清标记守卫  要断言写进去的是 None 字面量,不是查列名出现过

    两条原来的写法都能被"实现的反面"通过 —— 分别是「标记挪到执行器之后」
    和「把 None 改成 utc_now()」。
    """
    order = ast.dump(_fn(GUARDS_12_2, "test_the_marker_is_written_before_the_paid_executors"))
    assert "lineno" in order, "顺序守卫退回成了名字包含,它验证不了顺序"

    clears = _fn(GUARDS_12_2, "test_reclaiming_a_receipt_clears_the_previous_call_marker")
    dump = ast.dump(clears)
    assert "Dict" in dump and "is None" in ast.unparse(clears), (
        "清标记守卫退回成了字符串包含 —— 把 None 改成 utc_now() 它照样绿"
    )


# ==========================================================
# 3:EX-06 必须能从界面走到
# ==========================================================


def test_the_frontend_actually_sends_the_idempotency_key():
    """后端从 batch12-2 起就认这个头,而前端一个字都没发。

    没有这一条的话,0032 那条迁移的 docstring 里点名的场景
    (运营双击 -> 两个 BatchJob、两套条目、两份进度)在界面上照样能复现,
    而仓库里所有测试都是绿的。
    """
    api = (FRONTEND / "api" / "batch.ts").read_text(encoding="utf-8")
    assert "Idempotency-Key" in api, "前端建批次没有发幂等键,EX-06 在产品里够不到"

    bar = (FRONTEND / "components" / "workbench" / "BatchActionBar.tsx").read_text(
        encoding="utf-8"
    )
    assert "requestKey" in bar, "调用方没有传键 —— api 层支持了但没人用"
    assert "newRequestKey" in bar, "键不是生成的"


def test_the_key_is_minted_per_click_not_derived_from_the_request():
    """键必须每次点击一把随机的,**不许**按请求内容算。

    `batch_jobs.request_key` 上的唯一约束是全局的,不按操作人分。按内容算
    的话,两个运营先后选中同一批商品做同一个动作会算出同一把键 —— 第二个人
    静默拿到第一个人的批次,而他要做的那件事从来没发生过。界面上一切正常。
    那正是 EX-06 反对的静默。
    """
    helper = (FRONTEND / "utils" / "requestKey.ts").read_text(encoding="utf-8")
    assert "randomUUID" in helper, "键不是随机生成的"
    for derived in ("action", "product_ids", "productIds", "sha", "hash"):
        assert derived not in helper, (
            f"键的生成用到了 {derived} —— 按请求内容算的键会让两个运营撞在一起"
        )

    bar = (FRONTEND / "components" / "workbench" / "BatchActionBar.tsx").read_text(
        encoding="utf-8"
    )
    # 换计划要换键(旧键 + 新入参 = 一个没人看得懂的 409),
    # 提交成功要清键(下一次点击是新的一件事)
    assert bar.count("pendingKey.current = null") >= 2, (
        "幂等键的生命周期不完整:换计划和提交成功这两处都必须清掉它"
    )


def test_an_over_long_key_is_rejected_rather_than_truncated():
    """截断会把两把只有前缀相同的键变成同一把。

    表现是第二次提交拿到一个 409,而那个 409 说的是"这把键已经用在另一组
    入参上了" —— 没有任何人能从这句话推出"你的键太长了"。静默改写调用方
    给的标识符,和 EX-06 反对的静默返回原批次是同一类事。
    """
    fn = _fn(BATCH_SERVICE, "create_batch")
    src = ast.unparse(fn)
    assert "[:64]" not in src, "request_key 仍然在被截断"
    assert "len(key) > 64" in src, "超长的键没有被拒绝"


def test_a_replayed_request_is_visible_to_the_operator():
    """重发返回的 `counts` 是**上一次**的执行结果,界面必须说出来。

    不说的话运营看到的是一条和平时一模一样的"已执行 N 件",而那 N 件
    是上一次的 —— 他会以为刚刚又跑了一遍,然后去找那笔并不存在的费用。
    """
    bar = (FRONTEND / "components" / "workbench" / "BatchActionBar.tsx").read_text(
        encoding="utf-8"
    )
    assert "reused_request" in bar, "界面没有区分\"重发\"和\"又跑了一次\""
    api = (FRONTEND / "api" / "batch.ts").read_text(encoding="utf-8")
    assert "reused_request" in api, "响应类型里没有这一位,界面读到的永远是 undefined"


# ==========================================================
# 4:EX-03 不能停在下载那一步之前
# ==========================================================


def test_a_failed_persist_keeps_the_external_id():
    """`fetch_results` 已经返回 = 钱花了、图也生成了。

    落库崩了(存储层构造失败、flush 失败、影子写失败)原来裸 `raise`,
    外层落 `INTERNAL_ERROR`,而 `retry_task` 眼里那是个普通失败:
    掉进最后那个 `else`,清掉 external_task_id 回 QUEUED,重新 submit。
    一批已经付过钱的图被扔掉重买。
    """
    fn = _fn(GENERATION_TASKS, "_await_and_collect")
    src = ast.unparse(fn)
    assert f'"{PROVIDER_RESULT_PENDING_CODE}"' not in src, (
        "码在这里被硬编码了 —— 它该从 `gs` 上取,否则改名时这一处不会跟着动"
    )

    # 两处:一处在 get_status / fetch_results 的 ProviderError 分支,
    # 一处在 `_persist_candidates` 的收尾分支。只有一处 = 后者还在裸 raise
    uses = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and node.attr == "PROVIDER_RESULT_PENDING_CODE"
    ]
    assert len(uses) >= 2, (
        "`_await_and_collect` 里只有一处落这个码 —— 落库失败那一支还在裸 raise,"
        f"重试会清掉 external_task_id 重新提交(找到 {len(uses)} 处)"
    )

    # 那一支必须真的落在 `_persist_candidates` 的异常处理里,而不是别处
    persist_handlers = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Try)
        and "_persist_candidates" in ast.unparse(node.body)
    ]
    assert persist_handlers, "找不到落库那一段 try,这份守卫的假设已经过期了"
    assert any(
        "PROVIDER_RESULT_PENDING_CODE" in ast.unparse(handler)
        for try_node in persist_handlers
        for handler in try_node.handlers
    ), "落库失败之后没有落可续跑的码,重试仍然会重新提交生成"


def test_a_database_outage_is_not_disguised_as_a_resumable_failure():
    """库不可用不该被改判成"结果没取回来"。

    那一类落不进 FAILED(写不进去),而且不是任务的错 —— 它要一路抛到最外层
    那条 `except OperationalError` 走 autoretry。改判会把一次基础设施故障
    变成一条需要人点重试的失败。
    """
    fn = _fn(GENERATION_TASKS, "_await_and_collect")
    src = ast.unparse(fn)
    assert "OperationalError" in src, (
        "落库失败那一支没有把库不可用摘出去 —— 它会被当成可续跑的失败,"
        "而那时候连 FAILED 都写不进去"
    )


def test_the_resume_gate_documents_both_entry_points():
    """两处写这个码的地方代价不同,闸门的说明里必须都在。

    第一处(get_status / fetch_results 失败)是"我们没问出来";
    第二处(落库失败)是"问出来了,没存下" —— 后者更贵,而且它是后补的。
    只写第一处的话,下一个人看到落库失败落这个码会以为是误用。
    """
    gs_path = BACKEND_ROOT / "app" / "services" / "generation_service.py"
    doc = ast.get_docstring(_fn(gs_path, "can_resume_provider_results")) or ""
    assert "_persist_candidates" in doc, (
        "续跑闸门的 docstring 没有提落库失败那条进入路径"
    )
