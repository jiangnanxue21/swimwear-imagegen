"""A44 第五批:C-13 的 P0、B-07 的可执行修复,以及七条毛边。

    C-13   0027 downgrade 按第一个 `/` 切分,绕开了长度前缀 —— **会损坏数据**
    B-07   歧义旧 VARIANT owner 无可执行修复
    A-09   health query 从不失效,后端恢复后红色横幅不消失
    A-30   批准按钮禁用时 Tooltip 仍写"提交批准"
    A-34   saveBlob 同步 revokeObjectURL
    A-35   两份动作文案表并存
    A-36   兜底路由静默跳首页,没有 404
    A-37   `except IntegrityError` 把一切冲突塞进版本重试环
    A-42   Makefile 的 psql 写死用户名/库名
    A-43   nginx `/healthz` 用 add_header 设内容类型,并因此丢掉全部安全头
    R1-23  三处模块级 assert 在 `python -O` 下被剥离
    R1-28  共用口令含冒号被误拆成 `名字:口令`
    R1-37  prod overlay 没有覆盖弱默认口令

C-13 那条能在 Python 里把 SQL 的算式复算一遍,所以它跑的是真逻辑。
"""
from __future__ import annotations

import ast
import re

from tests.pure._helpers import BACKEND_ROOT, braced_block, window

APP = BACKEND_ROOT / "app"
MIGRATIONS = BACKEND_ROOT / "migrations" / "versions"
PROJECT = BACKEND_ROOT.parent
FRONTEND = PROJECT / "frontend" / "src"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _no_hash_comments(source: str) -> str:
    """去掉 `#` 注释再做字符串断言(YAML / Makefile)。

    第一版没有这一步,于是 R1-37 那条红在**我自己写的注释**上 ——
    注释里引用了 `${POSTGRES_PASSWORD:-imagegen}` 来说明"改之前是什么样"。
    这是同一个陷阱在这个仓库里的第四次出现,前三次分别在第三、四批。
    结论已经不用再推了:**任何字符串包含类断言,先剥注释。**
    """
    return re.sub(r"(?<!\$)#[^\n]*", "", source)


