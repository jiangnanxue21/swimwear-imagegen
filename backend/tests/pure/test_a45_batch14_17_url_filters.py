"""A45-batch14-17:筛选状态住在 URL 里(GAP-033 / FE-GLOBAL-06,阶段 0 最后一条代码项)。

## 这一批守的是什么

`useUrlSeed` 的口径是「URL 参数是初值,不是真相」,页面接手后 `clear()`
把参数擦掉。于是 §8.2 的四条要求里只剩「点进来能带条件」一条成立,
另外三条(URL 可复制、刷新保留、后退保留)全死在那一行 `clear()` 上。

本批把口径翻过来:**URL 是唯一真相**,组件内不再存筛选值。

## 为什么守卫压在 Python 这一侧

这批改动的正经覆盖是 Vitest(`tests/component/nav-and-url-filters.test.tsx`,
本批已重写)。但那批用例**在这台机器上一次都没跑过** —— 装不了 node_modules。
所以真正被执行过的验证只有这个文件,它读前端源码做断言。

这不是理想分工,是如实分工。写在这里是为了下一台有依赖的机器知道:
**先跑 Vitest,那才是行为验证;这里守的是结构。**

## 几条断言的形状说明

- 「不许再出现 X」这类**反向**断言,窗口一律走 `_helpers.window()` /
  `braced_block()`,理由见 14-14 那条门禁:反向断言碰上窄窗口会静默变真。
- 跨语言那两条(page_size 上限、排序白名单)取**子集/不超过**而不是相等:
  后端放宽限制不该让前端变红。
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT, braced_block, only_line, window

FRONTEND = PROJECT_ROOT / "frontend" / "src"
APP = BACKEND_ROOT / "app"

#: 本批搬进 URL 的四页。第四页(审计)不在 GAP-033 原文点名的三页里 ——
#: 但它用的是同一个 hook、同一个缺陷:「我的操作记录」这个链接刷新一次
#: 就变回全站记录。只搬三页等于把病根留在第四页上,下一个人照着它抄。
PAGES = (
    "WorkbenchListPage.tsx",
    "TaskListPage.tsx",
    "WorkbenchReviewPage.tsx",
    "AuditLogPage.tsx",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _page(name: str) -> str:
    return _read(FRONTEND / "pages" / name)


def _code_only(src: str) -> str:
    """去掉注释。注释里出现的字样不是代码在做的事 —— 这个仓库栽过四次。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def _spec_body(src: str) -> str:
    """`useUrlFilters({ ... })` 那张声明表的表体。

    按括号配平切:表里的值自己带花括号(`intParam(1, { min: 1 })`),
    按第一个 `}` 切会切在半路上,而**后面那几个参数会静静消失** ——
    切窄之后断言只是变得更容易满足,不会变红。
    """
    return braced_block(src, "useUrlFilters({", what="筛选声明表")


def _spec_keys(src: str) -> set[str]:
    return set(re.findall(r"^\s*([a-z_]+):\s*\w+Param", _spec_body(src), re.M))


