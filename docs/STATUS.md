# 当前状态

<!-- DELIVERY_STAGE: 4 -->
<!-- CODE_STAGE: 6 -->
<!--
  两个标记,两件事。别把它们合并计算。

    DELIVERY_STAGE  **欠账结算阶段**。语义:还款日 <= 它的欠账必须已经还清。
                    `verify_delivery.py` 的「欠账守卫都在还款日之内」读它。
                    它是一条**闸**,不是一份进度报告。
    CODE_STAGE      **已落码阶段**,如实描述代码推进到 PRD §13 的第几阶段。
                    不参与任何闸,只负责让上面那个落差看得见。

  两者的差就是欠着的账。`check_stage_markers_are_consistent()` 只钉一个方向
  (CODE_STAGE >= DELIVERY_STAGE)—— 反过来意味着结算跑到了落码前面,
  那只可能是有人为了让门禁变绿而调高了闸。

  改 DELIVERY_STAGE 之前先想一遍:每往上加一,所有写着「还款日:阶段 N <= 新值」
  的欠账守卫会当场变红。**那是它存在的全部意义。** 变红时该做的是还上那笔欠账,
  或者把还款日往后改并写清为什么 —— 不是回来把这个数字调回去。
  改 CODE_STAGE 的判据只有一条:PRD §13 那一阶段的交付项是不是都落码了。

  为什么拆成两个:docs/notes/2026-08-09-two-stage-markers.md
-->

这份文档回答一件事:**某项能力现在到底能不能用。** 正文在下面,逐轮的历史快照
按日期归档在 `docs/notes/`,索引在文件末尾。

