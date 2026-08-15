"""独立 AI 能力测试不应偷改正式评分或正式文案。"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.core.enums import EvaluationDepth
from app.listings.contracts import ContentPlanData
from app.listings.copy_generator import CopyDraft


def test_copy_diagnostic_reuses_confirmed_facts_without_saving_a_copy():
    from app.workbench import service

    seen = {}

    class Generator:
        billable = False
        usage_provider = "template"

        def generate(self, **kwargs):
            seen.update(kwargs)
            return CopyDraft(
                title="Navy One Piece Swimsuit",
                bullet_points=("Navy colour", "One piece silhouette", "Beach ready"),
                description="Navy one piece swimsuit for beach days.",
                keywords=("navy", "swimwear"),
                claims=(
                    {
                        "field_name": "primary_color",
                        "value": "NAVY",
                        "text_span": "Navy",
                        "location": "title",
                    },
                    {
                        "field_name": "garment_type",
                        "value": "ONE_PIECE",
                        "text_span": "One Piece Swimsuit",
                        "location": "title",
                    },
                ),
            )

    originals = (
        service.copy_service.content_plan_data,
        service.copy_generator.get_generator,
    )
    service.copy_service.content_plan_data = lambda *_a, **_kw: ContentPlanData(
        spu="SPU-1",
        facts={"primary_color": "NAVY", "garment_type": "ONE_PIECE"},
    )
    service.copy_generator.get_generator = lambda _name=None: Generator()
    try:
        # 这个对象故意没有 add/flush/commit；诊断函数若开始保存正式版本会立刻失败。
        result = service.diagnose_copy(object(), SimpleNamespace(id=uuid4()))
    finally:
        service.copy_service.content_plan_data, service.copy_generator.get_generator = originals

    assert result["copy"]["title"] == "Navy One Piece Swimsuit"
    assert seen["facts"] == {"primary_color": "NAVY", "garment_type": "ONE_PIECE"}


def test_scoring_diagnostic_records_a_diagnostic_attempt_without_mutating_candidate():
    from app.services import evaluation_service as service

    task_id = uuid4()
    task = SimpleNamespace(
        id=task_id,
        product_id=uuid4(),
        current_round=2,
        provider_params={},
    )
    candidate = SimpleNamespace(
        id=uuid4(),
        task_id=task_id,
        round_number=2,
        storage_path="generated/candidate.jpg",
        status="DOWNLOADED",
    )

    class RuleSet:
        evaluator_options = {}
        thresholds = {}

        def payload(self, **kwargs):
            return kwargs

    result = SimpleNamespace(
        evaluator="test-evaluator",
        model_name="test-model",
        depth=EvaluationDepth.FULL,
        duration_ms=18,
        raw={},
        grade=None,
        recommended_action=None,
        grade_reasons=[],
    )

    class Evaluator:
        evaluator_name = "test-evaluator"

        async def evaluate_structured(self, *_args, **_kwargs):
            return result

    captured = {}
    fake_attempt = SimpleNamespace(id=uuid4())
    originals = {
        "load_active_rule_set": service.load_active_rule_set,
        "product_metadata": service.product_metadata,
        "_prompt_context": service._prompt_context,
        "_rule_pack_context": service._rule_pack_context,
        "_reference_asset_paths": service._reference_asset_paths,
        "grade_candidate": service.grade_candidate,
        "_record_attempt": service._record_attempt,
        "_billing": service._billing,
    }
    service.load_active_rule_set = lambda _session: RuleSet()
    service.product_metadata = lambda *_a, **_kw: {}
    service._prompt_context = lambda *_a, **_kw: {}
    service._rule_pack_context = lambda *_a, **_kw: {}
    service._reference_asset_paths = lambda *_a, **_kw: []
    service.grade_candidate = lambda *_a, **_kw: SimpleNamespace(
        grade="A", action="AUTO_APPROVE", reasons=("通过",)
    )

    def record(*_args, **kwargs):
        captured.update(kwargs)
        return fake_attempt

    service._record_attempt = record
    service._billing = lambda _evaluator: None
    session = SimpleNamespace(get=lambda *_a, **_kw: SimpleNamespace(audience=None))
    try:
        returned, attempt = service.diagnose_candidate(
            session, task, candidate, evaluator=Evaluator()
        )
    finally:
        for name, value in originals.items():
            setattr(service, name, value)

    assert returned is result
    assert attempt is fake_attempt
    assert captured["diagnostic"] is True
    assert captured["round_number"] == 2
    assert candidate.status == "DOWNLOADED"
