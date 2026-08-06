# A45 batch12-2：独立异常场景审查（EX-01 ~ EX-06）的修复

> **⚠️ 本文件是那一轮的原文，没有改写。下面几条结论已被 `docs/REVIEW-A45-BATCH12-3.md` 更正，
> 读到时以那一份为准。**
>
> | 位置 | 这里说了什么 | 实际 |
> |---|---|---|
> | 第二节「自动重复付费从 1 次降到 0 次」 | 无条件成立 | **只在标记写成功时成立。** `_mark_provider_call` 的返回值当时是丢掉的，而 `_execute` 传给 `receipt_route` 的 `call_dispatched` 是显式算的——标记写失败之后的付费调用会留下一条「没有标记的 IN_FLIGHT」，下一次回收读成"可证明没花钱"自动重跑。而且那一支不消耗付费额度，于是连 `MAX_BILLED_EXECUTIONS`（=2）都不再兜底，只剩 `MAX_ITEM_ATTEMPTS`（=3）。batch12-3 第一节修 |
> | 第三节 EX-06「幂等只覆盖批次内部付费动作」 | 已修 | **只修了服务端。** 前端一个字都没发 `Idempotency-Key`，0032 docstring 里点名的双击场景在界面上照样能复现。batch12-3 第三节修 |
> | 第三节 EX-03「外部任务还在时，重试必须接着取」 | 已修 | **停在下载那一步之前。** `_persist_candidates` 崩了仍然裸 `raise` → `INTERNAL_ERROR` → `retry_task` 清 ID 回 QUEUED 重新 submit，而那时 `fetch_results` 已经返回、钱已经花了 |
> | 第五节被改动的守卫 | 钉住了新行为 | 其中两条的**断言比自己的 docstring 弱一档**，分不出实现和实现的反面（`utc_now()` 冒充清标记、标记挪到执行器之后）。batch12-3 第二节就地补强 |
> | 第七节「失败任务上点重试…不再有静默重新计费的分支」 | 已解封 | 少算了落库失败那一条链路。补上之后仍需先跑真库第 5b 组 |
>
> 门禁数字也已过期：纯逻辑回归现为 **1824**（失败恒为 2），`verify_imports` 324 文件，
> 前端语法解析 84/84。

> 纯逻辑回归 **1776 → 1809，失败数恒为 2**（本机缺 `sqlalchemy` / `pydantic`，与改动无关）；
> `verify_delivery` 13/13；`verify_imports` 323 文件全通；`verify_sample_data` 5/5；
> 前端源码语法解析 83/83（TS1xxx 为 0）。
> 迁移新增 **0031**、**0032**，链仍单一 head。

审查报告列了 6 条。本轮**全部修完**，其中 EX-02 的定性与报告不同，见下。

---

## 一、审视：报告哪里说得不对

### EX-02 的次数是 2 不是 3

报告写「最多无人值守重复付费 3 次」，依据是 `MAX_ITEM_ATTEMPTS = 3`。
但 A45-batch12 已经加过 `MAX_BILLED_EXECUTIONS = 2` 那道闸，
`receipt_route` 在 `executions >= 2` 时改判 `NEEDS_RECONCILIATION`。
所以实际上限是 **2 次执行 / 1 次自动重复付费**。

缺陷本身完全成立，只是量级小一档。**这一条不影响它的优先级**：
那一次重跑的发起者是回收器（beat 60 秒一拍），全程没有人在回路里，
而运营只能在事后从台账的 `executions > 1` 里发现——那时钱已经花完了。

### 其余五条复核后与报告一致

EX-01 / EX-03 / EX-04 / EX-05 / EX-06 的根因、触发路径、以及
「重复扣费 / 重复上架」的判定都核对无误，不再复述。

---

## 二、EX-02 没有照报告的建议改

报告建议：**IN_FLIGHT 租约回收后直接置人工，不再自动 redispatch。**

没有照做。理由写在 `batch.py` 里 `MAX_BILLED_EXECUTIONS` 那段注释上，
它是上一轮**刻意**选 2 而不是 1 时留下的：

> 取 1 等于每一次 pod 驱逐、每一次库抖动都要人工介入，50 件的批次会因为
> 一次基础设施抖动全部停下 —— 那种代价下这道闸迟早会被关掉。

