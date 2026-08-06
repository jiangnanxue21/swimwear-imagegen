"""A45 batch12:第二轮加固(异常 / 并发 / 部分失败 / 状态恢复)的守卫。

本轮只修了两条,都在**人工审核与自动流水线相撞**的那个交叉口上:

    R-01  A 档抽检换选候选图 -> `COMPLETED -> FORMATTING` 非法跳转 -> 整笔决策回滚
    R-02  抽检项点「重新生成」-> `COMPLETED -> REGENERATING` 非法跳转,措辞是状态机内部语言

两条的病灶是同一个假设:`review_service` 里那两处 `gs.transition` 默认
「进来的任务一定还没走完」。抽检项恰恰是在任务**继续往下跑**的前提下开出来的
(`NON_BLOCKING_REVIEW_REASONS`),所以它一定会撞上终态。

## 这份守卫最要紧的一条是第一条

第一条断言的是**终态仍然封闭** —— 也就是说,它守的不是修复,而是
「不要用那个显而易见的错误改法去修」。给状态机加一条 `COMPLETED -> FORMATTING`
的边能让 R-01 当场变绿,同时把「已完成任务不会被后台任务重新拉起」这个
不变量拆掉,而那个不变量没有第二处依据。

零三方依赖:只 import `workflows/state_machine`(它只依赖 `core/enums`
与 `core/errors`)与 `review_service` 的**源文本**。可用裸 python3 经
`tools/run_pure_tests.py` 执行。
"""
from __future__ import annotations

import ast

from app.core.enums import TaskStatus
from app.workflows.state_machine import (
    TERMINAL_STATES,
    can_transition,
    is_terminal,
)
from tests.pure._helpers import BACKEND_ROOT

REVIEW_SERVICE = BACKEND_ROOT / "app" / "services" / "review_service.py"


def _source() -> str:
    return REVIEW_SERVICE.read_text(encoding="utf-8")


def _fn(name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(_source())):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name} —— 改名了就把这份守卫一起改")