def _code_only(source: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", without_block)


# ==========================================================
# C-13:降级时按长度前缀切分(P0,会损坏数据)
# ==========================================================


def _sql_substring_offset(owner_id: str) -> int:
    """把迁移里那条算式在 Python 里复算一遍。

    `POSITION(':' IN x)` 与 `SPLIT_PART(x, ':', 1)` 都是 1-based / 取第一段,
    行为在 Python 里可以逐字对应 —— 所以这条测试测的是**算式本身**,
    不是"SQL 文本里有没有出现某个词"。
    """
    colon = owner_id.index(":") + 1  # POSITION 是 1-based
    length = int(owner_id.split(":", 1)[0])  # SPLIT_PART(..., 1)
    return colon + length + 2


def _sql_downgrade_bare(owner_id: str) -> str:
    return owner_id[_sql_substring_offset(owner_id) - 1 :]  # SUBSTRING 是 1-based


def test_the_downgrade_arithmetic_survives_a_slash_inside_the_spu():
    """C-13:SPU 含 `/` 时,按第一个斜杠切会切出一个**看起来合法的错值**。

    长度前缀存在的唯一理由就是这个:SPU 是自由文本,含斜杠不违法
    (`variant_owner_id()` 的注释里举的正是这个例子)。

    错值会直接写进 `owner_id`,降级完成后再 upgrade 也回不来 ——
    原值已经没了。这是 0027 里唯一一处会损坏数据的地方。
    """
    # 字面量而不是铸造函数:铸造在阶段 1 退役了(命名空间的存在理由是
    # 变体 id 取值为颜色名,而 owner_id 切 UUID 之后跨 SPU 撞不上)。
    # 这条守的是**存量行**能不能被 0027 的降级正确切开,所以期望值该是
    # 库里真正躺着的那串字节,不是某个函数当下的输出
    owner_id = "5:AB/CD/Red"

    # 改之前那条:SUBSTRING(owner_id FROM POSITION('/' IN owner_id) + 1)
    by_first_slash = owner_id[owner_id.index("/") + 1 :]
    assert by_first_slash == "CD/Red", "构造的用例没有触发那个 bug,断言无意义"

    assert _sql_downgrade_bare(owner_id) == "Red", (
        f"按长度前缀切也切错了:{_sql_downgrade_bare(owner_id)}"
    )


def test_the_downgrade_arithmetic_survives_a_colon_inside_the_spu():
    """SPU 含冒号同样不影响:取的是**第一个**冒号,正则已保证它是长度前缀。"""
    owner_id = "3:A:B/Red"
    assert _sql_downgrade_bare(owner_id) == "Red", f"切错了:{owner_id}"


def test_the_downgrade_agrees_with_the_python_parser_on_every_shape():
    """SQL 侧与 `split_variant_owner_id()` 必须给出同一个答案。

    Python 那一份一直是对的(它按长度前缀切),错的只有 SQL。这条把两者
    钉在一起:以后谁改了命名空间形式,两边会同时红,而不是只红一边。
    """
    from app.attributes import validation

    # 冻结字节。见上一条为什么不再从铸造函数取
    for owner_id, variant in [
        ("6:SW-001/black", "black"),
        ("5:AB/CD/Red", "Red"),
        ("3:A:B/Red", "Red"),
        ("4:SW-1/Red/Blue", "Red/Blue"),
        ("4:SW-1/Red~2", "Red~2"),
    ]:
        parsed = validation.split_variant_owner_id(owner_id)
        assert parsed is not None and parsed[1] == variant, (
            f"Python 侧对 {owner_id!r} 拆出来的不是 {variant!r}"
        )
        assert parsed is not None, f"Python 侧拆不出来:{owner_id}"
        assert _sql_downgrade_bare(owner_id) == parsed[1], (
            f"SQL 与 Python 对 {owner_id!r} 的结果不一致:"
            f"{_sql_downgrade_bare(owner_id)!r} vs {parsed[1]!r}"
        )


def test_the_migration_no_longer_splits_on_the_first_slash():
    """算式对了还不够 —— 迁移文件里那条 SQL 也得真的是它。"""
    source = _read(MIGRATIONS / "0027_product_variant_key.py")
    downgrade = source[source.index("def downgrade()") :]
    code = re.sub(r"^\s*#.*$", "", downgrade, flags=re.M)
    assert "POSITION('/' IN owner_id)" not in code, (
        "downgrade 又按第一个斜杠切了(C-13)"
    )
    assert "SPLIT_PART(owner_id, ':', 1)" in code, "downgrade 没有用长度前缀"


# ==========================================================
# B-07:可执行的修复路径
# ==========================================================


def test_there_is_an_executable_repair_path_for_bare_variant_owners():
    """B-07 的问题不是"有脏数据",是"**没有任何一条路可以走**"。

    `0027` 的 CTE 用当前 `primary_color` 算 bare,而 owner_id 是写入当时的
    颜色名 —— 改过名的行匹配不上,那一批的跨 SPU 串档根本没修,迁移却是绿的。
    补不回来(属性表没有 product_id),所以只能交给人:工具把证据摆出来,
    人指定归属。

    这条钉的是工具的**三条安全约束**,不是它跑得通(跑得通要真库)。
    """
    tool = BACKEND_ROOT / "tools" / "repair_variant_owners.py"
    assert tool.exists(), "B-07 仍然没有可执行的修复路径"
    tree = ast.parse(_read(tool))
    names = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }
    assert {"cmd_list", "cmd_assign"} <= names, "工具缺 list / assign 子命令"

    source = _read(tool)
    assert '"--apply"' in source, "没有 --apply —— 默认干跑才安全"
    assert "if not args.apply:" in source, "干跑分支不见了,会直接改数据"
    assert '"--actor"' in source and "required=True" in source, (
        "归属改写没有强制记操作者"
    )
    assert "audit.record(" in source, "改写不进审计"
    assert 'add_argument("--all"' not in source, (
        "不许有批量参数 —— 那会把这个工具变成 0027 那条 CTE 的手工版"
    )


# ==========================================================
# R1-23:模块级不变量在 `python -O` 下也要生效
# ==========================================================


