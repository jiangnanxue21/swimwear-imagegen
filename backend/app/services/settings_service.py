"""设置页的读写(阶段 8)。

三条规矩,写在最前面:

1. **明文密钥不出后端。** 落库前加密,回前端只给末位打码串,审计日志只记键名。
2. **只存差异。** 数据库里只有\"被后台改过\"的项;删掉一行就回到环境变量说了算的状态。
3. **加密不可用就拒绝写密钥。** 不降级、不猜、不留后门,错误信息直接说清楚缺什么。

配置项本身的定义在 ``app.core.settings_schema`` —— 这里只负责持久化与来源判定。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core import secrets
from app.core.clock import iso_utc
from app.core.enums import AuditAction
from app.core.env_source import is_env_provided
from app.core.errors import ErrorCode, ValidationError
from app.core.logging import get_logger
from app.core.settings_schema import FIELDS, SETTING_GROUPS, mask, normalize
from app.models.app_setting import AppSetting
from app.services import audit
from app.services.settings_runtime import invalidate

logger = get_logger(__name__)

SOURCE_DB = "db"
SOURCE_ENV = "env"
SOURCE_DEFAULT = "default"


def _rows(session: Session) -> dict[str, AppSetting]:
    return {row.key: row for row in session.query(AppSetting).all()}


def _env_lock() -> bool:
    from app.core.config import settings

    return bool(settings.SETTINGS_ENV_LOCK)


def _configured_value(key: str) -> str:
    """环境变量 / 默认值给出的值。两者由 pydantic-settings 合并,这里不再区分。"""
    try:
        from app.core.config import settings

        raw = getattr(settings, key, "")
    except ImportError:  # pragma: no cover - 被裁剪过的运行环境
        raw = ""
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if raw is None:
        return ""
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return str(raw)


def describe(session: Session) -> dict[str, Any]:
    """设置页要的全部内容:分组、字段、当前值、值是谁给的、能不能改。"""
    rows = _rows(session)
    locked_globally = _env_lock()

    groups: list[dict[str, Any]] = []
    for group in SETTING_GROUPS:
        fields: list[dict[str, Any]] = []
        for spec in group.fields:
            row = rows.get(spec.key)
            from_env = is_env_provided(spec.key)
            locked = locked_globally and from_env
            overridden = row is not None and not locked

            if overridden:
                source = SOURCE_DB
                raw = row.value_hint if spec.secret else row.value
                has_value = bool(row.value)
            else:
                fallback = _configured_value(spec.key)
                source = SOURCE_ENV if from_env else SOURCE_DEFAULT
                raw = mask(fallback) if spec.secret else fallback
                has_value = bool(fallback)

            fields.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "type": spec.type,
                    "help": spec.help,
                    "placeholder": spec.placeholder,
                    "secret": spec.secret,
                    "advanced": spec.advanced,
                    "minimum": spec.minimum,
                    "maximum": spec.maximum,
                    "options": [{"value": o.value, "label": o.label} for o in spec.options],
                    "value": raw,
                    "has_value": has_value,
                    "source": source,
                    "env_present": from_env,
                    "locked": locked,
                    "updated_at": iso_utc(row.updated_at) if overridden and row else None,
                    "updated_by": row.updated_by if overridden and row else None,
                }
            )
        groups.append(
            {
                "key": group.key,
                "title": group.title,
                "description": group.description,
                "badge": group.badge,
                "fields": fields,
            }
        )

    return {
        "groups": groups,
        "env_lock": locked_globally,
        "encryption": secrets.describe(),
    }


def _assert_writable(key: str) -> None:
    if key not in FIELDS:
        raise ValidationError(f"未知配置项:{key}", code=ErrorCode.INPUT_INVALID)
    if _env_lock() and is_env_provided(key):
        raise ValidationError(
            f"{FIELDS[key].label}:已由环境变量锁定(SETTINGS_ENV_LOCK),后台不可修改",
            code=ErrorCode.CONFIG_INVALID,
        )



def _auditable(row, spec) -> str | None:
    """这一项**改之前**的值,能进审计的那种形式(C-07)。

    敏感项只给掩码提示:`value_hint` 已经是"看得出换没换、看不出内容"的
    那份口径,审计沿用它。给密文同样不行 —— 密钥轮换之后那串东西谁也解不开,
    而审计要的是"当时是不是那一把",不是"当时那一把具体是什么"。

    改之前没有覆盖行时返回 `None`,意思是"这一项此前走的是环境变量/默认值"。
    """
    if row is None:
        return None
    return row.value_hint if spec.secret else row.value

def apply(session: Session, changes: dict[str, Any], actor: str) -> list[str]:
    """保存一批修改。空字符串表示\"删掉覆盖,回到环境变量/默认值\"。

    整批要么全成功要么全失败 —— 半套配置比旧配置更危险。
    """
    if not changes:
        return []

    normalized: dict[str, str] = {}
    for key, raw in changes.items():
        _assert_writable(key)
        try:
            normalized[key] = normalize(key, raw)
        except ValueError as exc:
            raise ValidationError(str(exc), code=ErrorCode.INPUT_INVALID) from None

    secret_writes = [k for k, v in normalized.items() if FIELDS[k].secret and v]
    if secret_writes and not secrets.available():
        raise ValidationError(
            secrets.describe()["message"] or "加密不可用,无法保存密钥",
            code=ErrorCode.CONFIG_INVALID,
        )

    rows = _rows(session)
    touched: list[str] = []
    #: C-07:每个被动过的键的改前 / 改后。敏感项只放掩码
    diff: dict[str, dict[str, str | None]] = {}
    for key, value in normalized.items():
        spec = FIELDS[key]
        row = rows.get(key)

        if not value:
            if row is not None:
                session.delete(row)
                touched.append(key)
            continue

        stored = secrets.encrypt(value) if spec.secret else value
        hint = mask(value) if spec.secret else ""
        # C-07:改前后各记一份。敏感项只记掩码,非敏感项记明文
        diff[key] = {
            "before": _auditable(row, spec),
            "after": hint if spec.secret else value,
        }
        if row is None:
            session.add(
                AppSetting(
                    key=key,
                    value=stored,
                    is_secret=spec.secret,
                    value_hint=hint,
                    updated_by=actor or "system",
                )
            )
        else:
            row.value = stored
            row.is_secret = spec.secret
            row.value_hint = hint
            row.updated_by = actor or "system"
        touched.append(key)

    if touched:
        audit.record(
            session,
            actor=actor,
            action=AuditAction.UPDATE,
            entity_type="app_setting",
            payload={
                "keys": sorted(touched),
                "cleared": sorted(k for k in touched if not normalized[k]),
                # C-07:记改**前后**,不只记"改过哪几个键"。
                #
                # 只记键名的话,事后能回答"谁在什么时候动过 FASHN_API_KEY",
                # 回答不了"他把它从什么改成了什么" —— 而排查一次配置事故
                # (模型突然切了、额度突然烧完)问的正是后者。
                #
                # **密文一律不进审计。** 敏感项只记掩码提示(`value_hint`
                # 的同一份口径),否则审计表会变成第二个明文口令仓库。
                "changes": diff,
            },
        )
        invalidate()
        logger.info("settings updated", extra={"extra_fields": {"keys": sorted(touched)}})
    return sorted(touched)


def reset(session: Session, keys: list[str], actor: str) -> list[str]:
    """删掉指定项的覆盖,回到环境变量/默认值。"""
    for key in keys:
        _assert_writable(key)
    rows = _rows(session)
    removed = []
    for key in keys:
        row = rows.get(key)
        if row is not None:
            session.delete(row)
            removed.append(key)
    if removed:
        audit.record(
            session,
            actor=actor,
            action=AuditAction.DELETE,
            entity_type="app_setting",
            payload={"keys": sorted(removed)},
        )
        invalidate()
    return sorted(removed)
