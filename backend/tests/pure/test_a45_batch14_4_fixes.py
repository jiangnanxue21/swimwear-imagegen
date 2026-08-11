"""A45-batch14-4:把 `mutate_contract_tests.py` 退役时失去的 15 条守卫补回来。

## 这些守卫从哪来

A8/A9 那一轮的 18 条变异指向 `test_frontend_contract.py` 里的 15 个测试。
那个模块后来被改写成契约比对(标签 / 枚举 / 路由 / 字段上限),15 个名字
一个不剩。而变异脚本按 `rc != 0` 判定"响了",`getattr` 抛的 AttributeError
恰好满足它 —— 于是 18 条**全部失效却报告"全部被抓住"**,直到 batch14-3
的锚点审计把它挖出来(见 `REVIEW-A45-BATCH14-3.md` F7)。

退役那份脚本时,第四节记了账:18 行 / 15 个不重复的守卫名,主题**一条都没有**
被别处接住。上一轮的探针判定说有 6 条还有人守,那是**误报** —— 逐条读过之后:

    RESOLVE_REJECTION / RELEASE_QUARANTINE  命中的是后端状态机测试,
                                            与首页卡片指向哪个路由无关
    useIdentity                             命中的是 hook 自身与 ColdStartBanner,
                                            没有一条守"菜单不再开第二份 whoami"
    UnsavedGuard                            只是 ImageSetTab 草稿那条注释里提了一句
    by_next_action                          grep 零命中

「符号在测试里出现过」不等于「那条决定被守着」—— 与 batch14-3 F6 里
「文件里出现过这串字不等于这行代码在生效」是同一个陷阱,换了一层。

## 写法

全部读源码、不 import:这些决定住在前端,而前端没有可在纯层执行的运行时。
一律先过 `_code_only()` —— A8/A9 那一轮 12 个变异有 2 个没被抓住,两处
都是"断言代码里用了 X"被**注释里**的 X 满足了。

每条守卫都有对应变异(`tools/mutate_batch14_4.py`),且变异是**先写的** ——
反过来做就是 batch13-3 / M11 的来路。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROJECT = BACKEND.parent
FRONTEND = PROJECT / "frontend" / "src"
APP = BACKEND / "app"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """去掉 `/* */` 与 `//` 注释。`(?<!:)` 是为了不误伤 `https://`。"""
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", without_block)


def _no_hash_comments(source: str) -> str:
    return re.sub(r"(?<!\$)#[^\n]*", "", source)


def _enum_members(path: Path, class_name: str) -> set[str]:
    """按 AST 取某个枚举类的成员名。

    **不能用正则扫全文件。**`flow.py` 里不止一个枚举(`FlowStep`、状态标记……),
    按缩进抓 `^    NAME = "..."` 会把它们一起捞进来 —— 第一版就是这么写的,
    表现是 `test_secondary_actions_do_not_hide_a_whole_step` 报了 12 个
    根本不是动作码的名字,而 `test_today_page_cards_point_at_real_actions_...`
    因为集合被撑大反而**假通过**了。宽集合让"必须属于"这类断言失效。
    """
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                target.id
                for item in node.body
                if isinstance(item, ast.Assign)
                for target in item.targets
                if isinstance(target, ast.Name)
            }
    raise AssertionError(f"{path.name} 里找不到枚举 {class_name}")


def _action_codes() -> set[str]:
    codes = _enum_members(APP / "workbench" / "flow.py", "NextActionCode")
    assert len(codes) >= 10, f"只解析到 {len(codes)} 个动作码,选择器失效了"
    return codes


def _app_tsx() -> str:
    return _code_only(_read(FRONTEND / "App.tsx"))


def _nav_keys() -> list[str]:
    """侧栏里每一个菜单项的 key。"""
    return re.findall(r"\{\s*key:\s*'([^']+)'", _app_tsx())


def _routed_paths() -> list[str]:
    return re.findall(r'<Route\s+path="([^"]+)"', _app_tsx())


# ==========================================================
# 1-4:菜单与路由(原 App.tsx 四条)
# ==========================================================


# ## 这里原来有一条 `test_admin_guarded_pages_are_not_in_the_operator_menu`
#
# 它断言 `NAV` 里存在 `adminOnly: true` 的分组、且管理员页只出现在那一组。
# 后来菜单改成**不再按账号隐藏入口**(理由写在 `App.tsx` 与 `AppLayout.tsx`:
# 新人第一次进来还不是管理员,也必须找得到设置页;真正的边界在后端
# `require_admin`),而这条守卫没有跟着改 —— 于是它从那次翻转起就一直红着,
# 直到 a46-phase2 才被人看见。
#
# 更要紧的是它和 `frontend/tests/component/nav-and-url-filters.test.tsx` 里的
# 「「系统管理」不按账号隐藏」**互为反面**:一条要求 `adminOnly` 存在,
# 另一条要求它不存在。两条守卫对同一件事给出相反的判据时,红的那一条会被
# 当成"环境问题"绕过去,而绿的那一条被当成事实 —— 这比只有一条守卫更糟。
#
# 删而不是翻转,依据是 `frontend/CLAUDE.md` 第三条:**不要用 Python 扫前端
# 源码来做断言**。菜单可见性完全落在前端内部,不是跨语言契约;Vitest 那条
# 读的是真实的 `NAV` 对象,而不是拿正则去猜 TSX 的缩进形状。
# 判定留一份,在会渲染它的那一侧。


def test_nav_is_grouped_and_every_entry_is_routed():
    """每个菜单项都要有一条真实路由接着。

    拼错一个字符的表现是点了没反应(或者落到 NotFound),而**菜单项本身
    看起来完全正常** —— 这是静态匹配能抓、人眼很难抓的一类。
    """
    keys = _nav_keys()
    assert len(keys) >= 10, f"只解析到 {len(keys)} 个菜单项,选择器多半失效了"
    routed = set(_routed_paths())
    missing = [k for k in keys if k not in routed]
    assert not missing, f"这些菜单项没有对应路由(点了不会有反应):{missing}"

    # 这里原来还有一句 `assert "adminOnly: true" in _app_tsx()`。菜单改成不按
    # 账号隐藏之后它就是错的,和上面删掉的那条同一次翻转、同一个原因。
    # **剩下的这一半留着**:它验的是"菜单项拼错一个字符"这类只在点击时才暴露的
    # 问题,offline 门禁跑得到而 Vitest 要 node_modules —— 两者不冲突,
    # 只要它别再去断言菜单该按角色裁剪。

    # 同一个页面不许出现在两组里。
    #
    # 这一条是**补上删那条守卫时漏掉的那个洞**:变异 Q1「把 /settings 挪进普通
    # 运营菜单」原来由被删的那条抓,删完之后 `mutate_batch14_4.py` 报 19/20 ——
    # 它只跑本文件这一个套件,而 Vitest 那边的「同一个 key 不出现在两个组里」
    # 它看不见。
    #
    # 和 Vitest 那条**不矛盾,是同一句话**(这正是它和被删那两条的区别:
    # 那两条说的是相反的话)。留两份的理由是它们跑在不同的门禁里:
    # 这一份进 `make check-offline`,不需要 node_modules。
    duplicated = sorted({k for k in keys if keys.count(k) > 1})
    assert not duplicated, (
        f"这些菜单项同时出现在两个组里:{duplicated} —— 侧栏会同时点亮两处,"
        f"而运营会以为那是两个不同的页面"
    )


def test_default_route_lands_on_the_todo_homepage():
    """`/` 落在待办首页,不是指标仪表盘。

    §8.2:运营早上打开的第一屏应该是"今天要做什么",不是"昨天做了多少"。
    退回 `/dashboard` 不会报任何错,只是每个人每天第一眼看错东西。
    """
    src = _app_tsx()
    default_route = re.search(r'<Route\s+path="/"\s+element=\{<Navigate\s+to="([^"]+)"', src)
    assert default_route, "找不到默认路由 —— `/` 现在不重定向了?"
    assert default_route.group(1) == "/today", (
        f"默认路由指向 {default_route.group(1)},应该是 /today(§8.2 的动线)"
    )


def test_routes_are_not_gated_by_role():
    """**路由对所有人注册,裁的是菜单和页面里的动作。**

    按角色裁路由的话,管理员把链接发给运营,运营打开是 NotFound ——
    而正确的表现是打开、看到页面、被明确告知"这一项要管理员"。
    两者的区别在于运营知不知道自己该找谁。
    """
    src = _app_tsx()
    gated = re.findall(r"\{\s*\w*[Aa]dmin\w*\s*&&\s*<Route", src)
    assert not gated, (
        f"路由被角色条件包住了({len(gated)} 处)—— "
        f"运营会看到 NotFound 而不是「需要管理员」"
    )
    assert re.search(r"<Route\s+path=\"/settings\"", src), "/settings 的路由不见了"


# ==========================================================
# 5-7:首页(原 TodayPage.tsx 三条)
# ==========================================================


def _today_page() -> str:
    return _code_only(_read(FRONTEND / "pages" / "TodayPage.tsx"))


def test_today_page_reads_its_numbers_from_the_backend():
    """首页不自己把计数加起来。

    自己聚合的表现是"首页说 12、点进去只有 9" —— 而两个数字都对,
    只是口径是两处各写一份的。待办口径只能有一处,在后端。
    """
    src = _today_page()
    assert "board.data?.summary.by_next_action" in src, "首页不再读后端给的 by_next_action 了"
    aggregations = re.findall(r"SECONDARY_ACTIONS\.reduce\(", src)
    assert not aggregations, "首页自己 reduce 出了一个总数 —— 待办口径出现了第二处定义"


def test_today_page_cards_point_at_real_actions_and_real_routes():
    """卡片上的动作码必须真实存在,跳转路径必须真的注册过。

    写错一个码的表现是那张卡永远显示 0 —— **看起来像"今天没有这类待办"**,
    而不是像一个 bug。这是最贵的一种静默失效:它不报错、不留痕,
    只是让一批商品永远没人处理。
    """
    src = _today_page()
    real_codes = _action_codes()

    used = set(re.findall(r"byAction\?\.\[?([A-Z_]+)\]?", src))
    unknown = used - real_codes
    assert not unknown, f"首页卡片引用了不存在的动作码(那张卡会永远显示 0):{sorted(unknown)}"

    routed = set(_routed_paths())
    for target in re.findall(r"to:\s*'([^']+)'", src):
        path = target.split("?")[0]
        assert path in routed, f"首页卡片跳到一条没注册的路由:{target}"

    for code in re.findall(r"next_action=([A-Z_]+)", src):
        assert code in real_codes, f"跳转 URL 里的动作码不存在:{code}"


def test_secondary_actions_do_not_hide_a_whole_step():
    """「其余待办」必须覆盖所有**不在大卡上**的动作码。

    漏掉一个码的表现:那一步的商品在首页一处都不显示,运营看完的结论是
    "今天没事干",而实际上有五件卡在等他上传背面图。`DONE` 不在这里 ——
    它是"已导出、无待办"。
    """
    src = _today_page()
    real_codes = _action_codes()

    block = re.search(r"const SECONDARY_ACTIONS:[^=]*=\s*\[(.*?)\]", src, re.S)
    assert block, "找不到 SECONDARY_ACTIONS"
    secondary = set(re.findall(r"'([A-Z_]+)'", block.group(1)))

    on_cards = set(re.findall(r"byAction\?\.\[?([A-Z_]+)\]?", src))
    uncovered = real_codes - secondary - on_cards - {"DONE"}
    assert not uncovered, (
        f"这些动作码在首页一处都不显示,整整一步对运营是隐形的:{sorted(uncovered)}"
    )
    assert not (secondary & on_cards), (
        f"同一个码既上大卡又进「其余待办」,会被数两遍:{sorted(secondary & on_cards)}"
    )


# ==========================================================
# 8:运行中任务数(前端 + 后端各一处)
# ==========================================================


def test_in_flight_task_count_comes_from_the_state_machine():
    """"哪些状态算运行中"是状态机的知识,两处都不许自己抄一份。

    抄一份的表现:状态机加了一个新的中间态,而仪表盘的数字**少了一截** ——
    没有任何地方会报错,数字只是安静地不对。
    """
    service = _no_hash_comments(_read(APP / "services" / "dashboard_service.py"))
    assert "sm.IN_FLIGHT_STATES" in service, (
        "dashboard_service 不再从状态机取运行中状态 —— 它自己抄了一份清单"
    )
    hardcoded = re.search(
        r'for\s+\w+\s+in\s*\(\s*"(QUEUED|PROVIDER_RUNNING)"', service
    )
    assert not hardcoded, "dashboard_service 里出现了写死的状态清单"

    today = _today_page()
    assert "tasks.data?.tasks.in_flight" in today, "首页不再读后端算好的 in_flight"
    for status in ("QUEUED", "PROVIDER_RUNNING"):
        assert status not in today, (
            f"首页文案里写死了状态名 `{status}` —— 状态机改了它不会跟着改"
        )


# ==========================================================
# 9:落地页读 URL 筛选
# ==========================================================


def _url_filter_spec(src: str) -> set[str]:
    """取 `useUrlFilters({ ... })` 那张声明表的键。

    按括号配平切,不按第一个 `}`:表里的值本身带花括号
    (`intParam(1, { min: 1 })`),按第一个 `}` 切会切在半路上,
    而切窄之后**后面那几个参数会静静消失** —— 断言只是变得更容易满足。
    """
    marker = "useUrlFilters({"
    assert src.count(marker) == 1, f"useUrlFilters({{ 出现 {src.count(marker)} 次"
    begin = src.index(marker) + len(marker) - 1
    depth = 0
    for i in range(begin, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                body = src[begin + 1 : i]
                break
    else:
        raise AssertionError("useUrlFilters 的声明表没有配平的收尾")
    return set(re.findall(r"^\s*([a-z_]+):\s*\w+Param", body, re.M))


def test_landing_pages_accept_the_filter_from_the_url():
    """从首页卡片点进来时,筛选条件跟着 URL 一起到。

    不读 URL 的表现:点"待处理驳回"进去,看到的是**全部**任务 ——
    运营要在几百行里自己找那几件,而他刚才明明已经说清楚要看哪一类。

    ## 这条守卫被改过一次口径(GAP-033)

    上一版断言的是「这一页 `import { useUrlSeed }` 并且调用了它」。
    那是**点名做法**,不是钉性质 —— 于是 GAP-033 把 hook 换成
    `useUrlFilters`(一次正当的改造)时它当场变红,而红的原因是
    "有人把这件事做得更好了"。

    和 14-16 撞见的 `heads == {"0037"}` 是同一个形状,CLAUDE.md 那条
    「规矩要么是不变式,要么是散文」说的也是同一件事。现在钉的是性质:
    **这一页从 URL 上读得到那个参数名**,至于是哪个 hook 干的不在射程内。

    好在两次都是红不是静默过期 —— 正向断言切错了会红,有人看得见。
    """
    for page, param in (
        ("TaskListPage.tsx", "status"),
        ("WorkbenchListPage.tsx", "next_action"),
        ("WorkbenchReviewPage.tsx", "filter"),
    ):
        src = _code_only(_read(FRONTEND / "pages" / page))
        # **行首整行锚定,不是 `"useUrlFilters" in src`。**
        # 第一版是子串判定,而变异注释掉的是 import 那一行 —— 调用点里
        # 还留着这个名字,断言照样满足,守卫绿着而页面根本编译不过。
        # 这是「文件里出现过这串字 != 这行代码在生效」在本仓库的第四次出现
        # (batch13-3 的 M2 / M11、batch14-2 的 N15 是前三次)。
        # `[^}]*` 本来就跨行(字符类含 \n),所以多行 import 也咬得住
        assert re.search(
            r"^import\s*\{[^}]*\buseUrlFilters\b[^}]*\}\s*from", src, re.M
        ), f"{page} 没有 import useUrlFilters —— 不再从 URL 读筛选,首页卡片点进去等于没筛"
        assert re.search(r"=\s*useUrlFilters\(", src), (
            f"{page} import 了 useUrlFilters 却没有调用它"
        )
        # 参数名必须出现在**声明表**里,而不是文件里随便哪个位置:
        # 表里那一行才是"这一页认这个参数"的唯一依据
        spec = _url_filter_spec(src)
        assert param in spec, (
            f"{page} 的筛选表里没有 `{param}` —— 首页卡片带的条件会被当成不认识的参数丢掉"
        )


# ==========================================================
# 10:身份只探一次
# ==========================================================


def test_identity_is_probed_in_one_place():
    """`/auth/whoami` 只由 `useIdentity` 问一次,菜单不许自己再开一份。

    两处各写一份 `useQuery` 的表现:`enabled` 条件会分叉,于是横幅说
    "口令没问题"、菜单同时按"不是管理员"渲染 —— 两个都对着自己那份查询,
    而运营看到的是自相矛盾的界面。
    """
    layout = _code_only(_read(FRONTEND / "components" / "AppLayout.tsx"))
    assert "useIdentity()" in layout, "菜单不再用统一的那次身份探测"

    extra = re.findall(r"useQuery\s*(?:<[^>]*>)?\s*\(", layout)
    assert not extra, (
        f"AppLayout 里自己开了 {len(extra)} 个 useQuery —— "
        f"身份只能由 useIdentity 问一次,否则 enabled 条件会分叉"
    )
    assert "whoami" not in layout, "AppLayout 直接调了 whoami,绕过了 useIdentity"


# ==========================================================
# 11:后端零填充
# ==========================================================


def test_summary_by_next_action_is_zero_filled_for_the_homepage():
    """`by_next_action` 要把**每个**动作码都填成 0,不能只给非零的。

    只给非零的表现:首页那张卡在计数归零时**整张消失**,运营以为界面坏了;
    而 `?? 0` 这种前端兜底会把"后端漏了这个码"和"真的是 0"变成同一件事。
    """
    flow = _no_hash_comments(_read(APP / "workbench" / "flow.py"))
    assert re.search(
        r"by_action:\s*dict\[str,\s*int\]\s*=\s*\{\s*code\.value:\s*0\s+for\s+code\s+in\s+NextActionCode\s*\}",
        flow,
    ), (
        "by_next_action 不再对整个 NextActionCode 枚举零填充 —— "
        "少填的码在首页会表现成「那张卡不见了」"
    )

    tree = ast.parse(_read(APP / "workbench" / "flow.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.DictComp) and "NextActionCode" in ast.unparse(node):
            assert not node.generators[0].ifs, (
                "零填充的推导式上加了过滤条件 —— 被滤掉的那个码在首页会消失"
            )


# ==========================================================
# 12-14:离开保护(数据路由 + 四条出口 + 保存后放行)
# ==========================================================


def test_router_is_a_data_router_so_blocker_works():
    """必须是 `createBrowserRouter`,且布局路由要渲染 `Outlet`。

    **`useBlocker` 只在数据路由下可用。** 换回 `<BrowserRouter>` 的表现不是
    报错,而是离开保护整条**静默失效** —— 未保存的编辑被吞掉,
    而没有任何地方会告诉你保护没生效。这是本批最值得优先补的一条。
    """
    app = _app_tsx()
    assert "createBrowserRouter" in app, (
        "路由不是数据路由了 —— useBlocker 会静默失效,离开保护整条不生效"
    )
    main = _code_only(_read(FRONTEND / "main.tsx"))
    assert "RouterProvider" in main, "main.tsx 没有用 RouterProvider"
    assert "<BrowserRouter" not in main, "main.tsx 换回了非数据路由"

    layout = _code_only(_read(FRONTEND / "components" / "AppLayout.tsx"))
    assert "<Outlet />" in layout, "布局路由不渲染 Outlet —— 子路由一个字都不显示"


def test_unsaved_guard_covers_all_four_ways_out():
    """四种离开方式都要挡:菜单 / 标签 / 商品切换走 `useBlocker`,
    刷新与关窗走 `beforeunload`。

    只挡站内导航的表现:运营改完文案顺手按 F5,改动没了 ——
    而站内导航那三条一直好好的,所以这个洞很难被复现。
    """
    src = _code_only(_read(FRONTEND / "components" / "UnsavedGuard.tsx"))
    assert "useBlocker" in src, "站内导航不再被拦(菜单 / 标签 / 切商品三条出口)"
    assert re.search(r"window\.addEventListener\(\s*'beforeunload'", src), (
        "没有挂 beforeunload —— 刷新和关窗这两条出口是敞开的"
    )
    assert re.search(r"window\.removeEventListener\(\s*'beforeunload'", src), (
        "beforeunload 没有解绑,组件卸载后仍然会拦"
    )


def test_unsaved_guard_releases_when_the_edit_is_saved():
    """保存之后要放行挂起的那次导航。

    不放行的表现:运营点保存、弹窗消失,然后**页面还停在原地** ——
    他刚才点的那个菜单被永久挂起了,只能再点一次。
    """
    src = _code_only(_read(FRONTEND / "components" / "UnsavedGuard.tsx"))
    assert re.search(
        r"if\s*\(blocker\.state\s*===\s*'blocked'\s*&&\s*!dirty\)\s*blocker\.proceed\(\)", src
    ), "保存后不再放行挂起的导航 —— 运营点的那个菜单被永久挂起"


def test_every_editing_surface_is_guarded():
    """每个有未保存态的编辑面都要挂 `UnsavedGuard`,且 `dirty` 必须是真实的脏标记。

    传写死的 `true` 比不挂更糟:每一次离开都弹窗,运营会学会**无脑点确定** ——
    然后真正有未保存改动的那一次也被点掉了。
    """
    surfaces = {
        "components/workbench/CopyTab.tsx",
        "components/workbench/DraftTab.tsx",
        "components/workbench/AttributeTab.tsx",
        "components/workbench/ImageSetTab.tsx",
        "pages/PromptsPage.tsx",
        "pages/SettingsPage.tsx",
    }
    for rel in sorted(surfaces):
        src = _code_only(_read(FRONTEND / rel))
        mount = re.search(r"<UnsavedGuard\s+dirty=\{([^}]+)\}", src)
        assert mount, f"{rel} 没挂离开保护 —— 这个面上的未保存改动会被静默吞掉"
        expression = mount.group(1).strip()
        assert expression not in ("true", "false"), (
            f"{rel} 的 dirty 传了写死的 `{expression}` —— "
            f"恒真会训练运营无脑点确定,恒假等于没挂"
        )
        assert re.search(rf"\b{re.escape(expression.split('.')[0])}\b\s*[=,)]", src), (
            f"{rel} 的 dirty 表达式 `{expression}` 在文件里找不到定义"
        )
