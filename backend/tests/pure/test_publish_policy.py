"""发布提交的判定与事务边界(任务 14)。

分两部分:

    行为   `workflows/publish_policy.py` 是纯函数,可以逐条穷举九种响应形状
    结构   `services/publish_service.py` 要碰库,这里用 AST 钉住它的**事务边界**

第二部分学 `test_transaction_boundaries.py` 的写法:全部找**真实的调用节点**,
不用 `"commit" in source`。字符串包含式的断言,把调用删掉只留 import 照样绿。
"""
from __future__ import annotations

import ast

from app.channels import registry
from app.core.enums import (
    PublishAttemptStatus,
    PublishOperation,
    PublishStatus,
)
from app.core.errors import ErrorCode
from app.workflows import publish_policy as policy
from tests.pure._helpers import BACKEND_ROOT, expect_raises

SERVICE = BACKEND_ROOT / "app" / "services" / "publish_service.py"


# ---------------------------------------------------------------- 操作类型


def test_operation_is_derived_from_state_not_from_the_caller():
    """按钮只知道「提交」,只有库里那一列知道这个商品在不在平台上。"""
    assert policy.resolve_operation(external_spu_id=None) is PublishOperation.CREATE
    assert policy.resolve_operation(external_spu_id="") is PublishOperation.CREATE
    assert policy.resolve_operation(external_spu_id="SIM-X-1-ab") is PublishOperation.UPDATE


def test_creating_something_that_already_exists_is_refused_not_silently_upgraded():
    """静默改成 UPDATE 会让调用方永远不知道自己的状态判断是错的。"""
    exc = expect_raises(
        policy.PublishPolicyError,
        policy.resolve_operation,
        external_spu_id="SIM-CREATE_OK-1-ab",
        requested=PublishOperation.CREATE,
    )
    assert exc.code is ErrorCode.PUBLISH_DUPLICATE_CREATE


def test_updating_something_that_was_never_created_is_refused():
    """悄悄改成 CREATE 会往平台塞一个谁都没打算创建的商品。"""
    exc = expect_raises(
        policy.PublishPolicyError,
        policy.resolve_operation,
        external_spu_id=None,
        requested=PublishOperation.UPDATE,
    )
    assert exc.code is ErrorCode.CHANNEL_FIELD_MISSING


# ---------------------------------------------------------------- 409


def test_idempotent_conflict_counts_as_accepted():
    """409 是好消息:上一次其实成功了。判成失败会变成死循环。"""
    out = policy.classify_response(
        status_code=409,
        body={"external_spu_id": "SIM-CREATE_OK-100-ab", "message": "该键已创建过商品"},
        operation=PublishOperation.CREATE,
    )
    assert out.succeeded
    assert out.attempt_status == PublishAttemptStatus.SUCCEEDED.value
    assert out.external_spu_id == "SIM-CREATE_OK-100-ab"
    assert out.retriable is False


def test_conflict_without_an_external_id_is_unknown_not_success():
    """平台说创建过却不说是哪个 —— 我们无从对账,不能记成已上架。"""
    out = policy.classify_response(status_code=409, body={"message": "冲突"})
    assert out.attempt_status == PublishAttemptStatus.UNKNOWN.value
    assert out.listing_status == PublishStatus.SUBMIT_RESULT_UNKNOWN.value
    assert out.needs_human is True


# ---------------------------------------------------------------- 429


def test_rate_limiting_does_not_touch_the_local_status():
    """这次没送到,商品还在排队。标成 SUBMIT_FAILED 会让运营以为要人工处理。"""
    out = policy.classify_response(
        status_code=429, headers={"Retry-After": "3"}, body={"error": "rate_limited"}
    )
    assert out.listing_status is None
    assert out.retriable is True
    assert out.retry_after_seconds == 3
    assert out.error_code == ErrorCode.CHANNEL_RATE_LIMITED.value


def test_an_unparseable_retry_after_falls_back_to_our_own_curve():
    """真实平台的 Retry-After 也可能是 HTTP-date。限流响应本身没有错。"""
    out = policy.classify_response(status_code=429, headers={"retry-after": "Wed, 21 Oct 2026"})
    assert out.retry_after_seconds is None
    assert out.retriable is True


# ---------------------------------------------------------------- 其余状态码


def test_auth_failure_is_not_retriable():
    """凭证不改,重发一万次还是同一个结果,而每次都在给风控加分。"""
    out = policy.classify_response(status_code=401, body={"message": "签名校验未通过"})
    assert out.attempt_status == PublishAttemptStatus.FAILED.value
    assert out.listing_status == PublishStatus.SUBMIT_FAILED.value
    assert out.retriable is False


def test_field_rejection_goes_to_the_fix_flow_not_the_retry_flow():
    out = policy.classify_response(status_code=422, body={"message": "字段校验未通过"})
    assert out.listing_status == PublishStatus.VALIDATION_FAILED.value
    assert out.retriable is False


