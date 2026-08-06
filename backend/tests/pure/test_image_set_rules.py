"""M5:图片集规则(零三方依赖)。

图片编排的错误在平台那边的表现通常是「审核不通过」加一句含混的理由,
所以它必须能在本地被穷举测试出来。
"""
from __future__ import annotations

import ast

from app.core.enums import ImageSetStatus, RoleSource
from app.listings.contracts import ImageSetSnapshot
from app.listings.image_set_rules import (
    MODEL_ROLE_PRIMARY_MIN_CONFIDENCE,
    ImageSetViolation,
    ItemView,
    SetView,
    can_be_primary,
    needs_downgrade,
    next_version,
    ordered_items,
    primary_for,
    resolve_set,
    validate_set,
)
from tests.pure._helpers import BACKEND_ROOT

APP_DIR = BACKEND_ROOT / "app"


def _item(mid="m1", **kw):
    base = dict(media_asset_id=mid, role="PRODUCT_FRONT", sort_order=0)
    base.update(kw)
    return ItemView(**base)


def _set(items=(), **kw):
    base = dict(
        image_set_id="s1", spu="SW-1", version=1,
        status=ImageSetStatus.APPROVED.value, items=tuple(items),
    )
    base.update(kw)
    return SetView(**base)


# ---------------------------------------------------------------- 解析


def test_resolution_prefers_the_most_specific_set():
    """(渠道,站点) 专用 → (渠道,通用) → 全局默认。"""
    generic = _set(channel=None, site=None, items=(_item(is_primary=True),))
    per_channel = _set(image_set_id="s2", channel="SHEIN", site=None,
                       items=(_item(is_primary=True),))
    per_site = _set(image_set_id="s3", channel="SHEIN", site="BR",
                    items=(_item(is_primary=True),))
    pool = [generic, per_channel, per_site]

    assert resolve_set(pool, channel="SHEIN", site="BR").image_set_id == "s3"
    assert resolve_set(pool, channel="SHEIN", site="US").image_set_id == "s2"
    assert resolve_set(pool, channel="OTHER", site="US").image_set_id == "s1"


def test_resolution_takes_the_highest_approved_version():
    v1 = _set(image_set_id="a", version=1)
    v3 = _set(image_set_id="b", version=3)
    v2 = _set(image_set_id="c", version=2)
    assert resolve_set([v1, v3, v2], channel=None, site=None).version == 3


def test_only_approved_sets_are_resolvable():
    """发布只读 APPROVED(硬规矩 5)。"""
    for status in (
        ImageSetStatus.DRAFT.value,
        ImageSetStatus.PENDING_REVIEW.value,
        ImageSetStatus.ARCHIVED.value,
    ):
        assert resolve_set([_set(status=status)], channel=None, site=None) is None


def test_a_newer_draft_does_not_shadow_an_approved_version():
    """草稿版本号更高时,发布仍然取已批准的那一版。

    否则"改了但还没批准"会立刻影响线上 —— 而审批这一步就没有意义了。
    """
    approved = _set(image_set_id="ok", version=2)
    draft = _set(image_set_id="draft", version=5, status=ImageSetStatus.DRAFT.value)
    assert resolve_set([approved, draft], channel=None, site=None).image_set_id == "ok"


# ---------------------------------------------------------------- 主图


def test_missing_primary_is_reported_per_variant():
    problems = validate_set(_set(items=(_item(variant_id="RED"),)))
    assert [p.code for p in problems] == [ImageSetViolation.MISSING_PRIMARY_FOR_VARIANT]
    assert problems[0].variant_id == "RED"


def test_multiple_primaries_are_reported():
    problems = validate_set(_set(items=(
        _item("m1", variant_id="RED", is_primary=True),
        _item("m2", variant_id="RED", is_primary=True, sort_order=1),
    )))
    assert ImageSetViolation.MULTIPLE_PRIMARY_FOR_VARIANT in [p.code for p in problems]


def test_model_guessed_role_cannot_be_primary():
    """角色是模型猜的且把握不足时不能当主图(§7.3)。

    主图放错是消费者第一眼就看到的错误,而平铺图和模特图恰恰是
    角色识别最容易混的一对。
    """
    weak = _item(is_primary=True, asset_role_source=RoleSource.MODEL.value,
                 asset_role_confidence=MODEL_ROLE_PRIMARY_MIN_CONFIDENCE - 0.01)
    assert not can_be_primary(weak)
    codes = [p.code for p in validate_set(_set(items=(weak,)))]
    assert ImageSetViolation.UNTRUSTED_PRIMARY_ROLE in codes

    strong = _item(is_primary=True, asset_role_source=RoleSource.MODEL.value,
                   asset_role_confidence=MODEL_ROLE_PRIMARY_MIN_CONFIDENCE)
    assert can_be_primary(strong)


def test_human_assigned_role_needs_no_confidence():
    """人不会给自己的判断打分。"""
    item = _item(is_primary=True, asset_role_source=RoleSource.HUMAN.value,
                 asset_role_confidence=None)
    assert can_be_primary(item)


