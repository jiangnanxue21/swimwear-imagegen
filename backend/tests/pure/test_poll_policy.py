"""平台状态轮询的判定与事务边界(任务 15)。

分三部分,与 `test_publish_policy.py` 同构:

    行为   `workflows/poll_policy.py` 是纯函数,逐条穷举平台可能的回答
    翻译   `RejectCategory` -> `RejectionReason` 的闭合性
    结构   `services/poll_service.py` 要碰库,用 AST 钉住它的事务边界

第三部分全部找**真实的调用节点**,不用 `"commit" in source`:字符串包含式的
断言,把调用删掉只留 import 照样绿(阶段 7 有过教训)。
"""
from __future__ import annotations

import ast

from app.core.enums import PublishStatus, RejectCategory
from app.workbench import platform as pf
from app.workflows import poll_policy as policy
from app.workflows import publish_policy
from tests.pure._helpers import BACKEND_ROOT

SERVICE = BACKEND_ROOT / "app" / "services" / "poll_service.py"


# ---------------------------------------------------------------- 翻译点唯一


def test_the_platform_status_map_is_the_same_object_not_a_copy():
    """两处各存一份的话,「平台说 REVIEWING 我们记成什么」会有两个答案。

    这条断言的是**同一个对象**而不是内容相等:内容相等挡不住有人在这里
    重新写一份字面量 —— 那份字面量今天恰好一样,明天单边改一处就开始分叉,
    而分叉的表现是同一个商品的状态取决于最后一次是提交路径写的还是轮询路径写的。
    """
    assert policy.PLATFORM_STATUS_MAP is publish_policy.PLATFORM_STATUS_MAP


# ---------------------------------------------------------------- 该不该问


def test_a_listing_without_an_external_id_is_never_polled():
    """没有 external_spu_id 就没有可问的对象,平台只会回 404。

    少了这条判断,日志里会堆满 404,而真正的 404(商品在平台上消失了)
    从此没人看得见。
    """
    assert not policy.should_poll(
        status=PublishStatus.SUBMITTED.value, external_spu_id=None
    )
    assert not policy.should_poll(
        status=PublishStatus.SUBMITTED.value, external_spu_id="   "
    )
    assert policy.should_poll(
        status=PublishStatus.SUBMITTED.value, external_spu_id="SIM-CREATE_OK-1-ab"
    )


def test_listed_and_rejected_are_deliberately_not_polled():
    """两者不在轮询范围里,但理由完全不同 —— 都写在 POLLABLE_STATUSES 的注释里。

    LISTED   平台仍可能单方面下架,但持续轮询全部在架商品的代价随上架量线性
             增长。首期靠 4.1 节 H 的清单核对兜住,而那条路径走 `poll_one`,
             显式绕开这张表
    PLATFORM_REJECTED   它等的是人,不是平台
    """
    assert PublishStatus.LISTED.value not in policy.POLLABLE_STATUSES
    assert PublishStatus.PLATFORM_REJECTED.value not in policy.POLLABLE_STATUSES
    assert PublishStatus.SUBMIT_RESULT_UNKNOWN.value in policy.POLLABLE_STATUSES


# ---------------------------------------------------------------- 退避


def test_the_first_poll_is_never_immediate():
    """返回 0 的话,一个刚提交的商品会被连续问到状态变化为止。"""
    assert policy.poll_backoff_seconds(0) == policy.MIN_POLL_INTERVAL_SECONDS
    assert policy.poll_backoff_seconds(-3) == policy.MIN_POLL_INTERVAL_SECONDS


def test_backoff_grows_then_stops_growing():
    """封顶的是**频率**不是次数 —— 轮询不通往放弃(见 poll_backoff_seconds)。"""
    assert policy.poll_backoff_seconds(1) > policy.poll_backoff_seconds(0)
    assert policy.poll_backoff_seconds(5) > policy.poll_backoff_seconds(3)
    assert policy.poll_backoff_seconds(999) == policy.MAX_POLL_INTERVAL_SECONDS
    # 大数不许溢出成一个荒唐的秒数
    assert policy.poll_backoff_seconds(10**6) == policy.MAX_POLL_INTERVAL_SECONDS


def test_the_minimum_interval_is_shorter_than_the_simulator_review_window():
    """否则人工测试时"审核中"那个中间态会被整段跳过。

    前端有一条分支专门显示它,而一个从来没被走过的分支等于没写。
    """
    from app.channels.simulator import REVIEW_SECONDS

    assert policy.MIN_POLL_INTERVAL_SECONDS < REVIEW_SECONDS


# ---------------------------------------------------------------- 平台答了


def test_a_listed_answer_moves_the_local_status():
    out = policy.classify_poll(status_code=200, body={"platform_status": "LISTED"})
    assert out.answered
    assert out.listing_status == PublishStatus.LISTED.value
    assert out.platform_status == "LISTED"
    assert out.rejection is None


