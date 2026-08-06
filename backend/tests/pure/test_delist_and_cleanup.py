"""DELIST 路径与测试商品清理(任务 16 / 4.1 节 H)。

`cleanup_service.py` 与 `publish_service.py` 都要碰库,所以这里的做法和
`test_publish_policy.py` 末尾那批一样:能纯函数验的纯函数验,验不了的用 AST
钉住结构。

被钉住的四件事,每一件都对应一个具体的坏结局:

    DELIST 不看草稿状态     否则清理预案在最需要它的时候执行不下去
    下架报文不带内容        否则一次清理动作会顺手把旧内容发上去
    清理必须限定作用域      否则一次手滑把生产上所有在架商品下架
    清单先看后做            否则"看一眼"和"动手"共用一条代码路径
"""
from __future__ import annotations

import ast

from app.channels.simulator import (
    EXTERNAL_ID_PREFIX,
    SimScenario,
    is_simulated_external_id,
    make_external_id,
    pick_scenario,
)
from app.core.enums import PublishOperation, PublishStatus
from tests.pure._helpers import BACKEND_ROOT

PUBLISH = BACKEND_ROOT / "app" / "services" / "publish_service.py"
CLEANUP = BACKEND_ROOT / "app" / "services" / "cleanup_service.py"


# ---------------------------------------------------------------- 状态机


def test_the_delist_branch_has_its_own_in_flight_status():
    """7.3 节的下架分支需要一个"发起了、还没被平台确认"的状态。

    没有它的话下架排队之后 listing 只能落 SUBMITTING,而运营看到"提交中"
    会以为在上架。
    """
    assert PublishStatus.DELISTING.value == "DELISTING"
    assert PublishStatus.DELISTED.value in {s.value for s in PublishStatus}


def test_delist_queued_is_deliberately_absent():
    """7.3 节画的是三步,我们只落一个状态 —— 队列已经有一个权威表示了。

    再给它一个状态名,就有两处各自记录"排队了没有",而两处迟早会不一致
    (outbox 已经 LEASED、listing 还写着 DELIST_QUEUED),那时没人说得清该信哪个。
    """
    assert "DELIST_QUEUED" not in {s.value for s in PublishStatus}


# ---------------------------------------------------------------- 模拟商品识别


def test_simulator_ids_are_recognisable_by_prefix_alone():
    """4.1 节 H 的清单要能一眼分出"哪些是模拟的"。

    拿着一份混着 SIM- 的清单去平台后台核对,核不上,然后这份清单就没人信了。
    """
    from datetime import UTC, datetime

    made = make_external_id(
        scenario=SimScenario.CREATE_OK,
        spu="SW-001",
        now=datetime(2026, 7, 31, tzinfo=UTC),
    )
    assert made.startswith(f"{EXTERNAL_ID_PREFIX}-")
    assert is_simulated_external_id(made)


def test_a_structurally_broken_sim_id_is_still_a_sim_id():
    """用解析来回答"是不是模拟的"会让一个坏数据变成一次异常,而调用点是一份报表。"""
    assert is_simulated_external_id("SIM-")
    assert is_simulated_external_id("sim-whatever")
    assert is_simulated_external_id("SIM-NOT_A_SCENARIO-x")


def test_real_platform_ids_are_not_mistaken_for_simulated_ones():
    for real in ("SPU123456", "", None, "SIMILAR-1-2-3", "simulacrum"):
        assert not is_simulated_external_id(real), real


def test_the_simulator_never_refuses_a_delist():
    """一个会拒绝下架的模拟器会让清理预案演练不下去(4.1 节 H)。"""
    picked = pick_scenario(spu="SW-001", operation=PublishOperation.DELIST)
    assert picked is SimScenario.CREATE_OK


# ---------------------------------------------------------------- publish_service


