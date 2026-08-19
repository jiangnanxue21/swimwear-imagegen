"""驳回记录的时间戳,以及靠它闭合的那条恢复链(A67-ISSUE-001)。需要真实 PostgreSQL。

## 这一批补的是一个纯测试证明不了的洞

外部审计在 a67 抓到:`TimestampMixin` 声明 `created_at` 是 NOT NULL +
`server_default=now()`,而 0019 把这一列建成可空、无默认。两个生产创建路径都
不显式写时间戳(ORM 认为库会自己填),于是**真实库里新建的驳回记录拿到 NULL**。

`platform_service._gate_for` 把它转成 `rejected_at` 交给 `platform.resolve_gate`,
而那个函数的两个循环都写在 `if since is not None:` 里面 —— `rejected_at` 为空时
没有任何后续动作会被认成「驳回之后发生的」。运营重导了、重提了,闸门照样说缺。

**为什么此前一条测试都没红。** `conftest.engine` 走 `alembic upgrade head` 建库,
所以真库上这一列确实是可空的;但 `test_poll_and_delist_db.py` 对新建驳回的断言
只覆盖 status / note / located_by / export_at / count / platform status ——
没有一条看时间戳,也没有一条把「建驳回 → 之后重新导出 → 关闭」跑完整。
于是 DB CI 正常执行,这个洞照样过得去。

而 `audit_migration_chain.py` 在 a67 时明说不比 nullable / server_default,
对这个缺陷天然失明(a68 已扩容,见该文件 `_column_facts`)。

## 两层,缺一不可

    schema 层   information_schema 里这两列到底是不是 NOT NULL + 有默认。
                迁移 0058 真的在库上执行过了吗
    行为层      创建 → 重导 → 关闭这条链走不走得通。**这一层才是要保的东西** ——
                schema 对而链路不通,和 schema 错的后果一模一样

只留 schema 层的话,下一个人把 `_gate_for` 改成读别的列,这批测试全绿而
恢复链再次断掉。审计报告那句「行为测试必须保留,不能只再加一个 AST/字符串
守卫」说的就是这件事。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.core.enums import AuditAction, PlatformStatus, RejectionStatus
from app.models.platform_rejection import PlatformRejection
from app.services import audit
from app.workbench import platform as pf
from app.workbench import platform_service as ps
from tests.conftest import requires_db
from tests.test_publish_flow_db import _draft, _product

pytestmark = requires_db

_TIMESTAMP_COLUMNS = ("created_at", "updated_at")


def _record_export(session, draft, *, fingerprint: str, at: datetime) -> None:
    """造一次导出留痕。

    走 `pf.export_audit_payload` 而不是手写 dict:写方与读方
    (`export_entry`)同处一个文件,手写一份的话,键名漂移时这批测试会
    继续绿 —— 而那正是 `export_audit_payload` 文档里记着的上一次事故。

    `created_at` 显式写:审计表的这一列有 server default,不覆盖的话所有
    留痕都落在同一微秒上,「之后」这个词就没有意义了。
    """
    row = audit.record(
        session,
        actor="tester",
        # 和真实导出路径同一个动作名(`workbench/service.py` 那处 record_export)。
        # 「这是一次导出」写在 payload 的 `action` 里,不在 AuditAction 上 ——
        # 后者是审计表的动作分类,前者是 `export_entry` 认的那一个
        action=AuditAction.UPDATE,
        entity_type="ListingDraft",
        entity_id=draft.id,
        payload=pf.export_audit_payload(
            fmt="xlsx",
            spu=draft.spu,
            fingerprint=fingerprint,
            components={"copy": "v2"},
            export_count=2,
        ),
    )
    row.created_at = at
    session.flush()


def _rejection(session, *, sku: str, spu: str):
    """走真实录入路径建一条驳回。**不手工 INSERT。**

    手工造行会显式带上时间戳,而"生产路径不带时间戳"正是这个缺陷的前半段 ——
    绕过它,这批测试就永远复现不出要测的东西。
    """
    product = _product(session, sku=sku, spu=spu)
    draft = _draft(session, product, fingerprint="fp-rejected")
    _record_export(
        session,
        draft,
        fingerprint="fp-rejected",
        at=datetime.now(UTC) - timedelta(hours=2),
    )
    rejection = ps.record_rejection(
        session,
        draft,
        product,
        reason_code=pf.RejectionReason.IMAGE_ISSUE.value,
        platform_note="平台说主图不合规",
        wanted_fingerprint=None,
        actor="tester",
    )
    session.flush()
    return product, draft, rejection


# ============================================================ schema 层


@pytest.mark.parametrize("column", _TIMESTAMP_COLUMNS)
def test_the_migrated_database_declares_the_timestamp_not_null(session, column):
    """迁移建出来的表,这两列必须是 NOT NULL。

    读 `information_schema` 而不是读 ORM:ORM 一直是对的,错的是库。
    问 ORM 等于问被告他自己有没有罪。
    """
    is_nullable = session.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'platform_rejections' AND column_name = :c"
        ),
        {"c": column},
    ).scalar_one()
    assert is_nullable == "NO", (
        f"platform_rejections.{column} 在真实库上仍然可空 —— "
        "迁移 0058 没有执行,或者被后来的迁移撤掉了"
    )


@pytest.mark.parametrize("column", _TIMESTAMP_COLUMNS)
def test_the_migrated_database_fills_the_timestamp_by_itself(session, column):
    """而且必须有库级默认。

    NOT NULL 单独存在是不够的:ORM 编译 INSERT 时**不带**这两列(它认为库会填),
    没有默认的话拿到的不是 NULL,是一次 NotNullViolation —— 缺陷换了个形状,
    从"闸门永远不闭合"变成"驳回根本录不进去"。
    """
    default = session.execute(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'platform_rejections' AND column_name = :c"
        ),
        {"c": column},
    ).scalar_one()
    assert default and "now()" in default.lower(), (
        f"platform_rejections.{column} 没有库级默认(当前 {default!r}) —— "
        "ORM 不会在 INSERT 里带这一列的值"
    )


# ============================================================ 行为层


def test_a_new_rejection_gets_a_timestamp_without_anyone_writing_one(session):
    """生产路径不写时间戳,而记录建出来必须带着时间。

    这一条直接钉住缺陷的**前半段**。`record_rejection` 从头到尾没有碰
    `created_at`(去 `platform_service.py` 那两处 `PlatformRejection(...)` 看,
    参数表里没有它),所以这个断言测的正是"库自己填上了"。
    """
    _, _, rejection = _rejection(session, sku="SW-TS-01", spu="SW-TS-01")

    assert rejection.created_at is not None
    assert rejection.updated_at is not None


def test_the_rejection_time_is_readable_as_a_gate_boundary(session):
    """而且这个时间要能被闸门当边界用。

    `_gate_for` 走 `clock.iso_utc(rejection.created_at)`。时间戳为 NULL 时
    它得到 None,`resolve_gate` 里两个循环整个跳过 —— 拿不到边界的闸门
    不是"判错了",是"根本没在判"。
    """
    _, draft, rejection = _rejection(session, sku="SW-TS-02", spu="SW-TS-02")

    gate = ps._gate_for(rejection, draft, ps._export_audit_entries(session, draft.id))

    # 被驳回的那次导出发生在两小时前。它**不该**被算成「驳回之后」——
    # 算进去的话,一条什么都没改的驳回会自己过闸。边界取错方向的后果
    # 不是少提醒一次,是台账里出现一条假的"已解决"
    assert gate.has_new_export is False, (
        "驳回之前的那次导出被算成了「驳回之后」—— 边界比真实驳回时刻还晚"
    )
    assert gate.satisfied is False


def test_fixing_and_re_exporting_closes_the_rejection(session):
    """完整闭环:建驳回 → 改了并重新导出 → 闸门满足 → 关闭。

    **这是这个文件真正要保的那一条。** 上面每一条单独绿都不能推出它绿:
    时间戳有值、schema 也对,只要边界取错一个方向,这条链照样走不通。

    两半证据都要给全,缺一半闸门就该拦下来(`resolve_gate` 的设计方向是
    宁可误报):驳回之后有新的导出留痕,而且新导出的指纹已经不同于被驳回那版。
    """
    _, draft, rejection = _rejection(session, sku="SW-TS-03", spu="SW-TS-03")
    assert rejection.status == RejectionStatus.OPEN.value

    # 运营改了东西:草稿指纹变了,并且重新导出了一次(晚于驳回)
    draft.source_fingerprint = "fp-fixed"
    _record_export(
        session,
        draft,
        fingerprint="fp-fixed",
        at=rejection.created_at + timedelta(minutes=30),
    )

    gate = ps._gate_for(rejection, draft, ps._export_audit_entries(session, draft.id))
    assert gate.has_new_export is True, "重新导出没有被认成「驳回之后发生的」"
    assert gate.fingerprint_changed is True
    assert gate.satisfied is True, f"闸门仍然说缺:{gate.missing}"

    resolved = ps.resolve_rejection(
        session,
        rejection,
        draft,
        note="主图已换,重新导出",
        actor="tester",
    )
    session.flush()

    assert resolved.status == RejectionStatus.RESOLVED.value
    # 中间态是给"声明修过了但还没重导"的,这条路径不该落到那里
    assert resolved.status != RejectionStatus.FIXED_PENDING_EXPORT.value
    assert ps.open_rejections(session, draft.id) == []


def test_a_null_timestamp_would_have_jammed_the_gate(session):
    """把时间戳手工抹成 NULL,闸门必须当场卡住 —— 这是缺陷的**原始形状**。

    留这一条是因为上面那批全绿只证明"现在是好的",证明不了"坏了会红"。
    这里把库拉回缺陷发生时的状态,断言后果确实是那个后果:

        运营明明重新导出了 → 闸门仍然说缺新的导出

    收紧约束之后没法再用 ORM 写 NULL 进去(会撞 NotNullViolation),所以走
    一句 `UPDATE`,并在一个 SAVEPOINT 里做完就丢掉 —— 不给后面的用例留残骸。
    """
    _, draft, rejection = _rejection(session, sku="SW-TS-04", spu="SW-TS-04")
    draft.source_fingerprint = "fp-fixed"
    _record_export(
        session,
        draft,
        fingerprint="fp-fixed",
        at=rejection.created_at + timedelta(minutes=30),
    )
    session.flush()

    healthy = ps._gate_for(rejection, draft, ps._export_audit_entries(session, draft.id))
    assert healthy.satisfied is True

    # **显式 rollback,不用 `with`。** `with session.begin_nested():` 正常退出时
    # 是 RELEASE SAVEPOINT(把改动并进外层),不是回滚 —— 这里要的恰好是后者:
    # 那句 ALTER 不该活过这个用例
    nested = session.begin_nested()
    try:
        session.execute(
            text(
                "ALTER TABLE platform_rejections "
                "ALTER COLUMN created_at DROP NOT NULL"
            )
        )
        session.execute(
            text("UPDATE platform_rejections SET created_at = NULL WHERE id = :i"),
            {"i": rejection.id},
        )
        session.expire(rejection)
        assert rejection.created_at is None

        jammed = ps._gate_for(
            rejection, draft, ps._export_audit_entries(session, draft.id)
        )
        assert jammed.satisfied is False
        assert jammed.has_new_export is False, (
            "时间戳为空时闸门居然认出了新导出 —— 那说明边界判定已经不看 created_at 了,"
            "这条用例要保的东西换了地方,去看 _gate_for"
        )
    finally:
        nested.rollback()

    # 回滚之后约束和数据都该回来 —— 否则同一个 session 里后面的用例会踩到残骸
    session.expire(rejection)
    assert rejection.created_at is not None


def test_platform_status_stays_rejected_until_someone_says_otherwise(session):
    """顺带钉住 P1 的老不变量:关闭驳回不会让草稿自己跳回 SUBMITTED。

    放在这个文件里是因为上面那条闭环用例刚好走到这一步 —— 如果哪天有人为了
    让闸门更好过而顺手恢复了自动跳转,这里会红。
    """
    _, draft, rejection = _rejection(session, sku="SW-TS-05", spu="SW-TS-05")
    draft.source_fingerprint = "fp-fixed"
    _record_export(
        session,
        draft,
        fingerprint="fp-fixed",
        at=rejection.created_at + timedelta(minutes=30),
    )
    ps.resolve_rejection(
        session, rejection, draft, note="改好了", actor="tester"
    )
    session.flush()

    assert draft.platform_status == PlatformStatus.REJECTED.value


def test_every_rejection_row_in_the_table_carries_a_timestamp(session):
    """兜底:这张表里不该存在没有时间戳的行。

    上面那些用例各自只看自己造的那一条。这一条看的是**整张表** ——
    包括别的用例、别的夹具、以及将来某个新路径建出来的行。
    """
    _rejection(session, sku="SW-TS-06", spu="SW-TS-06")
    session.flush()

    orphans = session.scalars(
        select(PlatformRejection).where(
            (PlatformRejection.created_at.is_(None))
            | (PlatformRejection.updated_at.is_(None))
        )
    ).all()
    assert orphans == []