def test_reviewing_maps_to_platform_reviewing():
    out = policy.classify_poll(status_code=200, body={"platform_status": "REVIEWING"})
    assert out.listing_status == PublishStatus.PLATFORM_REVIEWING.value


def test_delisted_maps_to_delisted():
    """4.1 节 H 的核对靠这一条:平台说下架了,本地才敢写下架。"""
    out = policy.classify_poll(status_code=200, body={"platform_status": "DELISTED"})
    assert out.listing_status == PublishStatus.DELISTED.value


def test_an_unchanged_answer_still_reports_the_status_not_none():
    """None 在 PollOutcome 里已经有另一个含义了(别动),不能兼作"没变"。

    调用方判断变没变要比较新旧两个值。让"没变"也返回 None,
    `apply_poll_outcome` 就分不清"平台说还在审核"和"平台没答上来"。
    """
    out = policy.classify_poll(
        status_code=200,
        body={"platform_status": "REVIEWING"},
        current_status=PublishStatus.PLATFORM_REVIEWING.value,
    )
    assert out.listing_status == PublishStatus.PLATFORM_REVIEWING.value


def test_an_unrecognised_platform_status_does_not_move_anything():
    """轮询问的就是状态,答案不认识就是没有答案。

    提交路径遇到不认识的状态会落 SUBMITTED(至少知道平台收下了),
    轮询没有这个兜底。原话仍然留着,拿去补映射表。
    """
    out = policy.classify_poll(status_code=200, body={"platform_status": "SHELVED_SOON"})
    assert out.answered
    assert out.listing_status is None
    assert out.platform_status == "SHELVED_SOON"
    assert out.needs_human


def test_a_200_with_no_status_at_all_is_not_an_operator_problem():
    """平台回了 200 却什么都没说 —— 记下来,但别叫人。"""
    out = policy.classify_poll(status_code=200, body={})
    assert out.listing_status is None
    assert not out.needs_human


# ---------------------------------------------------------------- 驳回


def test_a_rejection_carries_the_triage_category():
    out = policy.classify_poll(
        status_code=200,
        body={
            "platform_status": "REJECTED",
            "reject_category": RejectCategory.IMAGE.value,
            "reject_code": "IMG_BACKGROUND_NOT_WHITE",
            "reject_message": "主图背景不是纯白",
        },
    )
    assert out.listing_status == PublishStatus.PLATFORM_REJECTED.value
    assert out.rejection is not None
    assert out.rejection.category is RejectCategory.IMAGE
    assert out.rejection.code == "IMG_BACKGROUND_NOT_WHITE"
    assert out.rejection.note == "主图背景不是纯白"


def test_an_unknown_reject_category_falls_back_instead_of_raising():
    """平台**已经驳回**了,这是既成事实。

    一个我们不认识的类目字符串不该让这条驳回记不进台账 —— 那样商品会顶着
    "审核中"沉底,而它其实已经被拒了。
    """
    out = policy.classify_poll(
        status_code=200,
        body={"platform_status": "REJECTED", "reject_category": "WHO_KNOWS"},
    )
    assert out.rejection is not None
    assert out.rejection.category is RejectCategory.OTHER


def test_a_rejection_without_a_category_still_produces_a_signal():
    out = policy.classify_poll(status_code=200, body={"platform_status": "REJECTED"})
    assert out.rejection is not None
    assert out.rejection.category is RejectCategory.OTHER


# ---------------------------------------------------------------- 平台没答上来


def test_a_404_is_never_read_as_delisted():
    """本模块最重要的一条。

    平台查不到这个 ID,可能是它真的被删了,也可能是我们拿着 A 店铺的 ID 去问了
    B 店铺。猜成"已下架"的代价是:一个仍然挂在平台上的商品从 4.1 节 H 的
    清单里消失,而那正是清理预案要防的结局本身。
    """
    out = policy.classify_poll(status_code=404, body={})
    assert out.listing_status is None
    assert out.listing_status != PublishStatus.DELISTED.value
    assert out.needs_human


def test_rate_limiting_and_server_errors_change_nothing():
    """问不到 ≠ 状态变了。

    最危险的具体形态是把一次 5xx 记成 SUBMIT_FAILED:商品在平台上好好上着架,
    而运营看到"提交失败"之后会去重新提交一次。
    """
    for code in (429, 500, 502, 503):
        out = policy.classify_poll(status_code=code, body={})
        assert out.listing_status is None, code
        assert not out.needs_human, code


def test_auth_failure_asks_for_a_human_but_still_changes_nothing():
    for code in (401, 403):
        out = policy.classify_poll(status_code=code, body={})
        assert out.listing_status is None, code
        assert out.needs_human, code


def test_an_unrecognised_status_code_changes_nothing_either():
    out = policy.classify_poll(status_code=418, body={})
    assert out.listing_status is None
    assert out.needs_human


def test_a_transport_failure_is_not_the_same_as_a_submit_timeout():
    """提交超时是严重的(平台可能已经建好了商品);轮询超时什么也没发生。

    所以这里不产生 UNKNOWN、不进人工队列、不改状态 —— 就是下一轮再问。
    """
    out = policy.transport_failure()
    assert out.listing_status is None
    assert not out.answered
    assert not out.needs_human


