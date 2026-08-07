"""A45-batch14-14:守卫的窗口必须是封闭的。

## 这一批把两次踩过的坑做成了门禁

14-13 一次撞见两条自己人写的假绿守卫,两条都是**反向断言吃了一段被切窄的
源码**:一条的定位串 `),` 被登记项注释里的 `(归属外键不存在),` 提前命中,
另一条用的是定长窗口 `[:200]`。

两条的后果**都不是变红,是变松**:正向断言碰巧还在残段里,而反向断言被残段
喂成**平凡真** —— 守卫在某次无关改动的那一刻悄悄不设防,并且照样打印 PASS。

这一批不再逐条修,而是把判据做成 `tools/audit_source_guards.py`:
**喂给反向断言的窗口必须是封闭的**。全树扫出 10 处,已逐处收口。

## 为什么只管反向断言

失效方向不一样:

    正向断言碰上窄窗口   断言变假 -> 红 -> 有人会看见
    反向断言碰上窄窗口   断言变真 -> 绿 -> 没有人会看见

一刀切「不许用字符串切源码」会把 87 处正当用法一起判红,而它们的失效
是响的。规矩要挑那条**不响**的。
"""
from __future__ import annotations

import sys

from tests.pure._helpers import (
    BACKEND_ROOT,
    braced_block,
    expect_raises,
    only_line,
    window,
)

sys.path.insert(0, str(BACKEND_ROOT / "tools"))
import audit_source_guards as audit  # noqa: E402

PROJECT = BACKEND_ROOT.parent


# ================================================================ 一、window()


def test_a_landmark_that_appears_twice_is_refused():
    """**不唯一的定位串等于没有定位串。**

    `index()` 挑第一个,而「第一个」是碰运气 —— 14-13 那条就是被注释里的
    `),` 抢先命中的。这条口径和 `audit_anchors.py` 对变异锚点的要求同源。
    """
    err = expect_raises(AssertionError, window, "aXbXc", "X", "Y")
    assert "出现 2 次" in str(err)


def test_an_end_marker_that_is_missing_or_repeated_is_refused():
    """终点 0 次会一路切到文件尾,多次会提前截断 —— 两种都让反向断言失去意义。"""
    assert "0 次" in str(expect_raises(AssertionError, window, "aXbc", "X", "Y"))
    assert "2 次" in str(expect_raises(AssertionError, window, "aXbYcY", "X", "Y"))


def test_a_window_with_nothing_after_the_landmark_is_refused():
    """起点标记之后什么都没有的窗口,让任何 `not in` 平凡为真。

    注意判的是**标记之后**,不是整段空不空:窗口把标记本身包在里面,
    所以「整段为空」永远不成立 —— 那样写出来的是一条不可达的防御代码
    (§3.29 第一节,上一批刚踩过)。
    """
    assert "什么都没有" in str(expect_raises(AssertionError, window, "aXYc", "X", "Y"))


def test_a_window_without_an_end_runs_to_the_end_of_the_file():
    """不传终点就切到文件末尾。那是**安全方向**(窗口只会更宽),所以不检查。"""
    assert window("aXbc", "X") == "Xbc"


def test_the_window_includes_its_landmark():
    assert window("aXbYc", "X", "Y") == "Xb"


# ================================================================ 二、braced_block()


def test_a_braced_block_survives_nesting():
    """**这是按「第一个 `}`」切所缺的那条性质。**

    nginx 的 location 里出现一个 `if` 或 `map` 就多一层括号,而切窄之后
    反向断言会静默变真 —— 那正是这一批要禁的形状。
    """
    text = (
        "head\n"
        "location = /x {\n"
        "  if (a) {\n"
        "    add_header Z;\n"
        "  }\n"
        "  keep_alive on;\n"
        "}\n"
        "tail {\n}\n"
    )
    block = braced_block(text, "location = /x {")
    # **断言要挑那条会分叉的**:`add_header` 在嵌套块**里面**,按第一个 `}`
    # 切也照样包得住它 —— 拿它当判据的第一版守卫,对着「退回第一个 `}`」
    # 那条变异是绿的。会分叉的是嵌套块**之后**的那一行。
    # (batch14-9 的 H1 同型:构造了一对输入,不等于构造了一对会分叉的输入)
    assert "keep_alive" in block, "嵌套那一层之后的内容被切掉了"
    assert "add_header" in block
    assert "tail" not in block, "切过头了,吃进了下一个块"


