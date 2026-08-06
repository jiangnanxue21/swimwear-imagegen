# A45-batch12-4 自审:复核上一轮的五处修改

> 基线包:`swimwear-imagegen-a45-batch12-4.zip`
> 上一轮:`docs/REVIEW-A45-BATCH12-4-RESPONSE.md`
> 本轮性质:**不是新一轮修复,是对 batch12-4 自己的复核**

---

# 0. 结论

五处修改(REG-01 ~ REG-05)**代码本身没有问题**,复核逐条确认了修法成立。

但上一轮新增的真库测试里有两条测不到它声称要测的东西,另外发现两处
本轮修改带出来的连带问题。三条都已修,守卫已补。

`docs/REVIEW-A45-BATCH12-4-RESPONSE.md` §0 那句话仍然原样成立:
**batch12-4 仍然不是正式人工测试基线**,真库测试本轮同样没有跑过。

---

# 1. 复核通过的部分

## 1.1 离线门禁复跑

| 门禁 | 结果 |
|---|---:|
| 后端纯逻辑测试 | 1843 / 1845 |
| 交付门禁 verify_delivery | 13 / 13 |
| `app.*` 导入检查 | 326 文件 |
| 样例数据检查 | 5 / 5 |
| `compileall` | 通过 |

那 2 条失败仍然是同一个成因:容器无网络装不上 `pydantic`,用例在 import
期就断了。与 batch12-3 §2.1、batch12-4 §1 记的口径一致。

## 1.2 五处修改逐条确认

* **REG-01** `all_downloads_failed = bool(provider_count) and not stored` 的判据
  正确,该分支在 `_empty_round()` 之前返回。`billable_units=max(provider_count, 1)`
  在 `provider_count == 0` 时算 1 单位,与 `record_usage` 文档里「失败调用也
  至少记 1」的口径一致 —— 不是漏掉的边界。
* **REG-02** 两个新常量不相交、并集等于原 `SPENT_STATUSES`;`SPENT_STATUSES`
  唯一的旧调用点(`_reap_batch` 的 `spent_only=False` 排除)语义没变;
  四批与 `STALLABLE_STATUSES` 的交叉不会重复回收同一行。
* **REG-03** 保存点位置对。`_persist_candidates()` 内部没有任何 `commit()`
  (`shadow_from_candidate` 也只 `flush`),保存点是干净的。
  `_drop_stray_pending_rows()` 在 `_finish_attempt` / `record_usage` **之前**
  调用,不会误摘收尾自己要写的行。迁移 0033 的约束名与模型
  `__table_args__` 一致;全库只有 `_persist_candidates` 一处写这张表。
* **REG-04** `begin_nested()` 的预 flush 确实发生在 SAVEPOINT 语句之前,
  把 `add` + `flush` 一起关进去这个修法成立。`except` 里回读不到赢家就
  原样抛,也对。
* **REG-05** `_ACTIVE_OUTBOX` 一票否决 + `_display()` 的「修正一」只把
  `PENDING` 转 `QUEUED`、`LEASED` 留在 `IN_FLIGHT`,两者都在 `hard_stops`
  里 —— SUBMIT 按钮确实关得住。

---

# 2. 本轮修的三条

## 2.1(高)REG-03 的真库用例注入点打错了地方

### 问题

`tests/test_a45_batch12_4_recovery_db.py` 的两条 REG-03 用例都靠
monkeypatch `gt._load_bytes`、在第二张候选上抛 `RuntimeError` 来"模拟落库崩溃"。

但 `_load_bytes` 的调用点在 `_persist_candidates()` **每张图那个
`try / except Exception` 里面**。异常会被就地吞掉、把该行标成
`DOWNLOAD_FAILED`,根本冒不出 `_persist_candidates()` —— 保存点一次都不会回滚。

实际结果是 3 张里 2 张成功、`stored` 非空、任务一路走到 SCORING,于是:

* `test_a_crash_mid_persist_leaves_zero_orphaned_candidates`
  的 `assert detail["status"] == "FAILED"` 会红,`remaining` 是 3 不是 0
* `test_resuming_after_a_persist_crash_does_not_duplicate_candidates`
  的 `assert error_code == PROVIDER_RESULT_PENDING_CODE` 会红

也就是说,这两条即使拿到 PostgreSQL 环境上跑,红的也不是被测代码,
而是用例自己的前提。上一轮报告 §4 把它们列为「REG-03 的真库行为验证」,
那个说法当时不成立。

### 改法

注入点换成 `media_service.shadow_from_candidate` —— 用例自己的 docstring
写的就是这个("影子写失败")。它在逐张 `try` 之外,且发生在**所有候选行
`flush()` 之后**,正是"候选已经写进事务、然后崩了"的形状,也就是
`_abandon_attempt()` 那句会话级 `commit()` 会把它们一起提交的那批行。

### 代码位置

* `backend/tests/test_a45_batch12_4_recovery_db.py`:两条 REG-03 用例。

### 离线门禁覆盖

`tests/pure/test_a45_batch12_4_fixes.py::test_the_recovery_db_tests_inject_the_crash_outside_the_per_image_try`
—— AST 检查这两条用例的函数体(跳过 docstring,那里**特意**点了
`_load_bytes` 的名字讲为什么不能打在那儿)。

这条守卫的意义在于:那两条用例要等有 PostgreSQL 的环境才跑得了,
而一个打错位置的注入点在那之前没有任何征兆。