## 一、能力现状

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 商品与素材 | ✅ 可用 | CRUD、批量导入(逐行计划 + 行级错误)、上传格式探测、双层哈希去重、原始文件永不覆盖、审计日志 |
| Provider 抽象 | ✅ 可用 | 虚拟试穿与商品转模特图两种模式;9 类错误各带 retriable / switchable / requires_human 策略 |
| Mock Provider | ✅ 可用 | 用 Pillow 真出图;可模拟成功/失败/超时/空结果/限流/内容安全 |
| FASHN | ⚠️ 已实现,未用真实 Key 验证 | 请求/状态/错误映射、轮询、自检齐备。首次验证步骤见 `docs/PROVIDER-FASHN.md` 第八节 |
| ComfyUI / fal.ai | 🚧 骨架 | 能力声明、配置检查、连接测试可用;请求映射待官方文档确认。创建任务时即被挡下,不会跑到 worker 才失败 |
| 状态机与幂等 | ✅ 可用 | 17 个状态完整转移表,非法跳转 409,终态封闭;幂等键由输入派生,每轮 seed 可复现 |
| 异步编排 | ✅ 可用 | Celery 流水线、协作式取消、Outbox + `make requeue` 兜底、每次调用落 attempt 与用量流水 |
| 评分与分档 | ✅ 可用 | 11 个维度;A/B/C/D 三条线取最差(硬错误一票否决、分数区间、问题严重度上限);总分由后端算,模型自报分只留档 |
| 视觉大模型评分器 | ✅ 可用 | 一套代码四种后端(OpenAI Responses / OpenAI 兼容 Chat / 火山方舟 / 阿里云百炼),按 `VISION_MODEL_API_STYLE` 分适配器。生产 fail-closed。详见 `docs/VISION-EVALUATOR.md` |
| 提示词统一管理 | ✅ 可用 | a56 起有机器可读的注册表(全仓 8 处提示词的盘点);a58 起 `/prompts` 是**列表 + 详情 + 单版本只读**三页,详情按路由 `:key` 走 ——男装提示词因此从「后端通、前端不可达」变成可编辑。**不可编辑的 6 处照样列出**并显示注册表里写的原因(它们的正文由代码拼装,存进库不会生效)。「近 7 天调用次数」那一列**刻意还没有** —— 后端不给算不准的数,前端不凑近似值。**a71 起 typecheck / lint / Vitest / build 四层跑过了**(原文「浏览器一次都没打开过(无 node_modules)」已过期);**真浏览器里仍然一次都没打开过** —— 那要 Playwright(任务 24)或人工 |
| AI 测试留档 | ⚠️ 已落码,未在任何库上跑过 | a57。评分与文案两条诊断链路各自留档到 `ai_test_runs`(迁移 0056),评分只落指向 `EvaluationAttempt` 的指针,文案落全文(它在此之前**一行都没存**,关掉页面就没了)。失败的测试也留档。写入失败**吞异常但不静默**:走 `eval.ai_test_record_failed` 进运行日志 —— 留档发生在付费调用之后,抛出去等于钱花了、结果也没了。历史表页面(FE-312~315)与读接口(BE-311)**已落码**(a62):`GET /ai-tests/runs` 在 `api/ai_tests.py`,历史 UI 在 `AITestPage`,`test_a62_ai_test_query.py` 27/27。**仍未在任何库上跑过** —— 整行那个 ⚠️ 说的是这件事,不是说没写 |
| 人工审核队列 | ✅ 可用 | 审核对象是**任务**不是候选图;A 档按比例随机抽检(不阻塞) |
| 多尺寸成品图 | ✅ 可用 | 五个用途,**绝不放大**;正方形补边不裁剪;重算不删旧图(旧 URL 仍可打开) |
| 渠道 API Simulator | ✅ 可用 | 九种平台行为（创建/更新/幂等冲突/限流/鉴权失败/字段拒绝/异步审核/发布成功/平台驳回）。**无状态**：场景与创建时刻编码在 external_spu_id 里，多进程与重启后行为一致。SPU 以 `SIM-XXX-` 开头即可触发对应场景 |
| 渠道报文构造 | ✅ 可用 | `generic.build_request` 纯函数，干跑与真实提交共用同一段构造逻辑；非创建操作缺 external_spu_id 当场抛错 |
| 自动上架与更新 | ✅ 可用 | 幂等创建/更新,三段事务(业务事务 / 事务外调用 / 新事务保存),投递走 `publish_outbox`。提交超时不猜结果,落 `SUBMIT_RESULT_UNKNOWN` 等人确认 |
| 平台状态轮询 | ✅ 可用 | 到期轮询 + 指数退避(封顶 1 小时,**不通往放弃** —— 轮询是读,停下来等于本地与平台永久分叉)。404 绝不当作已下架。平台驳回自动进既有驳回台账,`located_by=publish_attempt` |
| 下架与测试清理 | ✅ 可用 | `DELIST` 不看草稿状态、不带报文内容;4.1 节 H 清理预案见 `make cleanup`,必须限定作用域,默认只看不做,`verify` 未清干净时退出码 1 |
| 发布接口 | ✅ 可用 | 六个端点(提交/清单/详情/刷新状态/下架/清理清单)。状态派生只有一份,在 `workflows/publish_view.py`:后端给 `display_status` / `next_action` / `blocking_reasons` / `allowed_actions`,前端只展示和触发。多一个状态机里没有的 `STALLED` —— listing 说在途而 outbox 已 DEAD 的组合,少了它界面会一直显示「提交中」。前端在 `pages/PublishPage.tsx`(**任务 20-A**,B-02 已关闭;浏览器未实测,Playwright 在任务 24) |
| 身份与权限提示 | ✅ 可用 | `AUTH_FORBIDDEN` 与 `AUTH_FAILED` 分开:「登录态失效」与「权限不足」在前端是两句不同的话,且 403 不清登录态、不跳登录页。a46-phase6 之后浏览器不持有任何 Token,「口令写入失败」一族已随录入卡退役 |
| 付费调用花费台账 | ✅ 可用 | 按月/provider/天汇总,预算档位与耗尽日外推;金额取整数微单位。**是本系统台账不是厂商余额**,未配价的调用单独计数不计入金额 |
| 导出 | ✅ 可用 | 单件与批量,JSON + CSV + XLSX;CRLF + utf-8-sig;批量上限 1000 行 |
| 属性识别与确认 | ✅ 可用 | 一图一字段一条证据;未校准品类不自动确认;逐字段确认,批量确认排除冲突项 |
| 图片集编排 | ✅ 可用 | 拖拽 + 键盘;批准后不可原地改,重排 derive 出新版本 |
| 渠道文案 | ✅ 可用 | 生成 → 校验(禁词/声明/长度)→ 人工编辑落新版本 → 批准前强制重校验 |
| 上架草稿与导出闸 | ✅ 可用 | 手填字段从渠道 spec 反推;过期提示按 §4.5.1 展开(哪个上游、哪些字段、做什么) |
| 批量运营与异常页 | ✅ 可用 | 计数即筛选;异常按「步骤 → 问题码」两层分组;付费动作执行前确认 + 幂等回执。a71 起每个问题分组可**复制 SKU 清单 / 导出 CSV** —— 这一页把「十几件都是缺背面图」算出来了,而下一步(发给摄影、发工单)常常不在系统里,在此之前只能逐行手抄。CSV 走 CRLF + UTF-8 BOM,与后端导出同口径;它是**给人看的工作清单,不是上架文件**,判据写在 `utils/csv.ts` 顶部 |
| 批次原子领取与租约 | ✅ 可用 | 任务 17。`claim_items` **一次领一件**(`CLAIM_CHUNK = 1`,A42 从 10 收下来),`FOR UPDATE SKIP LOCKED` + 条目租约(`lease_until`,迁移 0025)。两个 worker 跑同一批次时各领各的,重复投递因此变成安全操作 —— 这是任务 18 敢自动重投的前提。**领取批量参与租约不变量**(`ITEM_LEASE_SECONDS > CLAIM_CHUNK × 单件最长合法耗时`),所以它住在 `batch.py` 而不是服务层;要调大必须先做续租,否则导入期 assert 直接拦住 |
| 批次异常恢复 | ✅ 可用 | 任务 18。租约过期 → `reap_expired_leases` 放回 PENDING(上限 3 次,超过落 `WORKER_LOST`);有 PENDING 却很久没动的批次 → `redispatch_stalled_batches` 重投一次。beat 每 60 秒一拍,没 beat 时退回 `make requeue`。**没有新建 outbox 表** —— 条目本身就是意图记录,理由见 `docs/DECISIONS.md` §3.12 |
| 请求的事务边界 | ✅ 可用 | 任务 19 后半。`get_session()` **不再替所有请求提交**,事务归接口函数所有;写端点各自 commit,GET 一律不提交(唯一例外是 `download_batch_file` 的下载审计流水,只增不改)。批次执行跨付费调用的长事务是**署名例外** —— 事务级 advisory 锁必须活到回执可见那一刻,见 `docs/DECISIONS.md` §3.19。门禁在 `tests/pure/test_transaction_boundaries.py` 的「HTTP 边界」一节 |
| SHEIN 渠道适配 | 🚧 骨架 + 解锁闸,**一次真实请求都没发过** | 五个纯模块(签名 / 响应解码 / 回调 / 端点与操作能力 / 提交前校验)+ 本轮新增的取证台账、店铺身份单点派生、绑定证据、站点计划、图片准备闸。`readiness.py` 把散在六处的取证标志汇成一句话,当前档位 **BLOCKED**;图片上传与转换按**写操作**计入(平台上真的产生东西,而且没有对账接口);阻断条数**不写在这里**(第五节那条规矩),以 `readiness.blocking_reasons()` 现算的为准。S-01～S-31 的官方页面**一页都没有被我方逐页核对过**,台账在 `docs/vendor/shein-openapi/SOURCES.md`。注册表里有它但没有发送端 —— `transport_kind()` 返回 `NONE`,而那和「挂了个模拟器」在状态页上是两句不同的话 |
| 平台侧状态与驳回回流 | ⚠️ 手工录入 | 平台状态 + 驳回台账;定位把握分 audit / current_draft / unlocated 三档如实标注。**不接平台 API** |
| 逐件快审 | ✅ 可用 | `/workbench-review`,一屏一件、J/K/A/R 键盘流(R 退回,弹窗开着时其余键让位)。**依然没有批量批准,同理也没有批量退回**。a71 给退回弹窗加了「最近用过」快捷行(本机存 code,中文与下一步仍现问后端);**下面那个单选列表一动不动** —— 顺序由后端给,而按位置形成的肌肉记忆一旦被打乱,选错原因就是把商品送去错的下一步 |
| 后台设置页 | ✅ 可用 | 改完不重启即生效(TTL 覆盖层);密钥 Fernet 加密落库,明文不出后端。详见 `docs/SETTINGS.md` |
| 对象存储 | ✅ 可用 | `S3ObjectStorage` 兼容 S3 / MinIO,与本地存储共用路径推导,可 `mc mirror` 迁移 |
| 今日待办首页 | ✅ 可用 | `/today`(默认落点),七张待办卡片 + 其余动作码收成一行;每张卡带筛选参数跳转,计数全部来自后端。**a71 补上自动刷新**:此前这一页渲染完就再也不取数(全局关了 `refetchOnWindowFocus`,而它自己没有 `refetchInterval`)—— 一份挂了半天的待办清单,运营只会被骗一次。现在 60 秒一拍 + 手动刷新 + 「更新于」,时间戳取**两个接口里较旧的那个**(判定在 `utils/freshness.ts`,离线可验)。**「最早一件等了多久」仍然没有**,理由见「已知限制」那一节 |
| 快审退回 | ✅ 可用 | A10。九个受控原因 + 可选说明(选「其他」必填),退回后由后端按原因推出唯一下一步——「其他」与认不出的原因码按**被退回的对象**兜底,不按原因猜。图片集与文案各一个退回接口,原因清单走 `GET /workbench/reject-reasons`,中文与下一步只有后端一份。两个编辑页都有退回回执(原因/说明/时间),文案的 `REJECTED` 按 `reject_reason` 分「快审退回」与「校验失败」两种显示 |
| 未保存离开保护 | ✅ 可用 | A11。六个编辑面(属性/图片集/文案/草稿/设置/提示词)统一挂 `<UnsavedGuard>`;站内导航靠 `useBlocker`,刷新与关标签靠 `beforeunload` |
| 运营菜单收敛 | ✅ 可用 | 四组分区。**「只对管理员显示」这半句已过期**(a46-phase2 订正):侧栏「系统管理」整组(adminOnly: true)只对管理员显示;顶栏用户菜单的「系统设置」也只给管理员。operator 手输 /settings 打得开,页面上是一句 403 —— 路由不按角色裁剪,只有菜单裁。真正的权限边界在后端 require_admin。a46-phase6 落地;a46-phase2 曾经把始终可见的设计写进代码,phase6 撤掉了它。**a47 又收了一轮**:运营那三组按「同一件事只留一个入口」收敛,`/reviews`(并进审核中心)、`/products`、`/spus/new`、`/workbench-import`、`/workbench-spus` 五项撤出菜单,`/tasks` 移进「系统管理」组(对运营即消失)——前提是工作台详情的生成任务页签先能答出状态/轮次/失败原因,那一步同轮落地。**六条路由一条都没删**,手输仍可打开。项数不写在文档里,判据是 `App.tsx` 的 `OPERATOR_NAV_ITEM_COUNT` + `nav-and-url-filters.test.tsx` 那条一致性断言 |
| 出图按方案执行 | ✅ 可用 | **a47 唯一一处业务正确性修复。** 此前六个 `GenerationPlan` 参数里只有 `budget_cap` 生效:`provider` / `model_template_id` 取调用方传的,`scene` / `pose` / `angles_json` 在建任务与执行链路里一次都没被读过 —— 而验收链路按 `gp.required_angles` 检查,于是「配了方案 → 出图不按它 → 验收按它判不完整」,运营看到不完整而他没做错任何事。现在方案解析后由后端决定 provider / 模板 / 一轮张数,scene+pose+angles 进提示词,幂等键用解析后的值,冲突在审计里记「请求 X,方案 Y,按 Y 执行」。**没有给 `generation_tasks` 加列**,追溯靠已有的 `generation_plan_id` + `plan_fingerprint`。管理员可传 `override_plan` 绕过(非管理员 403,不是静默忽略),绕过后两个 plan 列留空、**但预算照查**。四条等式的真验证在 `tests/test_a47_plan_governs_db.py` —— **写了、一次都没跑过**(无 PostgreSQL) |
| 建任务入口分层 | ✅ 可用 | a47 §7。运营版只看:商品、模特、出图方案(只读解析结果,来自 `GET /generation-plans/effective`)、角度、张数、预计付费调用次数。Provider / 每轮候选数 / 最多轮次 / 基础 seed / 提示词 / `provider_params` / `override_plan` 收进「高级选项」,默认收起且仅 `isAdmin` 可见。方案指定了 Provider 或模特时那两项在界面上**只读** —— 后端已按方案执行,再摆一个可选下拉是在界面上撒谎。**a71 起前端四层门禁跑过**(`task-create-modal.test.tsx` 在内),原文的「(无 node_modules)」已过期;**真浏览器仍未实测** |
| 工作台按款视图 | ✅ 可用 | a47 §8。`[按 SKU] [按款]` 切换,**默认按 SKU,本轮不切默认**(切默认的判据是测 10~20 款后的使用数据)。状态写进 URL,跟这一页另外七个筛选同一规矩。按款显示 SPU 码、款名、SKU 数、颜色数、完成度(旗下最小值)、阻断数(求和)、**卡点分布**、可展开 SKU 明细。**没有款级 `next_action`,而且不该有** —— 不同 SKU 卡在不同步骤时任何单选都是编的,理由见 `DECISIONS.md` §3.72 第四节。口径提醒:`blocking_count` 数问题条数,`blocked_steps` 数 SKU 个数,两者不相等。**后端 `/workbench/spus` 只认搜索词与分页**,另外六个筛选在按款下不生效,界面上明说 |
| 审核中心 | ✅ 可用 | a47 §6。`/workbench-review` 改名,顶部四条计数入口(候选图审核 / 图片集待批准 / 文案待批准 / 属性冲突),点进去仍是各自现有页面。**只做入口,不动模型**:没有统一审核表、统一 DTO、统一 mutation,三套审核语义一个都没改。计数全部来自后端已有接口(`/reviews?status=PENDING` 的 total、`summary.by_next_action` 两条),**读不到的整块不显示** —— 一个「0」和一个「没查到」在界面上长得一样,而运营会按前者理解 |
| 仪表盘 | ✅ 可用 | `/dashboard`,需求第十四章点名的全部指标。A9 之后它不再是首页,归到「系统管理」组 |
| 生成链路冒烟 | ✅ 可用 | `make smoke`,健康检查 → 素材 → **已授权模特模板** → 异步任务 → 评分 → 成品图 → 导出 → 仪表盘,走 Mock 不花钱。**不覆盖审核之后的链路(6.5–6.10:人工审核/图片集/文案/草稿/发布/轮询/下架)** —— 那段由真库 pytest(`test_publish_flow_db.py`、`test_poll_and_delist_db.py` 等)做集成验证,交互路径按 `LOCAL_MANUAL_TEST.md` 手工走。此行以前写"端到端",A45 独立审查 C-11 指出那是高估,已更正;同轮起冒烟改走已授权模板,不再借道 MODEL_REFERENCE 绕行缝 |
| 评分器校准 | ✅ 可用 | `make calibrate`,模型分档 vs 人工结论的混淆矩阵;样本不足 20 条拒绝给结论 |
| 失败提示分层 | ✅ 可用 | A12。判定只有 `describeError` 一份,展示只有 `<ErrorNotice>` 一个出口;运营看一句话 + 请求编号(可复制),管理员多一个**默认收起**的技术详情。`docker compose` / 迁移命令 / 表名收进管理员分支。**主流程 10 处已换,a67 又还了 6 处(基线 24 -> 18),全站仍有存量未迁移** —— **有棘轮守着**(`tests/pure/test_error_notice_ratchet.py`,宽口径按文件冻结、双向锁):新文件不许长新债,迁走一处要把基线调下来。**这里原来写的「没有任何自动化防线,只许减不许增靠代码评审」是过期的** —— 那条棘轮就是那句话要的东西,而且它在a58 当轮真的拦下了两个新页面。存量条数不写在这里(第五节那条规矩),以 `BROAD_BASELINE` 为准 |

## 二、MVP 明确排除、也确实没有实现的

多租户、计费、账号体系、电商平台 API 对接。

> 曾经有两条测试(`test_out_of_scope_features_are_not_implemented_early`、
> `test_out_of_scope_tables_are_not_created_early`)守着这条边界。**那两条已在测试
> 套件清理时删除**,理由是阶段边界属于项目管理约束,留着会在日后正常增加认证、
> 多租户、订单时挡路。所以**这条边界现在没有自动化防线,靠代码评审**。
> 这份文档此前声称那两条守卫仍在,是错的。

## 三、已知限制