def test_a_braced_block_refuses_an_ambiguous_start():
    assert "出现 2 次" in str(
        expect_raises(AssertionError, braced_block, "a {\n}\na {\n}\n", "a {")
    )


def test_a_braced_block_refuses_an_unbalanced_one():
    assert "配平" in str(expect_raises(AssertionError, braced_block, "a {\n b;\n", "a {"))


def test_only_line_is_immune_to_the_comment_that_fooled_window():
    """**行首锚定天生挡得住注释,而子串计数挡不住。**

    Makefile 里 `check-offline: test-pure` 出现两次,第二处是
    `#     check-offline: test-pure lint arch-check ...` 一句引用旧写法的注释。
    `window()` 数子串,于是被它挡回来(挡对了,但没有出路);
    `only_line()` 从**行首**(允许前导空白)起匹配,`#` 开头的那行根本不算命中。

    写本批那条登记守卫时连着撞了两次才想明白这件事 —— 两次都不是
    `window()` 太严,是**行**这种范围本来就该有自己的工具。
    """
    text = "#     build: a b lint\nbuild: a b c\n"
    assert only_line(text, "build: a b") == "build: a b c", "注释那行被算成命中了"
    assert only_line(text, "build: a") == "build: a b c"


def test_only_line_refuses_a_genuinely_ambiguous_prefix():
    """真有两行同前缀时必须拒绝 —— 那时 `[0]` 挑的是哪一行全看运气。"""
    two = "build: a b\n  build: a c\n"
    assert "2 行" in str(expect_raises(AssertionError, only_line, two, "build: a"))
    assert "0 行" in str(expect_raises(AssertionError, only_line, two, "nope:"))


# ================================================================ 三、审计判定


_INDEXED = '''
def test_x():
    src = _src("a.py")
    body = src[src.index("def f(") : src.index("def g(")]
    assert "bad" not in body
'''

_FIXED = '''
def test_x():
    src = _src("a.py")
    body = src[src.index("def f(") :][:200]
    assert "bad" not in body
'''

_OFFSET = '''
def test_x():
    src = _src("a.py")
    i = src.index("call(")
    body = src[i : i + 400]
    assert "bad" not in body
'''

_POSITIVE_ONLY = '''
def test_x():
    src = _src("a.py")
    body = src[src.index("def f(") : src.index("def g(")]
    assert "good" in body
'''

_CLOSED = '''
def test_x():
    src = _src("a.py")
    body = window(src, "def f(", "def g(")
    assert "bad" not in body
'''

_NO_SOURCE = '''
def test_x():
    body = some_list[a.index(1) : a.index(2)]
    assert "bad" not in body
'''


def test_an_index_located_window_feeding_a_negative_assertion_is_flagged():
    found = audit.violations_in(_INDEXED)
    assert len(found) == 1, found
    assert "按字符串定位切窗口" in found[0]


def test_a_fixed_width_window_is_flagged_in_both_of_its_shapes():
    """`[:200]` 与 `[i : i + 400]` 是同一件事的两种写法。

    两种都在这个仓库里真实出现过 —— 前者是 14-12 的 WIRED_MODULES 守卫,
    后者是 batch8 那条 admin 头检查。
    """
    for source in (_FIXED, _OFFSET):
        found = audit.violations_in(source)
        assert len(found) == 1, (source, found)
        assert "定长窗口" in found[0]


_PROPAGATED = '''
def test_x():
    src = _src("a.py")
    body = src[src.index("def f(") : src.index("def g(")]
    narrowed = body
    assert "bad" not in narrowed
'''


def test_a_window_stays_suspect_after_being_passed_to_another_name():
    """`narrowed = body` 之后它还是那段窗口。

    不跟踪传染的话,绕过这条门禁只要多写一行赋值 —— 而那正是被门禁挡住的人
    最先会试的那一下。
    """
    found = audit.violations_in(_PROPAGATED)
    assert len(found) == 1, found
    assert "narrowed" in found[0]


