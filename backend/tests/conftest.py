"""pytest 夹具。

两个设计决定,都吃过亏:

**不退化到 SQLite。** 项目用了 JSONB、UUID、部分唯一索引这些 PostgreSQL 专有能力,
换个引擎跑出来的绿是假的。

**CI 里连不上库就直接失败,不跳过。** 本机没库时跳过是合理的 —— 不该逼着每个人
先装一个 PostgreSQL 才能改一行文案。但 CI 里跳过就是**用一片绿掩盖了什么都没跑**:
上一轮评审时结果是 `730 passed, 73 skipped`,那 73 个恰好是所有集成、迁移、
并发测试,也就是唯一能验证这次改动的那批。看到 730 passed 的人不会去数 skipped。

判据是 ``CI`` 环境变量(GitHub Actions、GitLab CI、CircleCI 都会设),
也可以用 ``REQUIRE_TEST_DATABASE=1`` 显式要求。
"""
from __future__ import annotations

import os
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

# ---------------------------------------------------------------- 主密钥隔离
#
# ## 这几行必须在 `from app.core.config import settings` **之前**
#
# `settings` 是模块级单例,在被 import 的那一刻就把环境变量读完了。
# 放进 fixture 里设置是没用的——那时候单例已经构造好了,而这正是
# 上一版那处副作用的根因(方案 12.1 节任务 4:「修复密钥测试副作用」)。
#
# ## 副作用具体是什么
#
# `app.core.secrets._material()` 找不到 `SETTINGS_SECRET_KEY` 时,会走
# `_read_or_create_key_file()` —— 那个函数**在 `secrets_dir` 下真的生成一个
# 主密钥文件**,而 `secrets_dir` 默认就是仓库根的 `.secrets/`。
#
# 也就是说:在装了 pydantic 的环境里跑一次 pytest,仓库树里就会多出一个
# `.secrets/.settings.key`。这不是理论风险——「主密钥连着两个交付包出去」
# 那次事故,第一现场就是这个文件。而且它是无声的:测试全绿,
# 只有 `tools/verify_delivery.py` 会在下一次交付前叫一声。
#
# `tests/pure/test_stage2_pipeline.py` 里那段「设置环境变量、用完还原」的代码
# 在纯运行器下是对的(那时 config 模块 import 不进来,secrets 直接读 os.environ),
# 但在真实 pytest 下完全失效:config 已经先一步把空值读走了。
# 同一段代码在两个运行器下行为不同,这本身就是要修的东西。
#
# ## 修法
#
# 给整个测试会话一把固定的测试密钥 + 一个临时密钥目录,双保险:
# 有 `SETTINGS_SECRET_KEY` 就永远走不到写文件那条路;万一将来有代码绕过它,
# 写出来的文件也落在临时目录而不是仓库树里。
_SECRETS_TMPDIR = tempfile.mkdtemp(prefix="swimwear-test-secrets-")
os.environ.setdefault("SETTINGS_SECRET_KEY", "pytest-fixed-key-do-not-use-in-production")
os.environ.setdefault("SETTINGS_KEY_DIR", _SECRETS_TMPDIR)

from app.core.config import settings  # noqa: E402 —— 见上面那段,顺序是刻意的

#: 测试库地址必须以此结尾。夹具会无条件 DROP SCHEMA,这是最后一道护栏。
TEST_DB_SUFFIX = "_test"


def _derive_test_url() -> str:
    """从主库地址推出测试库地址。**改 database 字段,不做字符串拼接。**

    以前是 ``settings.sqlalchemy_url + "_test"``。数据库 URL 后面完全可以带
    查询参数或选项:

        postgresql+psycopg://u:p@h:5432/imagegen?sslmode=require
            + "_test"  ->  .../imagegen?sslmode=require_test

    拼出来的既不是测试库,也不是一个语法错误 —— 它是一个**指向主库**、
    只是 sslmode 写错了的地址。而下面的夹具接着就会对它执行
    ``DROP SCHEMA public CASCADE``。

    `make_url()` 解析之后只改 database 那一段,查询参数原样保留。
    """
    url = make_url(settings.sqlalchemy_url)
    return url.set(database=f"{url.database or 'imagegen'}{TEST_DB_SUFFIX}").render_as_string(
        hide_password=False
    )


