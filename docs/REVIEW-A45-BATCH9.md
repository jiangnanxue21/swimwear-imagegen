# 第九批：Celery 任务的故障恢复语义（自查发现，不在任何一份评审里）

> 纯逻辑回归 **1658 → 1661，失败数恒为 2**；`verify_imports` 310 文件全通；`verify_delivery` 13/13。

## 一、发现过程

上一轮回答"这些问题还在吗"时，为了核对表格第三行（**任务失败状态写库失败，
但 Celery 仍确认消息**），我把全部 8 个 Celery 任务的装饰器扫了一遍：

```
run_generation_task    autoretry=True     ← 早就修过，注释写得很清楚
run_batch_task         autoretry=False    ← 一模一样的形状，漏了
deliver_outbox         autoretry=False
poll_listings          autoretry=False
relay_dispatches       autoretry=False
reap_stalled           autoretry=False
reap_batch_leases      autoretry=False
refresh_stale_drafts   autoretry=False
ping                   autoretry=False
```

**A44 与 A45 两份评审都没有这一条。** 它是"同一个形状在另一个文件里"——
本轮反复出现的那类漏网（#1 的 admin 头、#7 的 nginx location、#4 的 saveBlob），
只是这次跨的是模块。

## 二、只改了一个任务，其余七个是**正确的**

### `run_batch_task`：必须自己重试

`task_acks_late=True` 的语义是"执行完成后 ack"，而**正常返回也算完成**。
原来那个 `except Exception` 把 `OperationalError` 一起吞了、照常返回 —— 消息
就此消失。而那一刻：

```
已领取的条目停在 RUNNING，带着 1800 秒的租约
reap_batch_leases 要等租约到期才回收（节拍 60 秒，但到不到期由 lease_until 决定）
→ 最长半小时里，这个批次在界面上看起来是活的
```

与 `run_generation_task` 注释里描述的形状**一字不差**。

改法有两处，缺一不可：

1. 装饰器加 `autoretry_for=(OperationalError,)`（业务失败仍然不重试——
   `max_retries=3` 只对 `autoretry_for` 里那一个生效）；
2. **`except OperationalError` 必须排在 `except Exception` 前面并原样抛出**。
   `autoretry_for` 只能看见抛出函数的异常，被兜底接住的话装饰器永远不触发——
   只改装饰器等于什么都没做。这是最容易做错的地方，单独钉了一条断言。

重投是安全的，理由模块头本来就写着：`run_batch` 只捞 PENDING 条目，
真撞上并发时 `_execute` 的 advisory lock 会让后来者拿不到锁，不会重复付费。

### 六个节拍任务：**下一拍就是它们的重试**

`relay_dispatches`（30s）· `reap_stalled`（300s）· `reap_batch_leases`（60s）·
`refresh_stale_drafts`（600s）· `deliver_outbox` · `poll_listings`。

给它们加 `autoretry` 会在已有节拍之上再叠一层重试队列——库恢复的那一刻，
一批堆积的重试和一次正常节拍会同时到达。**不加是对的，但原来没写为什么**，
于是它看起来像遗漏。已在 `celery_app.py` 的 beat 表上方写明。

### `ping`：不属于任何一类

健康探针没有要落库的状态，失败就是失败本身。门禁里显式豁免。

## 三、真正的产出是那条门禁

> **每个 Celery 任务要么自己重试，要么在 beat 里。两者都没有的，就是一颗静默失败的地雷。**

这条规则把"节拍任务不加 autoretry"从一个看不出来的遗漏，变成一个**被记录且被检查**
的豁免。新增任务时忘了想这件事，它会红。

三条断言全部拿改动前的形态验过会红。

## 四、这一批没有引入新的验证缺口

改的是重试语义，不是查询或数据。但 **D-01 需要一条**：真库上拔掉数据库连接，
确认批次任务会重试而不是静默返回——这正是这条修复唯一能被证伪的方式。
