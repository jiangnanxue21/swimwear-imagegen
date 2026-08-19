"""a65 守卫:迁移链重放(`tools/audit_migration_chain.py`)。

## 这条门禁回答的是一个我自己留了一轮的问题

a64 的交接写着「记录不是处理 —— 值得再问一次真库那一侧有没有 a61 式的余地」,
然后那一轮就结束了。问了之后的答案:**有,而且不小。** 迁移是纯声明式的,
56 份里只有 9 处 raw `execute` 且全是索引,所以表与列的演化能在内存里重放。

## 这里钉的是"门禁自己得能抓住它的来路"

三条自检各造一种真实缺陷:a57 手工发现的「迁移指向不存在的表」、
「ORM 加列而迁移没跟」、「迁移加列而 ORM 没跟」。一条抓不到自己 motivating case
的门禁,和一条什么都不守的门禁,在 CI 上长得一样。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.pure._helpers import offline_gate_targets

BACKEND = Path(__file__).resolve().parents[2]
PROJECT = BACKEND.parent
SCRIPT = BACKEND / "tools" / "audit_migration_chain.py"


def _run(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(cwd / "tools" / "audit_migration_chain.py")],
        cwd=cwd, capture_output=True, text=True, timeout=600,
    )


def _mutated(rel: str, old: str, new: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "backend"
        shutil.copytree(BACKEND, work, ignore=shutil.ignore_patterns("__pycache__"))
        target = work / rel
        source = target.read_text(encoding="utf-8")
        assert source.count(old) == 1, f"锚点过期({rel})—— 这条自检什么都没验"
        target.write_text(source.replace(old, new, 1), encoding="utf-8")
        return _run(work)


def test_the_chain_replays_clean_on_this_tree():
    proc = _run(BACKEND)
    assert proc.returncode == 0, f"审计自己就是红的:\n{proc.stdout}{proc.stderr}"


def test_nothing_is_left_unresolved():
    """解析不出来的那些**没被检查** —— 它们在报告里,但不算缺陷。

    这个数不为零时,对照结论就有一块是空的:一列没被重放出来,最终比对会说
    「ORM 多了这一列」,而那是门禁自己看不见,不是代码有问题。
    实测踩过两次(0020 的两层循环、0049 的单目标循环),都改了解析而不是加豁免。
    """
    proc = _run(BACKEND)
    assert "解析不出来的" not in proc.stdout, (
        f"有解析不出来的操作,对照结论有一块是空的:\n{proc.stdout}"
    )


def test_it_catches_a_migration_pointing_at_a_table_that_never_existed():
    """**a57 手工发现的那一条。** 迁移 0055 指着 `evaluations`,而全仓没有这张表。

    它在任何真实库上直接失败,而当时没有任何东西在看 —— 一份从未被执行过的迁移,
    它的表名没有任何东西在校验。
    """
    proc = _mutated(
        "migrations/versions/0055_prompt_version_string.py",
        '_TABLE = "evaluation_attempts"',
        '_TABLE = "evaluations"',
    )
    assert proc.returncode != 0
    assert "alter_column 目标表不存在:evaluations" in proc.stdout


def test_it_catches_a_column_the_orm_declares_but_no_migration_creates():
    proc = _mutated(
        "app/models/ai_test_run.py",
        '    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")',
        '    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")\n'
        "    ghost: Mapped[str | None] = mapped_column(String(8), nullable=True)",
    )
    assert proc.returncode != 0
    assert "ORM 多 ['ghost']" in proc.stdout


def test_it_catches_a_column_a_migration_creates_but_the_orm_never_declares():
    """这一支是 `prompt_template.py` 顶部那个坑的方向:

    约束/列**只写在迁移里**,`create_all()` 建出来的测试库缺了它 ——
    「测试库和生产库的结构不一致,比没有测试更危险:它让人以为测过了」。
    """
    proc = _mutated(
        "migrations/versions/0056_ai_test_runs.py",
        '        sa.Column("actor", sa.String(64), nullable=False, server_default="system"),',
        '        sa.Column("actor", sa.String(64), nullable=False, server_default="system"),\n'
        '        sa.Column("ghost", sa.String(8), nullable=True),',
    )
    assert proc.returncode != 0
    assert "迁移多 ['ghost']" in proc.stdout


def test_relationships_are_not_mistaken_for_columns():
    """`relationship()` 也写成 `Mapped[...]`,但它不是列。

    不排掉的话每一张有关系的表都会被报成「ORM 多了一列」—— 第一版正是这么跑出来的,
    18 张表全在假红里。
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'factory == "relationship"' in source