def test_primary_does_not_fall_back_to_the_spu_level():
    """**这条断言也翻过来了(§6.5)。** 理由同上一条。

    `variant_id=None` 仍然取通用主图 —— 那是单色 SPU 与 SPU 级展示位的
    用法,§6.5 管的是"颜色主图",不是"所有主图"。
    """
    s = _set(items=(
        _item("generic", is_primary=True, variant_id=None),
        _item("red", variant_id="RED", sort_order=1),
    ))
    assert primary_for(s, "RED") is None
    assert primary_for(s, None).media_asset_id == "generic"


# ---------------------------------------------------------------- 素材可用性


def test_quarantined_assets_are_rejected():
    """只有 READY 的素材能入集。"""
    problems = validate_set(_set(items=(
        _item(is_primary=True, asset_status="QUARANTINED"),
    )))
    codes = [p.code for p in problems]
    assert ImageSetViolation.UNUSABLE_ASSET in codes
    assert not can_be_primary(_item(is_primary=True, asset_status="QUARANTINED"))


def test_approved_set_with_a_quarantined_asset_must_be_downgraded():
    """素材被隔离时,引用它的已批准集自动降级(§7.3)。

    不降级的话,一张已经被判定为不合规的图会继续被发布 ——
    而隔离动作看起来是成功的。
    """
    bad = _set(items=(_item(is_primary=True, asset_status="QUARANTINED"),))
    assert needs_downgrade(bad)
    # 已经不是 APPROVED 的集不需要再降级
    assert not needs_downgrade(_set(
        status=ImageSetStatus.PENDING_REVIEW.value,
        items=(_item(asset_status="QUARANTINED"),),
    ))
    assert not needs_downgrade(_set(items=(_item(is_primary=True),)))


def test_disabled_items_do_not_trigger_violations():
    """停用的项不参与判定 —— 那正是"先关掉再说"的用法。"""
    s = _set(items=(
        _item("ok", is_primary=True),
        _item("bad", enabled=False, asset_status="QUARANTINED", sort_order=1),
    ))
    assert validate_set(s) == []
    assert not needs_downgrade(s)


def test_empty_set_is_reported_once():
    problems = validate_set(_set(items=()))
    assert [p.code for p in problems] == [ImageSetViolation.EMPTY_SET]

    all_disabled = validate_set(_set(items=(_item(enabled=False),)))
    assert [p.code for p in all_disabled] == [ImageSetViolation.EMPTY_SET]


# ---------------------------------------------------------------- 变体覆盖


def test_variant_without_any_image_is_reported():
    s = _set(items=(_item("red", variant_id="RED", is_primary=True),))
    problems = validate_set(s, variant_ids=("RED", "BLUE"))
    assert [p.variant_id for p in problems if
            p.code is ImageSetViolation.MISSING_IMAGE_FOR_VARIANT] == ["BLUE"]


def test_spu_level_images_no_longer_cover_a_variant():
    """**这条断言翻过来了(A45-batch14-20 / PRD §6.5)。**

    原来写的是「有 SPU 通用图时,变体不必各自有图」。那是 BLOCK-02 悬着
    的那几版里的权宜口径 —— 它存在的唯一理由是 `variant_id` 没有写入方,
    改成硬阻断会让每个多色 SPU 立刻无法批准。

    §6.5 把那个业务决定定死了:「不得回退使用其他颜色的图片,缺图就是
    缺图(BLOCKED)」。通用图只能进附图位,不算覆盖。留着旧口径的具体
    后果是红色 SKU 挂着一张黑色主图上架。
    """
    s = _set(items=(_item("generic", is_primary=True, variant_id=None),))
    missing = [
        p for p in validate_set(s, variant_ids=("RED", "BLUE"))
        if p.code is ImageSetViolation.MISSING_IMAGE_FOR_VARIANT
    ]
    assert {p.variant_id for p in missing} == {"RED", "BLUE"}


def test_a_single_colour_spu_is_untouched_by_that_change():
    """不传 `variant_ids` = 不做颜色级检查。单色 SPU 的行为一个字没变。"""
    s = _set(items=(_item("generic", is_primary=True, variant_id=None),))
    assert validate_set(s) == []


# ---------------------------------------------------------------- 排序与版本


def test_primary_is_always_first():
    """平台按传入顺序决定展示位,所以"主图第一"是数据层的事。"""
    s = _set(items=(
        _item("a", sort_order=0),
        _item("b", sort_order=1, is_primary=True),
        _item("c", sort_order=2),
    ))
    assert [i.media_asset_id for i in ordered_items(s)] == ["b", "a", "c"]


def test_ordering_is_deterministic_on_ties():
    s = _set(items=(_item("z", sort_order=5), _item("a", sort_order=5)))
    assert [i.media_asset_id for i in ordered_items(s)] == ["a", "z"]


def test_next_version_increments():
    assert next_version([]) == 1
    assert next_version([_set(version=1), _set(version=4), _set(version=2)]) == 5


# ---------------------------------------------------------------- 结构约束


