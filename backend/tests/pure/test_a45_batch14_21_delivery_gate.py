"""A45-batch14-21:还款日门禁自己的守卫。

## 为什么这条门禁需要被单独盯着

`verify_delivery` 里那条「欠账守卫都在还款日之内」是用来替**别人**记账的。
它自己失效的表现,恰恰是它要防的那件事:**一切照常绿,而欠账在后面躺着。**

这个仓库为同一个形状付过两次学费:

    §3.30  守卫也要被审 —— 一条只在别人身上生效的规矩,自己没有规矩管
    §3.36  门禁的覆盖面等于有人付过代价的地方

而这一条比前两次更需要:前两次失效时至少有一条业务守卫还在;这一条失效时
**什么都没有** —— 它是欠账这件事在仓库里唯一的机械记账。

## 这个文件不去跑那个门禁

跑它要读整棵树、要有 `docs/STATUS.md`,而且它的结论会随仓库状态变。
纯层钉的是**它有没有那几段判定**,以及那几段判定有没有被写成平凡真 ——
后者正是 D 组变异逐条撞的东西。
"""
from __future__ import annotations

import ast
import re

from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT

GATE = BACKEND_ROOT / "tools" / "verify_delivery.py"


def _gate_source() -> str:
    return GATE.read_text(encoding="utf-8")


def _gate_func(name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(_gate_source())):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"`verify_delivery` 里找不到 {name}")


def _gate_const(name: str) -> str:
    """从门禁源码里取出一个字符串常量。**不在这里抄一份。**

    抄一份的下场是变异 D8 撞出来的:把 `DEBT_GUARD_MARKER` 改成一个谁也
    匹配不上的值(繁体、半角冒号、多一个空格),门禁当场一条欠账都找不到
    —— 而这个文件里那条"至少有三条欠账"的守卫用的是**自己那份**字符串,
    照样找得到,照样绿。

    两份定义会漂移,而这里漂移的表现是**门禁恒绿**。所以判定用的字符串
    只有一个来源:门禁自己。
    """
    for node in ast.walk(ast.parse(_gate_source())):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
            and isinstance(node.value, ast.Constant)
        ):
            return node.value.value
    raise AssertionError(f"`verify_delivery` 里找不到常量 {name}")


def _code(fn: ast.FunctionDef) -> str:
    """函数体,剥掉 docstring。见 §3.34 第二节。

    这一条在本文件里格外要紧:那个门禁的文档字符串**逐字写着**它要检查的
    每一个关键词(「还款日:阶段 N」「欠账已还」……),不剥的话下面每一条
    断言都会被那段解释喂成平凡真 —— 门禁被掏空时守卫照样绿。
    """
    return "\n".join(
        ast.unparse(node)
        for node in fn.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    )


def test_the_gate_is_registered_in_the_checks_table():
    """写好了不挂进 `CHECKS` = 从来没跑过。

    这条门禁的其余部分再对,不在那张表里就等于不存在 —— 而"不存在"的
    表现和"全都通过"一模一样:`verify_delivery` 打印一行绿色的总数,
    只是那个总数少了一。
    """
    source = _gate_source()
    assert "check_no_debt_guard_is_past_its_due_stage)" in source, (
        "还款日门禁没有挂进 CHECKS —— 它从来不会被执行,而输出看起来一切正常"
    )


def test_the_gate_actually_compares_the_due_stage_against_the_current_one():
    """到期判定必须真的比一次。

    删掉这个比较是让门禁"看起来还在"的最省事做法:标记照读、还款日照解析、
    一条都不报。而那正是退回本批之前的状态 —— 守卫钉的是「没接线」这个
    事实,不钉「什么时候该接」。
    """
    body = _code(_gate_func("check_no_debt_guard_is_past_its_due_stage"))
    assert "<= stage" in body, "没有把还款日和当前阶段比 —— 到期不还不会有任何东西响"
    assert "overdue.append(" in body, "比了但不记 —— 结论没有落点"
    assert "raise Failure(" in body, "记了但不报 —— 门禁恒绿"


