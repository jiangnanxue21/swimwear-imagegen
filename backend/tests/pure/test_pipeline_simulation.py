"""编排流水线仿真。

用真实的 Mock Provider + 真实的状态机 + 真实的存储,配一个内存版任务对象,
把 app/tasks/generation_tasks.py 的编排顺序完整走一遍。

这不能替代真库集成测试(tests/test_api_generation.py),但它能在没有
PostgreSQL/Celery 的环境里守住最容易写错的东西:状态转移顺序、每轮 seed、
候选落盘、取消时机。

阶段 6 起把尾段也纳进来:评分 -> 分档 -> 决策 -> 多尺寸输出 -> COMPLETED。
评分器、分档规则、决策引擎、渲染都是**真的**,只有任务对象是内存版的 ——
因此"自动通过的任务最终停在哪个状态"这件事在这里就能拿到答案,
不必等一个有数据库的环境。
"""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.core.enums import CandidateStatus, Grade, OutputPurpose, TaskStatus
from app.core.hashing import hash_bytes
from app.evaluators.decision import CandidateVerdict, RoundOutcome, decide_round
from app.evaluators.mock import MockImageQualityEvaluator
from app.evaluators.rules import DEFAULT_THRESHOLDS, grade_candidate
from app.providers.base import GenerationMode, GenerationRequest
from app.providers.errors import ProviderError
from app.providers.mock import MockImageGenerationProvider
from app.services.formatting_service import plan_outputs, render_outputs
from app.services.image_probe import probe_image
from app.services.storage import LocalObjectStorage
from app.workflows import state_machine as sm
from app.workflows.idempotency import next_seed
from tests.pure._helpers import expect_raises


@dataclass
class FakeTask:
    """内存版任务。字段与 GenerationTask 同名,便于对照编排代码。"""

    status: str = TaskStatus.CREATED.value
    current_round: int = 0
    max_rounds: int = 3
    candidate_count: int = 4
    base_seed: int = 20240101
    cancel_requested: bool = False
    error_code: str | None = None
    history: list[str] = field(default_factory=list)

    def move(self, target: TaskStatus) -> None:
        sm.assert_can(self.status, target)
        self.status = target.value
        self.history.append(target.value)


@dataclass
class FakeAttempt:
    round_number: int
    attempt_number: int
    seed: int
    status: str = "SUBMITTED"
    candidate_count: int = 0
    error_code: str | None = None