def _calls(node: ast.AST) -> set[str]:
    """这段代码里出现的调用名,归一成 `模块.函数` 与裸函数名两种写法。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                names.add(f"{func.value.id}.{func.attr}")
            names.add(func.attr)
    return names


# ================================================ R-01 前提:终态必须保持封闭


def test_a_finished_task_still_cannot_be_dragged_back_into_the_pipeline():
    """病灶本身,同时是「不许这么修」的那条线。

    R-01 的现象是 `COMPLETED -> FORMATTING` 抛 `IllegalTransition`。
    最省事的改法是给转移表加一条边 —— 那会让 `state_machine` 顶部
    「终态不可再转移,防止已完成任务被后台任务重新拉起」这句话失去依据,
    而 `reap_stalled`、Outbox 重投、`_claim()` 全都建立在它上面。

    所以修复必须绕开状态机,而不是放松它。这条断言钉住这一点。
    """
    assert is_terminal(TaskStatus.COMPLETED)
    assert not can_transition(TaskStatus.COMPLETED, TaskStatus.FORMATTING), (
        "COMPLETED -> FORMATTING 被打开了。R-01 不该这么修:"
        "重出图不需要动任务状态,见 review_service._rebuild_outputs_for_finished_task"
    )
    assert not can_transition(TaskStatus.COMPLETED, TaskStatus.REGENERATING), (
        "COMPLETED -> REGENERATING 被打开了。R-02 不该这么修:"
        "抽检项要的是一句说得清的拒绝,不是把终态拆开"
    )


def test_every_terminal_state_stays_closed():
    """顺带守住另外三个终态 —— 它们和 COMPLETED 是同一条不变量。"""
    for state in TERMINAL_STATES:
        for target in TaskStatus:
            assert not can_transition(state, target), (
                f"{state.value} 是终态,却还能转到 {target.value}"
            )


# ================================================ R-01 修复:换图走重出图,不走状态机


def test_the_swap_path_checks_for_a_finished_task_before_transitioning():
    """`_format_after_approval` 必须先问「任务是不是已经走完了」。

    改动前它上来就 `gs.transition(task, FORMATTING)`,而抽检换图进来时
    任务是 COMPLETED。把这个判断拿掉,抽检换图会重新变成 409 + 整笔回滚。
    """
    fn = _fn("_format_after_approval")
    calls = _calls(fn)
    assert "sm.is_terminal" in calls or "is_terminal" in calls, (
        "_format_after_approval 不再判断任务是否已终态 —— 抽检换图会回到 R-01"
    )
    assert "_rebuild_outputs_for_finished_task" in calls, (
        "终态分支没有接到重出图路径上"
    )


def test_the_terminal_check_comes_before_the_transition():
    """顺序不能反。判断写在 `transition` **后面**等于没写。"""
    fn = _fn("_format_after_approval")
    check_line = min(
        (
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "is_terminal"
        ),
        default=None,
    )
    transition_line = min(
        (
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "transition"
        ),
        default=None,
    )
    assert check_line is not None, "找不到终态判断"
    assert transition_line is not None, "找不到 transition 调用"
    assert check_line < transition_line, (
        "终态判断排在 transition 之后 —— 那一行永远等不到执行"
    )


def test_the_rebuild_path_never_touches_the_task_state():
    """重出图分支里不许有任何 `transition`。

    有的话就说明又在试图移动一个终态任务,而那正是 R-01。
    """
    fn = _fn("_rebuild_outputs_for_finished_task")
    calls = _calls(fn)
    assert "gs.transition" not in calls and "transition" not in calls, (
        "重出图分支在改任务状态。任务已经是终态,这里只该重出图"
    )
    assert "output_service.build_outputs" in calls or "build_outputs" in calls, (
        "重出图分支没有真的出图"
    )


def test_only_completed_tasks_can_have_their_outputs_rebuilt():
    """能重出图的终态只有 COMPLETED 一个。

    被取消 / 被驳回的任务重出图,等于把一张不该上线的图挂上网站 ——
    `build_outputs` 会停用该商品同用途的旧图并启用新的一代。
    这条错了不会报错,所以要有断言。
    """
    tree = ast.parse(_source())
    allowed: set[str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if isinstance(target, ast.Name) and target.id == "_REBUILDABLE_TERMINAL_STATUSES":
            # 只取集合字面量的**直接**元素:`ast.walk` 会把
            # `TaskStatus.COMPLETED.value` 里嵌套的 `TaskStatus.COMPLETED`
            # 也收进来,于是一个元素数成两个
            literals = [n for n in ast.walk(node.value) if isinstance(n, ast.Set)]
            assert literals, "_REBUILDABLE_TERMINAL_STATUSES 不再是一个集合字面量"
            allowed = {ast.unparse(el) for el in literals[0].elts}
    assert allowed is not None, "找不到 _REBUILDABLE_TERMINAL_STATUSES"
    assert allowed == {"TaskStatus.COMPLETED.value"}, (
        f"可重出图的终态清单变了:{sorted(allowed)}。"
        "只有 COMPLETED 允许 —— 其余三个终态的图都不该出现在网站上"
    )


def test_a_failed_rebuild_raises_instead_of_reporting_success():
    """重出图失败必须抛,不许吞掉之后照常返回。

    吞掉的后果是三处各说一套:候选图换了、审核项记 APPROVED、
    成品图还是旧的那一代,而且没有任何一处会报错 —— 那正是本轮
    第 8 条(外部操作失败但本地被标记成功)要防的形状。

    这里同时要求异常处理里**没有** `return`:一个 `return` 就足以
    让这条修复失效,而它看起来完全人畜无害。
    """
    fn = _fn("_rebuild_outputs_for_finished_task")
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "重出图分支没有异常处理"
    for handler in handlers:
        raises = [n for n in ast.walk(handler) if isinstance(n, ast.Raise)]
        returns = [n for n in ast.walk(handler) if isinstance(n, ast.Return)]
        assert raises, "出图失败被吞掉了 —— 本地会记成换图成功,而网站上还是旧图"
        assert not returns, "异常处理里有 return,失败会被伪装成成功"


def test_the_rebuild_failure_message_tells_the_operator_it_did_not_take_effect():
    """报错要回答运营的两个问题:生效了没有、现在该做什么。

    「本次换图未生效」这句话不能少 —— 整笔回滚之后候选图没换,
    而运营的默认假设是"报错了但可能改了一半"。
    """
    source = _source()
    assert "未生效" in source, "换图失败的提示没有说清楚这次改动没生效"
    assert "重试" in source, "换图失败的提示没有告诉运营下一步做什么"


# ================================================ R-02 修复:拒绝要说人话


def test_regeneration_is_refused_before_anything_is_mutated():
    """`request_regeneration` 必须先判能不能转,再改 provider / max_rounds。

    顺序反过来时,报错来自状态机(「不能从 COMPLETED 转到 REGENERATING」),
    运营既不知道自己撞的是什么,也不知道该做什么。
    """
    fn = _fn("request_regeneration")
    guard_line = min(
        (
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "can_transition"
        ),
        default=None,
    )
    assert guard_line is not None, (
        "request_regeneration 不再预先判断状态 —— R-02 会回来"
    )

    mutation_lines = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "task"
    ]
    assert mutation_lines, "找不到对 task 的赋值,这份守卫的前提变了"
    assert guard_line < min(mutation_lines), (
        "状态判断排在改字段之后 —— 报错会来自状态机而不是这条规则"
    )


def test_the_refusal_names_the_spot_check_case_and_the_way_out():
    """抽检那一支要单独说话,并且要给出口。

    非阻塞项和「任务被别人处理过」是两种完全不同的情况,压成同一句话
    等于让运营自己猜。出口也必须写明:抽检项用通过/驳回关闭,
    要重做就新建任务。
    """
    source = _source()
    assert "抽检" in source, "拒绝措辞里没有点名抽检项"
    assert "新建" in source, "没有告诉运营重做该走哪条路"
    fn = _fn("request_regeneration")
    branches = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.If)
        and "blocking" in ast.unparse(node.test)
    ]
    assert branches, (
        "拒绝路径没有按 blocking 分支 —— 抽检项和阻塞项会拿到同一句话"
    )


def test_review_service_still_imports_the_state_machine():
    """两条修复都靠它。import 被清掉时立刻红,而不是等到运行时 NameError。"""
    tree = ast.parse(_source())
    imported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "sm" in imported, "review_service 不再 import state_machine"


# ================================================ 前后端一致:抽检换图会重出图

FRONTEND = BACKEND_ROOT.parent / "frontend" / "src"


def test_the_spot_check_banner_no_longer_promises_that_nothing_changes():
    """R-01 修好之后,抽检换图**真的会替换网站上的图** —— 提示必须跟上。

    这条守的是本轮自己造出来的一个坑:修复前那条路径是 409,
    于是页面上那句「不会回滚已通过的图」**碰巧**和观察到的行为一致。
    修好之后它就成了一句错话,而且是最坏的那一种 —— 它让运营以为
    换一张候选只是记个账。

    跨语言断言,理由与 batch10/11 一致:光断言后端不会变红。
    """
    page = (FRONTEND / "pages" / "ReviewDetailPage.tsx").read_text(encoding="utf-8")
    assert "不会回滚已通过的图" not in page, (
        "抽检提示又回到了「不会回滚已通过的图」。"
        "换选候选并通过会走 review_service._rebuild_outputs_for_finished_task,"
        "它会停用旧成品图并发布新的一代 —— 这句话是错的"
    )
    assert "重出成品图" in page, "抽检提示没有说明换图会重新出图"
    assert "替换网站上" in page, "抽检提示没有说明换图会替换线上的图"


def test_the_banner_warns_only_when_the_operator_actually_swapped():
    """默认不吓人,换了才警告。

    一律显示警告等于又造一条常亮提示,而常亮提示会被训练成噪音 ——
    这个仓库在 `unpriced_calls` 那条上已经吃过一次。
    """
    page = (FRONTEND / "pages" / "ReviewDetailPage.tsx").read_text(encoding="utf-8")
    assert "selectedId !== review.best_candidate_id" in page, (
        "抽检提示不再按「是否换过候选」分档"
    )


# ================================================ R-06 批量付费:自动重跑必须封顶

from app.workbench.batch import (  # noqa: E402
    ERROR_REGISTRY,
    MAX_BILLED_EXECUTIONS,
    BatchAction,
    BatchErrorCode,
    ReceiptRoute,
    ReceiptStatus,
    receipt_route,
)


def test_in_flight_receipt_stops_paying_after_the_ceiling():
    """病灶:IN_FLIGHT 会**自动**重新付费执行,原来一路跑到条目重试上限。

    IN_FLIGHT 的含义是「外部调用可能已经成功并计费,但结果没落库」——
    与生成链路的 SUBMIT_RESULT_UNKNOWN 是同一种不确定性。那边挡住自动重试
    并要求人工对账结论,这边原来只写一条 warning 就接着花钱,而发起者是
    回收器(beat 60 秒一拍),全程没有人在回路里。
    """
    # ## A45-batch12-2 / EX-03 之后这条守卫被**收紧**了
    #
    # 原来断言的是"第一次崩允许再自动跑一次,第二次才停",理由是绝大多数
    # IN_FLIGHT 崩在调用之前、分不出来只能折中。EX-02 报的正是那放过的一次:
    # 它是一笔**无人值守**的重复付费(发起者是回收器,60 秒一拍)。
    #
    # 迁移 0031 让"崩在调用之前还是之后"变成库里读得到的事实,于是折中的
    # 前提不成立了。现在的断言比原来强:调用发出过就**一次都不自动重跑**。
    for executions in (1, 2, 9):
        assert (
            receipt_route(
                ReceiptStatus.IN_FLIGHT.value,
                BatchAction.EXTRACT,
                executions=executions,
                call_dispatched=True,
            )
            is ReceiptRoute.NEEDS_RECONCILIATION
        ), f"executions={executions} 时仍然会自动重新付费"

    # 反过来:可证明没发出去的那一支不许被上限锁死,否则一次库抖动就会把
    # 一件本来免费可重跑的商品变成人工工单 —— 那种代价下这道闸迟早被关掉
    assert (
        receipt_route(
            ReceiptStatus.IN_FLIGHT.value,
            BatchAction.EXTRACT,
            executions=9,
            call_dispatched=False,
        )
        is ReceiptRoute.EXECUTE
    )


def test_the_ceiling_also_covers_billed_failure_without_revalidation():
    """FAILED_BILLED 且不可离线复校的那一支走的是同一条会计费的路,必须同样封顶。

    EXTRACT 全失败时一条证据都没落,只能重跑 —— 而重跑同样要钱。
    两个入口共用 `_billed_again_or_stop`,这条守着它们不分叉。
    """
    assert (
        receipt_route(
            ReceiptStatus.FAILED_BILLED.value, BatchAction.EXTRACT, executions=2
        )
        is ReceiptRoute.NEEDS_RECONCILIATION
    )


def test_free_paths_are_not_throttled_by_the_ceiling():
    """不花钱的走法不受上限约束,否则一次崩溃会把免费动作也锁死。"""
    # 明确没花过钱
    assert (
        receipt_route(
            ReceiptStatus.FAILED_UNBILLED.value, BatchAction.EXTRACT, executions=9
        )
        is ReceiptRoute.EXECUTE
    )
    # 复用成功结论,不调模型
    assert (
        receipt_route(ReceiptStatus.SUCCEEDED.value, BatchAction.EXTRACT, executions=9)
        is ReceiptRoute.REUSE_SUCCESS
    )
    # 文案:付费失败但原始结果可离线复校,复校不要钱
    assert (
        receipt_route(
            ReceiptStatus.FAILED_BILLED.value,
            BatchAction.GENERATE_COPY,
            executions=9,
        )
        is ReceiptRoute.REUSE_BILLED_FAILURE
    )


def test_no_receipt_always_executes():
    """没有回执 = 没做过。上限不该把第一次执行也挡住。"""
    assert receipt_route(None, BatchAction.EXTRACT, executions=9) is ReceiptRoute.EXECUTE


def test_the_ceiling_leaves_room_for_exactly_one_automatic_retry():
    """上限取值本身要有守卫。

    取 1 等于每次 pod 驱逐都要人工介入(50 件批次会因一次抖动全停);
    取 3 就回到了原来的行为。改动它必须是一个显式决定。
    """
    assert MAX_BILLED_EXECUTIONS == 2, (
        "自动付费执行上限被改了。1 = 每次基础设施抖动都要人工介入;"
        ">=3 = 退回原来那个「一次猝死最多三次付费」的行为"
    )


def test_the_stop_code_cannot_be_auto_retried():
    """`BILLED_RESULT_UNKNOWN` 必须是 needs_human 且不可重试。

    写成 retryable 的话,`retryable_rows` 会把它交回重试按钮,而点一次
    就是又一次可能的付费 —— 这个码存在的意义会被完全抵消。
    """
    meta = ERROR_REGISTRY[BatchErrorCode.BILLED_RESULT_UNKNOWN]
    assert meta.retryable is False, "费用不明的条目不该拿到重试按钮"
    assert meta.needs_human is True, "它必须落 NEEDS_HUMAN,否则回收器还会捡它"
    assert not meta.skip, "它不是跳过类:这一件确实没做成"


def test_the_stop_is_not_a_dead_end():
    """闸门必须有出口,否则它自己就是一个「核心流程无法继续」。

    回执按幂等键索引,而幂等键不会自己变 —— 没有解除通道的话,人对完账
    也没有任何办法让这一件重新开始。
    """
    tool = BACKEND_ROOT / "tools" / "resolve_billed_unknown.py"
    assert tool.exists(), "挡住了自动重跑却没给人工解除通道"
    text = tool.read_text(encoding="utf-8")
    assert "--apply" in text, "解除脚本必须默认干跑"
    assert "--note" in text, "解除必须留下人工对账结论"
    meta = ERROR_REGISTRY[BatchErrorCode.BILLED_RESULT_UNKNOWN]
    assert "resolve_billed_unknown" in meta.advice, (
        "错误提示没有告诉运营出口在哪 —— 那道闸对他就是个死胡同"
    )
