"""发布页的状态派生与接线(任务 18)。

分四部分:

    穷举   `PublishStatus` 的每一个取值都得有展示状态、每个码都得有文案
    行为   跨来源的组合 —— 这是 `publish_view` 存在的理由,也是这里的重点
    措辞   `describe_enqueue`,`reused_terminal` 的落点
    接线   用 AST 钉住"字段真的被读了""路由真的注册了"

第四部分是本轮的硬要求。A27 交接把 `reused_terminal` 列成缺口:字段算得对、
位置也对,但**没有任何读取点**,而交付门禁那条「落地的模块真的被接线了」
只查函数不查字段,抓不到它。所以这里补上,用真实的 AST 节点而不是
`"reused_terminal" in source` —— 后者在字段只出现在注释里时照样绿。
"""
from __future__ import annotations

import ast

from app.core.enums import (
    PublishAttemptStatus,
    PublishOutboxStatus,
    PublishStatus,
)
from app.workflows import publish_view as pv
from tests.pure._helpers import BACKEND_ROOT

SERVICE = BACKEND_ROOT / "app" / "services" / "publish_service.py"
API = BACKEND_ROOT / "app" / "api" / "publish.py"
ROUTER = BACKEND_ROOT / "app" / "api" / "router.py"


def _facts(**kw) -> pv.PublishFacts:
    """默认是"草稿可提交、什么都还没发生"。每条用例只写它关心的那几个字段。"""
    base = {"draft_submittable": True}
    base.update(kw)
    return pv.PublishFacts(**base)


# ---------------------------------------------------------------- 一、穷举


def test_every_publish_status_has_a_display_status():
    """新加一个 `PublishStatus` 却忘了决定它怎么显示,必须在这里红。

    忘了的后果不是崩溃 —— `_display` 会走进"查不到"分支返回 NEEDS_FIX,
    也就是说界面会给出一个**自信而错误**的答案,而且没有任何征兆。
    """
    missing = [s.value for s in PublishStatus if s.value not in pv.BASE_DISPLAY]
    assert not missing, f"这些状态没有对应的展示状态:{missing}"


def test_the_display_table_has_no_stale_entries():
    """反向也要查:枚举里删掉一个取值,表里那一行就该跟着删。

    留着不报错,但它会让下一个读表的人以为那个状态还在用。
    """
    unknown = [k for k in pv.BASE_DISPLAY if k not in {s.value for s in PublishStatus}]
    assert not unknown, f"表里这些取值已经不在 PublishStatus 里了:{unknown}"


def test_every_code_has_a_label():
    """界面上的中文只有后端一份。少一条,前端会显示一个枚举名给运营看。"""
    for status in pv.PublishDisplayStatus:
        assert status in pv.DISPLAY_STATUS_LABELS, f"展示状态 {status} 缺文案"
    for action in pv.NextActionCode:
        assert action in pv.NEXT_ACTION_LABELS, f"推荐动作 {action} 缺文案"
    for act in pv.ActionCode:
        assert act in pv.ACTION_LABELS, f"按钮 {act} 缺文案"


def test_every_blocker_has_both_a_label_and_advice():
    """光说"被挡住了"没用,要说下一步做什么 —— 否则运营只能来问人。"""
    for code in pv.BlockerCode:
        assert pv.BLOCKER_LABELS.get(code), f"{code} 缺标签"
        assert pv.BLOCKER_ADVICE.get(code), f"{code} 缺处置建议"


def test_terminal_attempt_statuses_agree_with_the_service():
    """判定层复制了一份终态集合,两份必须一致。

    复制是被迫的:`publish_service` 要 sqlalchemy,一 import 本模块就进不了
    `tests/pure/`。所以用 AST 从源码里读出 service 那一份来比 —— 单边改一处
    的后果是"这条链路死没死"在接口层和服务层有两个答案,而运营看到的是接口层那个。
    """
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if "TERMINAL_ATTEMPT_STATUSES" not in names or node.value is None:
            continue
        for sub in ast.walk(node.value):
            # PublishAttemptStatus.FAILED.value -> 取中间那层的 attr
            if isinstance(sub, ast.Attribute) and sub.attr == "value":
                inner = sub.value
                if isinstance(inner, ast.Attribute):
                    found.add(inner.attr)

    assert found, "没能从 publish_service.py 里解析出 TERMINAL_ATTEMPT_STATUSES"
    service_side = {getattr(PublishAttemptStatus, n).value for n in found}
    assert service_side == pv._TERMINAL_ATTEMPTS, (
        f"两份终态集合不一致:service={sorted(service_side)}、"
        f"view={sorted(pv._TERMINAL_ATTEMPTS)}"
    )


