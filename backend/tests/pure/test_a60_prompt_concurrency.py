"""a60 守卫:S4 的乐观锁(FE-309 / AC-35)、回滚理由(FE-310)、真 diff(FE-306/307)。

## 这一批为什么现在才做

「两个人同时改同一份提示词」在只有一个入口时几乎不会发生。a58 给提示词加了列表页,
八处提示词都摆在那里,它才从理论风险变成常态 —— **S4 依赖 S3 不只是排期,是这条
风险自己变大了。**

## 三个动作,不是只有保存

保存 / 切版本 / 恢复默认在这一点上是同一类:A 在切 v3、B 同时在切 v5,一样静默覆盖。
PRD v1.0 只给保存加,v2.0 订正为三个都加(AC-35)。这里逐个钉。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
FRONTEND = BACKEND.parent / "frontend" / "src"


def _strip_comments(source: str) -> str:
    """去掉 `/* */` 与 `//` 注释。误伤方向是多剥 —— 漏报好过假红(§3.37)。"""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//[^\n]*", "", source)


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


def _app(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


# ============================================================ 乐观锁


def test_all_three_write_paths_take_an_expected_version():
    """AC-35。少一个,那一个动作就是静默覆盖的入口。"""
    source = _app("services/prompt_service.py")
    tree = ast.parse(source)
    wanted = {"save_version", "activate_version", "reset_to_default"}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name not in wanted:
            continue
        names = {a.arg for a in fn.args.kwonlyargs} | {a.arg for a in fn.args.args}
        assert "expected_version" in names, f"{fn.name} 没有乐观锁入参"
        body = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))
        assert "_assert_expected_version" in body, f"{fn.name} 收了参数但没有检查它"
        wanted.discard(fn.name)
    assert not wanted, f"这几个函数不见了:{sorted(wanted)}"


def test_the_check_happens_after_the_lock_not_before():
    """拿锁之前读到的"当前版本"随时会变。

    检查过了照样能被别人插进来 —— 那种检查只是让人以为有防线,而它比没有
    防线更贵:有防线的地方没人会再去想并发。
    """
    tree = ast.parse(_app("services/prompt_service.py"))
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        body = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))
        if "_assert_expected_version" not in body:
            continue
        assert "_lock_key" in body, f"{fn.name} 检查了版本却没拿锁"
        assert body.index("_lock_key") < body.index("_assert_expected_version"), (
            f"{fn.name} 在拿锁之前检查版本 —— 检查完仍然可以被插进来"
        )


def test_the_default_prompt_is_expressible_as_a_version():
    """内置默认生效时服务端版本是 None,调用方看到的也该是 None。

    用 0 之类的哨兵值表示"没有版本",会让「回到默认」和「第 0 版」长得一样,
    而这两者的下一步完全不同。
    """
    tree = ast.parse(_app("services/prompt_service.py"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_assert_expected_version"
    )
    body = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))
    assert "actual = row.version if row is not None else None" in body
    # `expected is None` 是"不参与检查",不是"期望内置默认" —— 两者混用的话,
    # 一个不传参的脚本会被当成"我以为现在是内置默认"
    assert "if expected is None" in body


def test_the_conflict_answer_says_who_and_what_not_just_409():
    """运营此刻要判断的是"我该放弃还是该合并",而那要知道对方是谁、改成了第几版。

    少了这三样,唯一可行的动作是刷新重来一遍 —— 他刚写的那段就此没了,
    和静默覆盖只差一个提示框。
    """
    source = _app("api/prompts.py")
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_conflict"
    )
    body = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))
    for field in ("expected_version", "actual_version", "updated_by"):
        assert field in body, f"409 的响应体里没有 {field}"
    assert "status_code=409" in body
    # `actual` 为 None 要单独说,否则「现在是第 None 版」会原样出现在界面上
    assert "if exc.actual is None" in body


def test_every_write_endpoint_catches_the_conflict():
    """服务层抛了而接口层不接,运营看到的是 500。"""
    tree = ast.parse(_app("api/prompts.py"))
    for name in ("save_prompt", "activate_prompt", "reset_prompt"):
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == name
        )
        body = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))
        assert "PromptConflict" in body, f"{name} 没有接住版本冲突"
        assert "_conflict(" in body, f"{name} 没有翻成 409"


def test_the_frontend_sends_what_it_saw_on_all_three_actions():
    source = _read("pages/PromptsPage.tsx")
    assert "const seenVersion = query.data ? query.data.version : undefined" in source, (
        "前端没有记住「我看到的是第几版」 —— 三个动作因此都是无条件覆盖"
    )
    for call in ("promptsApi.save(", "promptsApi.activate(", "promptsApi.reset("):
        at = source.index(call)
        window = source[at : at + 200]
        assert "seenVersion" in window, f"{call} 没有把看到的版本传下去"


def test_a_conflict_is_not_offered_a_retry_button():
    """409 不是"失败",重试正是这里最不该做的事:它会覆盖对方。

    `<ErrorNotice>` 只在给了 `onRetry` 时才画重试按钮,所以这里靠**不给**来表达。
    """
    source = _read("pages/PromptsPage.tsx")
    # 窗口收到**那一个元素内部**。±400 字符的窗口第一版就抓到了隔壁
    # `surfaces` 那个 ErrorNotice 的 onRetry —— a59 那条门禁讲的正是这件事,
    # 而我在写它的守卫时又犯了一次:窗口宽窄取决于被读的文件长什么样
    at = source.index("error={conflict}")
    start = source.rindex("<ErrorNotice", 0, at)
    end = source.index("/>", at)
    element = source[start:end]
    assert "onRetry" not in element, "冲突提示上出现了重试按钮"
    assert 'type="warning"' in element, "409 被画成了 error —— 它不是失败"


def test_the_conflict_keeps_the_users_draft():
    """页面必须说清"你写的没丢"。不说的话,运营的第一反应是重新写一遍。"""
    source = _read("pages/PromptsPage.tsx")
    assert "没有被覆盖" in source


# ============================================================ FE-310 回滚理由


def test_rollback_and_reset_both_take_a_reason():
    tree = ast.parse(_app("api/prompts.py"))
    for cls in ("PromptActivateIn", "PromptResetIn"):
        node = next(
            n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls
        )
        fields = {
            t.target.id for t in node.body if isinstance(t, ast.AnnAssign)
            and isinstance(t.target, ast.Name)
        }
        assert "note" in fields, f"{cls} 没有理由字段"
        assert "expected_version" in fields, f"{cls} 没有乐观锁字段"


def test_the_reason_reaches_the_audit_not_only_the_log():
    """日志是滚动清理的,而"三个月前谁把口径退回去了"要能查得到。"""
    tree = ast.parse(_app("api/prompts.py"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "activate_prompt"
    )
    body = ast.unparse(ast.Module(body=fn.body, type_ignores=[]))
    at = body.index("audit.record")
    assert "'note': body.note" in body[at:] or '"note": body.note' in body[at:], (
        "回滚的理由没有进审计 payload"
    )


def test_the_rollback_reason_never_touches_the_version_row():
    """**这一条是被一次真实的连带损伤换来的。**

    第一版把理由拼到被切到那一版的 `note` 上。两个理由因此撞在一起(那一版的
    note 说「当初为什么这么写」,回滚的理由说「后来为什么退回来」),更要紧的是:
    `row.note = ...` 这句赋值让 `audit_column_writers.py` 按**列名**把
    `ListingImageSet.note` 也算成有写入点(那个审计刻意"多认一次"),
    它的欠账条目当场失效 —— **一次改动顺手抹掉了另一张表上的一笔账。**
    """
    # 按 AST 判赋值,不判源码子串 —— 上面那段 docstring 里正当地含着
    # `row.note = ...` 这串字(它讲的就是为什么不这么写)。子串断言会把自己的
    # 墓志铭当成尸体,`test_a56_prompt_registry.py` 里那条守卫记过同一个坑
    tree = ast.parse(_app("services/prompt_service.py"))
    offenders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Attribute) and target.attr == "note"
    ]
    assert not offenders, (
        "回滚理由又写回版本行了 —— 它会把 ListingImageSet.note 的欠账一起抹掉"
    )


def test_the_two_events_are_literals_so_the_registry_can_see_them():
    """`test_a53_log_console.py` 按 AST 找 `"event": "..."`,三元表达式两边它一个都看不见。

    第一版正是写成三元的,于是「登记了没人写」当场变红 —— 而变红的是**注册表那侧**,
    读起来像是登记多了,实际是调用点藏起来了。
    """
    from app.core.log_events import EVENTS

    for event in ("settings.prompt_save_conflict", "settings.prompt_rolled_back"):
        assert event in EVENTS
    source = _app("services/prompt_service.py")
    assert '"event": "settings.prompt_rolled_back"' in source
    assert '"event": "settings.prompt_activated"' in source
    assert "if note else" not in source, "事件码又写回三元了"
    # **两个字面量都在,不代表两条分支都走得到。** 把 `if note and note.strip():`
    # 改成 `if False:` 之后,字面量原样在、注册表也不红,而回滚事件永远不会产出 ——
    # 而"登记了没人写"那条守卫看的是有没有调用点,不是调用点到不到得了。
    # 所以这里判**那个分支的条件确实读了 note**
    tree = ast.parse(source)
    branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "settings.prompt_rolled_back"
        in ast.unparse(ast.Module(body=node.body, type_ignores=[]))
    ]
    assert branches, "找不到产出回滚事件的分支"
    for node in branches:
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        assert "note" in names, "回滚事件的分支条件不读 note —— 它可能永远走不到"


# ============================================================ FE-306 / 307 对比


def test_the_diff_is_an_actual_diff():
    """`diffOpen` 这个状态名从第一版起就承认这里该有对比,而里面只有一个只读 textarea。"""
    from app.core.clock import utc_now  # noqa: F401 —— 仅确保 app 可导入

    # a61 把判定搬进了 `utils/textDiff.ts` —— 留在 .tsx 里的函数
    # `node --test` 一行都跑不了(剥类型不做 JSX 变换)
    source = _read("utils/textDiff.ts")
    assert "function diffLines" in source
    # 判**定义**在不在,不判名字出现过 —— 只把定义改个名的话,调用点那串字
    # 照样在,守卫照样绿,而那个函数已经不存在了(变异 M13 就是这么造的)
    assert "function lcsTable" in source, "没有真的求最长公共子序列,只是并排显示两段文本"
    body = source[source.index("export function diffLines") :]
    assert "lcsTable(a, b)" in body, "diffLines 没有用那张表"
    page = _read("pages/PromptsPage.tsx")
    assert "<TextDiff" in page, "内置默认那个 Modal 里仍然没有对比"
    # **否定式断言先剥注释**(a62 的门禁扩到了这一档)。它要求那串字不存在,
    # 而一句解释"这里原来是个只读 textarea"的注释必然含着它 —— 于是断言被
    # 自己的墓志铭判红。肯定式没有这个问题:注释里多一处不影响结论
    assert "readOnly\n          autoSize={{ minRows: 16" not in _strip_comments(page), (
        "旧的只读 textarea 还在"
    )


def test_the_diff_compares_against_what_is_being_edited():
    """运营点开它想知道的是"我现在写的这段和默认差在哪",不是"上次保存的和默认差在哪"。"""
    page = _read("pages/PromptsPage.tsx")
    at = page.index("<TextDiff")
    window = page[at : at + 300]
    assert "right={content}" in window
    assert "left={query.data?.default_content" in window


def test_the_builtin_default_has_a_row_of_its_own():
    """内置默认在版本表里原本没有位置,而"当前生效的是它"正是版本表最该回答的事。"""
    page = _read("pages/PromptsPage.tsx")
    at = page.index('<Card title="版本历史"')
    window = page[at : at + 900]
    assert "内置默认" in window, "版本表顶部没有钉那一行"
    assert "生效中" in window


def test_the_diff_only_uses_colours_the_palette_really_has():
    """`brandVars` 是 `fromEntries` 出来的 Record —— 取一个不存在的键得到 undefined,

    而 CSS 收到 undefined 只是不上色:**颜色错了不会有任何征兆**。
    第一版写了 `successSoft` / `dangerSoft`,两个都不在 `lightTokens` 里。
    """
    theme = _read("theme.ts")
    block = theme[theme.index("const lightTokens") :]
    block = block[: block.index("\n}")]
    tokens = set(re.findall(r"^\s{2}([a-zA-Z]+):", block, re.M))
    used = set(re.findall(r"brandVars\.(\w+)", _read("components/TextDiff.tsx")))
    missing = used - tokens
    assert not missing, f"用了调色板里没有的 token:{sorted(missing)}"
