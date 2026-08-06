"""A44 第三批:SPU 聚合链的四重截断 + N+1(A-01 ～ A-06)。

查询形状的断言在 `test_workbench_query_budget.py` 的 SPU 那一节
(翻页落在 SQL、循环里零库读、total 不取自本页)。这个文件放的是另外两类:

    口径     全量计数用的那份轻量行,判据必须和界面上那份是同一个函数
    前端     `limit: 100` + `pagination={false}` 这一对不许再长回来

## 为什么四条 bug 合成一批改

A-01 ～ A-05 是同一次读取路径上的四重截断,A-06 的 N+1 就在那个循环里:

    前端 limit: 100  ->  后端 aggregate(rows)[:limit]  ->  total = len(groups)
                                                       ->  inconsistent 同源

拆开修会连着动同一段代码三次,而且中间态更糟 —— 比如只修 `total` 而不修
截断,页面就会显示"共 512 个"却只列出 100 行,运营会以为是自己没往下翻。
"""
from __future__ import annotations

import ast
import re

from tests.pure._helpers import BACKEND_ROOT, window

APP = BACKEND_ROOT / "app"
FRONTEND = BACKEND_ROOT.parent / "frontend" / "src"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """去掉 TS/TSX 的注释再做字符串断言。

    第一版没有这一步,于是这个文件的第一次运行就红了 —— 红在**修复自己的
    注释**上:`pagination={false}` 和 `limit: 100` 都被写进了"改之前是什么样"
    的说明里。那是假红,而假红和假绿一样有害:下一个人会把断言删掉,
    而不是把注释改掉。

    (同一个陷阱在前两批里各出现过一次,一次假红一次假绿,见评审第九节。)

    `//` 前面带冒号的不动 —— 那是 `https://`,不是注释。
    """
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", without_block)


