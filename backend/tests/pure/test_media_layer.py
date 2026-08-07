"""M1 素材层:去重语义、正交性、影子写映射、对账(零三方依赖)。

这批用例不碰数据库 —— 它们测的是**映射规则与判定逻辑**,而那正是
迁移期最容易出错、也最需要能快速穷举的部分。真库层的行为
(唯一约束真的生效、外键真的级联)在 `tests/test_api_media.py`。
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT

APP_DIR = BACKEND_ROOT / "app"


# ---------------------------------------------------------------- 映射表


def test_every_legacy_asset_type_maps_to_a_role():
    """旧 `AssetType` 必须全部有对应角色。

    漏一个的后果是那类素材影子写时角色为 None,而下游的图片集
    要按角色取图 —— 表现是「这张图在素材库里看得到,却怎么也进不了图片集」。
    """
    from app.core.enums import AssetType, MediaRole
    from app.media.mapping import LEGACY_ASSET_TYPE_TO_ROLE

    missing = [t.value for t in AssetType if t.value not in LEGACY_ASSET_TYPE_TO_ROLE]
    assert not missing, f"这些旧类型没有角色映射:{missing}"
    for role in LEGACY_ASSET_TYPE_TO_ROLE.values():
        assert isinstance(role, MediaRole)


def test_lossy_mappings_are_explicit():
    """两条有损映射必须映到明确的值,不能留 None。

    `GARMENT_CUTOUT -> FLAT_LAY`、`MODEL_REFERENCE -> MODEL_FRONT` 都是有损的:
    旧数据里分不出模特图的正反面。有损是事实,但**不该用 None 掩盖它** ——
    None 会让这条素材对下游不可用,而人工在素材库里看到一个明确的值
    才知道该不该改。
    """
    from app.core.enums import AssetType, MediaRole
    from app.media.mapping import LEGACY_ASSET_TYPE_TO_ROLE

    assert LEGACY_ASSET_TYPE_TO_ROLE[AssetType.GARMENT_CUTOUT.value] is MediaRole.FLAT_LAY
    assert (
        LEGACY_ASSET_TYPE_TO_ROLE[AssetType.MODEL_REFERENCE.value] is MediaRole.MODEL_FRONT
    )


# ---------------------------------------------------------------- 正交性


def test_media_asset_model_keeps_source_and_role_in_separate_columns():
    """`source` 与 `role` 必须是两列。

    合成一列是 v1 的原罪:AI 图天然只能是成品图,供应商推来的正面图
    和人工上传的正面图要走两条代码路径。用 AST 断言,而不是靠人记得。
    """
    tree = ast.parse((APP_DIR / "models" / "media_asset.py").read_text(encoding="utf-8"))
    cls = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "MediaAsset"
    )
    columns = {
        stmt.target.id
        for stmt in cls.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    }
    assert {"source", "role", "role_source", "role_confidence"} <= columns
    assert "asset_type" not in columns, "又把来源和角色混成一列了"
    assert "purpose" not in columns, "用途属于派生版本,不属于素材"


def test_the_dedupe_key_is_never_global():
    """去重键**不是全局 sha256**。这一条与阶段无关,任何一版都成立。

    跨商品不去重是刻意的:两个 SKU 用同一张图是两条记录,因为角色、
    变体绑定、授权、隔离状态都是按商品算的。物理层的内容寻址
    已经保证字节只存一份。全局唯一会让第二个商品根本存不进去。

    这一条从原 `test_dedupe_key_is_product_scoped` 拆出来。原来那条把
    「不许全局」和「今天是 product 作用域」写在一个断言组里,而前者是
    永久不变量、后者是一笔欠账 —— 混在一起的后果见下一条。
    """
    source = (APP_DIR / "models" / "media_asset.py").read_text(encoding="utf-8")
    assert 'UniqueConstraint("sha256"' not in source
def test_derivative_uniqueness_includes_transform_version():
    """派生版本的唯一键必须带算法版本。

    不带的话,裁切算法改版之后新图写不进去 —— 而旧图还挂在网站上,
    这时候需要的是两版共存,不是覆盖。
    """
    source = (APP_DIR / "models" / "media_asset.py").read_text(encoding="utf-8")
    assert '"transform_version"' in source
    idx = source.index("uq_media_derivatives_source_purpose_version")
    window = source[max(0, idx - 400):idx]
    for column in ("source_media_asset_id", "purpose", "transform_version"):
        assert f'"{column}"' in window, f"派生唯一键缺少 {column}"


# ---------------------------------------------------------------- 影子写


def test_shadow_write_happens_in_the_business_transaction():
    """影子写不能自己 commit。

    分两个事务写的话,业务提交成功而影子写失败时两边永久不一致,
    而且没有任何东西会发现 —— 对账要到迁移 B 才上线。

    用 AST 断言 `shadow_*` 函数体里没有 `session.commit()`。
    """
    tree = ast.parse((APP_DIR / "media" / "service.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("shadow_")):
            continue
        body = ast.unparse(node)
        assert "session.commit()" not in body, (
            f"{node.name} 自己提交了事务,影子写会脱离业务事务"
        )


def test_all_three_write_paths_are_wired():
    """三条写入路径都要接上影子写。

    漏一条的表现是那一类素材在素材库里根本不出现,而回填脚本
    只补历史数据、不补上线之后的新数据 —— 缺口会一直扩大。
    """
    cases = {
        "services/asset_service.py": "shadow_from_product_asset",
        "tasks/generation_tasks.py": "shadow_from_candidate",
        "services/output_service.py": "shadow_from_output_asset",
    }
    for relative, call in cases.items():
        source = (APP_DIR / relative).read_text(encoding="utf-8")
        assert call in source, f"{relative} 没有接影子写({call})"


def test_output_assets_become_derivatives_not_assets():
    """成品图落派生版本,不是新素材(§4.3)。

    当成独立素材的话,同一张图会在素材库里出现五遍(五个用途),
    而去重、授权、保留策略全是按素材算的 —— 一张图五份授权说不通。
    """
    source = (APP_DIR / "media" / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "shadow_from_output_asset"
    )
    body = ast.unparse(fn)
    assert "MediaDerivative(" in body
    assert "MediaAsset(" not in body, "成品图不该产生新素材"


def test_failed_downloads_produce_no_media():
    """下载失败的候选不产生素材。

    它记录的是「这一轮向 Provider 要过图但没拿到」,那不是一张图。
    造一条空素材会让素材库里出现打不开的条目,而识别层会去读它。
    """
    tree = ast.parse((APP_DIR / "media" / "service.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "shadow_from_candidate"
    )
    body = ast.unparse(fn)
    assert "return None" in body
    assert "storage_path" in body and "file_hash" in body


# ---------------------------------------------------------------- 对账


def test_role_is_excluded_from_parity_check():
    """角色**不参与**对账。

    旧枚举到新角色的映射本来就是有损的,而且人工在素材库里改过角色之后
    新旧两边必然不同。把它算进对账会产生一堆无法消除的告警,
    然后所有人开始忽略这张表 —— 那时候真正的不一致也没人看了。
    """
    from app.media.mapping import PARITY_FIELDS

    assert "role" not in PARITY_FIELDS
    assert "role_source" not in PARITY_FIELDS
    assert "status" not in PARITY_FIELDS, "隔离状态是人工改的,不该算不一致"
    # 真正该比的是那些"同一份事实的两种存法"
    assert {"storage_path", "sha256", "width", "height", "bytes"} <= set(PARITY_FIELDS)


def test_normalize_makes_numeric_types_comparable():
    """比较前统一形状。

    数值列在旧表是 int、新表可能是 Decimal(Numeric 列),直接 != 会产生
    一堆假的不一致;空串与 None 同样视作「没有值」。
    """
    from decimal import Decimal

    from app.media.mapping import normalize_for_parity as _normalize

    assert _normalize(1200) == _normalize(Decimal("1200"))
    assert _normalize(1200) == _normalize(1200.0)
    assert _normalize(None) == _normalize("")
    assert _normalize("a") != _normalize("b")


def test_missing_shadow_row_is_reported_as_a_mismatch():
    """影子记录缺失本身就是一条不一致。

    只比对字段的话,「这条根本没被影子写」会安静地通过 ——
    而那恰恰是最严重的一种迁移缺陷。
    """
    source = (APP_DIR / "media" / "service.py").read_text(encoding="utf-8")
    assert "__missing__" in source


# ---------------------------------------------------------------- 入口改造


def test_listing_entry_does_not_depend_on_generation_task():
    """上架入口只看素材,不看生成任务状态(§4.5)。

    供应商已有图的商品从来不会存在 `GenerationTask`,也必须能完整走完全流程。
    """
    tree = ast.parse((APP_DIR / "media" / "service.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "usable_assets"
    )
    # 跳过 docstring:那里提到 GenerationTask 恰恰是在说明"不依赖它"
    body = "\n".join(ast.unparse(stmt) for stmt in fn.body[1:])
    assert "GenerationTask" not in body, "上架入口又依赖生成任务状态了"
    assert "MediaStatus.READY" in body


def test_quarantined_assets_are_not_usable():
    """隔离的素材不参与上架。"""
    tree = ast.parse((APP_DIR / "media" / "service.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "usable_assets"
    )
    body = "\n".join(ast.unparse(stmt) for stmt in fn.body[1:])
    # 只放 READY,而不是"排除 DELETED"这种黑名单写法
    assert "MediaStatus.READY.value" in body
    assert "QUARANTINED" not in body, "应当是白名单(只要 READY),不是排除法"


# ---------------------------------------------------------------- 鉴权边界


def test_media_library_requires_an_operator_token():
    """素材库能看到未发布商品的全部原图,和商品列表同一敏感级别。"""
    source = (APP_DIR / "api" / "media_library.py").read_text(encoding="utf-8")
    assert "dependencies=[Depends(require_operator)]" in source


def test_signed_file_endpoint_is_not_under_the_media_library_prefix():
    """签名代发和素材库**不能共用前缀**。

    `/media-files` 在 `main.PUBLIC_PREFIXES` 白名单里(它自己验签名),
    共用前缀会把整个素材库的增删改查一起放开成匿名的。
    """
    main = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert '"/media-files"' in main
    assert '"/media"' not in main, "素材库前缀被放进了匿名白名单"

    files_api = (APP_DIR / "api" / "media_files.py").read_text(encoding="utf-8")
    assert '"/media-files/{storage_path:path}"' in files_api
