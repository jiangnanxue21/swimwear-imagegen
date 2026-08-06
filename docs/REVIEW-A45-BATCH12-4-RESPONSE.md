# A45-batch12-4:修 batch12-3 回归报告里的五条

> 基线包:`swimwear-imagegen-a45-batch12-3.zip`
> 本轮版本:**batch12-4**
> 上一轮:`docs/REVIEW-A45-BATCH12-3.md`(裁决:不能关闭 batch12-3)
>
> **后续复核见 `docs/REVIEW-A45-BATCH12-4-SELFCHECK.md`。**
> 本文 §2.3 与 §4.1 里说"REG-03 的真库行为验证已写好"的部分需要按那份
> 文档读:那两条用例的故障注入点原来打在 `_load_bytes` 上,而那个异常会被
> `_persist_candidates()` 逐张的 `try` 吞掉 —— 保存点根本不会回滚,
> 用例测不到它声称要测的东西。注入点已改到影子写。

---

# 0. 先说清楚这一版**不是**什么

**batch12-4 仍然不是"正式人工测试基线"。**

整改环境没有网络、没有可连接的 PostgreSQL/Redis、`npm ci` 装不了依赖 ——
与 batch12-3 §2.2 描述的局限完全相同。真 PostgreSQL 事务/唯一约束/回滚测试、
Redis/Celery 集成测试、前端 typecheck/lint/Vitest/build 本轮**仍未执行**。

因此本文档里凡是写"已修复"的条目,含义一律是:

> 代码已经改,**且被离线门禁覆盖到了**(纯逻辑断言或 AST 静态钉),
> 新增的真库测试(`tests/test_a45_batch12_4_recovery_db.py`)**写好了但没有跑过**,
> 需要在有 PostgreSQL 的环境里跑一次才能确认真的通过。

上一轮报告 §八「最终裁决」给的允许范围本轮原样适用:允许进入下一修复轮,
**不允许**关闭、**不允许**冻结正式人工测试版本。

---

# 1. 本轮实际执行的门禁

| 门禁 | 改动前(batch12-3) | 改动后(batch12-4) |
|---|---:|---:|
| 后端纯逻辑测试 | 1824 / 1824 | **1840 / 1842** |
| 交付门禁 verify_delivery | 13 / 13 | 13 / 13 |
| `app.*` 导入检查 | 324 文件 | **326 文件** |
| 样例数据检查 | 5 / 5 | 5 / 5 |
| `compileall` | 通过 | 通过 |

那 2 条纯逻辑测试失败**不是代码缺陷**,改动前后是同一个成因:本机装不上
`pydantic` / `sqlalchemy`(容器无网络),用例在 import 期就断了——与
batch12-3 报告 §2.1 记的口径一致。

新增测试:

* `tests/pure/test_a45_batch12_4_fixes.py`(16 条):结构守卫,零依赖可跑,
  已跑过。逐一把五处修复文件换回 batch12-3 旧版验证过会红:
  `generation_tasks.py` 红 6 条,`batch_service.py` 红 1 条,
  `publish_view.py` 红 2 条,`generation_service.py` + `state_machine.py`
  红到 import 失败(整个文件用到的新常量都不存在了)。
* `tests/test_a45_batch12_4_recovery_db.py`(**6 条**):跨函数行为验证,需要
  真 PostgreSQL。**本轮没有能跑它的环境,写好但未执行** —— 见 §4。

  > 更正(batch12-5 回归评审 §2.3):这里原本写的是 7 条,文件里只有 6 条。
  > 差的那一条不是业务缺陷,但它让"真库验证覆盖"被高估了一条 ——
  > 而这份文档的读者恰恰是拿它来判断"还差多少证据"的人。
  > 六条分别是:全部下载失败不重新 submit、下载恢复复用同一 attempt、
  > reaper 保留已知外部 ID、无外部 ID 进入人工对账、候选落库异常后零残留、
  > 候选落库异常后续跑不重复。
* 改写了 2 条 batch12-2 遗留测试,因为它们的断言编码的正是本轮要修的缺陷
  (细节见 §3.5)。

---

# 2. 本轮做了什么

batch12-3 报告 §一列了七条阻塞项和两条中低级问题,裁决「建议下一轮只修
五处」。本轮就做这五处,以及配套的迁移与守卫测试。

## 2.1 REG-01:区分"没有候选图"与"候选全部下载失败" —— 已修复

### 问题复述

`_persist_candidates()` 原来只返回一个 `stored` 列表。Provider 一张都没
返回、和返回了但全部下载失败,在调用方眼里长得一模一样(`stored == []`),
于是全部下载失败被 `if not stored:` 接走,直接进 `_empty_round()` —— 那条
路会转 REGENERATING 并**自动派发下一轮生成**,不需要任何人点击就重复扣费。

