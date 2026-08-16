"""M0 契约形状测试。

这批用例**只断言契约的形状与自洽性**,不测任何业务行为 —— 业务实现还没写。
它们存在的意义是:M1 之后每一层都以这些契约为输入,契约一旦被人顺手改坏
(加一个可变默认值、让适配器能查库、把 known 塞回抽取器签名),
这里当场变红,而不是等到 M7 做真实映射时才发现。

零三方依赖,`python3 tools/run_pure_tests.py` 可直接跑。
"""
from __future__ import annotations

import ast
import dataclasses

from app.attributes.contracts import (
    AttrValue,
    CanonicalProduct,
    CanonicalSku,
    CanonicalVariant,
    ConfidenceBreakdown,
)
from app.attributes.registry import (
    REGISTRY,
    AttributeField,
    AutoConfirm,
    ValueType,
    enum_definitions,
    extractable_fields,
    legacy_projection_map,
)
from app.core.enums import (
    DRY_RUN_STATES,
    PUBLISH_TERMINAL_STATES,
    AttributeSource,
    AttributeStatus,
    ImageSetStatus,
    MediaRole,
    MediaSource,
    MediaStatus,
    PublishStatus,
    RoleSource,
)
from app.listings.contracts import (
    ImageItemSnapshot,
    ImageSetSnapshot,
    ListingDraftData,
    MappedListing,
)
from app.media.contracts import MediaAssetData, MediaConsentData
from tests.pure._helpers import BACKEND_ROOT, expect_raises

APP_DIR = BACKEND_ROOT / "app"

#: M0 定稿的契约模块。它们**不允许**碰数据库、网络或 Web 框架。
CONTRACT_MODULES = (
    "media/contracts.py",
    "attributes/contracts.py",
    "attributes/registry.py",
    "listings/contracts.py",
    "channels/base.py",
    "channels/source_expr.py",
    "extractors/base.py",
)

#: 契约层不得出现的依赖。这不是洁癖:一旦契约能 import sqlalchemy,
#: 就会有人在 dataclass 上挂一个 relationship,于是「纯函数可离线测试」
#: 这条整个方案赖以成立的前提就没了。
FORBIDDEN_IMPORTS = ("sqlalchemy", "fastapi", "httpx", "celery", "boto3", "redis")


def _asset(**overrides) -> MediaAssetData:
    base = dict(
        id="m1",
        product_id="p1",
        storage_path="uploads/ab/cd/x.jpg",
        mime_type="image/jpeg",
        width=1200,
        height=1600,
        bytes=204800,
        sha256="a" * 64,
        source=MediaSource.MANUAL_UPLOAD,
        status=MediaStatus.READY,
    )
    base.update(overrides)
    return MediaAssetData(**base)


# ---------------------------------------------------------------- 依赖方向