| 项 | 说明 |
| --- | --- |
| 迁移链本身已被机器验过,但仍未在任何库上执行 | **a65 起 `make audit-migration-chain` 在内存里重放整条链**(零依赖):每一步的目标表/列必须存在,最终 schema 与 ORM 逐表逐列对得上。a57 那条「迁移 0055 指向全仓不存在的表」现在是自动抓得到的。**但它只比列名集合** —— 类型 / 可空 / 默认值对不上看不出来(BE-307 那次正是类型),`op.execute` 的 raw DDL 也不解析。**升级前仍然要在测试库上真跑一遍升降级**,这条门禁替代不了它 |
| 迁移 0055 / 0056 从未在任何库上执行 | 文件已落、迁移链单一 head,但**没有一个库跑过它们**。0055 曾经指着一张全仓不存在的表(`evaluations`),a57 才发现并改成 `evaluation_attempts` ——而它躺了整整一轮没有任何东西报警,因为**一份从未被执行过的迁移,它的表名没有任何东西在校验**。升级前先在测试库上走一遍 |
| ~~`make fe-check` 至今未兑现~~ **已于 a71 首次真的跑完,原文过期** | `npm ci` 在交付机上装得上(387 包),于是 typecheck / ESLint / Vitest / build 四层**第一次全部执行**:tsc 0 错、ESLint `--max-warnings=0` 干净、**Vitest 30 文件 204 条全绿**、`vite build` 出得了产物。首跑当场抓到一条长期红着的用例(`ops-log-page` 用 `?follow=false` 造 URL,而 `flagParam` 只认 `1`/`0`,于是那条断言从写下起就没验到过东西,另两条靠默认值碰巧变绿)—— 这正是「没跑过的绿不算绿」。**仍然没有的是 Playwright**(任务 24 未开工)与**真浏览器里的人工点击**:jsdom 不是浏览器,布局、字体、真实网络与打印一条都验不到。**别把 `fe-test-pure` 的绿读成 `fe-check` 的绿** —— 前者的射程只有 `.ts` 里的纯函数,剥类型不做 JSX 变换,`.tsx` 一行都跑不了;这条口径不因为本轮跑通了 fe-check 而放宽,离线环境里能跑的仍然只有那一层 |
| 调色板 token 写错在运行时完全静默 | `brandVars` 是 `fromEntries` 出来的 Record,取不存在的键得到 `undefined`,而 CSS 收到 undefined 只是不上色。TS 类型上会拦,但这台机器跑不了 typecheck。**a61 起离线体检扫这一类**(第四格),判据取 `theme.ts` 的 `lightTokens` |
| 费用只按主币种汇总,不折算 | 「本月已花费」与预算进度条只统计 `SPEND_CURRENCY` 那一种,**这是刻意的**(汇率是估的,这一页用来发现异常不是对账)。a32 起有别的币种时页面会明说「另有 X 没有计入」并列出各币种金额(后端 `by_currency`,金额由 `format_money` 生成);a34 起 `by_currency` 还带 `calls` / `unpriced_calls`,于是**只有未配价调用的币种不再从提示里消失** —— 它金额是 0,而那个 0 的意思是"未知"不是"没花"。仍**没有**的是每日曲线本身 —— `/spend` 页至今不画曲线,`daily[]` 的数据没有消费方 |
| ~~图片集变体绑定无入口~~ **已关闭,原文整条过期(2026-08-09 评审订正)** | 原文写着「前端没有设置变体的地方,`variant_id` 恒为 null,所以变体覆盖规则至今不触发,**BLOCK-02 保持「处理中」**」——**三句话全部与代码事实相反**,而且方向是最危险的那种:把已经做完的说成没做,下一个人会去补一个已经存在的门禁,或误判多色 SPU 今天可以带缺图批准。事实是:①入口在 `ImageSetTab.setVariant()`(A-26,行级颜色选择器 + 「再绑一个颜色」);②`image_set_rules.coverage()` 已删掉「有通用图就算覆盖」那条放行(§6.5:通用图只能进附图位、不得跨色回退);③`image_set_service.validate()` 与**批准路径**都传 `variant_ids=required_variant_ids(...)` + `required_angles`,有问题时抛 **409 阻断批准**。原文最后那句「今天所有图都是通用图,改成硬阻断等于让每个多色 SPU 立刻无法批准」是当时不做的理由,它随 A-26 的入口一起失效 |
| SHEIN 的五个纯模块从未与真实平台往返过 | 签名算法没有被官方示例向量验证过(`signing.OFFICIAL_EXAMPLE_ARCHIVED` 为 False),响应形状、回调验签算式、回调媒体类型、官方特殊字符清单全部未取证。**这不是「写好了没测」,是「按一份读不到的文档写的骨架」** —— `readiness.py` 因此拒绝解锁任何真实写操作,`build_request()` 抛 `ChannelNotReady` 并逐条说缺什么。第一步不是写代码,是把 S-01～S-31 逐页重开并按 `SOURCES.md` 的形状存档 |
| 投递证据的落库那一半欠着 | 评审 P-06 的四句里落了三句半:三档 typed 证据、单点翻译、客户端自动重试关闭都有了,**「service 在结果事务 append-only 落库」没有**。重试会覆盖同一行 attempt 的 `safe_response_snapshot`,所以要一张 append-only 的表 + 一条迁移,而迁移升降级要真库才验得了。登记成带还款日的欠账守卫(阶段 5),在 `tests/pure/test_delivery_evidence.py` 末尾 |
| 待办只有件数,没有「等了多久」 | 首页与各队列都只报件数,于是「待审文案 23」里有没有压了三天的,界面上问不出来。**不做的理由是没有诚实的数据源**:`products.updated_at` 会被任何一次编辑刷新,拿它当"进入这一步的时刻"算出来的天数是错的 —— 那正是本仓库拒绝过的那类「算不准的数」(见提示词那一行的「近 7 天调用次数」)。要做得先给流程步骤补一个**状态进入时刻**,由 `flow` 在状态迁移时写,再由 `summarize` 一并输出每个动作码的最早一件。属于数据模型改动,不在前端糊 |
| 退回人不回显 | 退回回执显示原因、补充说明与时间,不显示退回人 —— 系统没有账号体系,`rejected_by` 取的是 `X-Actor` 头(缺省 `system`),显示出来是一个假的问责对象。该值仍落库并进审计,查得到 |
| 属性值只能改标量与列表 | a31 按后端给的 `value_type` 渲染控件,列表字段不再被回传成字符串(BLOCK-11)。但值是**结构化对象**时仍然只读 —— 当前注册表里没有这类字段,真出现时界面会如实说「编辑器装不下」,而不是 `JSON.stringify` 之后当字符串存回去 |
| A12 只覆盖主流程 | 换掉的是工作台主链路的 10 处。快审页(4 处)、审核队列、商品详情、任务详情、设置页等 17 处仍是 `Alert + readError`:文案对,但技术层没有落点,管理员在那些页面还得开控制台。**这 17 处没有被冻结进任何契约测试** —— 本文此前两处都声称有,是错的。`describeError` / `readError` / `<ErrorNotice>` 的**行为**在 `frontend/tests/` 里是有测试的(`client.test.ts`、`error-and-cold-start.test.tsx`),但**没有任何一条去数调用点**,所以加第 18 处 `Alert + readError` 不会有东西报错。要变成棘轮,得加一条统计 `readError(` 的 `<Alert>` 用法数与 `useWriteError(` 调用点数、只许减不许增的测试。另有 5 处 `message.error(readError(e))` 的 toast 按设计不迁移(全站 toast 调用点共 16 个,本文原写「100 多处」是错的)—— toast 装不下折叠面板 |
| **三类外部集成的真端点验证:全部「从未」** | 出图 Provider(fashn / fal / comfyui)、视觉抽取器、视觉评分器 —— 请求构造有测试、响应解析有测试,而**真实往返零次**。这正是 A42 那次事故的形状:两侧的测试全绿,没有一条跨过中间那道缝。真实 API 与文档的偏差(字段大小写、错误体形状、限流头、分页语义)只在真实往返里暴露。逐项判据在 `docs/provider-endpoint-ledger.json`(由 `make provider-smoke` 写入,不要手工编辑),守卫在 `tests/pure/test_provider_endpoint_ledger.py`。台账的限度要写清:它拦得住「忘了记」,拦不住「编一个日期」—— 证据在那次冒烟的输出里,不在这个 JSON 里 |
| 生产镜像与迁移编排已改,但**构建一次都没跑过** | 后端 Dockerfile 改成两阶段、非 editable 安装、非 root、带 HEALTHCHECK,并在运行阶段做两遍 import 自检;生产迁移从 backend 启动命令摘成一次性 `migrate` service。改动本身有 YAML 与 AST 层的核对,而 `docker build` / 起容器这一层在打包环境里没有 Docker,**一次都没执行**。跑法:`make images-smoke`;真机上按 `make p0-gate` 的 P0-3 走 |
| 无账号体系 | 审计操作者取 `X-Actor` 头,缺省 `system`;改配置另有 `ADMIN_TOKEN` 一道运维口令 |
| `POST /api/reviews/{id}/approve|reject|regenerate` 曾无条件 500 | `_basic_review_out` 签名被改成批量形态(`item, *, products, task_statuses`),列表页跟着改了,三个写接口没改,它们仍按老签名调用。A42 修:抽 `_single_review_out(session, item)` 转发,这样下一次签名变化时编译期报错而不是运行期 500。这是第二条真实生产缺陷(第一条是环境判定那一面) |
| 环境真实性横幅曾错报三档 | A37～A41 期间 `/api/environment` 把 Mock 出图与 Mock 评分报成 REAL、把正常工作的渠道 Simulator 报成 UNAVAILABLE。A42 已修。新接一个真后端时要在实现类上写 `is_simulator = False`,忘了写只会多喊一次警告,不会反过来说假的是真的 |
| ~~属性识别只有 Mock~~ **A45-batch14 接上了 vision,但一次都没连过真模型** | 任务 7 已落码:`extractors/vision.py`,双 API 形状,复用 `llm/` 的传输层与图片层,`EXTRACTOR_BACKEND=vision` 即启用。**但它从未对着真实端点发过一次请求** —— 守卫全是纯逻辑与 AST,验的是「请求体长什么样、响应怎么解析」,验不到「厂商真的会这么回答」。第一次接真端点时要盯三件事:结构化输出降级(strict json_schema 被拒 → 换 json_object → 换 prompt_only)、`finish_reason=length` 有没有如实报成「输出被截断」而不是「JSON 不合法」、以及**账单上的调用数与 `provider_usage_records` 里 `operation='attribute_extract'` 的行数对不对得上**。`describe_extractors()` 从本批起如实上报(`configured` 问实现自己),所以配错时状态条会说「没配好」而不是「已就绪」 |
| ~~**识别输入白名单还没收(§5.1 未落地)**~~ **已解决,原文已过期** |判定在 A45-batch14-7 就接上了(AI 图进不来),取数入口在 A45-batch14-19 收口(`media.evidence_assets_for()`)。原文说「今天仍走 `usable_assets()`、AI 图会进识别输入并产生真实付费调用」——**那句话从 14-7 起就不成立**,留着它会让人去查一个不存在的问题。真库验证 `tests/test_a45_batch14_19_evidence_query_db.py` 已写、未跑 |
| **AI 图伪装成样品:两条路堵了,第三条还开着** | A45-batch14-11 接了 §11 / AC-22 的溯源冲突拦截:同 SPU 同 sha256 命中带溯源的行时,新建那一路落隔离、去重命中那一路不再补角色。**但拦截的判据是「本系统生成过这张图」,不是「这张图是 AI 画的」** —— 从别处拿来的 AI 图(外部工具生成、供应商推来的合成图)没有任何溯源痕迹,照样成为 `PRODUCT_EVIDENCE`。堵那一条要靠图像侧的判别,不在本期范围。另外:冲突**只记 `logger.warning`,没有落审计**(`ingest()` 手里没有 actor,记谁头上是一个业务决定);隔离行经 `release()` 人工放行后会**重新成为证据**(它的 `source` 是 `MANUAL_UPLOAD`,而 `evidence_class` 由 source 派生)—— 那是设计(人工放行的语义就是「我确认这确实是实物样品照」),但**没有人在真界面上走过这一步** |
| ~~**"不指定模特"绕行缝(C-10)**~~ **已于 2026-08-11 关闭,但是以拒绝的方式关的** | `generation_service._assert_assets_are_usable()` 的 `MODEL_REFERENCE` 分支不再 return,改为 `ValidationError`;`tasks/generation_tasks._build_request` 里那条"拿不到模板图就退回自由上传模特图"的兜底也删了(它是同一条缝的第二道门,而且发生在四道检查都过了之后)。**关的方式是 fail-closed,不是接通闭环**:自由素材今天解析不到授权主体 —— `ProductAsset` 上没有指向 ModelTemplate 或授权记录的列,`MediaAsset.consent_id` 列在但**全仓没有写入点**,`MediaConsent` 又没有受众字段(连 §10.5 都判不了)。运营的出路是把那张模特图登记成 ModelTemplate 再选它,那是**唯一**能执行四道检查的路径。要重开自由上传这条路,得先补上上面三样中的任意一条可用链路 |
| 「有证据但不采信」没有界面 | A45-batch14-11 的判定已经产出 `ATTR_EVIDENCE_ONLY` 阻断与 `FILL_ATTRIBUTES` 动作码,前端补了动作码的三张镜像表 + 首页「其余待办」。**但属性页上没有为这条动线做任何事** —— 没有「这批字段为什么不采信」的分组展示,也没有一键人工填写入口。运营点「人工填写属性」会落在属性页,然后自己逐个找。真实抽取器接上、校准为空时这条动线会是主路径,那时要补 |
| ~~识别的付费调用与 HTTP 请求事务同生死~~ **已由 0054 关闭** | 两个 HTTP 入口现在只 `queue_extraction()`，先提交 QUEUED run，再投递 Celery；真实模型调用与 `record_usage` 在 worker 的独立会话中执行，后续 HTTP 回滚不再撤销已经发生的调用流水。迁移 `0054` 同时落输入快照、逐图成绩、取消与恢复列，relay/reaper 补漏投与卡死。Windows 真基础设施记录见本文顶部与 `AC-VERIFICATION.md` §11。**这只关闭“HTTP 事务同生死”这笔账**；真实供应商的计费口径仍未连端点验证，见上方 vision 限制，不合并宣称 |
| **运行日志控制台没有连过真 Redis** | a53 落地了环形缓冲(`ops:log_ring`)、载荷旁挂库(`ops:llm:{id}`)与三个读接口,但**写入、读取、TTL 到期三条都只有纯测试用假客户端覆盖过**。真 Redis 上还没验的是:LTRIM 之后 `held` 与 `cap` 对不对得上、TTL 到期后 `/api/ops/llm/{id}` 是不是如实回 404(而不是回一个半截记录)、以及 Celery worker 与 API 两个进程写进同一个键时 `seq` 去重在跟随模式下够不够用。**日志写失败是静默吞掉的**,所以这三样出问题时不会有任何报错 —— 唯一的信号是 `/api/ops/logs/meta` 里的 `dropped_since_boot` |
| 运行日志页浏览器未实测 | 前端门禁(typecheck / lint / Vitest 9 条 / build)全绿,但**没有人在浏览器里点开过这一页**。Playwright 用例属于任务 24,尚未开工 |
| 三个 ops 端点没有被 TestClient 打过 | `make test-nodb` 与 `pytest` 需要 fastapi / sqlalchemy,交付这一轮的机器上没装。判定层(域推导、折叠、404 分两种措辞)有纯测试,**接口层的取数与闸没有** |
| 配置变更无值历史 | 审计只记谁改了哪些键,不记改前改后的值(记了等于把明文密钥换个地方存) |
| worker 配置最终一致 | 改完配置 worker 最迟 `SETTINGS_CACHE_TTL_SECONDS` 秒跟上,期间两边可能不同 |
| FALLBACK 路由 | 显式抛错说明尚未实现,不假装支持 |
| ComfyUI / fal.ai | 仍是骨架,请求映射待官方文档确认 |
| FASHN 不支持取消 | 官方文档没有取消端点;轮询期间 Celery worker 阻塞,上量后需改 webhook 驱动 |
| Provider 切换只有机制 | C/D 档规则会要求换 Provider,但今天只有 Mock 已配置,`next_configured_provider()` 返回 None 并把原因写进 attempt |
| 成品图无批量重算入口 | 网站改尺寸时需要整批重算,目前只在「再次通过」时触发 |
| Celery 单队列 | 未做优先级与并发调优 |
| 租约回收对「跑到一半才崩」仍会重复付费 | 回执表(`batch_action_receipts`)挡的是「已经跑完的不重跑」,它在调用**之后**才写。worker 恰好死在付费调用与写回执之间时,重排那一次会真的再花一笔。唯一的缓解是租约足够长(`ITEM_LEASE_SECONDS` > `CLAIM_CHUNK` × 单件最长合法耗时,带 assert 守着)与 `MAX_ITEM_ATTEMPTS = 3` 的上限。**这一段没有真库测试** —— 见下一行 |
| 发布投递的状态互踩已经关掉(A45-batch17-2),重叠投递窗口仍在 | `publish_service._save()` 原来无条件写三张表:租约过期被重领之后,迟到返回的那次调用会把 DONE / SUCCEEDED / LISTED 覆写成 DEAD / UNKNOWN / SUBMIT_RESULT_UNKNOWN —— 商品在平台上好好的,界面说结果未知需人工。**本行原来根本不存在**:批次侧的同型缺口 A43 已修并记账,发布侧当时既没修也没记。现在落库走令牌(迁移 0047,`lease_token`),执行权不在自己手上时结果只进审计。仍**没有**关掉的是重叠投递本身:调用耗时越过 `LEASE_SECONDS = 180` 时同一份报文仍可能被发两遍,靠幂等键 + 唯一索引 + 平台 409 三道防线保证不多出商品(`DECISIONS.md` §3.19),而三道防线里第三道要依赖平台真的实现了幂等。今天不可达 —— 唯一的 transport 是进程内瞬时返回的 Simulator;第一个真实 HTTP transport 接入且客户端超时未钳制在 `LEASE_SECONDS` 以下时变为可达。真库双 session 用例 `tests/test_publish_lease_concurrency_db.py`(7 条)**已写、未跑** |
| 租约到期窗口仍在,但**状态互踩已经关掉** | `reap_expired_leases` 只看 `lease_until` 过期,**不问 owner 还在不在**(系统里没有活体名单,`lease_owner` 明写只用于排查)—— 这一半仍然成立,而且是刻意的。**另一半已经不成立了**:本行原来还写着「`_apply_outcome` 不校验本 worker 是否仍持有租约就覆写 status」,那是 A42 的事实;A43 / BLOCK-02 之后落库走 `apply_outcome(owner=, token=)`,四个条件进 WHERE,令牌被回收吊销之后旧 worker 的更新影响 0 行、结果只进审计。不带校验的 `_apply_outcome` 现在是**零调用点的废弃函数**(留着是为了让旧调用点立刻可见)。所以准确的说法是:**重叠执行仍可能发生(钱可能真的花两次),状态互踩不会**;前者靠租约时长与续租控制,后者靠令牌。双 session 场景在 `tests/test_batch_lease_concurrency_db.py`(10 条,含「租约过期但 worker 还活着」那一条),A42 跑绿过,batch12-6 之后**未重跑** |
| ~~租约与回收的行为**未在真库验证**~~ **A42 已验** | `tests/test_batch_lease_concurrency_db.py`,8 条双 session 用例,在真 PostgreSQL 16 上跑绿:两个 worker 互不相交(`SKIP LOCKED`)、活租约领不走、过期租约可接管、`lease_until IS NULL` 的存量残骸可领、回收放回队列、超上限落 `WORKER_LOST`、一次只领一件。变异验证过:删掉 `skip_locked` 会在 3 秒锁超时后失败并点名原因(**不是挂起** —— 用例给 B 装了 `lock_timeout`,否则 CI 只会报「作业超时」);领取漏掉 `IS NULL` 分支会让存量残骸那条变红 |
| 提交这件事集成测试验不了 | `tests/conftest.py` 的 `client` 夹具把 `db_session` 覆盖成一个**不提交**的 session(每个用例一个事务,结束回滚)。同一个 session 里读得到未提交的写,所以「真的提交了」与「只是 flush 了」在 API 测试里完全等价 —— 一个写端点漏掉 commit,现有测试一条都不会红。这不是夹具写错了(用例之间要隔离只能这么写),但代价是提交落在测试射程之外。**唯一防线是 `tests/pure/test_transaction_boundaries.py`「HTTP 边界」那五条**,它们做过变异验证。摘掉请求级自动 commit 这次改动**没有在真库上跑过** |
| 任务 19 两半都没有运行时证据 | N+1 那半(a38)守的是源码形状,`REVIEW.md` B.8 的「实测 SQL 计数」仍欠;事务边界这半(a42)守的是「谁写了 commit」,不是「事务真的在那一刻结束」。两条都拦得住最容易发生的退化,都**不能替代**真库验证 |
| 识别与提交的**次数**已进台账,但供应商侧口径未验 | A45-batch18 / P1-2 + P2-2:传输层重试与 FASHN 分批提交原来都按「一次业务调用 = 1 个计费单位」记,于是重试过的少记、preflight 失败的多记。现在识别按**网络往返次数**记(`llm/transport` 的 `on_attempt` → `extractors/call_accounting`),FASHN 失败按**已发出的 POST 数**记(`providers/call_accounting`),次数单独落 `provider_usage_records.provider_attempts`(迁移 `0048`)。**仍未验证的是供应商那一侧**:超时后重发对方到底计不计费、`x-fashn-credits-used` 是本次还是累计、失败时带不带那个头 —— 三个问题都要连真端点才答得出,今天全部按「宁可多记」处理并如实标 `units_source=inferred` |
| 供应商幂等键默认关着 | `EXTRACTOR_MODEL_SEND_IDEMPOTENCY_KEY=false`。打开后每张图带一把 `<run 幂等键>:<素材 id>` 的 `Idempotency-Key`,让供应商自己把传输层重发认成同一笔。默认关是因为**不认识这个头的严格网关会直接 400**,而那时的表现是「识别整条不通」,没有人会往幂等这个方向查。开之前先拿一次真实调用确认端点接受它 —— 这件事没做,所以重试仍可能让同一张图被受理两次(台账会如实记 2) |
| 清理清单现在含**结果未知且无 ID** 的行 | A45-batch18 / P1-3:原来查询强制 `external_spu_id IS NOT NULL`,于是一次 CREATE 到达平台、响应在返回 ID 前超时的行会从 inventory / plan / verify **同时**消失,报告说 `clean=true` 而平台上留着孤儿商品。现在这类行留在清单上,标 `needs_reconcile`、不可自动下架、带 `locator`(店铺 / SPU / 批次 / 时间窗),并让 `clean` 变成 False(CLI 退出码 1)。**没有验证过的是「按 locator 真能在平台后台找到它」** —— 那要真实渠道沙箱 |
| 破坏性清理禁止 channel-only | A45-batch18 / P1-4:`delist --apply` 现在必须带 `--tag` 或 `--shop`;只给 `--channel` 会被拒。预览不受限。原来两者共用一道 `any(...)` 闸,一次参数遗漏就是该渠道下全部店铺、全部批次的批量下架 |
| 批次租约的预算算错了三批,现已按识别配置推导 | A45-batch18 / P2-1:`LONGEST_LEGAL_ITEM_SECONDS` 原来是 `90×3×4`,三项全部取自 **VISION_MODEL_\*(评分器)**,而批次里跑的 EXTRACT 读的是 **EXTRACTOR_MODEL_\***。真实上限 `60×3×12 = 2160` 秒 > 当时的租约 1800 秒 —— 那条「租约必须长于单件最长合法耗时」的不变量**在默认配置下根本不成立**,而模块级 `if` 因为被除数取错一次都没红过。现在 `ITEM_LEASE_SECONDS` = 3600、`BATCH_PROGRESS_STALL_SECONDS` = 2700,并加了一条读**实际部署配置**的启动检查(`lease_budget_shortfall()`,设置页改大超时/图片上限时会报 error 日志) |
| 付费调用前的续租现在是**已提交**的事实 | A45-batch18 / P2-1:A43 加的 `renew_lease()` 顺序一直是对的(在 `_execute()` 之前),但那条 UPDATE 留在外层事务里,而外层事务要等结果保存完才提交 —— 整个付费调用期间回收器读到的仍是**领取时**那个 `lease_until`,续租等于没续。源码扫描看不见这个区别(两种写法顺序完全一样)。现在续租后立即提交,并补了双会话真库用例 `tests/test_a45_batch18_lease_visibility_db.py`(3 条,含一条反向用例证明不提交时确实读不到)。**该用例已写、未跑** |
| 门禁扫工作树,交付的是版本库 | A45-batch18 / P1-1:外部评审发现 `0046` / `0047` 两条迁移与对应测试**未被 Git 跟踪** —— 本机 `alembic heads` 说 0047、`verify_delivery` 16/16、纯测试全绿,而 clean checkout 的最后一条迁移是 `0045`。新增门禁 `check_every_migration_and_db_test_is_tracked_by_git()`(问 `git ls-files`,不是 Git 工作树时直接失败)。**它写完当场就红了**,红在本批自己新增的 `0048` 和那个新 DB 测试上 |
| 向导浏览器**部分**实测(2026-08-09 评审补) | A45-batch29 时这一行写的是「**没有人在浏览器里从头走过一遍**」。现在 AC-16 那一条走过了:`frontend/tests/e2e/wizard-refresh.spec.ts` 3 条真浏览器用例(刷新后停在 URL 上那一步、停在那个颜色、不带 `step` 时落后端算出的当前步),`page.reload()` 真的重建 JS 上下文 —— 组件卸载重挂载替代不了它。做过反证:把 `WizardPage` 的 `values.step ?? wizard.current_step` 改成恒用后端值,第一条当场变红。**仍然没有走过的**:七步从头到尾的业务动线(任务 24 的主体)、antd 布局在真实分辨率下读不读得下去、以及「断线重连不丢进行中任务」(要真 worker)。发布页(20-A)仍是一条浏览器用例都没有 |
| AC-05 的闸只在 HTTP 边界 | A45-batch29:`_ensure_action_allowed` 挂在 `generate_copy` / `build_draft` / `export_draft` 三个**接口**上,不在 service 层。**这是刻意的**:批次执行直接调 service 函数,它有自己的跳过判定与回执表,在 service 层加同一道闸会让批次在"这一件还没就绪"时抛异常而不是记一条跳过。代价是:任何绕过 HTTP 直接调 service 的新代码路径不受它管,而今天没有守卫能发现新增了这样一条路径 |
| 影响提示是**对象级**的,没有字段级 | A45-batch30:AC-17 的原话是"对象 / 字段 / 需要执行的动作"。`stale_matrix` 是 (变更源 × 对象) 的矩阵,给得出对象与动作,**给不出"哪几个字段会失效"** —— 那取决于运营具体改的是哪一个字段,而提示发生在他改之前、还没选定字段的时候。字段级今天只有**事后**那一份:草稿变 STALE 之后 `GET /draft/stale-reason`(BE-205)会点名字段。要做事前字段级,得让接口接受"我要改哪个字段"并按注册表推依赖,那是一条新口径,本批不猜 |
| 费用预估的用量是估的,而且方向恒定偏小 | A45-batch30:按"每个缺角度出 1 张候选图、每张评一次分"算,不含重试、评分退回与人工重做。文案生成今天不写付费流水(生成器未接付费后端),Mock 抽取器/评分器不计费 —— 两者都不计入,并在 `notes` 里说出来。未配价的动作金额记 `None` 且**不进总额**,界面显示「未配价」而不是 ¥0。所以那个总数**永远是下限**,`is_complete=false` 时界面会明说它不全 |
| 本地存储 | 后端 `/files` 直接托管,仅适合开发;生产改 `STORAGE_BACKEND=s3` |
| 上传走内存 | 20 MB 上限下可接受;大文件需改流式落盘 |
| 平台侧全手工 | 平台状态与驳回原因都靠人录入,不接平台 API |
| ~~API 驳回关不掉~~ **新数据已能自证关闭,旧数据仍需人工(2026-08-09 评审订正)** | 原文写「`resolve_gate()` 认的证据是驳回之后有一次新的**导出**,而 API 自动上架的商品根本不走导出,于是关不掉,**由任务 20-B 补齐**」——**20-B 已经落码**。`platform_service._publish_attempt_entries()` 把「驳回之后有一次**成功的**提交尝试」当作等价证据喂进 `resolve_gates()` 与解决路径;关联走 `ChannelListing.draft_id`,指纹那半边仍落在当前草稿指纹上,所以**没改草稿的重复提交过不了闸**;只认 `SUCCEEDED`(PENDING/IN_FLIGHT 没结果、UNKNOWN 不知道平台收没收到、FAILED/ABORTED 没提交成功)。**仍然欠两件**:①**真库 seam 没有** —— 从一行真实 `PublishAttempt` 穿过 `platform_service` 到驳回关闭,没有一条真库用例走完过;②`draft_id IS NULL` 的历史驳回关联不上尝试,仍然只能在工作台手工标记。发布接口对后者照旧如实报 `REJECTION_CANNOT_AUTO_CLOSE` |
| 发布页没跑过浏览器 | 界面已补齐(B-02 关闭,`PublishPage.tsx` + 侧栏「发布上架」+ 导出页跳转)。**「没有 tsc、没有 vitest」这半句已过期**(A45-batch24 订正):`npm run typecheck` 与 `node tools/syntax-check.mjs` 从阶段 4 起就覆盖全部 89 个前端文件,`PublishPage.tsx` 在内。仍然成立的是最后半句 —— **没有一条用例点开过它**:Vitest 的 78 条覆盖 saveBlob、路由拦截、草稿页图片预览等,不含发布页;Playwright 未开工。门禁验到"入口存在且不自建第二份判定",验不到"点下去这一步真的发生了" |

