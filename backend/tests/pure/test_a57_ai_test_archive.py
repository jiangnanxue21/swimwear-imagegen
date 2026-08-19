"""a57 守卫:AI 测试留档(BE-309/310)与迁移 0055 的目标表订正(BE-307)。

## 这一批守的是什么

三类,失效方式各不相同:

    成形    `payload_out` 评分放指针、文案放全文;两侧都过脱敏
    不变量  引用着当前生效版本的留档,多老都不删
    接线    三条评分出路 + 两条文案分支各留档一次;迁移指着一张真的存在的表

第三类里那条迁移检查是本批的来路。`0055_prompt_version_string.py` 原文
`_TABLE = "evaluations"`,而全仓没有这张表 —— 它在任何真实库上都会以
`relation "evaluations" does not exist` 失败。**一份从未被执行过的迁移,
它的表名没有任何东西在校验**:模型侧不知道迁移写了什么,门禁只查迁移链
head 唯一,autogenerate 不回头看手写的 alter。所以这里补一条**全量**检查,
不只钉 0055:任何 `op.alter_column` / `op.create_index` 的目标表,必须在
本仓的迁移里被 `create_table` 过。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.workflows.ai_test_record import (
    DEFAULT_RETENTION_DAYS,
    KIND_COPY,
    KIND_EVALUATION,
    AiTestRecord,
    purgeable,
    record_for_copy,
    record_for_evaluation,
)

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
MIGRATIONS = BACKEND / "migrations" / "versions"


# ============================================================ 成形


def test_an_evaluation_run_stores_a_pointer_not_a_second_copy_of_the_output():
    """评分正文在 `EvaluationAttempt`。这里再抄一份,两份就会分叉。"""
    record = record_for_evaluation(
        candidate_id="c-1",
        attempt_id="a-1",
        success=True,
        billable=True,
        actor="alice",
        prompt_version="7",
        evaluator="vision",
        model_name="gpt-4o",
    )
    assert record.kind == KIND_EVALUATION
    assert record.payload_out == {"attempt_id": "a-1"}
    # 正文的任何一角都不许在这里出现 —— 出现了就是开始抄第二份
    assert "scores" not in (record.payload_out or {})
    assert "raw" not in (record.payload_out or {})


def test_a_copy_run_stores_the_whole_output_because_nothing_else_does():
    """`diagnose_copy` 不建 ContentPlan / ListingCopy:不存这里就彻底没了。"""
    result = {
        "success": True,
        "billable": False,
        "generator": "template",
        "prompt_version": "template-1",
        "copy": {"title": "一件泳衣", "description": "描述"},
        "notes": ["note-1"],
        "violations": [{"code": "TITLE_TOO_LONG", "blocking": False}],
        "trace": {"duration_ms": 120, "total_tokens": 88},
    }
    record = record_for_copy(product_id="p-1", result=result, actor="bob")
    assert record.kind == KIND_COPY
    assert record.payload_out is not None
    assert record.payload_out["copy"] == {"title": "一件泳衣", "description": "描述"}
    assert record.payload_out["notes"] == ["note-1"]
    # 违规项跟着输出一起存:"这一版是不是老写超长标题"的答案在这里
    assert record.payload_out["violations"][0]["code"] == "TITLE_TOO_LONG"
    assert record.duration_ms == 120
    assert record.total_tokens == 88


def test_a_failed_copy_test_is_archived_with_its_error_not_dropped():
    """「跑了但没成」和「没跑过」在历史表里必须分得开。"""
    result = {
        "success": False,
        "billable": True,
        "generator": "llm",
        "prompt_version": "",
        "message": "模型超时",
        "error_code": "PROVIDER_TIMEOUT",
        "copy": None,
        "notes": [],
        "trace": {},
        "violations": [],
    }
    record = record_for_copy(product_id="p-1", result=result, actor="bob")
    assert record.success is False
    assert record.error_code == "PROVIDER_TIMEOUT"
    assert record.error_message == "模型超时"
    assert record.billable is True


def test_a_successful_run_carries_no_error_message():
    """成功时 `message` 是「文案测试完成」,把它落进 error_message 是在造噪声。"""
    record = record_for_copy(
        product_id="p-1",
        result={"success": True, "message": "文案测试完成", "trace": {}},
        actor="bob",
    )
    assert record.error_message is None


def test_both_payload_sides_go_through_the_same_redaction_as_the_sidecar():
    """新开一个去向,最容易漏的就是在新去向上把老规矩忘了。

    这张表将来会被 `GET /ai-tests/runs` 整表读出去,而文案输出里可能带着
    模型原样吐回来的东西。用一个 `safe_payload_for_log` 一定会处理的形状
    (裸 `Bearer` 串)来验,不看源码里写没写那个函数名。
    """
    result = {
        "success": True,
        "billable": False,
        "generator": "llm",
        "prompt_version": "llm-1",
        "copy": {"description": "Authorization: Bearer sk-live-0123456789abcdef"},
        "notes": [],
        "violations": [],
        "trace": {},
    }
    record = record_for_copy(product_id="p-1", result=result, actor="bob")
    assert "sk-live-0123456789abcdef" not in str(record.payload_out)


def test_success_flag_never_leaks_into_the_token_count():
    """`bool` 是 `int` 的子类。不排掉的话 `True` 会变成 token 数 1。"""
    record = record_for_evaluation(
        candidate_id="c-1",
        attempt_id="a-1",
        success=True,
        billable=False,
        actor="alice",
        total_tokens=True,
        duration_ms=False,
    )
    assert record.total_tokens is None
    assert record.duration_ms is None


def test_an_empty_actor_falls_back_to_system_rather_than_an_empty_string():
    record = record_for_evaluation(
        candidate_id="c-1",
        attempt_id="a-1",
        success=True,
        billable=False,
        actor="   ",
    )
    assert record.actor == "system"


def test_the_row_kwargs_cover_every_column_the_model_declares():
    """成形层漏一个键,那一列就永远是 NULL,而没有任何东西会红。

    读模型源码而不是 import 它 —— `models/ai_test_run.py` 要 sqlalchemy,
    而这一层的全部价值在于没有库也验得动。
    """
    source = (APP / "models" / "ai_test_run.py").read_text(encoding="utf-8")
    declared = set(re.findall(r"^    (\w+): Mapped\[", source, re.M))
    written = set(
        record_for_evaluation(
            candidate_id="c-1", attempt_id="a-1", success=True,
            billable=False, actor="alice",
        ).as_row_kwargs()
    )
    # id / created_at / updated_at 由 Base 与两个 Mixin 写,构造点不传是正常的
    missing = declared - written
    assert not missing, f"这些列没有写入路径:{sorted(missing)}"
    stray = written - declared
    assert not stray, f"成形层多给了模型没有的键(会被 SQLAlchemy 静默忽略):{sorted(stray)}"


def test_every_column_gets_a_real_value_from_some_builder():
    """**键在不代表值会被写。** 这一条是 a63 补的,补的是 a57 自己漏掉的那道缝。

    上面那条只验**键集**覆盖模型的列。而 `prompt_template_version` 从建表起
    就有键、值恒为 None —— 键在、值恒空,上面那条一直是绿的。

    另一道防线也没看见它:`ai_test_archive` 当时用 `AiTestRun(**kwargs)` 构造,
    于是 `audit_column_writers.py` 把整个模型划进「判不了」,而那条审计存在的
    全部理由就是「每一列都答得出谁写它」。**两侧各自的测试都绿,没有一条跨过中间。**

    所以这里判的是:两个构造器**合起来**,每一列至少有一条路径给出非 None 值。
    合起来而不是各自 —— 两类留档本就不该填满同一组列(评分落序号、文案落哈希),
    要求各自填满会逼出一堆假值。
    """
    evaluation = record_for_evaluation(
        candidate_id="c-1",
        attempt_id="a-1",
        success=True,
        billable=True,
        actor="alice",
        prompt_key="vision_system_prompt",
        template_version="3",
        evaluator="vision",
        model_name="gpt-4o",
        depth="FULL",
        reference_count=2,
        error_code="X",
        error_message="boom",
        duration_ms=12,
        total_tokens=34,
    )
    copy_run = record_for_copy(
        product_id="p-1",
        result={
            "success": False,
            "billable": True,
            "generator": "llm",
            "prompt_version": "gpt-4o:a1b2c3d4e5f6",
            "message": "超时",
            "error_code": "PROVIDER_TIMEOUT",
            "copy": {"title": "x"},
            "notes": [],
            "violations": [],
            "trace": {"duration_ms": 9, "total_tokens": 7, "model": "gpt-4o"},
        },
        actor="bob",
        prompt_key="copy_llm_system_prompt",
        fact_count=3,
    )
    filled: set[str] = set()
    for record in (evaluation, copy_run):
        filled |= {k for k, v in record.as_row_kwargs().items() if v is not None}

    source = (APP / "models" / "ai_test_run.py").read_text(encoding="utf-8")
    declared = set(re.findall(r"^    (\w+): Mapped\[", source, re.M))
    never = declared - filled
    assert not never, (
        f"这些列没有任何构造路径会给出非 None 值,恒等于默认值:{sorted(never)}"
    )


def test_the_two_version_columns_do_not_mix_shapes():
    """**评分落序号列,文案落哈希列 —— 两列的语义不许互串(a63 订正)。**

    a57 把评分的 `attempt.prompt_version`(其实是 `prompt_templates.version`
    自增序号)塞进了 `prompt_version`,而那一列的文档写着「内容哈希」。
    后果不是一行错,是两列的语义一起塌了:`prompt_version` 混装两种形状
    (评分 "3"、文案 "gpt-4o:a1b2c3"),`prompt_template_version` 恒为 NULL。
    **FE-314 的互链一直定不下口径,根因就在这里** —— 按哪一列查都对不上。
    """
    evaluation = record_for_evaluation(
        candidate_id="c-1", attempt_id="a-1", success=True, billable=False,
        actor="alice", template_version="7",
    )
    assert evaluation.prompt_template_version == 7
    assert evaluation.prompt_version is None, (
        "评分链路还没接内容哈希 —— 把序号塞进哈希列,BE-305 真接上那天"
        "没有任何东西能分辨哪些行是旧口径"
    )
    copy_run = record_for_copy(
        product_id="p-1",
        result={"success": True, "prompt_version": "gpt-4o:a1b2c3d4e5f6", "trace": {}},
        actor="bob",
    )
    assert copy_run.prompt_version == "gpt-4o:a1b2c3d4e5f6"
    assert copy_run.prompt_template_version is None


def test_an_unparsable_serial_becomes_none_not_zero():
    """0 会让「没有版本」和「第 0 版」长得一样。

    `prompt_service._assert_expected_version` 刚为同一个理由拒绝过哨兵值。
    """
    for bad in (None, "", "  ", "abc", "3.5"):
        record = record_for_evaluation(
            candidate_id="c", attempt_id="a", success=True, billable=False,
            actor="x", template_version=bad,
        )
        assert record.prompt_template_version is None, bad


def test_the_archive_writer_names_every_column_instead_of_splatting():
    """`AiTestRun(**kwargs)` 让列审计把整个模型划进「判不了」。

    而那条审计存在的全部理由就是「每一列都答得出谁写它」——
    一个 `**` 就能让一整张表退出它的射程,且不会有任何征兆。
    """
    tree = ast.parse((APP / "services" / "ai_test_archive.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "id", None) != "AiTestRun":
            continue
        assert not any(kw.arg is None for kw in node.keywords), (
            "又用 **kwargs 构造了 —— 列审计会看不见这张表的任何一列"
        )
        return
    raise AssertionError("找不到 AiTestRun 的构造点")


# ============================================================ 保留期不变量


@dataclass
class _Row:
    created_at: datetime
    prompt_version: str | None = None


def _now() -> datetime:
    return datetime(2026, 8, 19, tzinfo=UTC)


def test_a_run_referencing_a_live_prompt_version_is_never_purged():
    """本批的核心不变量(PRD §14.3 决议)。

    删掉当前生效那一版的唯一证据,等于在它最会被查的时候把它清掉 ——
    而"回看多久"这个产品问题真正担心的就是这件事。**多老都不删。**
    """
    ancient = _Row(_now() - timedelta(days=3650), prompt_version="live-1")
    assert purgeable([ancient], active_versions={"live-1"}, now=_now()) == ()


def test_an_old_run_whose_version_is_no_longer_active_is_purgeable():
    row = _Row(_now() - timedelta(days=DEFAULT_RETENTION_DAYS + 1), prompt_version="old-9")
    assert purgeable([row], active_versions={"live-1"}, now=_now()) == (row,)


def test_a_young_run_is_kept_even_when_its_version_is_dead():
    """两道闸的顺序:年龄先判。没过期的一律不碰。"""
    row = _Row(_now() - timedelta(days=DEFAULT_RETENTION_DAYS - 1), prompt_version="old-9")
    assert purgeable([row], active_versions=set(), now=_now()) == ()


def test_no_active_template_at_all_is_a_legal_input():
    """新部署从没在页面上保存过提示词。此时只剩年龄那道闸。"""
    row = _Row(_now() - timedelta(days=DEFAULT_RETENTION_DAYS + 1), prompt_version=None)
    assert purgeable([row], active_versions=set(), now=_now()) == (row,)


def test_the_retention_days_are_tunable_but_the_invariant_is_not():
    """产品把天数调到 0,引用还活着的记录仍然不删。"""
    row = _Row(_now() - timedelta(days=1), prompt_version="live-1")
    assert purgeable([row], active_versions={"live-1"}, now=_now(), retention_days=0) == ()


def test_a_negative_retention_is_rejected_rather_than_silently_inverted():
    """负数会把"未来的记录"也算成过期 —— 那是一条会删掉刚写的行的路径。"""
    try:
        purgeable([], active_versions=set(), now=_now(), retention_days=-1)
    except ValueError:
        return
    raise AssertionError("负的保留期被接受了")


def test_purge_returns_the_rows_themselves_not_a_count():
    """只给数字的话,"删掉的是哪几条"事后答不出来。"""
    row = _Row(_now() - timedelta(days=400), prompt_version="old-9")
    assert purgeable([row], active_versions=set(), now=_now())[0] is row


# ============================================================ 接线


def _tree(rel: str) -> ast.AST:
    return ast.parse((APP / rel).read_text(encoding="utf-8"))


def _calls_named(tree: ast.AST, name: str) -> int:
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        label = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if label == name:
            count += 1
    return count


def test_every_evaluation_exit_archives_exactly_once():
    """三条出路:解析失败 / Provider 出错 / 成功。前两条是 early return。

    在函数末尾合并写一次的话,那个合并点**根本到不了** —— 而"失败的测试
    没进历史"正是这张表最该记住的那一类。
    """
    tree = _tree("services/evaluation_service.py")
    assert _calls_named(tree, "_archive_diagnostic") == 3, (
        "评分诊断的三条出路必须各留档一次"
    )


def test_the_copy_test_archives_on_both_branches():
    tree = _tree("workbench/service.py")
    assert _calls_named(tree, "_archive_copy_test") == 2, (
        "文案测试的成功与失败两条分支必须各留档一次"
    )


def test_the_archive_writer_never_lets_its_own_failure_reach_the_caller():
    """留档在模型调用之后。抛出去 = 钱花了、结果也没了,而丢的本来只是台账。"""
    tree = ast.parse((APP / "services" / "ai_test_archive.py").read_text(encoding="utf-8"))
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "写入没有被保护起来"
    for handler in handlers:
        raises = [n for n in ast.walk(handler) if isinstance(n, ast.Raise)]
        assert not raises, "留档失败被重新抛出,调用方会收到 500"


def test_the_archive_failure_is_loud_even_though_it_is_swallowed():
    """吞掉不喊 = 一张有洞而看起来完好的档案,比没有档案更糟。

    **判的是"那个 except 分支里写了这个事件码",不是"这个文件里出现过它"。**
    第一版写的是 `"eval.ai_test_record_failed" in source` —— 而模块文档里
    也提到了这个码(讲的正是为什么要喊),于是把 `logger.error` 整段删掉之后,
    子串仍在、守卫照样绿。a59 的 `tools/audit_guard_windows.py` 把它揪了出来,
    而它是那条门禁上线当天抓到的第一条真洞。
    """
    tree = ast.parse((APP / "services" / "ai_test_archive.py").read_text(encoding="utf-8"))
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "写入没有被保护起来"
    loud = []
    for handler in handlers:
        body = ast.unparse(ast.Module(body=handler.body, type_ignores=[]))
        if "eval.ai_test_record_failed" in body and "logger.error" in body:
            loud.append(handler)
    assert loud, "留档失败的 except 分支里没有那条 ERROR 日志 —— 吞掉了,而且没喊"


def _passes_real_actor(tree: ast.AST, func_name: str) -> bool:
    """那个端点函数**自己**有没有把真实 actor 传下去。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            for kw in call.keywords:
                if kw.arg != "actor":
                    continue
                value = kw.value
                if (
                    isinstance(value, ast.Call)
                    and getattr(value.func, "id", None) == "current_actor"
                ):
                    return True
    return False