这个担心是真的。一道会被关掉的闸不如没有闸，因为它还骗人。

但那段推理有个前提：**分不出「崩在调用之前」和「崩在调用之后」。**
分不出来才需要折中。所以本轮修的是那个前提，不是那个数字。

### 做法

迁移 0031 给 `batch_action_receipts` 加一列 `provider_call_at`，
由新函数 `_mark_provider_call()` 在**独立事务**里、紧贴付费调用之前写入
（独立事务的理由与 `_claim_in_flight` 完全相同：业务事务会随崩溃一起回滚，
而这个标记的全部意义就是崩溃之后还读得到）。

于是 `receipt_route` 的 IN_FLIGHT 分支不用猜了：

```
provider_call_at 为空    EXECUTE               可证明没花钱，免费重跑，不扣额度
provider_call_at 有值    NEEDS_RECONCILIATION  停下等人对账
```

**自动重复付费从 1 次降到 0 次**，同时那条「基础设施抖动不该变成人工工单」
的路径在可证明的范围内保住了。

### 诚实说明：能被判成"安全"的窗口很窄

认领与调用之间只隔着几条语句，所以「崩在调用之前但回执已存在」是少数。
这一列的主要价值不是省人工，是两件别的事：

1. **让"停下来"这个决定有依据**，而不是赌一把；
2. 给出对账真正需要的东西——**一个时刻**。运营拿着它才能去服务商后台按
   时间窗查，而不是对着一条只写着"费用不明"的记录发呆。

### `MAX_BILLED_EXECUTIONS` 没有删

它的适用范围收窄到一条：`FAILED_BILLED` 且动作不可离线复校（目前只有
`EXTRACT`）。那是一次**已知**付费的失败、而且一条证据都没落库，除了重跑
没有别的路，而那条路上仍然分不出"重跑会不会有用"。

---

## 三、六条的落点

| ID | 根因 | 改在哪 |
| --- | --- | --- |
| EX-01 | 回收分批带 `external_task_id IS NOT NULL`，而受理与落库之间隔着一次 commit | `generation_service._reap_batch` / `reap_stalled` |
| EX-02 | IN_FLIGHT 分不出崩在调用前后，只能折中放过一次自动付费 | 迁移 0031、`batch.receipt_route`、`batch_service._mark_provider_call` |
| EX-03 | `retry_task` 只护 `SUBMIT_RESULT_UNKNOWN`，取结果失败走通用分支清 ID | `_await_and_collect`、`can_resume_provider_results`、状态机加边 |
| EX-04 | `reconcile_unknown()` 实现了但没接线；DEAD 连服务函数都没有 | `api/publish.py` 两端点、`publish_service.redeliver_dead`、`publish_view`、前端 |
| EX-05 | `can_resume_formatting()` 只查库不验文件 | `output_service.source_asset_problem`、`_format_outputs` 前置、`retry_task` 拦截 |
| EX-06 | 幂等只覆盖批次内部付费动作，不覆盖"创建批次"这条命令 | 迁移 0032、`batch_request_fingerprint`、`Idempotency-Key` |

### EX-01：分批依据从"ID 落库了吗"改成"状态是什么"

崩在 `session.commit()` 那一句的行长成「SUBMITTING + ID 为空」。
带 ID 条件分批的话它掉进 `WORKER_STALLED`——那个码的含义是"钱没花出去，
直接重试即可"，于是一键重试就再买一次。

现在两批只按状态划分，并**拆成三次 UPDATE 写**，因为两种"结果未知"
给运营的下一步动作不同：

- 有外部 ID：照着 ID 去后台核对；
- 没有外部 ID：按商品 + 提交时间窗核对。

原来那句话会让第二种情况的运营去查一个不存在的 ID。
日志新增 `possibly_billed_without_external_id` 计数，它出现即说明落 ID 的
commit 在失败——那是一个该告警的信号，而不是一个该被平均掉的数字。

### EX-03：外部任务还在时，重试必须接着取而不是重新买

把 `_run` 尾部 81 行抽成 `_await_and_collect()`，于是它有了第二条进入路径。
新增 `PROVIDER_RESULT_PENDING` 码，**判据是错误码不是阶段**：

