# A45 batch12-3:对 batch12-2 那一轮的四条回头修

> 纯逻辑回归 **1809 → 1824,失败数恒为 2**(本机缺 `sqlalchemy` / `pydantic`,与改动无关);
> `verify_delivery` 13/13;`verify_imports` 324 文件全通;`verify_sample_data` 5/5;
> 前端源码语法解析 **84/84**(新增 `utils/requestKey.ts`)。
> **没有新增迁移**,链仍停在 0032、单一 head。

batch12-2 的六条修复方向都对,主干也都核对无误。但有四处**论证或收尾没走完**,
而它们各自的失败方向都指回同一件事:钱。本轮全部修完。

---

## 一、EX-02 的「0 次」只在标记写成功时成立,而失败时比修之前更差

`_mark_provider_call` 的 docstring 里那段"返回 False 时为什么可以继续"写着:

> 回执仍然是 IN_FLIGHT,`receipt_route` 拿到的 `call_dispatched` 仍然是
> 保守的 True(默认值),行为退回到"停下来等人对账"。

**这句话不成立。** 默认值只保护不传这个参数的调用方,而唯一真的调用方
`_execute` 是显式传的:

```python
call_dispatched=(
    True if receipt is None else receipt.provider_call_at is not None
)
```

标记写失败 → 这一列是 NULL → `call_dispatched=False` → `receipt_route` 判
`EXECUTE`。也就是说"**标记没写进去、但付费调用发出去了、然后崩溃**"这条路,
恰好会被下一次回收判成「可证明没花钱」,自动重跑一次 —— EX-02 要挡的就是这件事,
只是触发条件从"分不出来"换成了"标记那一次 UPDATE 失败"。

更麻烦的是那一支**刻意不消耗付费额度**(在标记可信时这个判断是对的)。
原来这条路由 `MAX_BILLED_EXECUTIONS = 2` 兜着,现在它完全不受上限约束,
只剩 `MAX_ITEM_ATTEMPTS = 3`。在这个场景下自动重复付费不是 1 次也不是 0 次,
是**最多 3 次**。没有兜住,是退了一档。

概率也不像"数据库抖一下"那么远:`_claim_in_flight` 与 `_mark_provider_call`
每件条目各开一个新的 `SessionLocal`,而业务会话早就 check out 了。50 件的批次
撞上连接池耗尽时,先失败的恰恰是这两个新会话,业务会话照常把付费调用跑完 ——
两种失败并不同步。

### 做法

`_mark_provider_call` 的返回值不再可以丢,处理与 `_claim_in_flight` **同一条**:
记不上就不许调用付费模型。

停下来的代价只是这一件要重试,而重试是安全的 —— 没有标记就说明也没有调用,
`receipt_route` 会让它免费重跑。**这正是这条修复自己的论证**,batch12-2 在
`_claim_in_flight` 上用了它,在这里没有用。

顺带两处:

- `_mark_provider_call` 现在看 `rowcount`。UPDATE 匹配到 0 行和 UPDATE 抛错是
  同一个结果(标记没落下),原来前者返回 True;
- 拒绝之前把回执落成 `FAILED_UNBILLED`。留着 IN_FLIGHT 也能免费重跑,但那一行
  会被 `resolve_billed_unknown.py` 的第二个条件捞出来变成一条**假的对账工单** ——
  而我们知道这次没调用,那就该说出来。

`receipt_route` 的 docstring 里补了一段说明默认值保护不了显式调用方,并指向
`_execute` 那一侧的约定。删掉那段的人会照着"有默认值兜着"再丢一次返回值。

---

## 二、两条新守卫分不出实现和实现的反面

batch12-2 承认过"纯逻辑守卫挡不住实现写错了"。但有两条的**断言比它自己的
docstring 弱一个量级**,而且不需要真库就能补上:

| 用例 | 原断言 | 能通过它的错误实现 |
| --- | --- | --- |
| `test_reclaiming_a_receipt_clears_the_previous_call_marker` | `"provider_call_at" in ast.dump(fn)` | 把 `"provider_call_at": None` 改成 `utc_now()` —— 正是第四节 2 小节专门写了一段的那个 bug |
| `test_the_marker_is_written_before_the_paid_executors` | 两个名字都出现在 `_calls(_execute)` 里 | 把 `_mark_provider_call` 挪到 `_exec_extract` 之后 —— 而 docstring 说的是"必须在**之前**" |