TEST_DB_URL = os.getenv("TEST_DATABASE_URL") or _derive_test_url()


def _truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _assert_safe_to_wipe(url_string: str) -> None:
    """确认这个地址真的是一个可以随便清空的测试库。

    夹具会 ``DROP SCHEMA public CASCADE`` —— 那是不可逆的,而且没有任何提示。
    有人把 ``TEST_DATABASE_URL`` 指错(复制粘贴主库地址是最常见的一种),
    代价是整个生产库。两道护栏都要过:

    1. 库名必须以 ``_test`` 结尾。这条能挡住绝大多数手滑;
    2. 还要显式设 ``ALLOW_DESTRUCTIVE_TEST_DB=1``。因为第 1 条挡不住
       "生产库恰好就叫 xxx_test" 这种情况,而那种情况**必须**由人来确认。

    两条都写在这里而不是文档里:文档挡不住任何东西。
    """
    url = make_url(url_string)
    database = url.database or ""
    if not database.endswith(TEST_DB_SUFFIX):
        raise RuntimeError(
            f"拒绝在 {database!r} 上执行 DROP SCHEMA:测试库名必须以 "
            f"{TEST_DB_SUFFIX!r} 结尾。\n"
            "这批夹具会无条件清空整个 schema,指错库等于删掉真实数据。\n"
            f"请把 TEST_DATABASE_URL 指向一个专用测试库(例如 {database}{TEST_DB_SUFFIX})。"
        )
    if not _truthy("ALLOW_DESTRUCTIVE_TEST_DB"):
        raise RuntimeError(
            f"拒绝在 {database!r} 上执行 DROP SCHEMA:需要显式确认。\n"
            "这批夹具会 DROP SCHEMA public CASCADE,不可逆。\n"
            "确认这个库可以被清空之后,设置 ALLOW_DESTRUCTIVE_TEST_DB=1 再跑。\n"
            "(CI 里把它写进环境变量即可;本机建议只在专用测试库上设。)"
        )


#: 测试引擎的连接参数。**必须和 `db/session.py` 的生产引擎带同一条时区钉子。**
#:
#: `clock.py` 开头把这件事的后果写全了:全部时间列是 `timestamptz`,而我们
#: 写进去的是 naive UTC(`utc_now()`),naive 值发给 timestamptz 时
#: PostgreSQL 按**会话的 TimeZone 参数**解释它。
#: 生产引擎因此把会话时区钉死成 UTC,并注明「这不是一个偏好设置,是一个
#: 正确性前提」。
#:
#: 这里原来是裸的 `create_engine(TEST_DB_URL)` —— 于是那个正确性前提
#: **只在生产成立,测试反而是唯一不满足它的地方**。
#:
#: ## 它是怎么表现出来的(A45-batch16 后一轮,真库 3 条 reaper 用例)
#:
#: 两侧都是 Python naive 时,非 UTC 会话时区让两边**等量偏移**,比较依然
#: 成立 —— 所以大部分真库用例(例如 `test_batch_lease_concurrency_db.py`,
#: 全程 naive↔naive)在一台 `timezone=Asia/Shanghai` 的库上照样全绿。
#: 咬人的是**DB 侧 `now()` 摆现场 + Python naive 判定**的混用形态::
#:
#:     UPDATE ... SET updated_at = now() - interval '1 hour'   真实 instant T-1h
#:     cutoff = utc_now() - 60s(naive,被按 +08 解释)         真实 instant T-8h-60s
#:
#: `updated_at < cutoff` 于是恒假,`reap_stalled()` 一条都收不回来,而
#: **没有任何地方报错** —— 看上去正是"回收器坏了"。`_lease_is_not_alive()`
#: 里的 `phase_lease_until < now` 同理。
#:
#: 换句话说:少这一行,回收器相关的真库用例会在非 UTC 库上诬告业务代码。
#: 钉住它,那些用例问的才是它们自称在问的那件事。
TEST_CONNECT_ARGS = {"options": "-c timezone=utc"}