def test_no_module_level_assert_guards_a_configuration_invariant():
    """`python -O` 会把模块级 assert 整条剥离 —— 而那正是生产。

    三处守卫(两处在 `workbench/batch.py`,一处在 `workflows/dispatch_policy.py`)
    原来都是 assert,也就是三条**只在开发机上生效**的守卫。

    顺带记一件事:`test_batch_lease.py` 里原本有一条门禁**要求**它写成
    assert —— 一条门禁在钉住一个坏形状。改的时候那条会先红,而红的时候
    最省事的做法是把代码改回去。所以这类"钉形状"的断言,钉的是哪个形状
    本身要想清楚。
    """
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        for node in ast.parse(_read(path)).body:
            if isinstance(node, ast.Assert):
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert not offenders, (
        "模块级 assert 会被 `python -O` 剥离,守卫等于不存在 -> " + "; ".join(offenders)
    )


def test_ci_smokes_the_invariants_under_optimised_mode():
    """改成 `if ... raise` 之后,-O 冒烟才有意义 —— 它真的会执行那几个判断。"""
    ci = _read(PROJECT / ".github" / "workflows" / "ci.yml")
    assert "python -O -c" in ci, "CI 没有 -O 冒烟"
    for module in ("app.workbench.batch", "app.workflows.dispatch_policy"):
        assert module in ci, f"-O 冒烟没有覆盖 {module}"


# ==========================================================
# A-37:只有版本冲突才重试
# ==========================================================


def test_only_a_version_collision_goes_back_into_the_retry_loop():
    """别的完整性冲突重试一万次也不会成功。

    最后它以「图片集版本连续 N 次冲突,请稍后重试」报出来,把排查方向指向
    并发,而真实原因是入参(比如同素材同变体重复)。方向错的错误消息,
    比没有消息更贵。
    """
    source = _read(APP / "listings" / "image_set_service.py")
    assert "_is_version_collision(exc)" in source, "又接住所有 IntegrityError 了(A-37)"
    # 钉 AST 而不是数字符:`except` 之后 400 个字符里有没有 `raise` 这种断言,
    # 注释里出现一次就能骗过去
    tree = ast.parse(source)
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and node.type is not None
        and "IntegrityError" in ast.dump(node.type)
    ]
    assert handlers, "找不到 IntegrityError 的处理分支"
    guarded = [
        h
        for h in handlers
        if any(
            isinstance(n, ast.If)
            and "_is_version_collision" in ast.dump(n.test)
            and any(isinstance(r, ast.Raise) and r.exc is None for r in ast.walk(n))
            for n in h.body
        )
    ]
    assert guarded, "不是版本冲突的那一支没有原样重新抛出(A-37)"

    tree = ast.parse(source)
    known = {
        element.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "_VERSION_COLLISION_CONSTRAINTS"
        for element in ast.walk(node.value)
        if isinstance(element, ast.Constant)
    }
    assert known, "找不到版本冲突的约束名清单"
    model = _read(APP / "models" / "listing_image.py")
    for name in known:
        assert name in model, (
            f"{name} 不在模型的约束声明里 —— 判据指向一个不存在的约束名"
        )


# ==========================================================
# A-09 / A-30 / A-34 / A-35 / A-36:前端五条
# ==========================================================


def test_the_health_banner_recovers_on_its_own():
    """A-09:后端恢复之后红色横幅必须自己消失。

    全仓没有任何地方 invalidate `['health-probe']`(注释原来声称"第一次成功
    的业务请求会让红页消失",那是假的)。所以失败之后必须自己轮询。
    """
    src = _code_only(_read(FRONTEND / "hooks" / "useIdentity.ts"))
    # 只看 health 那一段:`auth-probe` 关掉聚焦重试是对的(它由存口令那一步
    # 显式 invalidate),一刀切会把一个正确的写法一起判红
    health = window(
        src, "const health = useQuery", "const probe = useQuery", what="health 探针那一段"
    )
    assert "refetchOnWindowFocus: false" not in health, (
        "健康探针仍然完全不在窗口聚焦时重试(A-09)"
    )
    assert "refetchInterval:" in health, "失败之后没有任何自愈路径"
    assert "backendWasDown" in health, "轮询没有和失败状态挂钩 —— 健康时也会打噪音"