def test_contract_modules_have_no_infrastructure_imports():
    """契约层不许 import 任何基础设施。

    用 AST 找 import 语句,不用字符串包含 —— 后者在注释里写一句
    就能骗过去,而这条约束恰恰最容易在「就用一下」时被破坏。
    """
    problems: list[str] = []
    for relative in CONTRACT_MODULES:
        tree = ast.parse((APP_DIR / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                head = name.split(".")[0]
                if head in FORBIDDEN_IMPORTS:
                    problems.append(f"{relative} 导入了 {name}")
    assert not problems, "契约层被基础设施污染 -> " + "; ".join(problems)


def test_every_contract_module_exists():
    for relative in CONTRACT_MODULES:
        assert (APP_DIR / relative).is_file(), f"缺少契约模块 {relative}"


def test_contracts_are_frozen_dataclasses():
    """契约一律是 frozen dataclass。

    可变的契约对象会被中途改写,而它同时也是快照的来源 ——
    落库的 `canonical_snapshot` 与内存里那份对不上,是最难查的一类 bug。
    """
    for cls in (
        MediaAssetData,
        MediaConsentData,
        AttrValue,
        CanonicalProduct,
        CanonicalVariant,
        CanonicalSku,
        ImageSetSnapshot,
        ImageItemSnapshot,
        ListingDraftData,
        MappedListing,
    ):
        assert dataclasses.is_dataclass(cls), f"{cls.__name__} 不是 dataclass"
        params = cls.__dataclass_params__
        assert params.frozen, f"{cls.__name__} 必须是 frozen"


# ---------------------------------------------------------------- 素材契约


def test_media_source_and_role_are_orthogonal():
    """来源与角色互不约束。

    AI 生成的正面图、供应商推来的尺码表,都必须是合法组合。
    v1 把两者挤进一个 AssetType,AI 图就只能当成品图。
    """
    for source in MediaSource:
        for role in MediaRole:
            asset = _asset(source=source, role=role)
            assert asset.source is source
            assert asset.role is role


def test_only_ready_assets_are_usable():
    assert _asset(status=MediaStatus.READY).is_usable
    for status in (
        MediaStatus.PENDING,
        MediaStatus.QUARANTINED,
        MediaStatus.FAILED,
        MediaStatus.DELETED,
    ):
        assert not _asset(status=status).is_usable, f"{status} 不该可用"


def test_model_guessed_role_cannot_be_primary_without_high_confidence():
    """角色是模型猜的就不能当主图(§7.3)。"""
    assert not _asset(
        role_source=RoleSource.MODEL, role_confidence=0.89
    ).can_be_primary
    assert _asset(role_source=RoleSource.MODEL, role_confidence=0.9).can_be_primary
    # 人工指定的不看置信度
    assert _asset(role_source=RoleSource.HUMAN, role_confidence=None).can_be_primary
    # 隔离的素材无论如何都不行
    assert not _asset(
        status=MediaStatus.QUARANTINED, role_source=RoleSource.HUMAN
    ).can_be_primary


def test_consent_scopes_are_independent():
    """四个授权标志位相互独立,缺一不可(§12.1)。"""
    from app.core.enums import ConsentScope

    consent = MediaConsentData(
        id="c1",
        subject_type="REAL_MODEL",
        scope=frozenset({ConsentScope.AI_PROCESSING.value}),
    )
    assert consent.covers(ConsentScope.AI_PROCESSING)
    # 能用 AI 处理 ≠ 能跨境传输 ≠ 能商用发布
    assert not consent.covers(ConsentScope.CROSS_BORDER_TRANSFER)
    assert not consent.covers(ConsentScope.COMMERCIAL_PUBLISH)


# ---------------------------------------------------------------- 属性注册表


def test_registry_enum_fields_align_with_core_enums():
    """注册表声明的枚举取值必须逐一落在 core/enums.py 上。

    两处各写一份取值表,迟早有一处漏掉新增的值,而表现是
    模型输出了一个「合法」的值却在归一化时被丢弃。
    """
    for name, spec in REGISTRY.items():
        if spec.value_type not in (ValueType.ENUM, ValueType.ENUM_LIST):
            continue
        assert spec.enum is not None, f"{name} 缺少枚举类"
        values = [v.value for v in spec.enum]
        assert values, f"{name} 的枚举是空的"
        assert len(values) == len(set(values)), f"{name} 的枚举有重复值"


def test_non_visual_fields_never_reach_the_extraction_schema():
    """看图看不出来的字段,禁止让模型猜。

    这是 v1 靠提示词叮嘱的一条规矩,v2 把它变成注册表的静态断言。
    材质是最典型的例子:模型会非常自信地给出「82% 锦纶 / 18% 氨纶」,
    而没有任何图像证据能推翻它。
    """
    extractable = set(extractable_fields())
    for name, spec in REGISTRY.items():
        if not spec.visual:
            assert name not in extractable, f"{name} 不可见却进了识别 Schema"
            assert spec.auto_confirm_policy is AutoConfirm.NEVER
    assert "material" in REGISTRY and "material" not in extractable


def test_enum_definitions_only_cover_extractable_fields():
    defs = enum_definitions()
    assert set(defs) <= set(extractable_fields())
    for name, values in defs.items():
        assert values, f"{name} 的枚举定义是空的"


def test_never_policy_has_no_auto_confirm_threshold():
    for name, spec in REGISTRY.items():
        if spec.auto_confirm_policy is AutoConfirm.NEVER:
            assert spec.auto_confirm_threshold is None, f"{name} 不该有阈值"
        else:
            assert spec.auto_confirm_threshold is not None


def test_identity_fields_use_the_strictest_threshold():
    """颜色与品类错了直接上错货,阈值必须最高(§6.4)。"""
    assert REGISTRY["garment_type"].auto_confirm_threshold == 0.95
    assert REGISTRY["primary_color"].auto_confirm_threshold == 0.95
    assert REGISTRY["neckline_type"].auto_confirm_threshold == 0.92


def test_legacy_projection_covers_the_existing_product_columns():
    """8 个兼容投影列必须都能从注册表映射回去(§5.4)。

    少一个的意思是:那一列从此没有事实源在维护它,而旧查询还在读它。
    """
    projected = set(legacy_projection_map().values())
    expected = {
        "primary_color",
        "secondary_colors",
        "pattern_type",
        "garment_type",
        "material",
        "strap_type",
        "neckline_type",
        "back_style",
    }
    assert expected <= projected, f"缺少投影:{sorted(expected - projected)}"


def test_registry_rejects_self_inconsistent_fields():
    """契约的自洽性在构造时就查,不留到运行期。"""
    from app.core.enums import NecklineType, OwnerType

    # ENUM 却没给枚举类
    expect_raises(
        ValueError,
        AttributeField,
        name="bad",
        label="占位",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
    )
    # 看不出来的字段却允许自动确认
    expect_raises(
        ValueError,
        AttributeField,
        name="bad2",
        label="占位",
        owner_type=OwnerType.SPU,
        value_type=ValueType.TEXT,
        visual=False,
        auto_confirm_policy=AutoConfirm.THRESHOLD,
    )
    # multi_value 与 ENUM_LIST 不一致
    expect_raises(
        ValueError,
        AttributeField,
        name="bad3",
        label="占位",
        owner_type=OwnerType.SPU,
        value_type=ValueType.ENUM,
        enum=NecklineType,
        multi_value=True,
    )


# ---------------------------------------------------------------- 置信度


def test_uncalibrated_confidence_is_none_not_zero():
    """未校准返回 None,不是 0。

    返回 0 会让它看起来像「置信度很低」,于是被当成一个普通的低分值,
    而实际含义是「这个数字根本没有意义」。fail closed 的前提是
    这两种情况在类型上就分得开。
    """
    breakdown = ConfidenceBreakdown(
        calibrated=None,
        agreement_factor=1.0,
        quality_factor=1.0,
        visibility_prior=1.0,
        source_factor=1.0,
    )
    assert breakdown.value is None


def test_confidence_factors_multiply():
    breakdown = ConfidenceBreakdown(
        calibrated=0.9,
        agreement_factor=0.95,
        quality_factor=0.9,
        visibility_prior=0.95,
        source_factor=0.8,
    )
    expected = 0.9 * 0.95 * 0.9 * 0.95 * 0.8
    assert abs(breakdown.value - expected) < 1e-9


def test_ai_generated_source_factor_is_expressible():
    """AI 图只能是旁证(硬规矩 6)。

    契约层保证 `source_factor` 是一个独立因子 —— 于是「AI 图打八折」
    是一个可配置的数,而不是散落在合并逻辑里的 if。
    """
    ai = ConfidenceBreakdown(
        calibrated=0.95,
        agreement_factor=1.0,
        quality_factor=1.0,
        visibility_prior=1.0,
        source_factor=0.8,
    )
    original = dataclasses.replace(ai, source_factor=1.0)
    assert ai.value < original.value


# ---------------------------------------------------------------- 刊登契约


def _draft(**overrides) -> ListingDraftData:
    image_set = ImageSetSnapshot(
        image_set_id="is1",
        spu="SW-1",
        version=1,
        status=ImageSetStatus.APPROVED,
        items=(
            ImageItemSnapshot(
                media_asset_id="m1",
                role=MediaRole.PRODUCT_FRONT,
                sort_order=0,
                storage_path="uploads/a.jpg",
                width=1200,
                height=1600,
                is_primary=True,
            ),
        ),
    )
    product = CanonicalProduct(
        spu="SW-1",
        spu_attrs={
            "garment_type": AttrValue(
                field_name="garment_type",
                value="ONE_PIECE",
                source=AttributeSource.EXTRACTED,
                status=AttributeStatus.CONFIRMED,
                version_id="v1",
            )
        },
    )
    base = dict(
        spu="SW-1",
        channel="SHEIN",
        site="TODO_SITE",
        category_id="TODO",
        product=product,
        image_set=image_set,
    )
    base.update(overrides)
    return ListingDraftData(**base)


def test_only_approved_image_sets_are_publishable():
    """发布只读 APPROVED(硬规矩 5)。"""
    draft = _draft()
    assert draft.image_set.is_publishable
    for status in (
        ImageSetStatus.DRAFT,
        ImageSetStatus.PENDING_REVIEW,
        ImageSetStatus.ARCHIVED,
    ):
        stale = dataclasses.replace(draft.image_set, status=status)
        assert not stale.is_publishable, f"{status} 不该可发布"


def test_confirmed_attrs_excludes_suggestions():
    """`ContentPlan.facts` 只能来自 CONFIRMED(§8.1)。"""
    product = CanonicalProduct(
        spu="SW-1",
        spu_attrs={
            "a": AttrValue(
                field_name="a",
                value="X",
                source=AttributeSource.EXTRACTED,
                status=AttributeStatus.CONFIRMED,
            ),
            "b": AttrValue(
                field_name="b",
                value="Y",
                source=AttributeSource.EXTRACTED,
                status=AttributeStatus.SUGGESTED,
            ),
        },
    )
    assert set(product.confirmed_attrs()) == {"a"}


def test_fingerprint_is_stable_for_the_same_facts():
    """同一份事实必须给出同一个指纹。

    不稳定的指纹会让草稿重算一次就 STALE 一次,这个机制立刻变成噪音,
    然后有人会把它关掉。
    """
    assert _draft().source_fingerprint() == _draft().source_fingerprint()


def test_fingerprint_changes_when_any_upstream_changes():
    """上游任一变化 → 指纹变化 → 草稿 STALE(§9.1)。"""
    base = _draft()
    original = base.source_fingerprint()

    # 图片集出了新版本
    new_set = dataclasses.replace(base.image_set, version=2)
    assert dataclasses.replace(base, image_set=new_set).source_fingerprint() != original

    # 属性被改了(版本 id 变化)
    changed_attr = dataclasses.replace(
        base.product.spu_attrs["garment_type"], version_id="v2"
    )
    new_product = dataclasses.replace(
        base.product, spu_attrs={"garment_type": changed_attr}
    )
    assert dataclasses.replace(base, product=new_product).source_fingerprint() != original

    # 运营改了手填字段
    assert (
        dataclasses.replace(base, manual={"price": "9.9"}).source_fingerprint()
        != original
    )

    # spec 换版
    assert dataclasses.replace(base, spec_version="1.1").source_fingerprint() != original


def test_mapped_listing_is_invalid_only_on_blocking_violations():
    """warning 不该挡住导出。

    同义词词典扫描出的颜色词不一致只能是 warning —— 词典永远不全,
    做成硬失败会把正常文案全拦下来,然后有人把这条检查删掉。
    """
    from app.listings.contracts import FieldViolation

    warn = FieldViolation(field_key="k", code="C", message="m", level="warning")
    err = FieldViolation(field_key="k", code="C", message="m", level="error")
    assert MappedListing(violations=(warn,)).is_valid
    assert not MappedListing(violations=(warn, err)).is_valid


# ---------------------------------------------------------------- 发布状态


def test_publish_terminal_states_are_only_two():
    """终态只有 CANCELLED / ARCHIVED(意见 #18)。

    `VALIDATION_FAILED` 改了字段就能重来,`LISTED` 后面还有更新和下架。
    把它们标成终态的代价是将来改状态机要动所有断言。
    """
    assert PUBLISH_TERMINAL_STATES == {
        PublishStatus.CANCELLED.value,
        PublishStatus.ARCHIVED.value,
    }
    assert PublishStatus.VALIDATION_FAILED.value not in PUBLISH_TERMINAL_STATES
    assert PublishStatus.LISTED.value not in PUBLISH_TERMINAL_STATES


def test_dry_run_states_never_reach_the_platform():
    """干跑不进 LISTED(意见 #16)。

    干跑止于 DRY_RUN_COMPLETED:不产生平台 ID、不更新刊登状态、
    不计入任何 Dashboard 指标。这里断言的是状态集,
    M8 的转移表还要再断言一次「不存在通往 SUBMITTED 的路径」。
    """
    forbidden = {
        PublishStatus.UPLOADING_IMAGES.value,
        PublishStatus.SUBMITTING.value,
        PublishStatus.SUBMITTED.value,
        PublishStatus.LISTED.value,
        PublishStatus.PLATFORM_REVIEWING.value,
    }
    assert not (DRY_RUN_STATES & forbidden), "干跑状态集里混进了真实提交的状态"
    assert PublishStatus.DRY_RUN_COMPLETED.value in DRY_RUN_STATES


# ---------------------------------------------------------------- 抽取器签名


def test_extractor_signature_has_no_known_parameter():
    """抽取器签名里**不能有** `known`(意见 #8)。

    把它写成契约级约束而不是提示词里的一句叮嘱:签名里没有这个参数,
    就没人能在某次「临时调试」时把已有值传进去,然后忘了删。
    """
    tree = ast.parse((APP_DIR / "extractors" / "base.py").read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "extract"
    )
    names = {a.arg for a in (*fn.args.args, *fn.args.kwonlyargs)}
    assert "known" not in names, "抽取器又能拿到已有值了,交叉验证会退化成复读"
    assert "image" in names, "必须是逐图调用"
    assert "images" not in names, "不能一次塞多张图 —— 增量识别与失败隔离都靠逐图"