def test_rules_module_is_dependency_free():
    """规则是纯函数 —— 编排错误必须能在不带数据库的测试里穷举。"""
    tree = ast.parse((APP_DIR / "listings" / "image_set_rules.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for n in names:
            assert not n.startswith("sqlalchemy"), "图片集规则依赖了数据库"


def test_primary_uniqueness_index_handles_null_variants():
    """主图唯一索引必须套 COALESCE。

    PostgreSQL 把 NULL 当成互不相等 —— 不套的话可以插进任意多张
    `variant_id` 为 NULL 的主图,而这个错误要到平台那边显示错主图时
    才会被发现。
    """
    migration = (
        BACKEND_ROOT / "migrations" / "versions" / "0013_listing_image_sets.py"
    ).read_text(encoding="utf-8")
    assert "COALESCE(variant_id, '')" in migration
    assert "COALESCE(channel, '')" in migration
    model = (APP_DIR / "models" / "listing_image.py").read_text(encoding="utf-8")
    assert "COALESCE(variant_id, '')" in model


def test_quarantine_downgrades_approved_sets():
    """隔离素材必须联动降级已批准的图片集(§7.3)。

    M1 文档里把这条记成「等 M5 图片集落地后接上」。接上之前,
    隔离一张图在界面上是成功的,而它仍然会被发布出去。
    """
    source = (APP_DIR / "media" / "service.py").read_text(encoding="utf-8")
    assert "downgrade_sets_using" in source, "隔离没有联动图片集"
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "quarantine"
    )
    assert "downgrade_sets_using" in ast.unparse(fn)


def test_approved_sets_cannot_be_edited_in_place():
    """改已批准的集只能走 derive。

    原地改的话,「平台驳回的是哪一版图」没法回答 —— 而驳回回流
    第一件要做的事正是定位到具体那一版的具体那一项。
    """
    api = (APP_DIR / "api" / "image_sets.py").read_text(encoding="utf-8")
    tree = ast.parse(api)
    routes = {
        n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
    }
    # 没有 PATCH/PUT 之类的原地修改入口
    assert "derive" in routes
    assert not any(name in routes for name in ("update_set", "patch_set", "edit_items"))
    assert '"/{image_set_id}/derive"' in api


def test_approve_rejects_an_invalid_set():
    """校验放在批准这一步,不是发布那一步。

    发布时才发现主图缺失,人已经在等平台回执了;批准时发现,
    他手边正好就是那批图。
    """
    service = (APP_DIR / "listings" / "image_set_service.py").read_text(encoding="utf-8")
    tree = ast.parse(service)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "approve"
    )
    body = ast.unparse(fn)
    assert "validate_set" in body and "ValidationError" in body


def test_approving_archives_the_previous_version_in_the_same_scope():
    """同 scope 下只能有一版 APPROVED。

    不归档旧版的话,resolve 按版本号取最高的,而"回滚到旧版"
    就变成了"再建一个更高版本"——那不是回滚。
    """
    service = (APP_DIR / "listings" / "image_set_service.py").read_text(encoding="utf-8")
    tree = ast.parse(service)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "approve"
    )
    assert "ARCHIVED" in ast.unparse(fn)


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


# ---------------------------------------------------------------- 11 枚举


def test_a_string_status_is_still_recognised_as_approved():
    """评审第 11 条:`self.status is ImageSetStatus.APPROVED` 对字符串恒为 False。

    快照的状态来自 ORM 行或序列化数据时就是字符串,于是一个已批准的
    图片集被判定成不可发布 —— 而且没有任何报错。
    """
    from_orm = ImageSetSnapshot(
        image_set_id="is1", spu="SW-1", version=1, status="APPROVED"
    )
    assert from_orm.status is ImageSetStatus.APPROVED
    assert from_orm.is_publishable

    from_enum = ImageSetSnapshot(
        image_set_id="is1", spu="SW-1", version=1, status=ImageSetStatus.ARCHIVED
    )
    assert not from_enum.is_publishable


def test_an_illegal_status_is_rejected_at_construction():
    """认不出来的状态要当场炸,不能悄悄变成"不可发布"。"""
    try:
        ImageSetSnapshot(image_set_id="is1", spu="SW-1", version=1, status="APROVED")
    except ValueError:
        return
    raise AssertionError("非法状态没有被拒绝")


# ---------------------------------------------------------------- 15 入参约束


def test_image_set_schema_constrains_lengths_and_ordering():
    """评审第 15 条:超长值和重复排序号要到 flush 撞库时才失败,表现为 500。"""
    source = (APP_DIR / "schemas" / "image_set.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "max_length=32" in source, "channel / site / derivative_purpose 缺少长度约束"
    assert "max_length=64" in source, "variant_id 缺少长度约束"
    assert "ge=0" in source, "sort_order 允许了负数"
    validators = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and any("model_validator" in ast.unparse(d) for d in n.decorator_list)
    ]
    assert validators, "缺少同一请求内排序号与素材组合的唯一性校验"


def test_the_service_rejects_duplicates_before_they_reach_the_database():
    fn = _fn(_tree(APP_DIR / "listings" / "image_set_service.py"), "create_set")
    assert "_reject_duplicates" in _calls(fn)