def test_both_endpoints_pass_a_real_actor_instead_of_defaulting_to_system():
    """留档要答得出"谁跑的"。默认值 `system` 是给内部调用的,不是给端点的。

    **这条守卫的第一版是被变异抓出来的**,形状与 batch13-3 的 M11 同族:
    它写的是 `"actor=current_actor(request)" in source`,而 `api/workbench.py`
    里另有四处端点也这么写 —— 把文案测试那一处删掉,子串照样在,守卫照样绿,
    而留档从此全部署名 `system`。**窗口没封闭的守卫,验的是"这个文件里有没有
    人这么写过",不是"这里写没写"。** 改成按 AST 定位到那个函数内部。
    """
    checks = (
        ("api/reviews.py", "test_candidate_evaluation"),
        ("api/workbench.py", "test_copy_generation"),
    )
    for rel, func in checks:
        tree = ast.parse((APP / rel).read_text(encoding="utf-8"))
        assert _passes_real_actor(tree, func), f"{rel}::{func} 没有传真实操作者"


# ============================================================ 迁移目标表


def _created_tables() -> set[str]:
    tables: set[str] = set()
    for path in MIGRATIONS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"create_table", "rename_table"}
                and node.args
            ):
                for arg in node.args[:2]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        tables.add(arg.value)
    return tables