# ---------------------------------------------------------------- 二、行为


def test_a_dead_outbox_on_an_in_flight_listing_reads_as_stalled():
    """**本模块存在的理由。**

    listing 说 SUBMITTING、attempt 说 IN_FLIGHT,只看这两样的界面会说
    「正在处理」。而 outbox 已经 DEAD —— 不会再投了。运营会一直等。
    """
    view = pv.build_view(
        _facts(
            listing_status=PublishStatus.SUBMITTING.value,
            attempt_status=PublishAttemptStatus.IN_FLIGHT.value,
            outbox_status=PublishOutboxStatus.DEAD.value,
        )
    )
    assert view.display_status is pv.PublishDisplayStatus.STALLED
    # A45-batch12-2 / EX-04:停住的结论不变,**建议变了**。
    #
    # 原来是 INVESTIGATE,而那个值的含义是"需要人工核实,没有按钮"。
    # 在 `/redeliver` 接出来之前那句话是准确的;现在有按钮了,继续说没有
    # 会让运营根本不去找它 —— 于是新出口等于没接。
    assert view.next_action is pv.NextActionCode.REDELIVER
    assert pv.ActionCode.REDELIVER in view.allowed_actions


def test_a_stalled_listing_still_offers_a_way_out():
    """EX-04 的正题:**每一种"停住"都必须有一个可点的出口。**

    这条守的不是某个具体建议,是"不许再出现一个只能干瞪眼的终局"。
    报告里 EX-04 的真实危害不是少两个按钮 —— 是运营为了把商品弄上架,
    会去改草稿换输入,从而算出一把**新的幂等键**,而那是唯一真的会在平台上
    多出一件商品的操作。闸门没有出口时,人会自己找一条更危险的路。
    """
    unknown = pv.build_view(
        _facts(
            listing_status=PublishStatus.SUBMIT_RESULT_UNKNOWN.value,
            attempt_status=PublishAttemptStatus.UNKNOWN.value,
        )
    )
    assert pv.ActionCode.RECONCILE in unknown.allowed_actions

    dead = pv.build_view(
        _facts(
            listing_status=PublishStatus.SUBMITTING.value,
            attempt_status=PublishAttemptStatus.FAILED.value,
            outbox_status=PublishOutboxStatus.DEAD.value,
        )
    )
    assert pv.ActionCode.REDELIVER in dead.allowed_actions


def test_recovery_actions_stay_off_healthy_listings():
    """两条恢复动作都会**真的对外发一次请求**,不该出现在正常的行上。"""
    healthy = pv.build_view(
        _facts(
            listing_status=PublishStatus.LISTED.value,
            attempt_status=PublishAttemptStatus.SUCCEEDED.value,
            outbox_status=PublishOutboxStatus.DONE.value,
            external_spu_id="SPU-1",
        )
    )
    assert pv.ActionCode.RECONCILE not in healthy.allowed_actions
    assert pv.ActionCode.REDELIVER not in healthy.allowed_actions


def test_a_terminal_attempt_alone_is_enough_to_read_as_stalled():
    """取「或」不取「且」。

    投递失败先写 attempt,再决定 outbox 要不要重试。要求两边都到位的话,
    「attempt 已经有结论、而根本没有 outbox 行接着投」这种形状会被读成
    "正在处理",而它永远不会自己往前走。

    ## 为什么这里的 outbox 是 None 而不是 PENDING(A45-batch12-3 / REG-05)

    这条用例原来传的是 `outbox_status=PENDING`,断言仍然是 STALLED ——
    也就是把「outbox 还在投递路径上」和「链路已经死了」判成同一件事。
    那正好是 `redeliver_dead()` 之后的形状,于是重新投递在界面上等于没生效。
    见下面 `test_a_redelivered_outbox_is_not_stalled_even_after_a_failed_attempt`。

    "或"要守的是**没有活跃 outbox 时**单凭 attempt 也能判死,这条用例现在
    钉的是那个。
    """
    view = pv.build_view(
        _facts(
            listing_status=PublishStatus.SUBMITTING.value,
            attempt_status=PublishAttemptStatus.FAILED.value,
            outbox_status=None,
        )
    )
    assert view.display_status is pv.PublishDisplayStatus.STALLED


