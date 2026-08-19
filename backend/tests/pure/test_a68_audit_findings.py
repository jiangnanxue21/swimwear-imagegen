"""a68 守卫:a67 外部审计那四条,各留一个不会被静默撤销的钉子。

## 这个文件的射程,写在最前面

**它不是这四条的证明。** 真正的证明分别在:

    ISSUE-001   tests/test_platform_rejection_timestamps_db.py(真库,含闭环)
                tools/audit_migration_chain.py(扩容后能重新抓到它)
    ISSUE-002   frontend/tests/component/error-and-cold-start.test.tsx(真实渲染矩阵)
    ISSUE-003   tests/pure/test_a68_fe_pure_runtime.py + CI 里那次真实执行
    ISSUE-004   docs/STATUS.md、HANDOVER.md 本身

审计报告花了整整一节说这件事(§7.2):`643 anchors + 772 source guards +
ErrorNotice ratchet 6/6` 全绿,而 ISSUE-002 照样存在 —— 因为 ratchet 证明的是
「页面换成了 ErrorNotice」,不是「ErrorNotice 在 403 + onRetry 时行为正确」。

所以下面这些断言只回答一个问题:**修复有没有被悄悄撤掉。** 它们守的是
"那行判据还在""那条迁移还在""那个比对还在",不是"它是对的"。把它们读成
后者,就正好重演了这一轮要修的那个错误。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
BACKEND = PROJECT / "backend"
FRONTEND = PROJECT / "frontend"

MIGRATION_AUDIT = BACKEND / "tools" / "audit_migration_chain.py"
TIMESTAMP_MIGRATION = BACKEND / "migrations" / "versions" / "0058_platform_rejection_timestamps.py"
ERROR_NOTICE = FRONTEND / "src" / "components" / "ErrorNotice.tsx"


# ============================================================ ISSUE-001


def test_the_timestamp_migration_is_still_in_the_chain():
    """0058 还在,而且还接在 0057 后面。

    迁移文件被挪走 / 改 down_revision 的后果不会当场报错 —— alembic 只认
    revision 号,链断在哪一环要到真库 upgrade 时才知道。
    """
    assert TIMESTAMP_MIGRATION.exists(), (
        "0058 不见了 —— platform_rejections 的时间戳会回到可空无默认"
    )
    source = TIMESTAMP_MIGRATION.read_text(encoding="utf-8")
    assert re.search(r'revision:\s*str\s*=\s*"0058"', source)
    assert re.search(r'down_revision:.*=\s*"0057"', source)


def test_the_migration_backfills_before_it_tightens():
    """回填必须在收紧之前。

    顺序反了的话,`SET NOT NULL` 会在一张还有 NULL 的表上直接失败 ——
    整条迁移中断,而且是在客户的库上。
    """
    source = TIMESTAMP_MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(source)
    upgrade = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
    )
    kinds: list[str] = []
    for node in ast.walk(upgrade):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if getattr(node.func.value, "id", None) != "op":
            continue
        if node.func.attr == "execute":
            kinds.append("backfill")
        elif node.func.attr == "alter_column":
            kinds.append("alter")

    assert "backfill" in kinds and "alter" in kinds
    assert kinds.index("backfill") < kinds.index("alter"), (
        "先收紧后回填 —— 这条迁移会在任何一张有历史 NULL 的表上当场失败"
    )


def test_the_backfill_never_reaches_for_the_export_time():
    """回填不许用 `export_at`。

    它是**被驳回的那次导出**的时间,早于驳回本身。拿它当 `rejected_at`,
    驳回之前的旧导出会被算成「之后」,闸门自动放行 —— 一条还没真修的驳回
    被悄悄关掉。`resolve_gate` 的方向是宁可误报,这里必须同向。
    """
    source = TIMESTAMP_MIGRATION.read_text(encoding="utf-8")
    upgrade_body = source[source.index("def upgrade()") :]
    # 注释里说明"为什么不用它"是应该的,这里只看 SQL 常量
    sql = "\n".join(
        re.findall(r'_BACKFILL_\w+\s*=\s*f?"""(.*?)"""', source, re.S)
    )
    assert sql, "找不到回填 SQL —— 它被内联了?那这条守卫要跟着改"
    assert "export_at" not in sql, (
        "回填用上了 export_at —— 那是驳回**之前**的时间,会让闸门误放行"
    )
    assert "now()" in sql, "没有兜底来源,历史行会留下 NULL 而收紧会失败"
    assert "audit_logs" in sql, "最精确的那个来源没用上"
    assert "op.execute" in upgrade_body


def test_the_migration_audit_compares_nullable_and_server_default():
    """迁移审计必须还在比可空与 server default。

    **a67 时它明说不比这两样,而 ISSUE-001 正好长在那个盲区里。** 扩容那天
    它立刻抓到了那 4 条差异(和外部审计独立算出来的完全一致)。这条守卫盯着
    这个能力不被"简化"掉 —— 那会让同一类缺陷重新变成不可见。
    """
    source = MIGRATION_AUDIT.read_text(encoding="utf-8")
    # **每一条都收窄到唯一窗口。** 只写 `"_column_facts" in source` 的话,
    # 那个名字在文件里出现 6 次(定义 1 次、调用 5 次)—— 把定义改坏,
    # 子串仍在别处,守卫照样绿。`audit_guard_windows.py` 盯的就是这件事,
    # 而这条守卫自己刚被它抓过一次。
    assert "def _column_facts(node: ast.AST)" in source, "列事实解析器没了"
    assert "def _alter_facts(node: ast.Call)" in source, (
        "alter_column 的事实变更不再被应用"
    )
    assert 'if mine["nullable"] != theirs["nullable"]:' in source, "可空比对没了"
    assert "nullable_pairs += 1" in source, (
        "可空比对的计数没了 —— 计数是「它到底比了几列」的唯一证据,"
        "没有它,解析失效时门禁会静静地比 0 列然后报绿"
    )
    assert 'theirs["server_default"] and not mine["server_default"]' in source, (
        "server default 的危险方向比对没了 —— ISSUE-001 正是这个方向"
    )

    # 摘要那行不许再自称"不比可空 / 默认值" —— 文档漂移是 ISSUE-004 的形状
    assert "不比可空" not in source, (
        "工具已经在比了,而它的自述还写着不比 —— 下一轮审核会照着这句话跳过它"
    )


def test_the_migration_audit_speaks_up_when_it_cannot_read_a_column_name():
    """`alter_column` 解析不出列名时要记进 unresolved,不许静默跳过。

    0058 第一版写的是 `for column in (...)`,而循环那一层只映射到
    add_column/drop_column。于是那四次 alter 对门禁**完全隐形**,门禁继续
    报着已经修好的差异 —— 而那是门禁自己看不见,不是代码有问题。

    少报好过误报是这个工具的既定方向,但少报也得出声。
    """
    source = MIGRATION_AUDIT.read_text(encoding="utf-8")
    block = source[source.index('elif op == "alter_column"') :]
    block = block[: block.index("return schema")]
    assert "column is None" in block and "unresolved.append" in block, (
        "alter_column 又回到了静默跳过 —— 一次改可空/改默认的操作会对门禁隐形"
    )


# ============================================================ ISSUE-002


def test_the_retry_button_reads_retriable():
    """重试按钮的判据是 `technical.retriable`。

    原来是 `outcome !== 'UNKNOWN'`,于是 403 / 404 / 422 一律画出重试按钮,
    而同一张卡片的技术详情里写着「重试不会改变结果」。运营信的是按钮。
    """
    source = ERROR_NOTICE.read_text(encoding="utf-8")
    action = source[source.index("action={") :]
    assert "technical.retriable" in action, (
        "重试按钮不再看 retriable —— 不可重试的错误会重新长出重试按钮"
    )
    assert "onRetry &&" in action


def test_the_retry_button_does_not_grow_a_second_judgement():
    """而且判定仍然只有 `describeError` 一份。

    这个组件的文件头写着「判定不在这里」:在这里加一个 `if (status === 403)`
    就是把判定分成两份,而两份迟早会各说各话 —— ISSUE-002 本身就是这么来的
    (组件自己看 outcome,而 client.ts 早算好了 retriable)。

    ## 这条守卫的第一版误伤了什么

    第一版禁的是裸的 `status ===`,于是它把这一行也判成了违规:

        technical.status === null ? '未到后端' : technical.status

    那是**显示格式化** —— 把"没有状态码"渲染成一句人话,不是在按状态码决定
    任何行为。禁它等于要求这个组件连"这一列是空的"都不许知道。

    所以判据收窄成**状态码字面量**:认的是 `=== 403`、`>= 500` 这种,
    也就是"组件自己认识 HTTP 语义"的那个形状。`=== null` 不在其中。
    """
    source = ERROR_NOTICE.read_text(encoding="utf-8")
    body = source[source.index("export default function ErrorNotice") :]
    # 注释里举例说明"不许这么写"是应该的,所以只看代码行
    code = "\n".join(
        line for line in body.splitlines()
        if not line.lstrip().startswith(("//", "*", "/*"))
    )
    hits = re.findall(r"(?:status|Status)\s*(?:===|!==|>=|<=|>|<)\s*(\d{3})", code)
    assert not hits, (
        f"ErrorNotice 里按状态码 {sorted(set(hits))} 自己做了判定 —— 判定又分成两份了。"
        "要改的是 client.ts 的 describeError,不是这里"
    )


def test_the_render_matrix_covers_the_combination_that_was_broken():
    """组件测试必须覆盖「不可重试的错误 + 传了 onRetry」。

    旧用例把错误类型和"传不传 onRetry"绑在一起测:403 那次不传,500 那次才传。
    于是真正出问题的组合一次都没渲染过,而六个 a67 页面全是那个组合。
    """
    matrix = (FRONTEND / "tests" / "component" / "error-and-cold-start.test.tsx").read_text(
        encoding="utf-8"
    )
    assert "传了 onRetry 之后" in matrix, "重试矩阵不见了"
    for code in ("403", "404", "422", "429", "500"):
        assert code in matrix, f"矩阵里缺 {code}"
    assert "kind=\"write\"" in matrix, (
        "写请求超时那一支没被覆盖 —— 而它正是旧判据唯一守住的东西"
    )
