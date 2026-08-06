"""时间。**新代码的「现在几点」一律走这里。**

标题曾经写的是「全仓只有这一份『现在几点』」。**那句话不成立**,而且它是
本文件第一行 —— 读到这里就停下的人拿走的正是那个错误结论。
现状与影响评估见下面的「收敛没有做完」一节。

## 要修的那件事

在此之前全仓有 33 处各写一遍 `utc_now()`,
而好几个模块的注释把它总结成了一句**不成立**的话::

    publish_service._now()   \"\"\"全仓统一:库里存 naive UTC。\"\"\"
    workbench/platform.py    \"\"\"库里存的是 naive UTC
                                (platform_status_at 等一律 .replace(tzinfo=None))\"\"\"

库里存的**不是** naive。`db/base.py` 的 `TimestampMixin` 和全部业务时间列
都是 `DateTime(timezone=True)`,也就是 PostgreSQL 的 `timestamptz` ——
`app/models/` 下没有一列是 naive 的。

这个错配今天不出事,靠的是运气:naive 值发给 `timestamptz` 时,PostgreSQL
**按会话的 TimeZone 参数解释它**。官方 postgres 镜像默认 UTC,于是正好对上。
换一台 `timezone=Asia/Shanghai` 的库(自建、云托管、或者谁改了
postgresql.conf),所有业务时间整体偏 8 小时:

    退避 next_attempt_at    立刻到期 -> 退避形同虚设
    租约 lease_until        提前失效 -> 两个 worker 同时接管
    卡死判定 updated_at     误判 -> 正在跑的任务被回收成 FAILED

而这一切**不报错**。

## 这一轮做了什么

两件事,缺一不可:

1. `db/session.py` 在连接上钉死 `-c timezone=utc`。这让"naive 值按什么时区
   解释"变成一个由代码决定的事实,而不是由部署环境决定的运气。
2. 把 33 处表达式收敛到 `utc_now()`。语义完全不变(仍然是 naive UTC)。

## 收敛**没有做完**(如实记录)

本模块开头那句「全仓只有这一份『现在几点』」和根 `CLAUDE.md` 硬规则 4 的
同一句话,**今天都还不成立**。全仓仍有 14 处直接 `datetime.now(UTC)`::

    workbench/platform_service.py   5 处
    channels/simulator.py           2 处
    listings/export_writer.py       2 处
    workbench/batch_service.py      1 处   ← 刻意保留,见下
    workbench/service.py            1 处
    services/spend.py               1 处
    api/workbench_batch.py          1 处
    scripts/export_parity.py        1 处

**数这个数的办法**(别手数,上一次就是手数漏的):按 AST 找
`datetime.now(...)` 调用,排除本文件自己那一处。

原来是 18 处。A34 收掉了 `batch_service.py:1007` —— 全仓**唯一**一处原样写着
下面那个被点名禁止的 `datetime.now(UTC).replace(tzinfo=None)` 的地方,
A27 那轮漏掉的那一处。A41 又收掉 `batch_service.py` 的 3 处:回执写入、
`create_batch` 与导出文件落库,它们的 `now` **全部用途都是写库**,归一到入口
语义完全不变,不需要真库验证。

`batch_service.py` 剩下的那一处(`batch_file`)是**刻意**的:它喂
`ExportMeta.exported_at`,而 `listings/export_writer.py` 直接对它 `.isoformat()`
写进导出文件。改成 naive 会让文件里丢掉 `+00:00`,而接口侧现在统一走
`iso_utc()`(永远带偏移)—— 两边又会对不上,只是方向反过来。收敛它得连
`export_writer` 一起改,那会改变已发出去的导出文件字节,必须带真库跑。

剩下 14 处是"aware 的 `now` 赋给变量、用到的地方各自归一"的形状,
收敛它们要动发布链路的事务编排,见本节末尾。

**今天不出事,但这个结论有边界,照抄之前先读完这段。**

做过两轮 AST 扫描:

    一、找「`utc_now()` 或 `datetime.now()` 与 ORM 时间列直接比较、
        且未过 `as_naive_utc`」—— 0 处命中。
    二、找「`now = datetime.now(UTC)` 之后,`now` 未经 `.replace(tzinfo=None)`
        就被继续使用」—— **6 处命中**,逐个看过:
          batch_service.py:1358 / service.py:1065   只进 strftime 做文件名,无关时区
          batch_service.py:1380 / service.py:1064   传给 `record_export(now=...)`,
                                                    它在 998 行自己 `.replace` 了
          batch_service.py:1352 / service.py:1046   进 `ExportMeta.exported_at` ——
                                                    见下面「已经出事的一处」

也就是说:**没有会抛 `TypeError` 的 naive/aware 混比路径**,写库的点全都在赋值
那一行归一了。

**这两轮扫描看不见什么**:aware 值先赋给中间变量、绕经几层函数调用之后
才参与比较的路径。第二轮只跟一跳。所以上面这句"不出事"的强度是
「静态跟一跳没发现」,不是「证明了没有」。

**已经出事的一处**:`workbench/service.py` 与 `batch_service.py` 把 aware 的
`now` 传进 `ExportMeta.exported_at`,于是导出文件里写的是 `...+00:00`;
而同一次导出写库用的是 naive(`record_export` 归一过),
`api/workbench.ts` 读出来 `.isoformat()` 没有偏移。
同一个事件,接口和文件里是两种字符串格式。

**A41 修掉了接口那一半**:所有出参时间戳统一走下面的 `iso_utc()`,永远带
`+00:00`,与导出文件里那份一致。顺带修掉的是一个更普遍的同源问题 ——
`SessionLocal` 配的是 `expire_on_commit=False`,于是"刚写完就回读"拿到的是
Python 侧的 naive 值、"刷新页面重新查"拿到的是 timestamptz 的 aware 值,
**同一列同一个接口会序列化出两种形状**。前端 `utils/datetime.ts` 两种都认,
所以它一直没有表现出来;但导出文件、批次清单和任何第三方消费者都会看见。

剩下那一半(`ExportMeta.exported_at` 本身仍然是 aware)刻意没动,理由见
上一节。收敛剩下的 14 处要动发布链路的事务编排(`platform_service`),
而那批改动的验证要跑 `requires_db` 的集成测试。**没有真库就别动** ——
这是一次单独的、要带库跑全量的改动,不是顺手能收的尾。

## 为什么没有顺手切成 aware

切成 aware 要同时改两样:列的类型语义,以及所有拿 ORM 读回来的值做 Python
比较的地方(`timestamptz` 读回来是 aware,写进去的是 naive,两者相减直接
抛 TypeError)。那是一次需要跑全量测试才敢动的改动,而这个交付包里
跑不了 pytest。所以这一轮只做"让它确定性正确"+"收敛到一个点",
把"改成 aware"留成一个后续的、一行的决定。
"""
from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """现在,naive UTC。

    naive 是**当前**的约定,不是终点:见模块开头。所有写库的时间都从这里取,
    不要再在别处写 `datetime.now(UTC).replace(tzinfo=None)` ——
    那正是让 33 个地方各自漂移的写法。
    """
    return datetime.now(UTC).replace(tzinfo=None)


