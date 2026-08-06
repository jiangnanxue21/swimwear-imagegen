# A45 batch12-5 回归评审 —— 修复报告

对应评审报告《A45 batch12-5 回归评审报告》第四节的三条 P1(NEW-01 / NEW-02 /
NEW-03),以及第七节给出的"最小下一轮修改范围"。**没有超出那三项做重构。**

---

# 一、结论自评

```text
NEW-01 内部费用台账重复记账      已修,机制有纯测试 + 真库测试守着
NEW-02 评分重排早于释放租约      已修,顺序有行号断言,行为有真库测试
NEW-03 租约/阈值低于合法最长耗时  已修,推导改成按配置算,并补齐心跳/续期/fencing
报告 §2.3 的文档口径出入          已更正(7 条 -> 6 条)

纯测试            1862/1862 通过,2 条因缺 pydantic/sqlalchemy 大声跳过(收尾后运行器
                  把"缺第三方依赖"单列为 SKIP,不再记成失败 —— 装好依赖的环境照常执行)
verify_imports    329 个文件通过
verify_delivery   13/13 通过
verify_sample_data 5/5 通过
compileall        通过
真库测试          新增 7 条,**一条都没跑过**(容器无 PostgreSQL)
```

**这一版仍然不能冻结为正式人工测试版本。** 理由和上一轮同构:这三条修复里
有两条(费用幂等、租约接管)最终证据必须来自真实 PostgreSQL,而本轮没有那个
环境。详见第五节。

---

# 二、三条 P1 的判断过程

## NEW-01:重复记账

### 先确认它到底是什么

报告说"不会导致 Provider 再收一次生成费",这一点复核后确认:
`_await_and_collect()` 续跑时走的是 `get_status` / `fetch_results`,
`provider.submit()` 只在 `_run()` 里调用一次。所以这是**内部账目失真**,
不是重复扣费。

但"不是扣费问题"不等于"不重要"。失真的那一批恰好是**恢复过的任务** ——
也就是最需要被看清楚的一批。而且它是单向的:只会多记,不会少记,
于是月度花费、外推金额、失败率全部朝同一个方向偏,厂商账单再也对不上。

### 根因不在调用点,在 `record_usage` 本身

报告把位置指到 `generation_tasks.py:917-940`。复核下来,那一行没有错 ——
错的是它依赖了一个不成立的前提:**"这个函数每条 attempt 只会执行一次"**。
REG-01 的修复给 `_await_and_collect()` 开了第二条进入路径(`resuming=True`)
之后,这个前提就没了,而 `record_usage()` 里没有任何东西能发现这件事:
它无条件 `ProviderUsageRecord(...)` + `session.add()`。

所以修在 `record_usage` 上,不是修在调用点上。修在调用点意味着下一条新路径
还要再想起来一次。

### 键的形状:为什么 `attempt_id` 是准确的,不是近似

报告建议 `attempt_id + operation=submit`,采纳。补一句论证:

- 换 Provider 重试 -> 新建 attempt(`_apply_decision` 的 switch_provider)
- 重生下一轮 -> 新建 attempt
- 续跑 -> **复用同一条 attempt**,因为它复用的正是同一个 `external_task_id`

也就是说 attempt 与"一笔已经付过的生成"是一一对应的。拿它当计费键是
准确的,不是"够用就行"。

### 否决掉的两个方案

**`UNIQUE (attempt_id, operation)`**:不行。`attempt_id` 可空(评分流水
`operation=vision_score` 那一批就没有 attempt),而 PostgreSQL 的联合唯一
在含 NULL 时形同虚设 —— 两行 `(NULL, 'vision_score')` 互不冲突,
约束等于没加,而它**看起来是加了的**。

**只在应用层去重、不加约束**:不行。理由和迁移 0033 对候选表的处理一致 ——
只有应用层的话,下一个人漏掉键的后果是一批看不出来的重复流水;有约束的话,
后果是一次 IntegrityError,而后者能在测试里被发现。