def _database_error() -> str | None:
    """连得上返回 None,连不上返回原因。"""
    try:
        engine = create_engine(
            TEST_DB_URL,
            connect_args={"connect_timeout": 2, **TEST_CONNECT_ARGS},
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _database_required() -> bool:
    """这台机器上,连不上库算不算失败。"""
    return _truthy("CI") or _truthy("REQUIRE_TEST_DATABASE")


_DB_ERROR = _database_error()

if _DB_ERROR is not None and _database_required():
    # collect 阶段就炸掉,而不是让 73 个用例安静地变成 skipped。
    # 在 CI 里"跑不了"和"跑过了"必须是两种不同的结果。
    raise RuntimeError(
        f"CI 环境要求可连接的 PostgreSQL,但连不上 {TEST_DB_URL}:{_DB_ERROR}\n"
        "这批测试覆盖迁移、并发和集成行为,跳过它们等于这次改动没有被验证。\n"
        "本机开发可以不设 CI/REQUIRE_TEST_DATABASE,那样它们会被正常跳过。"
    )

requires_db = pytest.mark.skipif(
    _DB_ERROR is not None,
    reason=f"需要可连接的 PostgreSQL(设置 TEST_DATABASE_URL):{_DB_ERROR}",
)


def _redis_error() -> str | None:
    """Celery 的 broker / result backend 连得上返回 None,否则返回原因。"""
    from urllib.parse import urlparse

    url = os.environ.get("CELERY_BROKER_URL") or os.environ.get(
        "REDIS_URL", "redis://localhost:6379/0"
    )
    parsed = urlparse(url)
    if parsed.scheme not in {"redis", "rediss"}:
        # 换了别的 broker(memory:// 之类)就不是这条守卫管的事
        return None
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 6379), timeout=2
        ):
            return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- Redis 必须点名
#
# **A42 撞过这个坑,报错完全不指向 Redis。** `task_always_eager=True` 不等于
# 不需要 Redis:Celery 仍然要构造 result backend。缺它的时候 `.delay()` 抛的是
#
#     AttributeError: 'NoneType' object has no attribute 'Redis'
#
# 而这个异常被 `_deliver` 吞进 outbox,任务安静地停在 `CREATED` ——
# 表现是**一批看不懂的失败**(有库无 Redis 时 24 条,比缺陷本身还多)。
# 从那 24 条里没有任何一条会让人想到 Redis。
#
# 所以这里和上面那条数据库守卫是同一个形状、同一个理由:CI 里
# 「跑不了」必须当场说出跑不了的是什么,而不是让人去日志里考古。
# 判据也一致 —— 本机开发不设 CI 就只是跳过,不逼人先装一个 Redis。
_REDIS_ERROR = _redis_error()

if _REDIS_ERROR is not None and _database_required():
    raise RuntimeError(
        f"CI 环境要求可连接的 Redis(Celery broker / result backend),但连不上:"
        f"{_REDIS_ERROR}\n"
        "注意 `task_always_eager=True` **不能**代替它:eager 只跳过投递,"
        "result backend 照样要构造。\n"
        "缺它的表现不是一条清楚的报错,是生成链路整批任务静默停在 CREATED —— "
        "所以这里提前拦住并点名。\n"
        "CI 里加一个 redis 服务并设 REDIS_URL 即可;本机开发不设 "
        "CI/REQUIRE_TEST_DATABASE 时会正常跳过。"
    )

# **这里刻意没有配一个 `requires_redis` 标记。**
#
# 上面这条守卫只在 CI 里生效,所以「本机有库、没 Redis」那种组合仍然会得到
# 那 24 条看不懂的失败。要治它得把标记逐条贴到真正依赖 Celery 的用例上,
# 而哪些用例依赖它**需要在真库上跑一遍才知道** —— 靠读代码猜出来的一份名单,
# 贴多了会把该跑的用例静默跳过,那比现在更糟。
#
# 所以这件事留给第一次真库全跑:那一次能同时拿到「缺 Redis 会红的用例」
# 的准确集合。在那之前,本条守卫至少让 CI 这一侧点得出名字。


