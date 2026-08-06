"""后台设置的运行时读取层 —— 让改完的配置不用重启就生效。

全系统读配置只有一个入口:``app.providers._config.provider_setting``。
FASHN 的 Key、试穿模型、超时,评分器的选择,轮次上限,全都走它。
所以这里只要接上那一个函数,设置页就对**所有**调用点同时生效,
包括另一个进程里的 Celery worker —— 它自己也会在 TTL 到期后重新读一遍。

三条设计约束:

1. **读失败绝不影响主流程。** 数据库连不上就退回环境变量,任务照跑。
2. **只缓存,不监听。** 没有额外的广播通道,worker 最多晚 TTL 秒看到新值。
   代价是一次生成任务可能用旧配置,收益是少一个会坏的组件。
3. **只有声明表里的键能被覆盖。** 数据库里就算被塞进 DATABASE_URL 也不会生效。
"""
from __future__ import annotations

import threading
import time

from app.core.env_source import is_env_provided
from app.core.logging import get_logger
from app.core.settings_schema import FIELDS

logger = get_logger(__name__)

DEFAULT_TTL_SECONDS = 10.0

_lock = threading.Lock()
_cache: dict[str, str] = {}
_loaded_at: float = 0.0
_warned_at: float = 0.0


def _ttl() -> float:
    try:
        from app.core.config import settings

        return max(0.0, float(settings.SETTINGS_CACHE_TTL_SECONDS))
    except Exception:  # noqa: BLE001
        return DEFAULT_TTL_SECONDS


def _env_lock_active() -> bool:
    try:
        from app.core.config import settings

        return bool(settings.SETTINGS_ENV_LOCK)
    except Exception:  # noqa: BLE001
        return False


def read_from_db() -> dict[str, str]:
    """把 app_settings 读成 {键: 生效值}。密钥在这里解密,只留在内存。"""
    from app.core.secrets import decrypt
    from app.db.session import SessionLocal
    from app.models.app_setting import AppSetting

    session = SessionLocal()
    try:
        rows = session.query(AppSetting).all()
        result: dict[str, str] = {}
        for row in rows:
            if row.key not in FIELDS:
                continue
            value = decrypt(row.value) if row.is_secret else row.value
            if value:
                result[row.key] = value
        return result
    finally:
        session.close()


def overrides(force: bool = False) -> dict[str, str]:
    """当前生效的覆盖值。带 TTL 缓存,读失败时沿用上一次的结果。"""
    global _cache, _loaded_at, _warned_at

    now = time.monotonic()
    if not force and _loaded_at and (now - _loaded_at) < _ttl():
        return _cache

    with _lock:
        # 双检:等锁期间可能已经有别的线程刷过了
        if not force and (time.monotonic() - _loaded_at) < _ttl() and _loaded_at:
            return _cache
        try:
            _cache = read_from_db()
            _loaded_at = time.monotonic()
        except Exception:  # noqa: BLE001 - 数据库暂时不可用不该让生成任务挂掉
            _loaded_at = time.monotonic()
            if (time.monotonic() - _warned_at) > 60:
                _warned_at = time.monotonic()
                logger.warning("cannot read settings overrides; falling back to environment")
        return _cache


def invalidate() -> None:
    """写入后立即失效本进程缓存;其它进程等 TTL 到期。"""
    global _loaded_at
    with _lock:
        _loaded_at = 0.0


def override_for(name: str) -> str | None:
    """给 provider_setting 用:这一项有没有被后台覆盖。没有就返回 None。"""
    key = (name or "").strip().upper()
    if key not in FIELDS:
        return None
    if _env_lock_active() and is_env_provided(key):
        return None
    value = overrides().get(key)
    return value or None