## 2.2(中)续跑成功之后,attempt 上留着上一次的失败字段

### 问题

`_finish_attempt()` 只在 `error is not None` 时写 `error_code` /
`error_message` / `regeneration_reason`,**成功时不清空**。

这个函数是按"一条 attempt 只收尾一次"写的 —— 成功路径上那三列本来就是空的,
不写等于写空。而 REG-01 / REG-02 / REG-03 各开了一条"同一条 attempt 再跑
一遍"的续跑路径,那个前提就没了:

    status              SUCCEEDED
    error_code          PROVIDER_RESULT_PENDING
    error_message       Provider 返回了 3 个候选,全部下载失败
    regeneration_reason DOWNLOAD_FAILED

三列都经 `AttemptOut` 原样回到接口,运营看到的是一条"成功但写着全部下载
失败"的记录。`_abandon_attempt()` 写下的 `INTERNAL_ERROR` 同理。

这一轮修复的整个目的就是让人能相信界面上那句话,而这条记录说的是
两件互相矛盾的事。

### 改法

`_finish_attempt()` 在 `status is AttemptStatus.SUCCEEDED` 时把这三列显式
置 `None`。位置在 `if error is not None` **之前** —— 反过来写的话,
失败路径刚写下的三列会被紧接着抹掉,一次失败就彻底没有原因了。

`_await_and_collect()` 会在 `_finish_attempt(SUCCEEDED)` 之后补写
`regeneration_reason`(Provider 一张都没返回那条),那次赋值因此留得住。
`repair_strategy` 不清:它只在 `_score_and_decide()` 里写,而那一步在
所有续跑路径之后,不会出现跨轮残留。

### 代码位置

* `backend/app/tasks/generation_tasks.py`:`_finish_attempt()`。

### 离线门禁覆盖

`::test_a_successful_attempt_clears_the_previous_rounds_failure_columns`
—— 三列都要清,且清空必须排在失败赋值之前。

### 真库覆盖(未执行)

`test_resuming_after_a_persist_crash_does_not_duplicate_candidates`
末尾新增四条断言:续跑成功后 attempt 的三列为 `None`、状态为 `SUCCEEDED`。

## 2.3(低)续跑拿回来的候选变少时,尾巴上那几行会滞留

### 问题

`_persist_candidates()` 按 `candidate_index` 认领已有行,但只遍历**这一遍**
的候选。上一遍存了 3 行、这一遍 `fetch_results()` 只返回 2 个的话,
index 2 那一行既不会被改写、也不会进 `stored` —— 它以 `DOWNLOAD_FAILED`
的样子永远留在库里,界面上就是一张查不出所以然的失败候选。

Mock 一次返回固定张数,不会出现这个形状;分批提交的 provider
(FASHN 是一次一张凑够张数)在部分子任务失效时会。

### 改法

新增 `_drop_shrunk_tail()`,在落库循环之后、`flush()` 之前清掉
`index >= len(candidates)` 的行。

**只删还没成功的那些**:`PENDING` / `DOWNLOAD_FAILED` 的行没有字节、
没有影子素材、更不可能有评分,删掉不牵连任何东西。`DOWNLOADED` 的不删 ——
那是一张真的存在于我们自己存储里的图,而 `evaluations` 是
`ON DELETE CASCADE`。这种形状目前没有已知成因,出现就说明有别的地方错了,
记一条 `logger.error` 让人来看,比替人做决定安全。

判据与迁移 0033 报错信息里那句「不要直接删」是同一条理由。

### 代码位置

* `backend/app/tasks/generation_tasks.py`:`_drop_shrunk_tail()`(新增)、
  `_persist_candidates()` 的调用点与 docstring。

### 离线门禁覆盖

`::test_a_shrinking_result_set_does_not_leave_orphan_rows`。

---

# 3. 三条守卫都验过会红

按 batch12-4 §1 的做法,逐条把修复换回旧版确认守卫真的会红:

| 守卫 | 换回旧版之后 |
|---|---|
| `test_a_successful_attempt_clears_the_previous_rounds_failure_columns` | 红 |
| `test_a_shrinking_result_set_does_not_leave_orphan_rows` | 红 |
| `test_the_recovery_db_tests_inject_the_crash_outside_the_per_image_try` | 红 |

---

# 4. 仍未关闭的事

上一轮报告 §4 那四条**原样成立**,一条都没有因为本轮而关闭:

1. `tests/test_a45_batch12_4_recovery_db.py` 仍然没有跑过。本轮只是把其中
   两条的注入点修对,让它**将来跑起来时红的是被测代码而不是用例前提**。
2. REG-04 仍然没有并发测试。
3. REG-05 的 `redeliver_dead()` → `build_view()` 端到端仍然没有真库测试。
4. 前端 `npm ci`、PostgreSQL / Redis 集成测试、Ruff、import-linter 本轮
   同样未执行 —— 本轮没有碰 `frontend/`。

---

# 5. 下一轮的顺序

与上一轮 §5 相同,第一步不变:

```
1. 在有 PostgreSQL 的环境里跑 tests/test_a45_batch12_4_recovery_db.py
2. 跑真 PostgreSQL 事务/唯一约束/回滚测试
3. REG-04 补并发测试
4. 前端门禁
5. 到那时候再谈是否可以关闭 batch12-3 / 冻结正式人工测试版本
```
