"""数据库引擎与会话工厂(同步 SQLAlchemy 2)。"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.sqlalchemy_url,
    pool_pre_ping=True,
    future=True,
    echo=False,
    # 会话时区钉死 UTC。**这不是一个偏好设置,是一个正确性前提。**
    #
    # 全部时间列都是 `timestamptz`(`db/base.py`),而我们写进去的是 naive
    # (`core/clock.utc_now()`)。naive 值发给 timestamptz 时,PostgreSQL
    # 按**会话的 TimeZone 参数**解释它 —— 也就是说"这个时间是几点"
    # 取决于数据库怎么配,而不是取决于代码。
    #
    # 官方 postgres 镜像默认 UTC,所以今天是对的。换一台
    # `timezone=Asia/Shanghai` 的库,退避会立刻到期、租约会提前失效、
    # 正在跑的任务会被卡死回收器误判 —— 全部无声。
    #
    # 钉在这里而不是要求运维去改 postgresql.conf:那是一条写在文档里、
    # 迟早会被漏掉的要求,而这一行走到哪个库都成立。
    connect_args={"options": "-c timezone=utc"},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖:每请求一个会话,异常回滚。**它不提交。**

    ## 为什么这里原来有一行 `session.commit()`,现在没有了(任务 19 后半)

    §7.8 第一条禁止项就是「请求级自动 commit 与 Service commit 混用」。
    混用的后果不是多提交一次(那无害),是**事务边界没有主人**:

        接口层写着 commit          有 38 处,发布链路还刻意分三段提交
        依赖层又替所有人 commit    包括那些一行 commit 都没写的端点

    于是「这个请求到底在哪一刻落库」要看两个地方,而两边都以为是自己说了算。
    真正咬人的是读路径:自动 commit 意味着**任何一处误写都会被提交**,
    哪怕那个端点从头到尾没打算写库。评审第 19 条花了一整轮把列表、详情、
    异常、SPU、过期原因五个 GET 改成只读,a38 又加了
    `tests/pure/test_workbench_query_budget.py` 盯着 `collect()` 里的写
    必须挡在 `dry_run` 后面 —— 而这一行在下面兜着,让所有那些努力
    只差一次手滑就白做。

    ## 摘掉它安全吗:摘之前逐个端点走过调用图

    **写端点**:49 个,其中 13 个当时没有自己 commit,全靠这一行。
    那 13 个已在同一轮补上显式提交(`preview_import` 除外 —— 它是只读
    的预览 POST,本来就不该提交)。

    **读端点**:逐个 GET 走调用图找可达的会话写,只有两类,
    都不需要这一行:

        refresh_draft 的 flush     全部挡在 `dry_run` 后面,a38 的门禁盯着
        download_batch_file        唯一一处 GET 里的合法写(只增不改的
                                   审计流水),它自己显式 commit

    ## 为什么这件事必须靠门禁而不是靠测试

    `tests/conftest.py` 的 `client` 夹具把 `db_session` 覆盖成一个
    **不提交**的 session(整个用例一个事务,结束回滚)。同一个 session 里
    读得到未提交的写,所以「提交了」和「只是 flush 了」在测试里**完全等价** ——
    那 13 个端点漏不漏提交,现有测试一条都答不上来。这也是这个洞能留到
    今天的原因。守它的是 `tests/pure/test_transaction_boundaries.py`,
    不是集成测试。
    """
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
