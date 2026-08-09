"""A45 batch10:前端受众接线复核的逐条整改。

这一轮修的是《前端审视结果》十条。它们有一个共同形状:**后端护栏都在,
前端把它们绕开了** —— 受众与授权这套闸口在真人操作的路径上一次都不会
生效,只在后端接口被直接调用时生效。

所以这份文件里的守卫多数是**跨语言**的:光断言后端行为不会变红,
上一轮就是这么漏过去的。每条都做过变异验证(把接线拿掉,测试确实变红)。

零三方依赖:只 import `core/audience`、`workbench/flow`、`workbench/batch`
与几份 TS/PY 源文本。
"""
from __future__ import annotations

import re

from app.workbench import batch as b
from app.workbench.flow import (
    AttributeFacts,
    AudienceFacts,
    DraftFacts,
    MaterialFacts,
    NextActionCode,
    ProductFlow,
    SetupFacts,
    evaluate,
    matches_filters,
)
from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT

FRONTEND = PROJECT_ROOT / "frontend" / "src"


def _code_only(source: str) -> str:
    """去掉 TS/TSX 注释。**字符串断言的第一步,本仓踩过五次。**

    第五次就是写这份文件的时候:`forProductAudience` 的守卫第一版直接
    `in` 全文,而同一个文件的注释里正好解释了"这个参数曾经是死代码" ——
    把接线整行删掉,守卫照样绿。变异验证当场抓到,这行才补上。
    """
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", without_block)


def _fe(rel: str) -> str:
    """前端源码,**已去注释**。要看原文的用 `_fe_raw`。"""
    return _code_only((FRONTEND / rel).read_text(encoding="utf-8"))