def test_a_debt_guard_without_a_due_date_is_rejected():
    """没声明还款日的欠账要被拦下。

    少了这一条,欠账又回到"只写在 docstring 里"的状态 —— 而 §3.34 那句话
    说的就是它:写在文档里的欠账有两种下场,被忘掉,或者在还清之后继续
    吓唬人。
    """
    body = _code(_gate_func("check_no_debt_guard_is_past_its_due_stage"))
    assert "undeclared.append(" in body, "没有还款日的欠账被放过了"
    assert "if not due:" in body, (
        "「有没有声明还款日」这个分支不见了 —— 而没有声明正是最常见的那种"
    )


def test_the_name_hints_close_the_bypass():
    """反向检查在:不写标记不能当成绕过。

    整条门禁最省事的绕法是把 `记的是欠账` 那句话删掉。命名式反向检查堵的
    就是这一步 —— 它盯的是名字,而改名的人会知道自己在改什么。
    """
    source = _gate_source()
    body = _code(_gate_func("check_no_debt_guard_is_past_its_due_stage"))
    assert "DEBT_GUARD_NAME_HINTS" in body, "命名式反向检查被删了 —— 删标记即可绕过"
    assert "unmarked.append(" in body
    hints = re.search(r"DEBT_GUARD_NAME_HINTS = \((.*?)\)", source, re.S)
    assert hints and hints.group(1).count('"') >= 6, (
        "命名式清单空了或只剩一条 —— 它要覆盖仓库里真实用过的那几种写法"
    )


def test_an_empty_ledger_is_a_failure_not_a_silent_pass():
    """一条欠账都找不到时**红**,不是静静通过。

    归零只有两种可能:真的全还清了(那时该连这条门禁一起重新想),
    或者标记被人顺手删了。后者才是常见的那一种,而它的表现是这条门禁
    静静地永远绿 —— 与「决策日志一节都没有」那条同一个形状。
    """
    body = _code(_gate_func("check_no_debt_guard_is_past_its_due_stage"))
    assert "seen_debt" in body, "没有统计找到几条欠账 —— 标记被删时门禁会安静地全绿"
    assert "if not seen_debt:" in body


def test_the_current_stage_comes_from_status_not_from_a_constant_here():
    """当前阶段读 `docs/STATUS.md`,不写死在脚本里。

    写死的常量没有人会记得改,而 STATUS 顶部那张阶段清点表是每批都要动的
    东西。分叉的表现是门禁按一个没人维护的数字判到期 —— 而它偏向的方向
    是"永远不到期"。
    """
    fn = _gate_func("_declared_stage")
    body = _code(fn)
    assert "STATUS.md" in body, "当前阶段不是从 STATUS 读的"

    # **不能只断言 `raise Failure(` 在函数体里。** 变异 D6 撞出来的:
    # 在它前面插一句 `return 99`、把原来那段塞进 `if False:`,
    # 这条断言照样成立 —— 死代码把它喂成了平凡真。
    #
    # 与 §3.34 第二节那两条同源(docstring 喂假断言),而这次的来源是
    # **死代码**。判据换成一个死代码没法满足的形状:出口只许有一个,
    # 而且必须是那次转换 —— 多一个出口就意味着某条路径绕过了报错。
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert len(returns) == 1, (
        f"`_declared_stage` 有 {len(returns)} 个出口 —— "
        "多出来的那个会让「读不到标记」这件事静静地拿到一个数字"
    )
    assert "int(match.group(1))" in ast.unparse(returns[0]), (
        "唯一的出口不是那次转换 —— 门禁会按一个编出来的阶段判到期"
    )
    assert "raise Failure(" in body, (
        "读不到标记时没有报错 —— 静静跳过等于这条门禁自己也成了一笔没有"
        "还款日的欠账"
    )
    caller = _code(_gate_func("check_no_debt_guard_is_past_its_due_stage"))
    assert "_declared_stage()" in caller, "门禁没有去问当前阶段"