def test_server_errors_are_worth_resending():
    out = policy.classify_response(status_code=503)
    assert out.retriable is True
    assert out.listing_status is None


def test_an_unrecognised_4xx_is_a_refusal_not_a_retry():
    out = policy.classify_response(status_code=418)
    assert out.retriable is False
    assert out.error_code == ErrorCode.PUBLISH_REJECTED.value


# ---------------------------------------------------------------- 2xx


def test_a_created_listing_without_an_external_id_is_unknown():
    """没有外部 ID 就没有凭据证明这次到达过平台,后续轮询也没有目标。"""
    out = policy.classify_response(status_code=201, body={"platform_status": "LISTED"})
    assert out.attempt_status == PublishAttemptStatus.UNKNOWN.value
    assert out.needs_human is True


def test_async_review_lands_on_reviewing_not_on_listed():
    """「提交成功」不等于「上架成功」,把两者混同是这类集成最常见的错。"""
    out = policy.classify_response(
        status_code=202,
        body={"external_spu_id": "SIM-ASYNC_REVIEW-1-ab", "platform_status": "REVIEWING"},
    )
    assert out.succeeded
    assert out.listing_status == PublishStatus.PLATFORM_REVIEWING.value


def test_sku_ids_come_back_in_the_shape_the_column_expects():
    out = policy.classify_response(
        status_code=201,
        body={
            "external_spu_id": "SIM-CREATE_OK-1-ab",
            "external_sku_ids": {"SW-1-BLK-S": "SIM-CREATE_OK-1-ab-aaa"},
            "platform_status": "LISTED",
        },
    )
    assert out.external_sku_ids == {"SW-1-BLK-S": "SIM-CREATE_OK-1-ab-aaa"}


def test_an_unknown_platform_status_is_kept_verbatim_not_guessed():
    """本地状态是我们的解读,platform_status 是平台的原话。两者都要留。"""
    out = policy.classify_response(
        status_code=200,
        body={"external_spu_id": "SIM-CREATE_OK-1-ab", "platform_status": "PENDING_QC"},
    )
    assert out.listing_status == PublishStatus.SUBMITTED.value
    assert out.platform_status == "PENDING_QC"


def test_a_successful_delist_is_not_read_back_off_the_platform_status():
    """下架成功时平台通常仍返回商品记录,照着翻译会把它记成「在架」。"""
    out = policy.classify_response(
        status_code=200,
        body={"external_spu_id": "SIM-CREATE_OK-1-ab", "platform_status": "LISTED"},
        operation=PublishOperation.DELIST,
    )
    assert out.listing_status == PublishStatus.DELISTED.value


# ---------------------------------------------------------------- 超时


def test_a_timed_out_create_is_never_resent_automatically():
    """平台可能已经建好了,再发一次就是店铺里多一个没人认领的商品。"""
    out = policy.classify_transport_failure(operation=PublishOperation.CREATE)
    assert out.attempt_status == PublishAttemptStatus.UNKNOWN.value
    assert out.retriable is False
    assert out.needs_human is True
    assert out.error_code == ErrorCode.PUBLISH_RESULT_UNKNOWN.value


def test_a_timed_out_update_may_be_resent_because_it_cannot_duplicate():
    """目标由 external_spu_id 确定,重发是同一个商品改两次。"""
    for op in (PublishOperation.UPDATE, PublishOperation.REPUBLISH, PublishOperation.DELIST):
        out = policy.classify_transport_failure(operation=op)
        assert out.attempt_status == PublishAttemptStatus.UNKNOWN.value
        assert out.retriable is True, op
        assert out.needs_human is False, op


# ---------------------------------------------------------------- 退避与放弃


def test_the_platform_knows_its_own_window_better_than_our_curve():
    assert policy.backoff_seconds(1, retry_after=45) == 45
    assert policy.backoff_seconds(9, retry_after=None) == policy.MAX_BACKOFF_SECONDS


def test_backoff_is_capped_so_it_never_becomes_a_silent_give_up():
    assert policy.backoff_seconds(0) == 0
    assert policy.backoff_seconds(3) == 8
    assert policy.backoff_seconds(99) == policy.MAX_BACKOFF_SECONDS
    assert policy.backoff_seconds(1, retry_after=99999) == policy.MAX_BACKOFF_SECONDS


def test_retries_run_out_so_a_misconfiguration_stops_costing_money():
    assert policy.exhausted(policy.MAX_DELIVERY_ATTEMPTS - 1) is False
    assert policy.exhausted(policy.MAX_DELIVERY_ATTEMPTS) is True


def test_an_in_flight_verdict_must_settle_once_retries_run_out():
    """一直 429 的提交每轮都是「还没有结论」,用尽之后必须落成有结论的状态。"""
    settled = policy.settle_exhausted(policy.classify_response(status_code=429))
    assert settled.attempt_status == PublishAttemptStatus.FAILED.value
    assert settled.listing_status == PublishStatus.SUBMIT_FAILED.value
    assert settled.retriable is False


