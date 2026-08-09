# CLAUDE.md — 仓库总纲

**服装 AI 图片生成与 API 自动上架系统。** 后端 FastAPI + Celery + PostgreSQL,
前端 React 18 + Vite + antd,图片走 Provider(FASHN / ComfyUI / mock),
产出 Listing 后经渠道 Adapter 自动上架。

**范围是服装,不是只有泳装。** 品类是参数:渠道字段 spec 按
`field_spec(category_id=...)` 读 `spec/{category_id}.yaml`,属性注册表按品类校准。
泳装是**目前唯一已校准、且有渠道 spec 的品类**(`CATEGORY_ID = "swimwear"` 是默认值),
所以代码里、样例数据里、评分提示词里出现「泳装 / swimwear」的地方,
多数是在如实描述那个品类,**不是待清理的旧称**。改这类字样之前先分清是哪一种,
判据与三类不动的东西见 `docs/DECISIONS.md` §3.20。

**动手之前先读 `docs/REVIEW.md`。** 那是本仓库的施工方案(a20 v4.1),
第 12 章有任务表和依赖图,已完成项已划掉。它不是背景资料,是验收口径 ——
`backend/tools/verify_delivery.py` 里有一半检查直接引用它的节号。

## 目录

```
backend/          FastAPI + Celery + SQLAlchemy2 + Alembic   → backend/CLAUDE.md
frontend/         React18 + Vite + antd + Vitest + Playwright → frontend/CLAUDE.md
comfyui/          ComfyUI 工作流模板与配置样例
sample-data/      10 个示例商品 + 30 张示例图(首次演示用)
docs/             REVIEW.md(施工方案)、vendor/(第三方接口参考)
tools/pack.sh     交付打包:先按黑名单排除,打完再解开复验
.github/workflows/ci.yml   门禁执行者
HANDOVER.md       最近交接。过程文档不留档 —— 结论进 docs/DECISIONS.md
```

## 门禁

```bash
make check          # = check-offline + fe-check,需联网(前端要装依赖)
make check-offline  # 后端子集:纯测试 + ruff + 架构契约 + 交付自检 + 样例数据
```

CI(`.github/workflows/ci.yml`)跑六个 job:`gates` / `backend` / `frontend` /
`e2e` / `images` / `all-green`。分支保护挂 `all-green` 一个就够。

**`make check-offline` 跑绿 ≠ 全都过了。** 它不含前端类型、lint、Vitest 与构建;
目标自己会在末尾打印这句话。改了前端还只跑 offline,等于没验。

**`make check` 跑绿也 ≠ CI 会绿。** 这里原来写的是「一条命令跑全部」,
而 `check = check-offline + fe-check`,两者都**不跑 `pytest`**。
`make check` 覆盖不到的是整整三个 CI job:

```
backend  真库 pytest + Alembic 升降级 + -O 冒烟   ← 要 PostgreSQL + Redis
e2e      Playwright                              ← 要 npx playwright install
images   docker build ×2                         ← 要 docker daemon
```

也就是说本地能跑的那一份**结构上就比 CI 窄**,而窄的方向永远是更松。
`verify_delivery.py` 里那条检查的措辞是「make check **覆盖前端门禁**」——
它是准确的,被夸大的是这一段。要在本地逼近 CI,得自己起库与 Redis 跑
`make test`,再加 `make fe-e2e`;docker 那两条本地没有等价物。

### 本地真实基础设施验证须由用户明确触发

日常协作默认只运行不依赖真实 PostgreSQL / Redis 的验证。**没有用户在当前任务中的
明确指令,不得**设置真库测试环境变量、运行 `requires_db` 用例或 Alembic 真库升降级,
也不得连接 Redis 做 PING、Celery worker / broker 或其他 Redis 集成验证。

如果改动的完整验收确实需要上述验证,本轮只做两件事:在交付说明中明确标为
「未执行」,并提醒用户需要验证的范围与建议命令;等用户明确指令后再执行。
这条是本地协作执行约束,**不改变** CI、发布或阶段验收原有的真库 / Redis 门禁,
也不允许把未执行写成已验证。

