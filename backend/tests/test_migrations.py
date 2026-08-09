"""Alembic 迁移测试。需要真实 PostgreSQL。

验证 upgrade head 后表结构与 ORM 元数据一致,且 downgrade 能清干净。

两条测试卫生原则:
- 每个用例自己把库清空再迁移,不依赖前一个用例留下的状态(否则加一个用例、
  换一次执行顺序就红一片);
- 本模块会把库清空,跑完必须还原,否则后面的模块会撞上"表不存在"。
"""
from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.models import (
    Base,
    ColorVariant,
    ListingImageItem,
    ListingImageSet,
    MediaAsset,
    Product,
    Spu,
)
from app.models.generation_plan import GenerationPlan
from tests.conftest import TEST_CONNECT_ARGS, TEST_DB_URL, requires_db

pytestmark = requires_db


def _reset(engine) -> None:
    """回到空 schema；本模块已由破坏性测试库守卫限制。

    不能用 ``Base.metadata.drop_all``：迁移显式命名的外键不一定等于 ORM
    元数据推导的名称。按 ORM 名称删除一个由 Alembic 建出的库会在 teardown
    中途失败，并把半清理状态留给后续用例。
    """
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture
def alembic_config():
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "migrations")
    )
    return cfg


@pytest.fixture
def clean_engine():
    """给本模块一个干净的库,用完还原成 ORM 建的表。

    `connect_args` 与 `conftest.TEST_CONNECT_ARGS` 同源:全仓的 `create_engine`
    都得带那条时区钉子,漏一个就多一处"只在这台库上对"的路径。
    """
    engine = create_engine(TEST_DB_URL, connect_args=TEST_CONNECT_ARGS)
    _reset(engine)
    yield engine
    _reset(engine)
    Base.metadata.create_all(engine)
    engine.dispose()


def test_upgrade_creates_all_tables(alembic_config, clean_engine):
    command.upgrade(alembic_config, "head")
    tables = set(inspect(clean_engine).get_table_names())
    assert {"products", "product_assets", "audit_logs"} <= tables


def test_upgrade_matches_orm_metadata(alembic_config, clean_engine):
    """迁移建出来的列必须和 ORM 声明的完全一致,少一列多一列都要红。"""
    command.upgrade(alembic_config, "head")
    inspector = inspect(clean_engine)
    for table in Base.metadata.sorted_tables:
        actual = {c["name"] for c in inspector.get_columns(table.name)}
        expected = {c.name for c in table.columns}
        assert expected == actual, (
            f"{table.name} 列不一致: 缺 {expected - actual} 多 {actual - expected}"
        )


def test_downgrade_removes_every_table(alembic_config, clean_engine):
    """回滚到 base 之后,一张业务表都不能剩。

    断言的是**全部** ORM 表,不是抽查三张。原先纯测试里有一个静态版本
    (AST 扫 create_table / drop_table 是否配对),它认不出辅助函数、
    重命名和条件迁移,已经删掉 —— 那之后这条就是唯一守着「迁移只写不删」
    的地方,抽查等于没查。
    """
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    remaining = set(inspect(clean_engine).get_table_names())
    leftover = sorted({t.name for t in Base.metadata.sorted_tables} & remaining)
    assert not leftover, f"downgrade 后仍残留: {leftover}"


def test_upgrade_is_idempotent_from_scratch(alembic_config, clean_engine):
    """upgrade -> downgrade -> upgrade 必须能重复执行。

    迁移只写不删(忘了 drop index、drop enum)时,第二次 upgrade 会炸,
    而这恰恰是回滚演练时才会发现的问题,代价很高。
    """
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    tables = set(inspect(clean_engine).get_table_names())
    assert {"products", "generation_tasks", "generation_candidates"} <= tables


def test_0052_downgrade_archives_the_unrepresentable_draft(
    alembic_config, clean_engine
):
    """0051 不能表示同层 ACTIVE + DRAFT;降级保留 ACTIVE 且不删草稿正文。"""
    command.upgrade(alembic_config, "head")
    with Session(clean_engine) as session:
        spu = Spu(
            spu_code="MIG-0052",
            internal_name="0052 downgrade",
            audience="WOMEN",
            base_category="swimwear",
            created_by="migration-test",
        )
        session.add(spu)
        session.flush()
        active = GenerationPlan(
            spu_id=spu.id,
            provider="mock",
            scene="studio",
            pose="STANDING_FRONT",
            angles_json=[{"angle": "FRONT", "count": 1}],
            plan_fingerprint="active-fingerprint",
            status="ACTIVE",
        )
        draft = GenerationPlan(
            spu_id=spu.id,
            provider="mock",
            scene="beach",
            pose="STANDING_FRONT",
            angles_json=[{"angle": "FRONT", "count": 1}],
            plan_fingerprint="draft-fingerprint",
            status="DRAFT",
        )
        session.add_all((active, draft))
        session.commit()
        active_id, draft_id = active.id, draft.id

    command.downgrade(alembic_config, "0051")
    with clean_engine.connect() as conn:
        statuses = dict(
            conn.execute(
                text(
                    "SELECT id::text, status FROM generation_plans "
                    "WHERE id IN (:active_id, :draft_id)"
                ),
                {"active_id": str(active_id), "draft_id": str(draft_id)},
            ).all()
        )
    assert statuses[str(active_id)] == "ACTIVE"
    assert statuses[str(draft_id)] == "ARCHIVED"

    command.upgrade(alembic_config, "head")


