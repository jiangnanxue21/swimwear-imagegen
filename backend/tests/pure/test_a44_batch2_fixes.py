"""A44 第二批的复核补丁:A-07 / A-08 / A-13 的评审发现。

补丁本身的判定逻辑测试在 `test_workbench_platform.py`(A-13 那 5 条)与
`test_workbench_query_budget.py`(A-07/A-08 的查询形状)。这个文件放的是
**复核阶段补上的那几条**,每条对应一处"补丁做对了主体、漏了一个角"的地方。

    tiebreaker   清单与详情各自挑"最新 attempt",并列时可能挑中不同的行
    迁移 0029    A-13 的证据链按 draft_id 反查,而那一列上没有索引
    前端契约     后端加了 has_new_attempt,前端类型里没有 -> 界面拿不到

办法同 `test_a44_batch1_fixes.py`:钉 AST 节点,不做字符串包含 —— 那种断言
把代码删掉只留注释照样绿。
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"
MIGRATIONS = BACKEND_ROOT / "migrations" / "versions"
FRONTEND = BACKEND_ROOT.parent / "frontend" / "src"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


# ==========================================================
# A-13:证据链那一列要有索引
# ==========================================================


def test_the_draft_id_index_exists_in_both_model_and_migration():
    """模型与迁移必须同时声明,否则 autogenerate 会想把它删掉。

    **退化成什么样:** 只写迁移不写模型 -> 下一次 autogenerate 生成一条
    `drop_index`,索引在某次例行迁移里静悄悄消失,而 A-13 的证据链查询退回
    全表顺序扫 —— 没有任何报错,只是驳回页越来越慢。
    """
    model = _read(APP / "models" / "publishing.py")
    tree = ast.parse(model)
    declared = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Index"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert "ix_channel_listings_draft" in declared, (
        "模型没声明 draft_id 索引 —— 迁移建的那条会被 autogenerate 当成多余的"
    )

    migration = _read(MIGRATIONS / "0029_channel_listing_draft_index.py")
    assert 'down_revision: Union[str, None] = "0028"' in migration, (
        "0029 没有接在 0028 后面 —— 迁移链断了"
    )
    mtree = ast.parse(migration)
    ops = {
        node.func.attr
        for node in ast.walk(mtree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "create_index" in ops and "drop_index" in ops, (
        "0029 的 upgrade/downgrade 不是成对的建/删索引"
    )


def test_the_evidence_query_still_joins_on_the_indexed_column():
    """索引是为 `_publish_attempt_entries` 的那个 WHERE 建的。

    **退化成什么样:** 有人把关联口径从 `ChannelListing.draft_id` 改成别的
    (比如绕 spu),索引还在、但一次都用不上,而没有任何测试会红。
    """
    src = _read(APP / "workbench" / "platform_service.py")
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_publish_attempt_entries"
    )
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "draft_id" in attrs, "证据链不再按 draft_id 关联,0029 的索引就白建了"

    # 口径断言必须钉**属性节点**,不能用 `"SUCCEEDED" in ast.dump(fn)` ——
    # 这个函数的 docstring 里就写着"只认 SUCCEEDED",dump 会把它一起吐出来,
    # 于是把状态改成 PENDING 照样绿。上一批的复核说明记过同一个坑(那次是
    # 假红方向,这次是假绿方向),两个方向都要防。
    statuses = {
        node.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "PublishAttemptStatus"
    }
    assert statuses == {"SUCCEEDED"}, (
        f"证据链认的状态是 {sorted(statuses) or '(一个都没有)'} —— "
        "PENDING/IN_FLIGHT 还没有结果,UNKNOWN 不知道平台收没收到,"
        "FAILED/ABORTED 根本没提交成功,都不构成「已重新提交给平台」"
    )


# ==========================================================
# 前端契约:后端加的字段前端要拿得到
# ==========================================================


def test_the_frontend_declares_has_new_attempt():
    """后端 `serialize_resolve_gate` 加了字段,前端类型里没有 = 界面拿不到。

    这正是 A-09 的成因形状:一处改动看起来完成了,另一头没有对上,而两边
    都不报错。上一批的复核说明里自己也栽过一次(失效了一个不存在的 query key)。

    **退化成什么样:** 后端算得好好的"还缺哪一步",界面永远只显示导出那半边。
    """
    ts = _read(FRONTEND / "api" / "workbench.ts")
    body = ts.split("export interface ResolveGate")[1].split("}")[0]
    for field in ("has_new_export", "has_new_attempt", "fingerprint_changed"):
        assert field in body, f"ResolveGate 接口缺 {field}"

    py = _read(APP / "workbench" / "platform_service.py")
    fn = next(
        n
        for n in ast.walk(ast.parse(py))
        if isinstance(n, ast.FunctionDef) and n.name == "serialize_resolve_gate"
    )
    keys = {
        n.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "has_new_attempt" in keys, "后端不再输出 has_new_attempt,前端类型成了空头支票"