def as_naive_utc(value: datetime | None) -> datetime | None:
    """把任意 datetime 归一成 naive UTC。`None` 原样返回。

    从 ORM 读回来的 `timestamptz` 是 **aware** 的,而 `utc_now()` 是 naive。
    两者直接比较会抛::

        TypeError: can't compare offset-naive and offset-aware datetimes

    而它会发生在退避计算、租约判定这些**只在重试时才走到**的路径上 ——
    也就是最不容易在开发期撞见、最需要它别出事的路径。

    要拿库里读回来的值和 `utc_now()` 做运算时,先过这个函数。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def iso_utc(value: datetime | None) -> str | None:
    """出参里的时间戳。**永远带 `+00:00`**,`None` 原样返回。

    ## 为什么裸 `.isoformat()` 不够

    `SessionLocal` 配的是 `expire_on_commit=False`,于是 commit 之后对象保留的
    是**刚刚在 Python 里赋的那个 naive 值**,而不是回读数据库得到的 aware 值。
    同一列因此会序列化出两种形状::

        写完立刻回读(inline 执行、创建后返回)   2026-08-01T10:00:00
        刷新页面重新查(timestamptz 读回来)      2026-08-01T10:00:00+00:00

    前端 `utils/datetime.ts` 的 `HAS_TZ` 分支两种都认,所以界面上看不出来 ——
    这正是它能一直存在的原因。但导出文件、批次清单和任何直接消费这些 JSON 的
    第三方拿到的是不一致的格式,而"同一个事件两种字符串"这件事只会在对账那天
    才被发现(`core/clock.py` 开头「已经出事的一处」记的就是它)。

    归一到**带偏移**那一侧而不是去掉偏移:少了偏移的时间戳是有歧义的,
    读的人必须先知道"这个系统存的是 UTC"才能解释它,而那句话不在报文里。
    """
    naive = as_naive_utc(value)
    if naive is None:
        return None
    return naive.replace(tzinfo=UTC).isoformat()
