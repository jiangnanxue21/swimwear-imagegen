"""a62 守卫:BE-311 读接口、清理拍子接线、FE-312/313/315。

## 这一批的主题是「把三笔刻意留着的账一起还了」

a57 写清理判定时刻意不接线,理由是「没有读接口,删对了没有只能直接查库看」;
a58 把男装解锁时留下 FE-306 的统计列;a60 写完 S4 时 `purgeable()` 仍然没有调用点。
BE-311 一落地,这三条的前提同时消失 —— **所以它们必须同批还清,而不是各欠各的**:
一笔"前提已经满足"的欠账,和一笔"还等着前提"的欠账,在台账上长得一样,
而后者会一直被当成前者跳过去。

## 参数判定为什么值得穷举

`build_query` 是一层看起来很薄的东西,而它每一条都对应一次真实事故的形状:
认不出的取值静默变成"不筛"(§3.88 ②)、时间解析失败让整个端点 500(§3.88 ①)、
页大小没有上限。这三件在集成测试里最难覆盖全 —— 要造一堆参数组合,而每一种
都只差一个字母。
"""
from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.workflows.ai_test_query import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    FilterRejected,
    build_query,
    parse_ts,
    serialize_run,
)

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
FRONTEND = BACKEND.parent / "frontend" / "src"


