"""状态机:合法转移、非法跳转拦截、终态封闭、关键路径可达。"""
from __future__ import annotations

from app.core.enums import TaskStatus as S
from app.workflows.state_machine import (
    AWAITING_HUMAN_STATES,
    CLAIMABLE_STATUSES,
    STALLABLE_STATUSES,
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransition,
    allowed_next,
    assert_can,
    can_cancel,
    can_retry,
    can_transition,
    is_in_flight,
    is_terminal,
    path_exists,
)
from tests.pure._helpers import expect_raises


def test_every_state_has_a_transition_entry():
    """漏掉任何一个状态都会让 assert_can 把它当成终态,静默切断流水线。"""
    missing = sorted(s.value for s in S if s not in TRANSITIONS)
    assert not missing, f"转移表缺少状态: {missing}"


def test_happy_path_is_walkable():
    path = [
        S.CREATED, S.QUEUED, S.PREPROCESSING, S.SUBMITTING,
        S.PROVIDER_RUNNING, S.DOWNLOADING, S.SCORING,
        S.AUTO_APPROVED, S.FORMATTING, S.COMPLETED,
    ]
    # 刻意错位一位取相邻对,两边长度天生差 1 —— 这里的 strict=False 是结论不是省事
    for current, target in zip(path, path[1:], strict=False):
        assert can_transition(current, target), f"{current} -> {target} 应当合法"


def test_manual_review_path_is_walkable():
    path = [S.SCORING, S.MANUAL_REVIEW, S.MANUALLY_APPROVED, S.FORMATTING, S.COMPLETED]
    # 刻意错位一位取相邻对,两边长度天生差 1 —— 这里的 strict=False 是结论不是省事
    for current, target in zip(path, path[1:], strict=False):
        assert can_transition(current, target)


def test_regeneration_loops_back_through_preprocessing():
    """重生必须重建请求(换 seed / 换模特 / 换提示词),因此回到 PREPROCESSING。"""
    assert can_transition(S.SCORING, S.REGENERATING)
    assert can_transition(S.REGENERATING, S.PREPROCESSING)


def test_regeneration_has_no_shortcut_to_submitting():
    """回归用例:曾允许 REGENERATING -> SUBMITTING,导致同一件事有两条路径,
    且编排代码实际走的是 PREPROCESSING,两者不一致。"""
    assert not can_transition(S.REGENERATING, S.SUBMITTING)


def test_illegal_jump_is_rejected():
    err = expect_raises(IllegalTransition, assert_can, S.CREATED, S.COMPLETED)
    assert err.http_status == 409
    assert "CREATED" in err.message and "COMPLETED" in err.message


def test_cannot_skip_provider_call():
    """不允许绕过 Provider 直接进评分 —— 那意味着候选图凭空出现。"""
    assert not can_transition(S.PREPROCESSING, S.SCORING)
    assert not can_transition(S.QUEUED, S.DOWNLOADING)


def test_terminal_states_accept_nothing():
    for state in TERMINAL_STATES:
        assert allowed_next(state) == frozenset(), f"{state} 是终态却仍可转移"
        assert is_terminal(state)


def test_completed_task_cannot_be_restarted():
    """防止后台任务把已完成的任务重新拉起。"""
    for target in (S.QUEUED, S.SUBMITTING, S.REGENERATING, S.CANCELLED):
        expect_raises(IllegalTransition, assert_can, S.COMPLETED, target)


def test_cancel_available_from_every_non_terminal_state():
    for state in S:
        if state in TERMINAL_STATES:
            assert not can_cancel(state)
        elif state in (S.MANUALLY_APPROVED, S.FORMATTING):
            # 已批准并开始出图,再取消会留下半成品,只能等它跑完或失败
            assert not can_cancel(state)
        else:
            assert can_cancel(state), f"{state} 应当可取消"


def test_failed_task_can_be_retried():
    assert can_retry(S.FAILED)
    assert can_transition(S.FAILED, S.QUEUED)


def test_terminal_tasks_cannot_be_retried():
    for state in TERMINAL_STATES:
        assert not can_retry(state)


def test_timeout_does_not_allow_direct_resubmit():
    """需求第九章:网络超时后先查外部任务状态,不能直接重复提交。

    因此 SUBMITTING 不能自转,必须先进 PROVIDER_RUNNING 去查。
    """
    assert not can_transition(S.SUBMITTING, S.SUBMITTING)
    assert can_transition(S.SUBMITTING, S.PROVIDER_RUNNING)
    assert can_transition(S.PROVIDER_RUNNING, S.SUBMITTING)


