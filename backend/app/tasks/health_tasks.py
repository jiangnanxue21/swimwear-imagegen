"""Worker 存活验证任务。用于 `make worker-ping` 与部署自检。"""
from __future__ import annotations

import time

from app.tasks.celery_app import celery_app


@celery_app.task(name="health.ping")
def ping() -> dict:
    return {"pong": True, "ts": time.time()}