def _fn(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


# ==========================================================
# A-03:全量告警数的判据
# ==========================================================


def test_the_global_count_uses_the_same_judgement_as_the_page():
    """全量计数走的是 `spu_rules.aggregate` 那一份判据,不是第二份实现。

    `_inconsistent_spu_count` 为了省钱只给 `SkuRow` 装了 `attributes`
    (没有 completion / blocking / 颜色尺码)。这条钉的是**那样也够**:
    不一致判定只看属性,轻量行与完整行给出同一个结论。

    要是哪天有人为了再省一点,把判定改写成一句 SQL 或者一段 `set()` 比较,
    这条不会红 —— 但界面上的红字和角标就会开始各说各的(§3.4)。所以
    `_inconsistent_spu_count` 的 docstring 里写明了不下推的理由。
    """
    from app.workbench import spu as spu_rules

    def row(sku: str, **attrs: str) -> spu_rules.SkuRow:
        return spu_rules.SkuRow(product_id=sku, sku=sku, attributes=attrs)

    rows = [
        # 面料两个值 -> 不一致
        ("SPU-A", "", row("A-1", fabric="涤纶")),
        ("SPU-A", "", row("A-2", fabric="氨纶")),
        # 完全一致
        ("SPU-B", "", row("B-1", fabric="涤纶")),
        ("SPU-B", "", row("B-2", fabric="涤纶")),
    ]
    groups = spu_rules.aggregate(rows)
    assert len(groups) == 2
    assert sum(1 for g in groups if g.inconsistent) == 1, (
        "轻量行上的不一致判定和完整行不一致了"
    )


def test_a_missing_value_is_not_a_second_value():
    """"还没确认"不等于"确认成了别的" —— 否则刚导入的商品整页发红。

    这条是 `_consistency` 的既有语义(见 spu.py)。写在这里是因为全量计数
    现在**按这条语义数**:少了它,`inconsistent_spus` 会在一批新商品导入
    后突然从 3 跳到 500,而一件都没做错。
    """
    from app.workbench import spu as spu_rules

    rows = [
        ("SPU-C", "", spu_rules.SkuRow(product_id="1", sku="C-1", attributes={"fabric": "涤纶"})),
        ("SPU-C", "", spu_rules.SkuRow(product_id="2", sku="C-2", attributes={})),
    ]
    assert not spu_rules.aggregate(rows)[0].inconsistent


def test_variant_dimensions_never_count_as_inconsistent():
    """颜色/尺码在同一 SPU 下本来就该不同。

    数错方向的表现很好认:**每一个**多颜色 SPU 都变成红的,角标等于 SPU 总数。
    """
    from app.workbench import spu as spu_rules

    rows = [
        (
            "SPU-D",
            "",
            spu_rules.SkuRow(
                product_id="1", sku="D-1", attributes={"primary_color": "黑", "size": "S"}
            ),
        ),
        (
            "SPU-D",
            "",
            spu_rules.SkuRow(
                product_id="2", sku="D-2", attributes={"primary_color": "白", "size": "M"}
            ),
        ),
    ]
    assert not spu_rules.aggregate(rows)[0].inconsistent


# ==========================================================
# A-06:属性取数的两个版本必须同口径
# ==========================================================


def test_both_attribute_readers_share_one_value_formatter():
    """逐件版与批量版必须共用 `_display_attribute_value`。

    不一致判定是拿显示字符串做**相等比较**的。两处各写一份格式化
    (比如批量版少了 `json.dumps(ensure_ascii=False)`)的表现是:同一个
    SPU 在列表里一致、在角标口径里不一致,而两边都不报错。
    """
    tree = ast.parse(_read(APP / "api" / "workbench_batch.py"))
    for name in ("_confirmed_attributes", "_confirmed_attributes_bulk"):
        called = {
            node.func.id
            for node in ast.walk(_fn(tree, name))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "_display_attribute_value" in called, (
            f"{name} 没走公共的显示值格式化 —— 判据要分叉了"
        )


def test_the_endpoint_no_longer_reads_attributes_row_by_row():
    """`list_spus` 里不许再出现逐件的 `_confirmed_attributes`。

    查询预算那条(`test_spu_aggregation_reads_nothing_row_by_row`)从调用图
    数得到同一件事;这条是直接钉名字,红起来的时候更好读。
    """
    tree = ast.parse(_read(APP / "api" / "workbench_batch.py"))
    per_row = [
        node.lineno
        for node in ast.walk(_fn(tree, "list_spus"))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_confirmed_attributes"
    ]
    assert not per_row, f"list_spus 第 {per_row} 行又在逐件取属性(A-06)"


# ==========================================================
# A-01 / A-04:前端那一对
# ==========================================================


def test_the_spu_page_does_not_hardcode_a_row_cap():
    """`limit: 100` 不许长回来。

    它和 `pagination={false}` 是一对:单看任何一个都像合理写法,合起来
    才是"第 101 个 SPU 起在界面上不存在"。
    """
    page = _code_only(_read(FRONTEND / "pages" / "WorkbenchSpuPage.tsx"))
    assert "limit: 100" not in page, "SPU 页又硬编码了 limit: 100(A-01)"
    assert "page_size: pageSize" in page, "请求没带页宽,服务端会按默认值发货"

    # 只看**主表**那一段。展开行里的 SKU 子表关掉分页是对的:一个 SPU
    # 就那么几个 SKU,给它配页码条只会让展开行变高
    main_table = window(page, "<Table<SpuGroup>", "expandable=", what="SPU 主表那一段")
    assert "pagination={false}" not in main_table, (
        "主表又关掉了分页 —— 关掉之后 total 再准也没有页码条可点"
    )
    assert "total: query.data?.total" in main_table, (
        "页码条的总数不是服务端那个全量 total —— 用 items.length 的话永远只有一页"
    )

    client = _code_only(_read(FRONTEND / "api" / "batch.ts"))
    spus_decl = window(client, "spus: async", "audit: async", what="spus 客户端声明")
    assert "limit" not in spus_decl, (
        "api 客户端的 spus 参数表里还留着 limit —— 留着就还会有人传"
    )
    assert "page_size" in spus_decl and "page" in spus_decl


def test_the_sorting_comment_no_longer_claims_it_sorts_everything():
    """A-04:那句"客户端排序对的是全量"必须消失。

    它撒的谎在改完之后**更大**了(现在浏览器手上只有一页),所以列头上
    补了提示。这条钉的是注释与界面提示都在。
    """
    page = _read(FRONTEND / "pages" / "WorkbenchSpuPage.tsx")
    assert "客户端排序对的是全量" not in _code_only(page), "那句撒谎的注释还在(A-04)"
    assert "排序只作用于当前页" in page, (
        "列头没有告诉运营排序范围 —— 界面不能默认它排的是全量"
    )


# ==========================================================
# A-19:保存不许把字段清掉
# ==========================================================

#: 前端本地草稿里额外的行标识,不属于接口入参
_LOCAL_ONLY_DRAFT_FIELDS = {"key"}


def _ts_interface_fields(source: str, name: str) -> set[str]:
    """从 `export interface X { ... }` 里取字段名。

    只做到"够用":这个仓库的接口定义都是一层平铺的字面量,没有嵌套对象、
    没有方法签名。真要解析 TS 得起一个 node 进程,而这个文件是纯 Python 的。
    """
    head = source.index(f"interface {name} {{")
    body = source[head : source.index("\n}", head)]
    return set(re.findall(r"^\s{2}(\w+)\??:", body, flags=re.M))


def _fn_body(source: str, start: str, end: str) -> str:
    """从 `start` 截到它**之后**第一个 `end`。

    第一版写的是 `source.index(end)` —— 从文件开头找,于是 `toDraft` 那次
    截出了一段空字符串,断言报"七个字段全漏了"。假红的老朋友:
    它红得越吓人,越容易被人当成断言写坏了而直接删掉。
    """
    head = source.index(start)
    return source[head : source.index(end, head)]


def test_the_image_set_draft_round_trips_every_input_field():
    """服务端 item -> 本地 draft -> 提交 payload,字段集不许在中间少一个。

    A-19 的原始形状:`toDraft()` 与 `payload()` 都逐字段列举,两处都漏了
    `derivative_purpose`。而漏掉它**在界面上没有任何症状** ——

        保存 = 派生新版 = 整批重建 items
        后端 `item.get("derivative_purpose")` 取不到 -> 落 NULL

    也就是说,运营每点一次"保存编排",库里已有的派生用途就被清空一次,
    没有报错、没有提示,直到某次导出用错了图才会有人发现。

    这条就是评审第七节给 D-11 提的那一句:**字段集相等**。它不是在测
    某一个字段,是在测"下次给入参加字段时会不会又漏"。
    """
    types = _read(FRONTEND / "api" / "imageSets.ts")
    wanted = _ts_interface_fields(types, "ImageSetItemInput")
    assert "derivative_purpose" in wanted, (
        "入参类型里没有 derivative_purpose 了 —— 这条断言要跟着改"
    )

    tab = _code_only(_read(FRONTEND / "components" / "workbench" / "ImageSetTab.tsx"))
    for label, body in (
        ("toDraft", _fn_body(tab, "function toDraft(", "\n}\n")),
        ("payload", _fn_body(tab, "const payload =", "const save =")),
    ):
        missing = sorted(
            field
            for field in wanted - _LOCAL_ONLY_DRAFT_FIELDS
            if not re.search(rf"\b{field}:", body)
        )
        assert not missing, (
            f"{label}() 漏掉了 {missing} —— 保存会把这些字段清成 NULL(A-19)"
        )


# ==========================================================
# A-20:两个上传上限不许交叉
# ==========================================================


def test_nginx_accepts_everything_the_backend_is_willing_to_take():
    """网关的 body 上限必须大于后端的上传上限。

    A-20 的原始形状:nginx `16m`,后端 `MAX_UPLOAD_SIZE_MB = 20`。17–20MB
    的素材在**生产**被 nginx 用一张 HTML 413 拦下,前端拿不到后端那个带
    error code 的 JSON;开发直连 Vite 不过 nginx,所以本地怎么试都是好的。

    钉的是**关系**不是数字:谁把后端上限调到 30 而没动 nginx,这条会红。
    留余量是因为 multipart 有边界和文件名开销,正好卡齐会让临界文件随机失败。
    """
    config = ast.parse(_read(APP / "core" / "config.py"))
    backend_mb = next(
        node.value.value
        for node in ast.walk(config)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "MAX_UPLOAD_SIZE_MB"
        and isinstance(node.value, ast.Constant)
    )

    nginx = _read(BACKEND_ROOT.parent / "frontend" / "nginx.conf")
    caps = re.findall(r"^\s*client_max_body_size\s+(\d+)m;", nginx, flags=re.M)
    assert len(caps) == 1, f"nginx.conf 里有 {len(caps)} 处 client_max_body_size,预期 1 处"
    gateway_mb = int(caps[0])

    assert gateway_mb > backend_mb, (
        f"nginx 只收 {gateway_mb}MB,而后端愿意收 {backend_mb}MB —— "
        f"中间那 {backend_mb - gateway_mb}MB 会在生产变成一张 HTML 413(A-20)"
    )


# ==========================================================
# A-25 / A-26 / B-01:多颜色图片闭环
# ==========================================================

_IMAGE_SET_TAB = "components/workbench/ImageSetTab.tsx"


def test_the_row_key_matches_the_backend_uniqueness_key():
    """前端行标识必须带上 `variant_id`(A-25)。

    后端唯一键是 `(image_set_id, media_asset_id, variant_id)`,`0016` 那条
    唯一索引对 NULL 走 `COALESCE(variant_id, '')` —— **同一张素材绑两个
    不同变体是合法数据**。前一版把 key 写成裸 `media_asset_id`,那种数据
    一进来就是 React 重复 key,而且行操作按 key 过滤会一次命中两行。

    归一口径也要一致:前端 `variant_id ?? ''`,入参校验
    `schemas/image_set.py` 里是 `item.variant_id or ""`。
    """
    tab = _code_only(_read(FRONTEND / _IMAGE_SET_TAB))
    assert "function rowKey(" in tab, "行标识没有收敛成一个函数,三处各拼各的迟早对不上"
    assert "${mediaAssetId}#${variantId ?? ''}" in tab, (
        "rowKey 没有把 variant_id 归一进去 —— 同素材绑两个变体又会撞 key(A-25)"
    )
    assert "key: asset.id," not in tab, "add() 又用裸素材 id 当行标识了"

    schema = _read(APP / "schemas" / "image_set.py")
    assert 'item.variant_id or ""' in schema, (
        "后端的归一口径变了,前端 rowKey 要跟着改 —— 两边不一致时唯一性判断会分叉"
    )


def test_row_actions_are_addressed_by_index_not_by_key():
    """行操作一律按下标,不许按 key 过滤(A-25)。

    **退化成什么样:** 谁把 `remove` 改回 `items.filter((i) => i.key !== key)`。
    在同素材绑两个变体的集上,那一行删除会删掉两行 —— 而两行长得一模一样,
    运营只会以为自己点重了,不会报 bug。
    """
    tab = _code_only(_read(FRONTEND / _IMAGE_SET_TAB))
    for pattern in ("i.key !== key", "i.key === key", "i.key === dragKey"):
        assert pattern not in tab, f"行操作又按 key 过滤了({pattern})"
    assert "const remove = (index: number)" in tab, "remove 不是按下标定位的"
    assert "const setPrimary = (index: number)" in tab, "setPrimary 不是按下标定位的"
    assert "const setRole = (index: number" in tab, "setRole 不是按下标定位的"


def test_there_is_a_row_level_variant_selector():
    """A-26 / B-01:每一行都能选颜色变体。

    这是 B-01 缺的那个入口。在它之前 `variant_id` 恒为 null,而覆盖横幅会
    告诉运营「这个 SPU 有 3 个颜色,这一版绑定了 0 个」—— 一个说明问题却
    无处可点的提示。后端从 A42 起就支持,前端一直没接。
    """
    tab = _code_only(_read(FRONTEND / _IMAGE_SET_TAB))
    assert "variant_id: null," in tab, "默认值应当仍是通用图(单色 SPU 不该被平白打标签)"
    assert "const setVariant = (index: number" in tab, (
        "没有行级变体绑定入口 —— B-01 的闭环还是断的(A-26)"
    )
    assert "onChange={(value) => setVariant(index, value || null)}" in tab, (
        "变体下拉没有接到 setVariant 上"
    )
    assert "coverage?.required ?? []" in tab, (
        "下拉选项不是来自 coverage.required —— 自己编一份变体清单会和后端对不上"
    )


def test_the_variant_selector_submits_keys_not_colour_names():
    """下拉的**值**是稳定 key,显示的才是当前颜色名。

    `resolve_ref` 的第一级是 key 完全相等;颜色名那一级在两个变体撞名时
    返回 None(不猜)。所以提交身份、显示名字。撞名时下拉里还要能分得开,
    否则界面上出现两个「红色」,选错要到平台侧才暴露。
    """
    tab = _code_only(_read(FRONTEND / _IMAGE_SET_TAB))
    for anchor in ("const setVariant", "const duplicateForVariant", "{ value: '', label: '通用图"):
        assert anchor in tab, (
            f"找不到锚点 `{anchor}` —— 变体绑定入口还没接上,或者被改名了(A-26)"
        )
    selector = tab[tab.index("const setVariant") : tab.index("const duplicateForVariant")]
    assert "patch(index, { variant_id: variantId })" in selector, (
        "setVariant 落库的不是传进来的 key"
    )
    options = tab[
        tab.index("{ value: '', label: '通用图") : tab.index(
            "<Tooltip title={item.is_primary"
        )
    ]
    assert "value: key," in options, "下拉的 value 不是稳定 key"
    # A45-#23 之后,下拉与横幅共用 `distinctNameOfVariant()`。**断言钉的是
    # "走了显示名函数且那个函数会消歧",不是某一个函数名的字面量** ——
    # 第一版钉的是 `nameOfVariant(` + `labelCollisions.includes(key)` 这两串,
    # 于是把内联逻辑抽成共用函数(正确的修法)会让它当场变红。
    # 本仓第五次撞见门禁钉写法而不是不变量。
    assert "distinctNameOfVariant(" in options or "nameOfVariant(" in options, (
        "下拉显示的是 key 而不是当前颜色名"
    )
    helper = tab[tab.index("function distinctNameOfVariant") :]
    helper = helper[: helper.index("\n}\n")]
    assert "collisions.includes(key)" in helper and "key.slice(" in helper, (
        "两个变体同名时下拉里分不开 —— 选错的后果要到平台侧才暴露"
    )
    # 横幅必须走同一个函数,否则又是两份实现(A45-#23 的原始形态)
    assert tab.count("distinctNameOfVariant(") >= 3, (
        "同名消歧没有被下拉与横幅共用 —— 同一页里一处分得开一处分不开"
    )


def test_binding_the_same_pair_twice_is_refused_before_the_request():
    """(素材, 变体) 这一对重复时前端就拦下。

    后端会 422(`第 N 项重复`),但那要等到点保存 —— 而运营那时已经拖完了
    十几行。这条不是替代后端校验,是把它提前到发生的那一刻。
    """
    tab = _code_only(_read(FRONTEND / _IMAGE_SET_TAB))
    assert "const taken = (" in tab, "没有 (素材, 变体) 去重判断"
    assert "(i.variant_id ?? '') === (variantId ?? '')" in tab, (
        "去重判断没有按 '' 归一 —— NULL 互不相等,漏判的正是通用图那一行"
    )
    assert "taken(items[index].media_asset_id, variantId, index)" in tab, (
        "setVariant 没有在改之前查重"
    )


def test_approving_an_all_generic_multicolour_set_asks_first():
    """B-01 第三块:批准之前把代价说清楚,但**不硬阻断**。

    硬阻断会让每一个多色 SPU 立刻无法批准(它们的图全是通用图)——
    那不是修复是停产,`variant_coverage` 的 docstring 里写的就是这条。
    A-26 之后"绑不了"这个理由不成立了,所以从默默放行改成知情放行。

    要不要升级成硬阻断是业务规则决定,取决于存量集要不要先补绑一轮。
    """
    tab = _code_only(_read(FRONTEND / _IMAGE_SET_TAB))
    approve_block = tab[tab.index("disabled={!canApprove}") : tab.index("批准图片集")]
    assert "coverage.variant_binding_missing" in approve_block, (
        "批准按钮完全不看变体覆盖 —— B-01 的第三块还没接上"
    )
    assert "modal.confirm(" in approve_block, "全是通用图时批准没有二次确认"
    can_approve_block = tab.split("canApprove =")[1].split("\n\n")[0]
    assert "canApprove" in tab and "variant_binding_missing &&" not in can_approve_block, (
        "覆盖缺口被写进了 canApprove —— 那是硬阻断,会让所有多色 SPU 立刻批不了"
    )
