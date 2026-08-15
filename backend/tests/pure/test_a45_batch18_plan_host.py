"""A45-batch18 守卫:方案面板的宿主页,以及它依赖的那把主键。

## 这一批还的是哪一笔账

阶段 4 交付第一项「GenerationPlan 实体与 UI」的 UI 那一瓣。面板与 API 客户端
从 A45-batch14-20 起就写完了,随后**四批全树零 import**。三次交接给出的理由
各不相同,而只有最后一次是对的:

    14-20   「跑不了 tsc / Vitest」          —— 真的,但那是环境,不是这一笔
    14-23   「等有人写一行 import」          —— **错的**,无处可写
    14-23 后「缺一个知道 SPU 主键的宿主页」  —— 对的,本批落的就是它

第二条值得记:照着它去做的人会发现无处可写,然后多半随手把 `spu` 字符串码
传进 `spuId`,那时接口 422,而错因指向后端。**一条过期的理由比没有理由更糟**
(§3.33)。

## 这里的守卫分三段,因为这一类返工有三种断法

`test_publish_frontend_entry.py` 顶部那张分工表写过同一件事:一个能力算
「已交付」要同时满足**有客户端 / 有页面 / 路由挂上了**。本批多守一段:
**入口** —— 页面挂了路由但没有任何一处链得到它,和没有页面在运营眼里是
同一件事,而两者都不会报错。

可达性本身(点下去真的渲染出来)归 Playwright,纯层不碰。
"""
from __future__ import annotations

import ast
import re

from app.workbench import spu as spu_rules
from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT, braced_block, window

FRONTEND = PROJECT_ROOT / "frontend" / "src"


def _row(sku: str, spu_id: str | None) -> spu_rules.SkuRow:
    return spu_rules.SkuRow(product_id=f"p-{sku}", sku=sku, spu_id=spu_id)


def _group(*rows: spu_rules.SkuRow) -> spu_rules.SpuGroup:
    (group,) = spu_rules.aggregate(("SW-001", "泳衣", row) for row in rows)
    return group


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(path) -> str:
    """剥掉 TS/TSX 的注释再断言。

    **这一条是变异验证逼出来的。** 第一版直接查整份源码里有没有
    `GenerationPlanPanel`,而变异 C2 把那行 import 注释掉之后
    `// import GenerationPlanPanel ...` 照样命中 —— 守卫绿着,面板已经不在页面上。

    同一个坑在本仓是第四次:batch13-3 的 M11(JSX 渲染块死代码化)、
    batch14-2 的 N15(类型里删字段)、14-22 的 L5(被同一函数里另一段对的代码
    喂真)。前三次的修法都是"把断言锚得更死";这次换成**从输入里把注释去掉**,
    因为解释性文字本来就不该参与判定 —— 锚点再准也挡不住下一段刚好提到它的注释。
    """
    source = path.read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


# ================================================ 一、主键的收敛(穷举)


def test_a_group_reports_the_primary_key_its_rows_agree_on():
    """正常形状:建档路径建出来的一组,每行都挂着同一个 SPU 主键。"""
    group = _group(_row("SW-001-BLK-S", "u-1"), _row("SW-001-BLK-M", "u-1"))
    assert group.spu_id == "u-1"


def test_a_group_whose_rows_disagree_reports_no_key_instead_of_picking_one():
    """**这是这个属性存在的理由。**

    两行商品共用一个字符串码却指向不同主键(§4.1 的唯一约束不允许,但约束在
    库里、这个函数在纯层)。挑一个出来的代价不是"显示错一个 id":运营点进去
    看到的是另一个款,方案面板会照着那个主键列方案、启用、把**别人的**
    图片集判过期 —— 每一步都不报错。

    与 `FieldConsistency.value` 同一条口径:不一致时返回 None,不猜。
    """
    group = _group(_row("SW-001-BLK-S", "u-1"), _row("SW-001-RED-S", "u-2"))
    assert group.spu_id is None


def test_a_group_with_no_keys_at_all_does_not_invent_one():
    """老建档路径(`create_product` / CSV 导入)建的行今天仍可能没有这一列。"""
    assert _group(_row("SW-002-BLK-S", None), _row("SW-002-BLK-M", None)).spu_id is None


def test_an_empty_group_is_not_reachable_but_the_property_still_answers():
    """`aggregate()` 不产出空组,但属性不许因此假设 `skus` 非空。

    直接构造一个空组:它是 `SpuGroup` 的合法取值(冻结 dataclass 谁都能造),
    而 `spu_id` 在那上面必须给 None 而不是抛 StopIteration。
    """
    empty = spu_rules.SpuGroup(
        spu="SW-000", name="", skus=(), common=(), inconsistent=(), variants={}
    )
    assert empty.spu_id is None


