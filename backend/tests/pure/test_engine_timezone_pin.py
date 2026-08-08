"""每一个 `create_engine(...)` 都必须钉死会话时区 UTC。

## 这条守卫为什么存在

`db/session.py` 的生产引擎写着 `connect_args={"options": "-c timezone=utc"}`,
并在注释里说明「这不是一个偏好设置,是一个正确性前提」:全部时间列是
`timestamptz`,而写进去的是 naive UTC(`core/clock.utc_now()`),naive 值发给
timestamptz 时 PostgreSQL 按**会话的 TimeZone 参数**解释它。

那句话当时只对生产成立。`tests/conftest.py` 与 `tests/test_migrations.py`
三处 `create_engine` 是裸的 —— **测试是全仓唯一不满足这个"正确性前提"的地方**。

## 它咬人的形状很窄,所以更难查

两侧都是 Python naive 时,非 UTC 会话时区让两边**等量偏移**,比较依然成立。
所以绝大多数真库用例在一台 `timezone=Asia/Shanghai` 的库上照样全绿 ——
包括整整 10/10 的 `test_batch_lease_concurrency_db.py`。

只有「**DB 侧 `now()` 摆现场 + Python naive 判定**」的混用形态会翻::

    UPDATE ... SET updated_at = now() - interval '1 hour'    真实 instant  T-1h
    cutoff = utc_now() - 60s(naive,被按 +08 解释)          真实 instant  T-8h-60s

`updated_at < cutoff` 恒假,`reap_stalled()` 一条都收不回来。而这**不报错**:
读到的现象是"回收器不回收",指向的是业务代码,修的却该是连接参数。
A45-batch16 后一轮的 3 条真库失败正是这个形状,当时一度被定性成 reaper 的
业务缺陷。

## 为什么这道断言在纯测试里

它读源码,不连库 —— 于是**在没有数据库的机器上也拦得住**。而钉子被删掉这件事
最可能发生在没有库的那台机器上(改完提交,CI 有库但用例仍然全绿,因为 CI 的
postgres 镜像默认就是 UTC)。等到有人在非 UTC 的库上跑,现场已经是一堆
被诬告的业务代码。

真库那一侧另有一道:`conftest.engine` 建库时 `SHOW TimeZone` 当场自证 ——
参数传了不等于生效,那道管"生效没有",这道管"传了没有"。
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT

#: 钉子的内容。三处引擎必须写同一句,不许各写各的。
PIN_OPTION = "-c timezone=utc"

#: 全仓允许出现 `create_engine(...)` 的文件。**新增一处要连同理由加进来。**
#: 名单本身也是断言的一部分:漏掉的文件会在下面第二条里被点名。
ENGINE_SITES = (
    "app/db/session.py",
    "tests/conftest.py",
    "tests/test_migrations.py",
)


def _engine_builder_names(tree: ast.AST) -> set[str]:
    """这份文件里,哪些名字绑着 `sqlalchemy.create_engine`。

    只认字面名 `create_engine` 是不够的 —— `from sqlalchemy import
    create_engine as _ce` 会静默绕过整道守卫(变异 T6 撞破)。守卫的缺口和
    它守的缺陷是同一类:看上去覆盖到了,实际有一条没人走过的旁路。
    """
    names = {"create_engine"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "create_engine" and alias.asname:
                    names.add(alias.asname)
    return names


def _create_engine_calls(rel: str) -> list[ast.Call]:
    tree = ast.parse((BACKEND_ROOT / rel).read_text(encoding="utf-8"))
    builders = _engine_builder_names(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in builders)
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_engine"
            )
        )
    ]


#: 测试侧引用钉子时用的名字。写成常量而不是在断言里散着,理由和它守的那件事
#: 是同一条:字面量抄两遍,迟早只改一遍。
PIN_ALIAS = "TEST_CONNECT_ARGS"


def _pins_timezone(call: ast.Call, source: str) -> bool:
    """这次调用是否带上了时区钉子。

    两种写法都算数:直接写字面量,或者引用 `TEST_CONNECT_ARGS`。引用时值不在
    这一行,由 `test_the_test_engines_share_one_definition_of_the_pin` 保证
    那个名字确实绑着钉子 —— 两条断言合起来才封闭,单独任何一条都不封闭。
    """
    for kw in call.keywords:
        if kw.arg != "connect_args":
            continue
        segment = ast.get_source_segment(source, kw.value) or ""
        return PIN_OPTION in segment or PIN_ALIAS in segment
    return False


def test_every_engine_pins_the_session_timezone():
    """三处引擎的每一次 `create_engine` 都带着时区钉子。"""
    missing = []
    for rel in ENGINE_SITES:
        source = (BACKEND_ROOT / rel).read_text(encoding="utf-8")
        for call in _create_engine_calls(rel):
            if not _pins_timezone(call, source):
                missing.append(f"{rel}:{call.lineno}")
    assert not missing, (
        f"这些 create_engine 没有钉死会话时区:{missing}\n"
        f"补上 connect_args={{'options': '{PIN_OPTION}'}}(测试侧用 "
        "conftest.TEST_CONNECT_ARGS)。\n"
        "少这一行的后果不是报错,是「DB 侧 now() + Python naive 判定」的用例"
        "在非 UTC 库上无声判错 —— 退避、租约、卡死回收全在其中。"
    )


def test_no_engine_is_built_outside_the_known_sites():
    """名单之外不许再冒出 `create_engine`。

    没有这条,上面那条断言的是一份会过期的名单:新写的第四处引擎不在名单里,
    于是它漏不漏钉子,没有任何地方问得到。
    """
    found = set()
    for path in BACKEND_ROOT.rglob("*.py"):
        if any(part in {"__pycache__", ".venv", "node_modules"} for part in path.parts):
            continue
        rel = path.relative_to(BACKEND_ROOT).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "create_engine" not in source:
            continue
        if _create_engine_calls(rel):
            found.add(rel)
    assert found <= set(ENGINE_SITES), (
        f"这些文件新建了引擎却不在 ENGINE_SITES 名单上:{sorted(found - set(ENGINE_SITES))}\n"
        "把它加进名单(顺带就会被上面那条检查时区钉子),"
        "或者让它复用已有的引擎。"
    )


def test_the_test_engines_share_one_definition_of_the_pin():
    """测试侧的钉子只有一份定义,不许各写各的。

    两份字面量的下场,`test_a45_batch14_stage3_extractor.py` 刚演过一次:
    四处散落的入口名在生产改名后集体过期,而跳过让它们躺了一整批。
    """
    conftest = (BACKEND_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert conftest.count(PIN_OPTION) == 1, (
        "tests/conftest.py 里出现了不止一处时区钉子字面量 —— "
        "留一份 TEST_CONNECT_ARGS,其余引用它"
    )
    assert PIN_ALIAS in conftest, f"conftest 没有导出 {PIN_ALIAS}"

    migrations = (BACKEND_ROOT / "tests" / "test_migrations.py").read_text(
        encoding="utf-8"
    )
    assert PIN_OPTION not in migrations, (
        "tests/test_migrations.py 自己抄了一份钉子字面量 —— "
        "改成 from tests.conftest import TEST_CONNECT_ARGS"
    )
    assert "TEST_CONNECT_ARGS" in migrations, (
        "tests/test_migrations.py 的引擎没有引用 TEST_CONNECT_ARGS"
    )


def test_the_build_fixture_verifies_the_pin_took_effect():
    """`conftest.engine` 必须当场问一次库,而不是信"参数传了就生效了"。

    传错键名、驱动不认、被谁覆盖掉,都会让钉子静默失效 —— 那正是本文件
    其余几条断言看不见的一层。
    """
    conftest = (BACKEND_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(conftest)
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "engine"
        ),
        None,
    )
    assert fn is not None, "conftest 里没有 engine 夹具了?"
    body = ast.get_source_segment(conftest, fn) or ""
    assert "SHOW TimeZone" in body, (
        "engine 夹具不再自证时区钉子生效 —— 建库是每次真库运行的第一步,"
        "失败该发生在这里,读到的该是原因,而不是十几条「回收器没回收」"
    )