## 硬规则

1. **凭据不进仓库树。** `.secrets/` / `.env` / `*.key` / `*.pem`。
   这条有过事故:主密钥连着两个交付包出去,第二个包和它毫无关系。
   三道拦截:`.gitignore` → `tools/pack.sh` 打完复验 → `verify_delivery.py`。
   两个测试运行器现在都自带固定测试密钥 + 临时密钥目录,所以**跑测试不再需要
   人工先设 `SETTINGS_SECRET_KEY`**(细节见 `backend/tests/conftest.py` 顶部
   与 `backend/tools/run_pure_tests.py` 顶部)。这里原来写的是「跑测试前先确认
   `SETTINGS_SECRET_KEY` 有值」—— 那是一条**靠人记住**的防线,而它漏了:
   任务 4 当时只修了 pytest 那条路,`make test-pure`(文档里最常被执行的命令)
   在裸 shell 下照样会在仓库根真的生成密钥文件,而且测试全绿、没有任何提示。
   A42 补上了。**新增第三个运行器时,同样的三行必须跟着走。**

2. **打包只用 `make pack V=aNN`。** 手打 `zip -r` 是上面那次事故的根因 ——
   手打的命令没有记忆。

3. **门禁清单不许只写在文档里。** `verify_delivery.py` 会逐条在
   `.github/workflows/ci.yml` 里找**命令字面量**(不是 step 名字)。
   加一条门禁 = 改三个地方:Makefile、ci.yml、`check_ci_runs_every_gate()` 的表。

4. **前端不许推测状态。** 后端返回 `display_status` / `next_action` /
   `blocking_reasons` / `allowed_actions`,前端只展示和触发。
   反面教材是 `describe_extractors()` 曾经硬编码 `configured: true` ——
   前端老实展示了,错在后端给了一个不是从真实来源推出来的值。
   **后端返回的每个状态字段都必须能追溯到真实来源,不许为了接口形状完整填常量。**

   **这条有第二次事故(A42 修的),形式相反:不是填了假常量,是漏了一整列。**
   `core/environment.build_facet()` 读 `implemented` / `configured` /
   `is_simulator` 三列,而 `providers` 与 `evaluators` 两个注册表从来没报过
   第三列。缺列不报错,`bool(None)` 是 False,而 Mock 的 `is_configured()`
   恒为 True —— 判定一路走到 REAL,状态条对运营说「真的在调外部出图服务,
   会产生费用」。默认 Mock 环境下四档错了三档,而**两侧的测试全绿**:
   判定被 8 种组合穷举钉死,注册表也有形状测试,没有一条跨过中间那道缝。
   所以这条规则要加一句:**接口形状不是靠两端各自的测试守住的,得有一条真的
   调注册表、真的跑判定的用例**(`tests/pure/test_environment.py` 的「注册表接缝」)。
   新增一个会被 `build_facet` 读的注册表时,先去那一节加一行。

5. **注释写「为什么」,不写「是什么」。** 本仓库的注释密度偏高是刻意的:
   大部分注释记录的是某个决定背后踩过的坑。改代码时如果发现注释和代码对不上,
   先查是哪一边过时了 —— 别默认删注释。

## 当前进度(2026-08-06)

Phase 0(门禁与工程基线)**代码侧已清空**:CI 建立、Vitest 接线(0 skip,这一条由
`verify_delivery.py` 的「前端用例 0 skip」守着;**条数不写在这里** ——
原来写的「56 条」在加到 57 条时静默过期了,而 `docs/STATUS.md` 第五节
正是为这类数字立的规矩)、Playwright 骨架、密钥副作用修复、第一批低价值测试迁移、
**筛选状态进 URL(GAP-033,A45-batch14-17 —— 清单上最后一条代码项)**。