def test_a_half_migrated_group_still_reports_the_key_the_migrated_rows_carry():
    """**混合期是给主键的,不是给 None 的 —— 这是一个决定。**

    同一个字符串码下,一部分行已经带上 `spu_id`、另一部分还没有。两种选法:

        给主键   链接可用,运营配得了方案;没主键的那几行只是还没迁完
        给 None  整个 SPU 的方案入口消失,而它明明已经建过档了

    选后者的代价更贵且更隐蔽:迁移是逐批做的,中间态可能持续很久,而那期间
    这一页会说"这个 SPU 配不了方案",没有任何地方解释为什么。

    前提是**非空的那些必须一致** —— 不一致时上一条守卫接管,仍然给 None。
    """
    group = _group(_row("SW-003-BLK-S", "u-9"), _row("SW-003-BLK-M", None))
    assert group.spu_id == "u-9"


def test_the_serialized_row_carries_both_the_code_and_the_key():
    """出参并排给出两样东西,不合并成一个字段。

    前端要判"这一行点不点得进去",判据是主键在不在,不是码长什么样。
    合并的话前端只能去猜哪一种字符串是 UUID —— 又一份判定,又一处会漂。
    """
    (row,) = spu_rules.serialize([_group(_row("SW-001-BLK-S", "u-1"))])
    assert row["spu"] == "SW-001"
    assert row["spu_id"] == "u-1"

    (legacy,) = spu_rules.serialize([_group(_row("SW-002-BLK-S", None))])
    assert legacy["spu_id"] is None, "老建档路径那一行必须如实给 null"


# ================================================ 二、取数侧真的传了那一列


def test_the_aggregate_endpoint_passes_the_real_column():
    """`list_spus` 构造 `SkuRow` 时必须写 `product.spu_id`。

    这一条是 §3.38 的形状:列落库了、判定读着,而**没有人写它**。差别是
    这次的"写"不在数据库侧而在取数侧 —— 恒为 None 的效果一样:
    聚合页每一行都判成"老建档路径",链接全部消失,而接口 200、页面正常。

    按 AST 找而不是查字符串:`product.spu_id` 这几个字在同一个文件里还出现在
    别处(14-26 的 P1、14-22 的 L5 都栽在"被同一函数里另一处对的代码喂真")。
    """
    source = _read(BACKEND_ROOT / "app" / "api" / "workbench_batch.py")
    tree = ast.parse(source)
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "list_spus"
    ]
    assert len(functions) == 1, "找不到 list_spus —— 守卫失锚,不是代码对了"

    constructions = [
        node
        for node in ast.walk(functions[0])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "SkuRow"
    ]
    assert constructions, "list_spus 里没有构造 SkuRow —— 守卫失锚"

    for call in constructions:
        keywords = {kw.arg: kw for kw in call.keywords}
        assert "spu_id" in keywords, (
            "SkuRow 构造点没写 spu_id —— 出参那一列会恒为 null,"
            "而聚合页的链接会全部消失且不报错"
        )
        segment = ast.get_source_segment(source, keywords["spu_id"].value) or ""
        assert "product.spu_id" in segment, (
            f"spu_id 的来源不是 products.spu_id,而是 `{segment}` —— "
            "拿字符串码顶主键会让前端把它塞进需要 UUID 的接口,得到 422"
        )


# ================================================ 三、宿主页:欠账还清


def test_the_plan_panel_has_a_host_page_and_it_is_routed():
    """四段分开断言:有客户端 / 有面板 / 有宿主页 / 路由挂上了。

    分开写是为了让漏掉的那一步能被点名。这类返工最常见的形态就是
    "页面写完了,忘了挂路由" —— 那时它在仓库里存在、在界面上不存在。
    """
    client = FRONTEND / "api" / "generationPlans.ts"
    panel = FRONTEND / "components" / "GenerationPlanPanel.tsx"
    host = FRONTEND / "pages" / "SpuDetailPage.tsx"
    assert client.exists(), "没有生成方案的前端客户端"
    assert panel.exists(), "没有方案面板"
    assert host.exists(), "面板没有宿主页 —— 这正是本批要还的那笔账"

    host_source = _code_only(host)
    # 钉的是 **import 语句**,不是"这个名字出现过"。变异 C2 证过后者不成立:
    # 把 import 注释掉之后,JSX 里的 `<GenerationPlanPanel .../>` 照样让
    # 断言为真 —— 而那个组件此刻根本不存在(tsc 会红,纯层看不见)。
    # 「出现了某个名字」不等于「那个名字是从这里来的」,与 14-11 的 W2 同型。
    assert "from '../components/GenerationPlanPanel'" in host_source, (
        "宿主页没有 import 面板 —— 面板又回到了那个写完了但进不去的状态"
    )
    assert "<GenerationPlanPanel" in host_source, "import 了但没有渲染它"
    assert "spusApi.get" in host_source, (
        "宿主页没有按主键取 SPU —— 那它手上的 spuId 只能是路由参数原样透传,"
        "而面板需要的是一个**存在**的 SPU"
    )

    app = _code_only(FRONTEND / "App.tsx")
    assert 'path="/spus/:spuId"' in app, "宿主页没有挂路由 —— 页面存在但走不到"
    assert "SpuDetailPage" in app, "App.tsx 没有 import 宿主页"