| ~~集成测试有 15 条真实失败~~ **A42 已全部修** | 第一次把 `requires_db` 那批真的跑起来:1652 条 0 跳过,**最初 15 条失败**。根因是三件事:(1)产品缺陷:`POST /api/reviews/{id}/approve|reject|regenerate` 三个接口无条件 500 —— `_basic_review_out` 签名被改而三处调用没改,**这是生产 bug,非测试杂项**;(2)既有夹具缺陷:`celery_eager` 把 `commit` 换成 `flush`,导致应用代码的 `rollback()` 回到用例开头,同一任务被派发两次,第二次抢不到、rollback 全清 —— 改用 `savepoint` 模式;(3)过期断言:人工通过后任务继续走完出图,不停在中间态。修后:1652 全绿,含批次并发 8 条、生成链路 21 条、审核链路 17 条,全部真库双 session/Celery eager 跑过 |
| ~~菜单按角色收敛不是权限~~ **菜单不再按角色收敛(a46-phase2 订正)** | 原文写「A8 的「系统管理」组只对管理员**显示**」。那次翻转之后 `NAV` 里已经没有 `adminOnly`,`nav-and-url-filters.test.tsx` 反过来钉着「不按账号隐藏」——而纯层还留着两条断言 `adminOnly` 必须存在的守卫,两边**互为反面**,红的那条一直没人看。本轮删掉纯层那两条(依据 `frontend/CLAUDE.md` 第三条:菜单可见性不是跨语言契约),判定只留 Vitest 一份。真正的边界仍然是后端 `require_admin` |
| ~~角色判定可被本地伪造~~ **a46-phase6 关闭** | 后端 `/auth/whoami` 答不上时降级看"本浏览器填了管理口令没"。往 localStorage 塞个假口令就能让菜单长出管理项,但那一栏的每个请求仍会被后端挡回 403。**浏览器登录模式下这条降级不再生效**:`is_admin` 由 Session 决定,a46-phase6 删掉了 localStorage 口令链 —— isAdmin 现在只认 probe.data?.is_admin === true,后端不答就是 false。行为已关闭;条目保留供历史追溯 |
| 浏览器登录与 Token UI 退役 | ✅ 可用(**前端用例未在浏览器里跑过**) | a46-phase2。`/login` 页 + 401 跳登录并带 `?next=` + 顶栏退出登录;`apiClient` 开 `withCredentials`,身份探测不再要求本地口令。部署认哪种凭据由 `/health` 的 `auth_mode` 说(`session` / `token`),前端三处分岔都读它。**新增的 Vitest、Playwright 与 `auth_mode` 那两条 pytest 一次都没执行过** —— 打包机器没有网络,装不上 `node_modules` 与 Playwright 浏览器。**条数不写在这里**(这一栏原先写「12 条 Vitest」,那是 phase2 自审时当场就改掉的第一版数,而订正只落在 HANDOVER,没有回流到这里 —— 正是第五节那条规矩要防的形状):逐文件清单与复跑命令见 `HANDOVER.md` 的 a46-phase2 与 a46-phase6 两节 |
| 刷新弹窗文案改不了 | `beforeunload` 只能触发浏览器自己的原生弹窗,文案由浏览器决定。站内导航那条路径是自绘 Modal,措辞可控 |
| 前端已改为数据路由 | `main.tsx` 用 `RouterProvider` + `createBrowserRouter`,**不是**为了用 loader/action,只因为 `useBlocker` 只在数据路由下可用。路由仍用 JSX 声明 |
| ~~首页筛选不进 URL~~ **A45-batch14-17 已修(GAP-033 关闭)** | 筛选状态改为住在 URL 里(`useUrlFilters`,URL 是唯一真相),工作台列表 / 生成任务 / 逐件快审 / 操作审计四页搬完,`useUrlSeed` 已删除。PRD §8.2 的四条要求现在全满足:URL 可复制、刷新保留、后退保留、点进来带条件。**代价与新风险各一条**:改筛选走 push,连点五个筛选要按五次后退(那是筛选类界面的正常行为,也是运营唯一能撤销一次误点的手段);后退/前进会改筛选却不经过任何 setter,所以清空勾选改挂在 `filters.signature` 上 —— 不这么做的话 BLOCK-09(越界批量)会从那扇门原样回来。**这一批的行为覆盖(Vitest 13 条)一次都没跑过**,被执行过的只有 12 条读源码的 Python 守卫 |
| ~~还有两页的筛选活不过一次刷新~~ **a50/a51 已搬(2026-08-12)** | `ProductListPage` / `ReviewQueuePage` 的筛选与排序已接进 `useUrlFilters`,GAP-033 最后两页收口。**这一行原来写的是「本批没有动它们一个字」** —— 那句话在 a50/a51 之后就不成立了,而它一直留到 a67 被外部审计点名(ISSUE-004)。行为覆盖仍只有 `nav-and-url-filters.test.tsx`,**浏览器里没跑过** |
| 「运行中任务」卡片不带筛选 | 它的计数是一个状态集合(`IN_FLIGHT_STATES`),而任务列表接口只收单个 status。点进去是全量列表,卡片上已写明 |
| 只读接口的 N+1 只在两处修过 | A41 把 `api/workbench.py` 的 `list_products` / `product_flow` 的兜底 `rollback` 挪到返回前(约束 14)。**同型风险没有全仓扫过** —— 任何「rollback / commit 之后再读 ORM 对象」的路径都会退化成逐行 SELECT,而 `test_workbench_query_budget.py` 结构上看不见它(它数的是源码里的 `session.*` 调用点,而这类查询是属性访问隐式触发的)。真实条数要带库开 `echo=True` 数一遍,这件事还没做 |
| 出参时间戳只统一了两组 | A41 的 `core/clock.iso_utc()` 收了工作台与批次两组出参(永远带 `+00:00`)。`platform_service`、发布接口、任务/审核那几组仍是裸 `.isoformat()`,于是「刚写完就回读」与「刷新重查」还会给出两种形状(`expire_on_commit=False` 的后果)。前端两种都认,所以它不会表现出来 —— 会看见的是导出文件和第三方消费者 |
| 本轮前端改动没有 Vitest 覆盖 | A41 修的两处前端缺陷(`saveBlob` 的 revoke 时序、列密度偏好被 matchMedia 推翻)都没补用例:改动是在离线环境做的,装不了 node_modules,写一条跑不了的用例会让「0 skip」那道门禁变成红的。有依赖的机器上应当补 —— 尤其列密度那条,它的失败路径是「手动设置 + 拖窗口」,不写用例下次还会漏 |
| 批量导出多出的那一列没有跟真实平台对过 | A41 给批量 SKU 表加了「所属 SPU」列（约束 17），列头复用 SPU 表里 `source: product.spu` 那个字段的列头。**目前只有 Simulator，没有真实渠道 transport**，所以「平台的导入器认不认这一列」没有验证过。接第一个真实渠道时要连同 `channels/generic/spec/swimwear.yaml` 一起复核：如果平台要求 SKU 行自带父 SPU，正解是把 `spu_code` 加进 `row_fields`（那会 bump `spec_version`、让全部草稿过期），而不是继续靠导出器补列 |
| 轮询守卫多一条 join 查询 | 约束 18 的 `_delivery_pending` 每保存一次轮询结果就查一次 `publish_outbox` join `publish_attempts`。走的是 `ix_publish_attempts_listing_started`，而同一轮里每行本来就要打一次真实外部调用，所以代价可以忽略——但这是**推理**，没有实测。上量后如果轮询扫描变慢，这里是第一个要看的地方 |

