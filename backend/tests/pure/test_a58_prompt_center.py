"""a58 守卫:提示词管理中心的前端半(FE-301~303)与男装可达性(AC-24)。

## 这一批为什么只读源码

没有 node_modules 的环境里 `tsc` / ESLint / Vitest 一个都跑不动,而这一轮改的
恰恰是前端。所以这里的断言只能是源码形状 —— **并且要写清它守不住什么**:
守不住渲染是否正确、类型是否对得上、点下去会不会报错。它守得住的是一类具体
的回归:**「后端通、前端不可达」那个中间态换个位置重新出现。**

`docs/DECISIONS.md` §3.87 立过一条规矩:「凡是"做了什么"能被观察的,就别去比
代码长什么样」。这里不违反它 —— 前端行为在这个环境里**观察不到**,源码形状是
仅有的证据。等 `make fe-check` 跑得动的那天,这几条该被行为测试取代,而不是
叠加;哪条被取代了,在这里删掉并写明为什么删(照 LG-410 的做法)。

## 男装那条是本批的核心

a56 把 `vision_system_prompt_men` 标成 `ui_reachable=False`,并写着「解锁条件 =
FE-301 落地」。a58 翻转它。**翻转的判据是"同一段代码",不是"页面上能看见它"** ——
详情页里只要还留着一处写死的女装 key,男装那条路径就是没人走过的代码,而 True
会把「后端通、前端不可达」换成一个更糟的状态:看起来可达、点进去出错。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from app.prompts import registry

BACKEND = Path(__file__).resolve().parents[2]
FRONTEND = BACKEND.parent / "frontend" / "src"


def _strip_comments(source: str) -> str:
    """去掉 `/* */` 与 `//` 注释,只留会被执行的部分。

    粗糙但够用:它会误伤字符串里的 `//`(比如 URL),而本批的断言只关心
    `vision_system_prompt` 这类标识符 —— 误伤方向是**多剥**,也就是更容易漏报
    而不是更容易假红。假红最省事的消法是把整条门禁删掉(§3.37),所以偏这一边。
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//[^\n]*", "", source)


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


# ============================================================ AC-24:男装可达


def test_the_mens_prompt_is_reachable_now_that_the_page_is_key_driven():
    """§14.1 那笔欠账的还款凭据。"""
    men = registry.surface_of("vision_system_prompt_men")
    assert men is not None
    assert men.editable is True
    assert men.ui_reachable is True, "AC-24 未达成:男装仍然前端不可达"


def test_the_detail_page_hardcodes_no_prompt_key_at_all():
    """**本批最要紧的一条。**

    `ui_reachable=True` 的全部依据是"两份提示词走同一段代码"。详情页里只要
    还留着一处写死的 key,男装那条路径就是没人走过的代码 —— 而那时候注册表上
    那个 True 是一句错话,比原来的 False 更糟:它看起来可达。

    连 `VISION_SYSTEM_PROMPT` 这个常量名也不许在详情页出现:引用它就意味着
    某处仍然在假设"默认就是女装",而那个假设正是这一轮要拆掉的。
    """
    # **先剥注释再判。** 注释里提到这个词是在讲这一页的历史,而
    # 「不许在注释里提到自己改掉了什么」会把 §3.33(过期的理由比没有理由更糟)
    # 反过来执行。这条断言的两个更朴素的版本都被自己的注释判过红:
    # 裸子串一次,加引号一次(注释里用反引号引代码,那是本仓的文档习惯)。
    code = _strip_comments(_read("pages/PromptsPage.tsx"))
    assert "vision_system_prompt" not in code, "详情页的代码里仍有写死的提示词 key"
    assert "VISION_SYSTEM_PROMPT" not in code, "详情页仍在引用女装常量"
    assert "useParams" in code, "详情页没有从路由取 key"


def test_every_editable_and_reachable_surface_has_a_way_in():
    """能改的每一处,列表页都要给得出入口。

    反过来也钉:`editable=False` 的项**不许**给入口 —— 给了得到的是
    「保存成功、毫无效果」,比不给更伤人,因为它连报错都没有。
    """
    source = _read("pages/PromptListPage.tsx")
    link = "to={`/prompts/${row.key}`}"
    assert link in source, "列表页的入口不是按 key 拼的"
    # **判的是"这个入口被那个条件挡着",不是"这个文件里出现过那个条件"。**
    # 第一版写的是 `"row.editable && row.ui_reachable" in source` —— 而同一个
    # 表达式在「状态」列里也出现一次,于是把链接那处的条件改成恒真,子串照样在、
    # 守卫照样绿,而不可编辑的项从此都有了一个保存成功却毫无效果的入口。
    # 与 a57 的 M10、batch13-3 的 M11 同族,连着两轮由变异抓出来。
    before = source[: source.index(link)]
    gate = before.rindex("row.editable && row.ui_reachable")
    assert len(before) - gate < 200, (
        "入口没有被 editable+ui_reachable 挡着 —— 条件在别处出现不算数"
    )