class Pipeline:
    """把编排逻辑抽出来跑,顺序与 generation_tasks._run 保持一致。"""

    def __init__(self, storage: LocalObjectStorage, provider=None, options: dict | None = None):
        self.storage = storage
        self.provider = provider or MockImageGenerationProvider()
        self.options = options or {}
        self.attempts: list[FakeAttempt] = []
        self.candidates: list[dict] = []
        self.outputs: list[dict] = []

    def _check_cancelled(self, task: FakeTask) -> None:
        if task.cancel_requested:
            task.move(TaskStatus.CANCELLED)
            raise _Cancelled

    def run_round(self, task: FakeTask) -> FakeTask:
        if task.status == TaskStatus.CREATED.value:
            task.move(TaskStatus.QUEUED)
        self._check_cancelled(task)

        # QUEUED 与 REGENERATING 都合法地进入 PREPROCESSING
        task.move(TaskStatus.PREPROCESSING)
        task.current_round += 1
        seed = next_seed(task.base_seed, task.current_round)
        request = GenerationRequest(
            mode=GenerationMode.VIRTUAL_TRY_ON,
            garment_image_url="http://local/garment.jpg",
            model_image_url="http://local/model.jpg",
            candidate_count=task.candidate_count,
            seed=seed,
            options=self.options,
        )

        self._check_cancelled(task)
        attempt = FakeAttempt(
            round_number=task.current_round,
            attempt_number=(
                len([a for a in self.attempts if a.round_number == task.current_round]) + 1
            ),
            seed=seed,
        )
        self.attempts.append(attempt)

        task.move(TaskStatus.SUBMITTING)
        try:
            external_id = asyncio.run(self.provider.submit(request))
        except ProviderError as exc:
            attempt.status, attempt.error_code = "FAILED", str(exc.code)
            task.move(TaskStatus.FAILED)
            task.error_code = str(exc.code)
            raise

        task.move(TaskStatus.PROVIDER_RUNNING)
        self._check_cancelled(task)
        asyncio.run(self.provider.get_status(external_id))  # 超时前先查状态

        try:
            results = asyncio.run(self.provider.fetch_results(external_id))
        except ProviderError as exc:
            attempt.status, attempt.error_code = "FAILED", str(exc.code)
            task.move(TaskStatus.FAILED)
            task.error_code = str(exc.code)
            raise

        task.move(TaskStatus.DOWNLOADING)
        saved = 0
        for index, item in enumerate(results):
            payload = item.metadata["inline_bytes"]
            info = probe_image(payload)
            stored = self.storage.save(
                payload,
                file_hash=hash_bytes(payload),
                extension=info.extension,
                prefix="candidates",
            )
            self.candidates.append({
                "round": task.current_round, "index": index,
                "path": stored.storage_path, "seed": item.metadata["seed"],
                "status": CandidateStatus.DOWNLOADED.value,
            })
            saved += 1
        attempt.status, attempt.candidate_count = "SUCCEEDED", saved

        if saved == 0:
            task.move(TaskStatus.REGENERATING)
            return task

        task.move(TaskStatus.SCORING)
        return task

    # ---------- 阶段 4 + 6 的尾段 ----------

    def evaluate_round(self, task: FakeTask, *, outcome: str = "auto") -> list[CandidateVerdict]:
        """用真实的 Mock 评分器 + 真实的分档规则评这一轮。"""
        evaluator = MockImageQualityEvaluator()
        verdicts: list[CandidateVerdict] = []
        for candidate in [c for c in self.candidates if c["round"] == task.current_round]:
            payload = {
                "mock_evaluator": {"outcome": outcome, "round_number": task.current_round}
            }
            result = asyncio.run(
                evaluator.evaluate_structured(
                    ["garment.jpg"], candidate["path"], {}, payload
                )
            )
            decision = grade_candidate(result, DEFAULT_THRESHOLDS)
            candidate["grade"] = decision.grade.value
            candidate["status"] = CandidateStatus.SCORED.value
            verdicts.append(
                CandidateVerdict(
                    key=f"{candidate['round']}-{candidate['index']}",
                    grade=decision.grade,
                    overall_score=result.overall_score,
                    hard_fail=result.hard_fail,
                    hard_fail_codes=tuple(result.hard_fail_codes),
                )
            )
        return verdicts

    def finish_round(self, task: FakeTask, *, outcome: str = "auto") -> dict:
        """评分 -> 决策 -> (通过则)多尺寸输出 -> 终态。

        顺序与 generation_tasks._apply_decision + _format_outputs 保持一致。
        """
        verdicts = self.evaluate_round(task, outcome=outcome)
        decision = decide_round(
            verdicts,
            round_number=task.current_round,
            max_rounds=task.max_rounds,
            thresholds=DEFAULT_THRESHOLDS,
            task_key="sim",
        )

        if decision.outcome is RoundOutcome.AUTO_APPROVED:
            task.move(TaskStatus.AUTO_APPROVED)
            return self.format_outputs(task, decision.selected_key)

        if decision.outcome is RoundOutcome.MANUAL_REVIEW:
            task.move(TaskStatus.MANUAL_REVIEW)
            return {"outcome": decision.outcome.value, "outputs": 0}

        task.move(TaskStatus.REGENERATING)
        return {"outcome": decision.outcome.value, "outputs": 0}

    def fail_formatting(self, task: FakeTask) -> None:
        """模拟多尺寸转换那步崩掉:已通过 -> FORMATTING -> FAILED。"""
        task.move(TaskStatus.FORMATTING)
        task.error_code = "INTERNAL_ERROR"
        task.move(TaskStatus.FAILED)

    def retry_formatting(self, task: FakeTask) -> dict:
        """重试续跑,顺序与 generation_service.retry_task + _run 的续跑分支一致。

        关键点:**不新增 attempt** —— 续跑的意义就在于不再调用 Provider。
        """
        selected = next(
            (c for c in self.candidates if c["status"] == CandidateStatus.SELECTED.value),
            None,
        )
        assert selected is not None, "没有选中的候选图就不该走续跑"
        task.move(TaskStatus.FORMATTING)
        return self.format_outputs(
            task, f"{selected['round']}-{selected['index']}", already_formatting=True
        )

    def format_outputs(
        self, task: FakeTask, selected_key: str | None, *, already_formatting: bool = False
    ) -> dict:
        """阶段 6:通过之后产出网站要用的多尺寸图,然后才算 COMPLETED。"""
        chosen = next(
            (
                c
                for c in self.candidates
                if f"{c['round']}-{c['index']}" == selected_key
            ),
            None,
        )
        if chosen is None:
            return {"outcome": "AUTO_APPROVED", "outputs": 0}

        chosen["status"] = CandidateStatus.SELECTED.value
        if not already_formatting:
            task.move(TaskStatus.FORMATTING)

        source = self.storage.read(chosen["path"])
        info = probe_image(source)
        rendered = render_outputs(source, plan_outputs(info.width, info.height))
        for item in rendered:
            stored = self.storage.save(
                item.data,
                file_hash=hash_bytes(item.data),
                extension=item.extension,
                prefix="outputs",
            )
            self.outputs.append(
                {
                    "purpose": item.plan.purpose.value,
                    "path": stored.storage_path,
                    "width": item.width,
                    "height": item.height,
                    "size": len(item.data),
                }
            )

        task.move(TaskStatus.COMPLETED)
        return {"outcome": "AUTO_APPROVED", "outputs": len(rendered)}