### 从过程文档收编的遗留项

原先散在 `FE-OPS-REVIEW-2.md` / `FE-OPS-CHANGELOG.md` 里,那两份已删除,
结论收在这里。

| 项 | 状态 |
| --- | --- |
| 平板适配 | 待业务决策。先问运营「审图在哪儿审」——「回工位」则不做,「边拍边审」则只适配快审页与首页 |
| 间距令牌未铺开 | `space` / `radius` 已定义,近 500 处 inline style 未收敛(本文原写 446,已过期;要当前数字跑 `grep -ro 'style={{' frontend/src | wc -l`)。随手改到哪个文件就收敛哪个,不单独排期 |
| 两条 lint 规则未验证语义 | `no-restricted-syntax` 的 AST selector **写错不会报错、只会静默失效**。CI 现在会跑 `npm run lint`,但那只证明规则能加载,不证明它选中了目标。需要人为写一段违规代码验一次它真的报 |
| `color-mix()` 浏览器下限 | `BrandTag` 用了它。Chrome 111+ / Safari 16.2+ / Firefox 113+。要支持更老的浏览器需退回预设浅色或手写 rgba |
| 暗色模式需人眼过一遍 | `BrandTag` 的 10% / 35% 混合比是对着亮色调的,深底上 `textMuted` 那一档可能太淡 |

