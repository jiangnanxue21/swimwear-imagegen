"""阶段 P0 的两条代码项:R-04(坏候选图的重试没有出路)与 R-05(CREATED 下的非法边)。

PRD v3.1.1 §13 把「关闭 R-04 与 R-05」列进阶段 P0。两条的共同点是**运营手上
那个唯一的动作(再点一次)在特定路径上通不到任何地方**,而两条的病灶完全不同:

    R-04  同一个 `build_outputs`,两条调用路径,只有一条做了前置检查
    R-05  `can_retry()` 与三条续跑边的定义域不一样宽

## R-04:检查在自动流水线那条路上,人工审核这条没有

`_format_outputs`(自动)从 batch12-2 起会先调 `output_service.source_asset_problem()`,
把「原图没了」变成一个有名字的失败(`SOURCE_ASSET_MISSING` / `_CORRUPT`),
于是 `retry_task` 认得出它、挡得住重试、并告诉运营该换一张候选。

`review_service` 那两条(人工通过出图、抽检换图重出)**没有**这道检查,同一个原因
在那里落成 `INTERNAL_ERROR`:

    非终态   `retry_task` 眼里和别的崩溃没区别 -> 续跑判定成立 -> 又读同一个坏文件
    终态     抛出的话写着「请稍后重试」,而文件已经没了,再点一百次也是同一个结果

## R-05:`can_retry()` 比三条续跑边宽

`can_retry(s)` 的定义是「QUEUED 在 s 的出边里」,满足它的状态有**两个**:
`FAILED` 和 `CREATED`。而 FORMATTING / SCORING / PROVIDER_RUNNING 三条续跑边
只从 FAILED 出发。于是一条停在 CREATED 的任务只要撞上续跑判定,
`transition()` 就抛「不能从 CREATED 转到 SCORING」——一句状态机内部语言。

## 这份守卫的取数方式

零三方依赖:只 import `core/enums` 与 `workflows/state_machine`(两者都只依赖标准库),
其余一律读**源文本**做 AST 断言 —— `review_service` / `generation_service` /
`generation_tasks` 都 import sqlalchemy,在纯测试环境里 import 不进来。

所以这份守卫挡得住「有人把修复删掉」,**挡不住「修复本身逻辑错了」**。
后者要真库,见 `docs/STATUS.md` 阶段 P0 那一节。
"""
from __future__ import annotations

import ast

from app.core.enums import (
    SOURCE_ASSET_BROKEN_CODES,
    SOURCE_ASSET_CORRUPT_CODE,
    SOURCE_ASSET_MISSING_CODE,
    TaskStatus,
)
from app.workflows.state_machine import (
    TRANSITIONS,
    can_retry,
    can_transition,
)
from tests.pure._helpers import BACKEND_ROOT

REVIEW_SERVICE = BACKEND_ROOT / "app" / "services" / "review_service.py"
GENERATION_SERVICE = BACKEND_ROOT / "app" / "services" / "generation_service.py"
GENERATION_TASKS = BACKEND_ROOT / "app" / "tasks" / "generation_tasks.py"
OUTPUT_SERVICE = BACKEND_ROOT / "app" / "services" / "output_service.py"


# ---------------------------------------------------------------- AST 取数


def _fn(path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到函数 {name} —— 改名了就把这份守卫一起改")


def _calls(node: ast.AST) -> set[str]:
    """这段代码里出现的调用名,`模块.函数` 与裸函数名两种写法都收。"""
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


def _text(node: ast.AST) -> str:
    """节点里所有字符串字面量拼起来。用来断言\"给运营的那句话\"。"""
    return "\n".join(
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    )


def _kwargs(node: ast.AST, func_name: str) -> dict[str, ast.AST]:
    """找到对 `func_name` 的调用,返回它的关键字实参。"""
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else ""
        )
        if name == func_name:
            return {kw.arg: kw.value for kw in child.keywords if kw.arg}
    raise AssertionError(f"这段代码里没有对 {func_name}() 的调用")


# ================================================ R-05:前提(病灶本身)


def test_created_is_the_other_status_that_passes_can_retry():
    """R-05 的前提。**这条变红说明状态机改了,修复的依据要重新核。**

    `can_retry()` 问的是「QUEUED 在不在出边里」。满足它的状态恰好两个,
    而三条续跑边只从其中一个出发 —— 这个不对称就是 R-05 的全部成因。
    """
    retryable = {s for s in TRANSITIONS if can_retry(s)}
    assert retryable == {TaskStatus.CREATED, TaskStatus.FAILED}, (
        f"能重试的状态变成了 {sorted(s.value for s in retryable)}。"
        "R-05 的修复按「不止 FAILED 一个」写的,状态集合变了要重新核一遍"
    )


