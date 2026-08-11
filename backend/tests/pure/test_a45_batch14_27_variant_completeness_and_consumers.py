"""A45-batch14-27:三条「算好了没人读」的最后一跳。

## 本批修的是同一个形状的三次重演

上游算出了一个正确的结论,接线也在,而**最后一跳没有人接** ——
两侧的测试因此全绿,缺陷只在界面上可见,而界面没有门禁。

    variant_gate_roles   14-15 就递到 MaterialFacts 上,`_evaluate_material`
                         一次都没读过 —— 颜色完整度看起来是一道生效的门禁,
                         实际上不产出任何 issue / blocking_reason / next_action
    facts_stale          14-21 起逐行在回,`frontend/src/` 零引用 ——
                         「样品换了、这批事实已过期」运营完全看不见
    color_variant_id     上传接口收得下,前端表单不发 ——
                         界面传出来的素材归属恒为 null,于是 AC-21
                         在界面上**没有对象可验**

## 颜色维的来源是一个决定,不是一次实现(D1)

`variant_gate_roles` 的键原来从素材行上数出来。那条理由对指纹成立,
对门禁是反的:一张图都没传的颜色在素材里留不下痕迹,于是它永远不进表、
永远不被判缺图 —— **门禁最该拦的那一种,恰好是它唯一看不见的那一种。**

    备选           继续从素材反推(零改动)
    选错的后果     一件只传了 A 色图的三色商品一路走到刊登,B 色的图片集
                   拿不到样品,而没有任何一步报错
    这条守卫       `test_the_colour_dimension_comes_from_the_declared_list...`
                   构造「声明三色、只传一色」的 facts,断言另外两色进缺图清单。
                   退回素材反推的话它当场红

## 为什么守卫要盯**关键字**而不是「函数被调用过」

14-15 的变异 W1 已经演过一次:改个关键字名(`variant_gate_roles_unused=`)
调用还在,只断言"调用出现过"的守卫对它完全隐形。本文件里凡是钉接线的,
钉的都是 AST 上的关键字或读取点。
"""
from __future__ import annotations

import ast

from app.workbench.flow import IssueLevel, MaterialFacts, _evaluate_material
from tests.pure._helpers import BACKEND_ROOT, window

APP = BACKEND_ROOT / "app"
FRONTEND_SRC = BACKEND_ROOT.parent / "frontend" / "src"

_FRONT = frozenset({"PRODUCT_FRONT"})