def _codec_body(src: str, name: str) -> str:
    """取一个 codec 的函数体 —— **实现那一份,不是重载声明**。

    `enumParam` 有两条重载声明加一个实现,三处都以 `export function enumParam`
    开头,于是 `window()` 当场把它挡回来了(「不唯一的定位串等于没有定位串」)。
    挡得对:三处里只有最后一处有函数体,前两处切出来的是一段没有 `write` 的
    签名 —— 而这条守卫要查的正是 `write` 里那句判断。

    所以这里不切窗口,直接按花括号配平取实现的函数体。
    """
    declarations = list(re.finditer(rf"export function {name}\b", src))
    assert declarations, f"找不到 codec `{name}`"
    # 实现总是最后一处:重载声明以 `: Codec<...>` 收尾,只有实现带 `{{`
    head = declarations[-1].start()
    opening = src.index("{", src.index("): Codec", head))
    depth = 0
    for i in range(opening, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[opening : i + 1]
    raise AssertionError(f"codec `{name}` 的函数体没有配平的收尾")


def _effect_deps(src: str, body: str, *, what: str) -> str:
    """取一条 effect 的依赖数组内容。

    起点是 effect 的**函数体**(唯一),终点走 `]` —— 依赖数组里不会再有 `]`,
    所以这一段是封闭的。不拿 `}, [` 当 `window()` 的终点:它在一个页面里
    出现三次,而**不唯一的定位串等于没有定位串**。
    `only_line()` 也不行 —— 依赖跟在函数体后面,不是"以某串开头的一行"。
    """
    marker = body + "\n  }, ["
    tail = window(src, marker, what=what)
    rest = tail[len(marker) :]
    assert "]" in rest, f"{what}:依赖数组没有收尾"
    return rest[: rest.index("]")]


# ==========================================================
# 1:旧 hook 必须消失,不是"还留着但没人用"
# ==========================================================


def test_the_seed_hook_is_gone_from_the_tree():
    """`useUrlSeed.ts` 必须真的被删掉,不许留在树上。

    留着它的代价不是一个死文件:**下一个加筛选页的人会照着它抄**,
    而抄回来的是「读一次初值然后擦掉」——刷新就丢,而且不会有任何东西报错。
    这个仓库对"留一份旧的当参考"有过判断:能被抄的旧做法就是活的。

    删干净之后,这条规矩由"文件不存在"来保证,不靠谁记得。
    """
    assert not (FRONTEND / "hooks" / "useUrlSeed.ts").exists(), (
        "useUrlSeed.ts 还在树上 —— 下一个人会照着它抄回「用完擦掉」那套"
    )

    offenders = [
        page for page in PAGES if "useUrlSeed" in _code_only(_page(page))
    ]
    assert not offenders, f"这些页面还在用已删除的 useUrlSeed:{offenders}"


def test_no_page_wipes_a_filter_param_off_the_address_bar():
    """没有任何一页再把筛选参数从地址栏上擦掉。

    擦参数(`setParams(..., { replace: true })` 配一次 `delete`)正是
    FE-GLOBAL-06 点名的那件事。它的表现最容易被当成"本来就这样":
    地址栏干干净净,刷新回到全量列表,而运营以为是自己没筛。

    只查页面,不查 hook —— `useUrlFilters.patch()` 内部当然会 `delete`
    (默认值不写进 URL 就是靠它)。判据是**页面层不许自己动 URL**。
    """
    for page in PAGES:
        src = _code_only(_page(page))
        assert "useSearchParams" not in src, (
            f"{page} 自己动了 useSearchParams —— 筛选的读写只有 useUrlFilters 一个口子,"
            "页面各自改 URL 就又有了第二处真相"
        )
        assert "replace: true" not in src, (
            f"{page} 出现 `replace: true` —— 改筛选是一次导航,replace 掉的是"
            "运营按后退撤销一次误点的能力(§8.2「后退保留」)"
        )


# ==========================================================
# 2:默认值不写进 URL
# ==========================================================


def test_every_codec_drops_its_default_value_from_the_url():
    """每个 codec 在值等于默认值时都要返回 `null`(= 从 URL 上删掉)。

    不这么做的表现不是错误,是**地址变得没法贴**:进一次页面就攒出
    `?page=1&page_size=20&sort=updated_at&order=desc&only_blocked=0`,
    而「URL 可复制」这条要求的实际形态是运营把地址贴进群里 ——
    一串二十个参数的地址没有人会贴。

    逐个 codec 切自己的函数体,反向断言不吃别人的源码。
    """
    src = _read(FRONTEND / "hooks" / "useUrlFilters.ts")
    for codec, expected in (
        ("enumParam", "value === fallback"),
        ("textParam", "value === ''"),
        ("flagParam", "value === fallback"),
        ("intParam", "value === fallback"),
        ("oneOfParam", "value === fallback"),
    ):
        body = _codec_body(src, codec)
        assert "write:" in body, f"{codec} 没有 write"
        assert expected in body, (
            f"{codec} 的 write 不再判断默认值 —— 默认值会被写进地址栏,URL 贴不出去"
        )
        assert "null" in body.split("write:")[1], (
            f"{codec} 的 write 不再返回 null —— 参数删不掉,只会被写成空串"
        )


def test_every_codec_falls_back_to_its_own_default_when_it_cannot_read_the_value():
    """读不出来时退回**这个 codec 自己的默认值**,不是退回某个字面量。

    ## 这条是变异 U5 漏网之后补的

    上一版只守了 `write`(默认值不进 URL),`read` 那一半没人管。于是这条
    变异安静地过了:

        read: (raw) => (raw === '1' ? true : raw === '0' ? false : fallback)
        read: (raw) => raw === '1'

    看起来只是"少认一种写法",实际后果是 **`raw === null` 时返回 `false`** ——
    也就是审计页在**没有任何参数**的默认状态下,「只看上架闭环」变成关着的。
    那个开关默认开着是刻意的降噪(审计表里混着生成任务的状态流转),
    关掉之后运营要在一堆技术流水里找批准和导出记录,而界面上的开关
    和数据是一致的、没有任何地方报错。

    `write` 守得住而 `read` 守不住,是因为这两半的失效方向不一样:
    默认值漏进 URL 会**看得见**(地址变长),默认值读错只是**行为变了**。

    判据:每个 codec 的 `read` 里必须出现它自己的默认值来源。
    """
    src = _read(FRONTEND / "hooks" / "useUrlFilters.ts")
    for codec, default_source in (
        ("enumParam", "fallback"),
        ("textParam", "''"),
        ("flagParam", "fallback"),
        ("intParam", "fallback"),
        ("oneOfParam", "fallback"),
    ):
        body = _codec_body(src, codec)
        read = body[body.index("read:") : body.index("write:")]
        assert default_source in read, (
            f"{codec} 的 read 里没有 `{default_source}` —— 读不出来时退回的是一个"
            "写死的值,而不是这个参数自己的默认值。参数缺席那一路会一起走错"
        )


def test_the_hook_never_rewrites_the_url_while_reading():
    """读的那条路不许写 URL。

    非法参数顺手删掉是很自然的念头,而它是**渲染期发起一次导航** ——
    和上一版 `clear()` 同一个形状的错误(地址栏和用户抢方向盘),
    还会把一次手敲变成后退栈里的一格。退回默认值就够了。

    判据:`values` / `signature` 这两个 memo 里不许出现 `setParams`。
    """
    src = _read(FRONTEND / "hooks" / "useUrlFilters.ts")
    reading = window(src, "  const values = useMemo(", "  const patch = useCallback(",
                     what="读取路径")
    assert "setParams" not in reading, (
        "读取路径里出现了 setParams —— 渲染期改地址栏,和被修掉的 clear() 是同一个错误"
    )


# ==========================================================
# 3:BLOCK-09 换了一扇门 —— 清空必须挂在 signature 上
# ==========================================================


def test_selection_is_cleared_by_the_filter_signature_not_by_a_setter():
    """勾选的清空挂在 `filters.signature` 上,不是写在某个 onChange 里。

    ## 这条守的是一个**新开的**失效方向

    URL 化之前,「换筛选就清空勾选」(BLOCK-09)靠六个入口都走
    `changeFilter()` 一个口子。URL 化之后那个口子不再是唯一入口:
    **浏览器后退、前进、直接编辑地址栏都会改筛选,而它们一行 JS 都不经过。**

    勾 20 件 -> 按一次后退 -> 动作条仍说「已选 20 件」-> 点批量识别,
    提交的是那 20 件,其中大部分不在他眼前 —— BLOCK-09 原样回来了。

    「执行前显示可执行数量」那个确认框救不了它:数字本身是对的。

    所以判据是**依赖数组里必须是 signature**,不是"有没有清空这个动作"。
    """
    dependency = _effect_deps(
        _code_only(_page("WorkbenchListPage.tsx")),
        "  useEffect(() => {\n    setSelected([])",
        what="清空勾选 effect 的依赖",
    )
    assert "signature" in dependency, (
        "清空勾选没有挂在筛选签名上 —— 后退/前进会改筛选却不经过任何 setter,"
        "BLOCK-09 会从那扇门原样回来"
    )


def test_the_quick_review_cursor_resets_on_the_same_signal():
    """快审的游标同理:换筛选回到队列开头,而且要认后退。

    不认的表现:后退换回上一类,游标还停在第 7 位,而新队列只有 3 件 ——
    `current` 取到最后一件,运营以为自己在审第 7 件。
    """
    dependency = _effect_deps(
        _code_only(_page("WorkbenchReviewPage.tsx")),
        "  useEffect(() => {\n    setIndex(0)",
        what="快审游标复位的依赖",
    )
    assert "signature" in dependency, (
        "快审游标复位没有挂在筛选签名上 —— 后退换筛选时游标会停在上一条队列的位置"
    )


def test_the_cursor_and_the_selection_stay_out_of_the_url():
    """游标和勾选**不许**进 URL。

    它们不是筛选条件,是一次会话里的临时位置。写进 URL 的话,贴给同事的
    地址会带上「我勾了这 20 件」「我停在第 7 张」—— 而队列是活的,
    他打开看到的是另一件商品。一个**看起来精确、实际上指错**的地址
    比不带它更贵。

    筛选是条件(在两个人那里意思一样),游标是位置(不是)。
    """
    for page, forbidden in (
        ("WorkbenchReviewPage.tsx", ("index", "cursor")),
        ("WorkbenchListPage.tsx", ("selected", "selection")),
    ):
        keys = _spec_keys(_page(page))
        leaked = keys & set(forbidden)
        assert not leaked, f"{page} 把会话位置写进了 URL:{sorted(leaked)}"


# ==========================================================
# 4:一次 patch 写完,不要连进几格后退栈
# ==========================================================


def test_changing_a_filter_also_resets_the_page_in_the_same_write():
    """换筛选必须和「回第一页」写在同一次 patch 里。

    分两次写的表现有两个,都不报错:后退栈里多一格(按一次后退看到的是
    "筛选变了但页码没回去"这种没人产生过的中间态),以及**多打一次接口**。

    判据:分页页面上每一处 `patch({` 里只要出现了别的筛选键,
    就必须同时出现 `page:`。翻页自己那一处除外 —— 它改的就是页码。
    """
    for page in ("WorkbenchListPage.tsx", "TaskListPage.tsx", "AuditLogPage.tsx"):
        src = _code_only(_page(page))
        for call in re.findall(r"patch\(\{[^}]*\}\)", src, re.S):
            keys = set(re.findall(r"([a-z_]+):", call))
            others = keys - {"page", "page_size", "sort", "order"}
            if not others:
                continue  # 翻页 / 换排序自己那一处
            assert "page" in keys, (
                f"{page} 有一处改了筛选却没有同时回第一页:{call.strip()} —— "
                "第 5 页上换筛选会停在新结果的第 5 页,看起来像筛选被清空了"
            )


def test_sorting_lives_in_the_url_on_the_pages_that_moved():
    """搬了筛选的分页页面,排序也要一起搬。

    只搬一半比不搬更糟:刷新之后筛选还在、排序回到默认,而表头箭头是受控的、
    会跟着回到默认 —— 界面和数据仍然自洽,没有任何地方报错,
    只有运营发现「我明明按阻断数排过」。
    """
    for page in ("WorkbenchListPage.tsx", "TaskListPage.tsx"):
        keys = _spec_keys(_page(page))
        assert {"sort", "order"} <= keys, (
            f"{page} 的排序没进 URL(缺 {sorted({'sort', 'order'} - keys)}) —— "
            "刷新之后筛选还在而排序回到默认"
        )
        src = _code_only(_page(page))
        assert re.search(r"useServerSort\([^)]*,[^)]*,\s*\{", src, re.S), (
            f"{page} 的 useServerSort 没有接 store —— 排序仍然存在组件内"
        )


# ==========================================================
# 5:跨语言 —— 前端的两个白名单不许超出后端
# ==========================================================


def _endpoint(api_src: str, marker: str, *, what: str) -> str:
    """把一个端点的函数体从文件里切出来。

    起点是 `def xxx(`(唯一),终点是**下一个 `@router.` 装饰器** ——
    不拿 `session:` 之类当终点:它在一个文件里出现十几次,
    而 `window()` 会当场把这种写法挡回来(这条就是被它挡回来才改成现在这样的)。
    """
    tail = window(api_src, marker, what=what)
    nxt = tail.find("\n@router.", 1)
    return tail if nxt < 0 else tail[:nxt]


def _page_size_ceiling(api_src: str, marker: str, *, what: str) -> int:
    """从 `page_size: int = Query(default=20, ge=1, le=100)` 里取 `le`。"""
    line = only_line(_endpoint(api_src, marker, what=what), "page_size: int = Query",
                     what=what)
    found = re.search(r"le=(\d+)", line)
    assert found, f"{what}:page_size 的 Query 里没有 le="
    return int(found.group(1))


def test_frontend_page_size_options_stay_within_the_backend_limit():
    """前端能选到的每页条数不许超过后端的 `le=`。

    超了的表现是**一个 422**:运营从下拉框里选了一档,列表整个消失。
    而下拉框是前端自己列的,后端那个上限在另一个语言的另一个文件里 ——
    tsc 看不见 `workbench.py`,ruff 看不见 `.tsx`,没有单语言工具管得了这条缝。

    取「≤」而不是「相等」:后端放宽到 200 不该让前端变红。
    """
    for page, api_file, endpoint_marker in (
        ("WorkbenchListPage.tsx", "workbench.py", "def list_products("),
        ("AuditLogPage.tsx", "workbench_batch.py", "def query_audit("),
    ):
        options = re.search(r"const PAGE_SIZES = \[([\d, ]+)\]", _page(page))
        assert options, f"{page} 没有 PAGE_SIZES 常量"
        largest = max(int(x) for x in options.group(1).split(","))

        ceiling = _page_size_ceiling(
            _read(APP / "api" / api_file), endpoint_marker,
            what=f"{api_file} 的 {endpoint_marker}",
        )
        assert largest <= ceiling, (
            f"{page} 的每页条数最大档 {largest} 超过后端上限 {ceiling} —— 选中它就是 422"
        )


def test_frontend_sort_keys_are_a_subset_of_the_backend_whitelist():
    """前端的排序白名单必须是后端那份的子集。

    多一个的表现是后端**静默退回默认排序**(它自己有白名单),
    而前端的表头箭头受控、还指着那一列 —— 界面说「按这列排过」,数据没有。
    这正是硬规则 4 在禁的:状态字段必须能追溯到真实来源。
    """
    for page, const, api_file, backend_const in (
        ("WorkbenchListPage.tsx", "LIST_SORT_KEYS", "api/workbench.py", "_LIST_SORT_KEYS"),
        ("TaskListPage.tsx", "TASK_SORT_KEYS",
         "services/generation_service.py", "TASK_SORTABLE"),
    ):
        declared = re.search(rf"const {const} = \[(.*?)\]", _page(page), re.S)
        assert declared, f"{page} 没有 {const}"
        frontend_keys = set(re.findall(r"'([a-z_]+)'", declared.group(1)))

        table = braced_block(_read(APP / api_file), f"{backend_const} = {{",
                             what=f"{backend_const}")
        backend_keys = set(re.findall(r'"([a-z_]+)":', table))

        unknown = frontend_keys - backend_keys
        assert not unknown, (
            f"{page} 的 {const} 里有后端不认的排序键 {sorted(unknown)} —— "
            "后端会静默退回默认排序,而表头箭头还指着那一列"
        )
