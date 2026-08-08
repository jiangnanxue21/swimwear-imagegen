"""A45-batch14-19:粗筛必须是超集 —— 这一条只有真库能回答。

## 为什么必须有这份

`evidence_assets_for()` 的分工是「SQL 粗筛 + Python 判定」,而粗筛的正确性
条件只有一条:**判定为 True 的行,一定通过粗筛**。粗筛多挡了一行,后果是
那张图静静地不进识别输入 —— 而「本该识别却没识别」没有任何地方会报:
运营看到的是一次成功的识别,只是少了几个字段,而字段少了会被当成
"这张图上看不出来"。

`tests/pure/` 那份守卫钉的是结构(粗筛用的列在不在那张被证明过的清单里)。
结构对不等于 SQL 对:`is_(None)` 写成 `isnot(None)`、`not_in` 写成 `in_`,
两者的 AST 形状几乎一样,而结果正好相反。**只有真的把行写进库、真的跑一遍
那条 SELECT,才分得出来。**

## 判据

对同一批构造出来的素材行,比较两条路径:

    参照   读出全部行,逐行跑 `asset_is_extraction_input()`
    实际   `evidence_assets_for()`(粗筛 + 同一个判定)

两者必须给出**完全相同**的 id 集合。参照那一路刻意不用粗筛 ——
拿粗筛去验粗筛,写反了两边一起反,而测试全绿。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.enums import MediaRole, MediaSource, MediaStatus
from app.media import evidence_rules
from app.media import service as media_service
from app.models.generation import (
    GenerationAttempt,
    GenerationCandidate,
    GenerationTask,
)
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.models.spu import ColorVariant, Spu
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def colours(session):
    spu = Spu(
        spu_code=f"EVIDENCE-{uuid.uuid4().hex[:8]}",
        internal_name="evidence query fixture",
        audience="WOMEN",
        base_category="swimwear",
        created_by="test",
    )
    session.add(spu)
    session.flush()
    rows = [
        ColorVariant(
            spu_id=spu.id,
            variant_code=code,
            working_name=name,
            display_name=name,
        )
        for code, name in (("A", "Black"), ("B", "Blue"))
    ]
    session.add_all(rows)
    session.flush()
    return spu, rows


@pytest.fixture
def product(session, colours):
    spu, variants = colours
    row = Product(
        spu=spu.spu_code,
        sku=f"EVIDENCE-{uuid.uuid4().hex[:12]}",
        name="evidence query fixture",
        category="swimwear",
        audience="WOMEN",
        spu_id=spu.id,
        color_variant_id=variants[0].id,
    )
    session.add(row)
    session.flush()
    return row


def _provenance(session, product):
    task = GenerationTask(
        product_id=product.id,
        mode="virtual_try_on",
        provider="mock",
        idempotency_key=f"evidence-{uuid.uuid4().hex}",
    )
    session.add(task)
    session.flush()
    attempt = GenerationAttempt(task_id=task.id, provider="mock")
    session.add(attempt)
    session.flush()
    candidate = GenerationCandidate(task_id=task.id, attempt_id=attempt.id)
    session.add(candidate)
    session.flush()
    return task.id, candidate.id


def _asset(
    session,
    product,
    *,
    source: str,
    status: str = MediaStatus.READY.value,
    generation_task_id=None,
    generation_candidate_id=None,
    color_variant_id=None,
    role=None,
) -> MediaAsset:
    row = MediaAsset(
        product_id=product.id,
        source=source,
        storage_path=f"products/{product.id}/{uuid.uuid4().hex}.jpg",
        mime_type="image/jpeg",
        width=800,
        height=1200,
        bytes=1024,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        status=status,
        generation_task_id=generation_task_id,
        generation_candidate_id=generation_candidate_id,
        color_variant_id=color_variant_id,
        role=role,
    )
    session.add(row)
    session.flush()
    return row


def _reference(session, product, *, imported_url_trusted: bool = False) -> set:
    """参照实现:**不带任何粗筛**,读全量再逐行跑判定。"""
    rows = session.scalars(
        select(MediaAsset).where(MediaAsset.product_id == product.id)
    )
    return {
        r.id
        for r in rows
        if evidence_rules.asset_is_extraction_input(
            r, imported_url_trusted=imported_url_trusted
        )
    }


def _matrix(session, product) -> None:
    """把每一类都摆一行进去。**覆盖矩阵就是这份测试的全部价值。**"""
    for source in MediaSource:
        _asset(session, product, source=source.value)
        _asset(session, product, source=source.value, status=MediaStatus.PENDING.value)
        _asset(
            session, product, source=source.value, status=MediaStatus.QUARANTINED.value
        )
    # 溯源列非空的两种(被人重新上传成 MANUAL_UPLOAD 的那一份正是靠这两列拦的)
    task_id, candidate_id = _provenance(session, product)
    _asset(
        session,
        product,
        source=MediaSource.MANUAL_UPLOAD.value,
        generation_task_id=task_id,
    )
    _asset(
        session,
        product,
        source=MediaSource.MANUAL_UPLOAD.value,
        generation_candidate_id=candidate_id,
    )
    for role in MediaRole:
        _asset(session, product, source=MediaSource.MANUAL_UPLOAD.value, role=role.value)


def test_the_coarse_filter_never_drops_a_row_the_predicate_would_keep(session, product):
    """**本文件的主张。** 粗筛 + 判定 == 全量 + 判定。

    不相等有两种方向,这里一次抓两种:

        实际 ⊂ 参照   粗筛多挡了 -> 本该识别的图静静消失,没有任何地方会报
        实际 ⊃ 参照   判定没跑 -> AI 图进了识别输入,每张一次真实付费调用
    """
    _matrix(session, product)
    session.flush()

    actual = {a.id for a in media_service.evidence_assets_for(session, product.id)}
    expected = _reference(session, product)

    assert actual == expected, (
        f"粗筛多挡了 {sorted(expected - actual)};多放了 {sorted(actual - expected)}"
    )


def test_the_trust_flag_travels_through_the_query(session, product):
    """`imported_url_trusted` 必须一路传到判定,不能在取数层被吃掉。

    被吃掉的表现:接上可信机制那天,外链图仍然一张都进不来,
    而调用方明明传了 True —— 这种"参数传了没用"的坏法不报错。
    """
    _asset(session, product, source=MediaSource.IMPORTED_URL.value)
    session.flush()

    assert not media_service.evidence_assets_for(session, product.id)
    trusted = media_service.evidence_assets_for(
        session, product.id, imported_url_trusted=True
    )
    assert {a.id for a in trusted} == _reference(
        session, product, imported_url_trusted=True
    )


def test_an_ai_image_shadowed_onto_the_product_never_reaches_extraction(
    session, product
):
    """§5.1 的字面场景:生成流水线把候选图影子写成本商品素材。

    `shadow_from_candidate` 写的是 `source=AI_GENERATED` + `status=READY`
    + 两条溯源列。这一行在旧的 `usable_assets()` 眼里是完全合格的素材。
    """
    task_id, candidate_id = _provenance(session, product)
    ai = _asset(
        session,
        product,
        source=MediaSource.AI_GENERATED.value,
        generation_task_id=task_id,
        generation_candidate_id=candidate_id,
    )
    ok = _asset(session, product, source=MediaSource.MANUAL_UPLOAD.value)
    session.flush()

    got = {a.id for a in media_service.evidence_assets_for(session, product.id)}
    assert ai.id not in got, "AI 图进了识别输入 —— 每张一次真实付费调用"
    assert ok.id in got, "人工上传的图被一起挡掉了"


def test_the_scope_filter_keeps_shared_assets_for_every_colour(
    session, product, colours
):
    """按颜色取证据时,共享素材对每一个颜色都算数。

    漏掉共享的那一半,表现是"这个颜色的图明明传了正面照,门禁还说缺" ——
    而共享素材(`color_variant_id IS NULL`)恰恰是绝大多数存量行的样子。
    """
    _spu, variants = colours
    variant = variants[0].id
    other = variants[1].id
    shared = _asset(session, product, source=MediaSource.MANUAL_UPLOAD.value)
    mine = _asset(
        session,
        product,
        source=MediaSource.MANUAL_UPLOAD.value,
        color_variant_id=variant,
    )
    theirs = _asset(
        session,
        product,
        source=MediaSource.MANUAL_UPLOAD.value,
        color_variant_id=other,
    )
    session.flush()

    got = {
        a.id
        for a in media_service.evidence_assets_for(
            session, product.id, scope=variant, scope_is_explicit=True
        )
    }
    assert got == {shared.id, mine.id}, f"按颜色取证据取错了:{sorted(got)}"
    assert theirs.id not in got

    only_shared = {
        a.id
        for a in media_service.evidence_assets_for(
            session, product.id, scope=None, scope_is_explicit=True
        )
    }
    assert only_shared == {shared.id}, (
        "显式传 scope=None 应该只取共享素材 —— "
        "它和「没传 scope」语义相反,这正是 scope_is_explicit 存在的理由"
    )

    everything = {
        a.id for a in media_service.evidence_assets_for(session, product.id)
    }
    assert everything == {shared.id, mine.id, theirs.id}, "不传 scope 应该不限颜色"


def test_the_count_helper_agrees_with_the_unfiltered_query(session, product):
    """`usable_asset_count()` 必须和 `usable_assets()` 数同一批行。

    两者漂移的表现:证据为空时报给运营的那句话里的数字是错的 ——
    「12 条素材全部不能作为识别证据」而实际只有 3 条,人会去查一个不存在的问题。
    """
    _matrix(session, product)
    session.flush()

    assert media_service.usable_asset_count(session, product.id) == len(
        media_service.usable_assets(session, product.id)
    )


def test_extraction_reports_the_two_empty_cases_differently(session, product):
    """「一条素材都没有」和「有素材但都不能当证据」必须是两句话。

    合成一句的后果是运营被指向错误的下一步:告诉他"没有素材"他会去传更多图,
    而传进来的如果还是 AI 图,一张也进不来。
    """
    from app.attributes import service as attr_service
    from app.core.errors import ValidationError

    class _Extractor:
        name = "noop"
        billable = False

        def extract(self, *a, **kw):  # pragma: no cover - 两种情况都该在此之前被拦下
            raise AssertionError("不该跑到抽取器")

    # 一、素材库是空的
    with pytest.raises(ValidationError) as empty:
        attr_service.run_extraction(session, product=product, extractor=_Extractor())
    assert "没有可用于识别的素材" in str(empty.value)

    # 二、有素材,但一条都不能当证据
    _asset(session, product, source=MediaSource.AI_GENERATED.value)
    session.flush()

    with pytest.raises(ValidationError) as blocked:
        attr_service.run_extraction(session, product=product, extractor=_Extractor())
    message = str(blocked.value)
    assert "不能作为识别证据" in message
    assert "1 条" in message, f"条数没报准:{message}"
