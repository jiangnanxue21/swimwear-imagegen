"""Provider 查询与连接测试(需求第十四章 Provider 页面)。

绝不返回 API Key 本身,只返回"是否已配置"。

列表是只读的,开放;**连接测试要过 ``require_admin``** —— 它会拿着已配置的 Key
真的去打厂商接口,不设门槛等于把"替你烧额度"和"探测你配了哪几家"
这两件事一起开放出去。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_admin, require_operator
from app.providers.registry import describe_all, get_provider
from app.schemas.generation import ProviderOut, ProviderTestResult

# 整个路由挂 require_operator(读接口也要)。为什么挂在路由器上见 `deps.require_operator`
router = APIRouter(
    prefix="/providers",
    tags=["providers"],
    dependencies=[Depends(require_operator)],
)


@router.get("", response_model=list[ProviderOut])
async def list_providers() -> list[ProviderOut]:
    return [ProviderOut(**item) for item in describe_all()]


@router.post("/{name}/test", response_model=ProviderTestResult)
async def test_provider(
    name: str,
    actor: str = Depends(require_admin),
) -> ProviderTestResult:
    provider = get_provider(name)
    result = await provider.test_connection()
    return ProviderTestResult(**result)
