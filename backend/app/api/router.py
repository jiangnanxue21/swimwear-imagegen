"""API 路由汇总。

阶段 3:健康检查 + 商品与素材 + 生成任务 + Provider + 模特模板。
阶段 4:人工审核队列 + 评分只读端点。
阶段 6:仪表盘 + 成品图导出。
阶段 8:后台设置(Provider 密钥、生成模型、评分模型)。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    attributes,
    auth,
    dashboard,
    environment,
    exports,
    generation_plans,
    generation_tasks,
    health,
    image_sets,
    media_files,
    media_library,
    model_templates,
    products,
    prompts,
    providers,
    publish,
    reviews,
    settings,
    spus,
    usage,
    workbench,
    workbench_batch,
)

api_router = APIRouter()
api_router.include_router(health.router)
# 身份自查(A5)。走操作口令守卫,给冷启动横幅一次"带口令"的真实探测
api_router.include_router(auth.router)
# 私有素材代发。它自己验 URL 签名,因此不在 main.PUBLIC_PREFIXES 之外还要口令 ——
# <img> 标签带不了自定义请求头,详见 api/media_files.py
api_router.include_router(media_files.router)
# 素材库(M1)。走正常的操作口令
api_router.include_router(media_library.router)
# 属性识别(M3)
api_router.include_router(attributes.router)
# 图片集(M5)
api_router.include_router(image_sets.router)
api_router.include_router(products.router)
# 建档:SPU / 颜色变体 / SKU 展开(阶段 1,PRD v3.1 §12.1)。
# 挂在 products 后面是因为它是 products 的**上游**:一个 SPU 建完,
# 底下那几行 SKU 就是 products 表里的行
api_router.include_router(spus.router)
# 生成方案(阶段 4,PRD v3.1 §12.4)。挂在任务前面是因为它是任务的**上游**:
# 创建任务前要先解析出"这个颜色当前生效的是哪一份方案"
api_router.include_router(generation_plans.router)
api_router.include_router(generation_tasks.router)
api_router.include_router(providers.router)
# 环境真实性(任务 5)。前端状态条按它渲染 —— 运营要能一眼看出
# 现在看到的图和属性是真的还是 Mock 编的
api_router.include_router(environment.router)
api_router.include_router(model_templates.router)
api_router.include_router(reviews.router)
api_router.include_router(dashboard.router)
api_router.include_router(exports.router)
api_router.include_router(settings.router)
api_router.include_router(prompts.router)
# 付费调用花费台账(A23)。运营也要看 —— 预算快满了会影响今天怎么排活
api_router.include_router(usage.router)
# 发布(任务 18):提交/清单/详情/刷新状态/下架/清理预案。
# 挂在 workbench 之前是为了让 /publish 这一组在 OpenAPI 里连在一起 ——
# 运营页面按这一组来接,分散开之后没人能一眼看出发布链路一共有几个口
api_router.include_router(publish.router)
# 运营工作台(任务书阶段 1+2):流程聚合、文案、草稿、导出
api_router.include_router(workbench.router)
# 运营工作台(任务书阶段 3):批量任务、异常列表、CSV 导入、SPU 聚合、审计查询
api_router.include_router(workbench_batch.router)
