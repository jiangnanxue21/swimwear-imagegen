"""生成任务状态机。仅标准库,可独立测试。

设计原则(需求第九章):
- 转移表是唯一真相源,任何状态写入都必须先过校验;
- 非法跳转抛错而不是静默纠正 —— 静默纠正会让线上问题变成"数据莫名其妙",无法排查;
- 终态不可再转移,防止已完成任务被后台任务重新拉起;
- CANCELLED 可从任何非终态进入,取消必须随时生效。
"""
from __future__ import annotations

from app.core.enums import TaskStatus
from app.core.errors import ErrorCode, ValidationError

S = TaskStatus

#: 终态:进入后不再接受任何转移
TERMINAL_STATES: frozenset[TaskStatus] = frozenset(
    {S.COMPLETED, S.CANCELLED, S.AUTO_REJECTED, S.MANUALLY_REJECTED}
)

#: 需要外部系统响应、可能长时间停留的状态。看板上应显示进度而非"卡住"。
IN_FLIGHT_STATES: frozenset[TaskStatus] = frozenset(
    {S.QUEUED, S.PREPROCESSING, S.SUBMITTING, S.PROVIDER_RUNNING, S.DOWNLOADING}
)

#: 合法转移表。键为当前状态,值为允许进入的下一状态集合。
TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    S.CREATED: frozenset({S.QUEUED, S.CANCELLED, S.FAILED}),
    S.QUEUED: frozenset({S.PREPROCESSING, S.CANCELLED, S.FAILED}),
    S.PREPROCESSING: frozenset({S.SUBMITTING, S.CANCELLED, S.FAILED}),
    # 提交后网络超时不直接重提,而是回到 PROVIDER_RUNNING 去查外部状态(需求第九章)
    S.SUBMITTING: frozenset({S.PROVIDER_RUNNING, S.CANCELLED, S.FAILED}),
    # 允许回到 SUBMITTING:同一轮内换 Provider 或重试提交
    S.PROVIDER_RUNNING: frozenset({S.DOWNLOADING, S.SUBMITTING, S.CANCELLED, S.FAILED}),
    # 一张图都没下下来时不经过 SCORING —— 没有东西可评。这一轮直接按轮次规则处理:
    # 还有轮次就 REGENERATING,轮次耗尽就 MANUAL_REVIEW。两条边描述的是同一个现实,
    # 只是轮次余量不同,所以放在一起。
    S.DOWNLOADING: frozenset(
        {S.SCORING, S.REGENERATING, S.MANUAL_REVIEW, S.CANCELLED, S.FAILED}
    ),
    S.SCORING: frozenset(
        {
            S.AUTO_APPROVED,
            S.AUTO_REJECTED,
            S.REGENERATING,
            S.MANUAL_REVIEW,
            S.CANCELLED,
            S.FAILED,
        }
    ),
    # 重生一律回到 PREPROCESSING 重建请求:每轮至少换 seed,B 档还会换模特/提示词,
    # 这正是 preprocessing 的职责。不提供直达 SUBMITTING 的第二条路径 ——
    # 两条路径做同一件事,只会让"这轮的参数到底是哪来的"变得难以追溯。
    # 轮次耗尽时可直接转人工审核。
    S.REGENERATING: frozenset({S.PREPROCESSING, S.MANUAL_REVIEW, S.CANCELLED, S.FAILED}),
    S.AUTO_APPROVED: frozenset({S.FORMATTING, S.CANCELLED, S.FAILED}),
    S.MANUAL_REVIEW: frozenset(
        {S.MANUALLY_APPROVED, S.MANUALLY_REJECTED, S.REGENERATING, S.CANCELLED}
    ),
    S.MANUALLY_APPROVED: frozenset({S.FORMATTING, S.FAILED}),
    S.FORMATTING: frozenset({S.COMPLETED, S.FAILED}),
    # 失败可重试:默认回到队列重新走一遍。
    # 另有三条**续跑**边,共同点是「钱已经花了,崩的是后面某一步」:
    #
    #   FAILED -> FORMATTING  候选图已经评过、选过,只是多尺寸转换那步崩了
    #   FAILED -> SCORING     候选图已经下载入库,崩在评分那一步(worker 被回收)
    #   FAILED -> PROVIDER_RUNNING
    #                         外部任务已受理、ID 已落库,崩在查状态或下载这一步
    #                         (A45-batch12-2 / EX-03)
    #
    # 三种情况重新生成一遍都纯属浪费:真实 Provider 每轮都要花钱,而且换了
    # seed 之后拿到的根本不是当初那批图 —— 连"至少还留着这批"都保不住。
    #
    # 第三条少了的话,`get_status` / `fetch_results` 失败之后没有任何路径能回到
    # 那笔**已经付过钱**的外部任务上:重试只能走 FAILED -> QUEUED,而那条边会
    # 清掉 external_task_id 重新 submit —— 同一轮生成买第二次。
    #
    # 这三条边不是绕过生成的捷径:前提由 `generation_service` 的
    # can_resume_formatting() / can_resume_scoring() / can_resume_provider_results()
    # 判定 —— 前两者要求本轮**确实已经有候选图**,第三者要求
    # **external_task_id 还在**。都不满足的任务只能走 FAILED -> QUEUED。
    S.FAILED: frozenset(
        {S.QUEUED, S.FORMATTING, S.SCORING, S.PROVIDER_RUNNING, S.CANCELLED}
    ),
    S.MANUALLY_REJECTED: frozenset(),
    S.AUTO_REJECTED: frozenset(),
    S.COMPLETED: frozenset(),
    S.CANCELLED: frozenset(),
}