def test_the_scope_is_printed_not_only_documented():
    """射程要写在**输出**里。写在 docstring 里的射程没人读得到 ——

    跑门禁的人看到的只有 stdout(a64 那条守卫的同一个判据)。
    """
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    printed = "".join(
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"
    )
    # a66 把列型纳入射程,a68 又把可空与 server default 纳进来 —— 这句话
    # 每次都跟着改,守卫也每次都跟着改。**否则它守的是一句已经不成立的承诺**,
    # 而那正是 A67-ISSUE-004 的形状:代码前进、自述留在原地。
    #
    # 判据从"写着不比什么"换成"写着比了什么":前者随每次扩容作废,
    # 后者只在射程真的缩水时才红
    assert "可空" in printed and "server default" in printed, (
        "输出里没说它比到可空与 server default 这一层 —— a68 扩过容了"
    )
    assert "拿不出确定值的列直接跳过" in printed, "没说清跳过的条件"
    assert "raw DDL 不解析" in printed, "没说清 op.execute 不在射程内"


def test_the_gate_is_wired_into_all_three_places():
    makefile = (PROJECT / "Makefile").read_text(encoding="utf-8")
    assert "audit-migration-chain:" in makefile
    assert "audit-migration-chain" in offline_gate_targets(makefile), "没进 check-offline"
    ci = (PROJECT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "audit_migration_chain.py" in ci, "CI 没跑它"
    delivery = (BACKEND / "tools" / "verify_delivery.py").read_text(encoding="utf-8")
    assert '"audit_migration_chain.py"' in delivery, "门禁清单表里没登记"


# ============================================================ 列型比对(a66 扩)


def test_column_types_are_compared_not_only_names():
    """a65 只比列名集合。a66 扩到类型名与宽度 —— **扩容当天抓到一条真缺陷**。

    `platform_rejections.status` 库里是 `varchar(16)`,而 ORM 声明 `String(32)`,
    而 `RejectionStatus.FIXED_PENDING_EXPORT` 是 **20 个字符**。
    `platform_service.py:539` 那一句写入在真实库上会被 PostgreSQL 直接拒掉 ——
    运营点「我已经改好了」,拿到的是一个 500。
    """
    proc = _run(BACKEND)
    assert "两侧都解析出类型的列" in proc.stdout, "输出里没有报比了多少列"
    count = int(proc.stdout.split("两侧都解析出类型的列 ")[1].split()[0].rstrip("）)"))
    assert count > 400, f"只比了 {count} 列 —— 解析多半失效了,而失效时它照样是绿的"

    # a68:可空那一格也要报数,理由同上 —— 解析失效时它会静静地比 0 列
    assert "两侧都写明可空的列" in proc.stdout, "可空比对没有报数"
    nullable = int(
        proc.stdout.split("两侧都写明可空的列 ")[1].split()[0].rstrip("）)")
    )
    assert nullable > 400, f"只比了 {nullable} 列的可空 —— 解析多半失效了"


def test_it_catches_a_width_that_the_database_cannot_hold():
    """把迁移 0057 退回 16,门禁必须当场红。"""
    proc = _mutated(
        "migrations/versions/0057_platform_rejection_status_width.py",
        "        type_=sa.String(32),\n        existing_nullable=False,\n"
        '        existing_server_default="OPEN",\n    )\n\n\ndef downgrade',
        "        type_=sa.String(16),\n        existing_nullable=False,\n"
        '        existing_server_default="OPEN",\n    )\n\n\ndef downgrade',
    )
    assert proc.returncode != 0
    assert "platform_rejections.status" in proc.stdout


def test_the_rejection_status_column_really_fits_every_enum_value(  # noqa: D103
) -> None:
    """**这一条不看两侧一不一致,看那个宽度够不够装。**

    两侧一致但都太窄的话,上面那条守卫是绿的,而写入照样炸 —— 一致性和正确性
    是两件事,而这张表的状态值来自一个会加成员的枚举。
    """
    import ast as _ast

    from app.core.enums import RejectionStatus

    source = (BACKEND / "app" / "models" / "platform_rejection.py").read_text(
        encoding="utf-8"
    )
    tree = _ast.parse(source)
    width = None
    for node in _ast.walk(tree):
        if not (isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name)):
            continue
        if node.target.id != "status" or not isinstance(node.value, _ast.Call):
            continue
        for arg in node.value.args:
            if isinstance(arg, _ast.Call) and getattr(arg.func, "id", None) == "String":
                if arg.args and isinstance(arg.args[0], _ast.Constant):
                    width = arg.args[0].value
    assert width, "解析不出 status 的列宽 —— 这条守卫失去了判据"
    longest = max(RejectionStatus, key=lambda member: len(member.value))
    assert len(longest.value) <= width, (
        f"{longest.value} 有 {len(longest.value)} 个字符,而列宽只有 {width}"
    )