### 改法

`_persist_candidates()` 改为返回 `_PersistOutcome(provider_count, stored,
download_failed, reused)`。`_await_and_collect()` 里判据变成:

```python
all_downloads_failed = bool(outcome.provider_count) and not stored
```

`all_downloads_failed` 分支在 `_empty_round()` **之前**返回,走和落库失败
完全相同的出口:`_fail(session, task_id, PROVIDER_RESULT_PENDING_CODE, ...)`,
保留 `external_task_id`。只有 `provider_count == 0`(Provider 真的一张都
没返回)才会走到 `_empty_round()`。

顺带修了用量流水:`billable_units` 改成按 `outcome.provider_count` 记账,
不再用默认的 `max(candidate_count, 1)` —— 原来三张坏一张会记成两张的账,
全部下载失败记成一张,而钱是按 Provider 产出的张数收的。

### 代码位置

* `backend/app/tasks/generation_tasks.py`:`_PersistOutcome`、
  `_persist_candidates()`、`_await_and_collect()` 落库之后那一段。

### 离线门禁覆盖

`test_persist_reports_the_provider_count_separately_from_what_it_stored`、
`test_an_all_failed_download_round_does_not_reach_empty_round`、
`test_the_ledger_bills_what_the_provider_produced_not_what_we_saved`。

### 真库覆盖(未执行)

`test_a45_batch12_4_recovery_db.py::test_all_downloads_failing_keeps_the_external_id_and_does_not_resubmit`
—— 监听 mock provider 的 `submit()` 调用次数,全部下载失败之后断言仍为 1;
`::test_resuming_after_all_downloads_failed_reuses_the_same_attempt` ——
续跑成功后 attempt 数仍为 1、候选数不多不少。

---

## 2.2 REG-02:reaper 按状态 + 外部 ID 两个维度分类 —— 已修复

### 问题复述

`SPENT_STATUSES` 原来是一份不分内部结构的集合,`reap_stalled()` 只按
"有没有 external_task_id" 分两批,两批都落 `SUBMIT_RESULT_UNKNOWN`。而
`can_resume_provider_results()` 只认 `PROVIDER_RESULT_PENDING`,于是停在
`PROVIDER_RUNNING` / `DOWNLOADING`、**外部 ID 明明就在任务上**的那一批,
强制重试会清掉 ID 重新 submit —— 人是被系统逼着走上重复扣费那条路的。

### 改法

`state_machine.py` 把 `SPENT_STATUSES` 拆成两个不相交、并起来等于原清单
的子集:

```
SUBMIT_IN_DOUBT_STATUSES        = {SUBMITTING}
AWAITING_PROVIDER_RESULT_STATUSES = {PROVIDER_RUNNING, DOWNLOADING}
```

`reap_stalled()` 从三批改成四批(按报告 §REG-02 建议的表):

| 卡死状态 | 外部 ID | 回收结果 |
|---|---|---|
| SUBMITTING | 无/有 | `SUBMIT_RESULT_UNKNOWN`,人工对账 |
| PROVIDER_RUNNING / DOWNLOADING | **有** | `PROVIDER_RESULT_PENDING`,**不必对账** |
| PROVIDER_RUNNING / DOWNLOADING | 无 | `SUBMIT_RESULT_UNKNOWN`(不该存在的形状,按最贵一侧处理) |

`can_resume_provider_results()` 的 docstring 补了第三条进入路径
(reaper),说明它比前两条更贵、恢复方式相同。

### 代码位置

* `backend/app/workflows/state_machine.py`:两个新常量。
* `backend/app/services/generation_service.py`:`reap_stalled()` 四批,
  `can_resume_provider_results()` docstring。

### 离线门禁覆盖

`test_the_spent_statuses_split_into_two_disjoint_buckets`、
`test_awaiting_result_means_submit_already_returned`、
`test_the_reaper_gives_a_resumable_code_to_tasks_that_still_have_their_id`、
`test_the_resume_gate_still_only_accepts_a_task_that_has_its_id`。

### 真库覆盖(未执行)

`::test_reaper_gives_a_resumable_task_its_id_back_and_worker_finishes_with_one_submit`
—— 报告 §REG-02 点名要求的那条链路:submit 成功 -> 模拟 worker 死于
PROVIDER_RUNNING(见测试内 docstring,替换 `_await_and_collect` 前半段
来复现"进程被 kill"而不放宽真实执行路径)-> `reap_stalled` -> 不带 force
的 `retry_task` -> worker 续跑 -> COMPLETED,断言全程 `submit()` 只调用
一次;`::test_reaper_still_sends_a_missing_id_task_through_reconciliation`
—— 反面对照,没有外部 ID 的仍然要求 force + 对账说明。