class IllegalTransition(ValidationError):
    """非法状态跳转。归到输入错误一类,返回 409。"""

    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        allowed = sorted(t.value for t in TRANSITIONS.get(current, frozenset()))
        super().__init__(
            f"不能从 {current.value} 转到 {target.value}"
            + (f";允许的下一步:{', '.join(allowed)}" if allowed else ";这是终态"),
            code=ErrorCode.INPUT_INVALID,
            http_status=409,
            detail={"current": current.value, "target": target.value},
        )


def is_terminal(state: TaskStatus | str) -> bool:
    return TaskStatus(state) in TERMINAL_STATES


def is_in_flight(state: TaskStatus | str) -> bool:
    return TaskStatus(state) in IN_FLIGHT_STATES


def can_transition(current: TaskStatus | str, target: TaskStatus | str) -> bool:
    return TaskStatus(target) in TRANSITIONS.get(TaskStatus(current), frozenset())


def assert_can(current: TaskStatus | str, target: TaskStatus | str) -> TaskStatus:
    """校验并返回目标状态。所有写库路径都必须先调用它。"""
    cur, tgt = TaskStatus(current), TaskStatus(target)
    if tgt not in TRANSITIONS.get(cur, frozenset()):
        raise IllegalTransition(cur, tgt)
    return tgt


def allowed_next(current: TaskStatus | str) -> frozenset[TaskStatus]:
    return TRANSITIONS.get(TaskStatus(current), frozenset())


def can_cancel(current: TaskStatus | str) -> bool:
    return TaskStatus.CANCELLED in allowed_next(current)


def can_retry(current: TaskStatus | str) -> bool:
    return TaskStatus.QUEUED in allowed_next(current)


