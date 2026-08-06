"""抽取器注册表(M3;真实后端 A45-batch14 接上)。

按 `EXTRACTOR_BACKEND` 选。`vision` 复用 `app/llm/` 的传输层与图片层
(M2 抽出来的那套),自己只有请求体形状和响应解析 —— 与当年注释里
预告的边界一字不差。
"""
from __future__ import annotations

from app.core.errors import ConfigError
from app.extractors.mock import MockAttributeExtractor
from app.extractors.vision import VisionAttributeExtractor

_BACKENDS = {"mock": MockAttributeExtractor, "vision": VisionAttributeExtractor}


def get_extractor(name: str | None = None):
    """取当前配置的抽取器。

    未配置时**不静默回退 Mock**:那会让生产环境安静地用假数据写商品属性。
    和评分器的 `VISION_MODEL_FAIL_CLOSED` 是同一条规矩 —— 选了 vision 但
    没配好,第一次调用会以 `NotConfiguredError` 失败,错误里点名缺哪个键。
    """
    from app.providers._config import provider_setting

    backend = (name or provider_setting("EXTRACTOR_BACKEND", "mock") or "mock").strip().lower()
    factory = _BACKENDS.get(backend)
    if factory is None:
        raise ConfigError(
            f"未知的抽取器后端:{backend};可选:{', '.join(sorted(_BACKENDS))}"
        )
    return factory()


def describe_extractors() -> list[dict]:
    """如实上报,包括"我是假的"这件事(REVIEW 任务 8)。

    原来这里无条件返回 `configured: True`。对 Mock 而言那句话**字面上不假**
    —— 它确实不需要任何配置 —— 但它是一个"如实上报"接口能犯的最糟的错:
    调用方拿到一排 `configured: True`,得出的结论是"属性识别已就绪",
    而实际写进商品的每一个属性值都是本地编的。

    属性会进文案宣称、进上架草稿。这一列说错,末端就是一份看起来合规的
    假 Listing。

    ## A45-batch14:两列都问实现自己,名单删掉了

    a37 版的 `configured` 按一张 `_SIMULATED_BACKENDS` 名单填,注释自己承认
    「接真后端时这一行必须改成问它自己」——现在真后端接上了,照办:
    `configured` 问 `is_configured()`,`is_simulator` 问类属性(硬规则 4:
    每个状态字段都要能追溯到真实来源;providers 那边的守卫
    `test_the_simulator_column_traces_to_the_implementation_not_a_list`
    点名过这里,本批次给 extractors 补了同款)。

    实例化放在 try 里不是防御性习惯:这个函数在 `/api/environment` 的
    启动路径上,任何一个后端的配置读取炸掉都不该让状态条整个打不开 ——
    报 `configured: False` 比报 500 诚实得多,也有用得多。
    """
    from app.providers._config import provider_setting

    active = (provider_setting("EXTRACTOR_BACKEND", "mock") or "mock").strip().lower()
    rows: list[dict] = []
    for name in sorted(_BACKENDS):
        factory = _BACKENDS[name]
        try:
            instance = factory()
            configured = bool(instance.is_configured())
        except Exception:  # noqa: BLE001 - 状态上报路径,失败即"没配好"
            configured = False
        rows.append(
            {
                "name": name,
                "configured": configured,
                "is_simulator": bool(getattr(factory, "is_simulator", True)),
                "active": name == active,
            }
        )
    return rows