def test_no_resume_edge_starts_from_created():
    """三条续跑边一条都不从 CREATED 出发 —— 所以续跑判定命中就是非法跳转。"""
    for target in (
        TaskStatus.FORMATTING,
        TaskStatus.SCORING,
        TaskStatus.PROVIDER_RUNNING,
    ):
        assert not can_transition(TaskStatus.CREATED, target), (
            f"CREATED -> {target.value} 被打开了。R-05 不该这么修:"
            "开边等于让一条还没跑过的任务直接跳进流水线中段,"
            "而那三条边的前提全都是「这一轮的钱已经花过了」"
        )


def test_the_fallback_edge_is_always_there():
    """兜底那一步不会落空:`can_retry()` 的定义就是 QUEUED 在出边里。

    修复里的 `if not sm.can_transition(...): target = QUEUED` 依赖这一点 ——
    否则退回来的那一跳自己也可能非法,把一个 409 换成另一个 409。
    """
    for status in (TaskStatus.CREATED, TaskStatus.FAILED):
        assert can_transition(status, TaskStatus.QUEUED)


# ================================================ R-05:修复本身


def test_retry_asks_the_transition_table_before_resuming():
    """续跑目标必须先过转移表。**这是 R-05 的修复本身。**"""
    retry = _fn(GENERATION_SERVICE, "retry_task")
    assert "sm.can_transition" in _calls(retry), (
        "`retry_task` 不再问转移表了。三条续跑边只从 FAILED 出发,"
        "而 `can_retry()` 对 CREATED 也返回 True —— 少了这道闸,"
        "CREATED 下命中续跑判定就会抛「不能从 CREATED 转到 SCORING」"
    )


def test_retry_transitions_exactly_once():
    """先算目标、再跳一次。**不是**每个分支各跳各的。

    分支里各写一次 `transition()` 的形状挡不住 R-05:那道转移表检查
    就得在四个地方各抄一遍,而抄漏一处不会有任何东西报错。
    """
    retry = _fn(GENERATION_SERVICE, "retry_task")
    hits = [
        child
        for child in ast.walk(retry)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "transition"
    ]
    assert len(hits) == 1, (
        f"`retry_task` 里有 {len(hits)} 处 transition() 调用。"
        "R-05 的修法是「算出 target -> 过一次转移表 -> 跳一次」,"
        "回到分支里各跳各的,那道闸就只挡住了它所在的那一条路"
    )


def test_retry_is_not_fixed_by_taking_created_out_of_can_retry():
    """不许用「让 CREATED 不能重试」去修 R-05。

    `CREATED -> QUEUED` 合法且正确:那是一条刚建出来的任务重新入队的路。
    摘掉它,「刚建出来的任务点重试」会从能走变成 409 —— 修好一个非法边,
    换来一个更常见的死路。
    """
    assert can_transition(TaskStatus.CREATED, TaskStatus.QUEUED), (
        "CREATED -> QUEUED 被摘掉了。R-05 要挡的是**续跑**,不是重试本身"
    )


def test_only_the_regenerate_path_clears_the_external_task_id():
    """清 `external_task_id` 只在重生那一路发生,而且只有一处。

    三条续跑边的共同前提是「这一轮的钱已经花过了」,而 `external_task_id`
    是唯一能把那笔结果取回来的凭证。清掉它 = 重新 submit = 同一轮买第二次。
    R-05 把四个分支合并成「算目标」之后,清 ID 这件事必须跟着走到合并后的
    那一处,否则续跑分支会顺手把凭证清掉。
    """
    retry = _fn(GENERATION_SERVICE, "retry_task")
    clears = [
        node
        for node in ast.walk(retry)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == "external_task_id"
            for t in node.targets
        )
        and isinstance(node.value, ast.Constant)
        and node.value.value is None
    ]
    assert len(clears) == 1, (
        f"`retry_task` 里有 {len(clears)} 处清 external_task_id。"
        "合并之后应该只剩重生那一处 —— 多一处就意味着某条续跑路径"
        "会把「已经花过钱」的凭证扔掉"
    )


# ================================================ R-04:两条路径必须同样检查