### 一个报告没提但必须一起决定的点:更新时怎么取值

`succeeded` / `candidate_count` / `error_code` 取新值 —— 恢复成功之后那次
调用的**结论变了**,留着旧值会让同一笔消费在库里既是成功又是失败,
失败率统计从此无解。

`billable_units` 取 `max(旧, 新)`。恢复这一遍拿回来的候选可能比第一遍少
(`_drop_shrunk_tail` 那条路),但钱是按第一次实际产出收的。往下改等于让
台账**少记**一笔已经花掉的钱 —— 而这个方向的错误没有人会去发现。

### 反方向也要守

轮询、取结果、评分**不传键**。那几类每一次都是真的又调了一次(评分尤其:
一张图评两次就是两次真实计费),给它们加幂等键会让台账少记。
纯测试里有一条专门守这个方向。

---

## NEW-02:派发早于释放租约

### 报告给的顺序是对的,但修的位置不够

报告的注入记录是 `['commit', 'dispatch', 'release', 'close']`,建议改成
`commit -> release -> dispatch`。照做了,但只改 `run_generation_task` 的入口
**堵不住**:

`_reschedule_scoring()` 内部是 `commit -> enqueue -> commit`。Outbox 行一提交,
relay 就可能立刻把消息投出去 —— 这中间租约还挂在我们身上。入口那次释放
发生在这之后,晚了。

所以两处都改:`_reschedule_scoring` 里释放排在 `enqueue` **之前**,
入口那次是第二道。

### 为什么先释放是安全的

走到那里时这一轮该做的已经全部提交完,当前 worker 除了返回没有别的动作。
租约的作用是"防止两个 worker 同时干活",而我们已经不干活了。

反过来,如果释放之后、`enqueue` 之前崩掉:任务停在 SCORING 且无人持有租约,
下一次投递能正常接管。**这是一个会自愈的死法**,比现在"消息发了但没人能执行"
那种好 —— 后者只能等回收器。

### 这条 bug 最难受的地方

每一层都认为自己成功了:派发成功、消费成功、退出正常,Outbox 是 DISPATCHED。
只有任务本身停在原地。所以真库测试那条**只认终态 COMPLETED**,
不去断言中间那些"看起来成功"的信号 —— 它们正是上一版骗过所有人的东西。

---

## NEW-03:租约与阈值

这一条改动最大,因为报告点出的"把 900 改大不够"是对的,而正确的修法
需要先换掉判据。

### 第一步:确认那三个系数

```text
候选张数    schemas/generation.py 是 le=8,dispatch_policy 按 4 算
评分次数    vision.py 读 VISION_MODEL_MAX_RETRIES=2,即单张最多 3 次,
            dispatch_policy 按 1 次算
取结果      轮询超时之后还有一次 fetch_results,完全没算
```

报告说的"合法最坏 2700 秒 vs 阈值 1800 秒"复核成立。

### 第二步:为什么不能只把常量改大

报告已经说了(可配置)。补充一点:那三个数都在 `core/settings_schema.py` 里,
也就是**运营在设置页上能改,页面写着"改完就生效"**。而
`reap_stalled(older_than_seconds=STALL_THRESHOLD_SECONDS)` 是**默认参数**,
在 import 那一刻求值 —— worker 进程会一直按旧值回收,直到有人重启它。

而"调大超时"正好是让合法耗时变长、最需要阈值跟着变的那个方向。
所以推导必须是**运行时函数**,不是常量。

### 第三步:判据从"整段耗时"换成"心跳间隔"

这是本轮最关键的一个判断。

如果保留"阈值必须大于整段合法耗时"这个判据,阈值要抬到 2700 秒以上,
再算上现有测试要求的余量就是 5400 秒 —— 也就是说**一个真死掉的 worker
要 90 分钟才有人管**。为了不误杀正常任务,把死亡检测牺牲掉了。

