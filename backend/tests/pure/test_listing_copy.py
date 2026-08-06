"""M6:claims 校验、内容计划、草稿 STALE(零三方依赖)。

M6 的出口条件是「**人造反例能被拦下**」。这个文件就是那批人造反例 ——
每一条都对应一种模型真的会犯的错。
"""
from __future__ import annotations

import ast

from app.attributes.contracts import AttrValue, CanonicalProduct
from app.core.enums import AttributeSource, AttributeStatus, DraftStatus, ImageSetStatus, MediaRole
from app.listings import claim_terms
from app.listings.content_plan import (
    CERTIFIED_CLAIM_VARIANTS,
    CLAIMS_REQUIRING_CERTIFICATION,
    build_plan,
    canonical_claim_id,
    certified_claim_ids,
    derive_forbidden_claims,
    filter_selling_points,
    trace_selling_points,
)
from app.listings.contracts import (
    CopySnapshot,
    ImageItemSnapshot,
    ImageSetSnapshot,
    ListingDraftData,
)
from app.listings.copy_rules import (
    CopyRules,
    CopyViolationCode,
    blocking,
    is_publishable,
    sanitize_claims,
    sanitize_copy,
    scan_synonyms,
    validate_claims,
    validate_copy,
    validate_payload,
    validate_structure,
    validate_wording,
)
from app.listings.draft import can_export, is_stale, missing_prerequisites, refresh_status
from tests.pure._helpers import BACKEND_ROOT

APP_DIR = BACKEND_ROOT / "app"

FACTS = {"primary_color": "NAVY_BLUE", "garment_type": "ONE_PIECE", "neckline_type": "V_NECK"}

GOOD_COPY = {
    "title": "Navy blue one-piece swimsuit with V-neck",
    "description": "A flattering one-piece in navy blue.",
    "bullet_points": ["Adjustable straps", "Removable padding", "Quick dry"],
}
GOOD_CLAIMS = [
    {"field": "primary_color", "value": "NAVY_BLUE", "text_span": "Navy blue", "location": "title"},
    {"field": "garment_type", "value": "ONE_PIECE", "text_span": "one-piece", "location": "title"},
]


def _codes(violations):
    return [v.code for v in violations]


# ---------------------------------------------------------------- claims 反例


def test_a_correct_copy_passes():
    assert validate_claims(GOOD_COPY, GOOD_CLAIMS, FACTS) == []


def test_model_invents_an_attribute_it_was_never_given():
    """模型宣称了一个事实源里没有的属性 —— 它在编。"""
    claims = [*GOOD_CLAIMS, {"field": "material", "value": "SILK",
                             "text_span": "silk", "location": "title"}]
    assert CopyViolationCode.CLAIM_NOT_IN_FACTS in _codes(
        validate_claims(GOOD_COPY, claims, FACTS)
    )


def test_model_claims_a_different_value_than_the_facts():
    """标注的值和事实源对不上。"""
    claims = [{"field": "primary_color", "value": "BLACK",
               "text_span": "Navy blue", "location": "title"}]
    assert CopyViolationCode.CLAIM_VALUE_MISMATCH in _codes(
        validate_claims(GOOD_COPY, claims, FACTS)
    )


def test_claims_detached_from_the_actual_text():
    """标注得很漂亮,而正文里根本没写这件事。

    这是最隐蔽的一种:claims 全对、facts 全对,但文案里没有那句话。
    只校验 claims 本身是抓不到的,必须回正文里逐字找。
    """
    claims = [{"field": "primary_color", "value": "NAVY_BLUE",
               "text_span": "deep ocean blue", "location": "title"}]
    assert CopyViolationCode.CLAIM_SPAN_NOT_FOUND in _codes(
        validate_claims(GOOD_COPY, claims, FACTS)
    )


