"""受众一等维度的判定与契约(PRD v2)。

这批用例逐条对应 PRD 的**验收项**,不是对着实现补的形状测试。
每个函数的 docstring 第一行写明它守的是哪一条 —— 将来有人改坏了,
错误信息要能直接指回需求,而不是"某个断言红了"。

零三方依赖(`python3 tools/run_pure_tests.py` 可直接跑):
只 import `core/enums`、`core/audience`、`attributes/registry`、
`extractors/schema`、`workbench/audience_rules` 与几份 YAML/TS 文本。
"""
from __future__ import annotations

import re

from app.attributes.registry import (
    ALL_AUDIENCES,
    REGISTRY,
    AttributeField,
    AutoConfirm,
    ValueType,
    extractable_fields,
    required_for_listing,
)
from app.core import audience as ar
from app.core.audience import AudienceConflictError
from app.core.enums import (
    AUDIENCE_LABELS,
    BODY_TYPES_FOR_AUDIENCE,
    Audience,
    HardFailCode,
    ModelBodyType,
    OwnerType,
)
from app.extractors.schema import build_extraction_schema
from app.workbench.audience_rules import draft_audience_gate, model_audience_problems
from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT, expect_raises

SPEC_DIR = BACKEND_ROOT / "app" / "channels" / "generic" / "spec"


# ================================================================ 1. 受众取值


def test_audience_has_no_unconfirmed_value():
    """§3.4 规则 3:UNISEX 不是"没填"。

    给"没填"一个枚举值,它会被当成合法受众流进模特筛选和规则包派生 ——
    未确认的状态必须由 `audience IS NULL` 表达,不是第四个枚举值。
    """
    assert {a.value for a in Audience} == {"WOMEN", "MEN", "UNISEX"}
    for forbidden in ("UNCONFIRMED", "UNKNOWN", "NONE", "PENDING"):
        assert forbidden not in {a.value for a in Audience}


def test_every_audience_has_a_chinese_label():
    """§20.4:普通运营端不显示原始码。少一个标签,界面上就冒出英文。"""
    missing = [a.value for a in Audience if a not in AUDIENCE_LABELS]
    assert not missing, f"这些受众没有中文标签:{missing}"


def test_blank_audience_coerces_to_none_but_garbage_raises():
    """空 = 待确认;写错的取值必须抛错,不能被当成"没填"。

    一个写错的受众字符串被静默当成 None,后果是该商品悄悄走回
    受众前时代的单受众路径,而没有任何人会发现。
    """
    assert ar.coerce(None) is None
    assert ar.coerce("") is None
    assert ar.coerce("  ") is None
    assert ar.coerce("men") is Audience.MEN
    assert ar.coerce(Audience.WOMEN) is Audience.WOMEN
    expect_raises(ValueError, ar.coerce, "WOMAN")


# ================================================================ 2. §10.5 硬约束


def test_model_audience_is_a_hard_constraint_not_a_ranking_preference():
    """§10.5:受众匹配是硬约束。

    男泳裤生成到女模特身上,出来的图往往在纯技术指标上是好的 ——
    评分器比对的是商品与参考图是否一致,**它不检查穿的人是谁**。
    所以这一条必须拦在选择阶段,不能靠后面两道关补救。
    """
    assert ar.model_matches_product(Audience.WOMEN, Audience.WOMEN)
    assert ar.model_matches_product(Audience.MEN, Audience.MEN)
    assert not ar.model_matches_product(Audience.WOMEN, Audience.MEN)
    assert not ar.model_matches_product(Audience.MEN, Audience.WOMEN)
    # UNISEX 商品:两种模特都可以,运营选择
    assert ar.model_matches_product(Audience.UNISEX, Audience.WOMEN)
    assert ar.model_matches_product(Audience.UNISEX, Audience.MEN)


def test_generation_is_blocked_before_the_task_is_created():
    """§12.4 生成前硬阻断:错配连费用都不该产生。

    阻断理由按 §20.4 的口径翻译,不吐 AUDIENCE_MISMATCH 原始码。
    """
    reason = ar.generation_block_reason(Audience.MEN, Audience.WOMEN)
    assert reason is not None
    assert "AUDIENCE_MISMATCH" not in reason, "§20.4:不许把原始错误码给运营看"
    assert "男装" in reason and "女装" in reason
    assert ar.generation_block_reason(Audience.MEN, Audience.MEN) is None


def test_body_type_vocabulary_is_split_by_audience():
    """§10.4:给男装模特打上 CURVY 不应该是一个可以做到的操作。"""
    women = BODY_TYPES_FOR_AUDIENCE[Audience.WOMEN]
    men = BODY_TYPES_FOR_AUDIENCE[Audience.MEN]
    assert ModelBodyType.CURVY in women and ModelBodyType.CURVY not in men
    assert ModelBodyType.MUSCULAR in men and ModelBodyType.MUSCULAR not in women
    assert ModelBodyType.REGULAR in men and ModelBodyType.REGULAR not in women
    # 每张词表的取值都必须真的在枚举全集里
    for audience, table in BODY_TYPES_FOR_AUDIENCE.items():
        assert table, f"{audience.value} 的体型词表是空的"
        for value in table:
            assert value in ModelBodyType

    assert "CURVY" not in ar.body_types_allowed(Audience.MEN)
    assert "CURVY" in ar.body_types_allowed(Audience.WOMEN)
    # UNKNOWN 两边都许:它是"没填",不是某个受众的体型词
    assert "UNKNOWN" in ar.body_types_allowed(Audience.MEN)