class _Cancelled(Exception):
    pass


def _storage(tmp: str) -> LocalObjectStorage:
    return LocalObjectStorage(tmp, "http://localhost:8000")


# ---------- 正常路径 ----------

def test_pipeline_reaches_scoring_and_stops_there():
    """阶段 3 的正确终点是 SCORING —— 评分属于阶段 4,不该在这里发生。"""
    with tempfile.TemporaryDirectory() as tmp:
        task = Pipeline(_storage(tmp)).run_round(FakeTask())
        assert task.status == TaskStatus.SCORING.value


def test_state_history_follows_the_designed_order():
    with tempfile.TemporaryDirectory() as tmp:
        task = FakeTask()
        Pipeline(_storage(tmp)).run_round(task)
        assert task.history == [
            "QUEUED", "PREPROCESSING", "SUBMITTING",
            "PROVIDER_RUNNING", "DOWNLOADING", "SCORING",
        ]


def test_one_round_produces_the_requested_candidate_count():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        pipeline.run_round(FakeTask(candidate_count=4))
        assert len(pipeline.candidates) == 4
        assert pipeline.attempts[0].candidate_count == 4


def test_every_provider_call_creates_exactly_one_attempt():
    """需求第九章:每次 Provider 调用生成一条 GenerationAttempt。"""
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        pipeline.run_round(task)
        assert len(pipeline.attempts) == 1
        task.status = TaskStatus.SCORING.value
        task.move(TaskStatus.REGENERATING)
        pipeline.run_round(task)
        assert len(pipeline.attempts) == 2


def test_candidates_are_actually_written_to_storage():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        pipeline.run_round(FakeTask())
        files = [p for p in Path(tmp).rglob("*") if p.is_file()]
        assert len(files) == 4
        for candidate in pipeline.candidates:
            assert (Path(tmp) / candidate["path"]).exists()


def test_candidates_land_under_candidates_prefix():
    """候选图与上传素材必须分开命名空间,便于生命周期管理。"""
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        pipeline.run_round(FakeTask())
        assert all(c["path"].startswith("candidates/") for c in pipeline.candidates)


# ---------- 多轮与 seed ----------

def test_each_round_uses_a_new_seed():
    """需求第九章:每轮使用新的 seed。"""
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        for _ in range(3):
            pipeline.run_round(task)
            if task.status == TaskStatus.SCORING.value:
                task.move(TaskStatus.REGENERATING)
        seeds = [a.seed for a in pipeline.attempts]
        assert len(set(seeds)) == 3, f"三轮 seed 应各不相同: {seeds}"


def test_rounds_produce_visually_different_candidates():
    """换了 seed 却出一样的图,等于白重生。"""
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        pipeline.run_round(task)
        task.move(TaskStatus.REGENERATING)
        pipeline.run_round(task)
        round1 = {c["path"] for c in pipeline.candidates if c["round"] == 1}
        round2 = {c["path"] for c in pipeline.candidates if c["round"] == 2}
        assert not (round1 & round2), "两轮候选图内容重复"


def test_round_counter_increments():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        pipeline.run_round(task)
        assert task.current_round == 1
        task.move(TaskStatus.REGENERATING)
        pipeline.run_round(task)
        assert task.current_round == 2