def test_claim_pointing_at_a_nonexistent_location():
    claims = [{"field": "primary_color", "value": "NAVY_BLUE",
               "text_span": "Navy blue", "location": "bullet.9"}]
    assert CopyViolationCode.CLAIM_LOCATION_INVALID in _codes(
        validate_claims(GOOD_COPY, claims, FACTS)
    )
    bad_loc = [{"field": "primary_color", "value": "NAVY_BLUE",
                "text_span": "x", "location": "footer"}]
    assert CopyViolationCode.CLAIM_LOCATION_INVALID in _codes(
        validate_claims(GOOD_COPY, bad_loc, FACTS)
    )


def test_required_facts_must_be_claimed():
    """颜色和品类没写进文案 —— 消费者搜不到这个商品。"""
    only_neckline = [{"field": "neckline_type", "value": "V_NECK",
                      "text_span": "V-neck", "location": "title"}]
    codes = _codes(validate_claims(GOOD_COPY, only_neckline, FACTS))
    assert codes.count(CopyViolationCode.REQUIRED_FACT_NOT_CLAIMED) == 2


def test_bullet_location_resolves_by_index():
    claims = [
        *GOOD_CLAIMS,
        {"field": "neckline_type", "value": "V_NECK",
         "text_span": "Adjustable", "location": "bullet.0"},
    ]
    # span 在 bullet.0 里确实存在,所以只有值不匹配之外没有别的问题
    assert CopyViolationCode.CLAIM_SPAN_NOT_FOUND not in _codes(
        validate_claims(GOOD_COPY, claims, FACTS)
    )


# ---------------------------------------------------------------- 结构与措辞


def test_length_is_counted_by_character():
    """按字符计,不按字节 —— 中文标题按字节算会莫名其妙超长。"""
    rules = CopyRules(title_max=10, title_min=1)
    long_cn = {"title": "藏青色连体泳衣带V领设计款", "bullet_points": ["a", "b", "c"]}
    codes = _codes(validate_structure(long_cn, rules))
    assert CopyViolationCode.TOO_LONG in codes
    ok_cn = {"title": "藏青连体泳衣", "bullet_points": ["a", "b", "c"]}
    assert CopyViolationCode.TOO_LONG not in _codes(validate_structure(ok_cn, rules))


def test_bullet_count_is_enforced():
    rules = CopyRules(bullet_min=3, bullet_max=5)
    few = {"title": "x" * 20, "bullet_points": ["a"]}
    assert CopyViolationCode.WRONG_BULLET_COUNT in _codes(validate_structure(few, rules))


def test_placeholders_are_caught():
    """模型偶尔把提示词模板原样吐出来。"""
    for text in ("Buy {product_name} now", "[TODO] fill this", "Lorem ipsum", "A <color> suit"):
        copy = {"title": text + " " * 20, "bullet_points": ["a", "b", "c"]}
        assert CopyViolationCode.PLACEHOLDER_LEFT in _codes(
            validate_structure(copy, CopyRules(title_min=1))
        )


def test_banned_words_and_forbidden_claims_are_separate():
    """禁词是"渠道不许出现",禁止宣称是"这件商品不能这么说"。

    维护方完全不同,合成一个列表会让"给这个 SPU 加一条例外"
    变成"给整个渠道加一条例外"。
    """
    rules = CopyRules(banned_words=("cheap",))
    copy = {"title": "A cheap UPF50+ swimsuit", "bullet_points": ["a", "b", "c"]}
    codes = _codes(validate_wording(copy, rules, forbidden_claims=("UPF50+",)))
    assert CopyViolationCode.BANNED_WORD in codes
    assert CopyViolationCode.FORBIDDEN_CLAIM in codes


# ---------------------------------------------------------------- 硬失败 vs warning


def test_synonym_mismatch_is_only_a_warning():
    """同义词扫描做成硬失败会把正常文案全拦下来。

    词典不可能覆盖所有颜色说法,而且 "black buckle" 里的 black
    本来就不指主体色。硬失败的结局是有人把整条检查删掉。
    """
    warnings = scan_synonyms(
        {"title": "Suit with a black buckle", "bullet_points": []},
        FACTS,
        {"black": "BLACK"},
    )
    assert warnings and all(not w.is_blocking for w in warnings)
    assert is_publishable(warnings), "warning 不该挡住发布"