# ================================================================ 3. §18.1 规则包键


def test_category_code_is_derived_from_audience_plus_family():
    """§18.1:单一事实源是 audience 字段,category_code 由它派生。"""
    assert ar.category_code_for(Audience.WOMEN, "swimwear") == "women_swimwear"
    assert ar.category_code_for(Audience.MEN, "swimwear") == "men_swimwear"
    assert ar.category_code_for(Audience.UNISEX, "swimwear") == "unisex_swimwear"
    # 受众未确认:退回品类族本身(仍可追溯到商品,不是模块常量)
    assert ar.category_code_for(None, "swimwear") == "swimwear"
    # 幂等:已带前缀的码再派生一次不会叠前缀
    assert ar.category_code_for(Audience.MEN, "men_swimwear") == "men_swimwear"


def test_conflicting_audience_and_category_code_raises_instead_of_picking_one():
    """§18.1:两者不一致时以 audience 为准**并报错**,不静默取其一。

    静默取其一:取 audience 会让草稿指纹和 spec 文件名对不上,
    取前缀会让 §4.2「受众不被静默改写」变成空话。两边都不能选。
    """
    expect_raises(
        AudienceConflictError, ar.category_code_for, Audience.MEN, "women_swimwear"
    )
    # 码里编了受众、字段却是空:同样不许把前缀里的受众"捡"进字段
    expect_raises(AudienceConflictError, ar.category_code_for, None, "men_swimwear")


def test_prefix_matching_never_confuses_unisex_with_women():
    """§18.1 点名的写法陷阱:`unisex_` 与 `women_` 的前缀匹配。"""
    assert ar.embedded_audience_of("unisex_rash_guard") is Audience.UNISEX
    assert ar.embedded_audience_of("women_swimwear") is Audience.WOMEN
    assert ar.embedded_audience_of("swimwear") is None
    assert ar.garment_family_of("unisex_rash_guard") == "rash_guard"
    assert ar.garment_family_of("swimwear") == "swimwear"


# ================================================================ 4. §18.3 属性条件化


def test_mens_products_never_get_asked_for_a_neckline():
    """阶段 1 验收:**男装商品的识别 Schema 里不出现 neckline_type**。

    问一个泳裤的"领型",模型会给一个非常自信的答案。
    """
    men = set(extractable_fields(Audience.MEN))
    assert "neckline_type" not in men
    assert "strap_type" not in men
    assert "back_style" not in men
    assert "waistband_type" in men


def test_womens_products_never_get_asked_for_a_waistband():
    """阶段 1 验收:**女装商品的识别 Schema 里不出现 waistband_type**。"""
    women = set(extractable_fields(Audience.WOMEN))
    assert "waistband_type" not in women
    assert "inseam_length" not in women
    assert "neckline_type" in women


def test_the_extraction_schema_itself_is_filtered_not_just_the_field_list():
    """过滤必须发生在 Schema 生成之前,不是提示词里叮嘱一句。"""
    schema = build_extraction_schema(audience=Audience.MEN)
    names = set(
        schema["properties"]["fields"]["items"]["properties"]["name"]["enum"]
    )
    assert "neckline_type" not in names
    assert "waistband_type" in names
    # 枚举提示同样按受众裁剪 —— 留着女装词表,模型照样会用上
    assert "neckline_type" not in schema["_enum_hints"]

    # 显式点名一个本受众不存在的字段:抛错,不静默过滤
    expect_raises(
        ValueError, build_extraction_schema, ("neckline_type",), audience=Audience.MEN
    )


def test_liner_type_is_never_guessed_from_an_image():
    """§18.4:平铺图和模特图通常都看不见内衬,与 material 同一档。"""
    liner = REGISTRY["liner_type"]
    assert liner.visual is False
    assert liner.auto_confirm_policy is AutoConfirm.NEVER
    assert liner.auto_confirm_threshold is None
    assert "liner_type" not in extractable_fields(Audience.MEN)


def test_required_fields_are_conditional_on_audience():
    """§18.3:必填集按受众。男装泳裤不该被要求填领型。"""
    men = set(required_for_listing(Audience.MEN))
    women = set(required_for_listing(Audience.WOMEN))
    assert "waistband_type" in men and "waistband_type" not in women
    assert "neckline_type" not in men and "neckline_type" not in women
    # 两个受众共同必填的三项(受众前时代的全局必填集)
    common = {"garment_type", "primary_color", "material"}
    assert common <= men and common <= women
    # 受众未确认:退回"所有受众都必填"的集合,与 v1 行为逐字段一致
    assert set(required_for_listing(None)) == common


