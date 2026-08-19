"""提示词统计的 PostgreSQL 与 API 接缝。

纯测试能钉住计数策略,钉不住 JSONB ``->>``、NULL 分组、两份提示词同版本号
和最终 HTTP 形状。这一层刻意穿过真实 PostgreSQL 与 prompts API。
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.clock import utc_now
from app.core.enums import EvaluationOutcome
from app.models.evaluation import EvaluationAttempt
from app.models.generation import GenerationTask
from app.models.product import Product
from app.services import prompt_service
from app.services.prompt_rules import VISION_SYSTEM_PROMPT, VISION_SYSTEM_PROMPT_MEN
from tests.conftest import requires_db

pytestmark = requires_db


def _task(session) -> GenerationTask:
    serial = uuid4().hex
    product = Product(spu=f"PROMPT-{serial[:8]}", sku=f"PROMPT-{serial}", name="统计接缝")
    session.add(product)
    session.flush()
    task = GenerationTask(
        product_id=product.id,
        mode="PRODUCT_TO_MODEL",
        provider="mock",
        idempotency_key=f"prompt-stats:{serial}",
        prompt_version="v1",
    )
    session.add(task)
    session.flush()
    return task


def _attempt(
    session,
    task: GenerationTask,
    *,
    key: str | None,
    version: str | None,
    outcome: EvaluationOutcome,
    age: timedelta = timedelta(),
    round_level: bool = False,
    diagnostic: bool = False,
) -> None:
    session.add(
        EvaluationAttempt(
            task_id=task.id,
            round_number=1,
            outcome=outcome.value,
            evaluator="mock",
            depth="FULL",
            prompt_key=key,
            prompt_version=version,
            created_at=utc_now() - age,
            meta={"round_level": round_level, "diagnostic": diagnostic},
        )
    )


def test_prompt_stats_cross_jsonb_grouping_versions_keys_and_api(admin_client, session):
    task = _task(session)
    prompt_service.save_version(session, VISION_SYSTEM_PROMPT, "women v1")
    prompt_service.save_version(session, VISION_SYSTEM_PROMPT, "women v2")
    prompt_service.save_version(session, VISION_SYSTEM_PROMPT_MEN, "men v1")

    # 女装 v1:窗口内成功 1、解析失败 1、诊断 1;窗口外成功 1 只进逐版统计。
    _attempt(
        session, task, key=VISION_SYSTEM_PROMPT, version="1",
        outcome=EvaluationOutcome.SUCCEEDED, diagnostic=True,
    )
    _attempt(
        session, task, key=VISION_SYSTEM_PROMPT, version="1",
        outcome=EvaluationOutcome.PARSE_FAILED,
    )
    _attempt(
        session, task, key=VISION_SYSTEM_PROMPT, version="1",
        outcome=EvaluationOutcome.SUCCEEDED, age=timedelta(days=8),
    )
    # 整轮汇总不对应一次模型调用,不能进分母。
    _attempt(
        session, task, key=VISION_SYSTEM_PROMPT, version="1",
        outcome=EvaluationOutcome.PROVIDER_ERROR, round_level=True,
    )
    _attempt(
        session, task, key=VISION_SYSTEM_PROMPT, version="2",
        outcome=EvaluationOutcome.PROVIDER_ERROR,
    )

    # 男装也有 v1,必须与女装 v1 分桶。
    for _ in range(2):
        _attempt(
            session, task, key=VISION_SYSTEM_PROMPT_MEN, version="1",
            outcome=EvaluationOutcome.SUCCEEDED,
        )
    # 迁移前/降级路径的 NULL 归属单报,不摊给女装。
    _attempt(
        session, task, key=None, version=None,
        outcome=EvaluationOutcome.UNAVAILABLE,
    )
    session.flush()

    listed = admin_client.get("/api/prompts")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    by_key = {item["key"]: item for item in payload["surfaces"]}
    assert by_key[VISION_SYSTEM_PROMPT]["stats"] == {
        "calls": 3,
        "failures": 2,
        "parse_failed": 1,
        "provider_error": 1,
        "unavailable": 0,
        "diagnostic": 1,
        "parse_failure_rate": 0.3333,
        "failure_rate": 0.6667,
    }
    assert by_key[VISION_SYSTEM_PROMPT_MEN]["stats"]["calls"] == 2
    assert by_key[VISION_SYSTEM_PROMPT_MEN]["stats"]["failures"] == 0
    assert payload["stats_unattributed"] == 1

    detail = admin_client.get(f"/api/prompts/{VISION_SYSTEM_PROMPT}")
    assert detail.status_code == 200, detail.text
    versions = {item["version"]: item["stats"] for item in detail.json()["versions"]}
    assert versions[1]["calls"] == 3  # 包含窗口外那次,排除整轮汇总
    assert versions[1]["failures"] == 1
    assert versions[1]["diagnostic"] == 1
    assert versions[2]["calls"] == 1
    assert versions[2]["provider_error"] == 1


def test_recent_window_query_can_use_the_dedicated_index(session):
    """关闭顺序扫描后,查询计划必须能选择与 WHERE 同序的两列索引。"""
    session.execute(text("SET LOCAL enable_seqscan = off"))
    plan = session.execute(
        text(
            "EXPLAIN SELECT outcome, count(*) FROM evaluation_attempts "
            "WHERE prompt_key = 'vision_system_prompt' "
            "AND created_at >= now() - interval '7 days' GROUP BY outcome"
        )
    ).scalars().all()
    assert "ix_evaluation_attempts_prompt_created_at" in "\n".join(plan)