def _app(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


def _strip_comments(source: str) -> str:
    """去掉 `/* */` 与 `//` 注释,只留会被渲染的部分。

    粗糙但够用,而且误伤方向是**多剥** —— 也就是更容易漏报而不是更容易假红。
    """
    import re as _re

    source = _re.sub(r"/\*.*?\*/", "", source, flags=_re.S)
    return _re.sub(r"//[^\n]*", "", source)


def _fe(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ============================================================ 参数判定


def test_an_unknown_kind_is_rejected_not_silently_ignored():
    """`kind=evaluatoin` 拼错一个字母时,返回全量比返回空更危险。

    运营会把那一屏读成「这段时间只有这些」—— §3.88 ② 修过同一个形状:
    `level=WARN` 打错被兜底成 INFO,筛选静默失效、拉回整窗。
    """
    try:
        build_query(kind="evaluatoin")
    except FilterRejected:
        # 吞的是**期望中的那一个**:下面的 `else` 在它没抛时失败。
        # `tests/pure/` 不许 import pytest,所以没有 `pytest.raises`。
        pass
    else:
        raise AssertionError("认不出的 kind 被静默放行了")
    assert build_query(kind="evaluation").kind == "evaluation"
    assert build_query(kind="copy").kind == "copy"
    # 不传 = 不筛,这一支是合法的
    assert build_query().kind is None
    assert build_query(kind="   ").kind is None


def test_the_page_size_is_capped_by_the_backend_not_the_caller():
    """这张表的 `payload_out` 对文案是全文,一条可能上千字。"""
    assert build_query(limit=100000).limit == MAX_PAGE_SIZE
    assert build_query(limit=5).limit == 5
    assert build_query().limit == DEFAULT_PAGE_SIZE


def test_an_oversized_limit_is_capped_rather_than_refused():
    """传 100000 是想"一次拿全"。

    拒绝它只会让调用方改成循环拉一万次;收到上限并**在出参里如实说明**,
    才是那个意图的正确答案。
    """
    assert build_query(limit=100000).limit == MAX_PAGE_SIZE
    source = _app("api/ai_tests.py")
    assert '"max_limit"' in source, "出参没有告知上限 —— 调用方会以为自己拿到了全部"
    assert '"limit": query.limit' in source, "出参没有报生效的每页条数"


def test_a_zero_or_negative_page_is_rejected():
    for bad in (0, -1):
        try:
            build_query(limit=bad)
        except FilterRejected:
            continue
        raise AssertionError(f"limit={bad} 被放行了")
    try:
        build_query(offset=-1)
    except FilterRejected:
        return
    raise AssertionError("负偏移量被放行了")


def test_a_broken_timestamp_is_a_400_not_a_500():
    """`ops_logs` 的 `parse_ts` 有过一次 500(§3.88 ①):

    无时区输入返回 naive,与 aware 比较时抛 TypeError,整个端点挂掉。
    **那条"容一手第三方写法"的路径恰恰是崩溃源。**
    """
    try:
        parse_ts("昨天")
    except FilterRejected:
        # 同上:这一段断言的是"它必须拒绝",`else` 负责在它放行时失败。
        pass
    else:
        raise AssertionError("认不出的时间被接受了")
    # 恒返 aware —— 这是那次 500 的根因
    assert parse_ts("2026-08-19T09:30:00").tzinfo is not None
    assert parse_ts("2026-08-19T09:30:00Z").tzinfo is not None


def test_a_naive_timestamp_is_read_as_utc_not_local():
    """这里与日志那边刻意不同,理由不同。

    日志的写入端 `formatTime` 用的是本机钟,所以那边按本机时区解释;
    这里是 API 入参,调用方可能在任何时区,而 `created_at` 落库就是 timestamptz。
    """
    assert parse_ts("2026-08-19T09:30:00").utcoffset() == timedelta(0)


def test_a_backwards_window_is_rejected():
    """起点晚于终点的窗口永远是空的 —— 而"空"和"这段时间没发生"长得一样。"""
    try:
        build_query(since="2026-08-19T10:00:00Z", until="2026-08-19T09:00:00Z")
    except FilterRejected:
        return
    raise AssertionError("倒着的时间窗被放行了")


def test_the_endpoint_turns_a_rejected_filter_into_400():
    source = _app("api/ai_tests.py")
    assert "except ai_test_query.FilterRejected" in source
    assert "status_code=400" in source


# ============================================================ 出参


class _Row:
    id = "r-1"
    kind = "copy"
    subject_type = "product"
    subject_id = "p-1"
    prompt_key = "copy_llm_system_prompt"
    prompt_version = "template-1"
    prompt_template_version = None
    generator = "template"
    model_name = None
    success = 1
    error_code = None
    error_message = None
    duration_ms = 12
    total_tokens = None
    billable = 0
    actor = "alice"
    created_at = datetime(2026, 8, 19, tzinfo=UTC)
    payload_in = {"fact_count": 3}
    payload_out = {"copy": {"title": "x"}}


def test_the_serialized_row_covers_every_column_the_model_declares():
    """漏一个键,那一列在界面上永远不存在,而没有任何东西会红。"""
    import re

    declared = set(
        re.findall(r"^    (\w+): Mapped\[", _app("models/ai_test_run.py"), re.M)
    )
    out = set(serialize_run(_Row(), iso=lambda v: str(v)))
    missing = declared - out - {"updated_at"}
    assert not missing, f"出参少了这些列:{sorted(missing)}"


def test_booleans_come_out_as_booleans():
    """SQLAlchemy 在某些后端上把 Boolean 读成 0/1。

    前端 `success: boolean` 收到 0 时 `!row.success` 仍然成立,但
    `row.success === true` 不成立 —— 而两种写法在这一页上都出现过。
    """
    out = serialize_run(_Row(), iso=lambda v: str(v))
    assert out["success"] is True
    assert out["billable"] is False


def test_the_timestamp_goes_through_the_caller_supplied_formatter():
    """裸 `.isoformat()` 会让同一列序列化出两种形状(`expire_on_commit=False`)。"""
    source = _app("api/ai_tests.py")
    assert "iso=iso_utc" in source, "出参的时间戳没走统一的格式化"
    # 按 AST 判调用,不判源码子串 —— 上面那段 docstring 里正当地含着
    # `.isoformat()`(它讲的就是为什么不这么写)。**这已经是第四次**被自己的
    # 墓志铭判红(§3.89 / a58 / a60 各一次),而前三次都在 .tsx 上、只能剥注释;
    # 这一次目标是 .py,AST 是现成的
    tree = ast.parse(_app("workflows/ai_test_query.py"))
    naked = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "isoformat"
    ]
    assert not naked, "判定层里有裸 .isoformat() —— 同一列会序列化出两种形状"


# ============================================================ 清理接线


def test_the_purge_judgment_finally_has_a_caller():
    """a57 刻意不接线,理由是「没有读接口,删对了没有只能直接查库看」。

    BE-311 一落地那个前提就消失了 —— **一笔"前提已经满足"的欠账,和一笔
    "还等着前提"的欠账,在台账上长得一样**,而后者会一直被当成前者跳过去。
    """
    source = _app("tasks/maintenance_tasks.py")
    assert "purgeable(" in source, "清理判定仍然没有调用点"
    assert "purge_ai_test_runs" in source


def test_the_purge_reads_the_active_versions_fresh_every_pass():
    """缓存一份"当时的生效版本"是这条不变量最容易被绕开的方式。

    页面上刚切了版本,这一拍就按旧集合把新生效那版的证据删了。
    """
    tree = ast.parse(_app("tasks/maintenance_tasks.py"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "purge_ai_test_runs"
    )
    body = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))
    assert "PromptTemplate.is_active" in body, "生效版本不是现查的"
    assert "active_versions=active" in body