def test_blocking_filter_separates_the_two():
    everything = validate_copy(
        {"title": "x", "bullet_points": []}, [], FACTS, CopyRules()
    )
    assert blocking(everything), "结构问题必须是硬失败"
    assert not is_publishable(everything)


# ---------------------------------------------------------------- 内容计划


def test_uncertified_claims_are_forbidden_by_default():
    """默认全禁,不是默认全放。

    一个漏配的检测报告清单,后果是文案里出现没有依据的功效宣称,
    而那在多数站点是下架级别的问题。
    """
    forbidden = derive_forbidden_claims(certified=())
    for claim in CLAIMS_REQUIRING_CERTIFICATION:
        assert claim in forbidden


def test_certified_claims_are_allowed():
    forbidden = derive_forbidden_claims(certified=("UPF50+",))
    assert "UPF50+" not in forbidden
    assert "抗菌" in forbidden


def test_untraceable_selling_points_are_dropped():
    """卖点必须能追溯到某个 fact 或人工卖点库。

    不丢的话,模型会稳定地生成一批读起来很好、但和这件商品无关的卖点 ——
    「采用高弹面料」在它不知道面料的情况下照样写得出来。
    """
    kept = filter_selling_points(
        ["Navy blue shade", "采用高弹面料", "Quick dry"],
        FACTS,
        manual_library=("Quick dry",),
    )
    assert "Navy blue shade" in kept        # 追溯到 primary_color
    assert "Quick dry" in kept              # 人工卖点库
    assert "采用高弹面料" not in kept        # 追溯不到


def test_plan_facts_come_only_from_what_was_passed_in():
    plan = build_plan(spu="SW-1", confirmed={"primary_color": "NAVY_BLUE", "material": ""})
    assert plan.facts == {"primary_color": "NAVY_BLUE"}, "空值不该进 facts"


# ---------------------------------------------------------------- 草稿 STALE


def _draft(**kw):
    image_set = ImageSetSnapshot(
        image_set_id="is1", spu="SW-1", version=1,
        status=ImageSetStatus.APPROVED.value,
        items=(ImageItemSnapshot("m1", MediaRole.PRODUCT_FRONT, 0, "p", 800, 800,
                                 is_primary=True),),
    )
    product = CanonicalProduct(
        spu="SW-1",
        spu_attrs={"garment_type": AttrValue(
            field_name="garment_type", value="ONE_PIECE",
            source=AttributeSource.EXTRACTED, status=AttributeStatus.CONFIRMED,
            version_id="v1")},
    )
    base = dict(spu="SW-1", channel="SHEIN", site="BR", category_id="c1",
                product=product, image_set=image_set,
                copies={"en": CopySnapshot(copy_id="c-en", locale="en", version=1)})
    base.update(kw)
    return ListingDraftData(**base)


def test_fingerprint_detects_every_upstream_change():
    import dataclasses

    base = _draft()
    stored = base.source_fingerprint()
    assert not is_stale(stored, base)

    # 文案出了新版本
    newer_copy = {"en": CopySnapshot(copy_id="c-en", locale="en", version=2)}
    assert is_stale(stored, dataclasses.replace(base, copies=newer_copy))

    # 图片集出了新版本
    newer_set = dataclasses.replace(base.image_set, version=2)
    assert is_stale(stored, dataclasses.replace(base, image_set=newer_set))

    # 运营改了手填字段
    assert is_stale(stored, dataclasses.replace(base, manual={"price": "9.9"}))


def test_stale_drafts_cannot_be_exported():
    """**STALE 草稿不允许导出与提交。**

    只看状态的话,一份"校验通过"的草稿会在属性被改之后继续可以提交,
    而提交上去的是旧事实。
    """
    base = _draft()
    stored = base.source_fingerprint()
    assert can_export(DraftStatus.VALIDATED.value, stored, base)
    assert not can_export(DraftStatus.VALIDATED.value, "outdated-fingerprint", base)
    assert not can_export(DraftStatus.STALE.value, stored, base)
    assert not can_export(DraftStatus.BUILDING.value, stored, base)