def _src(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


def _fe(rel: str) -> str:
    return (FRONTEND_SRC / rel).read_text(encoding="utf-8")


def _codes(facts: MaterialFacts) -> list[str]:
    return [i.code for i in _evaluate_material(facts).issues]


# ================================================================ 一、颜色门禁有人判


def test_the_variant_buckets_actually_produce_a_verdict():
    """算好的颜色桶必须变成 issue。

    在此之前 `_evaluate_material` 一个字都没读过它 —— 而**没有断言会红**:
    `MaterialFacts` 上的字段在、`service.py` 的构造关键字在、
    `sample_completeness` 的判定穷举验过。断的是中间那一跳,
    而那一跳没有任何一侧的测试跨过去。
    """
    facts = MaterialFacts(
        total=1,
        usable=1,
        gate_roles=_FRONT,
        variant_gate_roles={"cv-a": _FRONT, "cv-b": frozenset()},
        variant_labels={"cv-a": "黑色", "cv-b": "藏青"},
    )

    codes = _codes(facts)
    assert "MATERIAL_VARIANT_MISSING_REQUIRED_ROLE" in codes, (
        "颜色作用域算好了没人判 —— 门禁看起来生效,实际不产出任何结论"
    )
    # 有图的那个颜色不许进
    messages = [
        i.message
        for i in _evaluate_material(facts).issues
        if i.code == "MATERIAL_VARIANT_MISSING_REQUIRED_ROLE"
    ]
    assert len(messages) == 1 and "藏青" in messages[0], (
        f"该报的颜色报错了或报重了:{messages}"
    )


def test_the_variant_issue_names_the_colour_not_the_uuid():
    """提示语必须带颜色名。

    只说「缺少正面图」的话,一件三色商品会得到三条一模一样的阻断,
    运营无从知道该给哪个颜色补图 —— 而"补错颜色"在界面上没有任何反馈:
    那张图进了另一个颜色的作用域,原来那条阻断纹丝不动。
    """
    facts = MaterialFacts(
        total=1,
        usable=1,
        gate_roles=_FRONT,
        variant_gate_roles={"cv-b": frozenset()},
        variant_labels={"cv-b": "藏青"},
    )

    issue = next(
        i
        for i in _evaluate_material(facts).issues
        if i.code == "MATERIAL_VARIANT_MISSING_REQUIRED_ROLE"
    )
    assert "藏青" in issue.message, "提示语里没有颜色名"
    assert issue.ref == "cv-b", "`ref` 不是颜色 id —— 前端跳不到那个分组"


def test_the_variant_issue_says_or_not_just_the_first_role():
    """说「正面图**或**平铺图」,与 SPU 那一档同源。

    只报组里第一个角色的话,运营为了消掉那句话会把平铺图改标成正面图,
    而下游每一处按角色取图的地方跟着错。14-10 修的正是这个坏循环,
    颜色这一档不许把它重新引进来。
    """
    facts = MaterialFacts(
        total=1, usable=1, gate_roles=_FRONT,
        variant_gate_roles={"cv-b": frozenset()}, variant_labels={"cv-b": "藏青"},
    )

    issue = next(
        i
        for i in _evaluate_material(facts).issues
        if i.code == "MATERIAL_VARIANT_MISSING_REQUIRED_ROLE"
    )
    assert "或" in issue.message, (
        "颜色这一档只报了组里第一个角色 —— 那个坏循环从颜色维回来了"
    )


def test_the_variant_gate_blocks_it_does_not_merely_remind():
    """级别是 BLOCKING。

    降成 REMINDER 后行为几乎不变,变的是**可见度**:`summarize()` 不计数、
    列表页不显示徽标,于是「B 色缺正面图」只在详情页第三屏可见 ——
    而运营是在列表页决定今天做哪几件商品的。
    """
    facts = MaterialFacts(
        total=1, usable=1, gate_roles=_FRONT,
        variant_gate_roles={"cv-b": frozenset()}, variant_labels={"cv-b": "藏青"},
    )

    issue = next(
        i
        for i in _evaluate_material(facts).issues
        if i.code == "MATERIAL_VARIANT_MISSING_REQUIRED_ROLE"
    )
    assert issue.level is IssueLevel.BLOCKING, "颜色完整度降成了提醒"


def test_a_fully_covered_product_gets_no_variant_noise():
    """每个颜色都有正面图时,一条颜色问题都不许出现。

    反向的代价很具体:假阻断会训练运营忽略这一列,而下一条真的阻断
    也在同一列里。
    """
    facts = MaterialFacts(
        total=2, usable=2, gate_roles=_FRONT,
        variant_gate_roles={"cv-a": _FRONT, "cv-b": _FRONT},
        variant_labels={"cv-a": "黑色", "cv-b": "藏青"},
    )

    assert "MATERIAL_VARIANT_MISSING_REQUIRED_ROLE" not in _codes(facts)


def test_no_declared_colours_means_no_variant_issues_at_all():
    """老建档路径的商品(没有 SPU、没有颜色)不许因此被拦。

    `colours_for` 对它返回空表,于是这张表是空的 —— 空表不许被读成
    「缺了所有颜色」。这一档是 `MaterialFacts()` 的默认值,漏改的方向
    必须是**不拦**,而不是全库商品集体阻断。
    """
    assert "MATERIAL_VARIANT_MISSING_REQUIRED_ROLE" not in _codes(
        MaterialFacts(total=1, usable=1, gate_roles=_FRONT)
    )


# ================================================================ 二、颜色维的来源(D1)


def test_the_colour_dimension_comes_from_the_declared_list_not_the_assets():
    """**这是 D1 的落点。**

    「声明了三色、只传了一色的图」——另外两色必须进缺图清单。
    从素材反推的话它们在表里根本不存在,于是一条 issue 都不会有,
    而门禁看起来完全正常。

    这条守卫钉的是 `service._material_facts` 里那个键集合的**来源**,
    所以同时从两头验:判定这一头给足了输入就要报(上面那几条),
    取数这一头必须把没有素材的颜色也放进来(这一条)。
    """
    fn = next(
        n
        for n in ast.walk(ast.parse(_src("workbench/service.py")))
        if isinstance(n, ast.FunctionDef) and n.name == "_material_facts"
    )
    body = ast.unparse(fn)

    assert "colours_for(" in body, (
        "颜色维还是从素材反推的 —— 一张图都没传的颜色永远不会被判缺图,"
        "而那正是门禁最该拦的一种"
    )
    assert "variant_labels" in body, "颜色名没递过去,提示语只会是一串 UUID"

    # 判定侧:声明了三色、桶里两色是空的,必须报两条
    facts = MaterialFacts(
        total=1,
        usable=1,
        gate_roles=_FRONT,
        variant_gate_roles={"cv-a": _FRONT, "cv-b": frozenset(), "cv-c": frozenset()},
        variant_labels={"cv-a": "黑色", "cv-b": "藏青", "cv-c": "米白"},
    )
    reported = {
        i.ref
        for i in _evaluate_material(facts).issues
        if i.code == "MATERIAL_VARIANT_MISSING_REQUIRED_ROLE"
    }
    assert reported == {"cv-b", "cv-c"}, f"缺图的颜色报漏了:{sorted(reported)}"


def test_the_upload_whitelist_and_the_gate_read_the_same_colours():
    """界面能选的颜色 = 门禁会判的颜色 = 上传接口会接受的颜色。

    三处各自拼一遍 product -> spu -> colours 的话,分叉的表现不对称:

        界面多列一个   运营选中、上传、拿到 422,他不知道为什么
        界面少列一个   那个颜色永远传不了图 -> 永远缺正面图 -> 门禁一直说缺图,
                       而没有任何提示指向原因

    后一种没有人会报成 bug。所以钉的是**同一个取数**,不是同一句约定。
    """
    api = _src("api/products.py")
    assert "colours_for(" in api, "颜色清单端点没走同一个取数"
    assert "colours_for(" in _src("workbench/service.py"), "门禁没走同一个取数"

    # 上传校验那一头读的是同一张表
    guard = window(
        _src("services/product_service.py"),
        "def assert_colour_belongs_to",
        "def colours_for",
        what="归属校验",
    )
    assert "ColorVariant" in guard and "spu_id" in guard


def test_an_explicitly_empty_colour_scope_never_expands_to_all_colours():
    """空重试范围必须保持为空,不能悄悄变成一次全量付费调用。"""
    service = _src("attributes/service.py")
    assert "if only_variant_ids is not None:" in service, (
        "显式 [] 被按未传处理了 —— 一个空的颜色重试会扩大成整个 SPU"
    )


# ================================================================ 三、前端消费点


def test_the_upload_form_actually_sends_the_colour():
    """上传表单必须发 `color_variant_id`。

    **这是 AC-21 在界面上有没有对象可验的分界线。** 不发的话界面传出来的
    素材归属恒为 null,于是 §5.3 的颜色作用域指纹只有共享那一档能触发 ——
    「传 A 色图不 stale B 色事实」在真环境里没有东西可以验。

    后端从 14-22 起就收得下这个表单参数,断的一直是前端这一跳。
    """
    client = _fe("api/products.ts")
    assert "color_variant_id" in client, (
        "上传客户端不发颜色 —— 界面传出来的素材归属恒为 null,AC-21 无对象可验"
    )
    tab = _fe("components/workbench/MaterialTab.tsx")
    assert "colorVariantId" in tab or "color_variant_id" in tab, (
        "素材页没有选颜色的入口"
    )


def test_the_stale_flag_has_a_consumer_and_it_fails_closed():
    """`facts_stale` 必须有前端读取点,而且缺席时判过期。

    方向和后端 `AttributeValueOut.facts_stale: bool = True` 一致,
    理由也一样:缺席意味着**没有人算过它**,而"没算过"必须显示成
    "证明不了它仍然成立"。默认 `false` 的表现是全库事实显示正常 ——
    正是这一整条机制要防的那件事。
    """
    api = _fe("api/attributes.ts")
    assert "facts_stale" in api, "前端类型里没有这个字段 —— 后端算了没人读"
    assert "?? true" in api, (
        "缺席默认成了「不过期」—— 一次接口降级的表现是全库事实集体显示正常"
    )
    tab = _fe("components/workbench/AttributeTab.tsx")
    assert "factsStale(" in tab, "属性页没有展示「样品已变」"


def test_the_frontend_reads_the_attribution_column_not_the_hint():
    """前端分组读 `color_variant_id`,不读 `variant_hint`。

    第四次拒绝同一件事(前三次在 14-9 / 14-10 / `run_state.scope_of`)。
    hint 是模型猜的,拿它分组的表现是:界面按 A 色显示的一张图,
    在门禁和指纹眼里属于共享作用域 —— 三处对同一张图有两种答案。

    ## 定位串跟着实现搬过一次(a46-phase2 订正)

    `groupByColour` 后来从 `MaterialTab.tsx` 抽到了 `materialUtils.ts`,
    而这条守卫仍在 `MaterialTab.tsx` 里找它 —— `window()` 因此抛
    「起点出现 0 次」,这条守卫红了不知道多久。**它红得对**:定位串失效时
    切出来的窗口没有意义,`window()` 宁可炸也不肯把空窗口交给反向断言
    (空窗口会让 `not in` 平凡为真 —— 那才是真正危险的那一种)。

    所以修的是定位串,不是断言。**不变量一个字没变。**
    另外补一句正向的:`MaterialTab` 必须用这个共享实现,不许自己再分一次组 ——
    否则这条守卫盯着 utils、界面用的是另一份,又回到"守卫看的不是那个位置"。
    """
    utils = _fe("components/workbench/materialUtils.ts")
    grouping = window(utils, "export function groupByColour", what="分组")
    assert "variant_hint" not in grouping, (
        "分组读了 variant_hint —— 模型猜的值决定了运营看到的归属"
    )
    tab = _fe("components/workbench/MaterialTab.tsx")
    assert "groupByColour" in tab and "from './materialUtils'" in tab, (
        "素材页没有用共享的分组实现 —— 这条守卫会盯着一份没人用的函数"
    )


# ================================================================ 四、0046 身份迁移


def test_0046_scopes_image_item_rewrites_to_the_owning_spu():
    """旧 `variant_id` 只在 SPU 内唯一,迁移不得按它全表更新。

    两个 SPU 都有 ``BLK`` 是常态。缺少 ``listing_image_sets.spu`` 这层关联时,
    第一件商品会把两边的图片项都改成自己的颜色 UUID,而迁移仍然全绿。
    """
    migration = (
        BACKEND_ROOT / "migrations" / "versions" / "0046_stage1_identity_owner_uuid.py"
    ).read_text(encoding="utf-8")
    assert "FROM listing_image_sets s" in migration
    assert migration.count("s.spu = :spu") >= 2, (
        "0046 的 upgrade/downgrade 没有都按 image_set.spu 收窄图片标签改写 —— "
        "跨 SPU 同名颜色会被串成同一个 UUID"
    )
    assert "UPDATE listing_image_items SET variant_id" not in migration, (
        "0046 又退回了按裸 variant_id 全表更新"
    )


def test_0046_refuses_an_ambiguous_working_name_instead_of_choosing_one():
    """``working_name`` 不唯一;候选超过一个必须让迁移停下。"""
    migration = (
        BACKEND_ROOT / "migrations" / "versions" / "0046_stage1_identity_owner_uuid.py"
    ).read_text(encoding="utf-8")
    assert "if len(matches) > 1:" in migration
    assert "迁移不能替你任选一个" in migration
    assert "UPDATE products p\n               SET color_variant_id = cv.id" not in migration, (
        "0046 仍用 UPDATE ... FROM 任取一个 working_name 匹配"
    )