def _fn(path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
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
            out.append(
                f"{func.value.id}.{func.attr}"
                if isinstance(func.value, ast.Name)
                else func.attr
            )
    return out


def test_the_draft_status_check_is_skipped_for_delist():
    """清理要下架的恰恰是那批草稿早已过期的商品。

    `_assert_submittable` 会把 STALE 挡在门外 —— 用一条"防止发错内容"的规则
    去挡一个**根本不发内容**的操作,结果是残留在平台上的商品没有任何办法下架。

    断言的是 `_assert_submittable` 被包在一个条件里,而那个条件提到了 DELIST。
    直接调用(不在任何 If 里)就是没有豁免。
    """
    fn = _fn(PUBLISH, "enqueue")
    guarded = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if "_assert_submittable" not in _call_names(node):
            continue
        if "DELIST" in ast.dump(node.test):
            guarded = True
    assert guarded, "enqueue 里的草稿状态检查没有为 DELIST 开豁免"

    # 而且豁免只对 DELIST 开。写成 `if requested is None` 之类会顺手把
    # 所有显式声明的操作都放过去
    assert "_assert_submittable" in _call_names(fn)


def test_a_delist_request_carries_no_listing_content():
    """把一份可能已经过期的 payload 塞进下架请求,平台若照单更新,
    我们就在下架的同时把旧内容发了上去 —— 一次清理动作变成了一次内容回退。
    """
    fn = _fn(PUBLISH, "enqueue")
    found = False
    for node in ast.walk(fn):
        if isinstance(node, ast.IfExp) and "DELIST" in ast.dump(node.test):
            dumped = ast.dump(node)
            if "MappedListing" in dumped and "_mapped_from_draft" in dumped:
                found = True
    assert found, "enqueue 没有为 DELIST 构造空报文"


def test_the_queued_status_table_covers_every_operation():
    """写成表而不是 if 链,理由同 `generic._ROUTES`:漏改一处的代价是
    "下架排队了,页面上显示提交中",而运营会以为在上架。
    """
    src = ast.dump(_fn(PUBLISH, "_queued_status"))
    for op in (PublishOperation.UPDATE, PublishOperation.REPUBLISH, PublishOperation.DELIST):
        assert op.value in src, f"_queued_status 没有覆盖 {op.value}"
    assert PublishStatus.DELISTING.value in src


def test_the_test_batch_tag_is_never_overwritten_with_nothing():
    """一次没带批次号的更新不该把这一行从清理清单里抹掉。

    抹掉一行的后果是没人说得清平台上到底还剩几个 —— 4.1 节 H 要防的结局本身。
    """
    fn = _fn(PUBLISH, "enqueue")
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == "test_batch_tag"
                for t in node.targets
            )
        ):
            # 赋值必须发生在一个以 test_batch_tag 为条件的 If 里
            enclosing = [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.If) and node in ast.walk(n)
            ]
            assert enclosing, "test_batch_tag 被无条件赋值,None 会覆盖既有批次号"
            assert any("test_batch_tag" in ast.dump(n.test) for n in enclosing)


# ---------------------------------------------------------------- cleanup_service


def test_cleanup_refuses_to_run_without_a_scope():
    """一次跑成全量的清理会把生产上所有在架商品下架,而那通常撤不回来。

    这个模块存在的目的是防止事故,它自己不能是事故的来源。
    """
    fn = _fn(CLEANUP, "_require_scope")
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
    assert raises, "_require_scope 从来不拒绝任何调用"

    # 三个入口都必须先过这一关
    for entry in ("inventory", "plan_delist", "run_delist"):
        names = _call_names(_fn(CLEANUP, entry))
        assert "_require_scope" in names or "inventory" in names or "plan_delist" in names, (
            f"{entry} 没有经过作用域检查"
        )


def test_looking_and_doing_are_two_functions():
    """做成一个带 `dry_run=False` 的函数也能实现同样的效果,但那样
    "看一眼"和"动手"共用一条代码路径,而参数是最容易在复制粘贴时被改掉的东西。
    """
    plan = _fn(CLEANUP, "plan_delist")
    assert "enqueue" not in _call_names(plan), "plan_delist 不许真的排队"
    assert "publish_service.enqueue" not in _call_names(plan)

    run = _fn(CLEANUP, "run_delist")
    assert "publish_service.enqueue" in _call_names(run)


def test_queueing_a_cleanup_never_commits_on_its_own():
    """一次清理是一批排队动作,要么整批进队列要么一个都不进。

    半批进队列的后果是重跑时前半批因为幂等键相同被复用(没问题),
    而人会以为这次排了全部。
    """
    names = _call_names(_fn(CLEANUP, "run_delist"))
    assert "session.commit" not in names


def test_the_verify_report_reflects_the_state_after_asking_the_platform():
    """只用问之前那份快照的话,报表会说"全部已下架",而那正是它要验的东西。"""
    fn = _fn(CLEANUP, "verify")
    body_lines = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "inventory"
    ]
    poll_lines = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "poll_one"
    ]
    assert len(body_lines) >= 2, "verify 必须在问平台前后各读一次库"
    assert poll_lines, "verify 没有问平台"
    assert min(body_lines) < poll_lines[0] < max(body_lines)
