"""事务级 advisory lock(评审第 6、7 条)。

## 这把锁解决的是同一类问题

    版本号     `查当前最大值 + 1` —— 读-改-写
    唯一批准   `先归档旧的,再批准新的` —— 读-改-写

两者都不是原子的。并发请求会各自读到同一个"当前值",然后:

    文案 / 图片集版本   算出同一个版本号 -> 撞唯一约束 -> 用户看到 500
    图片集批准          各自归档对方之前读到的旧版 -> 同 scope 留下两个 APPROVED

第二个更糟:它不报错。库里安静地留着两版"已批准",而 `resolve_for_publish()`
按版本号取最高的那一版 —— 于是"回滚到旧版"这个动作有一半概率不生效,
且没有任何地方能看出为什么。

## 为什么是 advisory lock 而不是行锁

要串行的这段逻辑跨越多行、而且第一步是"查还不存在的那一行的版本号" ——
没有一行可以锁。锁一个由业务键算出来的数字是唯一可行的做法。

事务级(`pg_advisory_xact_lock`)在提交或回滚时自动释放,不需要显式解锁,
也就不会因为某条异常路径漏掉解锁而把整个 scope 锁死。

## 锁不是唯一防线

拿不到锁的路径(迁移脚本、手工 SQL、别的服务)照样能写进来。所以数据库层
仍然需要唯一索引兜底,调用方仍然需要在保存点里捕获冲突重试。锁负责让
**正常并发**不产生 500,索引负责让**异常写入**不产生脏数据。
"""
from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

#: 冲突重试次数。锁之外还留一层,理由见模块开头最后一段。
MAX_LOCK_RETRIES = 3


def advisory_key(namespace: str, *parts: Any) -> int:
    """把一组业务键映射成 advisory lock 用的 64 位整数。

    用 sha256 而不是 PostgreSQL 的 `hashtext()`:后者的算法在大版本之间变过,
    升级数据库会让锁的取值整体漂移 —— 升级过程中新旧进程各自锁在不同的数字上,
    等于没锁,而且完全没有征兆。自己算就跟数据库版本无关了。

    `None` 和空串必须能区分:`(spu, None, None)` 是"通用集",
    `(spu, "", "")` 是渠道字段被填了空串,两者在库里是两条不同的记录。
    所以用 `repr()` 而不是 `str()` 拼接。

    取前 8 字节转有符号整数,正好落在 `bigint` 范围内。
    """
    payload = ":".join([namespace, *(repr(p) for p in parts)])
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def advisory_xact_lock(session: Session, namespace: str, *parts: Any) -> None:
    """按业务键上事务级 advisory lock。

    锁的粒度是传进来的这组键,不是整张表:改 `SW-001` 的文案不该挡住
    `SW-002` —— 那会让批量操作变成串行队列。

    非 PostgreSQL 后端直接跳过。这个项目只跑在 PostgreSQL 上,
    但测试夹具偶尔会用别的引擎,不该在那里炸掉。
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)").bindparams(
            bindparam("lock_key", advisory_key(namespace, *parts))
        )
    )


def try_advisory_xact_lock(session: Session, namespace: str, *parts: Any) -> bool:
    """拿不到就立刻返回 False,不排队。

    ## 为什么付费调用那条路径必须用这个,不能用上面那个

    阻塞版会等到持锁事务提交。批量执行是一个事务跑完整批,于是第二个请求
    要等第一整批(可能 50 件)跑完 —— 一次 HTTP 请求等在那里,连接池占着,
    网关超时之后用户重试,又是一个排队的请求。

    更硬的理由是**死锁**:两个批次的商品集合与顺序都可能不同,
    A 先锁 SKU-1 等 SKU-2、B 先锁 SKU-2 等 SKU-1,PostgreSQL 会检出死锁
    并杀掉其中一个事务 —— 整批回滚。非阻塞版永远不等,所以不可能死锁。

    代价是拿不到锁的那一件不会被执行,要报一个可解释的失败让人重试。
    这是刻意的取舍:**宁可让运营多点一次重试,也不能多调一次付费模型。**

    ## 事务级而不是会话级

    会话级锁可以在每件之后立刻释放,锁的持有量恒为 1,看起来更省。
    但那样是**错的**:释放锁的时候本事务还没提交,回执对别人不可见,
    第二个请求拿到锁、查不到回执、照样调一次模型 —— 竞态原样回来。

    锁必须活到"回执对别人可见"的那一刻,而那一刻就是提交。所以一个批次
    会累积 N 把锁(N = 去重后的作用域数)。默认 `max_locks_per_transaction`
    是 64,但它是共享锁表的**平均**尺寸参数而非单事务硬上限
    (总容量 = 该值 ×(max_connections + max_prepared_transactions)),
    50 件的目标批量在默认配置下有充裕余量。真要跑上千件的批次,
    应当先把批量执行拆成每件一个事务,而不是把锁去掉。
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        # 非 PostgreSQL:没有锁可用,按"拿到了"继续。
        # 这里返回 False 会让所有非 PG 环境的批量执行全部失败。
        return True
    row = session.execute(
        text("SELECT pg_try_advisory_xact_lock(:lock_key)").bindparams(
            bindparam("lock_key", advisory_key(namespace, *parts))
        )
    ).scalar()
    return bool(row)