---

## 2.3 REG-03:候选落库异常不许提交半截数据 —— 已修复

### 问题复述

`_persist_candidates()` 崩溃后的收尾函数 `_abandon_attempt()` 内部会
`session.commit()`,而那是**会话级**的提交。上一轮那里的注释写着"这次的
`session.add()` 全部随外层 rollback 掉了" —— 这句话在 `_abandon_attempt()`
存在的前提下不成立:已经 `flush()` 过的候选行会被一起提交,库里留下
"任务失败、候选存了一半"。续跑重新 `fetch_results()` 之后,同一个外部结果
又存了一遍。

### 改法

两层防线:

1. **调用方**:候选落库整段套进 `session.begin_nested()` 保存点,异常时
   `ROLLBACK TO SAVEPOINT`,`_abandon_attempt()` 提交的就只剩它真正该写的
   attempt 收尾和用量流水。
2. **`_abandon_attempt()` 自己**:新增 `_drop_stray_pending_rows()`,提交前
   检查 `session.new` 里有没有非 attempt/task 的遗留待写行,有就 `expunge`
   掉并大声记一条 `logger.error`——这条日志出现就意味着有一条新路径忘了套
   保存点,而不是让它安静地写进库里。

`_persist_candidates()` 同时改成按 `(attempt_id, round_number,
candidate_index)` 认领已有行:已下载成功的直接复用(不重下,字节已经在
自有存储里),`PENDING`/`DOWNLOAD_FAILED` 的就地改写,不插新行 —— 这是
REG-01 两条续跑路径(全部下载失败、reaper 回收 DOWNLOADING)能够安全重跑
`_persist_candidates()` 的前提。

新增迁移 0033:`generation_candidates` 加唯一约束
`(attempt_id, round_number, candidate_index)`。应用层认领是第一道防线,
这条约束是第二道 —— 漏掉认领的路径造成的是一次 `IntegrityError`,能被
测试发现,而不是一批看不出来的重复候选。**迁移遇到存量重复时报错、不
自动删**:那些行上可能挂着 `evaluations`(`ON DELETE CASCADE`)。

### 代码位置

* `backend/app/tasks/generation_tasks.py`:`_rollback_savepoint()`、
  `_drop_stray_pending_rows()`、`_abandon_attempt()`、
  `_persist_candidates()`、`_await_and_collect()` 保存点那一段。
* `backend/app/models/generation.py`:`GenerationCandidate.__table_args__`。
* `backend/migrations/versions/0033_candidate_dedup_key.py`(新增)。

### 离线门禁覆盖

`test_persisting_candidates_happens_inside_a_savepoint`、
`test_abandoning_an_attempt_drops_rows_it_was_never_meant_to_commit`、
`test_persisting_is_idempotent_across_a_resume`、
`test_the_dedup_key_exists_in_the_schema_too`。

### 真库覆盖(未执行)

`::test_a_crash_mid_persist_leaves_zero_orphaned_candidates` —— 落库崩在
第二张候选时,断言库里候选行数为 0(不是 1);
`::test_resuming_after_a_persist_crash_does_not_duplicate_candidates` ——
崩溃后续跑成功,候选总数恰好等于 `candidate_count`(不是两倍)。

---

## 2.4 REG-04:`create_batch` 保存点范围 —— 已修复

### 问题复述

`session.add(job)` 原来在 `session.begin_nested()` 外面。`begin_nested()`
会先把当前 pending 对象 flush 一次,那次预 flush 发生在 `SAVEPOINT` 语句
**之前**,于是唯一键冲突在 `begin_nested()` 这一句就抛出来,而它在 `try`
之外 —— `except IntegrityError` 盖不住,第二个并发请求收到 500 而不是
回读到第一个批次。

### 改法

把 `session.add(job)` 和 `session.flush()` 一起挪进
`with session.begin_nested():` 块内。进入保存点时会话里没有这条 pending
的 job,预 flush 无事可做,冲突只可能由块内的 `flush()` 抛出。

### 代码位置

* `backend/app/workbench/batch_service.py`:`create_batch()` 幂等键分支。

### 离线门禁覆盖

`test_the_insert_is_inside_the_savepoint_not_before_it`。

### 真库覆盖

未新增(REG-04 的价值在并发场景,需要两个真实事务同时提交同一个
`Idempotency-Key`,留给下一轮的并发测试补上 —— 报告 §六 REG-04 的建议
测试清单)。

---

## 2.5 REG-05:活跃 Outbox 优先于历史失败 attempt —— 已修复

### 问题复述

