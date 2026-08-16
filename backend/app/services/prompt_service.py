"""提示词的读写与版本管理。

设计上的两个决定:

1. **默认值不入库。** 库里没有 active 记录时,评分器用 ``DEFAULT_SYSTEM_PROMPT``。
   第一次在页面上保存才产生 version=1。好处是新部署不必跑 seed,
   而且"从没改过"和"改过又改回来了"在库里是可区分的两件事。

2. **保存不做拦截,只做体检。** 已经确认过要完全开放全文编辑,所以
   ``lint_prompt()`` 返回的是警告清单,不是拒绝理由。但它必须存在 ——
   提示词和 JSON Schema 是同一份约束的两种表达,手滑删掉一个维度不会当场报错,
   只会让模型开始间歇性输出解析失败的结果,而界面上只显示"这张图评分失败"。
   查这种问题非常费劲,提前给一行提示的成本几乎为零。
"""
from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import iso_utc
from app.core.logging import get_logger
from app.models.prompt_template import PromptTemplate
from app.services.prompt_rules import (
    KNOWN_KEYS,
    MAX_PROMPT_CHARS,
    VISION_SYSTEM_PROMPT,
    VISION_SYSTEM_PROMPT_MEN,
    PromptWarning,
    default_content,
    lint_prompt,
)

logger = get_logger(__name__)

#: 纯策略部分在 prompt_rules 里,这里原样再导出,调用方不必关心这层拆分
__all__ = [
    "VISION_SYSTEM_PROMPT",
    "VISION_SYSTEM_PROMPT_MEN",
    "KNOWN_KEYS",
    "MAX_PROMPT_CHARS",
    "PromptWarning",
    "default_content",
    "lint_prompt",
    "get_active_content",
    "list_versions",
    "save_version",
    "activate_version",
    "reset_to_default",
    "describe",
]


def get_active_content(session: Session, key: str) -> tuple[str, int | None]:
    """当前生效的提示词与版本号。库里没有就返回代码默认值和 None。"""
    row = session.execute(
        select(PromptTemplate).where(
            PromptTemplate.key == key, PromptTemplate.is_active.is_(True)
        )
    ).scalar_one_or_none()
    if row is None:
        return default_content(key), None
    return row.content, row.version


def list_versions(session: Session, key: str, *, limit: int = 50) -> list[PromptTemplate]:
    return list(
        session.execute(
            select(PromptTemplate)
            .where(PromptTemplate.key == key)
            .order_by(PromptTemplate.version.desc())
            .limit(limit)
        ).scalars()
    )


#: 保存 / 启用时的重试次数。advisory lock 之外还留一层重试,是因为锁只覆盖
#: 走这条代码路径的进程 —— 迁移脚本、手工 SQL、别的服务都不会去拿这把锁。
MAX_VERSION_RETRIES = 3