def _fe_raw(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


def _be(rel: str) -> str:
    return (BACKEND_ROOT / "app" / rel).read_text(encoding="utf-8")


def _flow(**kw) -> ProductFlow:
    """素材齐、属性未完成的一件商品。受众由用例自己给。"""
    base = dict(
        product_id="p1",
        sku="SW-1",
        spu="SW",
        # 建档默认已完成。本夹具写于七步增维(batch27)之前,那时 `SETUP`
        # 还不是一步;`SetupFacts()` 空视图判 NEEDS_CONFIRM,而建档排在
        # `STEP_ORDER` 最前 —— 于是这一批本该验受众路径的用例会全部
        # 答"去补全建档"。与 `test_workbench_flow._flow` 同一个处理,
        # 建档自己的路径由 `test_a45_batch27_seven_steps.py` 穷举。
        setup=SetupFacts(has_spu_ref=True, has_colour_ref=True),
        material=MaterialFacts(
            total=1,
            usable=1,
            # 两份集合不是一回事,见 §6.2:`usable_roles` 是「库里有什么」,
            # `gate_roles` 是「门禁认什么」。这一批用例问的是受众那条路径,
            # 所以素材这一侧一律填成已过门禁
            usable_roles=frozenset({"PRODUCT_FRONT"}),
            gate_roles=frozenset({"PRODUCT_FRONT"}),
        ),
        attribute=AttributeFacts(
            required_fields=("garment_type",),
            confirmed=frozenset(),
            extraction_count=1,
            suggested=frozenset({"garment_type"}),
        ),
    )
    base.update(kw)
    return ProductFlow(**base)


# ================================================================ 1. age_verified


def test_age_verified_has_an_actual_form_control():
    """审视第 1 条:`age_verified` 是一个永远为 false 的字段。

    提交时写了 `age_verified: values.age_verified ?? false`,而表单里没有
    任何 `name="age_verified"` 的 Form.Item —— `values.age_verified` 恒为
    undefined,于是**每一个通过界面建出来的模特都永久挂着"年龄未确认"**。
    一个 100% 命中的警示等于没有警示。

    断言"提交里读了它"是不够的(上一版正是那样),所以这里要求控件存在。
    """
    page = _fe("pages/ModelTemplatesPage.tsx")
    # 注释里也会提到这个字段名(本次改动就写了一段),所以按**全部**出现位置
    # 找,只要有一处是真控件即可
    spots = [m.end() for m in re.finditer(r'name="age_verified"', page)]
    assert spots, "提交时读了 values.age_verified,但表单里没有对应控件 —— 它恒为 false"
    assert any("valuePropName" in page[i : i + 200] for i in spots), (
        "age_verified 是布尔控件,缺 valuePropName='checked' 时 antd 取到的是事件对象"
    )


def test_the_license_fields_are_actually_submitted():
    """审视第 2 条:§11 授权字段组后端能收能存,界面写不进去。

    创建表单只发 name/audience/pose/body_type/background/tags/notes,
    授权字段一个都不发 —— 于是 `license_status` 永远停在 UNVERIFIED,
    而 `assert_usable` 对非 LICENSED 提前 return:**那道授权闸口在产品里
    永远不会触发任何一次阻断**。
    """
    page = _fe("pages/ModelTemplatesPage.tsx")
    for field in (
        "license_status",
        "commercial_scope",
        "allowed_platforms",
        "allowed_regions",
        "license_expires_at",
        "allow_ai_dressing",
        "prohibited_categories",
    ):
        assert field in page, f"§11 的 {field} 在界面上没有任何入口"


def test_a_patch_route_exists_on_both_sides():
    """没有 update,授权字段就只能在创建那一刻写对,写错了永远改不了。"""
    api = _be("api/model_templates.py")
    assert re.search(r'@router\.patch\(\s*"/\{template_id\}"', api), (
        "后端没有 PATCH 路由 —— 授权字段只写不改"
    )
    client = _fe("api/generation.ts")
    assert re.search(r"update:\s*async", client), "modelTemplatesApi 没有 update"
    assert "apiClient.patch" in client, "update 没有真的发 PATCH"


def test_audience_is_not_patchable():
    """模特受众不可改:改它等于换一个人,而历史图片仍然溯源到这一行。"""
    # 纯测试不 import ORM 依赖的模块,按源文本读白名单
    src = _be("services/model_template_service.py")
    block = re.search(
        r"UPDATABLE_FIELDS: frozenset\[str\] = frozenset\((.*?)\)\n", src, re.S
    )
    assert block, "找不到 UPDATABLE_FIELDS —— PATCH 没有白名单就是黑名单语义"
    allowed = set(re.findall(r'"(\w+)"', block.group(1)))
    for forbidden in ("audience", "body_type", "storage_path", "file_hash"):
        assert forbidden not in allowed, f"{forbidden} 不该能通过 PATCH 改"
    assert "license_status" in allowed and "age_verified" in allowed


def test_both_write_doors_to_the_license_fields_agree_on_permission():
    """create 与 PATCH 的权限必须一致。

    第一版给 PATCH 挂了 `require_admin`,而 `create` 是 operator 且同样接收
    全部十二个授权字段 —— 包括 age_verified 和 license_status。同一份背书,
    建的时候不用口令、改的时候要,想绕开的人重建一条就行。
    **一道能被日常动作绕过的闸比没有闸更糟**,因为它看起来像已经管住了。
    """
    api = _be("api/model_templates.py")
    create = re.search(r"async def create_template\(.*?\n\)", api, re.S)
    patch = re.search(r"def update_template\(.*?\n\)", api, re.S)
    assert create and patch
    # 两者都在 require_operator 的路由器下,且都没有单独提权
    patch_decorator = re.search(
        r'@router\.patch\(\s*"/\{template_id\}",(.*?)\)\ndef update_template', api, re.S
    )
    assert patch_decorator, "找不到 PATCH 的装饰器"
    assert "require_admin" not in patch_decorator.group(1), (
        "PATCH 挂了 require_admin 而 create 没有 —— 这道闸能被重建绕开"
    )


# ================================================================ 2. §10.5 硬约束


def test_the_audience_filter_is_actually_called_with_a_product_audience():
    """审视第 3 条:§10.5 的硬约束筛选,前端一次都没调用过。

    `modelTemplatesApi.list()` 有 `forProductAudience` 参数、后端也实现了
    `for_product_audience` —— 但全仓唯一的调用点是 `modelTemplatesApi.list(true)`,
    **没有传商品受众**。这个参数是死代码。

    连带的后果写在 `core/audience.generation_block_reason` 的注释里:
    "这条正常情况下走不到 —— §10.5 已经让不匹配的模特不出现在列表里"。
    那句话在当时的前端下是不成立的。
    """
    modal = _fe("components/TaskCreateModal.tsx")
    assert "forProductAudience" in modal, (
        "TaskCreateModal 没有传商品受众 —— §10.5 的候选过滤是死代码"
    )
    # 光传还不够:换商品后必须重查,否则拿的是上一件商品的候选集
    key = re.search(r"queryKey:\s*\[[^\]]*model-templates[^\]]*\]", modal)
    assert key and "udience" in key.group(0), (
        "model-templates 的 queryKey 不含受众 —— 换商品后候选集不会重查"
    )


def test_the_model_dropdown_shows_the_audience():
    """§10.3:模特卡片必须展示受众。

    列表页的卡片一直有,但**真正做选择的那一处**(创建任务的下拉)
    label 是 `${t.name} · ${t.pose}` —— 不带受众。
    """
    modal = _fe("components/TaskCreateModal.tsx")
    label = re.search(r"label:\s*`\$\{t\.name\}[^`]*`", modal)
    assert label, "找不到模特下拉的 label"
    assert "audience" in label.group(0), (
        "模特下拉不显示受众 —— §10.5 的硬约束就只剩后端一道"
    )


# ================================================================ 3. 受众可写


def test_the_product_type_and_form_can_carry_an_audience():
    """审视第 4 条:`Product` 类型漏了 audience。

    后端 `ProductOut extends ProductBase`,audience 是实打实下发的。
    前端类型里没有它的后果不是"少一个字段":`ProductFormModal` 因此没有
    受众输入项,**界面上无法给商品设置或修改受众** —— 受众只能靠 CSV
    导入一次性写对,导入错了没有任何补救路径。
    """
    types = _fe("api/types.ts")
    product = re.search(r"export interface Product \{(.*?)\n\}", types, re.S)
    assert product and re.search(r"^\s*audience[?]?:", product.group(1), re.M), (
        "Product 类型里没有 audience"
    )
    form = _fe("components/ProductFormModal.tsx")
    assert 'name="audience"' in form, "商品表单里没有受众输入项"


def test_changing_a_confirmed_audience_asks_twice():
    """§20.5 最后一条:修改已确认受众要二次确认,并提示会作废已生成的图片。

    后端 `schemas/product.py` 的注释明写"那道确认在界面层"。
    """
    form = _fe("components/ProductFormModal.tsx")
    assert "modal.confirm" in form, "改受众没有二次确认"
    body = form[form.index("modal.confirm") : form.index("modal.confirm") + 1200]
    assert "作废" in body, "二次确认没有提示会作废已生成的图片"


def test_garment_types_are_narrowed_by_audience():
    """审视第 8 条:给一件男装商品选到 BIKINI_SET 不该是可以做到的操作。

    与体型词表(§10.4)同一个道理。全集仍在(后端按枚举全集收),
    这里只管展示时收窄。
    """
    types = _fe("api/types.ts")
    assert "GARMENT_TYPES_BY_AUDIENCE" in types
    table = types.split("GARMENT_TYPES_BY_AUDIENCE")[1]
    # `MEN:` 也会命中 `WOMEN:` 的尾巴 —— 锚到行首
    women = re.search(r"^\s*WOMEN:\s*\[([^\]]*)\]", table, re.M | re.S)
    men = re.search(r"^\s*MEN:\s*\[([^\]]*)\]", table, re.M | re.S)
    assert women and men
    assert "BIKINI_SET" in women.group(1) and "BIKINI_SET" not in men.group(1)
    assert "SWIM_BRIEFS" in men.group(1) and "SWIM_BRIEFS" not in women.group(1)
    assert "GARMENT_TYPES_BY_AUDIENCE" in _fe("components/ProductFormModal.tsx")


# ================================================================ 4. 审阅页受众


def test_the_audience_reaches_the_screens_where_review_actually_happens():
    """审视第 5 条:§13.2 说审阅页左栏常驻受众,而受众没出现在审阅页。

    上一轮的代码写在 `FlowHeader` 里,注释也引了 §13.2 —— 但 `FlowHeader`
    只被 `WorkbenchProductPage` 使用。逐件快审、单图评分详情、审核队列
    受众出现次数都是 0。

    上一轮的契约测试断言的是"至少一个组件读了 review_focus",`FlowHeader`
    满足了它 —— **那道网挡得住"完全没接线",挡不住"接在了不是审阅页的
    地方"**。这一条按页点名,不接受"某个组件读过了"。
    """
    for page in (
        "pages/WorkbenchReviewPage.tsx",
        "pages/ReviewDetailPage.tsx",
        "pages/ReviewQueuePage.tsx",
    ):
        text = _fe(page)
        # 断言**渲染**而不是"文件里出现过这个词":import 了却没用上,
        # 界面上一样看不到受众
        assert "<AudienceTag" in text, (
            f"{page} 没有渲染受众标签 —— §13.4 第 5 条(模特受众与商品受众"
            "一致)在这块屏幕上无法执行"
        )


def test_the_review_focus_reaches_the_quick_review_screen():
    """§13.3:标出需要重点检查的区域(按受众)。

    「男装该看腰头和裤长」这件事必须显示在**实际审图的那块屏幕**上。
    """
    assert "ReviewFocusLine" in _fe("pages/WorkbenchReviewPage.tsx")
    assert "ReviewFocusLine" in _fe("pages/ReviewDetailPage.tsx")


def test_the_review_api_emits_the_audience_it_needs():
    """反向:前端要读的字段,评分审核接口必须真的下发。"""
    schema = (BACKEND_ROOT / "app" / "schemas" / "evaluation.py").read_text("utf-8")
    block = re.search(r"class ReviewOut\(.*?\n\nclass ", schema, re.S)
    assert block
    for key in ("audience", "audience_label", "review_focus"):
        assert re.search(rf"^\s*{key}:", block.group(0), re.M), (
            f"ReviewOut 不下发 {key},审阅页拿不到"
        )
    assert "_apply_audience" in _be("api/reviews.py"), "字段声明了但没人填"


# ================================================================ 5. CONFIRM_AUDIENCE


def test_unconfirmed_audience_produces_a_reachable_next_step():
    """审视第 7 条:§8.2「待确认受众」这一步在流程里不存在。

    `FlowHeader` 会把没受众的商品标成橙色"待确认",但十七个动作码里没有
    `CONFIRM_AUDIENCE`,首页也没有这一档 —— **那个橙色标记是个死胡同**:
    它告诉你有问题,界面上没有任何地方能解决它。
    """
    result = evaluate(_flow(audience=AudienceFacts(audience=None)))
    assert result.next_action.code is NextActionCode.CONFIRM_AUDIENCE
    # 它必须是待确认档,不是阻断 —— 理由见下一条
    assert result.blocking_count == 0
    codes = {i.code for i in result.issues}
    assert "AUDIENCE_UNCONFIRMED" in codes


def test_legacy_products_are_not_stranded_by_the_new_step():
    """存量商品(迁移 0030 不回填受众)不能被这一步永久挡在导出之前。

    `workbench/audience_rules` 的兼容矩阵为「NULL 受众 + 无前缀规则包」
    留了唯一一条缝:放行 + 警告。列表页若无条件抢先要求确认受众,
    那批商品就再也走不到导出 —— **而导出闸恰恰说它们可以过**。
    两处的宽严必须一致,否则一个说"走不了"一个说"可以",两句都对。
    """
    done_attrs = AttributeFacts(
        required_fields=("garment_type",),
        confirmed=frozenset({"garment_type"}),
        extraction_count=1,
    )
    result = evaluate(
        _flow(
            audience=AudienceFacts(audience=None),
            attribute=done_attrs,
            draft=DraftFacts(exists=True, status="VALIDATED"),
        )
    )
    assert result.next_action.code is not NextActionCode.CONFIRM_AUDIENCE, (
        "属性已走完的存量商品被受众确认拉回去了 —— 它再也到不了导出"
    )


def test_an_illegal_audience_value_blocks_instead_of_warning():
    """写错的受众不是"没填"。

    给它兼容缝的话,这个错值会静默存在到永远 —— 该商品悄悄走回受众前
    时代的单受众路径,而没人会发现。
    """
    result = evaluate(_flow(audience=AudienceFacts(audience=None, invalid=True)))
    assert result.next_action.code is NextActionCode.CONFIRM_AUDIENCE
    assert result.blocking_count >= 1
    assert "AUDIENCE_INVALID" in {i.code for i in result.issues}


def test_the_new_action_code_is_wired_on_both_sides_and_in_batch():
    """动作码要三处齐全:前端两张表、批量的异常类别。

    少了前端那两张表,界面上会退化成英文常量;少了批量映射,那一类
    "卡住"会显示成泛化的"当前不该做这个动作"——技术上正确,
    但运营看不出该去哪儿。
    """
    wb = _fe("api/workbench.ts")
    for table in ("NextActionCode", "NEXT_ACTION_LABEL", "ACTION_TAB"):
        idx = wb.index(table)
        assert "CONFIRM_AUDIENCE" in wb[idx : idx + 1400], f"{table} 缺 CONFIRM_AUDIENCE"
    assert (
        b.PRECHECK_CODE_BY_ACTION[NextActionCode.CONFIRM_AUDIENCE]
        is b.BatchErrorCode.AUDIENCE_UNCONFIRMED
    )
    # 它自成一档而不是并进 NO_CONFIRMED_ATTRIBUTES:两者的下一步不同 ——
    # 一个去商品资料选受众,一个去属性页逐项确认
    assert (
        b.PRECHECK_CODE_BY_ACTION[NextActionCode.CONFIRM_ATTRIBUTES]
        is not b.BatchErrorCode.AUDIENCE_UNCONFIRMED
    )
    assert "AUDIENCE_UNCONFIRMED" in _fe("api/batch.ts"), "批量异常码没有中文"


def test_today_page_offers_the_step_it_counts():
    """§8.2 点名的第一类待办要有落点,不能只在计数里。"""
    today = _fe("pages/TodayPage.tsx")
    assert "CONFIRM_AUDIENCE" in today
    assert "next_action=CONFIRM_AUDIENCE" in today, "卡片没有带筛选跳转"


# ================================================================ 6. 批次可拆


def test_the_batch_export_error_has_a_matching_filter():
    """审视第 6 条:错误给了指令但没给行动。

    批量导出在批次内规则包不一致时说"请按受众分成两个批次分别导出",
    而当时列表页没有受众列、没有受众筛选、后端也没有 audience 查询参数
    —— 运营看到这句话之后,**没有任何界面手段能按受众把批次拆开**。
    §9.5 要求"错误必须包含行动"。
    """
    # 后端:接口收得下
    assert "audience: str | None = Query(" in _be("api/workbench.py")
    # 判定层:真的按它筛
    women = _flow(audience=AudienceFacts(audience="WOMEN"))
    men = _flow(audience=AudienceFacts(audience="MEN"))
    assert matches_filters(evaluate(women), audience="WOMEN", flow=women)
    assert not matches_filters(evaluate(men), audience="WOMEN", flow=men)
    # 待确认那一档要筛得出来 —— 它是最需要被找出来的一批
    blank = _flow(audience=AudienceFacts(audience=None))
    assert matches_filters(evaluate(blank), audience="UNCONFIRMED", flow=blank)
    assert not matches_filters(evaluate(women), audience="UNCONFIRMED", flow=women)
    # 前端:筛选控件存在。
    #
    # **这里原来断言的是 `"setAudience" in page`** —— 点名一个 setter,
    # 而不是钉「这一页筛得了受众」这条性质。GAP-033 把筛选搬进 URL、
    # setter 换成 `filters.patch({ audience })` 之后它当场变红,
    # 红的原因是"有人把这件事做得更好了"。这是同一个形状在本仓库的第三次
    # (14-16 的 `heads == {"0037"}`、14-4 那条点名 `useUrlSeed` 是前两次),
    # 判据与一般化见 `DECISIONS.md` §3.31。
    #
    # 现在钉的是不变式:**这一页认 audience 这个筛选,并且列里看得见受众**。
    # 至于筛选值存在 URL 上还是组件里、由哪个函数写进去,不在射程内。
    page = _fe("pages/WorkbenchListPage.tsx")
    assert re.search(r"\baudience\b", page), (
        "工作台列表不认 audience 筛选 —— 运营拆不开批次"
    )
    assert "AudienceTag" in page, "工作台列表没有受众列 —— 看不见该按什么拆"


# ================================================================ 7. 警告有出口


def test_the_audience_gate_warnings_have_an_outlet():
    """审视第 9 条:`export_gate` 的 warnings 被丢掉了。

    `draft_audience_gate` 一直返回 warnings(存量商品"受众未确认"那一档),
    但 service 只读了 `gate.problems` —— 这半边结论从来没有出口。
    它的唯一消费者是界面。
    """
    from app.workbench.audience_rules import draft_audience_gate

    gate = draft_audience_gate(None, "swimwear")
    assert gate.ok and gate.warnings, "这一档本来就该产出警告"

    assert "def audience_gate_warnings" in _be("workbench/service.py")
    assert "audience_warnings" in _be("api/workbench.py"), "算了但没下发"
    assert "audience_warnings" in _fe("api/workbench.ts"), "下发了但类型里没有"
    tab = _fe("components/workbench/ExportTab.tsx")
    # 要的是**条件渲染那一处**:只 map 不判空的话,空数组会渲染出一个
    # 空的 Alert 壳;只判空不 map 则等于没显示内容
    assert re.search(r"audience_warnings\??\.length", tab), (
        "ExportTab 没有按 audience_warnings 做条件渲染 —— 等于没有出口"
    )
    assert re.search(r"audience_warnings\.map", tab), "警告内容没有被渲染出来"


# ================================================================ 8. 已知缺口不隐身


def test_the_model_reference_bypass_is_recorded_as_a_known_gap():
    """审视第 10 条:生成时"不指定模特"绕过受众与授权检查。

    **这一条本轮没有关闭**,所以这里守的是"它必须还写在已知缺口里"。

    根因与 §17 第 2 条是同一个:`media_assets` 没有生成溯源/授权列,
    一张 MODEL_REFERENCE 素材身上没有 age_verified、没有 license_status、
    也没有受众。真要关闭它得先落那次迁移。

    界面这一半已经改了:下拉不再把它说成一个等价选项,而是明说它跳过
    §11 的检查。后端那一半留在 STATUS 里 —— 一条写进文档的缺口是可追的,
    一条没人记得的不是。
    """
    status = (PROJECT_ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    assert "MODEL_REFERENCE" in status, (
        "模特参考图绕过授权检查这条缺口从 STATUS 里消失了 —— "
        "它还没修好,不该从文档里消失"
    )
    # 界面不再把它当作等价选项。
    #
    # **这一条原来断言的是 `"§11" in modal`,那是错的口径**(A45-batch14-5 修)。
    # 它守的东西写在自己的文档字符串里:「明说它跳过 §11 的检查」——
    # 而运营手里没有那份文档,一个编号对他等于"这里有个原因,保密"。
    # 更糟的是它和前端 ESLint 的 `no-restricted-syntax` **正面冲突**:
    # 那条规则禁止内部编号出现在 title/extra/description 这类用户可见文案里。
    # 两道门禁互相要求对方红,而先跑的那一道决定谁赢。
    #
    # 现在断言那句话的**实质**:得让运营看懂留空会少掉哪一层检查。
    # 编号本身搬进了组件的注释,可追性没丢(下面第二条钉住它没被顺手删掉)。
    modal = _fe_raw("components/TaskCreateModal.tsx")
    visible = re.sub(r"(?<!:)//[^\n]*", "", re.sub(r"/\*.*?\*/", "", modal, flags=re.S))
    assert re.search(r"没有授权与年龄记录", visible), (
        "留空那条路径没有用人话说明它跳过了什么 —— 运营只会以为这是个等价选项"
    )
    assert "§11" not in visible, (
        "内部编号又回到了用户可见文案里(前端 ESLint 会红) —— 结论留下,编号搬注释"
    )
    assert "§11" in modal, "注释里那条溯源被顺手删了 —— 缺口就此不可追"


# ================================================================ 9. 自审补的四条
#
# 下面四条不是《前端审视结果》里的,是 batch10 交付后自查backend 时发现的。
# 前两条是**本轮改动自己造成的**,后两条是被本轮改动激活的既有隐患。


def test_the_audience_action_lands_somewhere_that_can_actually_set_it():
    """`CONFIRM_AUDIENCE` 指向的标签页必须真的能改受众。

    审视第 7 条骂的是"橙色标记是个死胡同:它告诉你有问题,界面上没有
    任何地方能解决它"。batch10 补了动作码却把它指向属性页,而属性页
    当时没有受众控件(`ProductFormModal` 只挂在 /products 那两页)——
    **那只是把死胡同挪了个位置。**

    所以这条断言的不是"有动作码",是"落点能干活"。
    """
    wb = _fe("api/workbench.ts")
    # 只在 ACTION_TAB 这张表里找 —— NEXT_ACTION_LABEL 里同名的键值是中文,
    # 不限定范围的话会先命中它(Python 的 \w 认中文)
    action_tab = re.search(
        r"ACTION_TAB[^=]*=\s*\{(.*?)\n\}", wb, re.S
    )
    assert action_tab, "workbench.ts 里找不到 ACTION_TAB"
    tab = re.search(r"CONFIRM_AUDIENCE:\s*'([a-z]+)'", action_tab.group(1))
    assert tab, "ACTION_TAB 里没有 CONFIRM_AUDIENCE"
    target = tab.group(1)
    assert target == "attribute", f"落点变成了 {target},这条守卫要跟着改"

    attribute_tab = _fe("components/workbench/AttributeTab.tsx")
    # `in` 会被 `<AudienceConfirmCardXX` 骗过去(前缀子串),要求标签边界
    assert re.search(r"<AudienceConfirmCard[\s/>]", attribute_tab), (
        "CONFIRM_AUDIENCE 指向属性页,而属性页没有受众确认控件 —— 死胡同"
    )
    card = _fe("components/workbench/AudienceConfirmCard.tsx")
    assert "productsApi.update" in card, "受众确认卡没有真的提交"
    # §9.4:受众与品类同屏,不拆两步
    assert "garment_type" in card, "§9.4 要求受众与品类同屏确认,不拆两步"


def test_license_dates_are_normalised_to_naive_utc():
    """授权日期必须归一成 naive UTC,否则授权闸口是**崩**不是判定。

    表单走 `model_dump(mode="json")`,日期到达服务层时是 ISO 字符串。
    直接塞进 DateTime 列:SQLite flush 当场 TypeError。而
    `assert_usable` 里那句 `expires <= now`(now 是 naive UTC):

        str   <= datetime  -> TypeError
        aware <= naive     -> TypeError

    在 batch10 之前这个坑是潜伏的 —— 界面上没有授权日期输入框,
    没人填得进来。本轮把输入框加上,它就活了。
    """
    src = _be("services/model_template_service.py")
    assert "def _naive_utc" in src, "没有归一化函数"
    # **逐处点名**,不数总数 —— 计数会被 `def _naive_utc(` 那一行凑够,
    # 而且数字对了也说不出漏的是哪一条路
    create = re.search(r"template = ModelTemplate\((.*?)\n    \)", src, re.S)
    assert create, "找不到 create 的建行语句"
    for col in ("license_start_at", "license_expires_at"):
        assert re.search(rf"{col}=_naive_utc\(", create.group(1)), (
            f"create 路径的 {col} 没有归一化 —— SQLite flush 会当场 TypeError"
        )
    update = re.search(r"def update_template\(.*?\n    return template\n", src, re.S)
    assert update and "_naive_utc(value)" in update.group(0), (
        "update 路径没有归一化;只修一条等于没修"
    )

    ns: dict = {}
    body = re.search(r"def _naive_utc.*?\n    return value\n", src, re.S).group(0)
    body = re.sub(
        r"raise ValidationError\(.*?\) from None", 'raise ValueError("bad")', body, flags=re.S
    )
    exec(compile(body, "<naive_utc>", "exec"),
         {"datetime": __import__("datetime").datetime,
          "UTC": __import__("datetime").UTC, "Any": object}, ns)
    f = ns["_naive_utc"]

    from datetime import datetime as dt

    got = f("2027-01-01T00:00:00+08:00")
    assert got.tzinfo is None, "带偏移的输入没有被转成 naive"
    assert got == dt(2026, 12, 31, 16, 0), "+08:00 应当换算成 UTC,不是直接砍掉 tzinfo"
    assert f("2027-01-01T00:00:00Z") == dt(2027, 1, 1)
    assert f(None) is None and f("  ") is None
    # 最要紧的一条:归一之后那句比较不再抛
    assert isinstance(f("2027-01-01T00:00:00+08:00") <= dt.now(), bool)


def test_a_typo_in_the_audience_filter_is_an_error_not_an_empty_list():
    """拼错的受众筛选值必须报错,不能"匹配不上返回空"。

    后者在界面上是「这个受众一件商品都没有」—— 一句错得很有说服力的话。
    `/model-templates` 那条早就是这么做的(它的注释写着理由:
    忽略这个筛选会让一个拼错参数的前端拿到女装模特去配男装商品),
    工作台这条必须一致。
    """
    women = _flow(audience=AudienceFacts(audience="WOMEN"))
    result = evaluate(women)
    try:
        matches_filters(result, audience="WOMAN", flow=women)
    except ValueError as exc:
        assert "不合法" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("拼错的受众取值被静默当成'没有匹配项'")

    # flow 缺席时也不许静默跳过筛选 —— 那会把全量当成筛选结果
    try:
        matches_filters(result, audience="WOMEN")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("没有 flow 时筛选被静默忽略,调用方拿到的是全量")

    # 接口层要把它翻成 400,不是 500
    api = _be("api/workbench.py")
    assert "except ValueError as exc:" in api and "INPUT_INVALID" in api


def test_the_filter_whitelist_matches_the_audience_enum():
    """筛选白名单与 `Audience` 必须同步(flow.py 是零依赖层,不 import 枚举)。"""
    from app.core.enums import Audience
    from app.workbench.flow import _AUDIENCE_FILTER_VALUES

    assert _AUDIENCE_FILTER_VALUES == {a.value for a in Audience} | {"UNCONFIRMED"}


def test_the_review_queue_does_not_reload_the_spec_per_row():
    """审核队列不能每行都重读一次规则包 YAML。

    `generic.field_spec` **每次都重新读并校验**(它自己的 docstring 写了
    理由:spec 是外部文件,可能被改坏),实测约 9ms 一次。队列页一屏 20 行
    就是 180ms,而这一屏上最多只有三种规则包。

    这是 batch10 把 `_apply_audience` 加进列表路径时带出来的,
    不是既有问题。
    """
    api = _be("api/reviews.py")
    assert "focus_cache" in api, "列表路径没有按规则包缓存 review_focus"
    list_call = re.search(
        r"return Page\[ReviewOut\]\((.*?)\n    \)", api, re.S
    )
    assert list_call and "focus_cache=focus_cache" in list_call.group(1), (
        "缓存建了但列表调用没传进去"
    )


# ================================================================ 8. 交付后复核第二批
#
# 三条都是「守卫是绿的、动线是断的」这一形状的续集:上面的守卫验的是
# "字段有发 / 控件存在 / 映射表在",验不到**值的形态**与**清空的语义**。
# 每条同样做过变异验证(把修复退回原样,对应用例确实变红)。


def test_blank_license_dates_do_not_masquerade_as_dates():
    """空白的授权日期不能以 '' 的形态去闯 pydantic。

    multipart 表单没有 null:浏览器"没填日期"发出来是**空字符串**,而
    `ModelTemplateForm` 的两个日期列是 `datetime | None` —— pydantic v2 对
    `''` 两个分支都不认,整个创建请求 422,报错还指向 license_start_at。
    也就是说**不填授权日期就建不了任何模特模板**,而"未填"恰恰是新模板
    的默认状态(§11:缺省一律"未核实")。

    归一必须在路由层(表单可以来自任何客户端,curl -F 同样发 ''),
    且要发生在 `ModelTemplateForm(` 之前 —— 这里按**代码顺序**断言,
    只查函数存在挡不住"函数在、没人调"。
    """
    api = _be("api/model_templates.py")
    assert "def _blank_to_none" in api, "缺少空白日期归一函数"
    for col in ("license_start_at", "license_expires_at"):
        call = re.search(rf"{col} = _blank_to_none\({col}\)", api)
        assert call, f"{col} 没有过归一 —— 空表单值会以 '' 闯进 datetime|None"
        assert call.start() < api.index("form = ModelTemplateForm("), (
            f"{col} 的归一发生在构造表单之后,等于没归一"
        )

    # 与 test_license_dates_are_normalised_to_naive_utc 同一手法:
    # 把函数体抠出来真跑,钉住语义而不是字符串
    body = re.search(
        r"def _blank_to_none.*?\n        return text or None\n", api, re.S
    )
    assert body, "找不到 _blank_to_none 的函数体"
    ns: dict = {}
    exec(compile(body.group(0).replace("    def ", "def ", 1), "<b2n>", "exec"), {}, ns)
    f = ns["_blank_to_none"]
    assert f("") is None and f("   ") is None and f(None) is None
    assert f("2027-01-01T00:00:00Z") == "2027-01-01T00:00:00Z", (
        "归一只该吃掉空白,不该动真实日期串"
    )


def test_clearing_a_confirmed_audience_reaches_the_wire():
    """清空已确认受众必须以 null 上线,不能死在 JSON.stringify 手里。

    antd 的 allowClear 清空后 values.audience 是 undefined,而 JSON 序列化
    会把 undefined 的键整个丢掉 —— PATCH 里于是没有 audience,后端按
    「未传不改」处理。结果是:运营在"作废已生成图片、同步同 SPU"的危险
    弹窗上点了确认,接口 200,toast 说已保存,受众一个字没变。

    后端对显式 null 的语义本来就是"清空"(schemas/product.py 的 PATCH
    注释 + product_service 那段「不要在这里跳过 None」),前端要做的只是
    把意图翻译到位:两个提交分支都必须用归一后的 payload。
    """
    form = _fe("components/ProductFormModal.tsx")
    assert re.search(
        r"\{\s*\.\.\.values,\s*audience:\s*next\s*\}", form
    ), "提交载荷没有把 undefined 受众归一成 null"
    assert "onSubmit(values)" not in form, (
        "还有分支在直接提交原始 values —— 清空受众在那条路上会被静默丢掉"
    )
    assert form.count("onSubmit(payload)") >= 2, (
        "二次确认与直通两条分支必须都走归一后的 payload"
    )


def test_switching_audience_clears_a_stale_garment_type():
    """换受众时要清掉不属于新受众的品类,收窄 options 不算收窄。

    `GARMENT_TYPES_BY_AUDIENCE` 只决定下拉里能点到什么;已经选中的值
    不在新清单里时,antd 会把它原样留在表单里 —— 提交出去就是
    「男装商品 garment_type=BIKINI_SET」,而后端按枚举全集收
    (取值本身不分受众,见 core/enums.GarmentType 的 docstring),
    不会替这里拦。`AudienceConfirmCard` 已经做了同样的清理,
    两个受众入口的行为必须一致。
    """
    form = _fe("components/ProductFormModal.tsx")
    handler = re.search(
        r"onChange=\{\(v\?\:\s*Audience\)\s*=>\s*\{(.*?)\n\s*\}\}", form, re.S
    )
    assert handler, "受众下拉没有联动清理"
    assert "GARMENT_TYPES_BY_AUDIENCE[v].includes(gt)" in handler.group(1), (
        "清理没有对照新受众的品类清单"
    )
    assert re.search(r"setFieldValue\(\s*'garment_type',\s*undefined\s*\)", handler.group(1)), (
        "越界品类没有被清空"
    )
    # 卡片侧的同一条规则仍然在(两个入口一致是这条测试的另一半)
    card = _fe("components/workbench/AudienceConfirmCard.tsx")
    assert "GARMENT_TYPES_BY_AUDIENCE[v].includes(pickedType)" in card
