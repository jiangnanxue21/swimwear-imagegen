"""后台写入的密钥的加密落库。

设置页把\"密钥只能来自环境变量\"这条规矩放宽了一格,所以必须补上对应的保护:
**明文永远不进数据库**。数据库备份、只读从库、误发的 dump 都是真实泄露途径,
加密之后这些途径拿到的只是密文。

密钥的密钥(下称主密钥)按这个顺序找:

    1. SETTINGS_SECRET_KEY —— 推荐。多机部署必须走这条,否则各节点解不开对方写的值。
    2. 密钥目录(SETTINGS_KEY_DIR)下的密钥文件 —— 没配时自动生成,权限 0600,并在日志里提醒。
       docker compose 里 backend 与 worker 共用同一个 secrets 卷,因此单机部署开箱可用。

这个目录**不是**存储目录。存储目录会被挂成 ``/files`` 静态服务,
主密钥放进去等于连同数据库一起泄露。旧版本放错了地方,首次启动会自动搬走并删掉原文件。

拿不到 cryptography 或拿不到主密钥时,``available()`` 返回 False,
写接口会**拒绝保存密钥类字段**并说明原因 —— 绝不退化成明文落库。
"""
from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)

#: 自动生成的主密钥文件名。放在存储目录下,与数据库分离。
KEY_FILE_NAME = ".settings.key"
#: 由口令派生主密钥时用的固定盐。固定是刻意的:必须跨进程、跨重启派生出同一把钥匙。
KDF_SALT = b"swimwear-imagegen/settings/v1"
#: 密文前缀,留给将来换算法时做版本区分
CIPHER_PREFIX = "fernet:"

SOURCE_ENV = "env"
SOURCE_FILE = "file"
SOURCE_NONE = "none"


class SecretsUnavailable(RuntimeError):
    """没有可用的加密能力。调用方应当拒绝写入密钥,而不是降级成明文。"""


def _fernet_class():
    try:
        from cryptography.fernet import Fernet
    except ImportError:  # pragma: no cover - 取决于运行环境是否装了 cryptography
        raise SecretsUnavailable(
            "缺少 cryptography 依赖,无法加密保存密钥。请安装后端依赖后重试"
        ) from None
    return Fernet


def _normalize_key(raw: str):
    """任意口令都接受:本身就是合法 Fernet key 就直接用,否则用 scrypt 派生一把。

    这样运维既可以 `openssl rand -base64 32`,也可以随手填一句长口令,
    不会因为格式不对就卡在\"保存失败\"上。
    """
    Fernet = _fernet_class()
    candidate = raw.strip().encode()
    try:
        Fernet(candidate)
        return candidate
    except (ValueError, TypeError):
        pass
    derived = hashlib.scrypt(candidate, salt=KDF_SALT, n=2**14, r=8, p=1, dklen=32)
    return base64.urlsafe_b64encode(derived)


def _key_file() -> Path:
    from app.core.config import settings

    return settings.secrets_dir / KEY_FILE_NAME


def _legacy_key_file() -> Path:
    """阶段 8 最初把主密钥写在了存储目录里。

    那个目录在 local 存储模式下被挂成 ``/files`` 静态服务,主密钥因此可以被
    直接下载 —— 加密落库防的是数据库泄露,主密钥跟着走 HTTP 出去,这层防护就是零。
    保留这个函数只为把旧部署的密钥搬走,不再往这里写任何东西。
    """
    from app.core.config import settings

    return settings.storage_dir / KEY_FILE_NAME