def test_in_flight_states_are_marked():
    assert is_in_flight(S.PROVIDER_RUNNING)
    assert is_in_flight(S.DOWNLOADING)
    assert not is_in_flight(S.COMPLETED)
    assert not is_in_flight(S.MANUAL_REVIEW)


def test_all_states_can_reach_a_terminal_state():
    """任何状态都必须能走到终态,否则任务会永久悬挂。"""
    stuck = [
        s.value for s in S
        if not any(path_exists(s, t) for t in TERMINAL_STATES)
    ]
    assert not stuck, f"这些状态走不到终态: {stuck}"


def test_completion_is_reachable_from_start():
    assert path_exists(S.CREATED, S.COMPLETED)
    assert path_exists(S.CREATED, S.MANUAL_REVIEW)


def test_accepts_plain_strings():
    """API 层传进来的是字符串,状态机必须能直接吃。"""
    assert can_transition("CREATED", "QUEUED")
    assert is_terminal("COMPLETED")


def test_empty_download_round_can_escalate_without_scoring():
    """一张图都没下下来时没有东西可评,轮次耗尽必须能直接转人工。

    阶段 4 的集成测试撞出过这个缺口:当时 DOWNLOADING 只能去 REGENERATING,
    于是"空轮次 + 轮次耗尽"会抛 IllegalTransition,任务莫名其妙变成 FAILED。
    """
    assert can_transition(S.DOWNLOADING, S.MANUAL_REVIEW)
    assert can_transition(S.DOWNLOADING, S.REGENERATING)


def test_scoring_can_reach_every_decision_state():
    """评分完之后的四条去向都必须存在,少一条就有任务会卡死在 SCORING。"""
    for target in (S.AUTO_APPROVED, S.AUTO_REJECTED, S.REGENERATING, S.MANUAL_REVIEW):
        assert can_transition(S.SCORING, target), f"SCORING 到不了 {target}"


def test_failed_formatting_can_resume_without_regenerating():
    """出图那步崩了,应该能直接回 FORMATTING 重做,而不是从头再生成一遍。

    重新生成有两个代价:真实 Provider 每轮都要花钱;而且换了 seed 之后
    拿到的根本不是当初通过审核的那张图。
    """
    assert can_transition(S.FAILED, S.FORMATTING)
    assert can_transition(S.FAILED, S.QUEUED), "常规重试的路不能被挤掉"


def test_failed_scoring_can_resume_without_regenerating():
    """卡在评分被回收成 FAILED 的任务,重试该从评分接着走。

    候选图已经生成并下载入库了 —— 钱花过了。回到 QUEUED 会重新调 Provider,
    再花一次,而且换了 seed 之后拿到的是另一批图。评分器不通或 worker 被杀,
    问题都不在图片上,重新生成修不好它。

    这条边能不能走由 `generation_service.can_resume_scoring()` 判定:
    它要求错误码是 WORKER_STALLED **且**本轮确实有候选图。
    """
    assert can_transition(S.FAILED, S.SCORING)
    assert can_transition(S.FAILED, S.QUEUED), "常规重试的路不能被挤掉"


def test_resume_path_does_not_open_a_shortcut_past_generation():
    """续跑边只允许回到"这一轮的钱已经花过了"的那几步,不能绕过生成本身。

    ## A45-batch12-2 / EX-03 之后这条守卫的边界挪了一格

    原来这三个状态一律禁止,理由是"直接跳进去会得到一个没有请求在跑却等着
    结果的任务"。那句话对 SUBMITTING 和 DOWNLOADING 仍然成立,对
    PROVIDER_RUNNING **不再成立** —— `can_resume_provider_results()` 要求
    `external_task_id` 还在任务上,而那个 ID 的含义正是"外部确实有一个
    请求在跑"。禁止它的代价是 EX-03:查状态/下载失败之后没有任何路径能回到
    那笔已经付过钱的外部任务,重试只能走 FAILED -> QUEUED 重新 submit。

    另外两个仍然禁止,而且理由比原来更具体:

        SUBMITTING   进去就会重新 `provider.submit()` —— 同一轮生成买第二次
        DOWNLOADING  没有候选可下;要重新取结果该走 PROVIDER_RUNNING,
                     `_await_and_collect` 会从 get_status 一路走到下载
    """
    for target in (S.SUBMITTING, S.DOWNLOADING):
        assert not can_transition(S.FAILED, target), f"FAILED 不该能直接去 {target}"
    assert not can_transition(S.QUEUED, S.FORMATTING), "没生成过的任务不该能直接出图"
    assert not can_transition(S.QUEUED, S.SCORING), "没生成过的任务不该能直接评分"