但回收器读的本来就是 `updated_at`,而 `updated_at` 在每个提交点都会推进。
要比的不是"一整轮多久",是"相邻两个提交点之间多久":

```text
PROVIDER_RUNNING   get_status + fetch_results,中间没有提交点   300 + 60 = 360
DOWNLOADING        整段在一个保存点里,中途不能提交              8 × 30  = 240
SCORING            逐张之间有心跳                              90 × 3  = 270
                                                        取最大 = 360 秒
```

于是 1800 秒的阈值有 5 倍余量,而且 8 张图的任务不会被误杀。**两边同时变好。**

代价是:SCORING 那一格的 270 秒**必须**靠逐张心跳才成立。没有心跳它就是
2160 秒,整个推导塌掉。所以心跳不是可选优化,是这个判据的前提 ——
纯测试里有一条专门断言"心跳在循环里面",另一条断言
"整段合法耗时必然大于租约长度"(即续期是强制的),防止有人哪天把它删掉。

### 第四步:fencing

报告说得很准:"只有释放租约时检查 owner,不是真正的 fencing"。

复核确认:`_release_phase()` 检查 `phase_lease_owner == owner`,而所有业务写入
(评分结果、状态转移、出图)一律不检查。被接管之后旧 worker 可以把自己那份
结论照常提交,盖掉新持有者。

做法是把三件事捆进同一次心跳:

```text
updated_at         对回收器说"我还活着"          少了 -> 误杀
phase_lease_until  对其它 worker 说"这里有人"    少了 -> 重复评分
rowcount 检查      对自己说"你已经不是持有者了"   少了 -> 旧 worker 盖掉新结论
```

分开做必然出现三者不一致的瞬间,而那正是问题发生的地方。

拿到"已经易主"的信号后抛 `_LeaseLost`,由入口静默收尾:
**不落 FAILED**(那会盖掉新持有者正要提交的成功结论 —— 正是 fencing 要防的
事情本身)、**不释放租约**(它已经不是我们的了)。

### 第五步:回收器加第二个独立信号

心跳判据有个前提 —— worker 活着就一定推得动 `updated_at`。库抖一下时这个
前提不成立,而后果是一个正在花钱评分的任务被落成 FAILED。

所以回收器现在还看租约:`phase_lease_until` 还在未来就不动手。续期成功
意味着它此刻确实还在跑,这是和心跳独立的第二个证据。

代价是一个真死掉的 worker 会多挡一个租约周期。方向是对的:
回收晚了只是让死掉的任务多躺一会儿,回收早了要重新花钱。
真库测试里有一条对照用例,确认租约过期之后它必须能被正常回收 ——
否则这道护栏会变成"永远收不掉"。

---

# 三、报告之外发现的两处

## 1. 首轮评分完全没有阶段租约(与 NEW-03 同源)

只有**续跑**分支调 `_claim_phase()`。首轮从 DOWNLOADING 直接转 SCORING,
整段裸奔 —— 而那是最长、最贵的一段:

```text
worker A 首轮评分中(SCORING,无租约)
重复消息到达 -> worker B 走 `_run` 的 SCORING 续跑分支
-> `_claim_phase()` 因为没人持有而**成功**
-> 同一批图被两个 worker 同时送进视觉模型
```

`_claim()` 挡不住:那条 UPDATE 只认 QUEUED / REGENERATING。

修法是在**转 SCORING 之前**抢租约(抢在之后会留一道缝,而重复投递是随时
会发生的事)。抢不到时不中止 —— 这一轮的钱已经花了,为一把租约把它丢回去
更糟,降级成上一版的行为,不会更差。

## 2. 报告 §2.3 的文档口径

`docs/REVIEW-A45-BATCH12-4-RESPONSE.md` 写"7 条",实际 6 条。已更正并列出
六条各是什么。不是业务缺陷,但那份文档的读者恰恰是拿它判断"还差多少证据"的人。

---

# 四、改动清单

## 代码