@pytest.fixture(scope="session")
def engine():
    """测试库用 **alembic upgrade head** 建,不用 ``create_all()``。

    这一条是上一轮评审第 20 条的直接后果:部分唯一索引
    (``uq_prompt_templates_one_active``、``uq_output_assets_product_purpose_enabled``)
    当时只存在于迁移里,``create_all()`` 建出来的库没有它们 ——
    于是并发冲突在测试里永远复现不出来,而生产环境会 500。

    索引现在已经补进 ORM 的 ``__table_args__``,但**建库仍然走迁移**:
    那才是生产库真正的形状。迁移和 ORM 哪天再次分叉,这里会当场发现,
    而不是等到上线。顺带也验证了迁移链本身跑不跑得通。
    """
    from alembic import command
    from alembic.config import Config

    # 在发出第一条 DROP 之前确认这个地址真的是测试库。
    # 放在夹具里而不是模块顶层:只有真的要建库时才需要这个确认,
    # 纯逻辑用例不该因为没设环境变量就跑不起来。
    _assert_safe_to_wipe(TEST_DB_URL)

    eng = create_engine(TEST_DB_URL, connect_args=TEST_CONNECT_ARGS)
    with eng.connect() as conn:
        # 钉子生效了吗 —— **当场问一次库,不靠"参数传了就等于生效了"。**
        # 传错键名、驱动不认、或者哪天有人把 connect_args 覆盖掉,都会让
        # 上面那段注释变成一句不成立的话,而后果是无声的(见 TEST_CONNECT_ARGS)。
        # 建库是每次真库运行的第一步,失败发生在这里,读到的是原因而不是
        # 十几条"回收器没回收"。
        tz = conn.execute(text("SHOW TimeZone")).scalar_one()
        if str(tz).lower() not in {"utc", "etc/utc"}:
            raise RuntimeError(
                f"测试库会话时区是 {tz!r},不是 UTC —— 连接上的 "
                f"{TEST_CONNECT_ARGS} 没有生效。\n"
                "naive UTC 值写进 timestamptz 会被按这个时区解释,于是"
                "「DB 侧 now() 摆现场 + Python naive 判定」的用例会无声地"
                "判错(退避、租约、卡死回收全在其中)。\n"
                "先修连接参数,不要去改被诬告的业务代码。"
            )
        # 上一次跑剩下的东西全部清掉,包括 alembic_version ——
        # 留着它 upgrade 会以为已经是最新的,直接建出一个空库
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        conn.commit()

    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(config, "head")

    yield eng

    with eng.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
        conn.commit()
    eng.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    """每个用例一个事务,结束回滚,用例之间互不干扰。

    ## `join_transaction_mode="create_savepoint"` 不是可选项(A42)

    被测代码里到处是**真的** `session.commit()` 与 `session.rollback()`,
    而且它们的配对是有语义的 —— 例如 `generation_tasks._claim()`:
    抢不到任务时 `rollback()` 丢掉这一次的 UPDATE,抢到了就 `commit()`。

    不开 savepoint 时,那个 `rollback()` 会一路回到**用例开头**:
    商品、任务、派发意图全没了,而调用栈上层还握着那些行的 ORM 对象,
    下一次 `flush()` 就是
    `ObjectDeletedError: Instance '<TaskDispatch>' has been deleted`。

    上一版的处置是在 `celery_eager` 里把 `commit` 换成 `flush`,
    那让问题更严重:**什么都没真正提交,于是任何一次 rollback 都是全清。**
    A42 之前 `tests/test_api_reviews.py` 14/17 红,根子就在这里。

    开了 savepoint 之后:`commit()` 释放保存点(数据留在外层事务里)、
    `rollback()` 只回到保存点,而 teardown 的 `transaction.rollback()`
    照样把整个用例清干净。**被测代码的事务语义因此是真的,不是被夹具改写过的。**
    """
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection,
        expire_on_commit=False,
        # 见 docstring:少了这一行,应用代码的 rollback() 会回到用例开头
        join_transaction_mode="create_savepoint",
    )
    db = factory()
    try:
        yield db
    finally:
        db.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def storage(tmp_path):
    from app.services.storage import LocalObjectStorage

    return LocalObjectStorage(tmp_path, "http://testserver")