def test_the_scheduled_pass_is_a_dry_run():
    """自动删一张带完整模型输出的档案表,风险高于它省下的磁盘。

    与 `cleanup_service.purge_superseded_exports` 同一条规矩(4.1 节 H):
    删除不可逆,默认只报告,真删要人显式触发。
    """
    tree = ast.parse(_app("tasks/maintenance_tasks.py"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "purge_ai_test_runs"
    )
    defaults = dict(
        zip(
            [a.arg for a in fn.args.args][-len(fn.args.defaults) :],
            fn.args.defaults,
            strict=True,
        )
    )
    node = defaults.get("apply")
    assert isinstance(node, ast.Constant) and node.value is False, (
        "清理任务默认就删 —— beat 上排的那一档必须是干跑"
    )
    beat = _app("tasks/celery_app.py")
    assert "maintenance.purge_ai_test_runs" in beat, "beat 上没有这一拍"


def test_the_purge_events_are_registered():
    from app.core.log_events import EVENTS, domain_of_event

    for event in ("ops.ai_test_purge_pass", "ops.ai_test_purge_failed"):
        assert event in EVENTS
        # 归 ops 而不是 eval:运营查它的场景是「这张表为什么这么大」
        assert domain_of_event(event) == "ops"


def test_the_failure_branch_keeps_the_same_shape():
    """异常分支手抄一份返回字典,成功路径加一个键时它不会跟着变 ——

    而读它做看板的一侧恰恰在出错时拿到另一种形状(a54 修过 `relay_dispatches`)。
    """
    tree = ast.parse(_app("tasks/maintenance_tasks.py"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "purge_ai_test_runs"
    )
    shapes = [
        {k.value for k in node.keys if isinstance(k, ast.Constant)}
        for node in ast.walk(fn)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
        for node in [node.value]
    ]
    assert len(shapes) >= 2, "找不到两条返回路径"
    base = shapes[0]
    for other in shapes[1:]:
        assert base <= other, f"异常分支少了这些键:{sorted(base - other)}"


# ============================================================ 前端消费


def test_the_read_endpoint_has_a_consumer():
    """**后端通、前端不可达**是这份 PRD 从 §14.1 起就在防的中间态。

    一个没有页面消费的 `GET /ai-tests/runs`,和男装那条死路是同一个形状。
    """
    assert (FRONTEND / "api" / "aiTests.ts").is_file(), "前端没有这条 api"
    page = _fe("pages/AITestPage.tsx")
    assert "aiTestsApi.list" in page, "AI 测试页没有消费它"
    assert "历史记录" in page


def test_the_page_no_longer_tells_people_not_to_refresh():
    """页面上那句「请保持页面打开,不要刷新」本身就是问题的自述。

    结果落库并可查之后它就该消失 —— 留着的话,一句已经不成立的提示会让运营
    继续守着页面等,而那正是留档要解决的事。
    """
    # 先剥注释。**这是本轮第三次、本会话第五次**被自己的注释判红:
    # 上面两处注释正当地引着那句旧文案(它们讲的就是"这句被什么替代了")。
    #
    # 值得单独记一笔:**否定式的源码子串断言比肯定式更容易踩这个坑** ——
    # 肯定式要求那串字存在,注释里多一处不影响结论;否定式要求它不存在,
    # 而一句解释"为什么删掉它"的注释必然含着它。a59 那条门禁判的是
    # `assert "x" in ...` 的窗口唯一性,对 `not in` 这一支帮不上忙。
    page = _strip_comments(_fe("pages/AITestPage.tsx"))
    assert "不要刷新" not in page, "那句提示还在,而它已经不成立了"
    assert "可以离开本页" in page, "没有换成结果落库之后该说的话"


def test_the_cost_confirmation_resets_after_every_run():
    """FE-315。这个勾的全部意义是「这一次可能真的花钱」。

    勾上之后一直勾着,等于第二次点击不再需要任何确认,而它同样会计费。
    **用 `onSettled` 而不是 `onSuccess`** —— 失败的调用一样计费。
    """
    page = _fe("pages/AITestPage.tsx")
    for setter in ("setScoreCostConfirmed(false)", "setCopyCostConfirmed(false)"):
        at = page.index(setter)
        window = page[max(0, at - 400) : at]
        assert "onSettled" in window, f"{setter} 不在 onSettled 里 —— 失败那次不会重置"


def test_the_result_tags_finally_show_the_prompt_version():
    """FE-313。此前那一行渲染四项,**唯独缺提示词版本** ——

    而"这一版跑出什么"正是这个页面存在的理由。
    """
    page = _fe("pages/AITestPage.tsx")
    assert "attempt.prompt_version" in page


def test_the_history_table_distinguishes_a_failed_run_from_no_run():
    """「跑了但没成」和「没跑过」在这张表里必须分得开。

    只记成功的话,一串失败会被读成一段没人用过的空白。
    """
    page = _fe("pages/AITestPage.tsx")
    assert "未成功" in page
    assert "还没有跑过测试" in page, "空表没有自己的说法"


def test_the_frontend_type_matches_the_endpoint_shape():
    import re

    source = _app("api/ai_tests.py")
    block = source[source.index("return {") :]
    keys = set(re.findall(r'"(\w+)":', block))
    types = _fe("api/types.ts")
    iface = types[types.index("export interface AiTestRunList {") :]
    iface = iface[: iface.index("}")]
    declared = set(re.findall(r"^  (\w+)\??:", iface, re.M))
    missing = keys - declared
    assert not missing, f"AiTestRunList 少了后端会返回的字段:{sorted(missing)}"


# ============================================================ FE-314 互链(a63)


def test_the_serial_filter_rejects_non_positive_values():
    """序号从 1 起。0 或负数查出来必然是空 ——

    而"空"和"这一版没跑过测试"在界面上长得一样。前端 `runsQueryForVersion`
    用同一条判据不画入口,两侧必须同向。
    """
    for bad in (0, -1, "abc", "3.5"):
        try:
            build_query(prompt_template_version=bad)
        except FilterRejected:
            continue
        raise AssertionError(f"prompt_template_version={bad!r} 被放行了")
    assert build_query(prompt_template_version="3").prompt_template_version == 3
    assert build_query().prompt_template_version is None


def test_the_endpoint_filters_on_the_serial_column_not_the_hash_column():
    """按 `prompt_version` 查评分那两份必然对不上 —— 它们落的是序号。"""
    source = _app("api/ai_tests.py")
    assert "AiTestRun.prompt_template_version == query.prompt_template_version" in source


def test_the_prompt_page_links_out_with_the_serial():
    page = _fe("pages/PromptsPage.tsx")
    assert "prompt_template_version=${row.version}" in page, "互链没有按序号拼参数"
    assert "runsQueryForVersion(promptKey, row.version)" in page, (
        "没有先问判定层要不要画这个入口 —— 内置默认那一支会画出一个必然空的链接"
    )


def test_the_ai_test_page_offers_a_way_back_to_everything():
    """筛着的时候必须给得出"看全部"的路。

    否则从提示词页跳过来的人会以为这一页只有这几条,而地址栏那两个参数
    不是每个人都会看。
    """
    page = _fe("pages/AITestPage.tsx")
    assert "看全部" in page
    assert "setSearchParams({})" in page


def test_the_filter_params_are_part_of_the_query_key():
    """不进 queryKey 的话,换一版之后 react-query 会拿缓存 ——

    界面显示的是上一版的记录,而地址栏说的是这一版。
    """
    page = _fe("pages/AITestPage.tsx")
    at = page.index("queryKey: ['ai-test-runs'")
    window = page[at : at + 120]
    assert "focusKey" in window and "focusVersion" in window