| 文件 | 改了什么 |
| --- | --- |
| `core/field_limits.py` | 新增 `MAX_CANDIDATE_COUNT = 8`,接口校验与耗时预算的唯一来源 |
| `schemas/generation.py` | `candidate_count` 改读该常量 |
| `workflows/dispatch_policy.py` | 三个手写常量 -> 两个纯推导函数;判据换成心跳间隔;余量倍数显式化 |
| `workflows/phase_budget.py` | **新增**。按设置页当前值代入推导,零重依赖,纯测试可求值 |
| `models/generation.py` | `ProviderUsageRecord.billing_key` + 唯一约束 |
| `migrations/versions/0034_usage_billing_key.py` | **新增**。存量重复时报错并给定位查询,不自动合并、不回填 |
| `services/generation_service.py` | `record_usage` 幂等化 + `submit_billing_key()`;回收器加租约判据;阈值改运行时解析 |
| `services/evaluation_service.py` | `evaluate_round` 接受 `heartbeat`,逐张回调 |
| `tasks/generation_tasks.py` | `_heartbeat` / `_LeaseLost` / `_release_lease_before_dispatch`;租约时长改推导;首轮抢租约;三个 submit 计费点接上键 |
| `tasks/maintenance_tasks.py` | 回收任务默认阈值改 None(运行时解析) |
| `scripts/requeue_stranded.py` | `--stalled-after` 默认值改按当前配置算 |

## 测试

| 文件 | 内容 |
| --- | --- |
| `tests/pure/test_a45_batch12_5_fixes.py` | **新增 19 条**。NEW-03 的推导是**调用真函数**算的,不是比对源码数字 |
| `tests/test_a45_batch12_5_lease_and_billing_db.py` | **新增 7 条**,需真 PostgreSQL。费用幂等、重排接手、心跳抗回收、fencing |
| `tests/test_a45_batch12_4_recovery_db.py` | 替身签名跟上 `lease` 形参(否则续跑那一半测的不再是真实签名) |

## 文档

`docs/REVIEW-A45-BATCH12-4-RESPONSE.md`:6 条恢复测试的口径更正。

## 收尾(与本报告首版之间补齐的三项)

| 文件 | 改了什么 |
| --- | --- |
| `Makefile` | `check-offline` 不再因缺 ruff 而中止:pip 工具缺失时**大声跳过**并继续跑其余门禁(工具在时失败照旧中止);顺带把只需 node 的前端语法体检收进离线子集 |
| `backend/tools/run_pure_tests.py` | 缺第三方依赖的用例单列为 SKIP(判定收窄到 import 阶段的非 app.* 顶层模块,断言失败与 app 模块缺失照旧红,变异验证过);`make check-offline` 因此能在无依赖环境跑到底 |
| `docs/swimwear_sample_to_listing_prd_v3_1.md` | PRD v3.1 → v3.1.1:新增 §0.4 同步 batch12-6 实现事实,§1.1 生成链路行与 §13-P0 按已落地状态改写;**生成阶段租约与批次条目租约明确分开**,后者的已知限制保留为独立 P0 待办 |

---

# 五、验证到什么程度 —— 以及没到什么程度

## 跑过的

```text
纯测试                1862/1862 + 2 条缺依赖 SKIP(新增 19 条全绿)
make check-offline    完整跑通:缺 ruff / lint-imports 各记一行 SKIP,
                      四项 verify_* 与前端语法体检(84/84)全部执行
verify_imports        329 个文件
verify_delivery       13/13
verify_sample_data    5/5
compileall            通过
```

## 纯测试做过变异验证

结构断言最大的风险是"写了但抓不住"。四个变异逐个验过:

| 变异 | 结果 |
| --- | --- |
| 删掉 dispatch 前那次释放 | 红 `test_the_task_entrypoint_releases_the_lease_before_dispatching` |
| 去掉落库出口的 `billing_key` | 红 `test_every_submit_usage_record_carries_a_billing_identity` |
| 把心跳挪出评分循环 | 红 `test_scoring_beats_once_per_candidate_inside_the_loop` |
| 预算改回按 4 张算 | 红 `test_the_pause_budget_is_derived_from_the_real_candidate_ceiling` |