```
NETWORK_TIMEOUT / RATE_LIMITED / PROVIDER_SERVICE_ERROR / RESULT_DOWNLOAD_FAILED
    -> 我们没问出来，外部任务还有效，保留 ID 接着问
CONTENT_SAFETY / GENERATION_FAILED / INPUT_INVALID / AUTH_FAILED
    -> 外部自己有了结论，再问一百次答案相同
```

状态机加了 `FAILED -> PROVIDER_RUNNING`。这条边原来被
`test_resume_path_does_not_open_a_shortcut_past_generation` 明令禁止，
理由是"直接跳进去会得到一个没有请求在跑却等着结果的任务"。
那句话对 `SUBMITTING` / `DOWNLOADING` 仍然成立，对 `PROVIDER_RUNNING`
不再成立——闸门要求 `external_task_id` 还在，而那个 ID 的含义正是
"外部确实有一个请求在跑"。守卫已按此更新，另外两个状态仍然禁止。

续跑分支自带两个兜底：停在 `PROVIDER_RUNNING` 却没有 ID、或找不到对应
attempt 时**不往下走**（往下走会重新 submit），落 `SUBMIT_RESULT_UNKNOWN`
交对账——那两种形状正是 EX-01 那一类。

### EX-04：出口必须存在，否则人会自己找一条更危险的路

这一条的真实危害不是"少两个按钮"。终态幂等复用会挡住一切用相同输入的
重新提交，于是运营要把商品弄上架，只剩下改草稿、换输入——
而那会**算出一把新的幂等键**，也就是唯一真的会在平台上多出一件商品的操作。

两条动作的全部安全性来自"不算新键"：

- `RECONCILE` → `reconcile_unknown()`，带原键去问，平台要么 409 带回既有 ID、
  要么返回上次结果，两种都不会多出商品；
- `REDELIVER` → 新增的 `redeliver_dead()`，复用原 attempt 与原键重新排队，
  行锁 + 说明必填 + 审计。

前端措辞刻意**不叫"重试"**：那两个字会让人以为点了就重新提交一次。
确认框里必须复述的只有一句——这不会在平台上多出一件商品。

### EX-05：坏文件既不能循环，也不能悄悄变成重新计费

`source_asset_problem()` 三步：`exists` → `read` → **真的解一次码**。
只看存在与长度会把一段 HTML 错误页当成可用图片，而那正是那个不收敛循环
的开头。存储层自身故障（S3 不通、凭据过期）原样抛出，不误判成文件丢失——
那一类重试就能好，判成 `SOURCE_ASSET_*` 会把它错误地挡在重试之外。

拦截放在 `retry_task`，因为**两条往下的路都是错的**：
续跑 FORMATTING 读同一个坏文件；回 QUEUED 重生要再花一次钱。
所以挡住，由人显式选：换一张候选（免费），或带 force + 说明重新生成。
坏文件分支排在 `can_resume_formatting` **之前**——排后面的话它永远走不到，
因为那个判定对坏文件同样返回 True。

### EX-06：请求级幂等，可空不强制

`request_key` + `request_fingerprint` + 唯一约束。同键同指纹返回原批次；
**同键不同指纹报 409**——静默返回原批次比多建一个更危险，因为界面上一切
正常，只是有一批商品从来没被处理过，而客户端以为做了。

指纹对商品顺序与重复做归一（`[A,B]` 与 `[B,A]` 是同一次请求），
但 `label` 计入——它不改变要做的事，却改变批次的身份。

并发靠唯一约束裁决，Python 这一层负责**认输之后回读**（保存点 +
`IntegrityError` 捕获），与 `generation_service` 处理生成幂等键冲突同一套路。

接口层识别复用后**直接返回，不再执行**：往下走正是这条幂等要挡的事。

---

## 四、顺带修掉的两处（报告未提及）

### 1. 对账出口会对新卡住的回执一条都列不出来

`tools/resolve_billed_unknown.py` 按 `executions >= MAX_BILLED_EXECUTIONS`
取数，那是"自动付费额度用完了"的口径。EX-02 修完之后 IN_FLIGHT 只要调用
发出过就当场停下，`executions` 还是 1——**闸门装上了，出口对不上号**。

已改成两条件取或，并在输出里加上 `provider_call_at`：
没有那个时刻，"去后台核对"就退化成在整天的请求里大海捞针。

### 2. 认领必须清掉上一轮的调用标记

