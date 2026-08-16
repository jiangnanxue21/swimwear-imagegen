"""识别 run 的身份与 §9.2 幂等 —— **真库那一半**(A45-batch14-20)。

## 这个文件在本轮一次都没跑过

打包这一批的机器上没有 PostgreSQL、没有 sqlalchemy。下面每一条都是
**规格**,不是成绩。它们验的三件事恰好是纯层结构守卫**验不到**的:

    部分唯一索引到底建没建起来      纯层只能看到 ORM 里那行声明
    IntegrityError 那一路捞不捞得到赢家   要两条真的并发事务
    server_default 对存量行生不生效  要一张真的有旧行的表

不把它们改写成纯层守卫来凑数:一条只验了「源码里出现过 IntegrityError」
的守卫会让本批的变异计数变成一句谎话。

## 跑法

    docker compose up -d db
    cd backend && pytest tests/test_a45_batch14_20_run_identity_db.py -v

## 先看这一条

`test_the_partial_unique_index_lets_a_failed_run_be_retried` 是最该先跑的:
它验的是**部分**唯一索引那个「部分」。谓词写丢了的话表现是一次识别失败
之后同样的输入再也建不出第二个 run —— 而那正是重试的定义,
运营看到的是一个再也识别不了的商品。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.attributes import run_state
from app.core.enums import ExtractionRunStatus as S
from app.core.enums import MediaStatus
from app.extractors.base import ImageExtractionResult
from app.models.attribute import ProductAttributeExtraction
from app.models.media_asset import MediaAsset
from app.models.product import Product
from app.models.spu import ColorVariant, Spu

pytestmark = pytest.mark.requires_db


# ================================================================ 夹具
#
# **这几个夹具住在本文件里,不进 `conftest.py`。**
#
# 它们本轮之前一个都不存在,于是下面 11 条用例全部 setup error —— 而
# setup error 不是失败:pytest 短摘要里它们和"没写"长得一模一样,
# 于是"唯一索引重试、重复请求不重复付费、增量识别、删 SPU 保留台账"
# 这几条性质**一条真库证据都没有**,而验收表上它们是绿的。
#
# 同级测试文件里的 fixture 不会自动共享(那正是 setup error 的成因),
# 所以只有两条路:进 `conftest.py`,或者进本文件。选后者是因为
# `extractor_spy` / `add_evidence_asset` 是本文件的专用形状 ——
# 放进 `conftest.py` 会让每一个新写的 DB 用例都看得见它们,
# 然后被当成"通用夹具"复用,而它们的语义(计次、不真的调模型)
# 只在这一组里成立。


def _media(session, product, *, colour=None, index: int = 0) -> MediaAsset:
    """一条**能进识别输入**的素材行(§5.1 白名单)。

    `source=MANUAL_UPLOAD` 且不带溯源列是关键:`evidence_assets_for()`
    的粗筛会把 AI 生成图排掉,而排掉之后 `run_extraction` 抛的是
    "没有可用于识别的素材",与本组要验的东西毫无关系。
    """
    row = MediaAsset(
        product_id=product.id,
        spu_id=product.spu_id,
        spu=product.spu,
        source="MANUAL_UPLOAD",
        storage_path=f"products/{product.id}/{index}-{uuid.uuid4().hex[:8]}.jpg",
        mime_type="image/jpeg",
        width=800,
        height=1200,
        bytes=1024,
        # 每张图各自的摘要:同 SPU 同颜色下 sha256 撞了会命中去重键(0044),
        # 于是"补一张图"那条用例补进去的是同一行,指纹不变
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        status=MediaStatus.READY.value,
        color_variant_id=colour,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def product(session):
    """**没有 `spu_id`** 的老建档商品 —— 那正是它在本组里的用途。

    `test_a_product_without_an_spu_still_runs_but_gets_no_key` 要的就是
    这一种:识别照跑,只是建不出幂等键。给它加上 SPU 就等于把那条用例
    验的东西换掉,而它仍然会是绿的。

    ## 缺的是 `spu_id`,不是 `spu`

    上一版这里写的是 `spu=None`,而 `products.spu` 是 `nullable=False`
    (`models/product.py`,那一列是反规范化读列、batch13 起由服务层随
    `spu_id` 同步)。于是每一条用到本夹具的用例都停在 NotNullViolation ——
    **停在 setup 而不是断言**,而 setup error 在 pytest 短摘要里和
    "这条用例没写"长得一模一样:验收表上"部分唯一索引""重复请求不重复付费"
    照旧是绿的,底下一条真库证据都没有。这正是本文件顶部那段话警告的形态,
    只是换了一个成因。

    老建档路径缺的是 `spu_id` 那一列(batch13 才加,`create_product` 与
    CSV 导入还不写它),字符串码一直都在 —— 所以这里给码、不给外键,
    `_run_identity` 照样会因为 `spu_id is None` 走 `PRODUCT_HAS_NO_SPU`。
    """
    row = Product(
        # 有码、无外键:这就是"老建档路径"在库里的真实形状
        spu=f"RUNID-LEGACY-{uuid.uuid4().hex[:8]}",
        spu_id=None,
        sku=f"RUNID-LEGACY-{uuid.uuid4().hex[:12]}",
        name="识别身份用例商品(无 SPU)",
        category="swimwear",
        audience="WOMEN",
    )
    session.add(row)
    session.flush()
    _media(session, row)
    return row


@pytest.fixture
def product_with_spu(session):
    """挂在真 SPU 下的商品,带一张证据图。

    `spu_id` 是幂等键的组成部分之一(`_run_identity`),没有它这一组里
    大半条用例问的"第二次请求命中不命中"根本无从发生 —— 键压根建不出来。
    """
    spu = Spu(
        spu_code=f"RUNID-{uuid.uuid4().hex[:8]}",
        internal_name="识别身份用例 SPU",
        audience="WOMEN",
        base_category="swimwear",
        created_by="test",
    )
    session.add(spu)
    session.flush()
    colour = ColorVariant(
        spu_id=spu.id,
        variant_code="A",
        working_name="黑",
        display_name="黑",
    )
    session.add(colour)
    session.flush()

    row = Product(
        spu=spu.spu_code,
        spu_id=spu.id,
        sku=f"{spu.spu_code}-M",
        name="识别身份用例商品",
        category="swimwear",
        audience="WOMEN",
        color_variant_id=colour.id,
    )
    session.add(row)
    session.flush()
    _media(session, row, colour=colour.id)
    return row


@pytest.fixture
def one_media_id(session, product_with_spu):
    """`only_media_ids=` 那条增量路径的入参。"""
    return _media(session, product_with_spu, index=99).id


@pytest.fixture
def add_evidence_asset(session):
    """再给某件商品加一张证据图。**返回可调用对象,不是行。**

    `test_adding_one_photo_makes_the_next_request_a_new_run` 要在两次
    `run_extraction` **中间**加图 —— 夹具直接返回一行的话,那张图在第一次
    识别之前就在库里了,两次的指纹相同,而那条用例会以"复用了旧 run"
    的名义变红,指向一个不存在的缺陷。
    """
    counter = {"n": 0}

    def _add(product, *, colour=None):
        counter["n"] += 1
        return _media(session, product, colour=colour, index=100 + counter["n"])

    return _add


class _ExtractorSpy:
    """只计次、不真的调模型。**`billable=False`。**

    付费开关关着有两个理由:一是本组验的是身份与幂等,不是记账
    (记账在 `test_a45_batch18_call_accounting.py`);二是打开它会让
    每条用例顺带写出 `provider_usage_records` 行,而那张表的断言
    在别处 —— 两组用例共用一张表的副作用是最难查的那种串扰。

    `calls` 是本组的判据本身:「第二次请求不重复付费」这句话在这里的
    可观测形式,就是这个数没有变。

    ## `declared_versions()` 不是可选的

    §9.2 的幂等键含模型版本与提示词版本两维,而 `_run_identity` 是用
    `getattr(extractor, "declared_versions", None)` 取的:**取不到就不建键**,
    退回本批之前的行为(见 `extractors/base.declared_versions` 与
    `attributes/service._run_identity` 的三种「建不出键」)。上一版的 spy 没有
    这个方法,于是 `no_key_reason` 一直是 `EXTRACTOR_DECLARES_NO_VERSIONS`,
    两次请求各建一个 run —— 而
    `test_a_second_identical_request_reuses_the_run_and_pays_nothing` 报的红
    看上去像"幂等没生效",实际上被测代码一次都没走到判定那一步。

    反方向更值得记一笔:`test_adding_one_photo_makes_the_next_request_a_new_run`
    在同一个缺陷下是**绿的** —— 它要的就是"两次不同的 run"。一条夹具缺陷
    同时制造一条假红和一条假绿,而假绿那条不会有人去看。

    这两个版本号是**声明**,与 `extract()` 返回的 `model_name` /
    `prompt_version` 不是同一件事:后者响应回来才知道,照它建键等于
    付过钱才算得出键,而键要挡的正是付钱前那两下。
    """

    name = "spy"
    billable = False

    def __init__(self) -> None:
        self.calls = 0

    def declared_versions(self) -> tuple[str, str]:
        return ("spy-model", "spy-1")

    def extract(self, *, image, target_fields, enum_defs, options):
        self.calls += 1
        return ImageExtractionResult(
            fields=(),
            unreadable_fields=tuple(target_fields),
            model_name="spy-model",
            prompt_version="spy-1",
            duration_ms=1,
        )


@pytest.fixture
def extractor_spy():
    return _ExtractorSpy()


def _run(session, product_id, *, key=None, status=S.COMPLETED, spu_id=None):
    row = ProductAttributeExtraction(
        product_id=product_id,
        extractor="mock",
        target_fields=["neckline"],
        image_count=1,
        succeeded_count=1,
        failed_count=0,
        schema_version="1",
        status=status.value,
        spu_id=spu_id,
        idempotency_key=key,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------- 索引


def test_the_same_key_cannot_be_taken_twice_while_it_is_occupied(session, product):
    """两行同键、都在占键档 -> 第二行必须被库拦下。

    拦不住的表现不是报错,是**双击建出两个 run**,每个都打一轮付费调用。
    """
    key = "k" * 64
    _run(session, product.id, key=key, status=S.RUNNING)
    with pytest.raises(IntegrityError):
        _run(session, product.id, key=key, status=S.COMPLETED)
        session.flush()


def test_the_partial_unique_index_lets_a_failed_run_be_retried(session, product):
    """同键、但前一行是 FAILED -> 必须能插进去。

    **这条验的是那个「部分」。** 谓词写丢了(全表唯一)的后果:
    一次失败之后同样的输入再也建不出第二个 run,而输入没变、模型没变、
    字段没变 —— 那正是重试的定义。
    """
    key = "f" * 64
    _run(session, product.id, key=key, status=S.FAILED)
    _run(session, product.id, key=key, status=S.RUNNING)  # 不该抛
    session.flush()


def test_partial_success_does_not_occupy_the_key_either(session, product):
    """PARTIAL_SUCCESS 同样不占键 —— 「按颜色重试」靠的就是这一条。

    它占键的话,失败的那几张永远不再有机会重跑,而 §13 阶段 3 把
    「按颜色重试」写成了交付项。
    """
    key = "p" * 64
    _run(session, product.id, key=key, status=S.PARTIAL_SUCCESS)
    _run(session, product.id, key=key, status=S.RUNNING)
    session.flush()


def test_null_keys_never_collide(session, product):
    """建不出键的那几路(没有 spu_id、增量识别、抽取器报不出版本)一律留空,
    而空键必须能重复 —— NULL 在唯一索引里互不相等。

    这条要是不成立,「建不出键」会从「退回本批之前的行为」变成
    「第二次识别直接 500」,而那是最糟的方向:挡住了本该跑的请求。
    """
    for _ in range(3):
        _run(session, product.id, key=None, status=S.COMPLETED)
    session.flush()


def test_the_index_predicate_in_the_database_matches_the_pure_one(session):
    """库里那条索引的谓词,与 `unique_index_predicate()` 生成的一致。

    迁移里写的是冻结字面量,纯层守卫比的是**源码**里那句话;
    这一条比的是**真的建出来的索引**。三者之间任意两个漂移都不报错,
    只是让「这个键被占了吗」在不同环境下有不同答案。
    """
    row = session.execute(
        text(
            "SELECT pg_get_indexdef(indexrelid) FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = 'uq_attr_extractions_idempotency_key'"
        )
    ).scalar_one()
    assert "UNIQUE" in row
    for status in sorted(s.value for s in run_state.KEY_OCCUPYING_STATUSES):
        assert status in row, f"索引谓词里没有 {status} —— 它和判定漂移了"
    for retryable in (S.FAILED, S.PARTIAL_SUCCESS, S.CANCELLED):
        assert retryable.value not in row, (
            f"{retryable.value} 出现在索引谓词里 —— 这个输入再也建不出第二个 run"
        )


# ---------------------------------------------------------------- 服务层


def test_a_second_identical_request_reuses_the_run_and_pays_nothing(
    session, product_with_spu, extractor_spy
):
    """同一个 SPU、同一批素材、同一组字段,连点两次 -> 一个 run、一轮调用。

    **这是 §9.2 的验收句。** 不成立时接口两次都返回 200、属性也都填上了,
    唯一的差别在账单上,而账单不会告诉你哪一笔是重复的。
    """
    from app.attributes import service as attr_service

    first = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    # **前提先钉死,再验结论。** 下面那两条断言在"第一次 run 根本没有键"
    # 和"有键但复用判定坏了"两种情况下报的是同一句红,而两者的修法
    # 一个在夹具、一个在 `run_state.reuse_verdict`。分开断言之后,
    # 报告里那条红指向的是哪一种,读一眼就知道
    assert first.idempotency_key, (
        "第一次 run 没有幂等键 —— 那么「第二次复用」根本无从发生。"
        "先看 `_run_identity` 的 no_key_reason(抽取器报不出版本?没有 spu_id?)"
    )
    assert first.status == S.COMPLETED.value, (
        f"第一次 run 落在 {first.status},只有 COMPLETED 才占键且判 REUSE_RESULT "
        "(见 run_state.reuse_verdict)—— 这条用例失去了前提"
    )

    calls_after_first = extractor_spy.calls
    second = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    assert second.id == first.id, "第二次建了一个新 run"
    assert second.idempotency_key == first.idempotency_key
    assert extractor_spy.calls == calls_after_first, "第二次又打了一轮付费调用"


def test_adding_one_photo_makes_the_next_request_a_new_run(
    session, product_with_spu, extractor_spy, add_evidence_asset
):
    """补一张图 -> 指纹变 -> 键变 -> 新 run。

    反方向才是真正的坏:补了图还命中旧键,于是新图**永远不会被识别**,
    而界面显示「识别完成」。
    """
    from app.attributes import service as attr_service

    first = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    add_evidence_asset(product_with_spu)
    second = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    assert second.id != first.id, "补了一张图之后还在复用旧 run —— 新图不会被识别"
    assert second.input_fingerprint != first.input_fingerprint


def test_a_product_without_an_spu_still_runs_but_gets_no_key(
    session, product, extractor_spy
):
    """没有 `spu_id` 的商品:识别照跑,只是没有幂等保护。

    方向是刻意的 —— 少挡一次的代价是一次重复付费,挡错一次的代价是
    一个再也识别不了的商品。老建档路径(阶段 1 的剩余项)还不写那一列。
    """
    from app.attributes import service as attr_service

    row = attr_service.run_extraction(session, product=product, extractor=extractor_spy)
    assert row.idempotency_key is None
    assert row.spu_id is None
    assert row.status in {s.value for s in S}


def test_an_incremental_run_gets_no_key_and_no_scope(
    session, product_with_spu, extractor_spy, one_media_id
):
    """只识别指定素材:不建键,也不写作用域。

    硬塞成 ALL 的话 `requested_scope` 会说一句不真的话 —— 而它是键的一部分,
    别的调用点照那句话复算会得到另一个键。
    """
    from app.attributes import service as attr_service

    row = attr_service.run_extraction(
        session,
        product=product_with_spu,
        extractor=extractor_spy,
        only_media_ids=[one_media_id],
    )
    assert row.idempotency_key is None
    assert row.requested_scope is None
    assert row.input_fingerprint, "增量 run 照样要记下它吃进去的那批素材"


def test_the_terminal_status_lands_in_the_column(session, product_with_spu, extractor_spy):
    """跑完之后 `status` 必须是终态,不能停在 RUNNING。

    停在 RUNNING 的后果不是显示错:RUNNING **占键**,于是这个 SPU
    再也识别不了第二次 —— 而没有任何地方会说为什么。
    """
    from app.attributes import service as attr_service

    row = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    assert run_state.is_terminal(row.status), f"跑完了还停在 {row.status}"


def test_a_legacy_row_defaults_to_the_closed_status(session, product):
    """0040 之前写下的行(没人算过它的状态)落在 `FAILED`。

    这一列不回填,理由在迁移文档里。方向必须是不放行那一侧:
    默认 COMPLETED 的话,一次从来没有被判定过的 run 会以「算数」的身份
    参与事实合并与占键两件要花钱的事。
    """
    session.execute(
        text(
            "INSERT INTO product_attribute_extractions "
            "(id, product_id, extractor, target_fields, image_count, "
            " succeeded_count, failed_count, schema_version, created_at, updated_at) "
            "VALUES (:i, :p, 'mock', '[]'::jsonb, 0, 0, 0, '1', now(), now())"
        ),
        {"i": uuid.uuid4(), "p": product.id},
    )
    status = session.execute(
        text(
            "SELECT status FROM product_attribute_extractions "
            "WHERE idempotency_key IS NULL ORDER BY created_at DESC LIMIT 1"
        )
    ).scalar_one()
    assert status == S.FAILED.value
    assert run_state.run_is_authoritative(status) is False
    assert S(status) not in run_state.KEY_OCCUPYING_STATUSES


def test_deleting_the_spu_keeps_the_ledger(session, product_with_spu, extractor_spy):
    """删掉 SPU 不连坐删掉识别记录 —— 那是「花了多少钱」的唯一凭据。

    `ON DELETE SET NULL`。断了归属之后下一次同型请求会因为取不到
    `spu_id` 而根本建不出键,于是不会误命中一条孤儿键。

    ## 为什么要先拆两条引用

    `spus` 上挂着三种不同的删除语义,而本条只验其中一种:

        products.spu_id            RESTRICT   拦住删除
        media_assets.spu_id        RESTRICT   拦住删除
        product_attribute_extractions.spu_id   SET NULL   **本条要验的**

    上一版直接 `DELETE FROM spus`,于是 PostgreSQL 在 `products` 那条
    RESTRICT 上就把整条语句挡了回来 —— 用例红在"删不掉",一步都没走到
    SET NULL。而那条红读起来像"删除语义坏了",实际上 RESTRICT 挡住删除
    正是它自己那条用例(`test_publish_flow_db` 那边)期待的行为。

    所以这里显式地按业务顺序拆:先把 SKU 与素材从这个 SPU 上摘下来
    (真实的下线流程也必须先做这一步,`ondelete=RESTRICT` 就是为了逼它),
    再删 SPU。**台账不摘** —— 摘台账等于把"这笔钱花在哪"这件事抹掉,
    而它恰好是本条要证明的东西。

    `color_variant_id` 一起清:颜色变体是 `spus` 的 CASCADE 子表,删 SPU
    会连带删掉它们,而 `products.color_variant_id` 同样是 RESTRICT。
    不清它的话删除会挡在第二道上,表现和第一道一模一样。
    """
    from app.attributes import service as attr_service

    row = attr_service.run_extraction(
        session, product=product_with_spu, extractor=extractor_spy
    )
    run_id = row.id
    spu_id = product_with_spu.spu_id
    assert row.spu_id == spu_id, "识别记录没记下归属 —— 下面验的 SET NULL 会恒真"

    # 拆掉两条 RESTRICT 引用(业务上的"从这个款下摘掉"),台账原样留着
    session.execute(
        text("UPDATE media_assets SET spu_id = NULL WHERE spu_id = :i"), {"i": spu_id}
    )
    session.execute(
        text(
            "UPDATE products SET spu_id = NULL, color_variant_id = NULL "
            "WHERE spu_id = :i"
        ),
        {"i": spu_id},
    )
    session.flush()

    session.execute(text("DELETE FROM spus WHERE id = :i"), {"i": spu_id})
    session.flush()
    assert (
        session.execute(
            text("SELECT count(*) FROM spus WHERE id = :i"), {"i": spu_id}
        ).scalar_one()
        == 0
    ), "SPU 没被删掉 —— 下面那条 SET NULL 断言会因为无事发生而恒真"

    session.expire_all()
    survivor = session.get(ProductAttributeExtraction, run_id)
    assert survivor is not None, "删 SPU 把识别记录一起带走了 —— 账本没了"
    assert survivor.spu_id is None


# ---------------------------------------------------------------- 异步执行


def test_queueing_is_idempotent_and_does_not_call_the_extractor(
    session, product_with_spu, extractor_spy
):
    """HTTP 阶段只建 QUEUED 行；重复请求复用该行，不能提前产生付费调用。"""
    from app.attributes import service as attr_service

    first = attr_service.queue_extraction(
        session,
        product=product_with_spu,
        extractor=extractor_spy,
        actor="tester",
    )
    first_disposition = first._request_disposition
    second = attr_service.queue_extraction(
        session,
        product=product_with_spu,
        extractor=extractor_spy,
        actor="tester",
    )

    assert first.status == S.QUEUED.value
    assert second.id == first.id
    # 同一 Session 会返回同一个 ORM 对象，第二次请求会更新它的瞬时处置标记；
    # 所以第一份响应语义要在第二次调用前截下来。
    assert first_disposition == "NEW_RUN"
    assert second._request_disposition == "RETURN_EXISTING"
    assert first.input_asset_ids
    assert first.requested_by == "tester"
    assert extractor_spy.calls == 0


def test_an_explicit_rerun_replaces_a_completed_result_without_bypassing_inflight_dedupe(
    session, product_with_spu, extractor_spy
):
    from app.attributes import service as attr_service

    first = attr_service.queue_extraction(
        session,
        product=product_with_spu,
        extractor=extractor_spy,
        actor="tester",
    )
    original_key = first.idempotency_key
    first.status = S.COMPLETED.value
    session.flush()

    rerun = attr_service.queue_extraction(
        session,
        product=product_with_spu,
        extractor=extractor_spy,
        force_rerun=True,
        actor="tester",
    )
    rerun_disposition = rerun._request_disposition
    duplicate = attr_service.queue_extraction(
        session,
        product=product_with_spu,
        extractor=extractor_spy,
        force_rerun=True,
        actor="tester",
    )

    assert rerun.id != first.id
    assert first.idempotency_key is None
    assert rerun.idempotency_key == original_key
    assert rerun_disposition == "NEW_RUN"
    assert duplicate.id == rerun.id
    assert duplicate._request_disposition == "RETURN_EXISTING"
    assert extractor_spy.calls == 0


def test_the_worker_claims_and_finishes_the_queued_snapshot(
    session, product_with_spu, extractor_spy, celery_eager, monkeypatch
):
    """worker 只消费排队时的素材快照，并把逐图 checkpoint 与终态一起落库。"""
    from app.attributes import service as attr_service
    from app.tasks import attribute_tasks

    monkeypatch.setattr(attribute_tasks, "get_extractor", lambda _name: extractor_spy)
    row = attr_service.queue_extraction(
        session,
        product=product_with_spu,
        extractor=extractor_spy,
        actor="tester",
    )
    session.commit()

    result = attribute_tasks.run_attribute_extraction.run(str(row.id))
    session.refresh(row)

    assert result["status"] == S.COMPLETED.value
    assert row.status == S.COMPLETED.value
    assert row.started_at is not None
    assert row.finished_at is not None
    assert row.image_outcomes == [
        {
            "asset_id": row.input_asset_ids[0],
            "scope": str(product_with_spu.color_variant_id),
            "succeeded": True,
        }
    ]
    assert row.failed_scopes == []
    assert extractor_spy.calls == 1


def test_cancelling_a_queued_run_is_terminal_and_costs_nothing(
    session, product_with_spu, extractor_spy, celery_eager, monkeypatch
):
    """尚未领取的 run 取消后直接终态；迟到的 worker 消息必须成为空操作。"""
    from app.attributes import service as attr_service
    from app.tasks import attribute_tasks

    monkeypatch.setattr(attribute_tasks, "get_extractor", lambda _name: extractor_spy)
    row = attr_service.queue_extraction(
        session,
        product=product_with_spu,
        extractor=extractor_spy,
        actor="tester",
    )
    attr_service.request_cancellation(session, extraction_id=row.id, actor="tester")
    session.commit()

    result = attribute_tasks.run_attribute_extraction.run(str(row.id))
    session.refresh(row)

    assert result["status"] == S.CANCELLED.value
    assert row.status == S.CANCELLED.value
    assert row.finished_at is not None
    assert extractor_spy.calls == 0
