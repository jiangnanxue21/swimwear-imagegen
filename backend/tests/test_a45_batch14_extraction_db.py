"""A45-batch14:识别链路的真库断言(阶段 3 第一批,PRD v3.1 §13)。

`tests/pure/test_a45_batch14_stage3_extractor.py` 钉的是**判定**:目标外的字段
算不算编造、无值证据归成哪一种形状、上限的边界在哪、请求体里出现什么。
这一批钉的是**落库行为**,每一条都对着一个纯测试够不着的东西:

    证据真的写进去了吗          纯测试只看到 `session.add(AttributeEvidence(...))`,
                                看不到 `missing_reason` 这一列在库里存不存得下
    NOT NULL 真的兜得住吗       `attribute_evidence.raw_value_json` 是 NOT NULL,
                                而无值证据往里写的是一个 dict —— 只有库能回答
    计数真的落到那一行了吗      `fabricated_field_count` 在 flush 之后才有值
    流水真的多出一行了吗        `record_usage` 会写 `provider_usage_records`,
                                而计价发生在写入那一刻(见 spend.record_cost)

## 这些用例一次都没跑过

和 12-4 的 6 条、12-5 的 7 条、12-7 的 11 条、batch13 的 18 条同一个池子:
**已写、未执行**(整改环境无 PostgreSQL)。写出来只是把阶段 3 的第一条验收
从「欠用例」推进到「欠执行」,不要读成「识别跑通了」。

**没有一条会真的调外部模型。** 抽取器是脚本化的假实现 —— 这里要验的是
落库与记账,不是厂商 API 的行为。真模型那一条只能人工连一次,
判据写在仓库根的 `HANDOVER.md`(不在 docs/ 下 —— batch14-11 修过一次指错路)。

跑起来需要:`make up` 之后 `pytest tests/test_a45_batch14_extraction_db.py`,
或在 CI 的 backend job 里(那里有 PostgreSQL + Redis)。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.core.enums import AttributeStatus, MediaStatus
from app.core.errors import ValidationError
from app.extractors.base import FieldMissing, FieldObservation, ImageExtractionResult
from app.models.attribute import AttributeEvidence, ProductAttributeExtraction
from app.models.generation import ProviderUsageRecord
from app.models.media_asset import MediaAsset
from app.models.product import Product
from tests.conftest import requires_db

pytestmark = requires_db


# ---------------------------------------------------------------- 脚本化抽取器


class _Scripted:
    """按脚本产出结果。**不发任何网络请求。**"""

    def __init__(self, results, *, billable=False, name="scripted"):
        self._results = list(results)
        self.name = name
        self.billable = billable
        self.usage_provider = "scripted-vendor"
        self.calls = 0

    def extract(self, *, image, target_fields, enum_defs, options):
        self.calls += 1
        item = self._results.pop(0) if self._results else _reply()
        if isinstance(item, Exception):
            raise item
        return item


def _reply(fields=(), unreadable=(), missing=()):
    return ImageExtractionResult(
        fields=tuple(FieldObservation(name=n, value=v, confidence=c) for n, v, c in fields),
        unreadable_fields=tuple(unreadable),
        missing=tuple(FieldMissing(name=n, reason=r) for n, r in missing),
        model_name="scripted-model",
        prompt_version="scripted-1",
        duration_ms=7,
    )


@pytest.fixture
def product(session):
    row = Product(
        spu="SPU-EXT-001",
        sku=f"SPU-EXT-001-{uuid.uuid4().hex[:8]}",
        name="识别用例商品",
        category="swimwear",
        audience="WOMEN",
    )
    session.add(row)
    session.flush()
    return row


def _asset(session, product, index: int = 0) -> MediaAsset:
    row = MediaAsset(
        product_id=product.id,
        source="MANUAL_UPLOAD",
        storage_path=f"products/{product.id}/{index}.jpg",
        mime_type="image/jpeg",
        width=800,
        height=1200,
        bytes=1024,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        status=MediaStatus.READY.value,
    )
    session.add(row)
    session.flush()
    return row


def _run(session, product, extractor, **kw):
    from app.attributes import service as attr_service

    return attr_service.run_extraction(
        session, product=product, extractor=extractor, **kw
    )


def _evidence(session, extraction_id):
    return list(
        session.scalars(
            select(AttributeEvidence).where(
                AttributeEvidence.extraction_id == extraction_id
            )
        )
    )


# ---------------------------------------------------------------- 无值证据落库


def test_a_missing_reason_row_survives_a_round_trip(session, product):
    """§4.5:落 evidence 不落 value。**这一条只有真库能回答。**

    `attribute_evidence.raw_value_json` 是 NOT NULL,而无值证据往里写的是
    `{"missing_reason": ...}` 一个 dict。纯测试看得到这行代码,看不到
    JSONB 列收不收、`missing_reason` 那 32 个字符够不够。
    """
    _asset(session, product)
    row = _run(
        session,
        product,
        _Scripted([_reply(missing=[("primary_color", "NOT_VISUALLY_DETERMINABLE")])]),
        fields=("primary_color",),
    )
    session.flush()
    session.expire_all()

    rows = _evidence(session, row.id)
    assert len(rows) == 1
    assert rows[0].missing_reason == "NOT_VISUALLY_DETERMINABLE"
    assert rows[0].normalized_value is None
    assert rows[0].raw_value_json == {"missing_reason": "NOT_VISUALLY_DETERMINABLE"}


def test_a_missing_row_never_produces_an_attribute_value(session, product):
    """无值证据不许被合并成一个值。

    合并层靠"归一失败不参与投票"兜着,而那是**另一个分支**的副作用 ——
    改那条分支的人不会想到这里。这条用例让那个依赖变得可见。
    """
    from app.attributes import service as attr_service

    _asset(session, product)
    row = _run(
        session,
        product,
        _Scripted([_reply(missing=[("primary_color", "INSUFFICIENT_EVIDENCE")])]),
        fields=("primary_color",),
    )
    written = attr_service.apply_evidence(
        session, product=product, extraction=row, actor="tester"
    )
    assert written == [], "模型说看不清,却写出了一个属性值"


def test_uncalibrated_valid_evidence_lands_as_a_candidate(session, product):
    """未校准不自动确认，但模型给出的合法值必须进入人工确认队列。"""
    from app.attributes import service as attr_service

    _asset(session, product, 0)
    _asset(session, product, 1)
    row = _run(
        session,
        product,
        _Scripted([
            _reply(fields=[("back_style", "TIE_BACK", 0.95)]),
            _reply(fields=[("back_style", "TIE_BACK", 0.95)]),
        ]),
        fields=("back_style",),
    )

    written = attr_service.apply_evidence(
        session, product=product, extraction=row, actor="tester"
    )

    assert len(written) == 1
    assert written[0].normalized_value == "TIE_BACK"
    assert written[0].system_confidence is None
    assert written[0].status == AttributeStatus.CANDIDATE.value


def test_the_old_unreadable_channel_lands_in_the_same_column(session, product):
    """v1 抽取器(只有 `unreadable_fields`)写出来的行,与 v2 形状一致。

    形状分叉的表现是界面上某一类缺失不显示 —— 不报错,只是不在。
    """
    _asset(session, product)
    row = _run(
        session,
        product,
        _Scripted([_reply(unreadable=["primary_color"])]),
        fields=("primary_color",),
    )
    session.flush()
    rows = _evidence(session, row.id)
    assert [r.missing_reason for r in rows] == ["INSUFFICIENT_EVIDENCE"]


# ---------------------------------------------------------------- 编造计数落库


def test_the_fabrication_count_lands_on_the_extraction_row(session, product):
    """§6.3:目标清单外的字段过滤并计数。计数要真的落到那一行上。

    纯测试验的是 `plan.fabricated` 这个数算得对;这里验的是它进了库 ——
    §14.4 的指标是 `SUM(fabricated_field_count)`,算得对但没落库等于没有。
    """
    _asset(session, product)
    row = _run(
        session,
        product,
        _Scripted(
            [
                _reply(
                    fields=[
                        ("primary_color", "NAVY", 0.9),
                        ("pattern_type", "STRIPE", 0.9),
                        ("made_up_field", "X", 0.9),
                    ]
                )
            ]
        ),
        fields=("primary_color",),
    )
    session.flush()
    session.expire_all()

    stored = session.get(ProductAttributeExtraction, row.id)
    assert stored.fabricated_field_count == 2
    assert [e.field_name for e in _evidence(session, row.id)] == ["primary_color"]


def test_the_schema_version_records_which_contract_produced_these_rows(session, product):
    """校准分箱按(字段 × 模型 × Prompt)查,而"当时的输出契约长什么样"靠它回答。

    留空的话,v1 与 v2 的证据在库里不可分辨 —— 而它们对 `missing` 的处理不同。
    """
    _asset(session, product)
    row = _run(session, product, _Scripted([_reply()]), fields=("primary_color",))
    session.flush()
    assert session.get(ProductAttributeExtraction, row.id).schema_version == "2"


# ---------------------------------------------------------------- 花费台账


def test_a_paid_run_writes_one_usage_row_per_image(session, product):
    """一次付费调用 = 一条流水。**失败的也算。**

    厂商在收到请求那一刻就开始计费,超时和解析失败都不退钱 ——
    而那正是最容易被漏掉的一类开销(a25 之前评分调用完全不进流水表)。
    """
    _asset(session, product, 0)
    _asset(session, product, 1)
    before = session.scalar(select(func.count(ProviderUsageRecord.id))) or 0

    _run(
        session,
        product,
        _Scripted(
            [_reply(fields=[("primary_color", "NAVY", 0.9)]), RuntimeError("模型炸了")],
            billable=True,
        ),
        fields=("primary_color",),
    )
    session.flush()

    rows = list(
        session.scalars(
            select(ProviderUsageRecord)
            .where(ProviderUsageRecord.operation == "attribute_extract")
            .order_by(ProviderUsageRecord.created_at)
        )
    )
    assert (session.scalar(select(func.count(ProviderUsageRecord.id))) or 0) == before + 2
    assert sorted(r.succeeded for r in rows) == [False, True]
    assert {r.provider for r in rows} == {"scripted-vendor"}


def test_two_extraction_calls_do_not_collide_on_the_billing_key(session, product):
    """识别流水不传 `billing_key`,所以第二张图不会撞 `uq_provider_usage_records_billing_key`。

    传了的话第二次 flush 直接 IntegrityError —— 而那是一笔真实发生的消费。
    这条用例存在的理由就是那个唯一约束只有库里才有。
    """
    for index in range(3):
        _asset(session, product, index)
    _run(
        session,
        product,
        _Scripted([_reply() for _ in range(3)], billable=True),
        fields=("primary_color",),
    )
    session.flush()  # 撞约束的话这里炸


def test_a_mock_run_writes_no_usage_rows(session, product):
    """Mock 不该在花费台账里留下行 —— 那会让"这个月花了多少"变成假的。"""
    _asset(session, product)
    before = session.scalar(select(func.count(ProviderUsageRecord.id))) or 0
    _run(session, product, _Scripted([_reply()], billable=False), fields=("primary_color",))
    session.flush()
    assert (session.scalar(select(func.count(ProviderUsageRecord.id))) or 0) == before


# ---------------------------------------------------------------- 付费上限


def test_the_budget_refuses_before_spending_anything(session, product):
    """超限时**一次调用都不许发生**。

    纯测试按语句顺序钉住了"检查排在循环之前",这里验的是行为:
    `extractor.calls` 必须是 0 —— 拒绝之后账单上不该多出任何东西。
    """
    for index in range(20):
        _asset(session, product, index)
    extractor = _Scripted([_reply() for _ in range(20)], billable=True)

    with pytest.raises(ValidationError) as excinfo:
        _run(session, product, extractor, fields=("primary_color",))
    assert extractor.calls == 0
    assert "分批" in str(excinfo.value)


def test_batching_by_media_ids_gets_through(session, product):
    """拒绝之后运营要有路可走:按素材 id 分批提交。

    只说"太多了"的话,唯一能做的动作是删素材 —— 而素材本身没有问题。
    """
    assets = [_asset(session, product, i) for i in range(20)]
    extractor = _Scripted([_reply() for _ in range(5)], billable=True)
    row = _run(
        session,
        product,
        extractor,
        fields=("primary_color",),
        only_media_ids=[a.id for a in assets[:5]],
    )
    assert extractor.calls == 5
    assert row.image_count == 5


# ---------------------------------------------------------------- 迁移形状


def test_the_new_columns_exist_with_the_expected_shape(session):
    """迁移 0036 建出来的两列。

    夹具用 `alembic upgrade head` 建库(不是 `create_all()`),所以这条
    同时是"迁移真的能升上去"的证据 —— 而 0036 是 0035 之后第一条迁移,
    那条链上的回退路径至今没有人跑过。
    """
    from sqlalchemy import inspect

    inspector = inspect(session.get_bind())
    evidence_columns = {c["name"]: c for c in inspector.get_columns("attribute_evidence")}
    assert evidence_columns["missing_reason"]["nullable"] is True

    extraction_columns = {
        c["name"]: c for c in inspector.get_columns("product_attribute_extractions")
    }
    assert extraction_columns["fabricated_field_count"]["nullable"] is False