def test_every_formatting_path_verifies_the_source_asset_first():
    """出图之前先确认原图还能读 —— 三个入口一个都不能少。

    R-04 的形状就是「一条路做了、另一条没做」。这条断言按入口点名,
    新增第四条出图路径时它不会自动覆盖,但漏掉现有任何一条会当场变红。
    """
    auto = _calls(_fn(GENERATION_TASKS, "_format_outputs"))
    assert "source_asset_problem" in auto, "自动流水线那条检查没了(batch12-2 / EX-05)"

    manual = _calls(_fn(REVIEW_SERVICE, "_format_after_approval"))
    assert "_readable_source_problem" in manual, (
        "人工审核出图这条没有前置检查 —— R-04 就是这么来的:"
        "同一个 build_outputs,自动那条落 SOURCE_ASSET_MISSING(挡得住重试),"
        "这条落 INTERNAL_ERROR(挡不住,于是一遍遍读同一个坏文件)"
    )


def test_the_two_review_paths_share_one_verdict():
    """检查只做一次,结论传给终态那条分支。

    算两遍就有两个结论:两次读之间文件状态可以变(清理脚本正在跑),
    于是「挡不挡」和「说什么」可能来自不同的事实。而且 S3 后端下
    每一次都是真实往返。
    """
    fn = _fn(REVIEW_SERVICE, "_format_after_approval")
    kwargs = _kwargs(fn, "_rebuild_outputs_for_finished_task")
    assert "problem" in kwargs, (
        "终态那条分支不再接收结论了 —— 它会自己再算一遍,或者干脆不检查"
    )
    assert isinstance(kwargs["problem"], ast.Name) and kwargs["problem"].id == "problem"


def test_the_manual_path_lands_a_named_code_instead_of_internal_error():
    """人工出图路径上,原图坏了要落**专用码**。

    这是 R-04 修复的落点:`retry_task` 的 `SOURCE_ASSET_BROKEN_CODES` 分支
    靠这个码把人挡在重试之外,并告诉他该换一张候选(免费)还是重新生成(花钱)。
    落 INTERNAL_ERROR 的话那道闸看不见它。
    """
    fn = _fn(REVIEW_SERVICE, "_format_after_approval")
    branch = _problem_branch(fn)
    kwargs = _kwargs(branch, "transition")
    code = kwargs.get("error_code")
    assert isinstance(code, ast.Name) and code.id == "problem", (
        "原图坏了这条路没有把 `problem` 当错误码用。"
        "INTERNAL_ERROR 在 `retry_task` 眼里和别的崩溃没有区别"
    )


def test_the_named_failure_does_not_pass_through_formatting():
    """出图一步都没开始,就不该在界面上留下一条「正在出图」。"""
    fn = _fn(REVIEW_SERVICE, "_format_after_approval")
    branch = _problem_branch(fn)
    targets = {
        node.attr
        for node in ast.walk(branch)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "TaskStatus"
    }
    assert "FAILED" in targets, "原图坏了必须落到 FAILED —— 没有成品图就是没做完"
    assert "FORMATTING" not in targets, (
        "这条分支还在往 FORMATTING 跳。检查已经判定读不出来了,"
        "那一跳除了在界面上留一条假的「正在出图」之外什么都不做"
    )


def _problem_branch(fn: ast.FunctionDef) -> ast.If:
    """找到 `if problem is not None:` 那一段。"""
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "problem"
            and any(isinstance(op, ast.IsNot) for op in test.ops)
        ):
            return node
    raise AssertionError(
        f"{fn.name} 里没有 `if problem is not None:` —— R-04 的分支被删了"
    )


# ================================================ R-04:抽检换图那条路的措辞


def test_the_spot_check_path_stops_telling_people_to_retry_a_missing_file():
    """终态换图读不出原图时,话要说死,并给出真正的退路。

    这条路径**没有重试出口**:任务已经是 COMPLETED,转不了 FAILED,
    也没有任何入口会捡起它。原来它会掉进通用异常分支,得到
    「请稍后重试;若反复失败,检查该候选图的原图是否还在存储里」——
    而文件已经没了,再点一百次都是同一个结果。R-04 说的
    「人工反复点没有出路」指的就是这句话。
    """
    fn = _fn(REVIEW_SERVICE, "_rebuild_outputs_for_finished_task")
    branch = _problem_branch(fn)
    words = _text(branch)
    assert "重试不会好转" in words, "没有告诉运营重试是徒劳的"
    assert "改选" in words or "换一张" in words, "没有给出退路(换一张原图还在的候选)"
    assert "请稍后重试" not in words, (
        "这条分支还在说「请稍后重试」。文件丢了是个确定的结论,"
        "不是一次可能会好的失败"
    )