**剩下三项都不是代码问题,是执行环境问题**:容器里下不了 Playwright 浏览器、
`docker build` 没跑过、「连续两次全绿」要真实 runner。这三条现在是
「写好了、没实测」,阶段 0 **未收口**。别把「代码侧清空」读成「阶段 0 完成」。

Phase 3 起步:任务 17 / 18(批次租约与异常恢复)已落地,见下面单独一节。

**PRD §13 的阶段 5 已开工**(注意:是 PRD 的阶段划分,**不是** `REVIEW.md` 的
P5 那一格 —— 那里只有任务 24 Playwright)。两个批次已落码:

```
5-1  草稿的颜色维上游快照:两列 + 迁移 0049 + 零依赖判定层   A45-batch19
5-2B 接线:build_draft 写快照、refresh_draft 读、READY 门禁    A45-batch20
5-3  文案幂等单元(不含尺码)+ 迁移 0050                       A45-batch21
5-5  导出预览读同一份已存映射                                 A45-batch22
5-4  颜色投影与确认流(`display_name` 的唯一写入点)           A45-batch23
```

**五项交付到此全部落码。** 排期、五项与批次的对应、AC 证据与仍欠的账在
`docs/REVIEW-STAGE5-5-1-CONCLUSION.md`。

**PRD §13 的阶段 6(一体化向导)也已全部落码**,五批:

```
6-1  判据重述签认 + 颜色子态判定层                      A45-batch25
6-2 前置 步骤表的四条穷举守卫 + 权重常量改表            A45-batch26
6-2  七步增维 + 完成度口径迁移(**口径变过一次**)      A45-batch27
6-3  聚合工作流 API(AC-15)+ 总览页颜色维              A45-batch28
6-4  七步向导 UI + 刷新恢复 + **AC-05 服务端门禁**      A45-batch29
6-5  费用预估 + 上游变化影响提示(AC-17)               A45-batch30
```

分批理由、AC-05 为什么原来不属于任何一批、以及**五批之后仍然欠着的四件事**
在 `docs/REVIEW-STAGE6-CONCLUSION.md` §六 / §七。那四件里最要紧的两件:
**浏览器一次都没实测**(Playwright 在任务 24),**真库一次都没跑**。

阶段 6 带来过两次运营看得见的口径变更,都记在 `docs/STATUS.md`:
完成度五步等权变七步不等权(batch27);没配生成方案的商品阻断数从 0 变 1
(batch29 —— 那条阻断本来就在,只是被一个位置参数吞进了 `summary`,
见 `DECISIONS.md` §3.59 第一节)。

**而 `DELIVERY_STAGE` 仍然是 4 —— 这一次不是因为没做完。**
`还款日:阶段 N` 的语义是「推进到 N 之前必须还清」,推到 5 会让
11 条列写入欠账 + 3 条欠账守卫当场逾期变红,而**那 14 条都不是阶段 5 的
交付项**(识别侧 token 计量、原始响应留存、几个缺入口的 UI 字段)。
「五项落码完毕」与「标记可以推进」是两件事,别把它们合并计算。

Phase 2 进行中:任务 11(三张发布域表 + 迁移 `0022` + 幂等键)、任务 12
(渠道 API Simulator,九种行为,**无状态** —— 场景编码在 external_spu_id 里)、
任务 13(`generic.build_request`)、任务 14(幂等创建与更新)、任务 15
(状态轮询与驳回回流)、任务 16(DELIST 与测试商品清理)、
任务 25(发布接口)均已落地。

任务 14 把上面几样接成了一条真的会写库的链路,分三个模块:

```
app/workflows/publish_policy.py   纯判定:409/429/超时怎么算、退避多久、什么时候放弃
app/channels/registry.py          渠道 -> 谁构造报文、谁发出去(is_simulator 由此推出)
app/services/publish_service.py   事务编排:enqueue 不 commit,投递分三段事务
```

**改发布链路之前先读 `publish_service.py` 顶部的「事务边界」。** 那四行
(业务事务 / checkpoint / 事务外 / 新事务)是 7.8 节的硬要求,
`tests/pure/test_publish_policy.py` 末尾用 AST 钉着,不是靠自觉。

