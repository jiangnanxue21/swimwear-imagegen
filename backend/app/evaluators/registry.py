"""评分器注册表。

与 Provider 注册表同构:延迟构造、只读配置、绝不在导入期发网络请求。

原先这里的规则是「评分器用不了就退回 Mock」,理由是「闭环仍然成立,只是判断力下降」。
**那条理由是错的,已经改掉。**

Mock 评分器按文件指纹给分,真实商品图有相当比例会被判成 A 档,
于是它会被自动通过、自动出图、自动发布 —— 而运营端只看到一行「任务成功」。
「判断力下降」的真实后果是**没被真正评过的图直接上了网站**,这比任务失败严重得多。

现在的规则是失败关闭:

    显式配置 EVALUATOR_BACKEND=mock   -> 用 Mock,这是文档里的离线演练模式
    配了真实评分器但用不了 + 开发环境 -> 退回 Mock,并留一条 warning
    配了真实评分器但用不了 + 其它环境 -> 抛 EvaluatorUnavailableError,任务转人工审核

转人工而不是让任务失败:没有评分器时人还能看图,这条路是通的。
"""
from __future__ import annotations

from collections.abc import Callable

from app.core.errors import ErrorCode, EvaluatorUnavailableError, ValidationError
from app.core.logging import get_logger
from app.evaluators.base import ImageQualityEvaluator
from app.evaluators.mock import MockImageQualityEvaluator
from app.evaluators.vision import VisionModelImageQualityEvaluator
from app.providers._config import provider_setting

logger = get_logger(__name__)

EVALUATOR_FACTORIES: dict[str, Callable[[], ImageQualityEvaluator]] = {
    "mock": MockImageQualityEvaluator,
    "vision": VisionModelImageQualityEvaluator,
}

#: 请求映射已经落地、可以真跑的评分器。与 Provider 侧的 IMPLEMENTED_PROVIDERS 同构。
IMPLEMENTED_EVALUATORS: frozenset[str] = frozenset({"mock", "vision"})

#: 这些环境下允许在真实评分器用不了时退回 Mock。
#:
#: 刻意**不含** ``test``。"测试环境"在多数团队里是一台真机器:连着真实的商品数据、
#: 真实的 Provider Key,别人也能访问。视觉模型配错一个参数,评分就会静默退回
#: Mock —— 而 Mock 按文件指纹给分,真实商品图有相当比例会被判成 A 档,
#: 于是自动通过、自动出图,运营端只看到一行"任务成功"。
#:
#: 要在 test 环境用 Mock 是完全正当的需求,但那应该是一个**显式**的决定:
#: 设置 ``EVALUATOR_BACKEND=mock``。和 api/deps.py 的 LOCAL_ENVS 保持同样的判据 ——
#: 少一个字母的差别不该决定评分能不能被悄悄跳过。
LOCAL_ENVS = frozenset({"local", "dev", "development"})

#: 明确的生产环境。这两个名字下退回 Mock 的代价最大 —— 图会真的发到网站上。
PRODUCTION_ENVS = frozenset({"production", "prod"})


def _app_env() -> str:
    try:
        from app.core.config import settings

        return settings.APP_ENV.strip().lower()
    except Exception:  # noqa: BLE001 - 读不到配置时返回空,调用方按最保守的来
        return ""


def _fail_closed_configured() -> bool:
    """VISION_MODEL_FAIL_CLOSED。读不到时按 true —— 默认安全,不默认方便。"""
    try:
        from app.providers._config import provider_flag

        return provider_flag("VISION_MODEL_FAIL_CLOSED", True)
    except Exception:  # noqa: BLE001
        return True


def _mock_fallback_allowed() -> bool:
    """能不能在真实评分器用不了时退回 Mock。

    生产环境 + fail-closed 时一律不允许:Mock 按文件指纹给分,真实商品图有
    相当比例会被判成 A 档,于是自动通过、自动出图、自动发布,而运营端只看到
    一行「任务成功」。那不是「判断力下降」,那是没被评过的图上了网站。
    """
    env = _app_env()
    if env in PRODUCTION_ENVS and _fail_closed_configured():
        return False
    return env in LOCAL_ENVS


def is_usable(name: str) -> bool:
    """能不能真的拿来打分:已实现 + 已配置。"""
    key = (name or "").strip().lower()
    if key not in EVALUATOR_FACTORIES or key not in IMPLEMENTED_EVALUATORS:
        return False
    return get_evaluator(key).is_configured()


def available_names() -> list[str]:
    return list(EVALUATOR_FACTORIES)


def get_evaluator(name: str) -> ImageQualityEvaluator:
    factory = EVALUATOR_FACTORIES.get((name or "").strip().lower())
    if factory is None:
        raise ValidationError(
            f"未知评分器:{name};可选:{', '.join(available_names())}",
            code=ErrorCode.INPUT_INVALID,
        )
    return factory()


def get_active_evaluator(name: str | None = None) -> ImageQualityEvaluator:
    """取当前生效的评分器。用不了且不允许退回时抛 EvaluatorUnavailableError。"""
    requested = (name or provider_setting("EVALUATOR_BACKEND", "mock") or "mock").strip().lower()

    # 显式选 Mock 是一个正当选择(离线演练、无 Key 演示),不做任何阻拦
    if requested == "mock":
        return MockImageQualityEvaluator()

    if is_usable(requested):
        return get_evaluator(requested)

    reason = _why_unusable(requested)
    if _mock_fallback_allowed():
        logger.warning(
            "evaluator unusable, falling back to mock (dev environment only)",
            extra={"extra_fields": {"requested": requested, "reason": reason}},
        )
        return MockImageQualityEvaluator()

    logger.error(
        "evaluator unusable and mock fallback is not allowed outside development",
        extra={"extra_fields": {"requested": requested, "reason": reason}},
    )
    raise EvaluatorUnavailableError(
        f"评分器 {requested} 当前不可用({reason});任务已转人工审核。"
        "确认配置无误后重新生成,或显式设置 EVALUATOR_BACKEND=mock",
        detail={"evaluator": requested, "reason": reason},
    )


def _why_unusable(requested: str) -> str:
    if requested not in EVALUATOR_FACTORIES:
        return "名称未知"
    if requested not in IMPLEMENTED_EVALUATORS:
        return "请求映射尚未实现"
    try:
        missing = get_evaluator(requested).config.missing_fields()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 评分器没有 config 属性时走通用文案
        return "未配置"
    return f"未配置,缺少:{', '.join(missing)}" if missing else "未配置"


def describe_all() -> list[dict]:
    """供后台展示与 `core/environment` 的真实性判定。绝不返回 API Key 本身。

    `is_simulator` 是补的(A42)。缺了它 `build_facet` 会拿到 `None`,
    而 Mock 的 `is_configured()` 恒为 True —— 判定于是输出 REAL,
    状态条对运营说「真的在调视觉大模型评分」。默认环境下这句话是反的,
    完整分析见 `providers/registry.describe_all()` 的同一段注释。
    """
    active = (provider_setting("EVALUATOR_BACKEND", "mock") or "mock").lower()
    result = []
    for name in available_names():
        evaluator = get_evaluator(name)
        result.append(
            {
                "name": evaluator.evaluator_name,
                "display_name": evaluator.display_name or evaluator.evaluator_name,
                "configured": evaluator.is_configured(),
                "implemented": name in IMPLEMENTED_EVALUATORS,
                # 问实现类自己,不查名单(理由在 evaluators/base.py 那一行上)
                "is_simulator": bool(evaluator.is_simulator),
                "active": name == active,
            }
        )
    return result