def test_the_transient_branch_keeps_telling_people_to_retry():
    """反过来的一半:**说不清的时候**仍然该建议重试。

    这条和上一条必须同时成立。把通用异常分支也改成「重试不会好转」,
    等于把一次 S3 抖动说成永久损坏 —— 而那一类重试真的就好了。
    """
    fn = _fn(REVIEW_SERVICE, "_rebuild_outputs_for_finished_task")
    words = _text(fn)
    assert "请稍后重试" in words, (
        "通用异常分支的「请稍后重试」也被删了。那条路上的失败原因是未知的,"
        "未知不等于永久"
    )


def test_the_status_whitelist_is_checked_before_the_file():
    """先问「**该不该**上线」,再问「读不读得出来」。

    顺序反了的话,一个被驳回的任务会收到「请改选一张可用的候选」——
    而它换哪张都不该有图挂在网站上。
    """
    fn = _fn(REVIEW_SERVICE, "_rebuild_outputs_for_finished_task")
    body_lines = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            if "_REBUILDABLE_TERMINAL_STATUSES" in ast.dump(node.test):
                body_lines["status"] = node.lineno
    body_lines["problem"] = _problem_branch(fn).lineno
    assert "status" in body_lines, "终态白名单检查不见了"
    assert body_lines["status"] < body_lines["problem"], (
        "文件检查跑到了状态白名单前面。被取消/被驳回的任务不该收到"
        "「换一张候选」这种建议"
    )


# ================================================ 两头对得上


def test_a_storage_outage_is_not_reported_as_a_broken_file():
    """存储层自己不通 ≠ 文件坏了。这一类重试就能好,不许挡在重试之外。"""
    helper = _fn(REVIEW_SERVICE, "_readable_source_problem")
    handlers = [n for n in ast.walk(helper) if isinstance(n, ast.ExceptHandler)]
    assert handlers, (
        "`_readable_source_problem` 没有接住存储层异常。"
        "`source_asset_problem()` 明写它会把 S3 不通抛出去 ——"
        "不接的话一次凭据过期会让整批候选图被永久标成「坏了」"
    )
    returns = [
        n
        for handler in handlers
        for n in ast.walk(handler)
        if isinstance(n, ast.Return)
    ]
    assert returns and all(
        isinstance(r.value, ast.Constant) and r.value.value is None for r in returns
    ), "存储层异常被翻译成了某个错误码 —— 那会把可重试的失败挡在重试之外"


def test_the_codes_these_paths_produce_are_the_codes_retry_blocks_on():
    """两头必须是同一份清单,否则这条链在中间断掉也不会有人报错。

    产码的一头是 `output_service.source_asset_problem()`,
    挡人的一头是 `generation_service.retry_task()` 的 `SOURCE_ASSET_BROKEN_CODES`。
    R-04 的修复把第三、第四个调用点接了进来,而它们能不能挡住重试,
    完全取决于这两头对不对得上。
    """
    produced = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(_fn(OUTPUT_SERVICE, "source_asset_problem"))
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    assert {"SOURCE_ASSET_MISSING_CODE", "SOURCE_ASSET_CORRUPT_CODE"} <= produced
    assert SOURCE_ASSET_BROKEN_CODES == {
        SOURCE_ASSET_MISSING_CODE,
        SOURCE_ASSET_CORRUPT_CODE,
    }, "产码的一头和挡人的一头对不上了"

    retry = _fn(GENERATION_SERVICE, "retry_task")
    arms = [
        node
        for node in ast.walk(retry)
        if isinstance(node, ast.Name) and node.id == "SOURCE_ASSET_BROKEN_CODES"
    ]
    assert len(arms) == 3, (
        f"`retry_task` 里按这份清单判定的地方有 {len(arms)} 处,应该是 3 处:"
        "(1) 不带 force 时挡回去并说明该换候选;(2) 带 force 时逼出书面说明;"
        "(3) 走重生而不是续跑(那条路读的正是坏文件)。"
        "少一处不会有任何东西报错 —— R-04 的两条新路径落了专用码,"
        "而挡人的那一头少认一次"
    )
