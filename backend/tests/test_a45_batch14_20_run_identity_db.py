"""识别 run 的身份与 §9.2 幂等 —— **真库那一半**(A45-batch14-20)。

## 这个文件在本轮一次都没跑过

打包这一批的机器上没有 PostgreSQL、没有 sqlalchemy。下面每一条都是
**规格**,不是成绩。它们验的三件事恰好是纯层结构守卫**验不到**的:

    部分唯一索引到底建没建起来      纯层只能看到 ORM 里那行声明
    IntegrityError 那一路捞不捞得到赢家   要两条真的并发事务
    server_default 对存量行生不生效  要一张真的有旧行的表

不把它们改写成纯层守卫来凑数:一条只验了「源码里出现过 IntegrityError」
的守卫会让本批的变异计数变成一句谎话。

## 跑法

    docker compose up -d db
    cd backend && pytest tests/test_a45_batch14_20_run_identity_db.py -v

## 先看这一条

`test_the_partial_unique_index_lets_a_failed_run_be_retried` 是最该先跑的:
它验的是**部分**唯一索引那个「部分」。谓词写丢了的话表现是一次识别失败
之后同样的输入再也建不出第二个 run —— 而那正是重试的定义,
运营看到的是一个再也识别不了的商品。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.attributes import run_state
from app.core.enums import ExtractionRunStatus as S
from app.models.attribute import ProductAttributeExtraction

pytestmark = pytest.mark.requires_db


def _run(session, product_id, *, key=None, status=S.COMPLETED, spu_id=None):
    row = ProductAttributeExtraction(
        product_id=product_id,
        extractor="mock",
        target_fields=["neckline"],
        image_count=1,
        succeeded_count=1,
        failed_count=0,
        schema_version="1",
        status=status.value,
        spu_id=spu_id,
        idempotency_key=key,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------- 索引


def test_the_same_key_cannot_be_taken_twice_while_it_is_occupied(session, product):
    """两行同键、都在占键档 -> 第二行必须被库拦下。

    拦不住的表现不是报错,是**双击建出两个 run**,每个都打一轮付费调用。
    """
    key = "k" * 64
    _run(session, product.id, key=key, status=S.RUNNING)
    with pytest.raises(IntegrityError):
        _run(session, product.id, key=key, status=S.COMPLETED)
        session.flush()


def test_the_partial_unique_index_lets_a_failed_run_be_retried(session, product):
    """同键、但前一行是 FAILED -> 必须能插进去。

    **这条验的是那个「部分」。** 谓词写丢了(全表唯一)的后果:
    一次失败之后同样的输入再也建不出第二个 run,而输入没变、模型没变、
    字段没变 —— 那正是重试的定义。
    """
    key = "f" * 64
    _run(session, product.id, key=key, status=S.FAILED)
    _run(session, product.id, key=key, status=S.RUNNING)  # 不该抛
    session.flush()


def test_partial_success_does_not_occupy_the_key_either(session, product):
    """PARTIAL_SUCCESS 同样不占键 —— 「按颜色重试」靠的就是这一条。

    它占键的话,失败的那几张永远不再有机会重跑,而 §13 阶段 3 把
    「按颜色重试」写成了交付项。
    """
    key = "p" * 64
    _run(session, product.id, key=key, status=S.PARTIAL_SUCCESS)
    _run(session, product.id, key=key, status=S.RUNNING)
    session.flush()


def test_null_keys_never_collide(session, product):
    """建不出键的那几路(没有 spu_id、增量识别、抽取器报不出版本)一律留空,
    而空键必须能重复 —— NULL 在唯一索引里互不相等。

    这条要是不成立,「建不出键」会从「退回本批之前的行为」变成
    「第二次识别直接 500」,而那是最糟的方向:挡住了本该跑的请求。
    """
    for _ in range(3):
        _run(session, product.id, key=None, status=S.COMPLETED)
    session.flush()


def test_the_index_predicate_in_the_database_matches_the_pure_one(session):
    """库里那条索引的谓词,与 `unique_index_predicate()` 生成的一致。

    迁移里写的是冻结字面量,纯层守卫比的是**源码**里那句话;
    这一条比的是**真的建出来的索引**。三者之间任意两个漂移都不报错,
    只是让「这个键被占了吗」在不同环境下有不同答案。
    """
    row = session.execute(
        text(
            "SELECT pg_get_indexdef(indexrelid) FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = 'uq_attr_extractions_idempotency_key'"
        )
    ).scalar_one()
    assert "UNIQUE" in row
    for status in sorted(s.value for s in run_state.KEY_OCCUPYING_STATUSES):
        assert status in row, f"索引谓词里没有 {status} —— 它和判定漂移了"
    for retryable in (S.FAILED, S.PARTIAL_SUCCESS, S.CANCELLED):
        assert retryable.value not in row, (
            f"{retryable.value} 出现在索引谓词里 —— 这个输入再也建不出第二个 run"
        )


# ---------------------------------------------------------------- 服务层


def test_a_second_identical_request_reuses_the_run_and_pays_nothing(
    session, product_with_spu, extractor_spy
):
    """同一个 SPU、同一批素材、同一组字段,连点两次 -> 一个 run、一轮调用。

    **这是 §9.2 的验收句。** 不成立时接口两次都返回 200、属性也都填上了,
    唯一的差别在账单上,而账单不会告诉你哪一笔是重复的。
    """
    from app.attributes import service as attr_service

    first = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    calls_after_first = extractor_spy.calls
    second = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    assert second.id == first.id, "第二次建了一个新 run"
    assert extractor_spy.calls == calls_after_first, "第二次又打了一轮付费调用"


def test_adding_one_photo_makes_the_next_request_a_new_run(
    session, product_with_spu, extractor_spy, add_evidence_asset
):
    """补一张图 -> 指纹变 -> 键变 -> 新 run。

    反方向才是真正的坏:补了图还命中旧键,于是新图**永远不会被识别**,
    而界面显示「识别完成」。
    """
    from app.attributes import service as attr_service

    first = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    add_evidence_asset(product_with_spu)
    second = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    assert second.id != first.id, "补了一张图之后还在复用旧 run —— 新图不会被识别"
    assert second.input_fingerprint != first.input_fingerprint


def test_a_product_without_an_spu_still_runs_but_gets_no_key(
    session, product, extractor_spy
):
    """没有 `spu_id` 的商品:识别照跑,只是没有幂等保护。

    方向是刻意的 —— 少挡一次的代价是一次重复付费,挡错一次的代价是
    一个再也识别不了的商品。老建档路径(阶段 1 的剩余项)还不写那一列。
    """
    from app.attributes import service as attr_service

    row = attr_service.run_extraction(session, product=product, extractor=extractor_spy)
    assert row.idempotency_key is None
    assert row.spu_id is None
    assert row.status in {s.value for s in S}


def test_an_incremental_run_gets_no_key_and_no_scope(
    session, product_with_spu, extractor_spy, one_media_id
):
    """只识别指定素材:不建键,也不写作用域。

    硬塞成 ALL 的话 `requested_scope` 会说一句不真的话 —— 而它是键的一部分,
    别的调用点照那句话复算会得到另一个键。
    """
    from app.attributes import service as attr_service

    row = attr_service.run_extraction(
        session,
        product=product_with_spu,
        extractor=extractor_spy,
        only_media_ids=[one_media_id],
    )
    assert row.idempotency_key is None
    assert row.requested_scope is None
    assert row.input_fingerprint, "增量 run 照样要记下它吃进去的那批素材"


def test_the_terminal_status_lands_in_the_column(session, product_with_spu, extractor_spy):
    """跑完之后 `status` 必须是终态,不能停在 RUNNING。

    停在 RUNNING 的后果不是显示错:RUNNING **占键**,于是这个 SPU
    再也识别不了第二次 —— 而没有任何地方会说为什么。
    """
    from app.attributes import service as attr_service

    row = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    assert run_state.is_terminal(row.status), f"跑完了还停在 {row.status}"


def test_a_legacy_row_defaults_to_the_closed_status(session, product):
    """0040 之前写下的行(没人算过它的状态)落在 `FAILED`。

    这一列不回填,理由在迁移文档里。方向必须是不放行那一侧:
    默认 COMPLETED 的话,一次从来没有被判定过的 run 会以「算数」的身份
    参与事实合并与占键两件要花钱的事。
    """
    session.execute(
        text(
            "INSERT INTO product_attribute_extractions "
            "(id, product_id, extractor, target_fields, image_count, "
            " succeeded_count, failed_count, schema_version, created_at, updated_at) "
            "VALUES (:i, :p, 'mock', '[]'::jsonb, 0, 0, 0, '1', now(), now())"
        ),
        {"i": uuid.uuid4(), "p": product.id},
    )
    status = session.execute(
        text(
            "SELECT status FROM product_attribute_extractions "
            "WHERE idempotency_key IS NULL ORDER BY created_at DESC LIMIT 1"
        )
    ).scalar_one()
    assert status == S.FAILED.value
    assert run_state.run_is_authoritative(status) is False
    assert S(status) not in run_state.KEY_OCCUPYING_STATUSES


def test_deleting_the_spu_keeps_the_ledger(session, product_with_spu, extractor_spy):
    """删掉 SPU 不连坐删掉识别记录 —— 那是「花了多少钱」的唯一凭据。

    `ON DELETE SET NULL`。断了归属之后下一次同型请求会因为取不到
    `spu_id` 而根本建不出键,于是不会误命中一条孤儿键。
    """
    from app.attributes import service as attr_service

    row = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    run_id = row.id
    session.execute(text("DELETE FROM spus WHERE id = :i"), {"i": product_with_spu.spu_id})
    session.flush()
    session.expire_all()
    survivor = session.get(ProductAttributeExtraction, run_id)
    assert survivor is not None, "删 SPU 把识别记录一起带走了 —— 账本没了"
    assert survivor.spu_id is None
