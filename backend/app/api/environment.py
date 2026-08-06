"""这套部署的产出能不能当真(REVIEW 任务 5)。

前端状态条(任务 6)按这一个接口渲染。**只读、不带任何密钥**:
返回的是"哪一档"和"叫什么名字",不是配置值本身。

为什么单独一个接口,而不是让前端去拼 `/providers` + `/evaluators` + …:
拼的那套判定会落在前端(硬规则 4 不许),而且"Mock 的 is_configured
永远是 True"这个坑每个拼的人都要自己踩一遍。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import require_operator
from app.services import environment as env_service

router = APIRouter(
    prefix="/environment",
    tags=["environment"],
    dependencies=[Depends(require_operator)],
)


@router.get("")
def read_environment() -> dict[str, Any]:
    return env_service.describe()