def test_the_provider_resume_edge_is_gated_on_an_external_id():
    """PROVIDER_RUNNING 这条边敢开的**唯一**理由是它有闸门(EX-03)。

    边本身只说"这个跳转合法",说不了"什么时候该跳"。后者在
    `generation_service.can_resume_provider_results()` 里,而它必须同时要求:

        错误码是 PROVIDER_RESULT_PENDING   这次失败说的是"我们没问出来",
                                          不是"外部任务失败了"
        external_task_id 还在              没有它就无从继续

    少了第二条,这条边就退化成"从 FAILED 随便跳回等结果",而那正是原来
    禁止它的理由。用源码断言而不是调用,是因为 tests/pure 里没有数据库。
    """
    import ast

    from tests.pure._helpers import BACKEND_ROOT

    src = (BACKEND_ROOT / "app" / "services" / "generation_service.py").read_text(
        encoding="utf-8"
    )
    fn = next(
        node
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.FunctionDef)
        and node.name == "can_resume_provider_results"
    )
    body = ast.dump(fn)
    assert "PROVIDER_RESULT_PENDING_CODE" in body, "闸门没有检查错误码"
    assert "external_task_id" in body, "闸门没有要求外部任务 ID 还在"

    assert can_transition(S.FAILED, S.PROVIDER_RUNNING)
    assert can_transition(S.FAILED, S.QUEUED), "常规重试的路不能被挤掉"


# ==========================================================
# 「等人动」那一档(A34):清单钉在后端,前端只读
# ==========================================================
#
# 这一档原来只是一份写在 `types.ts` 里的清单。它决定的是轮询节拍:
# 停(终态)/ 慢(等人动)/ 快(机器在跑)。放错值不会报错,只会让某个
# 状态被问得太勤或太懒 —— 而"太懒"那一侧的表现是页面停在旧数据上。
#
# 下面三条是这份清单的**自洽约束**,和前端无关;跨语言那一半在
# `test_frontend_contract.py` 里逐值比对。


def test_awaiting_human_states_are_not_terminal():
    """终态永远不会变,慢轮询它纯属浪费。

    反过来说,一个真终态混进这一档的表现是:一个再也不会变的任务每 30 秒
    被问一次,直到有人关掉那个标签页。
    """
    overlap = sorted(s.value for s in AWAITING_HUMAN_STATES & TERMINAL_STATES)
    assert not overlap, f"{overlap} 已经是终态了,不该再轮询"


def test_awaiting_human_states_still_have_outgoing_edges():
    """「有人点一下就会继续走」必须在转移表上成立。

    没有出边 = 事实上的终态。这一条会在"某个状态的边被删光了、但它还留在
    这份清单里"的时候变红 —— 那时正确的动作是把它移进 TERMINAL_STATES,
    而不是让它继续被慢轮询。
    """
    for state in sorted(AWAITING_HUMAN_STATES, key=lambda s: s.value):
        assert allowed_next(state), (
            f"{state.value} 在转移表里没有任何出边,等于终态。"
            "它要么进 TERMINAL_STATES,要么该给它加边"
        )


def test_awaiting_human_states_are_not_held_by_a_worker():
    """等人动 ≠ 机器正在跑。两份清单必须不相交。

    `STALLABLE_STATUSES` 的定义就是"worker 正持有、且不等外部输入",
    `CLAIMABLE_STATUSES` 是"worker 会主动认领的起点"。任一份里的状态
    都会被后台推进,把它归进慢轮询会让一个每秒都在变的东西 30 秒才刷一次。
    """
    values = {s.value for s in AWAITING_HUMAN_STATES}
    held = sorted(values & set(STALLABLE_STATUSES))
    assert not held, f"{held} 是 worker 正持有的状态,不是在等人"
    claimable = sorted(values & set(CLAIMABLE_STATUSES))
    assert not claimable, f"{claimable} 是 worker 会主动认领的状态,不是在等人"