def test_status_carries_the_machine_readable_stage_marker():
    """标记真的在 `docs/STATUS.md` 里,而且是个数字。

    反向钉一句:标记不见了的时候门禁会红,但那时人最省事的做法是把门禁
    改成"读不到就跳过"。所以这条守卫和上面那条要成对 —— 一条钉标记在,
    一条钉读不到时必须报错。
    """
    status = (PROJECT_ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    match = re.search(r"<!--\s*DELIVERY_STAGE\s*[:：]\s*([0-9]+)\s*-->", status)
    assert match, "STATUS.md 里没有 `<!-- DELIVERY_STAGE: N -->` 标记"
    assert 1 <= int(match.group(1)) <= 6, (
        f"阶段写成了 {match.group(1)} —— PRD §13 只有 6 个阶段"
    )


def test_the_repaid_marker_exists_so_the_past_tense_does_not_trip_the_gate():
    """还清那一档有自己的一句话。

    这个仓库的习惯是把欠账守卫翻转成正向守卫、并留一段"原来记的是欠账,
    某批把它收了"的叙述。那段叙述会让欠账标记在**过去时**里命中 ——
    本批第一版当场误伤了 14-10 那条。

    解法不是把正则写得更巧(§3.26:按字符串在源码里找东西,找到的从来
    不保证是你以为的那个位置),是让三种状态各有一句话。
    """
    source = _gate_source()
    assert 'DEBT_REPAID_MARKER = "欠账已还"' in source, "还清那一档没有标记"
    body = _code(_gate_func("check_no_debt_guard_is_past_its_due_stage"))
    # 找的是 `head` 不是 `doc` —— 声明只认开头那一段(见下面那条守卫)。
    # 本批把范围收紧时这一条当场红了,红得对:它钉的形状确实变了
    assert "DEBT_REPAID_MARKER in head" in body, (
        "门禁没有认还清标记 —— 翻转过的守卫会被当成欠着的"
    )


def test_the_declaration_is_read_from_the_opening_paragraph_only():
    """声明只认 docstring 开头那一段。**这条是被门禁咬自己换来的。**

    第一版扫全文,当场误伤两批:先是一条**已还清**的守卫(叙述里留着
    "原来记的是欠账"),后是本文件里两条**盯着这条门禁自己**的元守卫 ——
    它们要讲清自己守的是什么,就必须说出那句标记,于是被判成欠账。

    第二个方向值得单独记:**一条守卫说出它守的那件事的名字,不等于它就是
    那件事。** 按全文匹配的判据分不开这两者,而分不开的表现是假红 ——
    而假红最省事的消法是把整条门禁删掉。

    收进第一段不是绕开,是把一条**本来就存在**的规矩写下来:仓库里的
    欠账守卫声明都在第一行。反向也钉一句:范围不许被放回全文。
    """
    source = _gate_source()
    assert "DEBT_DECLARATION_PARAGRAPHS" in source, (
        "声明范围又变回全文了 —— 解释里提一句标记就会被判成欠账,"
        "而假红最省事的消法是把门禁删掉"
    )
    body = _code(_gate_func("check_no_debt_guard_is_past_its_due_stage"))
    assert "DEBT_GUARD_MARKER not in head" in body, (
        "标记还是在整篇 docstring 里找的"
    )
    assert "DEBT_REPAID_MARKER in head" in body


def test_open_debts_all_declare_a_stage_that_has_not_arrived():
    """今天仍欠着的守卫，还款日都解析得出来、都还没到期。

    **这条钉的是"门禁真的读到了东西"。** 上面那些断言证明的是门禁有那几段
    判定;这一条证明它们在真实数据上跑得通 —— 一个正则写错、一个全角冒号
    没认,表现都是"一条欠账都没找到",而那时门禁看起来一切正常。

    条数不写死(§CLAUDE.md 那条:规矩要写成不变式,不是当时那个值),
    只要求**至少有**那么几条、且每一条都解析得出阶段。
    """
    stage = int(
        re.search(
            r"<!--\s*DELIVERY_STAGE\s*[:：]\s*([0-9]+)\s*-->",
            (PROJECT_ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8"),
        ).group(1)
    )
    # **标记从门禁那边取,不在这里抄。** 见 `_gate_const` 的文档
    marker = _gate_const("DEBT_GUARD_MARKER")
    repaid = _gate_const("DEBT_REPAID_MARKER")
    found: list[tuple[str, int]] = []
    for path in sorted((BACKEND_ROOT / "tests" / "pure").rglob("test_*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
                continue
            doc = ast.get_docstring(node) or ""
            head = doc.split("\n\n")[0]
            if marker not in head or repaid in head:
                continue
            due = re.search(r"还款日\s*[:：]\s*阶段\s*([0-9]+)", doc)
            assert due, f"{path.name}::{node.name} 的还款日解析不出来"
            found.append((node.name, int(due.group(1))))

    assert found, (
        f"只找到 {len(found)} 条欠账守卫 —— 正则认不出声明时,"
        "门禁的表现和「欠账全还清了」一模一样"
    )
    overdue = [name for name, due in found if due <= stage]
    assert not overdue, f"这些欠账已经到期:{overdue}"


def test_the_migration_chain_guard_still_proves_it_saw_every_file():
    """迁移链守卫的**覆盖率断言**必须还在。**这是一条元守卫。**

    那条守卫十二批以来一直是瞎的:正则只认 `revision: str = "..."`,
    而 0041 写的是裸赋值,于是它从来没进过链表。当时它**碰巧绿** ——
    0041 是 head,不出现在任何 down_revision 里,少了它照样只有一个 head。

    修法不是放宽正则(下一种写法会让同一件事再来一次),是让它自己证明
    自己看全了:解析出的 revision 条数必须等于 `versions/` 下的文件数。

    **但那条断言写在守卫自己身上,而守卫的断言没有人守。** 变异 C1 把它
    改成 `assert True` 时,那个套件跑起来一片绿 —— 没有任何东西会红。
    所以这里加一层:元守卫钉住"那句断言还在"。

    只加一层,不再往上套。§3.36:门禁的覆盖面等于有人付过代价的地方 ——
    这一层是本批真的付过代价的那一处(0042 挂上去时它红了),
    再往上就是没有对象的对称性。
    """
    src = (
        BACKEND_ROOT / "tests" / "pure" / "test_a45_batch14_15_attribution.py"
    ).read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef)
        and n.name == "test_the_migration_chain_has_exactly_one_head"
    )
    compares = [
        ast.unparse(n.test)
        for n in ast.walk(fn)
        if isinstance(n, ast.Assert)
    ]
    assert "len(revisions) == len(files)" in compares, (
        "迁移链守卫的覆盖率断言不见了 —— 它会退回那条看不见裸赋值 revision 的"
        "瞎正则,而漏掉的文件在链末端时它碰巧是绿的"
    )
    assert not any(c == "True" for c in compares), (
        "那条守卫里出现了 `assert True` —— 一条恒真的断言比没有断言更糟,"
        "它占着一个看起来有人在守的位置"
    )


def test_the_gate_offers_exactly_one_form_of_due_date():
    """还款日只有「阶段 N」一种写法。

    第一版有第二种「条件式」,意思是"某件事落地那天守卫自己会红"。回头核
    仓库里现存欠账时发现**没有一条用得上它** —— 每一条都自称会自翻转,
    而每一条同时都有真实的阶段死线。

    留一个永远不被走的分支,这条门禁的覆盖就有一格是假的。这与「别把明知
    抓不住的变异列进脚本」是同一条规矩:**数字要能被兑现,否则它是谎话。**
    """
    source = _gate_source()
    assert "_DUE_CONDITIONAL" not in source, (
        "「条件式」那个分支回来了 —— 仓库里没有一条欠账用得上它,"
        "而永不被走的分支会让覆盖数变成谎话"
    )
    assert source.count("_DUE_STAGE = re.compile") == 1


def test_git_tracking_gate_covers_runtime_tests_and_delivery_tools():
    """Git 门禁不能只看迁移和 DB 测试，clean checkout 还需要运行源码与工具。"""
    body = _code(_gate_func("check_every_migration_and_db_test_is_tracked_by_git"))
    required_roots = (
        "BACKEND / 'app'",
        "BACKEND / 'migrations' / 'versions'",
        "BACKEND / 'tests'",
        "BACKEND / 'tools'",
        "FRONTEND / 'src'",
        "FRONTEND / 'tests'",
        "FRONTEND / 'tools'",
        "PROJECT_ROOT / 'tools'",
    )
    for root in required_roots:
        assert root in body, f"Git 跟踪门禁漏掉交付目录：{root}"

    assert "rglob('*')" in body, "门禁没有递归检查目录里的新增源码"
    assert "p.suffix.lower() in suffixes" in body, "门禁没有把范围约束在源码后缀"
    assert "*git_paths" in body, "git ls-files 没有读取与工作树检查相同的目录集合"