def test_every_migration_touches_a_table_some_migration_actually_creates():
    """**本批的来路。** 0055 指着 `evaluations`,而全仓没有这张表。

    只查 `alter_column`:它的第一个参数一定是表名,而且是"改一张已经存在的
    表"这个语义里唯一会写错的地方。`create_index` 的表名参数位置随写法变,
    收进来会带一堆假红,而假红最省事的消法是把整条门禁删掉。

    表名从常量解析:0055 写的是 `_TABLE`,直接读 AST 拿不到值,所以先在
    模块里收一遍字符串常量赋值。
    """
    created = _created_tables()
    offenders: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts: dict[str, str] = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                consts[node.targets[0].id] = node.value.value
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "alter_column"
                and node.args
            ):
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    table = arg.value
                elif isinstance(arg, ast.Name):
                    table = consts.get(arg.id, "")
                else:
                    continue
                if table and table not in created:
                    offenders.append(f"{path.name} -> {table}")
    assert not offenders, f"这些迁移改的表全仓没有人建过:{offenders}"


def test_the_attempt_version_column_is_a_string_on_every_surface():
    """迁移改了型而 ORM 没改,是「测试库和生产库结构不一致」那一类。

    `prompt_template.py` 顶部记着同一个坑的上一次:约束只写在迁移里,
    `create_all()` 建出来的测试库缺了它,于是那件事在测试环境里永远测不出来。
    """
    model = (APP / "models" / "evaluation.py").read_text(encoding="utf-8")
    assert "prompt_version: Mapped[str | None] = mapped_column(String(32)" in model
    schema = (APP / "schemas" / "evaluation.py").read_text(encoding="utf-8")
    assert re.search(r"^    prompt_version: str \| None$", schema, re.M)
    frontend = BACKEND.parent / "frontend" / "src" / "api" / "types.ts"
    assert re.search(
        r"^  prompt_version: string \| null$", frontend.read_text(encoding="utf-8"), re.M
    )