def test_required_for_must_be_a_subset_of_applies_to():
    """自相矛盾的声明要在构造时就炸,不留到运行期。

    "对男装必填、但只在女装上存在"的字段,表现是男装商品永远缺一个
    它填不了的必填字段。
    """
    expect_raises(
        ValueError,
        AttributeField,
        name="broken",
        label="占位",
        owner_type=OwnerType.SPU,
        value_type=ValueType.TEXT,
        applies_to=frozenset({Audience.WOMEN}),
        required_for=frozenset({Audience.MEN}),
    )
    expect_raises(
        ValueError,
        AttributeField,
        name="nobody",
        label="占位",
        owner_type=OwnerType.SPU,
        value_type=ValueType.TEXT,
        applies_to=frozenset(),
    )


def test_every_registry_field_declares_a_reachable_audience():
    """注册表的每个字段都要至少对一个受众适用,且取值合法。"""
    for name, spec in REGISTRY.items():
        assert spec.applies_to, f"{name}: applies_to 为空"
        assert spec.applies_to <= ALL_AUDIENCES, f"{name}: applies_to 有非法取值"
        assert spec.required_for <= spec.applies_to, f"{name}: required_for 越界"


# ================================================================ 5. §14.4 硬错误码


def test_mens_hard_fail_codes_exist_and_audience_mismatch_is_its_own_code():
    """§12.4:AUDIENCE_MISMATCH 不能靠 GARMENT_WRONG 顶替。

    GARMENT_WRONG 的语义是"商品换了",而受众错配时商品往往是对的 ——
    错的是穿的人。两者混在一起会让重生策略选错方向。
    """
    values = {c.value for c in HardFailCode}
    for code in ("WAISTBAND_CHANGED", "INSEAM_CHANGED", "LINER_CHANGED"):
        assert code in values, f"§14.4 要求的男装码缺失:{code}"
    assert "AUDIENCE_MISMATCH" in values
    assert HardFailCode.AUDIENCE_MISMATCH is not HardFailCode.GARMENT_WRONG


def test_audience_mismatch_repairs_by_changing_the_model_not_the_seed():
    """重生策略的方向:换模特,不是换 seed。"""
    from app.evaluators.repair import REPAIR_TABLE

    action = REPAIR_TABLE[HardFailCode.AUDIENCE_MISMATCH]
    assert action.change_model_template is True
    assert action.change_seed is False, "换 seed 修不了'穿的人不对'"
    # 每条新码都要登记修复动作,否则只会得到兜底的"换 seed"
    for code in (
        HardFailCode.WAISTBAND_CHANGED,
        HardFailCode.INSEAM_CHANGED,
        HardFailCode.LINER_CHANGED,
    ):
        assert code in REPAIR_TABLE


# ================================================================ 6. §18.5 规则包


def _spec_text(name: str) -> str:
    return (SPEC_DIR / f"{name}.yaml").read_text(encoding="utf-8")


def _yaml_list(text: str, key: str) -> list[str]:
    """极简取列表:`key:` 之后连续的 `  - value` 行。不引 YAML 解析器。"""
    lines = text.splitlines()
    out: list[str] = []
    collecting = False
    for line in lines:
        if line.strip().startswith(f"{key}:"):
            collecting = True
            continue
        if collecting:
            stripped = line.strip()
            if stripped.startswith("- "):
                out.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("#"):
                break
    return out


def test_both_audience_rule_packs_exist_and_declare_their_audience():
    """§18.2 第 1 项:规则包必须声明受众。"""
    for name, expected in (("women_swimwear", "WOMEN"), ("men_swimwear", "MEN")):
        text = _spec_text(name)
        assert re.search(rf"^audience:\s*{expected}\s*$", text, re.M), (
            f"{name}.yaml 没有声明 audience: {expected}"
        )


def test_gendered_hard_fail_codes_are_not_enabled_in_the_other_pack():
    """§14.4:女装三条码在男装规则包里不启用,反之亦然。"""
    women = set(_yaml_list(_spec_text("women_swimwear"), "enabled_hard_fail_codes"))
    men = set(_yaml_list(_spec_text("men_swimwear"), "enabled_hard_fail_codes"))
    assert women and men, "启用清单是空的"

    womens_only = {"STRAP_CHANGED", "NECKLINE_CHANGED", "BACK_STYLE_CHANGED"}
    mens_only = {"WAISTBAND_CHANGED", "INSEAM_CHANGED", "LINER_CHANGED"}
    assert womens_only <= women and not (womens_only & men)
    assert mens_only <= men and not (mens_only & women)
    # 受众错配两个包里都要启用(§18.5 两份示例均含)
    assert "AUDIENCE_MISMATCH" in women and "AUDIENCE_MISMATCH" in men

    # 启用清单里的码必须真的在枚举里 —— 写错一个字母的表现是
    # 那条硬错误静默失效,而 spec 文件看上去毫无问题
    known = {c.value for c in HardFailCode}
    assert (women | men) <= known, f"未注册的码:{sorted((women | men) - known)}"