## 没跑过的(与上一轮相同)

新增的 7 条真库测试**一条都没执行**。容器没有 PostgreSQL,也装不上
(无网络)。所以下面这些仍然只有"代码结构正确"级别的证据:

- 费用流水在真实唯一约束下的行为
- 租约接管时 `rowcount` 判据在真并发下的表现
- 回收器新增的租约过滤条件对真 SQL 计划的影响

上一轮报告批评过"1824 条全绿同时仍存在自动重复扣费"。**这个风险这一轮
依然存在**,只是位置换了 —— 换到了"结构对了但真库行为没验过"。

---

# 六、准确性反思:我不确定的地方

写在这里的都是**已知风险**,不是猜测。

## 1. 逐张心跳改变了评分阶段的事务粒度

`evaluate_round` 现在每张图之间提交一次。好处是明确的:上一版整轮共用一个
事务,第 5 张崩掉时前 4 张**已经调用过、已经计费**的视觉请求会随 rollback
一起消失(报告 NEW-03 里"已产生的视觉模型费用可能没有正确进入台账"说的
就是这个)。

代价是:`_apply_decision` 之后如果失败,已提交的逐张评分不会回滚,续跑时会
重评一遍,产生重复的 `candidate_evaluations` 行。查过 —— 那张表没有唯一约束,
而且续跑路径本来就会重评,所以不是新增行为。**但它确实让这种情况更容易发生。**

## 2. 首轮抢不到租约时的降级

选择了"记警告并裸奔跑完",理由是钱已经花了。这意味着在那条罕见路径上
NEW-03 的保护不生效。判断是对的,但它是一个**静默降级** ——
只有日志会说,没有任何指标会亮。

## 3. 租约默认时长从 900 变成 1080

推导出来的(3 × 360)。副作用:一个真死掉的 worker 最多多挡 180 秒。
可接受,但这是一个没有被任何测试断言的行为变化。

## 4. 迁移 0034 会在有存量重复时**阻塞升级**

刻意的:迁移脚本没有资格替人判断"这两行里哪一行才是真的",那取决于厂商
账单上实际收了几次。但部署时必须知道这件事 —— 如果测试库里已经跑出过
重复流水,`alembic upgrade head` 会直接报错并列出重复的 attempt。
处理方式写在迁移的 docstring 里。

## 5. `record_usage` 现在会 flush

传了 `billing_key` 时先 `session.flush()` 再查(会话是 `autoflush=False` 建的,
不 flush 就看不见同一事务里刚 add 的行)。逐个查过三个调用点,那一句都是
no-op(`_finish_attempt` / `_persist_candidates` 刚 flush 过)。
**但这是一个时机改变**,如果将来有人在未 flush 的状态下调用它,行为会不同。

## 6. 报告里"可以推迟"的那条没有动

候选数据库回滚后的孤儿存储对象。同意报告的判断(内容寻址,重试会复用同一
对象,不影响正确性),本轮按"不扩大范围"的要求跳过。

---

# 七、人工测试准入建议

与报告第八节的判断一致,只改动其中两项:

```text
允许继续下一轮修复                YES
允许普通 Mock 人工测试            YES
允许真实付费 Provider 幸福路径小额  谨慎允许(与上轮一致)
允许真实付费异常恢复测试           NO  <- 要等真库测试跑过
允许冻结为正式人工测试版本         NO
```

## 下一步只有一件事

**找一个有 PostgreSQL 的环境,把这 13 条真库测试跑完**
(batch12-4 的 6 条 + batch12-5 的 7 条)。

在那之前,费用看板准确性验收和异常恢复验收都不该开。跑完之后如果全绿,
剩下的阻塞项就只有报告第八节里那些需要真实 worker kill / Broker 重投 /
网络分区的场景 —— 那些是环境问题,不是代码问题。