@pytest.fixture
def client(session, storage):
    from fastapi.testclient import TestClient

    from app.api.deps import db_session
    from app.api.products import get_storage
    from app.main import app

    app.dependency_overrides[db_session] = lambda: session
    app.dependency_overrides[get_storage] = lambda: storage
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def celery_eager(monkeypatch, session, storage):
    """让 Celery 同步执行,并让 worker 复用测试事务。

    生产里 worker 有自己的 SessionLocal;测试里必须共享同一个会话,
    否则 worker 看不到还没提交的测试数据。
    """
    import app.services.storage as storage_module
    import app.tasks.attribute_tasks as attribute_tasks
    import app.tasks.generation_tasks as gt
    from app.tasks.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False

    class _SessionProxy:
        def __call__(self):
            return session

    monkeypatch.setattr(gt, "SessionLocal", _SessionProxy())
    monkeypatch.setattr(attribute_tasks, "SessionLocal", _SessionProxy())
    monkeypatch.setattr(gt, "build_storage", lambda *a, **k: storage)
    monkeypatch.setattr(storage_module, "build_storage", lambda *a, **k: storage)
    # `close` 仍要挡住:任务的 `finally` 会关掉这个 session,而用例后面还要用它。
    #
    # **`commit` 不再换成 `flush`。** 换掉之后什么都没真正提交,于是应用代码里
    # 任何一次 `rollback()` 都变成"回到用例开头" —— 而 `_claim()` 抢不到任务时
    # 就会 rollback。eager 模式下 `deliver_pending` 会嵌套(任务在它的循环里内联
    # 跑完,又去派发下一轮,内层看到的还是同一条 PENDING),于是同一个任务被投两次,
    # 第二次必然抢不到、必然 rollback。真库上那是 14 条红。
    # 现在 `session` 夹具跑在 SAVEPOINT 上,真 commit / 真 rollback 都成立。
    monkeypatch.setattr(session, "close", lambda: None)
    yield
    celery_app.conf.task_always_eager = False


@pytest.fixture
def admin_client(client, monkeypatch):
    """带管理口令的客户端。

    读接口现在也要身份(评审第 1 条),所以这个夹具不只服务写操作 ——
    没有它,production 环境下连一次列表都拉不到。admin 天然包含 operator。

    显式设定 APP_ENV 和 ADMIN_TOKEN,不依赖开发机上的 .env ——
    否则这批测试在 APP_ENV=local 的机器上会因为免口令而通过,
    在 CI 上又会因为要口令而失败,两边都不能说明守卫是对的。
    """
    from app.api import deps

    monkeypatch.setattr(deps.settings, "APP_ENV", "production", raising=False)
    monkeypatch.setattr(deps.settings, "ADMIN_TOKEN", "test-admin-token", raising=False)
    client.headers.update({"X-Admin-Token": "test-admin-token"})
    return client


@pytest.fixture
def guarded_client(client, monkeypatch):
    """不带口令、且处在必须要口令的环境里。用来断言守卫真的挡得住。"""
    from app.api import deps

    monkeypatch.setattr(deps.settings, "APP_ENV", "production", raising=False)
    monkeypatch.setattr(deps.settings, "ADMIN_TOKEN", "test-admin-token", raising=False)
    return client


# ---------------------------------------------------------------- 浏览器登录
#
# 上面两个夹具(admin_client / guarded_client)走的是 **Legacy Header Token**,
# 本轮**原样保留** —— §37 要求那条路继续为 CLI / 脚本 / pytest 工作,而这两个
# 夹具正是它唯一的回归保护。把它们改成 Session 版等于把那条路的覆盖删掉,
# 而删掉的覆盖不会自己叫。
#
# 下面两个是新增的浏览器路线。


#: 测试用的两个密码。**必须互不相同** —— Settings._check_browser_auth 拦这件事,
#: 而如果夹具自己用了相同的密码,"两个角色形同一个"这个缺陷在测试里就不可见了。
BROWSER_ADMIN_PASSWORD = "test-admin-password"
BROWSER_OPERATOR_PASSWORD = "test-operator-password"