已就地补强:前者去 upsert 的 `set_` 字典里取那个值、断言它是 `None` 字面量;
后者按 `lineno` 比先后。另加一条 `test_a_marker_that_did_not_land_blocks_the_paid_call`
钉住第一节那个改动的三件事(返回值进了判断、失败分支不往下走、不留假工单)。

`test_a45_batch12_3_fixes.py` 里有一条 `test_the_marker_guards_assert_behaviour_not_spelling`
守着这两条不许退回字符串包含。

---

## 三、EX-06 在产品里没有入口

`frontend/src/api/batch.ts` 的 `create()` 不发 `Idempotency-Key`,全前端搜不到
任何生成这把键的地方。所以 0032 那条迁移的 docstring 里点名的场景——
**运营双击 → 两个 BatchJob,两套条目**——在界面上照样能复现,而仓库里所有测试
都是绿的。服务端那一层是对的,只是没有人用。

对比 EX-04 是前后端一起改的,这一条像是漏了收尾。

### 键的生成策略必须写下来

`batch_jobs.request_key` 上的唯一约束是**全局**的,不按操作人分。所以键
**不能**按请求内容算(动作 + 商品 + 标签之类)。那样算的话:

    两个运营先后选中同一批商品做同一个动作
    -> 算出同一把键
    -> 第二个人静默拿到第一个人的批次,而且什么都不会执行

界面上他会看到一个批次号、一份进度,一切正常 —— 只是那不是他建的,而他要做的
那件事从来没发生过。**这正是 EX-06 反对的那种静默。**

新增 `frontend/src/utils/requestKey.ts`:一次点击一把随机键,理由与 fallback
的取舍都写在那个文件里(`crypto.randomUUID` 只在安全上下文里存在,局域网 IP
上跑 dev server 时它是 undefined)。

### 生命周期是"一次提交意图",不是"一次 HTTP 请求"

`BatchActionBar` 里的 `pendingKey` ref:

    开/换计划   清掉 —— 换了动作或筛选就是另一件事,留着旧键会撞出一个
                没人看得懂的 409
    提交失败    **留着** —— 这正是重发要复用它的场景
    提交成功    清掉 —— 下一次点击是新的一件事

因为失败那一支不清键,原来那句"超时后不要提示重试 —— 重试会建出第二个同样的
批次"不再成立,注释已改。措辞仍然保守:让运营去批次页看一眼永远是对的,而
"可以放心重试"要等真库上那一组测试跑过之后再说。

### 重发必须在界面上说出来

后端对重发返回的 `counts` 是**上一次**的执行结果。不说的话运营看到的是一条和
平时一模一样的"已执行 N 件",他会以为刚刚又跑了一遍。`BatchJob` 加了
`reused_request?: boolean`(与 `dispatch_state` 同理,只在那一次写请求的响应里),
界面读到它时换一条专门的通知。

### 顺带:超长的键报错,不截断

`create_batch` 原来 `[:64]`。截断之后,两把只有前 64 字符相同的键会变成同一把,
表现是第二次提交拿到一个 409,而那个 409 说的是"这把键已经用在另一组入参上了"——
没有任何人能从这句话推出"你的键太长了"。静默改写调用方给的标识符,和 EX-06
反对的静默返回原批次是同一类事。现在报 422。

---

## 四、EX-03 停在下载那一步之前

`_await_and_collect` 把 `get_status` / `fetch_results` 的失败改判成
`PROVIDER_RESULT_PENDING`,但 `_persist_candidates` 那个 try 块仍然原样 `raise`,
外层落 `INTERNAL_ERROR`。回到 `retry_task`:

- 不是 `SUBMIT_RESULT_UNKNOWN`、不是 `SOURCE_ASSET_*`、不是 `PROVIDER_RESULT_PENDING`;
- `can_resume_formatting` 没有 SELECTED 行;
- `can_resume_scoring` 要 `WORKER_STALLED`;

于是掉进最后那个 `else`,**清 `external_task_id` 回 QUEUED,重新 submit**。

走到那一步 `fetch_results` 已经返回了 —— 钱花完了,图也确实生成了。单张下载
失败不会到这里(`_persist_candidates` 自己吞掉了);能到这里的是基础设施级故障:
存储层构造失败、`flush()` 失败、影子写失败。也就是 EX-05 花了一整段论证的那个
S3 / 库抖动家族。

worker **死亡**在 DOWNLOADING 是覆盖到的(`DOWNLOADING ∈ SPENT_STATUSES` →
`SUBMIT_RESULT_UNKNOWN`),进程内**抛异常**没有。所以 batch12-2 第七节那句
"三条链路…不再有静默重新计费的分支"比代码支持的要强一点。