def _advisory_key(key: str) -> int:
    """把提示词 key 映射成 advisory lock 用的 64 位整数。

    用 sha256 而不是 PostgreSQL 的 ``hashtext()``:后者的算法在大版本之间变过,
    升级数据库会让锁的取值整体漂移 —— 升级过程中新旧进程各自锁在不同的数字上,
    等于没锁,而且完全没有征兆。自己算就跟数据库版本无关了。

    取前 8 字节转有符号整数,正好落在 ``bigint`` 范围内。
    """
    digest = hashlib.sha256(f"prompt_template:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _lock_key(session: Session, key: str) -> None:
    """按提示词 key 上事务级 advisory lock。

    锁的粒度是 key,不是整张表:改 ``vision_system_prompt`` 不该挡住别的用途。

    为什么必须有:版本号是 ``查最大值 + 1``,活动版本切换是"先全部停用再启用",
    两者都是读-改-写。并发保存会撞 ``(key, version)`` 唯一约束,并发启用会撞
    ``uq_prompt_templates_one_active`` 部分唯一索引,用户看到的是一个 500 ——
    而他做的事情本身完全正当,只是同事恰好在同一秒也点了保存。

    事务级(``pg_advisory_xact_lock``)意味着提交或回滚时自动释放,
    不需要显式解锁,也就不会因为某条异常路径漏掉解锁而把整个 key 锁死。

    非 PostgreSQL 后端直接跳过 —— 这个项目只跑在 PostgreSQL 上,
    但测试夹具偶尔会用别的引擎,不该在那里炸掉。
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)").bindparams(
            bindparam("lock_key", _advisory_key(key))
        )
    )


def _next_version(session: Session, key: str) -> int:
    """下一个版本号。**必须在 `_lock_key()` 之后调用。**"""
    current = session.execute(
        select(func.max(PromptTemplate.version)).where(PromptTemplate.key == key)
    ).scalar_one_or_none()
    return (current or 0) + 1


def _deactivate_all(session: Session, key: str) -> None:
    for row in session.execute(
        select(PromptTemplate).where(
            PromptTemplate.key == key, PromptTemplate.is_active.is_(True)
        )
    ).scalars():
        row.is_active = False
    session.flush()


def save_version(
    session: Session,
    key: str,
    content: str,
    *,
    note: str | None = None,
    updated_by: str | None = None,
    activate: bool = True,
) -> PromptTemplate:
    """存一个新版本。默认立即生效。

    不覆盖任何旧版本 —— 旧的 content 是解释历史判定的唯一依据。
    """
    if key not in KNOWN_KEYS:
        raise ValueError(f"未知的提示词用途:{key}")
    if content is None:
        raise ValueError("提示词内容不能为 None")

    # 拿锁之后,"查最大版本号 -> 插入"这一段对同一个 key 是串行的
    _lock_key(session, key)

    last_error: IntegrityError | None = None
    for _ in range(MAX_VERSION_RETRIES):
        # 保存点必须在 **session.add 之前** 建起来。
        #
        # 以前的顺序是 add -> begin_nested -> flush。SQLAlchemy 在开启嵌套事务时
        # 会先把待处理对象自动 flush 出去,于是唯一约束异常发生在 SAVEPOINT
        # **建立之前** —— 整个外层事务当场进入失败状态,后面的 expunge() 和
        # 重试全都作用在一个已经不能再执行任何语句的会话上,只会抛
        # PendingRollbackError。也就是说这段重试逻辑写了,但一次都没能生效过。
        #
        # 反过来,先 begin_nested 再创建、add、flush,异常就一定发生在保存点
        # 内部,回滚只丢掉这一次插入,外层事务完好无损 —— 这正是当初想要的。
        try:
            with session.begin_nested():
                if activate:
                    # 先全部停用再插入。部分唯一索引在数据库层面兜底,
                    # 万一这两步之间崩了,插入会失败而不是留下两条 active
                    _deactivate_all(session, key)

                row = PromptTemplate(
                    key=key,
                    version=_next_version(session, key),
                    content=content,
                    note=(note or None),
                    updated_by=(updated_by or None),
                    is_active=activate,
                )
                session.add(row)
                session.flush()
            break
        except IntegrityError as exc:
            # 走到这里说明有进程绕过了 advisory lock(迁移脚本、手工 SQL)。
            # 重新算一次版本号再试,而不是把一个 500 扔给用户 ——
            # 他做的事情完全正当,只是撞上了别人。
            #
            # 保存点已经回滚,``row`` 从来没进过持久化状态,不需要 expunge。
            last_error = exc
            logger.warning(
                "prompt version collided, retrying",
                extra={"extra_fields": {"event": "settings.prompt_version_collision", "key": key}},
            )
    else:
        raise RuntimeError(
            f"提示词 {key} 连续 {MAX_VERSION_RETRIES} 次版本冲突,请稍后重试"
        ) from last_error

    logger.info(
        "prompt template saved",
        extra={
            "extra_fields": {"event": "settings.prompt_saved",
                "key": key,
                "version": row.version,
                "active": activate,
                "chars": len(content),
                "warnings": [w.code for w in lint_prompt(key, content)],
            }
        },
    )
    return row


def activate_version(session: Session, key: str, version: int) -> PromptTemplate:
    """回滚到某个历史版本。"""
    # 和 save_version 用同一把锁:两者都在改"哪一版是 active",
    # 各锁各的等于没锁
    _lock_key(session, key)

    row = session.execute(
        select(PromptTemplate).where(
            PromptTemplate.key == key, PromptTemplate.version == version
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"提示词 {key} 没有版本 {version}")

    _deactivate_all(session, key)
    row.is_active = True
    session.flush()
    logger.info(
        "prompt template activated",
        extra={
            "extra_fields": {
                "event": "settings.prompt_activated",
                "key": key,
                "version": version,
            }
        },
    )
    return row


def reset_to_default(session: Session, key: str, *, updated_by: str | None = None) -> None:
    """回到代码内置的默认提示词。

    做法是把所有版本停用,而不是插一条"内容等于默认值"的新版本 ——
    这样"当前用的是内置默认"在库里是可判定的状态,不必拿内容去和常量比对。
    """
    _lock_key(session, key)
    _deactivate_all(session, key)
    logger.info(
        "prompt template reset to built-in default",
        extra={"extra_fields": {"event": "settings.prompt_reset", "key": key, "actor": updated_by}},
    )


def describe(session: Session, key: str) -> dict[str, Any]:
    """页面要的全部信息:当前内容、版本、是否内置默认、体检结果、历史。"""
    content, version = get_active_content(session, key)
    return {
        "key": key,
        "content": content,
        "version": version,
        "is_default": version is None,
        "default_content": default_content(key),
        "warnings": [
            {"code": w.code, "message": w.message} for w in lint_prompt(key, content)
        ],
        "versions": [
            {
                "version": row.version,
                "is_active": row.is_active,
                "note": row.note,
                "updated_by": row.updated_by,
                "chars": len(row.content),
                "created_at": iso_utc(row.created_at),
            }
            for row in list_versions(session, key)
        ],
    }