def _two_spus_with_the_same_variant_key(engine) -> tuple[dict[str, str], dict[str, str]]:
    """在 0045 形状里造两个都有 ``BLK`` 的 SPU 与图片项。"""
    expected: dict[str, str] = {}
    image_sets: dict[str, str] = {}
    with Session(engine) as session:
        for suffix in ("A", "B"):
            code = f"MIG-0046-{suffix}"
            spu = Spu(
                spu_code=code,
                internal_name=f"0046 migration {suffix}",
                audience="WOMEN",
                base_category="swimwear",
                created_by="migration-test",
            )
            session.add(spu)
            session.flush()
            variant = ColorVariant(
                spu_id=spu.id,
                variant_code="BLK",
                working_name="Black",
                display_name="Black",
            )
            session.add(variant)
            session.flush()
            product = Product(
                spu_id=spu.id,
                color_variant_id=None,
                spu=code,
                sku=f"{code}-BLK-S",
                name=f"0046 product {suffix}",
                category="swimwear",
                audience="WOMEN",
                variant_key="BLK",
                size="S",
            )
            session.add(product)
            session.flush()
            media = MediaAsset(
                product_id=product.id,
                spu_id=spu.id,
                spu=code,
                source="MANUAL_UPLOAD",
                role="PRODUCT_FRONT",
                storage_path=f"migration/{suffix}.jpg",
                mime_type="image/jpeg",
                sha256=(suffix.lower() * 64),
                status="READY",
                evidence_class="PRODUCT_EVIDENCE",
            )
            image_set = ListingImageSet(spu=code, version=1, status="DRAFT")
            session.add_all((media, image_set))
            session.flush()
            session.add(
                ListingImageItem(
                    image_set_id=image_set.id,
                    media_asset_id=media.id,
                    role="PRODUCT_FRONT",
                    sort_order=0,
                    variant_id="BLK",
                )
            )
            expected[code] = str(variant.id)
            image_sets[code] = str(image_set.id)
        session.commit()
    return expected, image_sets


def test_0046_rewrites_same_named_image_variants_inside_their_own_spu(
    alembic_config, clean_engine
):
    """两个 SPU 都有 ``BLK`` 时,图片项不得被改成同一个颜色 UUID。"""
    command.upgrade(alembic_config, "0045")
    expected, _image_sets = _two_spus_with_the_same_variant_key(clean_engine)

    command.upgrade(alembic_config, "0046")
    with clean_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT s.spu, i.variant_id
                  FROM listing_image_items i
                  JOIN listing_image_sets s ON s.id = i.image_set_id
                 WHERE s.spu LIKE 'MIG-0046-%'
                 ORDER BY s.spu
                """
            )
        ).all()
    assert dict(rows) == expected

    command.downgrade(alembic_config, "0045")
    with clean_engine.connect() as conn:
        restored = conn.execute(
            text(
                """
                SELECT s.spu, i.variant_id
                  FROM listing_image_items i
                  JOIN listing_image_sets s ON s.id = i.image_set_id
                 WHERE s.spu LIKE 'MIG-0046-%'
                """
            )
        ).all()
    assert {value for _spu, value in restored} == {"BLK"}


def test_0046_refuses_ambiguous_working_names(alembic_config, clean_engine):
    """同一 SPU 两个颜色的临时名称相同时,迁移必须失败而不是任选一个。"""
    command.upgrade(alembic_config, "0045")
    with Session(clean_engine) as session:
        spu = Spu(
            spu_code="MIG-0046-AMB",
            internal_name="0046 ambiguous",
            audience="WOMEN",
            base_category="swimwear",
            created_by="migration-test",
        )
        session.add(spu)
        session.flush()
        session.add_all(
            (
                ColorVariant(
                    spu_id=spu.id,
                    variant_code="A",
                    working_name="Supplier Blue",
                    display_name="A",
                ),
                ColorVariant(
                    spu_id=spu.id,
                    variant_code="B",
                    working_name="Supplier Blue",
                    display_name="B",
                ),
                Product(
                    spu_id=spu.id,
                    color_variant_id=None,
                    spu=spu.spu_code,
                    sku="MIG-0046-AMB-S",
                    name="0046 ambiguous product",
                    category="swimwear",
                    audience="WOMEN",
                    variant_key="Supplier Blue",
                    size="S",
                ),
            )
        )
        session.commit()

    with pytest.raises(RuntimeError, match="迁移不能替你任选一个"):
        command.upgrade(alembic_config, "0046")
