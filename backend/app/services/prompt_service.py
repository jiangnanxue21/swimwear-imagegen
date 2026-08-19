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
from typing import Any, Final, TypeAlias

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import iso_utc
from app.core.logging import get_logger
from app.models.prompt_template import PromptTemplate
from app.prompts import registry
from app.services.prompt_rules import (
    KNOWN_KEYS,
    MAX_PROMPT_CHARS,
    VISION_SYSTEM_PROMPT,
    VISION_SYSTEM_PROMPT_MEN,
    PromptWarning,
    default_content,
    lint_prompt,
)
from app.workflows import prompt_stats

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


class _UncheckedExpectedVersion:
    """内部调用明确放弃乐观锁时使用的类型。

    ``None`` 已经有业务含义:调用方看到的是内置默认。再拿它表示“不检查”,
    默认态就会成为唯一绕得过乐观锁的状态。单独的哨兵把两种语义拆开。
    """

    __slots__ = ()


_UNCHECKED_EXPECTED_VERSION: Final = _UncheckedExpectedVersion()
ExpectedVersion: TypeAlias = int | None | _UncheckedExpectedVersion


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


class PromptConflict(Exception):
    """服务端的生效版本已经不是调用方以为的那一版(FE-309 / AC-35)。

    ## 这不是防御性编程

    A 保存了 v5,B 的页面还停在 v4 上编辑,B 点保存 —— 在此之前这里会**直接落 v6**,
    A 的改动无声消失。页面自己的注释写着「三个动作都会改变**在线上生效的评分口径**」,
    而这条路径上没有任何东西问过"你看到的还是现在这一版吗"。

    `save_version` 原有的 advisory 锁与重试守的是**另一件事** —— 版本号别撞、
    active 别有两条。两个人各自基于不同版本各存一版,对那把锁来说是完全合法的。

    ## 三个动作都要,不是只有保存

    保存 / 切版本 / 恢复默认在这一点上是同一类风险:A 在切 v3、B 同时在切 v5,
    一样静默覆盖。v1.0 只给保存加,v2.0 订正为三个都加(AC-35)。

    异常带上服务端现在的实况,让界面能说清"对方改了什么"而不是只说一句 409。
    """

    def __init__(self, key: str, expected: int | None, actual: int | None,
                 updated_by: str | None, updated_at: object) -> None:
        self.key = key
        self.expected = expected
        self.actual = actual
        self.updated_by = updated_by
        self.updated_at = updated_at
        super().__init__(f"{key} 的生效版本已变为 {actual},不是 {expected}")


def _assert_expected_version(session: Session, key: str, expected: ExpectedVersion) -> None:
    """乐观锁。哨兵表示内部调用不检查,``None`` 表示期望内置默认。

    **必须在 `_lock_key()` 之后调用** —— 拿锁之前读到的"当前版本"随时会变,
    检查过了照样能被别人插进来,那种检查只是让人以为有防线。
    """
    if expected is _UNCHECKED_EXPECTED_VERSION:
        return
    row = session.execute(
        select(PromptTemplate).where(
            PromptTemplate.key == key, PromptTemplate.is_active.is_(True)
        )
    ).scalar_one_or_none()
    # 内置默认生效中时,服务端版本是 None。调用方看到的也该是 None ——
    # 用 0 之类的哨兵值表示"没有版本",会让"回到默认"和"第 0 版"长得一样
    actual = row.version if row is not None else None
    if actual == expected:
        return
    logger.warning(
        "prompt save rejected: version moved under the caller",
        extra={
            "extra_fields": {
                "event": "settings.prompt_save_conflict",
                "key": key,
                "expected": expected,
                "actual": actual,
            }
        },
    )
    raise PromptConflict(
        key,
        expected,
        actual,
        row.updated_by if row is not None else None,
        row.updated_at if row is not None else None,
    )