def test_a_redelivered_outbox_is_not_stalled_even_after_a_failed_attempt():
    """A45-batch12-3 / REG-05:重新投递之后界面必须说"已排队"。

    `redeliver_dead()` 提交之后库里长这样:

        Listing  SUBMITTING
        Outbox   PENDING     <- worker 马上就会取
        Attempt  FAILED      <- 上一次的结局,历史,写完不改

    只看后两样会判成 STALLED + INVESTIGATE,而且因为 STALLED 不在
    `hard_stops` 里,普通 `SUBMIT` 按钮还会亮着。运营看到"重新投递没生效"
    之后点普通提交 —— 那条路会算出一把**新的幂等键**,也就是真的可能在
    平台上多出一件商品。重新投递给的出口,反被界面推回了它想避开的那条路。
    """
    view = pv.build_view(
        _facts(
            listing_status=PublishStatus.SUBMITTING.value,
            attempt_status=PublishAttemptStatus.FAILED.value,
            outbox_status=PublishOutboxStatus.PENDING.value,
        )
    )
    assert view.display_status is pv.PublishDisplayStatus.QUEUED
    assert view.next_action is pv.NextActionCode.WAIT
    assert pv.ActionCode.SUBMIT not in view.allowed_actions, (
        "重新投递之后仍然开放普通提交 —— 那条路会算出一把新幂等键"
    )


def test_a_leased_outbox_is_not_stalled_mid_write():
    """worker 正拿着这一行的时候,attempt 可能已经写成终态了。

    投递失败的写入顺序是「先写 attempt,再决定 outbox 重试还是判死」,
    中间那一瞬 outbox 还是 LEASED。那一瞬报 STALLED 会让一条**正在被处理**
    的记录出现在"需要人看一眼"的列表里 —— 而下一秒它自己就走了。
    """
    view = pv.build_view(
        _facts(
            listing_status=PublishStatus.SUBMITTING.value,
            attempt_status=PublishAttemptStatus.FAILED.value,
            outbox_status=PublishOutboxStatus.LEASED.value,
        )
    )
    assert view.display_status is not pv.PublishDisplayStatus.STALLED


def test_a_dead_outbox_on_a_listed_product_is_not_stalled():
    """已经上架了就不是"停住"。

    投递成功之后,outbox 也可能因为后续一次更新失败而 DEAD。把它读成停住,
    会让一批**已经在卖**的商品出现在"需要人看一眼"的列表里,然后那个列表
    就没人看了。
    """
    view = pv.build_view(
        _facts(
            listing_status=PublishStatus.LISTED.value,
            outbox_status=PublishOutboxStatus.DEAD.value,
        )
    )
    assert view.display_status is pv.PublishDisplayStatus.LISTED


def test_a_pending_outbox_reads_as_queued_not_in_flight():
    """排队和在途对运营是两件事:前者可以等,后者不能重复点。

    listing 两者都记 SUBMITTING(队列的权威表示是 outbox 行,见
    `PublishStatus.DELISTING` 的注释),所以这个区分只能在判定层做。
    """
    view = pv.build_view(
        _facts(
            listing_status=PublishStatus.SUBMITTING.value,
            outbox_status=PublishOutboxStatus.PENDING.value,
        )
    )
    assert view.display_status is pv.PublishDisplayStatus.QUEUED
    assert view.next_action is pv.NextActionCode.WAIT


def test_result_unknown_sends_you_to_poll_not_to_resubmit():
    """4.1 节 D:「API 超时后的未知结果可恢复」。

    平台可能已经收到了。直接重发是在赌平台真的实现了幂等键 ——
    赌输的代价是同一件商品在平台上出现两次。
    """
    view = pv.build_view(_facts(listing_status=PublishStatus.SUBMIT_RESULT_UNKNOWN.value))
    assert view.display_status is pv.PublishDisplayStatus.RESULT_UNKNOWN
    assert view.next_action is pv.NextActionCode.POLL
    assert pv.ActionCode.SUBMIT not in view.allowed_actions
    assert pv.BlockerCode.RESULT_UNKNOWN in {b.code for b in view.blocking_reasons}