任务 15/16 又接上了后半段。发布链路现在是七个模块:

```
app/workflows/publish_policy.py   纯判定:提交响应怎么算、退避多久、什么时候放弃
app/workflows/poll_policy.py      纯判定:轮询回答怎么算、退避多久(轮询**不通往放弃**)
app/channels/registry.py          渠道 -> 谁构造报文、谁发出去(is_simulator 由此推出)
app/services/publish_service.py   事务编排:enqueue 不 commit,投递分三段事务
app/services/poll_service.py      事务编排:领取推进 next_poll_at 即租约,同样三段
app/services/cleanup_service.py   4.1 节 H:列清单 / 核对 / 下架,先看后做
app/workbench/platform_service.py 驳回台账(`record_api_rejection` 是轮询的落点)
```

改这条链路之前先读四条**已经踩过的坑**:

1. **轮询封顶的是频率不是次数。** 投递重试每次都花钱,所以有放弃;轮询是读,
   放弃意味着本地看板和平台永久分叉,所以只有 `MAX_POLL_INTERVAL_SECONDS`。
2. **404 永远不写 DELISTED。** 查不到可能是 ID 错了。猜成已下架的代价是一个
   仍挂在平台上的商品从清理清单里消失 —— 4.1 节 H 要防的结局本身。
3. **API 上架的驳回不走 `locate_export()`。** 那个函数找不到导出记录就抛 409,
   而 API 上架的商品可能一辈子没导过 Excel。走 `record_api_rejection()`,
   定位依据换成提交尝试(`located_by="publish_attempt"`),表和状态机不变。
4. **时间一律走 `core/clock.utc_now()`。** `db/session.py` 在连接上钉死了
   `-c timezone=utc`。别在新模块里重新写 `datetime.now(UTC).replace(tzinfo=None)`
   —— 那是 A27 那轮在收敛的东西。
   **注意这里原来写着「全仓只有这一份『现在几点』」,那句话不成立** ——
   还剩 14 处直接 `datetime.now(UTC)`,清单与影响评估在 `core/clock.py`
   的「收敛没有做完」一节。这条是对**新代码**的要求,不是对现状的描述。
   **这个数字改过四次(18 → 17 → 16 → 14),别照抄下游文档里的旧值** ——
   `core/clock.py` 那一节写了怎么用 AST 重新数,手数漏过一次,
   **建成清单之后又漏账过一次**(A45-batch17-2:`cleanup_service.py` 与
   `model_license.py` 两处从未进过清单)。现在有
   `tests/pure/test_a45_batch17_2_clock_ledger.py` 每次现数一遍并和清单比对,
   所以这个数字从本批起是**被守着的**,不是被记着的。
   A34 收掉了 `batch_service.py:1007`;A41 收掉 `batch_service.py` 的 3 处
   (回执写入 / `create_batch` / 导出文件落库 —— 它们的 `now` 全部用途都是写库,
   归一到入口语义不变,不需要真库);A45-batch17-2 收掉上面那漏账的两处。
   **这里原来还写着「A34 收掉的是全仓唯一一处原样写着那个被点名禁止的形式的
   地方」—— 那半句是假的**:`model_license.py:53` 一直原样长着它,
   到 A45-batch17-2 才收。剩下 14 处是"aware 的 now 赋给变量、
   用到的地方各自归一",收敛它们要动发布链路的事务编排,必须带真库跑。
5. **出参里的时间戳走 `core/clock.iso_utc()`,不要裸 `.isoformat()`。**
   `SessionLocal` 配的是 `expire_on_commit=False`,于是"刚写完就回读"拿到的是
   Python 侧的 naive 值、"刷新页面重新查"拿到的是 timestamptz 的 aware 值 ——
   **同一列同一个接口会序列化出两种形状**。前端 `utils/datetime.ts` 两种都认,
   所以它一直没表现出来;导出文件和第三方消费者会看见。A41 收了工作台与批次
   两组出参,`platform_service` / 发布接口那几组还没收。

