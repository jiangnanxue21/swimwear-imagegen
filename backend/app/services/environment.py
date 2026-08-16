"""把四个注册表的事实合成一句「这套环境的产出能不能当真」(REVIEW 任务 5)。

判定在 `core/environment.py`(纯模块,穷举测试)。这里只做**组装**:
从各注册表取事实、按当前配置挑出"实际会被用到的那一个"、翻译成人话。

## 为什么按"实际生效的那一个"判,而不是按整张表

Provider 表里同时躺着 mock 和 fashn 是常态。按整张表判的话,只要配了 fashn
就会说"真实",而 `DEFAULT_PROVIDER` 仍然是 mock —— 那句话是错的,并且错在
最危险的方向上。所以每一档都先挑出生效的那个,挑不出来才退回描述整张表。
"""
from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.environment import build_facet, is_trustworthy, overall, pick_active


def describe() -> dict[str, Any]:
    """给 `/api/environment` 的完整回答。

    刻意**不吞异常**:某个注册表抛错时这个接口就该失败,而不是返回一个
    少了一档的乐观结论。少一档的表现和"那一档是真的"一模一样。
    """
    from app.channels import generic
    from app.channels.registry import describe_channels
    from app.evaluators.registry import describe_all as describe_evaluators
    from app.extractors.registry import describe_extractors
    from app.providers._config import provider_setting
    from app.providers.registry import describe_all as describe_providers

    settings = get_settings()

    # 三个"当前选的是谁"一律走 provider_setting,**不走 get_settings()**。
    # 后台设置页把这几个键写进数据库覆盖层,不重启就生效(docs/SETTINGS.md);
    # 而 `get_settings()` 看不见覆盖层。两者不一致时状态条会说 A 而任务跑的是 B ——
    # 这一页的全部作用就是回答"现在跑的到底是谁",它读错了就没有意义了。
    # 各注册表的 `active` 列用的也是这个读法,两边必须同源。
    wanted_provider = (
        provider_setting("DEFAULT_PROVIDER", settings.DEFAULT_PROVIDER) or "mock"
    ).strip().lower()
    wanted_evaluator = (
        provider_setting("EVALUATOR_BACKEND", settings.EVALUATOR_BACKEND) or "mock"
    ).strip().lower()
    wanted_extractor = (provider_setting("EXTRACTOR_BACKEND", "mock") or "mock").strip().lower()

    facets = [
        build_facet(
            key="image",
            label="出图",
            row=pick_active(describe_providers(), wanted_provider),
            wanted=wanted_provider,
            real_detail="真的在调外部出图服务,会产生费用。",
            simulated_detail=(
                "出的是本地假图。**它是真的图片文件,和真实产出在界面上分不出来。**"
            ),
        ),
        build_facet(
            key="scoring",
            label="评分",
            row=pick_active(describe_evaluators(), wanted_evaluator),
            wanted=wanted_evaluator,
            real_detail="真的在调视觉大模型评分。",
            simulated_detail="分数是本地编的。A/B/C/D 分档、自动通过、抽检全部基于这些假分数。",
        ),
        build_facet(
            key="attribute",
            label="属性识别",
            row=pick_active(describe_extractors(), wanted_extractor),
            wanted=wanted_extractor,
            real_detail="真的在调多模态模型识别属性。",
            simulated_detail=(
                "属性值是本地编的。它们会进文案宣称、进上架草稿 —— "
                "末端会是一份看起来合规的假 Listing。"
            ),
        ),
        build_facet(
            key="channel",
            label="渠道上架",
            # 传名字,**不传 None**。`describe_channels()` 的行没有 `active` 键
            # (它不是一张"选一个"的表,今天就一个渠道),于是 `pick_active` 的
            # 第一条线索落空;而第二条线索被 `None` 掐掉之后它只能返回 None,
            # `build_facet` 把 None 判成 UNAVAILABLE —— 于是状态条常年对运营说
            # 「generic 还没实现,只有骨架,这一步跑不通」,而它其实正在用
            # Simulator 正常工作。**两条线索必须至少留一条能用。**
            row=pick_active(describe_channels(), generic.CHANNEL),
            wanted=generic.CHANNEL,
            real_detail="真的在调平台接口。",
            simulated_detail=(
                "上架走的是渠道模拟器。它会返回像模像样的平台单号,但平台上没有这个商品。"
            ),
        ),
    ]

    # 严重程度排序只有一份,在 core/environment 里。在这里再写一遍 max(...)
    # 的后果不是多几行,是两份判定迟早各说各话 —— 而它们各说各话时,
    # 界面上那句话和接口返回的那个档位会对不上
    verdict = overall(facets)

    return {
        "fidelity": verdict.value,
        "trustworthy": is_trustworthy(verdict),
        "facets": [
            {
                "key": f.key,
                "label": f.label,
                "fidelity": f.fidelity.value,
                "backend": f.backend,
                "detail": f.detail,
            }
            for f in facets
        ],
        # 运行形态。影响可靠性,**不影响产出真实性**,所以不参与上面的判定 ——
        # 理由写在 core/environment.py 的模块注释里
        "deployment": {
            "batch_execution_mode": settings.BATCH_EXECUTION_MODE,
            "storage_backend": settings.STORAGE_BACKEND,
        },
    }