@pytest.fixture
def browser_auth(monkeypatch):
    """把浏览器登录配上,并把环境设成"必须要凭据"。

    ## 为什么打模块属性而不是设环境变量

    `settings` 是 `@lru_cache` 的 `get_settings()` 产出的**模块级单例**,
    进程起来时就构造完了。这时候再设 `os.environ` 一个字都不会生效。
    `deps.settings` / `auth.settings` / `config.settings` 是同一个对象,
    所以打其中任意一个即可 —— admin_client 一直是这么写的,照抄那个惯例。

    ## 这里**不**配 ADMIN_TOKEN / OPERATOR_TOKENS

    刻意的:这就是本轮之后最常见的部署形态(只有浏览器登录,没有机器凭据)。
    `rejection()` 对这种形态必须回 401「未登录」而不是 403「服务器没配口令」,
    而只有在夹具里把 Legacy Token 也留空,那条路径才走得到。
    """
    from app.api import deps

    monkeypatch.setattr(deps.settings, "APP_ENV", "production", raising=False)
    monkeypatch.setattr(
        deps.settings, "ADMIN_PASSWORD", BROWSER_ADMIN_PASSWORD, raising=False
    )
    monkeypatch.setattr(
        deps.settings, "OPERATOR_PASSWORD", BROWSER_OPERATOR_PASSWORD, raising=False
    )
    monkeypatch.setattr(deps.settings, "ADMIN_TOKEN", "", raising=False)
    monkeypatch.setattr(deps.settings, "OPERATOR_TOKENS", "", raising=False)
    return deps.settings


def _login(client, username: str, password: str):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"{username} 登录失败:{resp.status_code} {resp.text}"
    return resp


@pytest.fixture
def session_admin_client(client, browser_auth):
    """已经以 admin 身份登录的浏览器。

    TestClient 自带 cookie jar,所以登录之后后续请求会自动带上 Session Cookie ——
    不需要(也**不应该**)手工把 Cookie 抄进请求头:手抄会绕过 Set-Cookie 的
    属性(HttpOnly / SameSite / max-age),于是那些属性错了测试也不会知道。
    """
    _login(client, "admin", BROWSER_ADMIN_PASSWORD)
    return client


@pytest.fixture
def session_operator_client(client, browser_auth):
    """已经以 operator 身份登录的浏览器。"""
    _login(client, "operator", BROWSER_OPERATOR_PASSWORD)
    return client


# ---------------------------------------------------------------- 会话末尾复查


@pytest.fixture(autouse=True)
def _clean_login_throttle() -> Iterator[None]:
    """每条用例开始前清空登录失败计数(a51)。

    `api/auth.py` 那张表是**模块级**的,而 TestClient 的对端恒为
    `testclient` —— 也就是说整个进程里所有用例的失败登录都堆在同一个键上。
    不清的话,`test_auth_session.py` 里那几条故意登错的用例会把计数堆过阈值,
    然后**后面所有登录用例一起变成 429**。

    表现会非常难查:失败的不是制造失败的那几条,而是排在它们后面的;
    单独跑每一条都是绿的,全量跑才红,顺序一变红的还是另一批。
    """
    from app.api.auth import reset_login_throttle

    reset_login_throttle()
    yield
    reset_login_throttle()


@pytest.fixture(scope="session", autouse=True)
def _no_key_file_left_in_the_working_tree() -> Iterator[None]:
    """跑完之后确认仓库树里没有多出主密钥文件。

    上面的环境变量是**预防**,这条是**验尸**。两条都要有:预防依赖
    「没有别的代码路径会绕过它」,而那是个只能被证伪、不能被证实的假设。
    真绕过了的话,这条会当场点名——而不是等到某次交付前才被
    `tools/verify_delivery.py` 发现,那时候文件已经在包里了。
    """
    repo_secrets = Path(__file__).resolve().parents[2] / ".secrets"
    existed_before = repo_secrets.exists()
    yield
    if not existed_before and repo_secrets.exists():
        leaked = sorted(p.name for p in repo_secrets.iterdir())
        raise AssertionError(
            f"测试跑完之后仓库树里多出了 {repo_secrets},内容:{leaked}。\n"
            "有代码路径绕过了 conftest 顶部的密钥隔离——先找到它,"
            "再决定是补隔离还是改那段代码。"
        )