def test_same_task_replayed_from_scratch_is_reproducible():
    """同 base_seed 重跑,应当产出完全相同的候选图 —— 排障时能复现。"""
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        a = Pipeline(_storage(tmp_a))
        b = Pipeline(_storage(tmp_b))
        a.run_round(FakeTask(base_seed=555))
        b.run_round(FakeTask(base_seed=555))
        assert [c["path"] for c in a.candidates] == [c["path"] for c in b.candidates]


# ---------- 失败分支 ----------

def test_provider_failure_marks_task_failed_with_code():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp), options={"mock_outcome": "fail"})
        task = FakeTask()
        expect_raises(ProviderError, pipeline.run_round, task)
        assert task.status == TaskStatus.FAILED.value
        assert task.error_code == "GENERATION_FAILED"
        assert pipeline.attempts[0].status == "FAILED"


def test_timeout_is_recorded_with_its_own_code():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp), options={"mock_outcome": "timeout"})
        task = FakeTask()
        expect_raises(ProviderError, pipeline.run_round, task)
        assert task.error_code == "NETWORK_TIMEOUT"


def test_rate_limit_fails_at_submit_without_creating_candidates():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp), options={"mock_outcome": "rate_limit"})
        expect_raises(ProviderError, pipeline.run_round, FakeTask())
        assert pipeline.candidates == []


def test_empty_result_goes_to_regenerating_not_failed():
    """提交成功但没返回图 —— 应当重生,而不是判定任务失败。"""
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp), options={"mock_outcome": "empty"})
        task = FakeTask()
        pipeline.run_round(task)
        assert task.status == TaskStatus.REGENERATING.value


def test_failed_task_can_be_requeued():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp), options={"mock_outcome": "fail"})
        task = FakeTask()
        expect_raises(ProviderError, pipeline.run_round, task)
        assert sm.can_retry(task.status)
        task.move(TaskStatus.QUEUED)


# ---------- 取消 ----------

def test_cancel_before_submit_produces_no_provider_call():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask(cancel_requested=True)
        expect_raises(_Cancelled, pipeline.run_round, task)
        assert task.status == TaskStatus.CANCELLED.value
        assert pipeline.attempts == []


def test_cancelled_task_is_terminal():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask(cancel_requested=True)
        expect_raises(_Cancelled, pipeline.run_round, task)
        assert sm.is_terminal(task.status)
        assert not sm.can_retry(task.status)


def test_max_rounds_is_carried_on_the_task():
    """轮次上限的执行属于阶段 4,阶段 3 只要保证字段被正确带上。"""
    with tempfile.TemporaryDirectory() as tmp:
        task = FakeTask(max_rounds=3)
        Pipeline(_storage(tmp)).run_round(task)
        assert task.max_rounds == 3
        assert task.current_round < task.max_rounds


# ---------- 阶段 6:通过之后走到哪 ----------

def test_approved_task_ends_at_completed_not_auto_approved():
    """阶段 6 起 AUTO_APPROVED 不再是终点 —— 还要出图,出完才算完成。

    这条断言存在的意义:阶段 5 之前集成测试里写的是
    `status == "AUTO_APPROVED"`,阶段 6 接上多尺寸输出后那句话就过期了。
    在这里先钉死行为,再去改那边的断言,才不是"改成我以为的样子"。
    """
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        pipeline.run_round(task)
        result = pipeline.finish_round(task, outcome="a")

        assert result["outcome"] == "AUTO_APPROVED"
        assert task.status == TaskStatus.COMPLETED.value


def test_completed_history_passes_through_formatting():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        pipeline.run_round(task)
        pipeline.finish_round(task, outcome="a")

        assert task.history == [
            "QUEUED", "PREPROCESSING", "SUBMITTING", "PROVIDER_RUNNING",
            "DOWNLOADING", "SCORING", "AUTO_APPROVED", "FORMATTING", "COMPLETED",
        ]


def test_five_outputs_are_produced_for_the_selected_candidate():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        pipeline.run_round(task)
        pipeline.finish_round(task, outcome="a")

        assert len(pipeline.outputs) == len(OutputPurpose)
        assert {o["purpose"] for o in pipeline.outputs} == {p.value for p in OutputPurpose}


def test_only_one_candidate_is_selected():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        pipeline.run_round(task)
        pipeline.finish_round(task, outcome="a")

        selected = [c for c in pipeline.candidates if c["status"] == CandidateStatus.SELECTED.value]
        assert len(selected) == 1