`_claim_in_flight` 的 upsert 里 `paid_calls` / `reuse_count` 是"一律不碰"的
累计量，但 `provider_call_at` 是**本次执行的状态位**。不清的话新一次执行
还没走到调用就已经带着标记，崩溃时被判成"可能已计费"——一个凭空的、
而且会重复发生的人工工单。

---

## 五、被改动的既有守卫（四条）

每一条都是我有意推翻的行为契约，都重写了 docstring 说明边界为什么挪、
原理由的哪一部分仍然成立，而不是把断言改成新值了事。

| 用例 | 原断言 | 现断言 |
| --- | --- | --- |
| `test_state_machine::test_resume_path_does_not_open_a_shortcut_past_generation` | 三个状态一律禁止 | `SUBMITTING`/`DOWNLOADING` 仍禁止；新增一条守卫钉住 `PROVIDER_RUNNING` 的闸门必须同时查错误码与 ID |
| `test_publish_view::test_a_dead_outbox_on_an_in_flight_listing_reads_as_stalled` | `next_action == INVESTIGATE` | `REDELIVER`；另加两条守"每种停住都有出口"和"正常行上不出现恢复按钮" |
| `test_batch_lifecycle::test_an_interrupted_execution_is_retried_with_cost_acknowledged` | IN_FLIGHT → `EXECUTE_AGAIN_BILLED` | 按 `call_dispatched` 分两支；另加一条钉住**默认值必须保守** |
| `test_a45_batch12_fixes::test_in_flight_receipt_stops_paying_after_the_ceiling` | 第一次允许再跑一次 | 发出过就一次都不许；没发出的不受上限锁死 |

新增 `tests/pure/test_a45_batch12_2_fixes.py`，29 条。

---

## 六、还没做的：真库故障注入

本机没有 PostgreSQL / Redis / Docker / `psycopg`，报告第 E 节那六组测试
一条都没跑过。**纯逻辑守卫挡得住"实现被悄悄删掉"，挡不住"实现写错了"**，
而这六条修复里有四条的判定发生在查询条件和事务边界上。

CI 上必须补的，按优先级：

| 优先级 | 测试 | 注入点 | 核心断言 |
| --- | --- | --- | --- |
| 1 | Provider 成功、保存 external ID 失败 | 返回 ID 后的 commit | 落 `SUBMIT_RESULT_UNKNOWN`；普通 retry 409；submit 计数 1 |
| 2 | 批量付费成功后 worker 死亡 | 模型返回后、receipt commit 前 | 回收后进人工；外部调用计数仍为 1 |
| 3 | Provider 状态/下载暂时失败 | `get_status` / `fetch_results` 首次抛超时 | 重试保留 external ID；submit 不再调用 |
| 4 | 发布 CREATE 结果未知 | 外部成功但响应超时 | reconcile 复用原键，恢复原 external ID，不新建商品 |
| 5 | 候选文件丢失与损坏 | 删除、零字节、随机字节 | retry 不重进 FORMATTING；返回专用码与恢复动作 |
| 6 | 批次创建响应丢失 | 首次 commit 后断开，同键再提交 | 只存在一个 BatchJob，返回相同 batch_id |

前 4 条必须用真事务：mock Session 验证不了 commit、rollback、唯一约束和行锁。

---

## 七、人工测试的解封范围

报告第 F 节把这几件事列为阻塞。按本轮修复对照：

**已解封**（前提是先在真库上跑过上面第 1~3 组）：

- 真实付费 Provider 的异常恢复测试——EX-01/02/03 的自动重复付费路径已封死；
- 失败任务上点"重试"——三条链路（提交结果未知 / 取结果失败 / 坏文件）
  各自有明确的拦截或续跑，不再有静默重新计费的分支。

**仍然阻塞**：

- 「自动上架异常恢复验收」需要先跑过第 4 组。端点和界面已经有了，
  但没有在真平台或 Simulator 上验证过 reconcile 真的能拿回原 external ID。

**仍然不成立的说法**：

- 「现有费用台账是准确的真实消费记录」。EX-01/02 修的是**今后**不再重复
  扣费，历史上已经发生的重复扣费不会因为这次修复而回到账上。
  真实 Provider 上跑过的批次，费用仍需以服务商后台为准。