def test_the_operator_can_actually_reach_the_host_page():
    """有两条入口,而且都不是"知道 URL 才进得去"。

    只有路由没有入口,和没有页面在运营眼里是同一件事。两条各管一段动线:

        建档之后   刚建完的那个款,主键在手上(`created.id`)
        聚合页     已经存在的款,主键由后端在出参里给
    """
    create_page = _code_only(FRONTEND / "pages" / "SpuCreatePage.tsx")
    assert "/spus/${created.id}" in create_page, (
        "建档完成页没有通向方案配置的入口 —— 那是全站唯一一处一定拿得到"
        "新 SPU 主键的地方"
    )

    aggregate = _code_only(FRONTEND / "pages" / "WorkbenchSpuPage.tsx")
    assert "/spus/${row.spu_id}" in aggregate, "聚合页没有链到宿主页"


def test_the_aggregate_page_links_with_the_key_not_the_code():
    """**反向断言,窗口封闭。** 链接模板里不许出现 `row.spu`。

    退化回去最省事的路径不是删链接,是把 `row.spu_id` 换成 `row.spu`
    (两者在这一页上都拿得到,而后者恒非空,于是链接**每一行都会出现**)。
    那之后页面看起来更完整了,点进去是 422,而错因指向后端。

    窗口取 SPU 那一列的 `render` 块:整文件读的话,表格里正常显示的
    `{row.spu}` 会把这条断言喂假,守卫红在完全正确的代码上。

    **地标带上 `/spus/` 是后加的(2026-08-15)。** 原来只锚
    `<Link className="mono" to={`,而那时这一页只有这一条这样的链接。
    前端评审 P1-5 把 SKU 列的伪链接(`<a onClick={navigate(...)}>`)也换成了
    `<Link>`,同款地标于是出现两次,`braced_block` 当场按"起点不唯一"拒绝执行。

    它红得对:一个能匹配两处的地标,下一次多半会静静切错那一处。修法是把
    地标收窄到这一列真正的目标路径,而不是回头去改前端迁就它 —— 那条链接
    本来就该是 `<Link>`。反向断言仍然成立:换成 `row.spu` 时地标照样命中,
    切出来的块里就有 `row.spu}`。
    """
    aggregate = _code_only(FRONTEND / "pages" / "WorkbenchSpuPage.tsx")
    link_block = braced_block(
        aggregate,
        "<Link className=\"mono\" to={`/spus/",
        what="聚合页 SPU 列的链接目标",
    )
    assert "row.spu_id" in link_block
    assert "row.spu}" not in link_block, (
        "链接用的是字符串码不是主键 —— 那个接口收 UUID,点下去 422"
    )


def test_the_host_page_does_not_grow_a_second_answer():
    """**反向断言,窗口封闭。** 宿主页不许自己判"哪一份方案生效"。

    那条规则(颜色覆盖优先、回落 SPU 默认、只看 ACTIVE)在后端是可穷举的
    判定(`resolve_plan`)。在这里重写一遍就有了第二个答案,而两个答案漂移时
    没有任何地方会报错 —— 界面会说这个颜色按 A 方案出图,而任务是按 B 建的。
    硬规则 4。
    """
    body = braced_block(
        _code_only(FRONTEND / "pages" / "SpuDetailPage.tsx"),
        "export default function SpuDetailPage()",
        what="宿主页函数体",
    )
    for forbidden in ("'ACTIVE'", '"ACTIVE"', "resolvePlan"):
        assert forbidden not in body, (
            f"宿主页里出现了 {forbidden} —— 方案生效规则不许在前端重写一遍"
        )


def test_the_colour_label_rule_still_has_exactly_one_implementation():
    """颜色叫什么由 `colorVariantLabel` 一处说了算。

    宿主页要给面板一张 `颜色 id -> 显示名` 的表,而"显示名"的三级回落
    (`working_name` → `display_name` → `variant_code`)与后端
    `_material_facts` 拼 `variant_labels` 的顺序逐字相同。在这里手写一遍
    最省事(三个 `||` 而已),后果是同一个颜色在素材页的分组标题和方案面板的
    作用域列里叫两个名字,而运营会以为那是两个颜色。
    """
    host = _code_only(FRONTEND / "pages" / "SpuDetailPage.tsx")
    assert "colorVariantLabel" in host, "宿主页没有复用唯一那份颜色显示名规则"

    labels = window(
        host,
        "const variantLabels",
        "const colorColumns",
        what="宿主页拼 variantLabels 的那一段",
    )
    assert "working_name" not in labels, (
        "宿主页自己拼了一份显示名回落 —— 那是第二份实现"
    )