## 四、需要你决策 / 提供的

- [x] ~~视觉大模型评分器用哪家、哪个模型~~ —— 已接入,四种后端按配置切换,不需改代码。
      **但上线前必须用人工审核样本重新校准 A/B/C/D 阈值**:换模型会整体平移分数分布,
      阈值不动会导致大批误判(见 `docs/VISION-EVALUATOR.md` 第八节)。
- [x] ~~**任务编号冲突,需要定一个**~~ —— **A40 已定:发布接口 = 任务 25**,
      原任务 18(Batch Outbox)保持原义且已由 A35 完成。补号行在 `REVIEW.md`
      12.1 表末,理由见 `docs/DECISIONS.md` §3.11。以下是当时的原始描述:
      `docs/REVIEW.md` 第 12.1 节的任务表里,
      **任务 18 是「Batch Outbox 与异常恢复」**(P3,依赖 17),与发布 API 无关;
      任务表里根本没有「发布 API」这一项(任务 20-A 直接依赖 15/16)。
      「任务 18(发布 API)」这个叫法出现在 REVIEW.md 开头的进度快照、
      `CLAUDE.md`、`HANDOVER.md` 三处,是后来某轮意识到 20 之前缺一层接口时
      顺手安的号,撞上了已有的 18。
      **A28 做的是发布 API**(按 HANDOVER 的描述),不是 Batch Outbox。
      建议给发布 API 一个新号并补进任务表。这件事必须定:`CLAUDE.md` 写着
      REVIEW.md 是验收口径,而 `verify_delivery.py` 有一半检查引用它的节号。
- [x] ~~**两代审核页的性质**~~ —— **不是两代,是两个对象。** `/reviews` 审单张候选图,
      `/workbench-review` 审一版图片集 / 文案;审核中心顶部那条候选图计数
      **通向** `/reviews`,是调用关系不是竞争关系。两个都留,分工写进
      `docs/user/guide.md`,由 `tests/pure/test_review_entry_wiring.py` 钉住路由、
      跳转与那段分工说明。判据与「为什么没有可抽的共享组件」见 `DECISIONS.md` §3.109。
- [ ] **SHEIN 官方文档的取证基准**(评审 P-19,P0)。评审稿本身说得很清楚:
      基准没定之前,PRD 里那些以「仓内缺口」为依据的条款**不是有分歧,是不可评审**。
      需要两件事:①把 `swimwear-imagegen-a71.zip` 与 `SHA256SUMS-a71.txt` 对工作树
      逐项比较,明确「以工作树为基准」还是「先合入 a71 再评审」;
      ②S-01～S-31 逐页重开(S-07 切 `3001926`),按 `docs/vendor/shein-openapi/SOURCES.md`
      的形状存档。台账已经建好,三列和守卫都在等数据。
- [ ] **稳定店铺身份的取值来源**(评审 P-01,P0)。`query-store-info` 的响应里哪个字段
      是它,官方页面上要能读出来;另外三个实测动作(应用重装 / 授权轮换 / 多店粒度)
      要真的跑一遍 —— 清单在 `backend/app/channels/shein/shop_identity.py` 的 `PROBES`。
      **粒度那条跑完还要把结论填进 `IDENTITY_GRANULARITY`**(`PER_SHOP` 还是
      `PER_APP_AND_SHOP`):作用域按它分叉,填不上时 `derive()` 拒绝派生 ——
      跑完实测却不落结论,和没跑一样。
      **这两件都没有的话 SH-1 不可开工**,而现在这句话是机读的:`derive()` 会抛错。
- [ ] **双人复核要不要真做**(评审 P-03,P0)。本轮选的是「先不做,并把不可做写成
      会变红的事实」(`DECISIONS.md` §3.113)。真要做的话前置是**按人分的账号**,
      而不是现在这套角色口令 —— 那是一次身份模型改动,不是一个开关。