def test_the_approve_tooltip_covers_every_disabled_reason():
    """A-30:禁用按钮不许配一个可点的说明。

    Tooltip 的分支必须覆盖 `canApprove` 的每一个条件;缺哪一个,那个状态下
    就会显示"提交批准"而按钮是灰的。
    """
    tab = _code_only(_read(FRONTEND / "components" / "workbench" / "ImageSetTab.tsx"))
    tooltip = tab[tab.index("title={") : tab.index("批准图片集")]
    for branch in ("!imageSetId", "detailBroken", "current.isLoading", "busy", "dirty"):
        assert branch in tooltip, f"Tooltip 没有解释 {branch} 这一支(A-30)"


def test_the_blob_url_is_not_revoked_in_the_same_tick():
    """A-34:`click()` 只是把下载排进队列,同一拍里 revoke 是竞态。

    失败时**没有任何报错** —— 运营只会觉得"这个按钮有时候不好使"。

    ## 这条断言自己改过一次(A45-#4)

    第一版在 `ExportTab.tsx` 里找 `function saveBlob` —— 而那是一个**复制件**,
    正本在 `api/batch.ts`。也就是说这条门禁**钉住的是那个复制件的存在**:
    删掉复制件、改成 import 正本(正确的修法)会让它当场变红。

    本仓第四次撞见"门禁钉的是当时的样子而不是不变量"。现在钉两件事:
    全仓只有一个定义,且那一个不同步 revoke。
    """
    definitions = [
        path
        for path in FRONTEND.rglob("*.ts*")
        if "function saveBlob(" in _code_only(_read(path))
    ]
    assert len(definitions) == 1, (
        f"saveBlob 有 {len(definitions)} 处定义 -> {[p.name for p in definitions]};"
        "两份实现会各自演化,而没有任何东西会发现它们不一致(A45-#4)"
    )
    canonical = definitions[0]
    assert canonical.name == "batch.ts", f"saveBlob 的正本挪到了 {canonical.name}"

    src = _code_only(_read(canonical))
    save_blob = src[src.index("export function saveBlob") :]
    save_blob = save_blob[: save_blob.index("\n}\n")]
    assert "setTimeout" in save_blob, "又在同一拍里 revokeObjectURL 了(A-34)"
    assert "anchor.click()" in save_blob or "link.click()" in save_blob
    # A45-#22:0ms 只保证"下一个宏任务",几十 MB 的 zip 在 Safari 上仍偏紧
    delay = save_blob.split("revokeObjectURL(url),")[1].split(")")[0].strip()
    assert delay not in {"0", "0_000"}, (
        f"revoke 延后 {delay}ms 太紧 —— 大文件在 Safari 上会来不及读(A45-#22)"
    )


def test_action_labels_have_exactly_one_source():
    """A-35:同一个动作不许在两处各写一份文案。

    原来导出页自带 `{ export: '导出' }`,而共享表写的是「导出上架文件」——
    同一个动作在两页显示成两个词,谁也不会发现它们本该一致。
    """
    src = _code_only(_read(FRONTEND / "components" / "workbench" / "ExportTab.tsx"))
    assert "AUDIT_ACTION_LABEL" in src, "导出页没有用共享的那份文案表"

    colours = src[src.index("const ACTION_COLOR") :]
    colours = colours[: colours.index("}")]
    keys = set(re.findall(r"^\s*(\w+):", colours, flags=re.M))
    shared = _read(FRONTEND / "api" / "batch.ts")
    table = shared[shared.index("AUDIT_ACTION_LABEL") :]
    table = table[: table.index("}")]
    known = set(re.findall(r"^\s*(\w+):", table, flags=re.M))
    missing = sorted(keys - known)
    assert not missing, f"颜色表里的 {missing} 在共享文案表里没有对应项 —— 会显示成原始动作码"


def test_a_bad_url_lands_on_a_404_not_on_the_home_page():
    """A-36:静默跳首页会把坏链接和部署 base-path 问题一起藏起来。

    base-path 配错时整站每个深链都跳首页,而首页本身是好的 —— 冒烟一路绿。
    """
    app = _code_only(_read(FRONTEND / "App.tsx"))
    assert 'path="*" element={<NotFoundPage' in app, "兜底路由又跳首页了(A-36)"
    page = FRONTEND / "pages" / "NotFoundPage.tsx"
    assert page.exists(), "没有 404 页面"
    assert "location.pathname" in _read(page), (
        "404 页没有把访问的路径打出来 —— 排查这类问题第一句话就是「你访问的是哪个地址」"
    )