def test_review_checks_differ_between_audiences():
    """§14.2:男装那张表不是女装那张的改写。

    女装的"腰线"指连体泳衣的腰部收拢线,和男装的腰头是两回事,
    不能复用同一个检查项名字。
    """
    women = set(_yaml_list(_spec_text("women_swimwear"), "review_checks"))
    men = set(_yaml_list(_spec_text("men_swimwear"), "review_checks"))
    assert women and men
    assert "waistband_type" in men and "waistband_type" not in women
    assert "inseam_length" in men and "inseam_length" not in women
    assert "neckline" in women and "neckline" not in men
    assert "back_style" in women and "back_style" not in men
    # 两份结构相同、内容几乎无重叠 —— §18.5 说这正是受众必须独立成轴的证据
    assert len(women & men) <= 2, "两个受众的检查项重合太多,像是复制改写的"


def test_the_mens_pack_has_no_womens_structure_columns():
    """男装导出模板里不该有领型/背部/肩带列 —— 留着就是三个永远为空的列头。"""
    text = _spec_text("men_swimwear")
    for field in ("attr.neckline_type", "attr.back_style", "attr.strap_type"):
        assert field not in text, f"男装 spec 里不该出现 {field}"
    for field in ("attr.waistband_type", "attr.inseam_length", "attr.fit_type"):
        assert field in text, f"男装 spec 缺少 {field}"


# ================================================================ 7. §17 提交闸口


def test_a_mens_product_can_never_ship_with_the_womens_rule_pack():
    """§25.3 最后一条 + §17 第 3 条:这是本次改造的核心硬性质。

    它拦的正是 §0.2 那条 CATEGORY_ID 缺陷的后果:草稿用女装 spec 构造、
    商品是男装、所有字段校验都过。
    """
    blocked = draft_audience_gate(Audience.MEN, "women_swimwear")
    assert not blocked.ok
    assert "女装" in blocked.problems[0]

    # 受众前时代的旧包同样拦:确认了受众就必须重新生成草稿
    stale = draft_audience_gate(Audience.MEN, "swimwear")
    assert not stale.ok

    assert draft_audience_gate(Audience.MEN, "men_swimwear").ok
    assert draft_audience_gate(Audience.WOMEN, "women_swimwear").ok


def test_unconfirmed_audience_passes_only_with_a_pre_audience_pack():
    """存量兼容缝(见 audience_rules 模块文档)只对旧包成立。"""
    legacy = draft_audience_gate(None, "swimwear")
    assert legacy.ok, "存量商品不该被一条断言一夜之间全部拦住"
    assert legacy.warnings, "但必须说出来:受众未确认"

    # 草稿声明了受众、商品却没有:不许从草稿把受众"捡"回来(§4.2)
    inconsistent = draft_audience_gate(None, "men_swimwear")
    assert not inconsistent.ok


def test_a_garbage_audience_value_is_never_treated_as_unconfirmed():
    """库里出现错值时必须停下来,而不是走兼容缝。"""
    result = draft_audience_gate("WOMAN", "swimwear")
    assert not result.ok


def test_images_from_a_mismatched_model_block_submission():
    """§17 第 2 条:图片使用的模特受众必须与商品受众一致。"""
    assert not model_audience_problems(Audience.MEN, [Audience.MEN, Audience.MEN])
    problems = model_audience_problems(Audience.MEN, [Audience.MEN, Audience.WOMEN])
    assert len(problems) == 1
    # 同一模特的多张图只报一次
    repeated = model_audience_problems(
        Audience.MEN, [Audience.WOMEN, Audience.WOMEN, Audience.WOMEN]
    )
    assert len(repeated) == 1
    # UNISEX 商品:两种模特都不算问题
    assert not model_audience_problems(
        Audience.UNISEX, [Audience.WOMEN, Audience.MEN]
    )


# ================================================================ 8. §12.5 提示词


def test_each_audience_has_its_own_evaluator_prompt_key():
    """§12.5:受众编进提示词键,禁止在同一份提示词里用条件分支。"""
    from app.evaluators.vision_schema import prompt_key_for
    from app.services.prompt_rules import (
        KNOWN_KEYS,
        VISION_SYSTEM_PROMPT,
        VISION_SYSTEM_PROMPT_MEN,
    )

    assert VISION_SYSTEM_PROMPT != VISION_SYSTEM_PROMPT_MEN
    assert VISION_SYSTEM_PROMPT_MEN in KNOWN_KEYS
    assert prompt_key_for(Audience.MEN) == VISION_SYSTEM_PROMPT_MEN
    assert prompt_key_for(Audience.WOMEN) == VISION_SYSTEM_PROMPT
    # 受众未确认时不能落到男装那份 —— 女装是已校准的那一份
    assert prompt_key_for(None) == VISION_SYSTEM_PROMPT