### 做法

落库失败改判成同一个 `PROVIDER_RESULT_PENDING`,走同一条续跑闸门。续跑会重新
`fetch_results` 拿一批新的短效 URL,而不是拿旧的那批去下——那批链接本来就快
过期了,这也是必须重走一遍而不是"接着存"的理由。

重复候选不会发生:这次的 `session.add()` 全部随外层 rollback 掉了,只有
`_checkpoint` 提交过的 DOWNLOADING 状态留着。

`OperationalError` **摘出去**照旧 `raise`:那一类落不进 FAILED(写不进去),
而且不是任务的错,要一路抛到最外层走 autoretry。改判会把一次基础设施故障变成
一条需要人点重试的失败。

`can_resume_provider_results` 的 docstring 补了两处进入路径的分工——第二处更贵,
只写第一处的话,下一个人看到落库失败落这个码会以为是误用。

---

## 五、改动清单

| 文件 | 改了什么 |
| --- | --- |
| `app/workbench/batch_service.py` | `_mark_provider_call` 看 rowcount + docstring 结论反转;`_execute` 拒绝无标记的付费调用并落 `FAILED_UNBILLED`;`create_batch` 拒绝超长键 |
| `app/workbench/batch.py` | `receipt_route` 的 `call_dispatched` 补一段:默认值保护不了显式调用方 |
| `app/tasks/generation_tasks.py` | `_persist_candidates` 失败改判 `PROVIDER_RESULT_PENDING`,`OperationalError` 摘出 |
| `app/services/generation_service.py` | `can_resume_provider_results` docstring 补第二条进入路径 |
| `frontend/src/utils/requestKey.ts` | **新增**。一次点击一把键 + 非安全上下文的 fallback |
| `frontend/src/api/batch.ts` | `create()` 发 `Idempotency-Key`;`BatchJob` 加 `reused_request` |
| `frontend/src/components/workbench/BatchActionBar.tsx` | `pendingKey` 生命周期;重发的专门通知;超时文案改写 |
| `tests/pure/test_a45_batch12_2_fixes.py` | 两条守卫补强 + 新增一条 |
| `tests/pure/test_a45_batch12_3_fixes.py` | **新增**,12 条 |

---

## 六、CI 上仍然必须补的真库测试

batch12-2 第六节那六组一条都没跑过(本机没有 PostgreSQL / Redis / Docker /
`psycopg`),本轮同样没跑。**纯逻辑守卫挡得住"实现被悄悄删掉",挡不住
"实现写错了"**,而这四条修复里有三条的判定发生在事务边界和查询条件上。

那六组照旧,另加三组:

| 优先级 | 测试 | 注入点 | 核心断言 |
| --- | --- | --- | --- |
| 2b | 标记写失败、但付费调用成功 | `_mark_provider_call` 的 UPDATE 抛错 / 匹配 0 行 | 这一件当场失败且**没有调用模型**;回执落 `FAILED_UNBILLED`;外部调用计数为 0 |
| 5b | 取回结果后落库失败 | `_persist_candidates` 里 `storage.save` / `flush` 抛错 | 落 `PROVIDER_RESULT_PENDING`;`external_task_id` 保留;重试不再 submit;候选表没有半截行 |
| 6b | 同键重发,第一次已跑完 | 首次响应丢失后同键再提交 | 只存在一个 BatchJob;`reused_request=True`;**条目一件都没有被重新执行** |

2b 是第一节那条修复的唯一真验证,而 batch12-2 列的第 2 组(付费成功后 worker
死亡)抓不到它——那一组里标记是写成功的。

---

## 七、人工测试的解封范围(相对 batch12-2 的更正)

**batch12-2 第七节说已解封、实际还差一步的**:

- 「失败任务上点"重试"」。那一节说三条链路都有明确的拦截或续跑,而第四节
  那条(取回结果后落库失败)当时还会静默重新计费。现在补上了,但**必须先跑
  过第 5b 组**——判定发生在异常分支上,纯逻辑守卫只能证明那段代码存在。

**本轮新增、仍然阻塞的**:

- 「批次创建的双击 / 重发验收」。前端接上了 `Idempotency-Key`,但唯一约束下的
  并发裁决、以及"重发不重新执行"这两件事都没在真库上验证过(第 6、6b 组)。

**仍然不成立的说法**(与 batch12-2 一致,不重复论证):

- 「现有费用台账是准确的真实消费记录」。历史上已经发生的重复扣费不会因为这次
  修复而回到账上。