def save_version(
    session: Session,
    key: str,
    content: str,
    *,
    note: str | None = None,
    updated_by: str | None = None,
    activate: bool = True,
    expected_version: ExpectedVersion = _UNCHECKED_EXPECTED_VERSION,
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
    _assert_expected_version(session, key, expected_version)

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


def activate_version(
    session: Session,
    key: str,
    version: int,
    *,
    note: str | None = None,
    updated_by: str | None = None,
    expected_version: ExpectedVersion = _UNCHECKED_EXPECTED_VERSION,
) -> PromptTemplate:
    """回滚到某个历史版本。

    **`note` 不是可选的礼貌,是这个动作唯一的解释(FE-310)。** 保存有 note,
    占位文字甚至写着「回滚时这是唯一的线索」—— 而回滚本身此前没有。三个月后
    看到 v3 生效、v5 存在,没人知道为什么退回去了。按这一页自己的标准,回滚的
    理由比保存的理由更该留:保存至少还能从内容 diff 猜出来,回滚连 diff 都没有。

    **理由不写进版本行的 `note`。** 第一版是往被切到那一版的 note 上拼一行,
    两个理由撞在一起:那一版的 note 说的是「当初为什么这么写」,回滚的理由说的是
    「后来为什么退回来」,揉进一个字段之后再也分不开。而且 `row.note = ...` 这句
    赋值让 `audit_column_writers.py` 按**列名**把 `ListingImageSet.note` 也算成
    有写入点(那个审计刻意"多认一次"),它的欠账条目当场失效 —— 一次改动顺手
    抹掉了另一张表上的一笔账。

    所以理由只进两处:审计 payload(`api/prompts.py`)与
    `settings.prompt_rolled_back` 事件。版本表上「最近一次被切回的时间与理由」
    要从审计里读回来,那需要一个按 entity 查审计的读路径 —— 本轮不做,记在
    PRD §12.4 FE-310 那条下面。
    """
    # 和 save_version 用同一把锁:两者都在改"哪一版是 active",
    # 各锁各的等于没锁
    _lock_key(session, key)
    _assert_expected_version(session, key, expected_version)

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
    # 事件码必须是**字面量**:`test_a53_log_console.py` 按 AST 找 `"event": "..."`,
    # 三元表达式两边它一个都看不见,于是「登记了没人写」当场变红。
    # 字段表只有一份 —— a54 修过手抄返回字典那个坑,同一条道理
    fields = {"key": key, "version": version, "actor": updated_by}
    if note and note.strip():
        logger.info(
            "prompt template rolled back",
            extra={"extra_fields": {"event": "settings.prompt_rolled_back", **fields}},
        )
    else:
        logger.info(
            "prompt template activated",
            extra={"extra_fields": {"event": "settings.prompt_activated", **fields}},
        )
    return row


def reset_to_default(
    session: Session,
    key: str,
    *,
    updated_by: str | None = None,
    note: str | None = None,
    expected_version: ExpectedVersion = _UNCHECKED_EXPECTED_VERSION,
) -> None:
    """回到代码内置的默认提示词。

    做法是把所有版本停用,而不是插一条"内容等于默认值"的新版本 ——
    这样"当前用的是内置默认"在库里是可判定的状态,不必拿内容去和常量比对。
    """
    _lock_key(session, key)
    _assert_expected_version(session, key, expected_version)
    _deactivate_all(session, key)
    logger.info(
        "prompt template reset to built-in default",
        extra={
            "extra_fields": {
                "event": "settings.prompt_reset",
                "key": key,
                "actor": updated_by,
                "note": note,
            }
        },
    )


def get_version(session: Session, key: str, version: int) -> PromptTemplate | None:
    """取某一版的**完整正文**(PRD BE-303)。

    `describe()` 的版本列表刻意只给 `chars` 不给 `content` —— 一次拉全部
    历史正文是浪费。但「想看 v3 写了什么」此前唯一的办法是把它切成生效
    版本,而那个动作会立刻改掉线上评分口径:「想看一眼」和「想让它生效」
    被绑成了一个动作(PRD FE-306)。这个函数把两件事拆开。
    """
    return (
        session.query(PromptTemplate)
        .filter(PromptTemplate.key == key, PromptTemplate.version == version)
        .one_or_none()
    )


def _anti_injection_for(key: str) -> str | None:
    """这处提示词的防注入段正文(FE-305),没有就 None。

    只有 FREE 层的两份评分提示词带它 —— 其余几处的正文由代码拼装,
    「删掉会告警」这句话对它们不成立,折叠块也就不该出现。
    """
    surface = registry.surface_of(key)
    if surface is None or surface.tier is not registry.Tier.FREE:
        return None
    from app.evaluators.vision_schema import ANTI_INJECTION_BLOCK

    return ANTI_INJECTION_BLOCK


def describe(session: Session, key: str) -> dict[str, Any]:
    """页面要的全部信息:当前内容、版本、是否内置默认、体检结果、历史。"""
    from app.services import prompt_usage

    content, version = get_active_content(session, key)
    # FE-306 那一列。逐版取一次(一条 GROUP BY),而不是每行查一次 ——
    # 版本表可能有几十行,每行一次查询会把一次打开详情页变成几十次往返。
    #
    # 版本号在 `evaluation_attempts` 里是**字符串**(那一列 String(32)),
    # 所以下面按 `str(row.version)` 取桶。两边形状不一致的话拿不到数,
    # 而拿不到数长得就像"这一版没跑过" —— 守卫钉着这一条
    per_version = prompt_usage.version_stats(session, key)
    return {
        "key": key,
        "content": content,
        "version": version,
        "is_default": version is None,
        "default_content": default_content(key),
        # FE-305:防注入段单独送出去。**前端不许自己从 default_content 里切** ——
        # 切片的起止在 `vision_schema` 里由两个头尾常量定着,而那两个常量漂一个字,
        # 前端那份切法就会安静地切歪(import 时的 raise 只护着后端这一侧)。
        # 送 None 表示这处提示词没有这一段(TEMPLATE / MAPPING 层)
        "anti_injection_block": _anti_injection_for(key),
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
                # 没有任何调用的版本给的是一份全零统计,**不是 None** ——
                # 这一层答得出"这一版跑了 0 次"这件事本身。分不清"真的 0 次"
                # 与"统计还没覆盖到"的责任在展示层,靠下面那个 stats_since
                "stats": per_version.get(
                    str(row.version), prompt_stats.PromptCallStats()
                ).as_dict(),
            }
            for row in list_versions(session, key)
        ],
        # 统计的起点。0059 执行之前的调用不带归属,所以更早的版本会偏少 ——
        # 界面拿它说「统计自 X 起」,而不是让一个跑过 500 次的老版本
        # 顶着「调用 0 次」冒充没人用过(迁移 0059 的模块文档)
        "stats_since": iso_utc(prompt_usage.coverage_since(session)),
    }
