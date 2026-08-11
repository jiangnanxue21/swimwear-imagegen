"""生成方案的出入参(PRD v3.1 §12.4)。

角度的入参形状**故意收得很窄**:`list[AngleIn]`。接口层接受三种写法
(`workflows.generation_plan.normalize_angles` 那三种)是给内部调用留的
兼容,而对外只暴露一种 —— 前端有两种写法可选时,两个页面迟早会各挑一种,
然后"这两份方案一不一样"就有了两个答案。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import GenerationPlanStatus, ImageAngle


class AngleIn(BaseModel):
    angle: ImageAngle
    #: 该角度出几张候选。**不是最终入集数** —— 评分会淘汰,出 2 张留 1 张是常态
    count: int = Field(default=1, ge=1, le=8)


class GenerationPlanCreate(BaseModel):
    spu_id: UUID
    #: 空 = SPU 默认方案;非空 = 该颜色的覆盖(§4.7)
    color_variant_id: UUID | None = None
    model_template_id: UUID | None = None
    provider: str = Field(default="", max_length=32)
    scene: str = Field(default="", max_length=64)
    pose: str = Field(default="", max_length=32)
    angles: list[AngleIn] = Field(default_factory=list)
    budget_cap: Decimal | None = Field(default=None, ge=0)
    note: str | None = None


class GenerationPlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    spu_id: UUID
    color_variant_id: UUID | None
    model_template_id: UUID | None
    provider: str
    scene: str
    pose: str
    angles_json: list[dict[str, Any]]
    budget_cap: Decimal | None
    #: 派生值,**只读**。前端不许回传它 —— 唯一写入点在
    #: `services/generation_plan_service`,回传等于给它开第二个入口
    plan_fingerprint: str
    status: GenerationPlanStatus
    row_version: int
    note: str | None = None


class EffectivePlanOut(BaseModel):
    """**这件商品出图会按哪份方案跑**(a47 / PRD §7)。

    ## 为什么这个端点必须存在

    §7 要求运营版建任务弹窗把 Provider 与模特显示成「方案解析出来的只读结果」。
    前端凑得出这个答案吗?凑得出 —— 拉 `/generation-plans?spu_id=`,
    再自己实现一遍"颜色覆盖优先、回落 SPU 默认、只认 ACTIVE"。
    **而那正是硬规则 4 明令禁止的**:前端不许推测状态。更实际的是,
    那份解析规则一旦有两个实现,建任务用一份、界面显示另一份,
    运营会看着一份不是他即将执行的方案点下付费按钮。

    所以判定仍在 `workflows.generation_plan.resolve_plan` 一处,
    这个端点只是把它的答案送出去。

    ## 字段的口径

        plan                  解析结果。**没有生效方案时为 null,不是空对象**
        scope                 这份方案是颜色级覆盖还是 SPU 默认回落
        candidates_per_round  一轮要出几张(`gp.total_candidates`)。
                              前端不做这个加法 —— 它是"点下去要花几次钱"的因数
        required_angles       验收要覆盖的角度集合(`gp.required_angles`),已排序
        problems              这份方案能不能拿去建任务(`gp.plan_problems`)。
                              有问题也照样返回方案本身:藏起来的话运营看到的是
                              "没有方案",然后会去建第二份
    """

    product_id: UUID
    spu_id: UUID | None = None
    color_variant_id: UUID | None = None
    plan: GenerationPlanOut | None = None
    #: COLOR = 这个颜色自己的覆盖;SPU_DEFAULT = 从 SPU 默认回落来的
    scope: str | None = None
    candidates_per_round: int | None = None
    required_angles: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)


class GenerationPlanEffectOut(BaseModel):
    """启用一份方案会让哪些颜色的图片集过期(§8.1 第 7 行)。

    §7.5「上游变化前展示失效影响」要的就是这个 —— 让运营在**点下去之前**
    看见代价,而不是点完之后发现八个颜色都要重出。
    """

    plan: GenerationPlanOut
    stale_color_variant_ids: list[UUID] = Field(default_factory=list)