# ============================================================ FE-301 列表


def test_the_list_page_renders_every_tier_the_registry_can_produce():
    """注册表新增一个 tier 而列表页不认,那一组会整组消失,且不报错。"""
    source = _read("pages/PromptListPage.tsx")
    tiers = {surface.tier.value for surface in registry.PROMPT_SURFACES}
    declared = set(re.findall(r"^  (FREE|TEMPLATE|MAPPING):", source, re.M))
    missing = tiers - declared
    assert not missing, f"列表页没有这些层的文案:{sorted(missing)}"
    order = re.search(r"TIER_ORDER: PromptTier\[\] = \[([^\]]+)\]", source)
    assert order, "找不到分组顺序"
    assert set(re.findall(r"'(\w+)'", order.group(1))) >= tiers, (
        "分组顺序漏了某一层 —— 漏掉的那组不会显示,而且不会报错"
    )


def test_the_list_page_does_not_invent_the_call_statistics_column():
    """PRD FE-301 要「近 7 天调用次数」,而后端刻意还没给。

    `list_prompts` 的文档写着「先不给一个算不准的数」。前端不推测状态
    (硬规则第 4 条):后端不给就不显示,**不拿别的字段凑一个近似值**。
    一个凑出来的运营指标,比没有指标贵得多 —— 没有指标时人会去问,
    有一个错指标时人会照着它做决定。
    """
    source = _read("pages/PromptListPage.tsx")
    assert "近 7 天" not in source or "没有「近 7 天调用次数」这一列" in source
    assert "解析失败率" not in source.replace("PRD FE-301 要它", "")


def test_the_reason_shown_for_a_locked_surface_comes_from_the_registry():
    """不在前端重写一份说辞 —— 两份说辞迟早分叉,而分叉时读的人不知道信哪份。"""
    source = _read("pages/PromptListPage.tsx")
    assert "editable=False 的原因" in source, "列表页没有从注册表取原因"
    for surface in registry.PROMPT_SURFACES:
        if surface.editable:
            continue
        blob = "".join(surface.consumers)
        # **标记必须同形。** a56 有一处写的是 `editable=False:`(少了"的原因"),
        # 于是前端按标记取原因取到 null,界面显示一句兜底的抱歉 —— 而注册表里
        # 明明写着理由。一个被五种写法表达的机器可读标记,只是名义上可读
        assert "editable=False 的原因" in blob, (
            f"{surface.key} 不可编辑却没写原因(或标记写法与其余不同形)"
            " —— 列表页会显示一句兜底的抱歉"
        )


# ============================================================ FE-303 单版本只读


def test_the_version_view_can_only_read():
    """「想看一眼」和「想让它生效」在这之前是同一个动作。

    唯一能看到 v3 正文的办法是把它切成生效版,而那会立刻改掉线上每一张图的
    评分口径。所以这一页存在 —— 而它一旦长出一个"切到这版"的按钮,这个理由
    就没了:把切换放在一个标题写着"只读"的页面上,是在训练人忽略这一页的名字。
    """
    source = _read("pages/PromptVersionPage.tsx")
    for forbidden in ("promptsApi.activate", "promptsApi.save", "promptsApi.reset"):
        assert forbidden not in source, f"只读页调用了 {forbidden}"
    assert "promptsApi.readVersion" in source


def test_a_non_numeric_version_in_the_url_is_caught_before_the_request():
    """`Number('abc')` 是 NaN。不拦的话会去请求 `/versions/NaN`,拿回 404,

    而界面会说"读取失败" —— 那句话是对的但没用:错在地址,不在服务端。
    """
    source = _read("pages/PromptVersionPage.tsx")
    assert "Number.isInteger(version)" in source
    assert "enabled: versionValid" in source


def test_the_detail_page_links_out_to_the_read_only_view():
    source = _read("pages/PromptsPage.tsx")
    assert "/versions/${row.version}" in source, "版本表里没有「查看」入口"


# ============================================================ 路由与导航


def test_the_three_routes_exist_and_only_the_list_is_a_nav_entry():
    """PRD §12.1:复用「系统管理」组现有位置,**不新增侧栏条目**。

    详情与单版本是从列表点进去的,不是导航目标。把它们加进侧栏,运营会看到
    两个指向同一件事的入口,而其中一个还需要先知道 key 才点得动。
    """
    app = _read("App.tsx")
    for path in (
        'path="/prompts" element={<PromptListPage />}',
        'path="/prompts/:key" element={<PromptsPage />}',
        'path="/prompts/:key/versions/:version" element={<PromptVersionPage />}',
    ):
        assert path in app, f"路由缺失:{path}"
    nav_entries = re.findall(r"\{ key: '(/prompts[^']*)'", app)
    assert nav_entries == ["/prompts"], f"侧栏多出了提示词条目:{nav_entries}"