def test_refresh_marks_stale_but_leaves_archived_alone():
    base = _draft()
    assert refresh_status(DraftStatus.VALIDATED.value, "old", base) == DraftStatus.STALE.value
    # 归档的草稿不该因为上游变了就变成 STALE —— 它已经归档了
    assert refresh_status(DraftStatus.ARCHIVED.value, "old", base) == DraftStatus.ARCHIVED.value


def test_prerequisites_require_approved_artifacts():
    """发布只依赖已批准物(硬规矩 5),**不读生成任务状态**。"""
    problems = missing_prerequisites(
        image_set_status="PENDING_REVIEW",
        copy_statuses={"en": "DRAFT"},
        required_locales=("en", "pt-BR"),
    )
    assert any("图片集尚未批准" in p for p in problems)
    assert any("en 的文案尚未批准" in p for p in problems)
    assert any("缺少 pt-BR" in p for p in problems)

    assert missing_prerequisites(
        image_set_status="APPROVED",
        copy_statuses={"en": "APPROVED"},
        required_locales=("en",),
    ) == []


# ---------------------------------------------------------------- 结构约束


def test_copy_rules_module_is_dependency_free():
    for name in ("copy_rules.py", "content_plan.py", "draft.py"):
        tree = ast.parse((APP_DIR / "listings" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for n in names:
                assert not n.startswith("sqlalchemy"), f"{name} 依赖了数据库"


# 以下来自已删除的 test_code_review_round3.py(第三轮评审的阶段性容器文件)。
# 按被测模块归位,而不是按评审轮次分组 —— 否则同一个模块的测试会散落在多个文件里。


# ---------------------------------------------------------------- 源码静态检查工具

def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _fn(tree, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _calls(node) -> set[str]:
    """这段代码里出现的调用名,归一成 `模块.函数` 或 `函数`。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                names.add(f"{func.value.id}.{func.attr}")
            names.add(func.attr)
    return names


# ---------------------------------------------------------------- 1 声明背书


def test_an_unrelated_word_cannot_vouch_for_an_attribute():
    """评审第 1 条:用任意存在的单词给不存在的属性背书。

    这是最要命的一种 —— claims 全对、span 逐字能找到、facts 也对得上,
    只是那段文字和它声称的属性毫无关系。只做"片段是否出现"的校验
    对它完全无感,而模型偏偏很擅长这么干。
    """
    copy = {
        "title": "Summer swimsuit for the beach season",
        "bullet_points": ["a", "b", "c"],
    }
    claims = [
        # 用 "swimsuit" 证明颜色
        {"field": "primary_color", "value": "NAVY_BLUE",
         "text_span": "swimsuit", "location": "title"},
        # 用 "Summer" 证明款式
        {"field": "garment_type", "value": "ONE_PIECE",
         "text_span": "Summer", "location": "title"},
    ]
    codes = _codes(validate_claims(copy, claims, FACTS))
    assert codes.count(CopyViolationCode.CLAIM_SPAN_NOT_EVIDENCE) == 2
    # 背书不成立 -> 这个字段就不算被 claim 过 -> 必填属性缺失也要报
    assert CopyViolationCode.REQUIRED_FACT_NOT_CLAIMED in codes


def test_a_registered_localized_wording_still_passes():
    """本地化写法不能被误伤 —— 否则这条检查很快就会被关掉。"""
    copy = {
        "title": "Maiô azul marinho decote V",
        "bullet_points": ["a", "b", "c"],
    }
    claims = [
        {"field": "primary_color", "value": "NAVY_BLUE",
         "text_span": "azul marinho", "location": "title"},
        {"field": "garment_type", "value": "ONE_PIECE",
         "text_span": "Maiô", "location": "title"},
    ]
    assert validate_claims(copy, claims, FACTS) == []


def test_an_unregistered_short_value_cannot_be_verified_and_is_blocked():
    """词表查不到、值本身又太短 -> 拦下,不是放过。

    默认放过的话,`size=M` 这类属性等于没有校验,而"这件是 M 码"
    恰恰是消费者退货时会引用的那句话。
    """
    facts = {"size": "M", "primary_color": "NAVY_BLUE", "garment_type": "ONE_PIECE"}
    copy = {"title": "Medical grade navy blue one piece", "bullet_points": ["a", "b", "c"]}
    claims = [{"field": "size", "value": "M", "text_span": "Medical", "location": "title"}]
    assert CopyViolationCode.CLAIM_SPAN_NOT_EVIDENCE in _codes(
        validate_claims(copy, claims, facts)
    )


def test_a_list_valued_fact_matches_any_of_its_elements():
    """列表型事实(secondary_colors)按元素比,不按整个列表的字面量比。

    生成器本来就是取第一个元素写进 claim 的,要求 claim 复述整个列表
    会把正确的文案判成不一致。
    """
    facts = {"secondary_colors": ["WHITE", "BLACK"]}
    copy = {"title": "Suit with white trim detail", "bullet_points": []}
    claims = [{"field": "secondary_colors", "value": "WHITE",
               "text_span": "white", "location": "title"}]
    codes = _codes(validate_claims(copy, claims, facts, required_facts=()))
    assert CopyViolationCode.CLAIM_VALUE_MISMATCH not in codes
    assert CopyViolationCode.CLAIM_SPAN_NOT_EVIDENCE not in codes


# ---------------------------------------------------------------- 2 结构闸门


def test_a_numeric_title_is_a_violation_not_a_crash():
    """评审第 2 条:`title` 是整数时以前抛 TypeError,变成一个 500。"""
    copy = {"title": 12345, "bullet_points": ["a", "b", "c"]}
    codes = _codes(validate_copy(copy, [], FACTS, CopyRules()))
    assert codes == [CopyViolationCode.MALFORMED_OUTPUT]


def test_a_none_inside_claims_is_a_violation_not_a_crash():
    """`claims` 里混进 None 时以前抛 AttributeError。"""
    copy = {"title": "x" * 20, "bullet_points": ["a", "b", "c"]}
    codes = _codes(validate_copy(copy, [None], FACTS, CopyRules()))
    assert CopyViolationCode.MALFORMED_OUTPUT in codes


def test_a_string_bullet_list_is_not_silently_split_into_characters():
    """最隐蔽的一个:`list("abc")` 得到三个单字符,校验**通过**。

    它不报错,只是给出一个错的结论 —— 界面上显示"卖点 3 条,已通过"。
    """
    copy = {"title": "x" * 20, "bullet_points": "abc"}
    codes = _codes(validate_copy(copy, [], FACTS, CopyRules()))
    assert CopyViolationCode.MALFORMED_OUTPUT in codes
    # 结构闸门之外,单独跑结构规则时也不能把字符串当成三条卖点
    assert CopyViolationCode.WRONG_BULLET_COUNT in _codes(
        validate_structure(copy, CopyRules())
    )


def test_structure_failure_stops_before_the_rules_run():
    """结构不对就不再跑规则 —— 从错形状推出来的结论会把真正的原因埋掉。"""
    codes = _codes(validate_copy({"title": []}, [], FACTS, CopyRules()))
    assert set(codes) == {CopyViolationCode.MALFORMED_OUTPUT}


def test_malformed_output_is_still_storable():
    """结构错的那一版也要落库 —— 「模型到底吐了什么」是运营要看的东西。"""
    safe = sanitize_copy({"title": 5, "bullet_points": "abc", "keywords": None})
    assert safe["title"] == "5"
    assert safe["bullet_points"] == []
    assert safe["keywords"] == []
    assert sanitize_claims([None, {"field": "a"}, "x"]) == [{"field": "a"}]


def test_validate_payload_accepts_a_well_formed_object():
    assert validate_payload(
        {"title": "t", "description": "d", "bullet_points": ["a"], "keywords": [],
         "extra": {}},
        [{"field": "primary_color", "value": "NAVY_BLUE",
          "text_span": "navy", "location": "title"}],
    ) == []


# ---------------------------------------------------------------- 3 卖点溯源


def test_a_one_letter_size_cannot_justify_a_selling_point():
    """评审第 3 条:短值几乎能匹配任意文本。

    `size=M` 让「Medical grade compression」通过溯源,而这条卖点
    既没有依据,又是个法规敏感词(医用)。
    """
    facts = {"size": "M", "cup": "S", "garment_type": "ONE_PIECE"}
    kept = filter_selling_points(
        ["Medical grade compression", "Chlorine resistant fabric"], facts
    )
    assert kept == ()


def test_selling_points_match_on_word_boundaries():
    """`TOP` 不该在 `Stopper` 里命中。"""
    facts = {"garment_type": "TOP"}
    assert filter_selling_points(["Built-in stopper clip"], facts) == ()
    assert filter_selling_points(["Bikini top with soft cups"], facts) == (
        "Bikini top with soft cups",
    )


def test_selling_points_carry_their_source():
    """保留下来的卖点必须能回答"凭什么保留"。"""
    traced = trace_selling_points(
        ["Navy blue shade", "Quick dry"], FACTS, manual_library=("Quick dry",)
    )
    by_text = {t.text: t for t in traced}
    assert by_text["Navy blue shade"].origin == "fact"
    assert by_text["Navy blue shade"].field_name == "primary_color"
    assert by_text["Navy blue shade"].matched_term == "navy blue"
    assert by_text["Quick dry"].origin == "manual"


# ---------------------------------------------------------------- 8 审批


def test_approval_requires_validated_state_and_rechecks_the_rules():
    """评审第 8 条:只读历史 violations 有三条路能绕过去。

    状态不限(一版 DRAFT 的 violations 是空的)、规则不查、不重跑。
    """
    tree = _tree(APP_DIR / "listings" / "copy_service.py")
    fn = _fn(tree, "approve_copy")
    source = ast.unparse(fn)
    assert "CopyStatus.VALIDATED" in source, "审批没有限定 VALIDATED 状态"
    assert "spec_version" in source, "审批没有比较规则版本"
    assert "revalidate" in _calls(fn), "规则换版之后没有重跑校验"
    # 重跑必须把新结论写回去 —— 批不批得成另说,结论要留在库里
    assert "session.flush" in _calls(_fn(tree, "revalidate"))


# ---------------------------------------------------------------- 12 审计


def test_copy_generation_is_audited():
    """评审第 12 条:`actor` 是个没被用到的形参。

    文案是要对外发布的东西,"这一版是谁生成的、用哪个模型、哪一版提示词"
    出问题时是第一个要回答的。
    """
    fn = _fn(_tree(APP_DIR / "listings" / "copy_service.py"), "save_copy")
    assert "audit.record" in _calls(fn), "save_copy 没有写审计"
    payload = ast.unparse(fn)
    for key in ("model_name", "prompt_version", "spec_version", "violation_codes"):
        assert key in payload, f"审计负载里缺少 {key}"
    # 正文不进审计表:它已经在 listing_copies 里了,复制一份既没有新信息,
    # 又多一处要脱敏的地方
    assert '"title"' not in payload.split("audit.record")[-1]


# ---------------------------------------------------------------- 13 归一化


def test_banned_words_survive_unicode_evasion():
    """评审第 13 条:连字符、全角、零宽字符都能绕过 lower() 子串比对。"""
    rules = CopyRules(banned_words=("cheap",))
    for text in ("A ｃｈｅａｐ swimsuit", "A che\u200bap swimsuit", "A CHEAP swimsuit"):
        copy = {"title": text, "bullet_points": ["a", "b", "c"]}
        assert CopyViolationCode.BANNED_WORD in _codes(
            validate_wording(copy, rules)
        ), f"{text!r} 绕过了禁词检查"


def test_banned_words_do_not_hit_inside_longer_words():
    """误伤同样致命 —— 被误伤几次之后,这条检查就会被整个关掉。"""
    rules = CopyRules(banned_words=("sale",))
    copy = {"title": "Wholesale ready swimwear", "bullet_points": ["a", "b", "c"]}
    assert CopyViolationCode.BANNED_WORD not in _codes(validate_wording(copy, rules))


def test_forbidden_claims_catch_every_registered_spelling():
    """`UPF-50+` / `UPF 50+` / `ＵＰＦ50+` 都要拦住。"""
    forbidden = derive_forbidden_claims(certified=())
    for text in ("Fabric with UPF-50+ rating", "Fabric with UPF 50+ rating",
                 "Fabric with ＵＰＦ50+ rating"):
        copy = {"title": text, "bullet_points": ["a", "b", "c"]}
        assert CopyViolationCode.FORBIDDEN_CLAIM in _codes(
            validate_wording(copy, CopyRules(), forbidden_claims=forbidden)
        ), f"{text!r} 绕过了禁止宣称"


def test_one_prohibition_reports_once():
    """同一处禁令的多个写法只报一条 —— 否则一句话触发三条,列表全是噪音。"""
    forbidden = derive_forbidden_claims(certified=())
    copy = {"title": "Fabric with UPF50+ rating", "bullet_points": ["a", "b", "c"]}
    hits = [
        v for v in validate_wording(copy, CopyRules(), forbidden_claims=forbidden)
        if v.code == CopyViolationCode.FORBIDDEN_CLAIM
    ]
    assert len(hits) == 1


# ---------------------------------------------------------------- 14 认证 ID


def test_certification_is_recognised_by_id_not_by_spelling():
    """评审第 14 条:`UPF50+` / `UPF 50+` / `UPF-50+` 以前是三个互不相干的认证。

    运营在检测报告那栏填了其中一种写法,系统认定另外两种仍未认证。
    """
    for spelling in ("UPF50+", "UPF 50+", "UPF-50+", "FPU 50+", "UPF50"):
        assert canonical_claim_id(spelling) == "UPF50", spelling
    # ID 本身也要认得 —— 库里存的就是 ID
    assert canonical_claim_id("UPF50") == "UPF50"
    assert canonical_claim_id("完全没听过的认证") is None


def test_certifying_one_spelling_releases_the_whole_group():
    forbidden = derive_forbidden_claims(certified=("UPF-50+",))
    for variant in CERTIFIED_CLAIM_VARIANTS["UPF50"]:
        assert variant not in forbidden, f"{variant} 应当随整组一起放行"
    # 别的组不受影响
    assert "抗菌" in forbidden


def test_an_unrecognised_certificate_does_not_open_anything():
    """拼错的认证项应当表现为"仍被禁止",不是"被放开了"。"""
    assert certified_claim_ids(("UPF-5O+",)) == ()
    assert "UPF50+" in derive_forbidden_claims(certified=("UPF-5O+",))


# ---------------------------------------------------------------- 17 计数口径


def test_count_by_character_actually_changes_the_count():
    """评审第 17 条:这个配置项以前是死的,配成 False 行为完全不变。"""
    chinese = {"title": "藏青连体泳衣", "bullet_points": ["a", "b", "c"]}
    by_char = CopyRules(title_max=10, title_min=1, count_by_character=True)
    by_byte = CopyRules(title_max=10, title_min=1, count_by_character=False)
    assert by_char.measure("藏青连体泳衣") == 6
    assert by_byte.measure("藏青连体泳衣") == 18
    assert CopyViolationCode.TOO_LONG not in _codes(validate_structure(chinese, by_char))
    assert CopyViolationCode.TOO_LONG in _codes(validate_structure(chinese, by_byte))


# ---------------------------------------------------------------- 结构约束


def test_the_new_term_module_stays_dependency_free():
    """`claim_terms` 被 `copy_rules` / `content_plan` 依赖,必须和它们同级别地干净。"""
    tree = _tree(APP_DIR / "listings" / "claim_terms.py")
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            assert not name.startswith(("sqlalchemy", "pydantic", "fastapi", "app.")), (
                f"claim_terms 依赖了 {name}"
            )


def test_normalization_is_idempotent():
    """归一化跑两遍必须和跑一遍一样 —— 否则比对结果取决于调用次数。"""
    for text in ("ＵＰＦ-50+", "One\u200bPiece", "  Navy_Blue  ", "抗　菌"):
        once = claim_terms.normalize(text)
        assert claim_terms.normalize(once) == once, text
