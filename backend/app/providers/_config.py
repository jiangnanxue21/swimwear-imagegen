"""Provider 配置读取。

为什么单独抽一层:Provider 的**能力声明与配置检查**处在应用启动路径和
`GET /api/providers` 上,这两条路径必须极其健壮 —— 任何一个可选依赖缺失
都不该让 Provider 页面打不开。因此这里对 Settings 的依赖做成软依赖,
拿不到就退回 os.environ。

生产环境里永远走第一条分支;退化分支只在被裁剪过的运行环境(离线测试、
最小化镜像)生效。
"""
from __future__ import annotations

import os
from pathlib import Path

#: backend/ 的上一级,即项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _override(name: str) -> str | None:
    """后台设置页写进数据库的覆盖值。

    放在最前面读,是为了让\"改完就生效\"对所有调用点一次性成立 ——
    包括另一个进程里的 worker。读不到(数据库没起、表还没迁移、被裁剪的运行环境)
    一律当作没有覆盖,继续走环境变量,绝不因为读配置失败而中断生成。
    """
    # 纯测试与离线诊断需要一个**明确的环境变量-only 模式**。否则读取一个空的
    # API Key 也会先连接 app_settings,测试结果和开发者数据库里的覆盖值绑定。
    if os.environ.get("PROVIDER_SETTINGS_ENV_ONLY", "").strip().lower() in {
        "1", "true", "yes", "on"
    }:
        return None
    try:
        from app.services.settings_runtime import override_for

        return override_for(name)
    except Exception:  # noqa: BLE001
        return None


def provider_setting(name: str, default: str = "") -> str:
    override = _override(name)
    if override is not None:
        return override
    try:
        from app.core.config import settings

        return str(getattr(settings, name, default) or default)
    except ImportError:
        return os.environ.get(name, default)


def project_root() -> Path:
    try:
        from app.core.config import PROJECT_ROOT as configured_root

        return configured_root
    except ImportError:
        return PROJECT_ROOT


def provider_flag(name: str, default: bool = False) -> bool:
    raw = provider_setting(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def provider_int(name: str, default: int) -> int:
    try:
        return int(float(provider_setting(name, str(default))))
    except (TypeError, ValueError):
        return default


def provider_float(name: str, default: float) -> float:
    try:
        return float(provider_setting(name, str(default)))
    except (TypeError, ValueError):
        return default