def test_submit_is_not_offered_while_something_is_already_in_flight():
    """连点两次提交不该产生第二次投递。

    幂等键那一层会兜住(`_reuse` 不新建尝试),但让按钮亮着等于把幂等
    当成常规路径用 —— 而每兜一次,运营就少一次"我是不是点重了"的反馈。
    """
    view = pv.build_view(
        _facts(
            listing_status=PublishStatus.SUBMITTING.value,
            outbox_status=PublishOutboxStatus.LEASED.value,
        )
    )
    assert pv.ActionCode.SUBMIT not in view.allowed_actions
    assert view.next_action is pv.NextActionCode.WAIT


def test_an_unresolved_rejection_does_not_block_submitting():
    """改完内容重新提交**正是**解决驳回的方式。

    挡住它等于告诉运营"你得先关掉驳回才能修驳回"。驳回只影响推荐动作,
    不影响按钮可用性。
    """
    view = pv.build_view(
        _facts(
            listing_status=PublishStatus.PLATFORM_REJECTED.value,
            external_spu_id="SPU-1",
            unresolved_rejections=2,
            rejection_auto_closeable=True,
        )
    )
    assert pv.ActionCode.SUBMIT in view.allowed_actions
    assert view.next_action is pv.NextActionCode.RESOLVE_REJECTION


def test_poll_and_delist_need_an_external_id():
    """没有外部 ID = 那次提交从没到达平台,这两个按钮点了也是空转。

    `poll_one` 会直接返回 unanswered,下架没有目标 —— 而界面会显示
    "已刷新",于是运营以为问过了。
    """
    without = pv.build_view(_facts(listing_status=PublishStatus.SUBMIT_FAILED.value))
    assert pv.ActionCode.POLL not in without.allowed_actions
    assert pv.ActionCode.DELIST not in without.allowed_actions

    with_id = pv.build_view(
        _facts(listing_status=PublishStatus.LISTED.value, external_spu_id="SPU-1")
    )
    assert pv.ActionCode.POLL in with_id.allowed_actions
    assert pv.ActionCode.DELIST in with_id.allowed_actions


def test_a_delisted_listing_offers_no_second_delist():
    """已经下架了还亮着下架按钮,点下去只会多一条没有意义的提交尝试。"""
    view = pv.build_view(
        _facts(listing_status=PublishStatus.DELISTED.value, external_spu_id="SPU-1")
    )
    assert pv.ActionCode.DELIST not in view.allowed_actions
    # 但仍然可以刷新 —— "平台上到底还有没有它"这个问题对已下架的行同样成立
    assert pv.ActionCode.POLL in view.allowed_actions


def test_an_api_rejection_reports_that_the_gate_cannot_close_it():
    """任务 20 的缺口必须如实报出来,不许假装没有。

    `resolve_gate()` 认的证据是导出,而 API 上架的商品不走导出 ——
    运营改完会发现关不掉,然后开始怀疑是自己操作错了。
    """
    view = pv.build_view(
        _facts(
            listing_status=PublishStatus.PLATFORM_REJECTED.value,
            external_spu_id="SPU-1",
            unresolved_rejections=1,
            rejection_auto_closeable=False,
        )
    )
    codes = {b.code for b in view.blocking_reasons}
    assert pv.BlockerCode.REJECTION_CANNOT_AUTO_CLOSE in codes
    advice = next(
        b.advice for b in view.blocking_reasons
        if b.code is pv.BlockerCode.REJECTION_CANNOT_AUTO_CLOSE
    )
    assert "手工" in advice, "得告诉运营现在该怎么办,不能只说系统关不掉"


def test_no_rejection_is_not_the_same_as_auto_closeable():
    """`rejection_auto_closeable=None` 表示"不适用",不是"能自动关"。

    合并成一个布尔的话,"没有驳回"和"有驳回且系统能自证"会走进同一个分支,
    界面分不出来。
    """
    view = pv.build_view(_facts(listing_status=PublishStatus.LISTED.value,
                                external_spu_id="SPU-1"))
    assert not view.blocking_reasons