def test_giving_up_does_not_rewrite_an_unknown_into_a_failure():
    """那次调用确实不知道结果。重试次数用完不能倒回去改写历史。"""
    unknown = policy.classify_transport_failure(operation=PublishOperation.UPDATE)
    settled = policy.settle_exhausted(unknown)
    assert settled.attempt_status == PublishAttemptStatus.UNKNOWN.value
    assert settled.retriable is False


# ---------------------------------------------------------------- 渠道注册表


def test_the_simulator_flag_comes_from_the_registry_not_from_a_constant():
    """`describe_extractors()` 曾经硬编码 configured: true —— 同一类错误。

    自 SHEIN 进注册表起,"已注册"不再蕴含"有发送端":它有 builder(抛
    `ChannelNotReady`)而没有 transport。所以这条守卫按三档判,不再假设
    `transport_for()` 一定取得到 —— 假设它取得到的写法会在第一个只有 builder
    的渠道上变成 KeyError,而那是守卫自己崩掉,不是它发现了问题。
    """
    rows = {row["channel"]: row for row in registry.describe_channels()}
    assert rows, "注册表是空的,前端状态条将无内容可显示"
    for row in rows.values():
        kind = registry.transport_kind(row["channel"])
        assert row["transport_kind"] == kind
        if kind == registry.TRANSPORT_NONE:
            assert row["transport"] is None
            assert row["is_simulator"] is False
            continue
        transport = registry.transport_for(row["channel"])
        assert row["is_simulator"] == transport.is_simulator
        assert row["transport"] == transport.name


def test_an_unknown_channel_is_refused_rather_than_defaulted():
    """拼错的渠道名会让商品被发到另一个店铺,而那在平台上不可撤销。"""
    expect_raises(registry.UnknownChannelError, registry.transport_for, "SHEIN_BR")


# ---------------------------------------------------------------- 事务边界


def _tree() -> ast.AST:
    return ast.parse(SERVICE.read_text(encoding="utf-8"))


def _fn(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _call_names(node: ast.AST) -> list[str]:
    """归一成可比较的名字,如 'session.commit' / '_invoke'。"""
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


def test_enqueue_never_commits_the_business_transaction():
    """一旦这里提交,意图和业务数据就成了两个事务,Outbox 白做。"""
    names = _call_names(_fn("enqueue"))
    assert "session.commit" not in names
    assert "session.flush" in names


def test_the_external_call_happens_after_a_commit_not_inside_a_transaction():
    """7.8:外部调用前 commit checkpoint,一个事务不许跨越一次外部调用。"""
    body = _fn("run_due").body
    committed_at = None
    invoked_at = None
    for i, stmt in enumerate(body):
        names = _call_names(stmt)
        if committed_at is None and "session.commit" in names:
            committed_at = i
        if invoked_at is None and "_invoke" in names:
            invoked_at = i
    assert committed_at is not None, "run_due 里没有 checkpoint 提交"
    assert invoked_at is not None, "run_due 里没有外部调用"
    assert committed_at < invoked_at, "外部调用发生在 checkpoint 之前"


def test_the_call_itself_owns_no_transaction():
    """`_invoke` 里出现 commit/rollback,就说明有个事务跨越了这次调用。"""
    names = _call_names(_fn("_invoke"))
    for forbidden in ("session.commit", "session.rollback", "session.flush"):
        assert forbidden not in names, f"_invoke 不该碰事务,但它调用了 {forbidden}"


def test_results_are_saved_in_a_fresh_session():
    """结果必须落在新事务里 —— 领取那个事务早就提交了。"""
    names = _call_names(_fn("_save"))
    assert "session_factory" in names
    assert "session.commit" in names


def test_reconcile_is_a_separate_function_not_a_flag_on_the_retry_path():
    """做成 confirm=True 的参数,两种意图在代码里就长得一模一样。"""
    fn = _fn("reconcile_unknown")
    arg_names = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "confirm" not in arg_names
    # 确认路径不许算新键:算新键才是「重新创建」
    assert "build_publish_idempotency_key" not in _call_names(fn)


def test_a_dry_run_never_creates_an_outbox_row():
    """干跑的定义是「构造了报文但不发出去」。它进了 outbox 就一定会被发出去。"""
    fn = _fn("enqueue")
    dry_branches = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.If)
        and isinstance(n.test, ast.Name)
        and n.test.id == "dry_run"
    ]
    assert dry_branches, "enqueue 里找不到 dry_run 分支"
    for branch in dry_branches:
        for stmt in branch.body:
            assert "PublishOutbox" not in _call_names(stmt)
        assert any(isinstance(s, ast.Return) for s in branch.body), "干跑分支必须就此返回"