def test_the_list_route_is_matched_before_the_detail_route():
    """react-router v6 按具体度排序,不靠书写顺序 —— 但人读代码靠顺序。

    这一条守的是可读性之外的东西:三条路由必须**都在**,而且 `/prompts` 那条
    指向列表页。指向详情页的话,`useParams` 拿到 undefined,页面会去请求
    `/prompts/`,而那个地址在后端是列表接口,返回的形状对不上 —— 表现是白屏。
    """
    app = _read("App.tsx")
    list_at = app.index('path="/prompts" element={<PromptListPage />}')
    detail_at = app.index('path="/prompts/:key" element={<PromptsPage />}')
    assert list_at < detail_at


# ============================================================ 前后端契约


def _list_prompts_shape() -> tuple[set[str], set[str]]:
    """`list_prompts` 的(行字段, 信封字段)。

    ## 为什么用 AST,以及这里为什么分两层

    第一版把函数体里所有 `"键":` 都当成**行**字段,只把 `surfaces` 排掉 ——
    那在端点只返回 `{"surfaces": [...]}` 时是对的,而 a70 给信封加了
    `stats_window_days` / `stats_unattributed` / `stats_since` 三个同级键
    (窗口天数与未归属计数是整个列表一份的,不属于任何一行)。于是那一版
    把信封字段判成了行字段,报 `PromptSurface` 少了它们。

    **修的是守卫的模型,不是放宽它。** 分开之后两侧各自比对,比原来严:
    信封字段漏进 `PromptSurfaceList` 现在也会红,而上一版根本不看那个接口。
    """
    api_source = (BACKEND / "app" / "api" / "prompts.py").read_text(encoding="utf-8")
    tree = ast.parse(api_source)
    func = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "list_prompts"
    )

    def _dict_keys(node: ast.AST) -> set[str]:
        return (
            {
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if isinstance(node, ast.Dict)
            else set()
        )

    row: set[str] = set()
    envelope: set[str] = set()
    for node in ast.walk(func):
        # `item = {...}` 的字面量,外加 `item["stats"] = ...` 这类后补的键
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "item":
                    row |= _dict_keys(node.value)
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "item"
                    and isinstance(target.slice, ast.Constant)
                ):
                    row.add(target.slice.value)
        elif isinstance(node, ast.Return):
            envelope |= _dict_keys(node.value)
    envelope.discard("surfaces")  # 它的元素类型由 row 那一半管
    assert row and envelope, "没解析出 list_prompts 的返回形状,守卫形状变了"
    return row, envelope


def _declared_fields(interface: str, until: str) -> set[str]:
    types_source = _read("api/types.ts")
    iface = types_source[
        types_source.index(f"export interface {interface} {{") : types_source.index(until)
    ]
    return set(re.findall(r"^  (\w+)\??:", iface, re.M))


def test_the_frontend_type_matches_what_the_list_endpoint_returns():
    """接口加一个字段而类型没跟,前端拿得到但读不出来;反过来则是白等一个不存在的键。"""
    row, _envelope = _list_prompts_shape()
    declared = _declared_fields("PromptSurface", "export interface PromptSurfaceList")
    missing = row - declared
    assert not missing, f"PromptSurface 少了后端会返回的字段:{sorted(missing)}"


def test_the_frontend_type_matches_the_list_envelope():
    """信封字段(整个列表一份的那些)同样要对得上。

    a70 加的三个都在这一层:窗口天数、未归属计数、统计起点。少了它们前端
    读不出「近 N 天」的 N,只能自己写死一个 —— 而写死的那个和后端分叉时
    界面会用另一个窗口的数字说「近 7 天」。
    """
    _row, envelope = _list_prompts_shape()
    declared = _declared_fields("PromptSurfaceList", "export interface PromptVersionDetail")
    missing = envelope - declared
    assert not missing, f"PromptSurfaceList 少了后端会返回的字段:{sorted(missing)}"


def test_the_version_detail_type_matches_the_endpoint():
    api_source = (BACKEND / "app" / "api" / "prompts.py").read_text(encoding="utf-8")
    block = api_source[
        api_source.index("def read_prompt_version") : api_source.index("def read_prompt(")
    ]
    keys = set(re.findall(r'^\s+"(\w+)":', block, re.M))
    types_source = _read("api/types.ts")
    iface = types_source[types_source.index("export interface PromptVersionDetail {") :]
    iface = iface[: iface.index("}")]
    declared = set(re.findall(r"^  (\w+)\??:", iface, re.M))
    missing = keys - declared
    assert not missing, f"PromptVersionDetail 少了后端会返回的字段:{sorted(missing)}"


def test_the_api_client_exposes_both_new_endpoints():
    source = _read("api/prompts.ts")
    assert "list: async ()" in source
    assert "readVersion: async (" in source
    assert "'/prompts'" in source
    assert "/prompts/${key}/versions/${version}" in source
