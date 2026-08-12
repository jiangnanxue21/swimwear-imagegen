"""连接池容量与同步端点并发上限必须是一组(a51)。

## 这条守的是什么

a51 之前两侧各走各的默认值,而它们互相不知道对方存在:

    同步端点并发   anyio 线程池默认 40
    连接池容量     SQLAlchemy 默认 pool_size 5 + max_overflow 10 = 15

于是 40 个并发请求抢 15 条连接,25 个在池上排队 30 秒然后
`QueuePool limit of size 5 overflow 10 reached`。两边都"用了默认值",
所以两边看起来都没错 —— 这类缺陷只有把关系写成断言才拦得住。

## 为什么钉的是默认值,不是运行时

运行时的值可以被环境变量改,而**故意**把池调得比线程池小是合法的
(拿池当背压)。`main.py` 的 lifespan 对那种组合打一条 warning,不拦启动。

这里钉的是**仓库自带的那组默认值**要自洽:一个没配任何环境变量就跑起来的
部署,不该在中等并发下撞池。改其中一个而不改另一个,这条会红。
"""
from __future__ import annotations

import ast
from pathlib import Path

_CONFIG = Path(__file__).resolve().parents[2] / "app/core/config.py"
_SESSION = Path(__file__).resolve().parents[2] / "app/db/session.py"


def _defaults() -> dict[str, int]:
    """从源码里读默认值,不导入 pydantic —— `tests/pure/` 是零三方依赖跑的。"""
    tree = ast.parse(_CONFIG.read_text(encoding="utf-8"))
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        name = node.target.id
        if name not in {
            "DB_POOL_SIZE",
            "DB_MAX_OVERFLOW",
            "DB_POOL_TIMEOUT_SECONDS",
            "SERVER_THREADPOOL_SIZE",
        }:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
            found[name] = node.value.value
    return found


def test_every_knob_exists():
    """四个配置项都在。少一个,下面那条不变量就无从谈起。"""
    missing = {
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_POOL_TIMEOUT_SECONDS",
        "SERVER_THREADPOOL_SIZE",
    } - set(_defaults())
    assert not missing, f"这些连接池/并发配置项不见了:{sorted(missing)}"


def test_the_default_pool_can_serve_every_sync_request_slot():
    """**默认池容量 >= 默认线程池容量。**

    小于的话,超出的那部分请求会在池上等满 `pool_timeout` 然后 500,
    而那个 500 的现场不会提到"池比线程池小"一个字。
    """
    d = _defaults()
    capacity = d["DB_POOL_SIZE"] + d["DB_MAX_OVERFLOW"]
    assert capacity >= d["SERVER_THREADPOOL_SIZE"], (
        f"池容量 {capacity} < 同步端点并发上限 {d['SERVER_THREADPOOL_SIZE']} ——"
        " 两个数是一组,调一个要调另一个"
    )


def test_the_engine_actually_reads_all_four_knobs():
    """配置项存在 != 引擎用了它。

    这条钉的是**接线**:四个 setting 里任何一个没被 `create_engine` 读到,
    它就是一个"改了没反应"的旋钮 —— 而那比没有旋钮更糟,因为运维会以为
    自己已经调过了。
    """
    source = _SESSION.read_text(encoding="utf-8")
    for expected in (
        "pool_size=settings.DB_POOL_SIZE",
        "max_overflow=settings.DB_MAX_OVERFLOW",
        "pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS",
        "pool_recycle=settings.DB_POOL_RECYCLE_SECONDS",
    ):
        assert expected in source, f"db/session.py 没有把 {expected} 传给 create_engine"


def test_the_threadpool_limit_is_actually_applied():
    """`SERVER_THREADPOOL_SIZE` 必须真的被设到 anyio 的限流器上。

    只加配置不接线的话,实际并发仍然是 anyio 的默认 40,而配置项会让人
    以为自己已经把它降到 30 了 —— 一个说谎的旋钮。
    """
    main = (Path(__file__).resolve().parents[2] / "app/main.py").read_text(encoding="utf-8")
    assert "current_default_thread_limiter().total_tokens = settings.SERVER_THREADPOOL_SIZE" in main