- [ ] **fal.ai**:具体使用哪个 model endpoint,以及它的输入 schema。
- [ ] **ComfyUI**:服务地址、真实工作流 JSON、各输入节点的真实 ID。
- [ ] **主密钥轮换**(人工动作)。早期交付包带出过 `.secrets/.settings.key`,
      应视为已泄露。步骤见 `docs/DECISIONS.md` §3.1。

## 五、本机真环境验收快照(2026-08-07)

PG `39.97.61.13:5432` + Redis `39.97.61.13:6379`,本机直连,**不起 Docker**。
迁移文件树 head = `0045`;真库 `imagegen` / `imagegen_test` 起始 head = `0038`,
本次把 `imagegen_test` 升到 `0045` 后跑 P0。`imagegen` 库未动。

逐条数字、失败响应原文、复现命令见 **`docs/AC-VERIFICATION.md`**。
本节给一张总结表,数字不写死(STATUS 第五节那条规矩);命令拿不准以
`AC-VERIFICATION.md` §7 为准。

| P0 项 | 状态 | 一句话根因 |
|---|---|---|
| P0-1 真库 pytest 全量 | ❌ | `POST /api/products` 接口契约改了(SPU 外键化),12-4/12-5 fixture 仍走老路径,11/13 在 `_product_with_asset` 上 422 |
| P0-2 Alembic 升降级 | ✅ | `imagegen_test` 上 0038→0045→base→0045 全链通过;`imagegen` 未动 |
| P0-3 前端四条 + docker | ⚠️ | ~~typecheck 6 条 TS2322(`nav-and-url-filters.test.tsx`);Vitest 因 `node_modules/.vite` root-owned EACCES~~ **前两格已于 2026-08-09 复验关闭**(typecheck 退出 0 零诊断、Vitest 97/97,见 `AC-VERIFICATION.md` §10);**docker 两条仍未验证** |
| P0-4 R-04/R-05 | ✅ | `run_pure_tests.py batch12_7` 16/16 + `verify_imports.py` 419 文件 |
| P0-5 BILLED_RESULT_UNKNOWN 演练 | ✅ | `12-7_billed_unknown` + `batch_receipt_lifecycle` 18/18 |
| P0-6 租约 fencing 双 session | ⚠️ | `batch_lease_concurrency` 1 条真库跑绿;`12-5` 5 条同 P0-1 fixture 问题 |

AC-01~AC-22:22 条**全部未验证**(原文多数沿用 v3.0,仓库内不可得;AC-21/22
有原文但需真实样照 + 真实抽取器调度)。**§14.3 的人工测试准入仍未满足**。

---

## 六、怎么验证

```bash
make test-pure     # 纯逻辑测试,只要有 python3 就能跑,不需要任何三方依赖
make test          # 容器内全量 pytest(含 requires_db 模块)
                   # 需要 PostgreSQL + Redis;缺 Redis 时生成链路会静默停在 CREATED
                   # (Celery 即使 eager 也要构造结果后端),表现是一批看不懂的失败。
                   # A42 合入时补了三道:CI 起 redis 服务、conftest 在 CI 里连不上
                   # 就点名炸掉、verify_delivery 盯住 ci.yml 别把它删了。
                   # **本机「有库无 Redis」仍然会得到那批失败** —— 治它要逐条贴
                   # requires_redis,而名单得真库跑一遍才准,见 conftest.py 那段注释
make smoke         # 对着跑起来的系统走一遍完整闭环
cd frontend && node tools/syntax-check.mjs   # 前端全量语法解析
```

本文不写测试通过数。写死的数字会在下一次增删用例时过期,而它过期了不会有任何东西
报错 —— 这个仓库的文档里曾经有 19 处这样的数字,各自冻在自己那一刻。要当前数字,
跑上面第一条。

---

## 七、文档地图

一共 33 份。**每份都写明「什么时候看」** —— 如果一份文档回答不了「谁会在什么情况下
打开它」,它就不该留下。这个数和下表的行数由
`tests/pure/test_a46_phase5_doc_truth.py` 钉在一起:漏收一份活文档、或者加了行
不改这句话,都会变红。

按批次/阶段留档的 `docs/MERGE-A45-*.md`、`docs/REVIEW-A4x-*.md` 与
`docs/REVIEW-STAGE*-CONCLUSION.md` 已经**全部收编并删除**(眼下 0 份):结论沉进
`DECISIONS.md`,逐份的去处记在 §3.107,原件按老规矩不留档。外部审计原件是唯一
例外,移进了 `docs/notes/`。

**给人看的入口是 `docs/README.md`。** 那一页把下表按"谁会在什么情况下打开它"
分成四组并配了一张图;这里保留全量表格是因为守卫钉着它,两处内容一致。

| 文档 | 什么时候看 |
| --- | --- |
| `README.md` | 第一次接触这个项目;想知道它是什么、怎么跑起来。**它是短入口** —— 页面与接口的全表在 `docs/user/guide.md` |
| `docs/README.md` | 找文档的入口:全部文档按"谁会在什么情况下打开它"分四组,附一张文档地图 |
| `docs/ARCHITECTURE.md` | 想看各条流程长什么样 —— 十三张 SVG,每张下面注明对应的代码位置 |
| `docs/overview.html` | 同一套图的单文件版本,双击打开、不联网,适合投屏与离线看 |
| `docs/user/guide.md` | 运营视角:每一页干什么、每一组接口干什么、成品图五个用途、登录与两个账号 |
| `docs/development.md` | 本机开发、目录结构、依赖方向契约、门禁分层(每层验不到什么)、日常命令 |
| `docs/subsystems/README.md` | 要改某一块代码之前:十二个子系统各一页,写边界、契约与踩过的坑 |
| `docs/cookbook/README.md` | 要加一个 Provider / 评分后端 / 品类 / 配置项 / 日志事件 / 门禁 |
| `docs/swimwear_sample_to_listing_prd_v3_1_1.md` | 要对"这系统该做成什么样"下结论 —— 阶段划分(§13)、人工测试准入(§14.3)、各条硬规矩的原文都在这里;全仓注释以 §N 指它 |
| `docs/PRD-A55-PROMPT-REGISTRY-AND-LOG-CONSOLE.md` | 提示词注册表与运行日志控制台那一轮的需求原文 |
| `docs/STATUS.md` | **本文。** 想知道某个能力现在能不能用、有哪些已知限制、下一步卡在谁那里 |
| `docs/DECISIONS.md` | 要动数据库迁移、要改一条看起来「多余」的约束、要查某条决定为什么这么定。头部有全量索引 |
| `docs/UPGRADING.md` | 要升级一个已在运行的部署:必须做的人工动作、会被挡下的操作、不报错的口径变更 |
| `docs/STYLE.md` | 要写或改文档:五条可判定的写作约定 |
| `docs/notes/README.md` | 想知道某个坑的全过程,或者查一轮历史快照与交接 |
| `docs/DEPLOYMENT.md` | 要把它部署起来,或者线上出了问题要按故障对照表排查。**在 Windows / macOS 上部署看第十节** —— 换行符、宿主机网络、缺少 `make` 三处会卡住,报错不指向真正原因 |
| `docs/OPS-REVIEW.md` | 要改工作台/批次/冷启动/平台驳回相关的代码 —— 代码里 20 多处注释以 `OPS-REVIEW P1/P3/P4/P5` 指回它。也是想了解运营真实动线时最值得读的一份 |
| `docs/VISION-EVALUATOR.md` | 要换视觉大模型、调阈值、或排查评分结果不对 |
| `docs/PROVIDER-FASHN.md` | 要第一次用真实 Key 验证 FASHN,或排查它的报错与费用 |
| `docs/SETTINGS.md` | 要加一个可在网页上改的配置项,或搞清楚为什么某项「改了没反应」 |
| `docs/LOG-CONSOLE.md` | 要加一条日志、要查「这次模型调用到底发了什么」、或者想知道运行日志页与操作审计页各回答什么问题。第十章记着落地时对设计的五处订正 |
| `docs/log-console-prototype.html` | 运行日志页的交互原型,单文件,双击打开 |
| `comfyui/README.md` | 要接 ComfyUI —— 含节点 ID 的定位方法 |
| `sample-data/README.md` | 想知道示例商品与素材是怎么组织的 —— 占位图是生成物,首次要先跑生成脚本 |
| `docs/REVIEW.md` | 要知道下一步该做什么 —— 施工方案(a20 v4.1),第 12 章任务表已标完成状态 |
| `docs/REVIEW-CODE-ISSUES-2026-08-21.md` | 要知道代码层面还欠着什么 —— 按 B/F/X 编号的问题清单,每条带现象/证据/影响/改法/验证。**全部条目关闭后删除** |
| `docs/REVIEW-A28-TRACKING.md` | 要回答「a28 那份检视报告的 20 条阻断项现在还剩几条」。**下次复核只需核 4 条**,其余每条都绑了一条会红的测试 |
| `CLAUDE.md`(根 / backend / frontend) | 用 Claude Code 开工前。写的是约定与指针,不是目录说明 |
| `docs/AC-VERIFICATION.md` | 要回答「AC-01~AC-22 在本机真环境里到底哪几条跑过、哪几条没」 —— P0 6 项逐项结论、22 条 AC 状态表、复现命令合集都在这里 |
| `LOCAL_MANUAL_TEST.md` | 要在本机手工走一遍。Docker 启动、初始化、口令、**浏览器登录的六步验收(§4.5)**、6.1~6.10 逐步操作 |
| `docs/MANUAL-ACCEPTANCE.md` | 要做发布候选的完整 UAT 验收 —— 人员分工、两套库、配置清单、五个阶段的通过标准与证据要求 |
| `HANDOVER.md` | 想知道最近一轮改了什么、验了什么、**哪些没验**。只留最近一轮,更早的按轮次归档在 `docs/notes/` |
| `AGENTS.md`(根 / backend / frontend) | 读 `AGENTS.md` 的 agent 工具用。内容与同级 `CLAUDE.md` **逐字一致**,由守卫钉着不许分叉 |

另有 `docs/vendor/fashn-skill/` 是 FASHN 官方文档的存档,不是本项目文档,
不计入上表也不要改动 —— `docs/PROVIDER-FASHN.md` 的实现依据全部指向它。

## 历史快照索引

逐轮的评审快照与交接记录按日期归档在 [`notes/`](notes/README.md)。它们记的是
**写下那天的事实** —— 里面引用的文件后来可能已经删掉,那不是错误。