def test_a_draft_that_cannot_be_submitted_blocks_and_says_so():
    """BUILDING / INVALID / STALE 提交上去是在浪费一次平台配额。

    STALE 更糟:它会把**旧内容**发上去,而页面上显示的是新内容。
    """
    view = pv.build_view(pv.PublishFacts(draft_submittable=False))
    assert view.next_action is pv.NextActionCode.FIX_DRAFT
    assert pv.ActionCode.SUBMIT not in view.allowed_actions
    assert pv.ActionCode.DRY_RUN not in view.allowed_actions
    assert pv.BlockerCode.DRAFT_NOT_SUBMITTABLE in {b.code for b in view.blocking_reasons}


def test_an_unknown_publish_status_does_not_claim_to_be_in_flight():
    """查不到就**不猜**。

    猜一个"大概在跑吧"会给出自信而错误的答案;NEEDS_FIX 至少会把人
    引到能看见原始状态的地方。
    """
    view = pv.build_view(_facts(listing_status="SOME_NEW_STATUS"))
    assert view.display_status is pv.PublishDisplayStatus.NEEDS_FIX


def test_a_product_that_was_never_submitted_is_offered_the_submit_button():
    view = pv.build_view(_facts())
    assert view.display_status is pv.PublishDisplayStatus.NOT_SUBMITTED
    assert view.next_action is pv.NextActionCode.SUBMIT
    assert pv.ActionCode.SUBMIT in view.allowed_actions
    assert pv.ActionCode.DRY_RUN in view.allowed_actions


def test_dry_run_stays_available_even_when_submitting_is_blocked():
    """干跑不发任何东西,所以草稿状态之外没有别的前提。

    "上次结果未知"恰恰是运营最想先看一眼报文的时候。
    """
    view = pv.build_view(_facts(listing_status=PublishStatus.SUBMIT_RESULT_UNKNOWN.value))
    assert pv.ActionCode.DRY_RUN in view.allowed_actions
    assert pv.ActionCode.SUBMIT not in view.allowed_actions


def test_every_view_serializes_to_json_shaped_data():
    """接口直接返回 `as_dict()`,里面不能剩下枚举对象。"""
    out = pv.build_view(_facts(listing_status=PublishStatus.LISTED.value)).as_dict()
    assert isinstance(out["display_status"], str)
    assert isinstance(out["next_action"], str)
    assert all(isinstance(a, str) for a in out["allowed_actions"])
    for blocker in out["blocking_reasons"]:
        assert set(blocker) == {"code", "label", "advice"}
        assert all(isinstance(v, str) for v in blocker.values())


# ---------------------------------------------------------------- 三、措辞


def test_reusing_a_dead_attempt_says_nothing_will_happen():
    """A27 交接点名的缺口。

    幂等键含草稿指纹,草稿不变键就不变 —— 一条退避耗尽落进 DEAD 的提交,
    运营再点「提交」仍然走进 `_reuse`,`reused=True` 照旧。只看 `reused`
    的界面会说「已提交,正在处理」,而实际上什么都不会发生。
    """
    notice = pv.describe_enqueue(reused=True, reused_terminal=True, dry_run=False)
    assert notice.code is pv.EnqueueNoticeCode.ALREADY_STALLED
    assert notice.will_deliver is False


def test_reusing_a_live_attempt_still_promises_delivery():
    """沿用在途的那一条是**正常**的幂等行为,不该报警。"""
    notice = pv.describe_enqueue(reused=True, reused_terminal=False, dry_run=False)
    assert notice.code is pv.EnqueueNoticeCode.ALREADY_IN_PROGRESS
    assert notice.will_deliver is True


def test_dry_run_never_promises_delivery():
    """干跑的定义就是"构造了报文但不发出去"。"""
    for terminal in (True, False):
        notice = pv.describe_enqueue(reused=False, reused_terminal=terminal, dry_run=True)
        assert notice.code is pv.EnqueueNoticeCode.DRY_RUN
        assert notice.will_deliver is False


def test_a_fresh_enqueue_promises_delivery():
    notice = pv.describe_enqueue(reused=False, reused_terminal=False, dry_run=False)
    assert notice.code is pv.EnqueueNoticeCode.QUEUED
    assert notice.will_deliver is True


