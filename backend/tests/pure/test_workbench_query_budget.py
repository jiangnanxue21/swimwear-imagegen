"""工作台读路径的查询预算(任务 19 / BLOCK-20)。

`REVIEW.md` B.8 把「Workbench 存在 N+1」列为**未核实**断言,注明"需实测
SQL 计数"。真实计数要 PostgreSQL,这里做不到;这个文件退而求其次,
从 AST 走调用图数**会话调用点**,并按循环嵌套分层。

**它不是实测,是上界。** 数的是调用点不是行数,分支两边都算,动态派发不解析。
够用的理由是:要拦的那类退化(在按行循环里打一次库)在源码结构上就是可见的。

## 为什么不是断言一个总数

总数(当前 28)对无害重构太敏感:拆一个函数、多一个查询换少一次内存过滤,
都会踩红,而红的次数一多这条门禁就会被人加白名单绕过去。所以这里断言的是
**形状**:哪一类调用不许出现在哪个位置。

## 写这个工具时踩的坑,别再踩一遍

第一版把 `{a.id: a for a in session.scalars(q)}` 判成"循环里的查询"。
但那次查询是推导式的**取值源**,只求值一次 —— 它恰恰是批量取数的正确写法。
于是第一版会把**修复**报成缺陷,而把 `for item in items: session.get(...)`
这种真缺陷和它并列。`for` 的 `iter`、推导式第一个 generator 的 `iter`,
都在外层求值,必须按外层深度算。
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"

#: 会打一次数据库往返的会话方法
#:
#: `scalar`(单数)原来漏在外面。它和 `scalars` 一样是一次往返,而 A-07 的
#: `COUNT` 走的正是 `session.scalar()` —— 漏掉它,这个工具会把"循环里数一次
#: 总数"看成零成本。补上之后既有两条形状断言仍然是绿的(collect 的深度分布
#: 没变),所以这是纯粹的盲区修补,不是口径变更。
READ_METHODS = {"scalars", "scalar", "execute", "get", "query"}
#: 会写库的会话方法
WRITE_METHODS = {"flush", "commit", "add", "delete", "merge"}


def _load_modules() -> dict[str, ast.Module]:
    mods: dict[str, ast.Module] = {}
    for path in APP.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        name = ".".join(path.relative_to(APP).with_suffix("").parts)
        mods[name] = ast.parse(path.read_text(encoding="utf-8"))
    return mods


def _build_index(mods):
    """(模块, 函数名) -> 定义节点,以及 (模块, 本地名) -> 目标模块 的 import 表。"""
    funcs: dict[tuple[str, str], ast.AST] = {}
    aliases: dict[tuple[str, str], str] = {}
    for name, tree in mods.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault((name, node.name), node)
            elif isinstance(node, ast.ImportFrom) and node.module:
                base = (
                    node.module[4:] if node.module.startswith("app.") else node.module
                )
                for alias in node.names:
                    cand = f"{base}.{alias.name}"
                    aliases[(name, alias.asname or alias.name)] = (
                        cand if cand in mods else base
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app."):
                        local = alias.asname or alias.name.split(".")[-1]
                        aliases[(name, local)] = alias.name[4:]
    return funcs, aliases


class _CallGraphCounter:
    """从一个函数出发,记录每个会话调用点及其循环嵌套深度。"""

    def __init__(self, mods, funcs, aliases):
        self.mods, self.funcs, self.aliases = mods, funcs, aliases
        self.sites: list[tuple[int, str, str]] = []  # (深度, 路径, 方法名)

    def _resolve(self, mod: str, call: ast.Call):
        func = call.func
        if isinstance(func, ast.Name):
            if (mod, func.id) in self.funcs:
                return (mod, func.id)
            target = self.aliases.get((mod, func.id))
            if target and (target, func.id) in self.funcs:
                return (target, func.id)
            # `from app.x.y import z` 之后直接 `z()`:目标模块名以函数名结尾
            for candidate in self.mods:
                if (candidate, func.id) in self.funcs and candidate.endswith(
                    f".{func.id}"
                ):
                    return (candidate, func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            target = self.aliases.get((mod, func.value.id))
            if target and (target, func.attr) in self.funcs:
                return (target, func.attr)
        return None

    def run(self, mod: str, name: str) -> None:
        self._enter(mod, name, 0, frozenset(), name)

    def _enter(self, mod, name, depth, stack, path):
        key = (mod, name)
        if key in stack or key not in self.funcs:
            return
        self._visit(self.funcs[key], depth, mod, stack | {key}, path)

    def _visit(self, node, depth, mod, stack, path):
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "session"
                and func.attr in READ_METHODS | WRITE_METHODS
            ):
                self.sites.append((depth, path, func.attr))
            target = self._resolve(mod, node)
            if target:
                self._enter(
                    target[0], target[1], depth, stack, f"{path} > {target[1]}"
                )

        # 下面三块是这个工具的全部要害:什么东西在外层求值,什么在内层。
        if isinstance(node, (ast.For, ast.AsyncFor)):
            self._visit(node.iter, depth, mod, stack, path)  # 取值源:一次
            for stmt in node.body + node.orelse:
                self._visit(stmt, depth + 1, mod, stack, path)
            return
        if isinstance(node, ast.While):
            self._visit(node.test, depth, mod, stack, path)
            for stmt in node.body + node.orelse:
                self._visit(stmt, depth + 1, mod, stack, path)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for index, gen in enumerate(node.generators):
                # 第一个 generator 的 iter 在外层求值;第二个开始已经在循环里了
                self._visit(gen.iter, depth if index == 0 else depth + 1, mod, stack, path)
                for cond in gen.ifs:
                    self._visit(cond, depth + 1, mod, stack, path)
            elements = (
                [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
            )
            for element in elements:
                self._visit(element, depth + 1, mod, stack, path)
            return

        for child in ast.iter_child_nodes(node):
            self._visit(child, depth, mod, stack, path)


def _collect_sites() -> list[tuple[int, str, str]]:
    mods = _load_modules()
    funcs, aliases = _build_index(mods)
    counter = _CallGraphCounter(mods, funcs, aliases)
    counter.run("workbench.service", "collect")
    assert counter.sites, "一个会话调用点都没数到 —— 调用图解析坏了,不是代码变干净了"
    return counter.sites


# ---------------------------------------------------------------- 形状约束


def test_no_database_read_is_nested_two_loops_deep():
    """`collect()` 里不许有嵌套两层以上的库读。

    工作台三个 GET(列表 `/products`、异常 `/exceptions`、SPU 聚合 `/spu`)
    都对**全量商品**逐件 `collect()`。所以 `collect()` 里的每一层循环,
    实际是乘在商品数上的:

        商品数 × 图片集版本数 × 每版图片张数

    改之前 `image_set_service._to_view` 正是这个形状 —— 逐张图打一次
    `session.get(MediaAsset)`,而它自己又被 `resolve_for_publish` 按版本
    循环调用。三层乘起来,500 件商品的列表页要打几万次往返。

    修法不是把判定翻译成 SQL(那是 `flow.py` 明确拒绝的事,也是跟踪表
    对 BLOCK-20 的误解),而是把**取数**批量化:判定照旧在内存里对
    `flow_rules.evaluate()` 的输入做,一个字都没动。
    """
    deep = sorted(
        {(d, path, m) for d, path, m in _collect_sites() if m in READ_METHODS and d >= 2}
    )
    assert not deep, "库读嵌在两层循环里 -> " + "; ".join(
        f"深度{d} session.{m}() @ {path}" for d, path, m in deep
    )


def test_no_row_by_row_get_inside_a_loop():
    """循环体里不许出现 `session.get()`。

    `session.get()` 会先命中 identity map,所以本地测试里它常常不打库 ——
    这正是它危险的地方:开发机上一条 SQL 都看不到,生产上冷缓存时
    每行一次往返。要按主键取一批,用 `IN`(同仓库
    `workbench/service._set_items` 就是范本)。

    只管 `get`:`scalars` 出现在循环体里通常是按父行取子行,那需要的是
    预取,不是这条规则拦得住的,交给上面那条深度规则。
    """
    offenders = sorted(
        {(d, path) for d, path, m in _collect_sites() if m == "get" and d >= 1}
    )
    assert not offenders, "循环里逐行 session.get() -> " + "; ".join(
        f"深度{d} @ {path}" for d, path in offenders
    )


def test_read_path_never_writes_outside_dry_run_guard():
    """`collect()` 能碰到的写,必须全部挡在 `dry_run` 后面。

    §7.8 禁止「GET 修改业务状态」。评审第 19 条已经处理过一次:
    列表页原来 `collect()` 之后主动 commit,把草稿改成 STALE,于是刷新
    一次页面、浏览器预取一次,业务状态就变了。

    这里守的是它不再长回来。判据不是"没有写",而是"写都在 `not dry_run`
    的分支里" —— 写路径本来就该能写。
    """
    writes = [(d, path, m) for d, path, m in _collect_sites() if m in WRITE_METHODS]
    assert writes, "一处写都没数到,多半是调用图断了"

    source = (APP / "workbench" / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    unguarded: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        guarded = any(
            isinstance(sub, ast.Name) and sub.id == "dry_run" for sub in ast.walk(node.test)
        )
        if not guarded:
            continue
        # 这个 if 是 dry_run 守卫,里面的写是合规的 —— 记下来好排除
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in WRITE_METHODS
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "session"
            ):
                unguarded.append(sub.lineno)
    guarded_lines = set(unguarded)

    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "refresh_draft"
    )
    in_refresh = [
        sub.lineno
        for sub in ast.walk(fn)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr in WRITE_METHODS
        and isinstance(sub.func.value, ast.Name)
        and sub.func.value.id == "session"
    ]
    leaked = [line for line in in_refresh if line not in guarded_lines]
    assert not leaked, (
        f"refresh_draft 第 {leaked} 行的写没有挡在 dry_run 后面 —— "
        "只读接口会改业务状态"
    )


# ---------------------------------------------------------------- 重复取数


def _fn(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def test_collect_reads_attribute_values_exactly_once():
    """一件商品的属性值,`collect()` 只许取一次。

    改之前是**四次**:`_confirmed_version_ids` / `_attribute_facts` /
    `_attr_field_names` / `build_canonical` 各查各的,同参数、同 session、
    同事务,四条一模一样的 SELECT。乘以全量商品数。

    本轮收掉前三条(第四条在 `attributes.service.build_canonical`,
    生成链路与渠道层也在用,不在一个 bug 修复的范围内)。

    断言写在 `collect()` 自己身上而不是走调用图:那三个函数都保留了
    "不传就自己查"的兼容路径给 `collect()` 之外的调用方,调用图看到的
    永远是上界。**这里要钉的是 `collect()` 有没有把值传下去。**
    """
    tree = ast.parse((APP / "workbench" / "service.py").read_text(encoding="utf-8"))
    fn = _fn(tree, "collect")

    fetches = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "effective_map"
    ]
    assert len(fetches) == 1, (
        f"collect() 里 effective_map 出现 {len(fetches)} 次,应当只取一次再往下传"
    )


def test_collect_passes_the_attribute_values_it_fetched_downstream():
    """取了一次却不往下传,等于白取 —— 下游还是会各查各的。

    这条和上一条必须成对:少了它,把 `attr_values = effective_map(...)`
    取出来搁着不用照样绿,而四次 SELECT 一次没少。
    """
    tree = ast.parse((APP / "workbench" / "service.py").read_text(encoding="utf-8"))
    fn = _fn(tree, "collect")

    consumers = {"_confirmed_version_ids", "_attribute_facts", "_draft_facts"}
    missing: list[str] = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in consumers:
            continue
        passed = any(
            isinstance(a, ast.Name) and a.id == "attr_values" for a in node.args
        ) or any(
            isinstance(kw.value, ast.Name) and kw.value.id == "attr_values"
            for kw in node.keywords
        )
        if not passed:
            missing.append(node.func.id)
    assert not missing, (
        f"collect() 没有把取好的属性值传给 {sorted(set(missing))} —— 它们会自己再查一遍"
    )


# ---------------------------------------------------------------- 发布清单(A-07 / A-08)


def _publish_listing_sites() -> list[tuple[int, str, str]]:
    """从 `GET /publish/listings` 出发数会话调用点。

    这个入口原来不在预算表里 —— 上面那些断言全部从 `workbench.service.collect`
    出发,而发布清单是另一条读路径。A-08 那个 N+1 能活到人工评审才被发现,
    根因就是这里没有出发点,不是工具不管用:同一个 counter 换个入口就数得到
    (改之前 2 处循环内读,改之后 0 处)。
    """
    mods = _load_modules()
    funcs, aliases = _build_index(mods)
    counter = _CallGraphCounter(mods, funcs, aliases)
    counter.run("api.publish", "list_listings")
    assert counter.sites, "一个会话调用点都没数到 —— 调用图解析坏了,不是代码变干净了"
    return counter.sites


def test_publish_listing_reads_nothing_row_by_row():
    """A-08:`list_listings` 的循环体里不许有任何库读。

    改之前每行 4 组查询(草稿 / 最新 attempt / outbox / 未解决驳回),
    page_size=200 的一次请求要打八百多条 SQL。修法是按页各取一次再在内存
    映射,判定组装抽成 `_assemble_facts` 两条路径共用。

    这条比上面 `collect` 那两条严:那边允许深度 1(按父行取子行有时是对的),
    这里一次都不允许 —— 清单页的循环就是按行循环,循环里的任何一次往返
    都直接乘以页大小,没有"有时是对的"这种情况。

    **退化成什么样会红:** 谁在 `for listing in window:` 里塞回一次
    `session.get(ListingDraft, ...)`,或者让 `_facts()`(逐行取数那个)
    重新出现在循环里。
    """
    offenders = sorted(
        {(d, path, m) for d, path, m in _publish_listing_sites() if m in READ_METHODS and d >= 1}
    )
    assert not offenders, "发布清单在按行循环里读库 -> " + "; ".join(
        f"深度{d} session.{m}() @ {path}" for d, path, m in offenders
    )


def test_publish_listing_pages_in_the_database_not_in_python():
    """A-07:分页必须落在 SQL 上,不许物化全表再切片。

    改之前是 `rows = list(session.scalars(stmt))` 然后
    `rows[(page-1)*page_size : page*page_size]` —— 客户端要 20 条,进程
    也要把全部 ChannelListing 读进内存,`total` 则是切片前的 `len(rows)`。

    断言的是**形状**,不是 SQL 文本:出现 `.offset(...)` 和 `.limit(...)`,
    并且函数体里不出现对查询结果的下标切片。真实的 SQL 行为要真库才能验,
    那条留给 `tests/test_*_db.py`;这里守的是它不会悄悄退回 Python 切片。

    `ORDER BY` 的 tiebreaker 单独断言:`created_at` 的默认值是 PG 的
    `now()`(事务开始时间),同一事务写入的行时间戳完全相同,只按它排序
    翻页会重复或漏行。
    """
    tree = ast.parse((APP / "api" / "publish.py").read_text(encoding="utf-8"))
    fn = _fn(tree, "list_listings")
    calls = {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "offset" in calls and "limit" in calls, (
        "list_listings 没有用 offset/limit —— 分页又回到 Python 层了"
    )

    sliced = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)
    ]
    assert not sliced, f"list_listings 第 {sliced} 行在内存里切片分页(A-07 的原始缺陷)"

    # 这个函数里有**两处** order_by(翻页一处、批量取 attempt 一处)。
    # 第一版写的是 `any(len(args) >= 2 for ...)`,结果拿掉翻页的 tiebreaker
    # 照样绿 —— 另一处有三个键,`any` 就满足了。断言必须钉到翻页那一处:
    # 认它的特征是排 `ChannelListing` 而不是 `PublishAttempt`。
    ordered = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "order_by"
    ]
    assert ordered, "list_listings 没有 ORDER BY —— OFFSET 分页在无序结果上没有意义"

    paging = [
        node
        for node in ordered
        if not any(
            isinstance(sub, ast.Attribute) and sub.attr == "channel_listing_id"
            for sub in ast.walk(node)
        )
    ]
    assert len(paging) == 1, f"认不出翻页那处 order_by(候选 {len(paging)} 处)"
    assert len(paging[0].args) >= 2, (
        "翻页的 ORDER BY 只有一个键。created_at 会并列(PG 的 now() 是事务时间),"
        "翻页会重复或漏行 —— 补一个 id 之类的 tiebreaker"
    )


def test_publish_listing_and_detail_order_attempts_the_same_way():
    """清单与详情必须用**同一条**排序规则挑"最新 attempt"。

    两条路径是两条独立的 SQL。`created_at` 并列时,少了 tiebreaker 的那条
    由数据库任意挑 —— 于是列表显示"已提交"、点进去详情显示"失败",而
    §3.4 要求任何情况下不得出现两个数字。`_assemble_facts` 统一的是判定,
    统一不了取数,这条得在取数处守。

    断言的是两处 `order_by` 的键**个数**一致且都 >= 2(created_at + id)。
    """
    tree = ast.parse((APP / "api" / "publish.py").read_text(encoding="utf-8"))

    def _order_bys(fn_name: str) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(_fn(tree, fn_name))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "order_by"
        ]

    def _mentions(node: ast.Call, attr: str) -> bool:
        return any(
            isinstance(sub, ast.Attribute) and sub.attr == attr
            for sub in ast.walk(node)
        )

    # 详情路径:`_latest_attempt` 里只有这一处排序
    detail = _order_bys("_latest_attempt")
    assert len(detail) == 1, f"_latest_attempt 里有 {len(detail)} 处 order_by,预期 1 处"
    assert _mentions(detail[0], "id"), (
        "_latest_attempt 只按 created_at 排 —— 并列时和清单路径可能挑中不同的行"
    )

    # 清单路径:`list_listings` 里按 attempt 排的那一处(靠 channel_listing_id 认出来)
    batch = [n for n in _order_bys("list_listings") if _mentions(n, "channel_listing_id")]
    assert len(batch) == 1, f"list_listings 里按 attempt 排的 order_by 有 {len(batch)} 处,预期 1 处"
    assert _mentions(batch[0], "id"), (
        "list_listings 批量取 attempt 时没有 tiebreaker —— 与 _latest_attempt 口径不一致"
    )


# ---------------------------------------------------------------- SPU 聚合(A-01 ～ A-06)


def _spu_sites() -> list[tuple[int, str, str]]:
    """从 `GET /workbench/spus` 出发数会话调用点。

    D-13 记的第二个盲区。上面两组断言分别从 `workbench.service.collect` 和
    `api.publish.list_listings` 出发,而 SPU 聚合是第三条读路径 ——
    A-06 那个 N+1(循环里逐商品 `_confirmed_attributes`)能活过三份评审,
    根因同样是这里没有出发点。

    改之前这个入口数出来的是:

        深度2 session.scalars() @ list_spus > _confirmed_attributes > effective_map

    改之后为空。
    """
    mods = _load_modules()
    funcs, aliases = _build_index(mods)
    counter = _CallGraphCounter(mods, funcs, aliases)
    counter.run("api.workbench_batch", "list_spus")
    assert counter.sites, "一个会话调用点都没数到 —— 调用图解析坏了,不是代码变干净了"
    return counter.sites


def test_spu_aggregation_reads_nothing_row_by_row():
    """A-06:`list_spus` 自己的循环里不许有库读。

    ## 为什么要排除 `iter_flow_contexts`

    这一页每个 SKU 都要一份 `collect()` 的判定结论,而 `collect()` 内部
    自然有库读 —— 那是**它的**预算,由本文件开头那三条(从 `collect` 出发)
    管着。这里排除它之后,剩下的就是 `list_spus` 自己在按行循环里打的库,
    一次都不允许。

    排除的写法是按路径过滤而不是按深度放宽:放宽深度会把
    `list_spus > _confirmed_attributes` 一起放过去,而那正是要拦的东西。

    **退化成什么样会红:** 谁在 `for product, ctx in ...` 里塞回一次
    `_confirmed_attributes(session, product.id)`、或者任何按行的
    `session.get()` / `session.scalars()`。
    """
    offenders = sorted(
        {
            (d, path, m)
            for d, path, m in _spu_sites()
            if m in READ_METHODS and d >= 1 and "iter_flow_contexts" not in path
        }
    )
    assert not offenders, "SPU 聚合在按行循环里读库 -> " + "; ".join(
        f"深度{d} session.{m}() @ {path}" for d, path, m in offenders
    )


def test_spu_aggregation_pages_in_the_database_not_in_python():
    """A-01 / A-05:翻页必须落在 SQL 上,不许物化全表再切片。

    改之前是 `products = list(session.scalars(stmt))` -> 逐件
    `iter_flow_contexts` -> `aggregate(rows)[:limit]`。被切掉的那些商品
    的 `collect()` 一次没省,只是算完就扔。

    断言的是**形状**:出现 `.offset()` 和 `.limit()`,并且函数体里
    不出现对结果的下标切片。真实 SQL 行为要真库,那条留给 D-01。
    """
    tree = ast.parse((APP / "api" / "workbench_batch.py").read_text(encoding="utf-8"))
    fn = _fn(tree, "list_spus")
    calls = {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "offset" in calls and "limit" in calls, (
        "list_spus 没有用 offset/limit —— 翻页又回到 Python 层了"
    )

    sliced = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)
    ]
    assert not sliced, f"list_spus 第 {sliced} 行在内存里切片翻页(A-05 的原始缺陷)"


def test_spu_totals_are_not_taken_from_the_current_page():
    """A-02 / A-03:`total` 与 `inconsistent_spus` 都是全量口径。

    改之前 `total = len(groups)` 而 `groups` 已经被 `[:limit]` 截过,于是
    `total` 恒 <= limit;`inconsistent_spus` 同源。两个数字都**看起来正常**,
    这正是它比报错危险的地方。

    断言钉在返回字典上:`total` 不许是 `len(...)`,`inconsistent_spus`
    不许提到 `groups`(本页那份聚合结果)。
    """
    tree = ast.parse((APP / "api" / "workbench_batch.py").read_text(encoding="utf-8"))
    fn = _fn(tree, "list_spus")

    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert len(returns) == 1, f"list_spus 有 {len(returns)} 个 return,认不出出参字典"
    payload = returns[0].value
    assert isinstance(payload, ast.Dict), "list_spus 的 return 不是字面字典了,断言要跟着改"

    by_key = {
        k.value: v
        for k, v in zip(payload.keys, payload.values, strict=True)
        if isinstance(k, ast.Constant)
    }
    for key in ("total", "inconsistent_spus", "page", "page_size", "items"):
        assert key in by_key, f"出参里少了 {key}"

    total = by_key["total"]
    assert not (
        isinstance(total, ast.Call)
        and isinstance(total.func, ast.Name)
        and total.func.id == "len"
    ), "total 又是 len(...) 了 —— 它数的只会是本页"

    page_names = {
        n.id for n in ast.walk(by_key["inconsistent_spus"]) if isinstance(n, ast.Name)
    }
    assert "groups" not in page_names, (
        "inconsistent_spus 是从本页的 groups 上数出来的 —— 它显示为全局告警"
    )