def test_no_failure_branch_ever_proposes_a_status():
    """把上面几条合成一条不变量:答不上来的时候,一律不动本地状态。

    逐条写是为了失败时能指名道姓,这一条是为了将来有人加分支时被挡住。
    """
    for code in (401, 403, 404, 418, 429, 500, 503):
        assert policy.classify_poll(status_code=code, body={}).listing_status is None


# ---------------------------------------------------------------- 类目翻译


def test_every_reject_category_maps_to_a_real_reason_code():
    """闭合性。漏一个,「去处理」按钮就不知道往哪跳。

    `REJECTION_REASON_STEP` 对 `RejectionReason` 的闭合性另有测试守着,
    两条接起来才是完整的一条链:平台类目 -> 原因码 -> 流程步骤。
    """
    for category in RejectCategory:
        reason = pf.reason_for_category(category)
        assert isinstance(reason, pf.RejectionReason)
        assert reason in pf.REJECTION_REASON_STEP


def test_unknown_and_missing_categories_fall_back_to_other():
    assert pf.reason_for_category(None) is pf.RejectionReason.OTHER
    assert pf.reason_for_category("NOT_A_CATEGORY") is pf.RejectionReason.OTHER
    assert pf.reason_for_category("image") is pf.RejectionReason.IMAGE_ISSUE


# ---------------------------------------------------------------- 事务边界


def _tree() -> ast.AST:
    return ast.parse(SERVICE.read_text(encoding="utf-8"))


def _fn(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _call_names(node: ast.AST) -> list[str]:
    """归一成可比较的名字,如 'session.commit' / '_ask'。"""
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


def test_the_backoff_is_committed_before_the_platform_is_asked():
    """推进 `next_poll_at` 就是这张表的租约(见模块顶部)。

    放在外部调用之后的话,两个 worker 会同时问同一行 —— 各自都看到它到期。
    轮询虽然便宜,平台的限流配额不便宜。
    """
    body = _fn("poll_due").body
    committed_at = None
    asked_at = None
    for i, stmt in enumerate(body):
        names = _call_names(stmt)
        if committed_at is None and "session.commit" in names:
            committed_at = i
        if asked_at is None and "_ask" in names:
            asked_at = i
    assert committed_at is not None, "poll_due 里没有 checkpoint 提交"
    assert asked_at is not None, "poll_due 里没有外部调用"
    assert committed_at < asked_at, "外部调用发生在 checkpoint 之前"


def test_reserve_pushes_the_next_poll_time_forward():
    """租约本身:`_reserve` 必须写 next_poll_at,否则同一行会被反复领。"""
    targets = []
    for node in ast.walk(_fn("_reserve")):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            targets.append(node.attr)
    assert "next_poll_at" in targets
    assert "poll_attempt_count" in targets


def test_asking_the_platform_owns_no_transaction():
    """`_ask` 里出现 commit/rollback,就说明有个事务跨越了这次调用。"""
    names = _call_names(_fn("_ask"))
    for forbidden in ("session.commit", "session.rollback", "session.flush"):
        assert forbidden not in names, f"_ask 不该碰事务,但它调用了 {forbidden}"


def test_results_are_saved_in_a_fresh_session():
    names = _call_names(_fn("_save"))
    assert "session_factory" in names
    assert "session.commit" in names


def test_an_unanswered_poll_does_not_touch_last_polled_at():
    """那一列的含义是「我们对平台的了解是什么时候的」。

    一次失败的轮询没有更新任何了解。写上去的话,界面上会显示"刚刚同步过",
    而实际上已经连不上平台好几个小时了 —— 最坏的一种错误:它让人不去查。
    """
    fn = _fn("apply_poll_outcome")
    guard_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            if "listing_status" in ast.dump(node.test) and any(
                isinstance(op, ast.Is) for op in node.test.ops
            ):
                guard_line = node.lineno
                # 这个分支必须就此返回,不能继续往下走
                assert any(isinstance(s, ast.Return) for s in node.body), (
                    "答不上来的分支必须直接返回"
                )
                break
    assert guard_line is not None, "apply_poll_outcome 里找不到「别动」的判断"

    writes = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and node.attr == "last_polled_at"
    ]
    assert writes, "apply_poll_outcome 从来不写 last_polled_at?"
    for line in writes:
        assert line > guard_line, "last_polled_at 写在了「答不上来」的判断之前"


def test_forcing_a_poll_is_a_separate_function_not_a_flag():
    """定时扫描要节制,人点按钮要立刻回答 —— 两种意图不该长得一样。

    同 `publish_service.reconcile_unknown` 不做成 `confirm=True`。
    """
    scheduled = _fn("poll_due")
    arg_names = {a.arg for a in scheduled.args.args} | {
        a.arg for a in scheduled.args.kwonlyargs
    }
    assert "force" not in arg_names
    assert _fn("poll_one") is not None