def test_every_enqueue_notice_code_is_reachable():
    """枚举里有一个到不了的取值,等于有一段没有人读的文案。"""
    reached = {
        pv.describe_enqueue(reused=r, reused_terminal=t, dry_run=d).code
        for r in (True, False)
        for t in (True, False)
        for d in (True, False)
    }
    assert reached == set(pv.EnqueueNoticeCode)


# ---------------------------------------------------------------- 四、接线


def _attribute_readers(name: str) -> set[str]:
    """全仓里真正**读**这个字段的文件(AST 节点,不是字符串包含)。"""
    root = BACKEND_ROOT / "app"
    files: set[str] = set()
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == name:
                files.add(path.name)
    return files


def test_reused_terminal_is_actually_read_somewhere():
    """A27 交接:「`reused_terminal` 目前无人消费」—— 本轮必须补上。

    交付门禁那条「落地的模块真的被接线了」只查**函数**不查**字段**,
    所以它抓不到这一类。用 AST 找真实的属性读取节点,不用
    `"reused_terminal" in source`:后者在字段只出现在注释里时照样绿,
    而这个字段的注释恰好很长。
    """
    readers = _attribute_readers("reused_terminal") - {"publish_service.py"}
    assert readers, (
        "reused_terminal 仍然没有任何读取点。它要防的结局是:运营对着一件"
        "已经死掉的提交一直等 —— 算得再对,没人读就不会发生任何事。"
    )


def test_the_publish_router_is_registered():
    """路由写好了没挂上,表现是全部端点 404,而测试可以全绿。"""
    source = ROUTER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # include_router(publish.router) —— args[0] 是 Attribute,
    # 它的 value 才是模块名那个 Name
    included = {
        node.args[0].value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "include_router"
        and node.args
        and isinstance(node.args[0], ast.Attribute)
        and isinstance(node.args[0].value, ast.Name)
    }
    assert included, "没有解析出任何 include_router 调用"
    assert "publish" in included, f"发布路由没有挂进 api_router,已挂的有:{sorted(included)}"
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "app.api"
        for alias in node.names
    }
    assert "publish" in imported, "app/api/publish.py 没有被 router.py 导入"
    assert "publish.router" in source, "发布路由没有挂进 api_router"


def _registered_paths() -> set[str]:
    """从 `api/publish.py` 的装饰器里取出全部注册路径(含前缀)。"""
    tree = ast.parse(API.read_text(encoding="utf-8"))
    prefix = ""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "APIRouter"
        ):
            for kw in node.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    prefix = str(kw.value.value)
    paths: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in {"get", "post", "put", "delete"}
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                paths.add(prefix + str(dec.args[0].value))
    return paths


def test_every_action_code_has_an_endpoint_behind_it():
    """`allowed_actions` 里出现一个没有端点的动作 = 前端渲染出一个点了报 404
    的按钮,而那种错误只有运营会碰到。

    SUBMIT 与 DRY_RUN 共用同一个端点(`dry_run` 是入参),所以按端点核对
    而不是按动作数核对。
    """
    paths = _registered_paths()
    assert "/publish/products/{product_id}/submit" in paths  # SUBMIT / DRY_RUN
    assert "/publish/listings/{listing_id}/poll" in paths     # POLL
    assert "/publish/listings/{listing_id}/delist" in paths   # DELIST


def test_the_cleanup_inventory_is_exposed():
    """4.1 节 H 的清单必须有接口 —— 只有命令行的话,运营看不到它。"""
    assert "/publish/cleanup/inventory" in _registered_paths()


def test_poll_one_is_wired_to_the_refresh_endpoint():
    """A26 交接:「18 主要是把 `poll_one` / `cleanup_service.inventory` 接成接口」。

    找真实调用节点。定时轮询走的是 `poll_due`,人点刷新必须走 `poll_one` ——
    后者不看到期、不看本地状态,而那正是"平台上现在到底还有没有它"要问的。
    """
    tree = ast.parse(API.read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "poll_one" in called, "刷新端点没有调 poll_one"
    assert "inventory" in called, "清单端点没有调 cleanup_service.inventory"
    assert "safe_preview" in called, "干跑没有把脱敏报文返回给前端(6.4 发布前)"