任务 25(发布接口)把这条链路接了出去。**判定不在接口层**:

```
app/workflows/publish_view.py     纯判定:四个来源 -> display_status / next_action /
                                  blocking_reasons / allowed_actions
app/api/publish.py                六个端点,只做取数 / 调判定 / 持有事务
```

改发布接口之前先读两条:

1. **`STALLED` 是一个组合状态,`PublishStatus` 里没有它。** listing 说 SUBMITTING、
   attempt 说 IN_FLIGHT、outbox 已经 DEAD —— 四个来源没有任何一个单独知道
   「这件事死了」。少了它,界面会一直说「提交中」,运营会一直等。
   `EnqueueResult.reused_terminal` 的消费点也在这里(`describe_enqueue`)。
2. **判定必须留在 `workflows/`。** 那里零依赖,所以能在 `tests/pure/` 被穷举;
   搬进接口函数之后,覆盖「某个状态组合下按钮不该亮」就要起一个 FastAPI
   加一个库,而那种测试没人会为一个枚举分支去写。

**任务 20 已经拆成两件事(A45-batch18 / P3),别再当一个号用。**

    20-A  发布前端页面与状态操作   ✅ 已交付(`pages/PublishPage.tsx` + 路由),
                                  但**浏览器未实测** —— Playwright 在任务 24
    20-B  API 驳回的 resolve_gate   ✅ 新数据路径已落码(2026-08-09 评审订正),
                                  缺真库 seam 测试;旧数据仍需人工

拆开的原因是三份文档曾经对"任务 20 完成了没有"给出三个不同答案,
而它们各对了一部分:页面确实做完了,`resolve_gate` 确实还没有。
一个号绑着两件依赖与排期都不同的交付物,排期就得靠猜按哪一句算。

**20-B 已经落码,这里原来写的「下一步是 20-B」已过期**(2026-08-09 评审订正)。
`platform_service._publish_attempt_entries()` 把「驳回之后有一次**成功的**
提交尝试」当作等价证据喂进 `resolve_gates()` 与解决路径,关联口径是
`ChannelListing.draft_id`,指纹那半边仍落在当前草稿指纹上 ——
**所以没改草稿的重复提交过不了闸**。只认 `SUCCEEDED`:PENDING / IN_FLIGHT
还没有结果,UNKNOWN 不知道平台收没收到,FAILED / ABORTED 根本没提交成功。

仍然欠着两件,别把它们读成"没做":

    真库 seam   从一行真实 PublishAttempt 穿过 platform_service 到驳回关闭,
                没有一条真库用例走完过(判定与服务接线都有纯测试)
    旧数据      `draft_id IS NULL` 的历史驳回关联不上尝试,仍然只能人工标记

**发布接口 = 任务 25(A40 定的)。** `REVIEW.md` 12.1 的任务表里 18 一直是
「Batch Outbox 与异常恢复」,和发布 API 无关。这个号以前被两处占着,
A35 把 18 真的做完之后冲突就不再是口径问题而是事实冲突了,所以补号给发布接口。
理由见 `docs/DECISIONS.md` §3.11,补号行在 `REVIEW.md` 12.1 表末。
**看到旧文档里写「任务 18(发布 API)」,那是改名之前的叫法。**

付费调用花费台账已落地:`/spend` 页 + 全局预算告警横幅,数据来自
`provider_usage_records` 的真实行。**它是本系统的台账,不是厂商账户余额** ——
文案一律「预算」不许写「余额」,理由见 `backend/app/services/spend.py` 顶部。

## 批次可靠性:领取靠租约,恢复靠回收(任务 17 / 18,A35)

批次执行以前是「一次读走全部 PENDING,然后慢慢跑」。那一读没有加锁,
而且 worker 死掉之后它领走的条目**永远停在 RUNNING**:

```
app/workbench/batch.py          判定:ITEM_LEASE_SECONDS / MAX_ITEM_ATTEMPTS
                                lease_expired() / reclaim_verdict() / WORKER_LOST
app/workbench/batch_service.py  claim_items()  分批领,FOR UPDATE SKIP LOCKED + 租约
                                settle()       条件结算,finished_at 双向写
                                reap_expired_leases()      回收(跨批次全表)
                                redispatch_stalled_batches() 重投(不建 outbox 表)
app/tasks/maintenance_tasks.py  reap_batch_leases  beat 每 60 秒一拍
```

改这一段之前先读四条:

1. **租约必须长于「一次领取的全部条目顺序跑完」的最长合法耗时**,
   `batch.py` 里有 assert 守着,而且**那条 assert 必须乘 `CLAIM_CHUNK`**。
   回执表挡得住「跑完的不重跑」,挡不住「正在跑的又来一次」——**回执是调用之后
   才写的**。租约本身是唯一防线。
   **这里原来写的是「长于单件最长合法耗时」,口径写窄了,而防线因此从未生效:**
   A35～A41 期间 `CLAIM_CHUNK = 10`、`ITEM_LEASE_SECONDS = 1800`、
   单件最长合法耗时 1080 秒 —— 一次领取盖一个共用的 `lease_until`、
   执行期间不续期、`run_batch` 顺序跑,所以第 3 件开跑时租约就过期了。
   单件断言(1800 > 1080)在整个期间都是绿的。A42 把 `CLAIM_CHUNK` 搬进
   `batch.py`(常量必须和不变量在同一个模块里,否则 assert 看不见它)并收成 1。
   **要调大它必须先做续租**,否则 assert 会在导入期直接拦住。
2. **`lease_until IS NULL` 算已过期。** 与 `publish_service` 那条
   `next_attempt_at IS NULL` 是同一个坑的两面(§3.13):那边漏掉 NULL 是
   行永远领不到,这边把 NULL 当「永不过期」是存量残骸永远回收不了。
   判定口径在 `batch.lease_expired()` 一处,两处 SQL 必须与它同向。
3. **重投不许在没有租约的前提下做。** 「重投一次」在无锁领取下等于
   「再跑一遍」,而那是重复的付费调用。任务表把 18 排在 17 后面是安全依赖,
   不是排版。
4. **`claim_items` 不在 `WIRED_MODULES` 里**,因为它的唯一调用点在同一个
   文件(那条门禁刻意排除模块内互调)。它由 `tests/pure/test_batch_lease.py`
   用 AST 钉着 —— 改回无锁 SELECT 会让租约变成一个没人写的列,
   而回收器会把**正在跑**的条目当成残骸。

未做:任务 20-B 起的 Phase 4/5(20-A 页面已交付,浏览器未实测)。**任务 19 两半都已落地**(N+1 在 a38,事务边界在 a42),
但两半守的都是源码形状,不是运行时行为 —— 见下面单独一节。
**任务 5 / 6 已由 a37 完成**(12.1 表已标 ✅);这里原来还写着「未做:任务 5、6」,
和任务表对不上 —— 改了表没改总纲,正是 A38 走读点名过的那类过期。
**任务 7 / 9 已由 A45-batch14 / batch14-18 完成**;这里原来也还写着
「未做:任务 7(真实多模态抽取器)、任务 9(FASHN ProviderCall 持久化与 Usage)」,
**那句话在 A45-batch17-2 之前一直是错的** —— 任务 7 落在 `app/extractors/vision.py`
(注册表接线在 `api/attributes.py`、`workbench/batch_service.py`),任务 9 落在
`providers/fashn.py` + `providers/base.settle_billable_units` + `units_source` 列
(迁移 0039),调用点在 `tasks/generation_tasks.py`。两者都**从未连过真端点**,
`docs/STATUS.md`「已知限制」如实记着 —— 但"没连过真端点"和"没做"是两件事,
按这一段开工的人会去重做一遍已经写好的东西。
同一段话在同一份文件里连着说错三次(5/6、7/9),原因每次都一样:
**12.1 表改了,总纲没跟着改。** 下面那两行 236-237 恰好就在点名这一类过期。