# ==========================================================
# A-42 / A-43 / R1-28 / R1-37:部署侧四条
# ==========================================================


def test_the_psql_target_follows_the_configured_credentials():
    """A-42:改过库名/用户名的部署,`make psql` 不该连不上。"""
    makefile = _read(PROJECT / "Makefile")
    line = next(
        line
        for line in makefile.splitlines()
        if "psql -U" in line and "docker compose" in line
    )
    assert "POSTGRES_USER" in line and "POSTGRES_DB" in line, (
        f"make psql 又写死了用户名/库名:{line.strip()}"
    )


def test_the_health_location_keeps_the_security_headers():
    """A-43:location 里一旦出现 `add_header`,server 级的安全头全部不再继承。

    探活本身不敏感,但"某个 location 悄悄丢掉全部安全头"这个形状会被复制到
    下一个 location 上,而下一个可能不是探活。
    """
    nginx = _read(PROJECT / "frontend" / "nginx.conf")
    block = braced_block(nginx, "location = /healthz", what="/healthz 那个 location")
    assert "add_header" not in block, (
        "/healthz 里又出现 add_header —— server 级的四条安全头会全部失效(A-43)"
    )
    assert "default_type" in block, "设内容类型的指令是 default_type,不是 add_header"


def test_a_shared_password_with_a_symbol_prefix_is_not_split():
    """R1-28:共用口令含冒号时不该被当成 `名字:口令`。

    完全消歧做不到(`alice:tok` 两种读法都合法),所以判据是"前缀长得像不像
    名字"。这条测的是两个方向都还在。
    """
    # `app.api.deps` 会拉起 fastapi,而本环境没有。把这个纯函数连同它的
    # 判据正则单独取出来执行 —— 跑的仍然是仓库里那份源码,不是复刻品。
    source = _read(APP / "api" / "deps.py")
    tree = ast.parse(source)
    wanted = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name == "parse_operator_tokens")
        or (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "_NAME_PATTERN"
        )
    ]
    assert len(wanted) == 2, (
        "取不到 parse_operator_tokens + `_NAME_PATTERN` 这一对。名字判据不存在的话,"
        "含冒号的共用口令又会被拆成 `名字:口令`(R1-28)"
    )
    namespace: dict = {"re": re, "ROLE_OPERATOR": "operator"}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), "<deps>", "exec"), namespace)
    parse_operator_tokens = namespace["parse_operator_tokens"]
    ROLE_OPERATOR = "operator"

    assert parse_operator_tokens("p@ss:w0rd") == [(ROLE_OPERATOR, "p@ss:w0rd")], (
        "含符号的共用口令又被拆开了(R1-28)"
    )
    assert parse_operator_tokens("alice:tok-a,bob:tok-b") == [
        ("alice", "tok-a"),
        ("bob", "tok-b"),
    ], "`名字:口令` 这条正路被误伤了"
    assert parse_operator_tokens("s3cret") == [(ROLE_OPERATOR, "s3cret")]
    # 名字合法但没有口令 -> 整段按共用口令,而不是丢掉
    assert parse_operator_tokens("alice:") == [(ROLE_OPERATOR, "alice:")]


def test_production_cannot_inherit_the_development_password():
    """R1-37:prod overlay 必须覆盖弱默认口令,而且没配就起不来。

    启动失败是个好错误 —— 它发生在部署那一刻、由部署的人看到,
    而不是三个月后由别人发现。
    """
    prod = _no_hash_comments(_read(PROJECT / "docker-compose.prod.yml"))
    assert "POSTGRES_PASSWORD" in prod, "prod overlay 仍然没覆盖数据库口令(R1-37)"
    assert ":?" in prod, (
        "用的是 `:-` 默认值而不是 `:?` 必填 —— 没配照样能起来,起来的是弱口令"
    )
    assert ":-imagegen}" not in prod, "prod overlay 里出现了开发默认口令"
