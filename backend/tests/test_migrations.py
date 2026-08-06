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

from app.models import Base
from tests.conftest import TEST_DB_URL, requires_db

pytestmark = requires_db


def _reset(engine) -> None:
    """回到"什么都没有"的状态:业务表 + alembic 版本表全部清掉。"""
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


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
    """给本模块一个干净的库,用完还原成 ORM 建的表。"""
    engine = create_engine(TEST_DB_URL)
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