def path_exists(start: TaskStatus, goal: TaskStatus) -> bool:
    """图可达性。用于测试断言流水线没有被改成断路。"""
    seen, stack = {start}, [start]
    while stack:
        node = stack.pop()
        if node == goal:
            return True
        for nxt in TRANSITIONS.get(node, frozenset()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


#: worker 认领任务时允许的起始状态。
#: 首轮进来前编排层已经把 CREATED 转成 QUEUED,重生轮次从 REGENERATING 进。
CLAIMABLE_STATUSES: tuple[str, ...] = (S.QUEUED.value, S.REGENERATING.value)

#: 认领的目标状态。**认领本身就是这一步转移**,不是额外加的状态 ——
#: 它由一条带状态条件的 UPDATE 完成,因此绕过了 assert_can。
#: 合法性改由 test_state_machine 静态守住,见
#: test_every_claimable_status_can_legally_reach_the_claim_target。
CLAIM_TARGET: str = S.PREPROCESSING.value


#: worker 正持有、且不等外部输入的状态。卡在这里说明 worker 死了,不是在等人。
#:
#: 刻意不含 QUEUED:队列积压时任务合法地停在 QUEUED,那是容量问题不是卡死;
#: 而"消息根本没发出去"现在由 Outbox 负责,不需要靠状态去猜。
#: 也不含 MANUAL_REVIEW —— 那是在等人,等多久都正常。
STALLABLE_STATUSES: tuple[str, ...] = (
    S.PREPROCESSING.value,
    S.SUBMITTING.value,
    S.PROVIDER_RUNNING.value,
    S.DOWNLOADING.value,
    S.SCORING.value,
    S.AUTO_APPROVED.value,
    S.FORMATTING.value,
)

#: **机器不再写它,但它不是终态** —— 有人点一下就会继续走(A34)。
#:
#: 这一档原来只写在前端(`types.ts` 的 `AWAITING_HUMAN_TASK_STATUSES`),
#: 而硬规则 4 说的正是「前端不许推测状态」。当时用 `STALLABLE_STATUSES`
#: 做了负向约束(断言两份清单不相交),那挡得住"放错值",挡不住
#: **后端新增一个等人动的状态而前端不知道** —— 那个状态会掉进 LIVE 档,
#: 被每 2.5 秒问一次,而没有任何后台进程会改它。
#:
#: 判据是「出边只由人产生」,逐个对过 `TRANSITIONS`:
#:
#:     MANUAL_REVIEW  批准 / 驳回 / 打回重生,三条边都要人点
#:     FAILED         重试(以及两条续跑边)同样要人点;worker 不会自己捡起它
#:
#: 刻意**不含**这几个容易被误收进来的:
#:
#:     CREATED / REGENERATING   worker 会认领(`CLAIMABLE_STATUSES` /
#:                              `STRANDED_STATUSES`),不是在等人
#:     MANUALLY_APPROVED        人已经动过了,下一步是机器去 FORMATTING
#:     QUEUED                   队列积压是容量问题,不是在等人
#:
#: 三条一致性约束由 `tests/pure/test_state_machine.py` 守着(与终态不相交、
#: 与 `STALLABLE_STATUSES` 不相交、每个成员都还有出边),
#: 前端那份清单由 `test_frontend_contract.py` **逐值正向比对**。
AWAITING_HUMAN_STATES: frozenset[TaskStatus] = frozenset(
    {S.MANUAL_REVIEW, S.FAILED}
)

#: 请求已经送出去、但**对方受理了没有**这件事本身还不确定的状态。
#:
#: 只有 SUBMITTING 一个:`_checkpoint(task, "submitting")` 提交之后、
#: `provider.submit()` 返回并落 ID 之前,worker 死在这个窗口里的任务长这样。
#: 它可能已经计费,也可能一个字节都没发出去,而**我们没有任何凭证能去查** ——
#: 所以只能落 `SUBMIT_RESULT_UNKNOWN`,由人去 Provider 后台对账。
SUBMIT_IN_DOUBT_STATUSES: frozenset[str] = frozenset({S.SUBMITTING.value})

#: 已经**确定受理过**、正在等外部结果的状态(A45-batch12-3 / REG-02)。
#:
#: 与上面那一档的区别是确定性,不是阶段:进了这两个状态说明 `provider.submit()`
#: 已经成功返回过。**带着 external_task_id 的**这一类不该走对账流程 ——
#: 照着那个 ID 就能把结果取回来,而对账流程的出口(强制重试)会清掉 ID
#: 重新 submit,也就是把已经付过钱的那一笔生成扔掉再买一次。
#:
#: 没有 external_task_id 的那几行仍然按"提交存疑"处理:停在这两个状态却
#: 没有 ID 是不该存在的形状,而它的成因只可能是落 ID 那次 commit 崩了 ——
#: 也就是钱可能已经花了、我们却查无此 ID。
AWAITING_PROVIDER_RESULT_STATUSES: frozenset[str] = frozenset(
    {
        S.PROVIDER_RUNNING.value,
        S.DOWNLOADING.value,
    }
)

#: 请求可能已经发出去、因而**可能已经计费**的状态。
#: 在这些状态下卡死的任务不能让人一键退回 QUEUED —— 那等于再买一次。
#:
#: 这一档只回答"钱可能花了吗",**不再单独决定回收成哪个码**:
#: 具体分类由 `reap_stalled()` 按上面两档 + 有没有 external_task_id 做,
#: 理由见 `AWAITING_PROVIDER_RESULT_STATUSES`。合成一个码是 REG-02 报的那条。
SPENT_STATUSES: frozenset[str] = SUBMIT_IN_DOUBT_STATUSES | AWAITING_PROVIDER_RESULT_STATUSES