def test_the_version_writer_coerces_instead_of_silently_dropping():
    """原来那句 `isinstance(value, int) else None` 是个静默丢弃器。

    列还是 Integer 时它是对的,而评分链路一旦接上内容哈希,落库的版本会
    集体变空 —— 不报错、不告警,只是那一列从此全是 NULL。
    """
    from app.prompts.versioning import version_text

    assert version_text(7) == "7"
    assert version_text("a1b2c3d4e5f6") == "a1b2c3d4e5f6"
    assert version_text(None) is None
    assert version_text("  ") is None
    assert len(version_text("x" * 99)) == 32

    # 服务层必须**委托**这一份,不许再长回一个本地 isinstance 判断 ——
    # 长回去的话这条守卫仍然绿,而落库的版本又开始悄悄变空
    source = (APP / "services" / "evaluation_service.py").read_text(encoding="utf-8")
    assert "prompt_version=version_text(prompt_version)" in source
    assert "isinstance(prompt_version, int)" not in source


def test_the_new_events_are_registered_with_the_eval_domain():
    from app.core.log_events import EVENTS, LOGGER_DOMAIN_FALLBACK, domain_of_event

    for event in ("eval.ai_test_recorded", "eval.ai_test_record_failed"):
        assert event in EVENTS
        assert domain_of_event(event) == "eval"
    # 兜底域必须与事件码同向。分叉的表现是:漏写 event 的那条日志落进另一个域,
    # 而排障的人按域筛,永远筛不到它
    assert ("app.services.ai_test_archive", "eval") in LOGGER_DOMAIN_FALLBACK


def test_the_record_layer_stays_free_of_sqlalchemy():
    """成形与保留判定必须在没有库的单测里跑得动 —— 这一整个文件就是证据。

    与 `grading-stays-pure` 守的是同一件事:一旦"这条记录长什么样"只能靠
    起一套库来观察,就没有人会去质询它,而这张表的全部价值在于它可信。
    """
    tree = ast.parse((APP / "workflows" / "ai_test_record.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            if node.module.startswith("app."):
                imported.add(node.module)
    assert "sqlalchemy" not in imported
    assert not any(m.startswith("app.models") or m.startswith("app.db") for m in imported)


def test_the_archive_record_is_frozen_so_shaping_has_one_entrance():
    record = AiTestRecord(
        kind=KIND_COPY, subject_type="product", subject_id="p-1",
        success=True, billable=False, actor="bob",
    )
    try:
        record.actor = "someone-else"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("留档行可以被就地改写,成形规则因此不再只有一个入口")