def _atomic_write_key(path: Path, material: bytes) -> bytes:
    """把 ``material`` 写成主密钥文件,**只有第一个写成功的进程说了算**。

    ``O_CREAT | O_EXCL`` 是这里的全部要点:内核保证同一路径只有一个 open 能成功,
    其余的拿到 FileExistsError。以前用的是「先 exists() 再 write_bytes()」,
    那是个典型的 TOCTOU —— backend、worker、beat 在 docker compose 里同时首次启动,
    三个进程都看到文件不存在,各自生成一把不同的钥匙互相覆盖,然后
    **各自在进程内继续用自己那把**。表现是配置随机解不开,而且重启一次就变一个样。

    创建失败的进程一律回头读胜出的那份,绝不使用自己刚生成的那把。

    权限在 open 时就带上 0600,不是写完再 chmod:那中间有一个窗口,
    文件是可读的。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        # 目录可能是别人建的(共享卷),改不动权限不该让启动失败
        logger.warning("cannot tighten permissions on the secrets directory", extra={
            "extra_fields": {
                "event": "settings.dir_permissions_unchanged",
            }
        })

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = path.read_bytes().strip()
        if existing:
            return existing
        # 极窄的窗口:别人刚建了文件还没写完。等它写完比自己覆盖安全得多 ——
        # 覆盖会让那个进程继续用一把已经不在文件里的钥匙。
        for _ in range(50):
            time.sleep(0.02)
            existing = path.read_bytes().strip()
            if existing:
                return existing
        raise SecretsUnavailable(
            "主密钥文件存在但一直是空的,可能有进程写到一半退出了。"
            "请检查该文件后删除重试,或直接配置 SETTINGS_SECRET_KEY"
        ) from None

    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(material)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        # 写失败就把这个半成品清掉,别让后来的进程读到空文件
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return material


def _migrate_legacy_key(target: Path) -> bytes | None:
    """把旧位置的主密钥搬到新位置并删掉原文件。

    不搬的话,升级上来的部署会当场解不开所有已存密钥;搬完不删的话,
    那个文件还躺在对外目录里 —— 两件事必须一起做。搬运失败一律当作没搬,
    让调用方走\"重新生成\"的路径,绝不吞掉异常继续用一个来源不明的密钥。

    搬运本身也走原子创建:多个进程同时升级时,谁先建成谁的内容生效,
    后来的读回胜出的那份。旧文件内容是一样的,所以这里不存在\"搬错\"的可能。
    """
    legacy = _legacy_key_file()
    try:
        if not legacy.exists():
            return None
        existing = legacy.read_bytes().strip()
        if not existing:
            return None
        winner = _atomic_write_key(target, existing)
    except SecretsUnavailable:
        raise
    except OSError:
        logger.exception("cannot migrate the legacy settings key file", extra={
            "extra_fields": {
                "event": "settings.key_migration_failed",
            }
        })
        return None

    try:
        legacy.unlink()
        removed = True
    except OSError:
        removed = False

    logger.warning(
        "migrated the settings encryption key out of the public storage directory",
        extra={
            "extra_fields": {"event": "settings.key_migrated",
                "from": str(legacy),
                "to": str(target),
                "legacy_removed": removed,
            }
        },
    )
    if not removed:
        logger.error(
            "the legacy key file is still readable through /files; delete it by hand",
            extra={"extra_fields": {"event": "settings.legacy_key_readable", "path": str(legacy)}},
        )
    return winner


def _read_or_create_key_file():
    Fernet = _fernet_class()
    path = _key_file()
    try:
        if path.exists():
            existing = path.read_bytes().strip()
            if existing:
                return existing

        migrated = _migrate_legacy_key(path)
        if migrated:
            return migrated

        winner = _atomic_write_key(path, Fernet.generate_key())
        logger.warning(
            "generated a settings encryption key file; set SETTINGS_SECRET_KEY for multi-node",
            extra={"extra_fields": {"event": "settings.key_generated", "path": str(path)}},
        )
        return winner
    except SecretsUnavailable:
        raise
    except OSError as exc:
        raise SecretsUnavailable(
            "无法读写主密钥文件,请配置 SETTINGS_SECRET_KEY 后重试"
        ) from exc


def _material() -> tuple[bytes, str]:
    """返回(主密钥, 来源)。"""
    configured = ""
    try:
        from app.core.config import settings

        configured = (settings.SETTINGS_SECRET_KEY or "").strip()
    except ImportError:  # pragma: no cover - 被裁剪过的运行环境
        configured = os.environ.get("SETTINGS_SECRET_KEY", "").strip()

    if configured:
        return _normalize_key(configured), SOURCE_ENV
    return _read_or_create_key_file(), SOURCE_FILE


def _box():
    Fernet = _fernet_class()
    material, _ = _material()
    return Fernet(material)


def available() -> bool:
    """能不能加密。设置页据此决定是否允许保存密钥字段。"""
    try:
        _box()
        return True
    except SecretsUnavailable:
        return False
    except Exception:  # noqa: BLE001 - 任何异常都当成\"不可用\",绝不退化成明文
        logger.exception("settings encryption unavailable", extra={
            "extra_fields": {
                "event": "settings.encryption_unavailable",
            }
        })
        return False


_MESSAGES = {
    SOURCE_ENV: "使用 SETTINGS_SECRET_KEY 加密",
    SOURCE_FILE: (
        f"使用存储目录下自动生成的 {KEY_FILE_NAME} 加密;"
        "多机部署请改配 SETTINGS_SECRET_KEY"
    ),
    SOURCE_NONE: "加密不可用",
}


def describe() -> dict[str, object]:
    """给设置页的加密状态。不返回主密钥本身,也不返回它的任何片段。"""
    try:
        _, source = _material()
        _box()
        return {"available": True, "source": source, "message": _MESSAGES[source]}
    except SecretsUnavailable as exc:
        return {"available": False, "source": SOURCE_NONE, "message": str(exc)}
    except Exception:  # noqa: BLE001
        logger.exception("settings encryption unavailable", extra={
            "extra_fields": {
                "event": "settings.encryption_unavailable",
            }
        })
        return {
            "available": False,
            "source": SOURCE_NONE,
            "message": "加密不可用,密钥类配置暂时无法在后台保存",
        }


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return CIPHER_PREFIX + _box().encrypt(plaintext.encode()).decode()


def decrypt(stored: str) -> str:
    """解不开就返回空串并记一条日志 —— 换过主密钥的旧值不该让整个页面打不开。"""
    if not stored:
        return ""
    if not stored.startswith(CIPHER_PREFIX):
        # 不认识的格式一律当成解不开,绝不把它当明文返回
        logger.warning("stored setting is not a recognised ciphertext", extra={
            "extra_fields": {
                "event": "settings.ciphertext_unrecognised",
            }
        })
        return ""
    try:
        return _box().decrypt(stored[len(CIPHER_PREFIX):].encode()).decode()
    except Exception:  # noqa: BLE001 - InvalidToken 及其它一律同样处理
        logger.warning("cannot decrypt stored setting;主密钥可能已更换", extra={
            "extra_fields": {
                "event": "settings.decrypt_failed",
            }
        })
        return ""


#: 派生子密钥用的上下文标签。不同用途用不同标签,互相之间不可推导。
_URL_SIGNING_CONTEXT = b"swimwear-imagegen|media-url-signing|v1"
_IMPORT_PREVIEW_CONTEXT = b"swimwear-imagegen|import-preview-signing|v1"


def url_signing_key() -> bytes:
    """派生一把**只用于媒体 URL 签名**的密钥。

    从同一把主密钥派生,而不是新加一项配置:多一个必填密钥就多一处
    "忘了配所以悄悄退回默认值"的可能,而这个签名正是私有素材的唯一门禁。

    派生而不是直接复用主密钥:签名值会出现在 URL 里、进 access log、进浏览器
    历史。哪怕它只是一个 HMAC 输出,也不该和用来解密数据库里 API Key 的
    那把钥匙共用同一份材料 —— 一处的分析不该给另一处提供任何线索。
    """
    material, _ = _material()
    return hashlib.sha256(material + _URL_SIGNING_CONTEXT).digest()


def import_preview_signing_key() -> bytes:
    """派生一把只用于短期导入预览凭证的密钥。"""
    material, _ = _material()
    return hashlib.sha256(material + _IMPORT_PREVIEW_CONTEXT).digest()
