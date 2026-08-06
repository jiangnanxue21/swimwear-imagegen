"""§4.5 过期矩阵:6 行 × 3 列 = 18 格,逐格验证(§5.3 的判定方法本身)。

任务书 §5.3「过期草稿漏提示 0 次」的判定方法写明是「按 §4.5 矩阵逐行验证」。
在本文件出现之前,矩阵的三列散在三处实现(草稿列 `stale.diff_components`、
图片集列 `image_set_rules.needs_downgrade` + 版本化不可变、文案列
`copy_attrs_stale` + `approve_copy` 的重校验),没有任何一处把 18 个格子
当成封闭集合核对 —— 少实现一格不会有测试变红。

本文件分四段:

    闭合性   `stale_matrix.MATRIX` 恰好 18 格,取值与任务书表格逐字一致
    逐行     每一行,三列的声明行为都被行使一遍 —— 包括「不过期」的
             反向格:不是靠没写代码,而是靠判定输入里根本没有那个维度
    静态纪律 落库/批准这类离不开数据库的格子,用 AST 钉住其纪律
             (APPROVED 只能由 approve 赋予、导出前必须先刷新过期状态)
    §4.5.1   每条过期提示都说清对象、字段、动作;仅「已过期」三个字不算数
    性质     `diff_components` 自身的性质(空变化、手填字段、多变更、序列化形状)
             —— 不对应矩阵里的任何一格,但它们是上面每一行赖以成立的地基
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
from itertools import product

from app.core.enums import ImageSetStatus
from app.listings.copy_rules import CopyRules, CopyViolationCode, blocking, validate_wording
from app.listings.image_set_rules import ItemView, SetView, needs_downgrade
from app.workbench import stale_matrix as m
from app.workbench.stale import copy_attrs_stale, diff_components, serialize
from tests.pure._helpers import BACKEND_ROOT

APP_DIR = BACKEND_ROOT / "app"
WORKBENCH_SERVICE_SRC = (APP_DIR / "workbench" / "service.py").read_text(encoding="utf-8")
IMAGE_SET_SERVICE_SRC = (APP_DIR / "listings" / "image_set_service.py").read_text(encoding="utf-8")
COPY_SERVICE_SRC = (APP_DIR / "listings" / "copy_service.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 共用工具


def _base() -> dict:
    """一份「什么都没变」的草稿组件快照。逐行用例在它上面只改一处。"""
    return {
        "attrs": ["v-garment_type", "v-primary_color"],
        "attr_fields": {"v-garment_type": "garment_type", "v-primary_color": "primary_color"},
        "image_set": {"id": "set-1", "version": 2},
        "copies": {"en": {"id": "copy-1", "version": 3}},
        "manual": {"brand": "Marine", "price": "39.90"},
        "spec_version": "1.0.0",
        "mapping_version": "1",
    }


def _func(src: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"源码里找不到函数 {name}")


def _is_enum_value(node: ast.AST, enum_name: str, member: str) -> bool:
    """`node` 是否形如 `EnumName.MEMBER.value`(或 `EnumName.MEMBER`)。"""
    if isinstance(node, ast.Attribute) and node.attr == "value":
        node = node.value
    return (
        isinstance(node, ast.Attribute)
        and node.attr == member
        and isinstance(node.value, ast.Name)
        and node.value.id == enum_name
    )


def _funcs_assigning(src: str, enum_name: str, member: str) -> set[str]:
    """整份源码里,哪些函数体内出现了把 `EnumName.MEMBER[.value]` 赋给谁的语句。"""
    tree = ast.parse(src)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign) and _is_enum_value(inner.value, enum_name, member):
                hits.add(node.name)
    return hits


# ---------------------------------------------------------------- 闭合性


def test_matrix_has_exactly_one_cell_per_row_and_column():
    """格数**由行列自乘推出,不写字面量。**

    这里原来写的是 `len(pairs) == 24`。A45-batch14-20 按 §8.1 补了两行之后
    它变红了 —— 而口径其实一点没松:要守的不变式从来是"每个 (行, 列)
    恰好一格",24 只是当时行列数的一个副产品。手写的那个数字每加一行
    就要改一次,而改它的人**唯一能做的动作就是把它改成新的数字** ——
    于是这条断言实际上什么都没守住,只是在提醒有人动过矩阵。

    与 `docs/STATUS.md` 第五节那条规矩同源(数字不写在文档里),
    也是"点名做法"的同一类:断言里凡是出现"当时恰好是多少"的常量,
    都要换成推导式。
    """
    pairs = [(c.source, c.target) for c in m.MATRIX]
    assert len(pairs) == len(m.ChangeSource) * len(m.Target)
    assert sorted(pairs) == sorted(product(m.ChangeSource, m.Target))
    assert m.is_exhaustive()


def test_matrix_cells_match_the_task_book_table_verbatim():
    """逐格对照任务书 §4.5 + PRD v3.1 §8.1 的表格。这里是任务书的第二份
    拷贝 —— `stale_matrix.py` 改了任何一格,本测试必须跟着改,反之亦然。

    FACTS 那一列来自 §8.1「目标新增 FACTS 列」。加它之前这张自称封闭的矩阵
    回答不了「商品事实什么时候过期」,而那正是 D1 的落点。"""
    S, T, E = m.ChangeSource, m.Target, m.Effect
    expected = {
        (S.CONFIRMED_ATTRIBUTE_MODIFIED, T.FACTS): E.NEW_VERSION,
        (S.CONFIRMED_ATTRIBUTE_MODIFIED, T.IMAGE_SET): E.NOT_AFFECTED,
        (S.CONFIRMED_ATTRIBUTE_MODIFIED, T.COPY): E.STALE,
        (S.CONFIRMED_ATTRIBUTE_MODIFIED, T.DRAFT): E.STALE,
        (S.ASSET_REPLACED_OR_QUARANTINED, T.FACTS): E.STALE,
        (S.ASSET_REPLACED_OR_QUARANTINED, T.IMAGE_SET): E.STALE,
        (S.ASSET_REPLACED_OR_QUARANTINED, T.COPY): E.NOT_AFFECTED,
        (S.ASSET_REPLACED_OR_QUARANTINED, T.DRAFT): E.STALE,
        (S.APPROVED_IMAGE_SET_REARRANGED, T.FACTS): E.NOT_AFFECTED,
        (S.APPROVED_IMAGE_SET_REARRANGED, T.IMAGE_SET): E.RESET_TO_PENDING,
        (S.APPROVED_IMAGE_SET_REARRANGED, T.COPY): E.NOT_AFFECTED,
        (S.APPROVED_IMAGE_SET_REARRANGED, T.DRAFT): E.STALE,
        (S.APPROVED_COPY_EDITED, T.FACTS): E.NOT_AFFECTED,
        (S.APPROVED_COPY_EDITED, T.IMAGE_SET): E.NOT_AFFECTED,
        (S.APPROVED_COPY_EDITED, T.COPY): E.RESET_TO_PENDING,
        (S.APPROVED_COPY_EDITED, T.DRAFT): E.STALE,
        (S.EXPORT_SPEC_VERSION_CHANGED, T.FACTS): E.NOT_AFFECTED,
        (S.EXPORT_SPEC_VERSION_CHANGED, T.IMAGE_SET): E.NOT_AFFECTED,
        (S.EXPORT_SPEC_VERSION_CHANGED, T.COPY): E.NOT_AFFECTED,
        (S.EXPORT_SPEC_VERSION_CHANGED, T.DRAFT): E.ALL_DRAFTS_STALE,
        (S.COPY_RULES_CHANGED, T.FACTS): E.NOT_AFFECTED,
        (S.COPY_RULES_CHANGED, T.IMAGE_SET): E.NOT_AFFECTED,
        (S.COPY_RULES_CHANGED, T.COPY): E.REVALIDATE,
        (S.COPY_RULES_CHANGED, T.DRAFT): E.NOT_AFFECTED,
        # ---- A45-batch14-20 按 §8.1 补的两行(阶段 4 落地了它们的机制)----
        (S.GENERATION_PLAN_CHANGED, T.FACTS): E.NOT_AFFECTED,
        (S.GENERATION_PLAN_CHANGED, T.IMAGE_SET): E.STALE,
        (S.GENERATION_PLAN_CHANGED, T.COPY): E.NOT_AFFECTED,
        (S.GENERATION_PLAN_CHANGED, T.DRAFT): E.STALE,
        (S.APPROVED_IMAGE_SET_REPLACED, T.FACTS): E.NOT_AFFECTED,
        # **NEW_VERSION 不是 STALE**:新一版已经批准了、旧版自动退位,
        # 与第 3 行(已批准集被改动 → 重置为待批准)是两件事
        (S.APPROVED_IMAGE_SET_REPLACED, T.IMAGE_SET): E.NEW_VERSION,
        (S.APPROVED_IMAGE_SET_REPLACED, T.COPY): E.NOT_AFFECTED,
        (S.APPROVED_IMAGE_SET_REPLACED, T.DRAFT): E.STALE,
    }
    assert {(c.source, c.target): c.effect for c in m.MATRIX} == expected
    # 触发时机一列也不能少
    assert set(m.TRIGGERS) == set(m.ChangeSource)
    assert all(m.TRIGGERS[s] for s in m.ChangeSource)


def test_every_effective_cell_names_a_mechanism_that_exists_in_code():
    """有效应的格子必须点名机制,且点到的函数真的在点到的文件里。
    「不过期」的格子机制留空,但 note 必须说清为什么波及不到。"""
    for c in m.MATRIX:
        if c.effect is m.Effect.NOT_AFFECTED:
            assert not c.mechanism, f"{c.source}/{c.target}: 不过期的格子不该有机制"
            assert c.note, f"{c.source}/{c.target}: 不过期也要说清为什么"
            continue
        assert c.mechanism, f"{c.source}/{c.target}: 有效应却没点名机制"
        dotted = c.mechanism.split(":", 1)[0]
        parts = dotted.split(".")
        module_file = BACKEND_ROOT.joinpath(*parts[:-1]).with_suffix(".py")
        assert module_file.exists(), f"{c.mechanism}: 模块文件不存在 {module_file}"
        assert f"def {parts[-1]}(" in module_file.read_text(encoding="utf-8"), (
            f"{c.mechanism}: {module_file.name} 里没有函数 {parts[-1]}"
        )


# ---------------------------------------------------------------- 第 1 行:已确认属性被修改


def test_r1_copy_goes_stale_only_when_approved_and_attr_snapshot_drifts():
    # 已批准 + 属性版本集合漂移 → 过期
    assert copy_attrs_stale("APPROVED", ["a", "b"], ["a", "c"]) is True
    # 集合一致(顺序无关)→ 不过期
    assert copy_attrs_stale("APPROVED", ["b", "a"], ["a", "b"]) is False
    # 未批准的文案谈不上过期 —— 它本来就还没被采信
    assert copy_attrs_stale("VALIDATED", ["a"], ["b"]) is False
    assert copy_attrs_stale("DRAFT", ["a"], ["b"]) is False
    # 内容计划丢了 → 无法证明仍然成立,按过期算(默认偏保守)
    assert copy_attrs_stale("APPROVED", None, ["a"], plan_missing=True) is True


def test_r1_draft_expires_and_names_the_modified_field():
    current = _base()
    current["attrs"] = ["v-garment_type", "v-primary_color2"]
    current["attr_fields"]["v-primary_color2"] = "primary_color"
    changes = diff_components(_base(), current)
    assert [c.component for c in changes] == ["attributes"]
    assert "primary_color" in changes[0].fields
    assert changes[0].action


#: 属性层的字段名。判定输入里出现任何一个,第 1 行 IMAGE_SET 那一格的
#: 「不过期」就不再成立 —— 而"不过期"这一格没有机制可点名,它的全部
#: 防线就是下面那条断言。
ATTRIBUTE_DIMENSIONS = frozenset(
    {
        "garment_type", "primary_color", "secondary_color", "pattern_type",
        "strap_type", "neckline_type", "back_style", "coverage_level",
        "attribute_values", "confirmed_facts", "facts",
    }
)


def test_r1_image_set_judgement_inputs_contain_no_attribute_dimension():
    """图片集列写「不过期」的做法:属性根本不在判定输入里。

    ## 这条断言换过一次形状(A45-batch14-20)

    原来它把 `SetView` / `ItemView` 的字段集**逐字钉死**。§6.5 给 `ItemView`
    加了 `shared_opt_in` 与 `angle` 之后它变红了 —— 而那两个字段一个属性
    维度都没带进来,口径根本没松。

    钉全集的问题在于它**分不清两种改动**:加一个排版字段和加一个属性字段
    都让它变红,而它红了之后最省事的做法是把新字段名加进去 ——
    于是真正该拦的那一次也会被同样地"修好"。这是"点名做法"的第六次
    (前五次见 `docs/DECISIONS.md` §3.31 / §3.32 / §3.33)。

    改成钉**不变式本身**:判定输入里不许出现属性维度。谁要往里加属性,
    必须有意识地回到矩阵改第 1 行的这一格。
    """
    fields = {f.name for f in dataclasses.fields(SetView)} | {
        f.name for f in dataclasses.fields(ItemView)
    }
    leaked = fields & ATTRIBUTE_DIMENSIONS
    assert not leaked, (
        f"图片集判定的输入里出现了属性维度 {sorted(leaked)} —— "
        "第 1 行 IMAGE_SET 那一格的「不过期」不再成立,先去改矩阵"
    )
    # 反向:判定必须真的还在读素材状态,否则第 2 行那一格会静静失效
    assert "asset_status" in fields


# ---------------------------------------------------------------- 第 2 行:素材被替换或隔离


def _one_item_set(status: str, asset_status: str, enabled: bool = True) -> SetView:
    return SetView(
        image_set_id="s1",
        spu="SPU-1",
        version=3,
        status=status,
        items=(
            ItemView(
                media_asset_id="a1",
                role="FRONT",
                sort_order=0,
                is_primary=True,
                enabled=enabled,
                asset_status=asset_status,
            ),
        ),
    )


def test_r2_quarantined_asset_downgrades_the_approved_set():
    approved = ImageSetStatus.APPROVED.value
    # 已批准集引用了被隔离的启用素材 → 降级(任务书「过期」在图片集上的落点)
    assert needs_downgrade(_one_item_set(approved, "QUARANTINED")) is True
    # 那一项已停用 → 不降级
    assert needs_downgrade(_one_item_set(approved, "QUARANTINED", enabled=False)) is False
    # 全部素材可用 → 不降级
    assert needs_downgrade(_one_item_set(approved, "READY")) is False
    # 非已批准集不参与降级 —— 降级只对「本会被发布」的那一版有意义
    assert needs_downgrade(
        _one_item_set(ImageSetStatus.PENDING_REVIEW.value, "QUARANTINED")
    ) is False


def test_r2_copy_predicate_cannot_even_see_asset_state():
    """文案列写「不过期」:copy_attrs_stale 的参数表里没有素材维度,
    也没有图片集、没有导出模板版本 —— 第 2/3/5 行的文案列同源于此。"""
    params = set(inspect.signature(copy_attrs_stale).parameters)
    assert params == {"status", "plan_attr_ids", "confirmed_ids", "plan_missing"}


def test_r2_draft_expires_when_no_approved_set_remains():
    current = _base()
    current["image_set"] = None
    changes = diff_components(_base(), current)
    assert [c.component for c in changes] == ["image_set"]
    assert "不再是批准状态" in changes[0].message
    assert changes[0].action


def test_r2_refresh_draft_marks_stale_even_when_upstream_approval_is_revoked():
    """降级发生后当前组件可能组装不出来(current is None)。refresh_draft
    的两条路径都必须把草稿置为 STALE —— 只写指纹路径的话,「上游批准被
    撤销」这种更严重的情况反而不会被标记。"""
    func = _func(WORKBENCH_SERVICE_SRC, "refresh_draft")
    stale_assigns = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Assign) and _is_enum_value(node.value, "DraftStatus", "STALE")
    ]
    assert len(stale_assigns) >= 2, "refresh_draft 必须在撤销与指纹两条路径上都置 STALE"


# ---------------------------------------------------------------- 第 3 行:已批准图片集被重新编排


def test_r3_rearranging_produces_a_new_version_that_starts_unapproved():
    """「重置为待批准」由版本化不可变实现:derive() 走 create_set 生成新集,
    新集初始状态是 DRAFT;APPROVED 这个状态在整个 image_set_service 里
    只能由 approve() 赋予。"""
    # derive 的唯一产出路径是 create_set
    derive = _func(IMAGE_SET_SERVICE_SRC, "derive")
    called = {
        node.func.id
        for node in ast.walk(derive)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "create_set" in called
    # create_set 里 ListingImageSet(...) 的初始状态是 DRAFT
    create_set = _func(IMAGE_SET_SERVICE_SRC, "create_set")
    ctor_status = [
        kw.value
        for node in ast.walk(create_set)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ListingImageSet"
        for kw in node.keywords
        if kw.arg == "status"
    ]
    assert ctor_status and all(
        _is_enum_value(v, "ImageSetStatus", "DRAFT") for v in ctor_status
    )
    # APPROVED 只在 approve() 里被赋值(rollback_to 也委托 approve)
    assert _funcs_assigning(IMAGE_SET_SERVICE_SRC, "ImageSetStatus", "APPROVED") == {"approve"}


def test_r3_draft_expires_when_the_approved_version_changes():
    current = _base()
    current["image_set"] = {"id": "set-2", "version": 3}
    changes = diff_components(_base(), current)
    assert [c.component for c in changes] == ["image_set"]
    assert "v2" in (changes[0].old_value or "") and "v3" in (changes[0].new_value or "")


# ---------------------------------------------------------------- 第 4 行:已批准文案被编辑


def test_r4_editing_lands_as_a_new_version_that_must_be_reapproved():
    """save_copy 落的新版本状态只会是 VALIDATED / REJECTED;
    APPROVED 在整个 copy_service 里只能由 approve_copy 赋予。"""
    save_copy_src = ast.get_source_segment(
        COPY_SERVICE_SRC, _func(COPY_SERVICE_SRC, "save_copy")
    )
    assert save_copy_src is not None and "APPROVED" not in save_copy_src
    assert _funcs_assigning(COPY_SERVICE_SRC, "CopyStatus", "APPROVED") == {"approve_copy"}


def test_r4_draft_expires_per_locale_when_the_approved_copy_changes():
    current = _base()
    current["copies"] = {"en": {"id": "copy-2", "version": 4}}
    changes = diff_components(_base(), current)
    assert [c.component for c in changes] == ["copy"]
    assert changes[0].fields == ("en",)
    assert changes[0].action


# ---------------------------------------------------------------- 第 5 行:导出模板版本变更


def test_r5_template_version_change_expires_the_draft_and_only_the_draft():
    for key, component in (("spec_version", "spec"), ("mapping_version", "mapping")):
        current = _base()
        current[key] = "9.9.9"
        changes = diff_components(_base(), current)
        # 只有草稿列的组件被点名;图片集与文案不在变化清单里
        assert [c.component for c in changes] == [component]
        assert "重新生成" in changes[0].action
        # 旧值/新值必须是真实的版本号本身,不是「有值就行」——
        # 运营看的是这两个数字,占位符会让提示变成废话。
        assert changes[0].old_value == _base()[key]
        assert changes[0].new_value == "9.9.9"


# ---------------------------------------------------------------- 第 6 行:文案规则/禁词库变更


def test_r6_a_newly_banned_word_is_caught_by_revalidation():
    """「需重新校验」的意义:同一份文案,旧规则通过,新禁词库下被拦。
    这就是 approve_copy 在 spec_version 不一致时强制 revalidate 的原因。"""
    copy = {
        "title": "Chlorine resistant one-piece swimsuit",
        "description": "A flattering one-piece.",
        "bullet_points": ["Adjustable straps", "Removable padding", "Quick dry"],
    }
    rules_v1 = CopyRules(spec_version="1")
    rules_v2 = CopyRules(spec_version="2", banned_words=("chlorine resistant",))
    codes_v1 = [v.code for v in validate_wording(copy, rules_v1)]
    v2_violations = validate_wording(copy, rules_v2)
    codes_v2 = [v.code for v in v2_violations]
    assert CopyViolationCode.BANNED_WORD not in codes_v1
    assert CopyViolationCode.BANNED_WORD in codes_v2
    assert blocking(v2_violations), "禁词必须是硬失败,不然重校验只是走个过场"


def test_r6_approve_copy_revalidates_against_current_rules_before_approving():
    """AST 静态纪律:approve_copy 里,spec_version 的比对与 revalidate 调用
    必须发生在 `row.status = APPROVED` 赋值之前。顺序反了 = 旧规则批准。"""
    func = _func(COPY_SERVICE_SRC, "approve_copy")
    spec_compares = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Attribute)
        and node.left.attr == "spec_version"
    ]
    revalidate_calls = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "revalidate"
    ]
    approve_assigns = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Assign) and _is_enum_value(node.value, "CopyStatus", "APPROVED")
    ]
    assert spec_compares, "approve_copy 必须比对文案落库时与当前的规则版本"
    assert revalidate_calls, "版本不一致必须重校验,而不是直接批准"
    assert approve_assigns, "approve_copy 里找不到批准赋值 —— 测试与实现脱节"
    assert max(c.lineno for c in revalidate_calls) < min(a.lineno for a in approve_assigns)


def test_r6_draft_does_not_expire_on_rule_version_drift():
    """草稿列写「不过期」:组件快照里根本没有文案规则版本这一项。
    规则变化经由文案重新校验/重新批准(新版本)间接传导,那时命中第 4 行。"""
    stored = _base()
    current = {**_base(), "copy_rules_version": "2"}  # 未知键不构成草稿过期输入
    assert diff_components(stored, current) == []


# ---------------------------------------------------------------- §4.5.1 行为要求


def _all_matrix_draft_changes() -> list:
    """把矩阵里草稿列的每个有效应格子各行使一次,收齐产生的变化。"""
    cases = []
    r1 = _base()
    r1["attrs"] = ["v-garment_type"]
    r2 = _base()
    r2["image_set"] = None
    r3 = _base()
    r3["image_set"] = {"id": "set-2", "version": 3}
    r4 = _base()
    r4["copies"] = {"en": {"id": "copy-2", "version": 4}}
    r5 = _base()
    r5["spec_version"] = "2.0.0"
    for current in (r1, r2, r3, r4, r5):
        cases.extend(diff_components(_base(), current))
    return cases


def test_451_every_change_names_the_object_and_a_concrete_action():
    """§4.5.1:过期提示须说明哪个上游对象变了、变了哪些字段、要做什么。
    仅显示「已过期」三个字视为未完成。"""
    changes = _all_matrix_draft_changes()
    assert len(changes) == 5, "草稿列 5 个有效应格子,每格恰好一条变化"
    for c in changes:
        assert c.component, "必须点名上游对象"
        assert c.message and c.message != "已过期", "话术不能只有「已过期」三个字"
        assert c.action, "必须告诉运营下一步做什么"
    # 五格话术互不相同 —— 「上游变了」不是一句万金油
    assert len({c.message for c in changes}) == 5


def test_451_export_gate_refreshes_staleness_before_anything_else():
    """AST 静态纪律:export_gate 必须先 refresh_draft(过期即拒绝),
    再看状态是否可导出。顺序反了的话,一份看似 VALIDATED 的旧草稿
    会先通过状态闸 —— 「过期草稿一律禁止导出」就漏了。"""
    func = _func(WORKBENCH_SERVICE_SRC, "export_gate")
    refresh_calls = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "refresh_draft"
    ]
    publishable_refs = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Attribute) and node.attr == "PUBLISHABLE_STATUSES"
    ]
    stale_raises = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.If)
        and any(
            isinstance(n, ast.Name) and n.id == "is_stale" for n in ast.walk(node.test)
        )
        and any(isinstance(n, ast.Raise) for n in ast.walk(node))
    ]
    assert refresh_calls, "export_gate 必须调用 refresh_draft"
    assert publishable_refs, "export_gate 必须检查可导出状态"
    assert stale_raises, "过期必须以异常拒绝,而不是继续往下走"
    assert min(c.lineno for c in refresh_calls) < min(p.lineno for p in publishable_refs)


# ------------------------------------------- diff_components 自身性质(非矩阵行)
#
# 以下四条不对应 §4.5 矩阵里的任何一格,它们钉的是 `diff_components` 这个
# 函数本身的性质:没变化时闭嘴、手填字段也算上游、多个变更不能只报最上游的、
# 序列化形状对得上前端。上面每一行的「草稿列」都建立在这四条之上,所以放在
# 同一个文件里 —— 原先它们独立成 test_workbench_stale.py,而那个文件里另外
# 五条与 R1–R5 的草稿列重复(矩阵版本断言更强),已随文件一并删除。


def test_identical_components_report_nothing():
    assert diff_components(_base(), _base()) == []


def test_manual_field_drift_names_the_keys():
    """手填字段没有上游可言,但它变了草稿一样得重新生成。"""
    current = _base()
    current["manual"] = {"brand": "Marine", "price": "45.00"}
    changes = diff_components(_base(), current)
    assert [c.component for c in changes] == ["manual"]
    assert changes[0].fields == ("price",)
    assert changes[0].action


def test_multiple_upstream_changes_are_all_reported():
    """一次刷新里属性和文案都变了 —— 两条都要报,不能只报最上游的。"""
    current = _base()
    current["attrs"] = ["v-garment_type"]
    current["copies"] = {"en": {"id": "copy-9", "version": 4}}
    changes = diff_components(_base(), current)
    assert {c.component for c in changes} == {"attributes", "copy"}


def test_serialize_is_json_shaped():
    current = _base()
    current["spec_version"] = "2"
    payload = serialize(diff_components(_base(), current))
    assert payload and set(payload[0]) == {
        "component", "message", "fields", "old_value", "new_value", "action",
    }