def test_the_mens_prompt_is_a_separate_string_not_a_branch_in_the_womens():
    """§0.2 第二条:在女装那份里加"如果是男装则……"会平移女装的分档分布。

    这一条守的是**结构**:两份必须是各自独立的字符串常量。字节级不变的
    校验在阶段 4 的校准门禁里(历史样本重跑分档不变),那需要真实样本。
    """
    from app.evaluators.vision_schema import (
        DEFAULT_SYSTEM_PROMPT,
        DEFAULT_SYSTEM_PROMPT_MEN,
    )

    assert DEFAULT_SYSTEM_PROMPT_MEN != DEFAULT_SYSTEM_PROMPT
    # 女装那份里不许出现男装的条件分支
    for token in ("如果是男装", "男装泳裤", "腰头"):
        assert token not in DEFAULT_SYSTEM_PROMPT, (
            f"女装提示词里出现了 {token!r} —— 这会改变已校准的评分分布"
        )
    # 男装那份要真的覆盖 §14.2 的重点
    for token in ("腰头", "抽绳", "裤长", "内衬"):
        assert token in DEFAULT_SYSTEM_PROMPT_MEN
    # 男装那份要明确说清"不要为女装结构项编造判断"
    assert "不要为它们编造判断" in DEFAULT_SYSTEM_PROMPT_MEN


def test_response_format_name_carries_the_audience():
    """§12.5 列出的四个键名。"""
    from app.core.enums import EvaluationDepth
    from app.evaluators.vision_schema import response_format_name

    assert (
        response_format_name(EvaluationDepth.FULL, Audience.WOMEN)
        == "swimwear_women_image_evaluation_full"
    )
    assert (
        response_format_name(EvaluationDepth.QUICK, Audience.MEN)
        == "swimwear_men_image_evaluation_quick"
    )
    # 不传受众:保持旧名字,历史请求体的 diff 不该整片变红
    assert (
        response_format_name(EvaluationDepth.FULL)
        == "swimwear_image_evaluation_full"
    )


# ================================================================ 9. 阶段 0 缺陷