def test_outputs_land_under_their_own_prefix():
    """成品图与候选图分开放,便于运维单独同步到 CDN。"""
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        pipeline.run_round(task)
        pipeline.finish_round(task, outcome="a")

        assert all(o["path"].startswith("outputs/") for o in pipeline.outputs)
        assert all(c["path"].startswith("candidates/") for c in pipeline.candidates)


def test_square_output_is_square_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        pipeline.run_round(task)
        pipeline.finish_round(task, outcome="a")

        square = next(o for o in pipeline.outputs if o["purpose"] == OutputPurpose.SQUARE.value)
        assert square["width"] == square["height"]


def test_low_score_round_regenerates_and_produces_no_outputs():
    """D 档不出图 —— 没通过的图绝不能流到网站上。"""
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask(max_rounds=3)
        pipeline.run_round(task)
        result = pipeline.finish_round(task, outcome="stuck")

        assert task.status == TaskStatus.REGENERATING.value
        assert result["outputs"] == 0
        assert pipeline.outputs == []


def test_exhausted_rounds_reach_manual_review_without_outputs():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask(max_rounds=2)
        for _ in range(2):
            pipeline.run_round(task)
            outcome = pipeline.finish_round(task, outcome="stuck")

        assert task.status == TaskStatus.MANUAL_REVIEW.value
        assert outcome["outputs"] == 0
        assert pipeline.outputs == []


def test_regeneration_then_success_still_completes():
    """前两轮不达标、第三轮通过 —— 全流程仍要走到 COMPLETED 并出图。"""
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask(max_rounds=3)
        for _ in range(3):
            pipeline.run_round(task)
            result = pipeline.finish_round(task, outcome="improving")

        assert task.current_round == 3
        assert task.status == TaskStatus.COMPLETED.value
        assert result["outputs"] == len(OutputPurpose)


def test_hard_fail_never_produces_outputs():
    """硬错误候选不论总分多高都不该出图。"""
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask(max_rounds=1)
        pipeline.run_round(task)
        pipeline.finish_round(task, outcome="hard_fail")

        assert task.status == TaskStatus.MANUAL_REVIEW.value
        assert pipeline.outputs == []


def test_every_candidate_gets_a_grade():
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask(candidate_count=4)
        pipeline.run_round(task)
        pipeline.evaluate_round(task, outcome="b")

        grades = [c.get("grade") for c in pipeline.candidates]
        assert len(grades) == 4
        assert all(g in {g2.value for g2 in Grade} for g in grades)


def test_retry_after_formatting_failure_does_not_call_the_provider_again():
    """出图崩了之后重试,不该重新生成 —— 那要再花一次钱,而且换 seed 后
    拿到的根本不是当初通过的那张图。"""
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        pipeline.run_round(task)
        verdicts = pipeline.evaluate_round(task, outcome="a")
        best = max(verdicts, key=lambda v: v.overall_score)
        # 手工走"通过 -> 选中 -> 出图崩了"
        task.move(TaskStatus.AUTO_APPROVED)
        for candidate in pipeline.candidates:
            if f"{candidate['round']}-{candidate['index']}" == best.key:
                candidate["status"] = CandidateStatus.SELECTED.value
        pipeline.fail_formatting(task)

        assert task.status == TaskStatus.FAILED.value
        attempts_before = len(pipeline.attempts)

        result = pipeline.retry_formatting(task)

        assert task.status == TaskStatus.COMPLETED.value
        assert len(pipeline.attempts) == attempts_before, "续跑不该新增 Provider 调用"
        assert result["outputs"] == len(OutputPurpose)


def test_resume_keeps_the_originally_approved_candidate():
    """续跑用的必须还是当初被选中的那张,不是重新挑一张。"""
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(_storage(tmp))
        task = FakeTask()
        pipeline.run_round(task)
        pipeline.evaluate_round(task, outcome="a")
        task.move(TaskStatus.AUTO_APPROVED)
        pipeline.candidates[2]["status"] = CandidateStatus.SELECTED.value
        chosen_path = pipeline.candidates[2]["path"]
        pipeline.fail_formatting(task)

        pipeline.retry_formatting(task)

        selected = [
            c for c in pipeline.candidates if c["status"] == CandidateStatus.SELECTED.value
        ]
        assert len(selected) == 1
        assert selected[0]["path"] == chosen_path