| 日期 | 轮次 | 归档 |
| --- | --- | --- |
| 2026-08-21 | 《前后端代码问题清单》整改批 | [2026-08-21-status-code-issues-remediation](notes/2026-08-21-status-code-issues-remediation.md) |
| 2026-08-09 | 2026-08-09 回归收口：Mock / Simulator 已进入人工测试 | [2026-08-09-status-mock-uat-entry](notes/2026-08-09-status-mock-uat-entry.md) |
| 2026-08-09 | 2026-08-09 评审修复:F-12/F-4 颜色维可操作 | [2026-08-09-status-review-fix-color-axis](notes/2026-08-09-status-review-fix-color-axis.md) |
| 2026-08-09 | 2026-08-09 评审修复:F-17 方案编辑、纯测试执行器与 mutation anchors | [2026-08-09-status-review-fix-plan-editor](notes/2026-08-09-status-review-fix-plan-editor.md) |
| 2026-08-09 | A45-batch29 / batch30:阶段 6 收官(6-4 七步向导 + 6-5 费用与影响提示) | [2026-08-09-status-a45-batch29-30](notes/2026-08-09-status-a45-batch29-30.md) |
| 2026-08-09 | A45-batch28:阶段 6 批次 6-3 —— 聚合工作流 API(颜色子态终于有人读了) | [2026-08-09-status-a45-batch28](notes/2026-08-09-status-a45-batch28.md) |
| 2026-08-09 | A45-batch27:阶段 6 批次 6-2 —— 七步增维(**完成度口径变了,见下**) | [2026-08-09-status-a45-batch27](notes/2026-08-09-status-a45-batch27.md) |
| 2026-08-09 | A45-batch26(6-2 前置):照着自己写的方法跑了一遍,红名单是空的 | [2026-08-09-status-a45-batch26](notes/2026-08-09-status-a45-batch26.md) |
| 2026-08-09 | A45-batch25:阶段 6 开工(批次 6-1)—— 先立判据,再动最底下那一层 | [2026-08-09-status-a45-batch25](notes/2026-08-09-status-a45-batch25.md) |
| 2026-08-08 | A45-batch24:三位外部评审的复核 —— 三条接缝各接了一半,已收口 | [2026-08-08-status-a45-batch24](notes/2026-08-08-status-a45-batch24.md) |
| 2026-08-08 | A45-batch20:阶段 5 批次 5-2B —— 颜色维接线,以及这台机器上第一次跑到真库 | [2026-08-08-status-a45-batch20](notes/2026-08-08-status-a45-batch20.md) |
| 2026-08-08 | 阶段 5 / 5-1 评审复核结论(2026/08/08) | [2026-08-08-status-stage5-1-review](notes/2026-08-08-status-stage5-1-review.md) |
| 2026-08-08 | A45-batch19:阶段 5 开工,批次 5-1 —— 草稿的颜色维上游快照(基线 batch18,2026/08/08) | [2026-08-08-status-a45-batch19](notes/2026-08-08-status-a45-batch19.md) |
| 2026-08-08 | A45-batch18:阶段 4 收口 —— 方案面板终于有了宿主页(基线 17-2,2026/08/08) | [2026-08-08-status-a45-batch18](notes/2026-08-08-status-a45-batch18.md) |
| 2026-08-08 | A45-batch17-2:1442 包审阅意见的修复(基线 1505,2026/08/08) | [2026-08-08-status-a45-batch17-2](notes/2026-08-08-status-a45-batch17-2.md) |
| 2026-08-08 | A45-batch14-28:阶段 1 身份收口 + 三条「算好了没人读」的最后一跳(基线 14-27,2026/08/08) | [2026-08-08-status-a45-batch14-28](notes/2026-08-08-status-a45-batch14-28.md) |
| 2026-08-08 | 2026-08-08 · A45-batch17-1 补丁审核 | [2026-08-08-status-snapshot](notes/2026-08-08-status-snapshot.md) |
| 2026-08-07 | A45-batch14-26:三笔决定类欠账 + 老建档路径切 SPU(基线 14-25,2026/08/07) | [2026-08-07-status-a45-batch14-26](notes/2026-08-07-status-a45-batch14-26.md) |
| 2026-08-07 | A45-batch14-24:「每一列都答得出谁写它」门禁(基线 14-23,2026/08/07) | [2026-08-07-status-a45-batch14-24](notes/2026-08-07-status-a45-batch14-24.md) |
| 2026-08-07 | A45-batch14-23:§6.5 两列的写入路径 + §4.8 去重键拆账(基线 14-22,2026/08/07) | [2026-08-07-status-a45-batch14-23](notes/2026-08-07-status-a45-batch14-23.md) |
| 2026-08-07 | A45-batch14-22:素材颜色归属的写入路径 + 新结构样例数据(基线 14-21,2026/08/07) | [2026-08-07-status-a45-batch14-22](notes/2026-08-07-status-a45-batch14-22.md) |
| 2026-08-07 | A45-batch14-21:`facts_stale` 派生接线 + 欠账还款日门禁(基线 14-20 合并包,2026/08/07) | [2026-08-07-status-a45-batch14-21](notes/2026-08-07-status-a45-batch14-21.md) |
| 2026-08-06 | A45-batch14-20 并线合并:阶段 3 识别 run 身份 + 阶段 4 多颜色图片生产(基线 14-19,2026/08/06) | [2026-08-06-status-a45-batch14-20](notes/2026-08-06-status-a45-batch14-20.md) |
| 2026-08-06 | A45-batch14-20 §4.6 五列落库,§9.2 幂等与 §5.3 指纹接线(基线 14-19,2026/08/06) | [2026-08-06-status-a45-batch14-20-2](notes/2026-08-06-status-a45-batch14-20-2.md) |
| 2026-08-06 | A45-batch14-19 §5.1 白名单成为取数入口(基线 14-18,2026/08/06) | [2026-08-06-status-a45-batch14-19](notes/2026-08-06-status-a45-batch14-19.md) |
| 2026-08-06 | A45-batch14-18 计费量问厂商要,并记下是谁说的(基线 14-17,2026/08/06) | [2026-08-06-status-a45-batch14-18](notes/2026-08-06-status-a45-batch14-18.md) |
| 2026-08-06 | A45-batch14-17 筛选状态住进 URL,阶段 0 最后一条代码项收口(基线 14-16,2026/08/06) | [2026-08-06-status-a45-batch14-17](notes/2026-08-06-status-a45-batch14-17.md) |
| 2026-08-06 | A45-batch14-16 溯源列落库,候选落盘写入(基线 14-15,2026/08/06) | [2026-08-06-status-a45-batch14-16](notes/2026-08-06-status-a45-batch14-16.md) |
| 2026-08-06 | A45-batch14-15 素材归属外键落库,四批欠账收其二(基线 14-14,2026/08/06) | [2026-08-06-status-a45-batch14-15](notes/2026-08-06-status-a45-batch14-15.md) |
| 2026-08-06 | A45-batch14-14 守卫的窗口必须是封闭的(基线 14-13,2026/08/06) | [2026-08-06-status-a45-batch14-14](notes/2026-08-06-status-a45-batch14-14.md) |
| 2026-08-06 | A45-batch14-13 §11 第一行:按作用域的失败归集(基线 14-11/14-12 合树,2026/08/06) | [2026-08-06-status-a45-batch14-13](notes/2026-08-06-status-a45-batch14-13.md) |
| 2026-08-06 | A45-batch14-12 识别 run 的终态、取消与幂等键(基线 batch14-10,2026/08/06) | [2026-08-06-status-a45-batch14-12](notes/2026-08-06-status-a45-batch14-12.md) |
| 2026-08-06 | A45-batch14-11 §11 两条新场景:确认队列口径 + AI 图伪装拦截(基线 batch14-10,2026/08/06) | [2026-08-06-status-a45-batch14-11](notes/2026-08-06-status-a45-batch14-11.md) |
| 2026-08-06 | A45-batch14-11 首次在装齐依赖的机器上跑门禁(基线 batch14-10,2026/08/06) | [2026-08-06-status-a45-batch14-11-2](notes/2026-08-06-status-a45-batch14-11-2.md) |
| 2026-08-05 | A45-batch14-2 对 batch14 交付包的走读修复(基线 batch14 + 已合入 batch13-3,2026/08/05) | [2026-08-05-status-a45-batch14-2](notes/2026-08-05-status-a45-batch14-2.md) |
| 2026-08-05 | A45-batch14 阶段 3 第一批:真实多模态抽取器(基线 batch13-2,2026/08/05) | [2026-08-05-status-a45-batch14](notes/2026-08-05-status-a45-batch14.md) |
| 2026-08-05 | A45-batch13-3 对 batch13-2 交付包的走读修复(基线 batch13-2,2026/08/05) | [2026-08-05-status-a45-batch13-3](notes/2026-08-05-status-a45-batch13-3.md) |
| 2026-08-04 | A45-batch13-2 阶段 1 第一批的走读修复(基线 batch13,2026/08/04) | [2026-08-04-status-a45-batch13-2](notes/2026-08-04-status-a45-batch13-2.md) |
| 2026-08-04 | A45-batch13 阶段 1 第一批:身份骨架(基线 batch12-7,2026/08/04) | [2026-08-04-status-a45-batch13](notes/2026-08-04-status-a45-batch13.md) |
| 2026-08-04 | A45-batch12-7 阶段 P0(基线 batch12-6,2026/08/04) | [2026-08-04-status-a45-batch12-7](notes/2026-08-04-status-a45-batch12-7.md) |
| 2026-08-04 | A45-batch12-4 自审(2026/08/04) | [2026-08-04-status-a45-batch12-4](notes/2026-08-04-status-a45-batch12-4.md) |
| 2026-08-04 | A45-batch12 增补(2026/08/04) | [2026-08-04-status-a45-batch12](notes/2026-08-04-status-a45-batch12.md) |
| 2026-08-03 | A45 版本状态(2026/08/03) | [2026-08-03-status-a45](notes/2026-08-03-status-a45.md) |
| 2026-08-02 | A43 版本状态(2026/08/02) | [2026-08-02-status-a43](notes/2026-08-02-status-a43.md) |
| 2026-08-03 | A44 评审修复进度（batch1 ～ batch7） | [2026-08-03-status-a44](notes/2026-08-03-status-a44.md) |
| 2026-08-12 | 评审整改批:离线验证快照(2026-08-12) | [2026-08-12-status-offline-verification](notes/2026-08-12-status-offline-verification.md) |
| 2026-08-12 | 2026-08-12 评审整改 + a50/a51 收口交接 | [2026-08-12-status-review-remediation](notes/2026-08-12-status-review-remediation.md) |