def test_nobody_builds_a_draft_from_the_module_level_category_constant():
    """阶段 0 验收:草稿的 category_id 可追溯到具体商品,不来自模块常量。

    §0.2 点名的五个位置。这条用源码扫描而不是跑一次构造:那五处分布在
    两个模块的不同函数里,跑通一条路径证明不了另外四处没有回潮。
    """
    import ast

    watched = {
        "app/workbench/service.py": "wb",
        "app/workbench/batch_service.py": "batch",
    }
    offenders: list[str] = []
    for rel in watched:
        tree = ast.parse((BACKEND_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 形如 `generic.CATEGORY_ID` 的属性访问
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "CATEGORY_ID"
                and isinstance(node.value, ast.Name)
                and node.value.id == "generic"
            ):
                offenders.append(f"{rel}:{node.lineno}")

    # batch_service 里保留**一处**:批次为空时的兜底类目(next(iter(...), 默认))。
    # 那不是"构造草稿时取常量",它在收敛检查之后、且批次为空时根本不会写文件。
    assert len(offenders) <= 1, (
        "这些位置又在用模块常量构造草稿类目(§0.2 的缺陷回潮):" + ", ".join(offenders)
    )


def test_field_spec_is_never_called_without_a_category_on_the_draft_paths():
    """同一条缺陷的另一半:`field_spec()` 无参调用会拿到默认类目。"""
    import ast

    for rel in ("app/workbench/service.py", "app/workbench/batch_service.py"):
        tree = ast.parse((BACKEND_ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "field_spec"
                and not node.args
                and not node.keywords
            ):
                raise AssertionError(
                    f"{rel}:{node.lineno} 无参调用 field_spec(),会拿到默认类目"
                )


# ================================================================ 10. 跨语言契约


def test_frontend_audience_labels_match_the_backend():
    """后端加一个受众取值而前端不加文案,界面上会冒出英文常量。"""
    ts = (PROJECT_ROOT / "frontend" / "src" / "api" / "types.ts").read_text(
        encoding="utf-8"
    )
    block = re.search(
        r"export const AUDIENCE_LABEL: Record<Audience, string> = \{(.*?)\}", ts, re.S
    )
    assert block, "types.ts 里找不到 AUDIENCE_LABEL"
    labelled = set(re.findall(r"^\s*([A-Z_]+):", block.group(1), re.M))
    assert labelled == {a.value for a in Audience}


def test_frontend_body_type_tables_match_the_backend_per_audience():
    """§10.4 的词表两边必须逐字一致 —— 前端宽一格,那个操作就"可以做到"了。"""
    ts = (PROJECT_ROOT / "frontend" / "src" / "api" / "types.ts").read_text(
        encoding="utf-8"
    )
    block = re.search(
        r"BODY_TYPES_BY_AUDIENCE: Record<'WOMEN' \| 'MEN', readonly string\[\]> = \{(.*?)\n\}",
        ts,
        re.S,
    )
    assert block, "types.ts 里找不到 BODY_TYPES_BY_AUDIENCE"
    for audience in (Audience.WOMEN, Audience.MEN):
        # 行首锚定:`MEN: [` 也能匹配到 `WOMEN: [` 的中间
        row = re.search(
            rf"^\s*{audience.value}: \[(.*?)\]", block.group(1), re.S | re.M
        )
        assert row, f"前端缺少 {audience.value} 的体型词表"
        front = {v.strip().strip("'\"") for v in row.group(1).split(",") if v.strip()}
        back = set(ar.body_types_allowed(audience))
        assert front == back, (
            f"{audience.value} 体型词表两边不一致:"
            f"前端多 {sorted(front - back)}、少 {sorted(back - front)}"
        )


def test_frontend_garment_types_match_the_backend_enum():
    """男装品类值加进后端而前端没加,下拉框里就选不到那几个品类。"""
    from app.core.enums import GarmentType

    ts = (PROJECT_ROOT / "frontend" / "src" / "api" / "types.ts").read_text(
        encoding="utf-8"
    )
    block = re.search(r"export const GARMENT_TYPES = \[(.*?)\] as const", ts, re.S)
    assert block, "types.ts 里找不到 GARMENT_TYPES"
    front = set(re.findall(r"'([A-Z_]+)'", block.group(1)))
    assert front == {g.value for g in GarmentType}


def test_the_import_template_has_an_audience_column():
    """§9.1:导入模板**必须有受众列**,且允许留空。"""
    from app.services.product_import import ALL_COLUMNS
    from app.workbench.import_plan import COLUMN_LABELS, TEMPLATE_COLUMNS

    assert "audience" in ALL_COLUMNS
    assert "audience" in TEMPLATE_COLUMNS
    assert "audience" in COLUMN_LABELS
    # 标签要说清留空的去向,否则运营会以为不填就是默认女装
    assert "待确认" in COLUMN_LABELS["audience"]


# ================================================================ 11. §3.4 规则 2


def test_one_spu_cannot_carry_two_audiences():
    """§3.4 规则 2:受众挂在 SPU 上,「有男款也有女款」是两个 SPU。

    `products` 是 SKU 级表,草稿是 SPU 级的。同 SPU 两行受众不同时,
    草稿的规则包取决于**查到哪一行** —— 这个不确定性最终会被 §17 第 3 条
    在导出闸口拦下,但那时错误指向草稿,而根因在几天前的那次导入。
    """
    from app.services.product_import import parse_csv

    result = parse_csv(
        "spu,sku,name,audience\n"
        "SPU-A,SKU-A1,女款,WOMEN\n"
        "SPU-A,SKU-A2,男款,MEN\n"
    )
    assert len(result.rows) == 1, "冲突行必须被拦下,不能两行都进"
    assert len(result.errors) == 1
    err = result.errors[0]
    assert err.field == "audience"
    assert err.row_number == 3, "错误要指到冲突的那一行"
    assert "第 2 行" in err.message, "并且要说清与哪一行冲突"


def test_blank_and_declared_audience_in_one_spu_is_also_inconsistent():
    """留空也是一种取值:它会让同 SPU 的一部分 SKU 进"待确认"、另一部分不进。"""
    from app.services.product_import import parse_csv

    mixed = parse_csv(
        "spu,sku,name,audience\nSPU-B,SKU-B1,填了,WOMEN\nSPU-B,SKU-B2,没填,\n"
    )
    assert len(mixed.errors) == 1

    # 整个 SPU 都留空则没问题 —— 那是"这个 SPU 待确认受众",状态一致
    all_blank = parse_csv(
        "spu,sku,name,audience\nSPU-C,SKU-C1,没填,\nSPU-C,SKU-C2,也没填,\n"
    )
    assert not all_blank.errors and len(all_blank.rows) == 2


def test_the_json_import_path_enforces_the_same_rule():
    """两条导入路径必须是同一句话,抄一份到另一条上迟早分叉。"""
    from app.services.product_import import parse_json_rows

    result = parse_json_rows(
        [
            {"spu": "S", "sku": "A", "name": "x", "audience": "MEN"},
            {"spu": "S", "sku": "B", "name": "y", "audience": "WOMEN"},
        ]
    )
    assert len(result.errors) == 1
    assert result.errors[0].field == "audience"


# ================================================================ 12. §14.4 启用码接线
#
# 这一组守的是**声明与接线之间的缝**。上面第 6 组已经证明两份 spec 里的
# 启用清单写对了 —— 但那只读 YAML 文本,完全没有触及"这份清单有没有人用"。
# 第一版就是这样:清单写对了、测试全绿,而评分请求体里照样摆着女装的码。


def test_the_enabled_code_list_actually_reaches_the_request_body():
    """§14.4:男装的评分请求体里不该出现 STRAP_CHANGED。

    摆在可选列表里,模型迟早会挑一个用,而下游没有任何一层会因为
    "男装不该有肩带问题"把它挡回去 —— scoring 层只丢弃**枚举外**的码。
    """
    import sys

    sys.path.insert(0, str(BACKEND_ROOT / "tests" / "pure"))
    from test_vision_evaluator import ResponsesAdapter, _config, _prepared

    from app.core.enums import EvaluationDepth

    adapter = ResponsesAdapter(_config())

    def build(**kw):
        return adapter.build(
            depth=EvaluationDepth.FULL,
            system_prompt="S",
            user_prompt="U",
            references=[_prepared("R1")],
            candidate=_prepared("C"),
            **kw,
        )

    men_codes = ["GARMENT_WRONG", "WAISTBAND_CHANGED", "AUDIENCE_MISMATCH"]
    fmt = build(audience=Audience.MEN, allowed_hard_fail_codes=men_codes).body["text"][
        "format"
    ]
    assert fmt["name"] == "swimwear_men_image_evaluation_full"
    enum = fmt["schema"]["properties"]["hard_fail_codes"]["items"]["enum"]
    assert "STRAP_CHANGED" not in enum
    assert set(enum) == set(men_codes)

    # 受众前时代的调用:名字和码数都不变。这条是回归保护 ——
    # 收紧新路径不该动到已在跑的那条
    legacy = build().body["text"]["format"]
    assert legacy["name"] == "swimwear_image_evaluation_full"
    assert len(legacy["schema"]["properties"]["hard_fail_codes"]["items"]["enum"]) == len(
        HardFailCode
    )


def test_every_adapter_accepts_the_audience_parameters():
    """两个适配器 + 基类的 `build` 签名必须一致。

    第一版只改了基类和调用点,两个具体适配器没跟上 —— 那不是启动期错误,
    是**运行期 TypeError**:调用方按基类签名传参,打到一个不认识它的实现上。
    """
    import inspect
    import sys

    sys.path.insert(0, str(BACKEND_ROOT / "tests" / "pure"))
    from app.evaluators.vision import ChatCompletionsAdapter, ResponsesAdapter

    for adapter_cls in (ResponsesAdapter, ChatCompletionsAdapter):
        params = inspect.signature(adapter_cls.build).parameters
        for name in ("audience", "allowed_hard_fail_codes"):
            assert name in params, f"{adapter_cls.__name__}.build 缺少 {name}"


def test_the_evaluator_falls_back_per_audience_not_to_the_womens_prompt():
    """兜底也要分受众:取不到运营那版时,男装不能落到女装那份已校准的提示词上。"""
    from app.evaluators.vision import (
        AUDIENCE_KEY,
        VisionEvaluatorConfig,
        VisionModelImageQualityEvaluator,
        audience_from,
        enabled_codes_from,
    )
    from app.evaluators.vision_schema import (
        DEFAULT_SYSTEM_PROMPT,
        DEFAULT_SYSTEM_PROMPT_MEN,
    )

    ev = VisionModelImageQualityEvaluator(VisionEvaluatorConfig())
    assert ev.system_prompt({AUDIENCE_KEY: "MEN"}) is DEFAULT_SYSTEM_PROMPT_MEN
    assert ev.system_prompt({AUDIENCE_KEY: "WOMEN"}) is DEFAULT_SYSTEM_PROMPT
    # 受众取不到时落到**已校准**的那一份,不是男装那份未校准的
    assert ev.system_prompt({}) is DEFAULT_SYSTEM_PROMPT

    # 坏值降级成 None 而不是抛错:受众读失败不该让整批评分停摆
    assert audience_from({AUDIENCE_KEY: "MAN"}) is None
    assert enabled_codes_from({}) is None


# ================================================================ 13. 全受众可达性
#
# 第 6 组只断言了**派生出的字符串**,从没真正加载过那个 spec ——
# 于是 UNISEX 的规则包文件缺失了整整一轮没被发现:那不是"少一份配置",
# 是中性款商品在 `_current_draft_data` 直接抛 ChannelSpecError,
# 把整个工作台列表打挂。这一组守的是"枚举里的每个取值都真的走得通"。


def test_every_audience_value_resolves_to_a_loadable_rule_pack():
    """`Audience` 里的每个取值都必须能派生出一份**加载得动**的规则包。

    加一个受众取值而不加规则包,表现不是"这个受众没配置",
    是那个受众的商品整片打不开。
    """
    from app.channels import generic

    class _Product:
        def __init__(self, audience):
            self.audience = audience
            self.category = "swimwear"

    for audience in (*Audience, None):
        product = _Product(audience.value if audience else None)
        category_id = generic.category_id_for(product)
        spec = generic.field_spec(category_id=category_id)
        assert spec.header_fields, f"{category_id} 的规则包是空的"
        if audience is not None:
            assert spec.audience == audience.value, (
                f"{category_id} 声明的受众({spec.audience})与派生它的受众"
                f"({audience.value})不一致"
            )


def test_spec_required_fields_never_exceed_what_the_registry_requires():
    """导出必填不能严于属性必填,否则出现"导出要求填、属性页不要求填"的死锁。

    属性页按 `required_for_listing(audience)` 出必填清单,运营照着填完、
    流程显示完成 —— 然后导出闸口说缺字段,而界面上没有任何地方能补它。
    """
    from app.attributes.registry import REGISTRY, required_for_listing
    from app.channels import generic

    for audience in Audience:
        spec = generic.field_spec(
            category_id=ar.category_code_for(audience, "swimwear")
        )
        registry_required = set(required_for_listing(audience))
        for field in spec.header_fields:
            if not field.required:
                continue
            source = str(getattr(field, "source", "") or "")
            if not source.startswith("attr."):
                continue  # 手填字段与文案字段不归注册表管
            name = source.split(".", 1)[1]
            if name not in REGISTRY:
                continue
            assert name in registry_required, (
                f"{spec.category_id}:导出把 {name} 列为必填,但注册表里它对"
                f"{audience.value}不是必填 —— 属性页不会要求运营填它"
            )


# ================================================================ 14. §11.3 授权执行
#
# 同一条"声明与接线之间的缝":授权字段组存了 12 个字段,第一版只有 2 个
# 被读。存下来却从不检查,等于把一份合规记录做成装饰。


def _template(**kw):
    """脱库的模特模板替身。授权判定是纯逻辑,不该为了测它去起一个库。"""

    class _T:
        def __init__(self):
            self.license_status = "UNVERIFIED"
            self.license_expires_at = None
            self.allow_ai_dressing = False
            self.prohibited_categories = []
            self.__dict__.update(kw)

    return _T()


def test_unverified_licenses_do_not_block_new_tasks():
    """0030 的存量取向:UNVERIFIED 进待补清单,不进阻断。

    对 UNVERIFIED 执行 `allow_ai_dressing`(默认 False)会让存量模板全部停摆,
    而那与迁移里记的取向直接矛盾 —— 默认 False 的含义是"没人核实过",
    不是"授权禁止"。
    """
    from app.services.model_license import assert_usable

    assert_usable(_template())  # 不抛即通过


def test_expired_or_revoked_licenses_block_new_tasks():
    from datetime import UTC, datetime, timedelta

    from app.core.errors import ValidationError
    from app.services.model_license import assert_usable

    for status in ("EXPIRED", "REVOKED"):
        expect_raises(ValidationError, assert_usable, _template(license_status=status))

    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    # 状态没人改、但到期日过了 —— 同样阻断
    expect_raises(
        ValidationError,
        assert_usable,
        _template(license_status="LICENSED", license_expires_at=past),
    )


def test_a_verified_license_that_forbids_ai_dressing_is_enforced():
    """一旦有人把状态改成 LICENSED,那份条款的限制从那一刻起是权威的。"""
    from app.core.errors import ValidationError
    from app.services.model_license import assert_usable

    err = expect_raises(
        ValidationError,
        assert_usable,
        _template(license_status="LICENSED", allow_ai_dressing=False),
    )
    # §5.1:普通运营看不到授权细节,只看到"暂不可用"
    assert "暂不可用" in str(err)

    # 允许换装的已授权模特照常可用
    assert_usable(_template(license_status="LICENSED", allow_ai_dressing=True))


def test_prohibited_categories_are_checked_against_the_rule_pack_key():
    """禁用品类按规则包键比对 —— 与草稿用的是同一个派生。"""
    from app.core.errors import ValidationError
    from app.services.model_license import assert_usable

    licensed = dict(
        license_status="LICENSED",
        allow_ai_dressing=True,
        prohibited_categories=["men_swimwear"],
    )
    expect_raises(
        ValidationError,
        assert_usable,
        _template(**licensed),
        category_id="men_swimwear",
    )
    # 别的品类不受影响;不传品类时这一条跳过,其余三条照常判
    assert_usable(_template(**licensed), category_id="women_swimwear")
    assert_usable(_template(**licensed))


# ================================================================ 15. 下发即消费
#
# 后端 `_product_out` 下发了 audience / audience_label / review_focus,
# 而前端一处都没读 —— 这是"声明但没接线"在**跨语言边界**上的版本。
# 它比后端内部那两处更难发现:两边各自的测试都是绿的。


def test_the_audience_fields_the_api_emits_are_actually_consumed():
    """后端下发的受众三件套必须在前端类型里存在,且真的被某个组件读到。

    只加类型不算接线 —— 类型是给编译器看的,运营看的是界面。
    所以这里同时断言"类型里有"和"有组件在读"。
    """
    frontend = PROJECT_ROOT / "frontend" / "src"
    api_types = (frontend / "api" / "workbench.ts").read_text(encoding="utf-8")

    block = re.search(
        r"export interface WorkbenchProduct \{(.*?)\n\}", api_types, re.S
    )
    assert block, "workbench.ts 里找不到 WorkbenchProduct"
    for field in ("audience", "audience_label", "review_focus"):
        assert re.search(rf"^\s*{field}[?]?:", block.group(1), re.M), (
            f"后端 _product_out 下发了 {field},但前端类型里没有它"
        )

    # 至少一个组件真的读了它们
    components = "\n".join(
        path.read_text(encoding="utf-8")
        for path in frontend.rglob("*.tsx")
    )
    for field in ("audience_label", "review_focus"):
        assert f"product.{field}" in components, (
            f"前端类型里有 {field},但没有任何组件读它 —— 界面上仍然看不到"
        )


def test_the_backend_really_emits_those_three_fields():
    """反向:前端读的这三个键,后端 `_product_out` 必须真的产出。

    两侧任一边先改都会被这一对断言拦住 —— 这正是上一轮缺的那道网。
    """
    source = (BACKEND_ROOT / "app" / "api" / "workbench.py").read_text(
        encoding="utf-8"
    )
    body = re.search(r"def _product_out\(.*?\n    \}", source, re.S)
    assert body, "workbench.py 里找不到 _product_out"
    for key in ("audience", "audience_label", "review_focus"):
        assert f'"{key}"' in body.group(0), f"_product_out 不再下发 {key}"