def test_a_positive_assertion_on_the_same_window_is_deliberately_not_flagged():
    """**这条不对称是刻意的,不是漏网。**

    正向断言碰上窄窗口会变假、会红、有人会看见;把它一起判红只会让 87 处
    正当用法陪绑,而规矩一旦开始误伤,下一步就是有人给它加白名单。
    """
    assert audit.violations_in(_POSITIVE_ONLY) == []


def test_a_window_built_by_the_helper_is_not_flagged():
    """收口之后就该干净 —— 否则这条门禁没有出路,只能靠白名单活着。"""
    assert audit.violations_in(_CLOSED) == []


def test_slicing_something_that_is_not_source_code_is_not_flagged():
    """判据的前提是「这个函数读了源码」。不读源码的切片(比如切一个列表)
    不在射程里 —— 否则这条门禁会开始管一批和它无关的事。"""
    assert audit.violations_in(_NO_SOURCE) == []


def test_the_judgement_can_see_the_readers_it_claims_to_see():
    """把「读源码」的判据打空,`violations_in` 会永远返回空清单而看起来一切
    正常 —— 与 batch14-8 的 X1 同型:**穷举守卫会平凡通过**。

    所以单独钉住:那份读取函数名单真的覆盖到了本仓在用的几种写法。
    """
    for reader in ("read_text", "_src", "_ts", "_source", "_read", "_code_only"):
        source = f'''
def test_x():
    src = {reader}("a.py")
    body = src[src.index("a") : src.index("b")]
    assert "bad" not in body
'''
        assert audit.violations_in(source), f"{reader} 这种读法没被认出来"


# ================================================================ 四、全树与登记


def test_the_tree_has_no_open_windows_left():
    """全树扫一遍。本批把这个数从 10 收到 0。"""
    problems: list[str] = []
    scanned = 0
    for path in sorted((BACKEND_ROOT / "tests" / "pure").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        scanned += audit.scanned_guards(source)
        problems.extend(audit.violations_in(source, path.name))
    assert not problems, "又有反向断言吃上切窄的窗口了:\n  " + "\n  ".join(problems)
    # 非空:扫描器塌成「什么都不扫」时上面那条会平凡通过
    assert scanned > 200, f"只扫到 {scanned} 个读源码的守卫 —— 扫描器塌了"


def test_the_new_gate_is_registered_in_all_three_places():
    """**硬规则 3**:加一条门禁 = 改三个地方(Makefile、ci.yml、
    `check_ci_runs_every_gate()` 的表)。少一处的表现各不相同,
    但都通向同一个结局 —— 门禁只写在文档里。

    ci.yml 那一处找的是**命令字面量**,不是 step 名字:名字可以随便写,
    命令不能。
    """
    makefile = (PROJECT / "Makefile").read_text(encoding="utf-8")
    assert "audit-guards:" in makefile, "Makefile 里没有这个目标"
    assert "audit_source_guards.py" in makefile
    # 地标要挑真的唯一的那一段:`check-offline:` 与 `check-offline: test-pure`
    # 在 Makefile 里**都出现两次** —— 第二处是一句引用旧写法的注释。
    # 写这条守卫的时候连着被 `window()` 挡回来两次,而挡回来的正是
    # 「注释抢先命中」这个形状本身(和 14-13 那条 `),` 一模一样)。
    assert "audit-guards" in only_line(
        makefile, "check-offline: test-pure verify-delivery", what="check-offline"
    ), "check-offline 不跑它 —— 本地跑门禁的人看不到这一条"

    ci = (PROJECT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "tools/audit_source_guards.py" in ci, "CI 里没有这条命令"

    verify = (BACKEND_ROOT / "tools" / "verify_delivery.py").read_text(encoding="utf-8")
    # `required = {` 在这个文件里出现两次(另一处是别的检查),
    # 所以地标要连上它自己那个函数名 —— 又一次「不唯一的定位串」
    table = window(
        verify,
        "def check_ci_runs_every_gate",
        "    missing = [",
        what="check_ci_runs_every_gate 的表",
    )
    assert "audit_source_guards.py" in table, (
        "门禁表里没登记 —— 于是 CI 哪天把这一步删掉,不会有任何东西发现"
    )
