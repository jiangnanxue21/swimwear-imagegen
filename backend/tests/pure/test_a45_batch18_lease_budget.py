"""批次租约的预算算得对不对,以及续租是不是**别人看得见**(A45-batch18 / P2-1)。

## 这一组守的是哪两件事

**一、预算的三个数取自识别链路自己的配置。** 原来 `LONGEST_LEGAL_ITEM_SECONDS`
写的是 `90 * 3 * 4`,三项全部来自 **VISION_MODEL_\\*(评分器)**,而批次里跑的
EXTRACT 读的是 **EXTRACTOR_MODEL_\\***。两组键从一开始就是独立的
(`.env.example` 里那句"即便填同一个端点也要各填一遍"),于是那条
"租约必须长于单件最长合法耗时"的不变量一直在拿另一条链路的参数守本条链路:

    算的是   90 × 3 × 4 张   = 1080 秒,而租约 1800 —— 看起来富余
    实际是   60 × 3 × 12 张  = 2160 秒,而租约 1800 —— **不变量不成立**

也就是说那条模块级 `if` 因为被除数取错,一次都没有红过。而它守的东西是钱:
回收一件仍在跑的条目 = 同一件事第二次调用付费模型。

**二、续租必须在付费调用之前提交。** 原来 `renew_lease()` 那条 UPDATE 留在
外层事务里,而外层事务要等 `_execute()` 跑完、结果保存完才提交。整个付费
调用期间,`reap_expired_leases` 在自己的会话里读到的仍是**领取时**那个
`lease_until` —— 续租等于没续。原来的门禁只核验源码顺序(renew 在 _execute
之前),而顺序对、可见性不对,正是这次要修的。

真库那一半(另一个会话在调用期间真的读得到新截止时间)在
`tests/test_a45_batch18_lease_visibility_db.py`。两边缺一不可。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.workbench import batch as b

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SERVICE = BACKEND_ROOT / "app" / "workbench" / "batch_service.py"
CONFIG = BACKEND_ROOT / "app" / "core" / "config.py"


# ================================================================ 预算


def test_the_budget_is_derived_from_the_extractor_config_not_the_evaluator_config():
    """**这一条是 P2-1 上半段的全部内容。**

    三个数必须是 `EXTRACTOR_MODEL_TIMEOUT_SECONDS` /
    `EXTRACTOR_MODEL_MAX_RETRIES` / `EXTRACTOR_MAX_IMAGES_PER_RUN` 的默认值。
    取评分器那一组的话,算出来的数比真实上限小一半,而不变量会安静地通过。
    """
    assert b.DEFAULT_EXTRACT_TIMEOUT_SECONDS == 60
    assert b.DEFAULT_EXTRACT_MAX_RETRIES == 2
    assert b.DEFAULT_EXTRACT_MAX_IMAGES == 12
    assert b.LONGEST_LEGAL_ITEM_SECONDS == 60 * 3 * 12 == 2160


def test_the_three_defaults_match_what_config_actually_declares():
    """常量和 `Settings` 的默认值分叉 = 这条不变量又开始守错东西。

    读源码而不是 import settings:`Settings` 会读 `.env`,而门禁必须在
    任何一台机器上给同一个答案。
    """
    src = CONFIG.read_text(encoding="utf-8")
    tree = ast.parse(src)
    declared: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if stmt.value is not None:
                        try:
                            declared[stmt.target.id] = ast.literal_eval(stmt.value)
                        except ValueError:
                            # 字面量读不出来的字段(表达式、函数调用)**跳过而不是失败**:
                            # 这里只关心下面点名的那几个常量,而它们都是字面量。
                            # 读不出来的进不了 `declared`,取用时会 KeyError,
                            # 所以没有"静默用了错的值"这条路。
                            pass
    assert declared["EXTRACTOR_MODEL_TIMEOUT_SECONDS"] == b.DEFAULT_EXTRACT_TIMEOUT_SECONDS
    assert declared["EXTRACTOR_MODEL_MAX_RETRIES"] == b.DEFAULT_EXTRACT_MAX_RETRIES
    assert declared["EXTRACTOR_MAX_IMAGES_PER_RUN"] == b.DEFAULT_EXTRACT_MAX_IMAGES


def test_the_round_trip_count_is_retries_plus_one():
    """`1 + max_retries`,不是 `max_retries`。

    传输层的循环条件是 `attempt <= max_retries`,所以 2 次重试是 3 个往返。
    这个 off-by-one 正是原来那版把 90 说成"一次视觉调用"却乘 3 的来源 ——
    数字碰巧对了,而理由是错的,于是下一个人改重试次数时不会想到来改这里。
    """
    assert b.longest_legal_item_seconds(
        timeout_seconds=10, max_retries=0, max_images=1
    ) == 10
    assert b.longest_legal_item_seconds(
        timeout_seconds=10, max_retries=2, max_images=1
    ) == 30


def test_the_lease_actually_covers_the_corrected_budget():
    """不变量在**修正后的**数字下仍然成立。

    这条和 `test_batch_lease.py` 里那条是同一个断言,分开写是因为它们
    守的是不同的东西:那边守"这条 assert 还在",这边守"这次把被除数
    改对之后,租约也跟着抬上去了"。只改被除数不改租约的话,模块会
    直接 `raise RuntimeError` —— 那也是对的失败方式,但它会挡住启动,
    所以这条断言必须在 import 期之外再确认一次。
    """
    assert b.ITEM_LEASE_SECONDS > b.CLAIM_CHUNK * b.LONGEST_LEGAL_ITEM_SECONDS
    assert b.BATCH_PROGRESS_STALL_SECONDS > b.LONGEST_LEGAL_ITEM_SECONDS


def test_a_generous_deployment_config_is_caught_rather_than_silently_broken():
    """运维把单张超时调到 120、图片上限调到 20 时,预算翻了三倍多。

    模块级那条 `if` 用的是默认值,守不住这种情况。所以要有一个能拿
    **实际配置**当场算的函数 —— 它的返回值被
    `batch_service.lease_budget_shortfall()` 拿去和租约比。
    """
    generous = b.longest_legal_item_seconds(
        timeout_seconds=120, max_retries=3, max_images=20
    )
    assert generous == 120 * 4 * 20
    assert generous > b.ITEM_LEASE_SECONDS, (
        "这正是那个函数要报出来的情况;它不再成立时,说明租约又抬过了,"
        "把这条用例的数字改大而不是删掉它"
    )


# ================================================================ 续租可见性


def _run_batch_source() -> str:
    src = SERVICE.read_text(encoding="utf-8")
    start = src.index("def run_batch(")
    return src[start : src.index("\ndef settle(", start)]


def _run_batch_fn() -> ast.FunctionDef:
    src = SERVICE.read_text(encoding="utf-8")
    return next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "run_batch"
    )


def _call_lines(fn: ast.FunctionDef, name: str, *, qualified: bool = False) -> list[int]:
    """函数体里那个调用**真正发生**的行号,按出现顺序。

    `qualified=True` 时比整个点号路径而不是最后一段 —— 分得开
    `session.commit()` 与 `savepoint.commit()`。这一处必须分得开:
    保存点提交对别的会话是不可见的,拿它顶替会话提交,续租照样看不见。
    """
    return sorted(
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and (ast.unparse(n.func) if qualified else ast.unparse(n.func).split(".")[-1])
        == name
    )


def test_the_renewal_is_committed_before_the_paid_call():
    """**这一条是 P2-1 下半段的全部内容。**

    顺序对(renew 在 _execute 之前)但没提交,等于没续:回收器在它自己的
    会话里读到的仍是领取时那个 `lease_until`。

    ## 顺序按 AST 比,不按字符串位置比

    第一版用 `body.index("_execute(")`,而 `run_batch` 里在真正那次调用
    **之前**有一句注释写着「外层事务要等 `_execute()`」—— 那句话解释的正是
    这次修复本身。于是 `exec_at` 指到注释上,顺序断言在**正确实现**上恒假。

    这条守卫因此和 batch18 那条 `billable_units=1` 是同一型:守卫查的是
    "这几个字出现在第几个字节",而要问的是"这几件事按什么顺序执行"。
    调用的行号本来就是 AST 上的精确信息,没必要退回文本匹配。
    """
    fn = _run_batch_fn()
    renews = _call_lines(fn, "renew_lease")
    executes = _call_lines(fn, "_execute")
    commits = _call_lines(fn, "session.commit", qualified=True)

    assert renews, "run_batch 里找不到续租调用 —— 这条守卫失去了对象"
    assert executes, "run_batch 里找不到 _execute 调用 —— 这条守卫失去了对象"

    renew_at, exec_at = renews[0], executes[0]
    assert renew_at < exec_at, "续租排在付费调用之后,等于没续"
    between = [line for line in commits if renew_at < line < exec_at]
    assert between, (
        "续租之后、付费调用之前必须提交,否则别的会话看不见新的截止时间。"
        f"续租在第 {renew_at} 行、_execute 在第 {exec_at} 行,中间没有 commit"
    )


def test_the_renewal_commit_is_skipped_for_the_single_session_fixture():
    """`commit_each=False` 那条路径是测试夹具,只有一个 session。

    在那里提交会把夹具的事务边界打乱(它靠最后回滚清场),而那条路径上
    本来就不存在"别的 worker 看不看得见"这个问题。
    """
    body = _run_batch_source()
    renew_at = body.index("renew_lease(")
    window = body[renew_at : body.index("_execute(")]
    assert "if commit_each:" in window, "这次提交必须受 commit_each 保护"


def test_the_startup_check_reads_the_deployed_config_not_the_constants():
    """`lease_budget_shortfall()` 必须问配置,不能重复模块里那三个常量。

    重复的话它和模块级 `if` 守的是同一件事,而运维在设置页改的那一层
    仍然没有任何东西看着。
    """
    src = SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "lease_budget_shortfall"
    )
    body = ast.get_source_segment(src, fn) or ""
    assert "EXTRACTOR_MODEL_TIMEOUT_SECONDS" in body
    assert "EXTRACTOR_MAX_IMAGES_PER_RUN" in body
    assert "longest_legal_item_seconds" in body