`_stalled()` 原来是"outbox DEAD 或 attempt 终态,任一为真就算停住"。
`redeliver_dead()` 重新投递之后,库里是 `Outbox=PENDING`(worker 马上会取)
+ `Attempt=FAILED`(历史结局,写完不改)—— 只看 attempt 的话判成 STALLED,
界面显示"需要人工核实",而且因为 STALLED 不在 `hard_stops` 里,普通 SUBMIT
按钮还开着。运营看到"重新投递没生效",点了普通提交,而那条路会算出一把
**新的幂等键**,真的可能在平台上多出一件商品。

### 改法

`_stalled()` 新增活跃 Outbox 一票否决,判断顺序变成:

```
outbox 在 {PENDING, LEASED}   -> 没停(不管 attempt 是什么)
outbox == DEAD                -> 停了
否则                            -> 看 attempt 是不是终态
```

`redeliver_dead()` 之后 `display_status` 变成 `QUEUED`、`next_action`
变成 `WAIT`、`allowed_actions` 里没有 `SUBMIT`。

### 代码位置

* `backend/app/workflows/publish_view.py`:`_ACTIVE_OUTBOX`、`_stalled()`。

### 离线门禁覆盖(已跑,零依赖直接调真函数)

`tests/pure/test_a45_batch12_4_fixes.py::test_a_pending_outbox_outranks_a_terminal_attempt`、
`::test_a_dead_outbox_still_reads_as_stalled`(回归护栏:真正死掉的仍然
要报 STALLED)、`::test_active_and_terminal_outbox_states_do_not_overlap`。

`tests/pure/test_publish_view.py` 里原本编码了这个缺陷的用例
(`outbox=PENDING` 时仍断言 STALLED)已改写为 `outbox=None`,并新增
`test_a_redelivered_outbox_is_not_stalled_even_after_a_failed_attempt`、
`test_a_leased_outbox_is_not_stalled_mid_write` 两条。

---

# 3. 没有变的地方

## 3.1 迁移链

新增 0033,`down_revision = "0032"`,单一 head。`verify_delivery.py` 的
"迁移链单一 head" 检查通过。

## 3.2 前端

本轮完全没有碰 `frontend/`。EX-06 的前端接线(batch12-2 已做)、其余四条
都是纯后端问题。

## 3.3 REG-05 相关的既有测试改写

`tests/pure/test_publish_view.py::test_a_terminal_attempt_alone_is_enough_to_read_as_stalled`
原本传 `outbox_status=PublishOutboxStatus.PENDING`、断言 `STALLED`——那正是
本轮要修的缺陷本身。改写为 `outbox_status=None`,保留它原本要守的"取或不
取且"语义(没有活跃 outbox 时,单凭 attempt 终态也能判死)。

`tests/pure/test_a45_batch12_2_fixes.py::test_every_spent_status_is_a_status_the_reaper_treats_as_unknown`
断言的是"整份 `SPENT_STATUSES` 一律落 `SUBMIT_RESULT_UNKNOWN`",这正是
REG-02 报的缺陷。改名为
`test_every_spent_status_is_covered_by_exactly_one_reaper_bucket`,断言
改成"两档不相交、并起来等于原清单"。

---

# 4. 本轮仍未关闭 / 需要下一轮验证的事

1. **`tests/test_a45_batch12_4_recovery_db.py` 没有跑过。** 这是本轮最大
   的缺口:五条修复里三条(REG-01、REG-02、REG-03)的真库行为验证只存在
   于代码里,没有在真 PostgreSQL 上执行过一次。下一轮**第一件事**应该是
   跑这批测试,而不是接着写新代码。

2. **REG-04 没有并发测试。** 报告 §六 REG-04 建议的"两个真实事务同时提交
   同一个 Idempotency-Key"没有写,只有静态结构守卫。

3. **REG-05 的 `redeliver_dead()` 到 `build_view()` 端到端** 没有真库
   测试 —— 现有验证只到"喂进构造好的 `PublishFacts` 断言输出",没有验证
   `_facts()` / `_assemble_facts()` 从真实的 `PublishOutbox` 行读出来的
   `outbox_status` 确实是 `PENDING`。

4. 报告里没有点名要本轮处理、但仍然成立的局限原样保留:
   前端 `npm ci` 装不上(镜像 404)、PostgreSQL/Redis 集成测试、Ruff、
   import-linter 均未执行。

---

# 5. 建议下一轮的顺序

```
1. 在有 PostgreSQL 的环境里跑 tests/test_a45_batch12_4_recovery_db.py,
   把"未执行"改成"通过"或者暴露出这份修复自己的问题
2. 跑真 PostgreSQL 事务/唯一约束/回滚测试(报告 §2.2 列的门禁)
3. REG-04 补并发测试
4. 前端门禁(需要能访问 npm 镜像的环境)
5. 到那时候再谈是否可以关闭 batch12-3 / 冻结正式人工测试版本
```