## 请求的事务边界:归接口所有(任务 19 后半,A42)

`db/session.py::get_session()` **不再替所有请求提交**。它只剩异常回滚与关闭。

```
写端点      自己 session.commit()。漏了 = 接口返回 200 但什么都没存下来
GET 端点    一律不提交。唯一例外 download_batch_file(只增不改的下载审计流水)
批次执行    跨付费调用的长事务是署名例外,不要"顺手修好"(理由见下第 3 条)
```

改这一段之前先读三条:

1. **提交这件事,集成测试结构上验不了。** `tests/conftest.py` 的 `client`
   夹具把 `db_session` 覆盖成一个不提交的 session(每个用例一个事务,结束回滚),
   同一个 session 里读得到未提交的写 —— 于是「真的提交了」和「只是 flush 了」
   在 API 测试里完全等价。**唯一防线是
   `tests/pure/test_transaction_boundaries.py` 的「HTTP 边界」一节。**
   加白名单绕过它之前,先明白你绕过的是这件事的全部防线。
2. **白名单放行的是「不提交」,不是「不用管」。** `preview_import` 在白名单里,
   同时被反向钉着**一处会话写都不许有** —— 它哪天真开始写库,缺的那次提交
   会当场变红,而不是变成一个悄悄不落库的接口。
3. **批次执行的长事务不许"修"。** `try_advisory_xact_lock` 是事务级锁,
   必须活到回执对别人可见那一刻(也就是提交)。提前 commit 把锁放掉,
   第二个请求拿到锁、查不到回执、照样调一次付费模型。发布链路能分三段,
   是因为它的幂等靠唯一键不靠锁 —— 两条链路的幂等机制不同,事务形状因此不同。

兜底任务的返回形状也归在这一节:**异常分支不许手抄一份返回字典**。
成功路径加一个键时手抄的那份不会跟着变,而读它做看板的一侧恰恰在出错时
拿到另一种形状。`relay_dispatches` 与 `reap_batch_leases` 都改成从成功路径
那个构造器取形状,门禁盯着。

## 轮询节拍是三档,不是两档(A32)

前端凡是决定「还要不要继续问」的地方,都不许写成「终态就停、非终态就问」。
中间还有一档:**机器不再写它了,但有人点一下就会继续走**。

```
生成任务  api/types.ts   taskLiveness()   TERMINAL / AWAITING_HUMAN / LIVE
批次      api/batch.ts   batchLivenessOf() SETTLED  / STALLED        / LIVE
```

三份清单都钉在后端,`tests/pure/test_frontend_contract.py` 逐值比对:
任务的终态那份等于 `state_machine.TERMINAL_STATES`,**中间那档等于
`state_machine.AWAITING_HUMAN_STATES`**(A34 补的 —— 在此之前它只是一份
写在前端的清单,负向约束挡得住放错值,挡不住后端新增一个等人动的状态而
前端不知道);批次那份直接读后端现算的 `liveness` 字段(硬规则第 4 条)。

批次那一档还有一条**A34 修掉的坑**,改判定之前必读:`job.status` 和条目
不是同一份事实。`reset_items_for_retry()` 只把条目打回 PENDING,不动
`job.status` —— 于是"重试之后投递失败"这条动线上,status 停在终态而条目
还有一堆没跑的。只看 status 会判成 SETTLED,轮询彻底停止。
**两份事实冲突时信条目**,细节在 `workbench/batch.py` 的
「两份 status,信条目那一份」。

改这里之前先读一条**已经踩过的坑**:前端曾经把 `FAILED` 与 `MANUAL_REVIEW`
列进终态,而后端这两个都有出边。后果不是多刷几次,是**页面永远停在旧状态**
—— 别人推进之后这一边再也不知道。判定的名字也一并改了:`isLiveTaskStatus`
删掉了,因为那个名字说「活着」而实际含义是「非终态」,缺陷正是从这个歧义
里长出来的。
