# 泳衣新品样品到图片与 Listing 生产向导 PRD

**版本:** v3.1.1(实施修订版;实现对齐修订)
**日期:** 2026-08-04
**项目基线:** Swimwear ImageGen A45 / batch12-6(= batch12-5 + 回归评审三条 P1 修复)
**文档类型:** 产品、开发与验收基线
**修订依据:** v3.0 全文对照 batch12-5 代码库逐项核实(模型层、属性层、流程层、失效矩阵、幂等与批次机制、已知遗留项);v3.1 → v3.1.1 只同步 batch12-6 落地的实现事实(§0.4),不改任何业务规则、流程或验收标准
**实施前提:** 系统尚未投入使用,可以调整数据模型、接口和页面;不考虑存量**数据**迁移与旧流程兼容。注意:无数据迁移 ≠ 无代码迁移,见 §3。

---

## 前置:v3.0 原文缺失清册(文档债,A45-batch15-merged 补记)

> **这一节不是业务内容,是一张欠条。**先读它,因为它决定了下面 20 个章节里
> 哪些话你能查证、哪些不能。

**v3.0 文档不在这个仓库里。** 仓库最早的一版就是 v3.1,本文件是 v3.1.1。
而 v3.1 的写法是**增量修订**:凡是 v3.0 已经写对的地方,它写"沿用 v3.0"
而不重述。这个写法在 v3.0 拿得到时是合理的,在拿不到时**它把 21 个章节
变成了指向空地的引用**(下表 20 行 —— §7.4 与 §7.5 在正文里合写一个标题)。

### 甲、悬空引用逐条(21 处引用 / 21 个章节号)

| 章节 | 悬空的是什么 |
|---|---|
| §1.2 | 当前业务问题清单 |
| §2.2 | 非目标全部条目 |
| §3.3 | 复用边界 |
| §5.1 | 事实抽取输入白名单条件 |
| §5.3 | 指纹元素清单;改变/不改变指纹的操作清单 |
| §6.1 | "不得要求填写"清单;页面要求 |
| §6.2 | 样品上传全部规则(归属必确认、最低完整度等) |
| §6.3 | 一期识别字段与禁止字段清单 |
| §6.4 | 模特授权约束 |
| §6.5 | 事实一致性检查清单(九项) |
| §6.6 | Listing 输入白名单与产出口径 |
| §6.7 | 草稿预览表格与 READY 条件 |
| §7.3 | 十状态清单 |
| §7.4 / §7.5 | 页面固定信息与交互约束全部条目 |
| §8.2 | 版本规则 |
| §11 | 十四个异常场景 |
| §12.4 | 聚合返回清单 |
| §14.1 | **AC-01 ~ AC-20 全部原文** |
| §14.2 | 测试数据集数量表 |
| §15 | 十项运行指标定义 |

### 乙、§14.1 那一条最贵,单独说

AC-01~AC-20 是 §14.3「人工测试准入」的判据。**判据本身拿不到**,于是这 20 条
既不能验证通过、也不能判定失败 —— 只能是"未验证"。这与 P0 那 5 项的
"未验证"是**两种不同的东西**:P0 缺的是机器(数据库、docker),补一台机器就能推进;
AC-01~20 缺的是判据,补一百台机器也没用。

其中 **AC-02 / AC-06 / AC-07 / AC-20 在本仓库内没有任何出处** ——
§13 四条阶段验收行(§13 阶段 2 / 4 / 5 / 6)的并集只覆盖 16 条:

    AC-01 05 14 15 16 17    阶段 6
    AC-03 04                阶段 2
    AC-08 09 12 13          阶段 4
    AC-10 11 18 19          阶段 5

剩下那 4 条连"属于哪个阶段"都反推不出来。逐条状态见
[`docs/AC-VERIFICATION.md`](./AC-VERIFICATION.md)。

### 丙、解锁条件与守卫

**解锁条件只有一个:把 v3.0 原文补回仓库。** 这是文档债,不是代码债 ——
没有任何一次编码、任何一台机器能把它还上。

在还上之前,这张表由 `backend/tests/pure/test_a45_batch16_doc_truth.py` 盯着:
**表里列的章节集合,必须与正文里真正出现悬空引用的章节集合逐一相等。**
所以它对进展是中性的 —— 补回 v3.0 之后逐节消化,表跟着缩短,守卫一直是绿的;
而新增一处"沿用 v3.0"却不记进表里,它会红。

---

## 0. v3.0 → v3.1 修订摘要

v3.0 的业务方向(SPU/颜色/SKU 主线、事实与样品分层、原图与 AI 图隔离、七步向导)全部保留。修订集中在三类问题:

### 0.1 把"新建"改成"复用/扩展"(最重要的一类修正)

v3.0 有六处把**代码库已存在且已被测试固化的机制**写成了从零新建。按 v3.0 字面实施,会重写并作废一批成熟能力,同时丢掉它们已经解决过的坑(证据与值分离、置信度校准、封闭集合失效矩阵、唯一下一步判定)。逐项对照:

| # | v3.0 提出 | 代码库现状(batch12-5) | v3.1 处理 |
|---|---|---|---|
| 1 | 新建 `ProductFact` 单表(§4.5/4.6) | 属性层已是四表设计:`product_attribute_extractions` / `attribute_evidence` / `product_attribute_values` / `attribute_calibrations`。`owner_type` 已含 SPU/PRODUCT/VARIANT/SKU/CHANNEL 五层;状态枚举 `AttributeStatus` 与 v3.0 §4.6 的六个状态**逐字相同**;版本历史用 `is_current` 部分唯一索引;证据与采信值严格分离;置信度经校准表 fail-closed | **不建 ProductFact**。沿用四表,增补输入指纹列与 SPU/VARIANT 归属外键化(§4.5) |
| 2 | 新建 `AttributeExtractionRun`(§4.8) | `ProductAttributeExtraction` 已存在(模型/Prompt 版本、目标字段、成败计数、token 用量、脱敏原始响应)。缺:异步执行、SPU 级作用域、输入指纹、取消。接口注释已预告异步化是既定方向 | 扩展该表 + Celery 异步任务,不另建(§4.6) |
| 3 | 新建失效矩阵(§8.1) | `workbench/stale_matrix.py` 已把 6 变更源 × 3 目标 = 18 格做成**封闭集合**,逐格 `mechanism` 点名负责函数并有穷举测试 | 扩行不重写:新增变更源与 FACTS 目标列,沿用封闭集合测试法(§8) |
| 4 | 新建向导派生状态机(§7.3) | `workbench/flow.py` 已是零依赖纯函数状态机:五步、唯一下一步、BLOCKING/NEEDS_CONFIRM/REMINDER 三级、STALE 与 BLOCKED 分离 | 向导 = flow.py **增维**(SPU 聚合态 + 每颜色子态),不建第二状态机(§7) |
| 5 | SPU 级 ListingCopy(§4.10) | `ListingCopy` 键已是 `(spu, channel, site, locale, version)`,即已是 SPU 级 | 直接沿用;颜色层走属性层,不建颜色 copy 行(§4.9) |
| 6 | 图片集颜色绑定 | `ListingImageItem.variant_id` 列与 COALESCE 唯一约束**已在库**(NULL=SPU 通用,主图每变体唯一)。挂起的原因不是 schema,而是缺"通用图与颜色图混排以谁为准"的业务决定(BLOCK-02/04) | v3.0 §6.5 的规则恰好就是那个业务决定 → 保留规则,落点改为"补 UI 入口 + 结构化来源",不重建表(§6.5) |

同类的还有:生成任务幂等键(由输入派生,已存在)、批次付费动作回执与租约、SSRF 防护(`net_safety`)、上传校验、密钥与日志脱敏、ModelTemplate 授权闸(A45 §11)、审计流水——v3.0 §9/§10 所列各项在代码库中已成立,v3.1 改为"沿用 + 对新对象补键组成",不再表述为新建。

### 0.2 修复 v3.0 的三处规格缺陷

| # | 缺陷 | 修法 |
|---|---|---|
| D1 | **指纹作用域过宽**:§5.3 只定义了单一 SPU 级样品指纹。后果:给颜色 A 补传一张图,颜色 B 的事实与图片也会被判 stale,多颜色 SPU 会被反复无谓返工 | 双作用域指纹:共享事实用全 SPU 证据集指纹,颜色事实用该颜色证据子集指纹;失效矩阵按作用域细化(§5.3、§8.1) |
| D2 | **GenerationPlan 缺实体**:§6.4 与 §7.3 都引用"生成方案"(SPU 默认 + 颜色覆盖、方案指纹进幂等键),但 §4 数据模型没有定义它,方案无处持久化、指纹无从计算 | 新增 `GenerationPlan` 实体(§4.7) |
| D3 | **`evidence_class` 可被错标绕过**:§4.7 把证据等级设计成独立赋值的存储列,与 source/溯源可能漂移——一张 AI 图被(误)标成 `PRODUCT_EVIDENCE` 就穿透了 §5.1 白名单 | 溯源列(`generation_task_id`)+ 单一写入点 + 数据库 CHECK(AI 来源恒非 PRODUCT_EVIDENCE)+ 守卫测试;白名单实现为唯一取数入口(§4.4、§5.1) |

### 0.3 实施策略与阶段修订

- **"重建核心表"改为"规范化"**(§3):v3.0 阶段 0 低估了代价——`products.id` 被素材、生成、属性、图片集、文案、草稿、发布、批次全域外键或语义引用,"重建"意味着触碰全部服务层并作废约 1800 条纯逻辑测试。v3.1 改为:新增 `spus` / `color_variants` 表;`products` 保留为 SKU 粒度行,把字符串 `spu` 与 `variant_key` 升级为外键;素材/属性/生成/文案/草稿表**不重建**,按需增列。
- **阶段重排**(§13):真实抽取器与身份规范化解耦、可并行;v3.0 §14.3 罗列的准入项(真库测试、fencing 验证、重复扣费收口)**前置为阶段 P0 门禁**并逐条挂钩代码库现有追踪项——它们不依赖任何新功能,放在最后等于让每个阶段都建在未验证的地基上。
- **受众维度补齐**(v3.0 遗漏):A45 已把受众(WOMEN/MEN/UNISEX)做成一等业务维度(规则包、模特筛选 §10.5、生成前阻断 §12.4、授权 §11)。v3.1 明确:SPU 建档时受众**必填**——无存量数据,正好删除"NULL 受众 + 无前缀规则包"那条兼容缝;向导内所有受众闸继续生效;男装分档未校准前,男装任务自动通过保持关闭。
- **枚举收敛**:素材状态沿用现有 `PENDING/READY/QUARANTINED/FAILED/DELETED`,不引入近义新值(v3.0 的 UPLOADING/MISSING/DISABLED);事实来源沿用现有 `AttributeSource` 六值(MANUAL/EXTRACTED/IMPORTED/SUPPLIER/DEFAULT/DERIVED),不缩成三值——DEFAULT/DERIVED 在合并优先级里有既定语义。

### 0.4 v3.1 → v3.1.1:同步 batch12-6 实现事实(不改业务规则)

v3.1 定稿时,batch12-5 回归评审的三条 P1(费用台账重复记账、评分重排早于释放租约、租约与卡死阈值低于合法最长耗时)尚未修复,§13-P0 据此把它们列为待办。batch12-6 已把三条的**代码侧**全部落地,以下事实随之进入基线:

| # | batch12-6 落地事实 | 对本文档的影响 |
|---|---|---|
| 1 | **生成费用幂等**:`provider_usage_records.billing_key`(形状 `<attempt_id>:submit`)+ 唯一约束(迁移 0034);异常恢复只**更新**原计费记录(结论、候选数),计费单位只增不减;轮询/取结果/评分刻意不带键——那几类每次都是真实的新一次调用 | §1.1 生成链路行;§13-P0 真库项措辞;迁移 head 由 0033 变为 0034 |
| 2 | **生成阶段租约成为完整协议**:逐提交点心跳 + 续期、fencing(租约易主时旧 worker 立即停写、不落任何结论)、首轮评分同样持锁、回收器改用"心跳 + 活租约"双信号;租约时长与卡死阈值由 `workflows/phase_budget` 按**当前配置**推导(候选上限单一来源 `MAX_CANDIDATE_COUNT=8`),设置页热生效对回收器同样成立 | §1.1;§4.6 引用的"生成链路的租约/心跳模式"自此为既成事实而非预告;§13-P0 |
| 3 | **评分重排先还租约再派发**(commit → release → dispatch,两处调用点),消除"消息已消费、Outbox 已 DISPATCHED、任务却无人执行"的竞态 | 无独立条款,归入 §13-P0 fencing 项验收 |
| 4 | **真库用例存量**:batch12-4 恢复链路 6 条 + batch12-5 计费幂等/租约协议/接管 fencing 7 条 = **13 条已写、均未执行**(整改环境无 PostgreSQL) | §13-P0 第一条的清单口径 |

**边界必须说清:** 上述修复只覆盖**生成阶段租约**。批次条目租约(workbench 域,`_apply_outcome` 不校验持有者、`reap_expired_leases` 不问 owner)是另一套机制,batch12-6 未触碰,其真库双 session 场景在 §13-P0 中**保留为独立待办**——两者混为一谈会把一条未修的已知限制记成已修。

---

# 1. 背景与问题

## 1.1 当前项目已有能力(按代码库核实修订)

| 模块 | 现状 |
|---|---|
| 商品与素材 | Product 为 SKU 粒度行(`sku` 全局唯一),`spu` 为字符串字段,`variant_key` 为一次分配的颜色身份字符串;素材统一层 `MediaAsset`(source 与 role 正交、内容哈希去重、隔离态、合规列),旧 `ProductAsset` 仍并存 |
| 属性层 | 四表设计,证据与采信值分离,owner 分层(SPU/PRODUCT/VARIANT/SKU/CHANNEL),六状态,版本历史,校准 fail-closed。**抽取器只有 Mock**,且当前在请求内同步执行 |
| 生成链路 | 任务/尝试/候选三层,17 状态转移表,幂等键由输入派生,Celery 编排,费用流水(**batch12-6 起带计费幂等**:billing_key 唯一约束,异常恢复不重复记账),视觉评分器四后端可切换;阶段租约含逐提交点心跳/续期/fencing,卡死阈值按当前配置推导(batch12-6) |
| 审核与图片集 | 审核队列、逐件快审、A 档抽检;`ListingImageSet` 版本化,批准后重排派生新版本;**颜色绑定列在库但无 UI 入口,`variant_id` 恒为 null**(BLOCK-02) |
| 文案与草稿 | SPU 级 `ListingCopy`(渠道/站点/语言/版本),禁词与声明校验;`ListingDraft` + 导出闸,过期提示按矩阵展开 |
| 发布域 | 渠道 Simulator、幂等提交、轮询、下架、驳回台账(本期仍不接真实平台) |
| 失效与流程 | `stale_matrix` 封闭集合;`flow.py` 五步派生状态、唯一下一步 |
| 基础设施 | 批次租约(一次一件)+ 回执 + `BILLED_RESULT_UNKNOWN` 闸、审计、花费台账、设置热生效、S3 兼容存储 |

## 1.2 当前业务问题(保留 v3.0,两处按事实修订)

| 问题 | 当前表现 | 业务后果 |
|---|---|---|
| 主流程分散 | 商品、属性、任务、审核、图片集、文案和草稿分布在不同入口 | 运营需要记忆下一步,容易漏做、重复做或进入错误页面 |
| 建档字段过重 | 创建商品时可预填颜色、图案、领型等视觉投影字段 | 手工值和模型确认值形成两套事实(投影列虽受 AST 守卫保护为只读,但建档表单仍暴露它们) |
| 原图与 AI 图混用 | **已确认的成本洞**:属性识别对每张 AI 候选图都发付费调用,而其证据一票投不上(STATUS 遗留项);根因是 `media_assets` 缺生成溯源列 | AI 生成错误可能反向污染商品事实,且持续白花识别费用 |
| 多颜色关系不完整 | 颜色身份是 `variant_key` 字符串,属性 owner 用 `<len>:<spu>/<variant>` 命名空间拼接;图片集颜色绑定无入口 | 可能出现红色 SKU 使用黑色图片、一个颜色完成却误判整件完成 |
| 上游变化未统一失效 | 失效矩阵已存在但不含"事实"目标列,也不区分颜色作用域 | 已过时的事实仍可能被文案与草稿消费 |
| 真实属性识别未落地 | `_BACKENDS` 只有 mock,环境横幅常驻"模拟环境"提示 | 无法真实验证"上传样品后生成商品事实和文案" |

## 1.3 核心产品原则(不变)

> 样品不是直接交给模型自由生成标题和图片的输入。样品必须先形成可追溯、可确认的商品事实;图片生成和 Listing 生成分别消费这些事实,并分别通过审核。

---

# 2. 产品目标与非目标

## 2.1 产品目标(G5 与 G8 按事实收紧)

| 编号 | 目标 | 可验证结果 |
|---|---|---|
| G1 | 建立一体化新品生产向导 | 运营可从建档连续完成样品、事实、图片、Listing 和草稿 |
| G2 | 建立明确的 SPU、颜色变体和 SKU 模型 | 所有商品对象归属唯一外键,不再依赖字符串推断;属性 owner 不再使用命名空间拼接 |
| G3 | 最小化新品建档字段 | 运营无需提前填写视觉属性;建档表单不再暴露投影列 |
| G4 | 建立唯一商品事实源 | 图片核对、Listing 和草稿只读取 `CONFIRMED` 属性值;投影列保持单写入点 |
| G5 | 原始样品与 AI 图片严格分层 | 事实抽取的取数入口唯一且带守卫测试;AI 候选注册素材时自动携带溯源;识别不再对 AI 图付费调用 |
| G6 | 完整支持多颜色商品 | 每个 ACTIVE 颜色均有独立样品、事实、图片状态和草稿映射 |
| G7 | 建立明确的版本与失效规则 | 上游变化按扩展后的封闭矩阵自动使下游过期,且按颜色作用域隔离 |
| G8 | 接入一期真实多模态属性识别 | 真实样品能生成结构化、可人工确认的属性建议;未校准字段一律不自动确认(fail-closed 沿用) |

## 2.2 非目标(保留 v3.0 全部,补三条)

本期不包含:真实 SHEIN 或其他平台的自动发布;完整的模特授权合同管理平台;ERP、PIM、采购和供应商管理替代;自动定价、自动库存、销售预测;从图片推断精确材质比例、UPF、防晒等级、塑形功效、认证和产地;多模型竞赛、自动高置信度发布、复杂 Prompt A/B 平台;多店铺、多租户和企业级 RBAC;基于销售转化率自动优化图片与文案;旧数据迁移、旧接口兼容和旧流程保留。

补充明确排除:

- 男装评分阈值校准与男装自动通过(A45 阶段 4 遗留,需真实男装商品图,另行排期;向导内男装任务全量人工审阅);
- 账号体系(Reviewer 角色以流程限制实现,见 §10.1);
- 平台侧状态自动回流(仍手工录入,发布域维持 Simulator)。

---

# 3. 实施前提与重构原则

## 3.1 无数据迁移 ≠ 无代码迁移

系统尚未正式使用,因此允许:重置本地和测试数据库;调整接口与前端路由;删除含义重复的兼容缝(NULL 受众矩阵、`variant_key` 命名空间);更新 Mock 和测试数据;一次性切换到新流程。不需要:存量数据迁移脚本、新旧双写、旧接口兼容层、双轨运行。

但必须正视:`products.id` 与 `spu` 字符串被素材、属性 owner、生成任务、图片集、文案、草稿、发布、批次共约十个域引用;约 1800 条纯逻辑测试与几十条真库用例断言现有形状。**"重建核心表"的成本不在数据,在代码与测试。** 因此:

## 3.2 规范化而非重建

| 对象 | v3.0 方案 | v3.1 方案 |
|---|---|---|
| SPU | 新建表,清理旧 Product | 新建 `spus` 表;`products.spu` 字符串保留为反规范化读列,权威归 `products.spu_id` 外键 |
| 颜色变体 | 新建表 | 新建 `color_variants` 表;`products.variant_key` 退役,权威归 `products.color_variant_id` 外键 |
| SKU | 新建表 | **`products` 表就是 SKU 表**(本就是 SKU 粒度、`sku` 全局唯一),增列不重建 |
| 素材/属性/生成/图片集/文案/草稿 | 部分重建 | 一律增列/改约束,不重建 |

投影列(primary_color 等 8 列)处理:建档表单与创建接口不再接收它们;列本身保留并维持"属性服务单写入点 + AST 守卫"现状——列表页筛选、看板分组依赖它们,删除属于独立的清理任务,不进本期关键路径。

## 3.3 复用边界(保留 v3.0,落到点名)

向导负责编排和聚合,不重新实现:文件存储与图片校验(`services/storage`、`upload_validation`、`image_probe`)、Provider 适配器与注册表、GenerationTask 调度与幂等、审核队列与快审、ImageSet 版本机制、ListingCopy 与校验、Draft 与导出、审计、花费台账、错误分层(`describeError`/`ErrorNotice`)、批次租约与回执。

---

# 4. 目标数据模型

## 4.1 对象关系

```text
Spu
├── SPU 共享事实(属性层 owner_type=SPU)
├── SPU 通用素材(media_assets.color_variant_id IS NULL)
├── SPU 级 ListingCopy(现有表)
├── GenerationPlan(SPU 默认方案)
├── ColorVariant A
│   ├── 颜色事实(属性层 owner_type=VARIANT)
│   ├── 颜色样品素材
│   ├── GenerationPlan(颜色覆盖,可选)
│   ├── GenerationTask / GenerationCandidate(现有三层)
│   ├── Approved ImageSet(现有表,variant 绑定启用)
│   └── Sku 行(products 表,S/M/L…)
├── ColorVariant B
│   └── …
└── ListingDraft(现有表 + 上游版本快照)
    └── 颜色 → SKU → 图片集 → 结构化颜色字段映射
```

## 4.2 Spu(新表)

| 字段 | 说明 |
|---|---|
| `id` | UUID 主键 |
| `spu_code` | 全局唯一业务编码 |
| `internal_name` | 内部名称 |
| `audience` | 受众,**必填**(WOMEN/MEN/UNISEX;无存量数据,删除 NULL 兼容缝) |
| `base_category` | 基础类目(默认 swimwear;规则包键仍由 `core/audience.category_code_for` 唯一派生) |
| `supplier_ref` | 可选供应商编号 |
| `status` | DRAFT / ACTIVE / DISABLED |
| `created_by` | 创建人(沿用 X-Actor) |
| `created_at` / `updated_at` / `row_version` | 时间与乐观锁 |

`Spu` 不保存主颜色、图案、领型、肩带等视觉事实。原来挂在 Product 行上的受众列迁移语义:受众唯一权威在 `spus.audience`,`products` 上的同名列退役(同 SPU 受众一致性检查随之删除——结构上不可能再不一致)。

## 4.3 ColorVariant(新表)

| 字段 | 说明 |
|---|---|
| `id` | UUID 主键 |
| `spu_id` | 外键 |
| `variant_code` | SPU 内稳定唯一标识;`UNIQUE(spu_id, variant_code)` |
| `working_name` | 建档阶段内部临时名称 |
| `display_name` | 正式颜色名称。**投影列**:唯一写入点是属性服务在 VARIANT 层 `standard_color_name` 事实 CONFIRMED 时同事务写入(复用现有投影模式与 AST 守卫),不允许直接编辑 |
| `supplier_color_code` | 可选 |
| `sort_order` | 展示顺序 |
| `sellable_status` | PLANNED / ACTIVE / PAUSED / DISABLED |
| `created_at` / `updated_at` / `row_version` | — |

属性层 VARIANT owner_id 从 `<len>:<spu>/<variant_id>` 命名空间字符串**切换为 `color_variants.id` 的字符串形式**;SPU owner_id 同理用 `spus.id`。命名空间 hack 与其宽度注释一并退役(无存量数据,直接切换,守卫测试同步改写)。

## 4.4 Sku = products 表(增列)

保留:`sku` 全局唯一、`size` / `size_group`、状态、时间。变更:

| 字段 | 说明 |
|---|---|
| `spu_id` | 新增外键,必填 |
| `color_variant_id` | 新增外键,必填;`UNIQUE(color_variant_id, size)` |
| `barcode` | 新增,可选 |
| `price` / `inventory` / `cost` | 新增,可空;草稿 READY 前补齐(READY 门禁校验 ACTIVE SKU 非空,渠道 spec 手填字段机制继续从这里取值) |
| `spu`(字符串)| 降级为反规范化读列,由服务层随 `spu_id` 同步;禁止作为查询权威 |
| `variant_key` | 退役(列可保留一版做过渡断言,新代码禁止读取,守卫测试点名) |
| `audience` | 退役,见 §4.2 |
| 8 个投影列 | 保留现状(单写入点),建档接口不再接收 |

## 4.5 商品事实 = 现有属性层(扩展)

**不新建 ProductFact。** v3.0 §4.5/4.6 的诉求映射:

| v3.0 诉求 | 落点 |
|---|---|
| SPU 共享事实 / 颜色事实分层 | `product_attribute_values.owner_type` = SPU / VARIANT(已存在) |
| 六个状态 | `AttributeStatus`(已存在,逐字相同) |
| 版本 | `is_current` 部分唯一索引 + `superseded_at`(已存在;比整数 version 列多解决并发唯一性) |
| source_type | `AttributeSource` 六值(已存在;MANUAL 永不被自动覆盖的合并规则沿用) |
| evidence_json | `attribute_evidence` 表(已存在,一图一字段一条;比内联 JSON 多出按图追溯与跨图合并) |
| `input_fingerprint` | **新增列**:`product_attribute_values.input_fingerprint`(该值产生/确认时的作用域指纹,见 §5.3);`facts_stale` 由它与当前指纹比较派生,不落状态 |
| 缺失原因 | **新增机制**:模型对字段返回 `missing_reason ∈ {INSUFFICIENT_EVIDENCE, NOT_VISUALLY_DETERMINABLE, AWAITING_SUPPLIER_DATA}` 时,落 evidence 不落 value;"未知"不作为确定值写入(沿用现有 CANDIDATE 语义扩展) |

## 4.6 识别运行 = ProductAttributeExtraction(扩展 + 异步化)

新增列:`spu_id`(作用域改为 SPU)、`status`(QUEUED/RUNNING/PARTIAL_SUCCESS/COMPLETED/FAILED/CANCELLED)、`input_fingerprint`、`input_asset_ids`(输入素材快照)、`requested_scope`(SHARED / 颜色列表 / ALL)、`idempotency_key`(§9.2)、`cost` 挂现有用量流水(不重复建账)。执行改为 Celery 任务(复用生成链路的租约/心跳模式),接口立即返回 run_id,支持 cancel(协作式,沿用生成任务的取消语义)。同步路径退役。

## 4.7 GenerationPlan(新表,修复 D2)

| 字段 | 说明 |
|---|---|
| `id` / `spu_id` | 主键与归属 |
| `color_variant_id` | 空 = SPU 默认方案;非空 = 该颜色覆盖 |
| `model_template_id` | 外键;创建/启用时走现有授权闸(`assert_usable`)与受众筛选(§10.5) |
| `provider` | 现有注册表取值 |
| `scene` / `pose` / `angles_json` | 场景、姿势、目标角度与每角度候选数量 |
| `budget_cap` | 可选,本方案累计费用上限;超出时新任务创建被拒(读现有花费台账) |
| `plan_fingerprint` | 由上述字段派生,进生成幂等键(§9.3) |
| `status` / `row_version` / 时间 | — |

约束:`UNIQUE(spu_id, COALESCE(color_variant_id,''))`——每层当前生效一份;修改即改 fingerprint。

## 4.8 MediaAsset(增列与归属改造)

沿用现有表与状态枚举(PENDING/READY/QUARANTINED/FAILED/DELETED)。变更:

| 变更 | 说明 |
|---|---|
| `spu_id` 外键 | 新增,必填 |
| `color_variant_id` 外键 | 新增,可空(NULL = SPU 通用素材);替代自由文本 `variant_hint` 作为权威,`variant_hint` 保留为识别建议位(人工确认后写外键) |
| `product_id` | 改可空:仅 SKU 专属资料(尺码表个别场景)填写,且必须与 spu/颜色一致(CHECK) |
| 去重键 | 改为 `UNIQUE(spu_id, COALESCE(color_variant_id,''), sha256)`:同图上传到两个颜色 → 命中提示,要求人工确认后才各自成行(§11 场景 1) |
| `generation_task_id` / `generation_candidate_id` | **新增溯源列**。候选图落盘注册素材时由生成链路写入;这是关闭三条已知缺口的共同前置:识别误读 AI 图的成本洞、A45 §17-2 图片模特受众核对、"不指定模特"绕过授权闸 |
| `evidence_class` | 新增存储列,但**单写入点派生**,规则见下;库级 CHECK:`source='AI_GENERATED' OR generation_task_id IS NOT NULL → evidence_class <> 'PRODUCT_EVIDENCE'`;AST 守卫测试同投影列模式 |
| `source` | 沿用现值并补齐 v3.0 枚举缺口(MANUAL_UPLOAD / SUPPLIER_FEED / IMPORTED_URL / AI_GENERATED / PLATFORM_SYNC) |
| `role` | 沿用;补 SIZE_CHART / PACKAGING / LABEL 取值;`role_source` 机制沿用(模型定的角色不用于门禁,见 §6.2) |

`evidence_class` 派生规则(唯一写入函数,禁止路由层/脚本直写):

```text
generation_task_id IS NOT NULL 或 source = AI_GENERATED → GENERATED_RESULT
role = MODEL_REFERENCE                                   → REFERENCE_ONLY
source = PLATFORM_SYNC                                   → CHANNEL_DERIVATIVE
source ∈ {MANUAL_UPLOAD, SUPPLIER_FEED, 可信 IMPORTED_URL} 且以上皆非 → PRODUCT_EVIDENCE
```

`lifecycle_stage` **不落列**:INPUT/CANDIDATE/APPROVED_OUTPUT/CHANNEL_OUTPUT 可由溯源列、候选关联与图片集/发布关联无歧义派生,存列只会制造第二个漂移源。

## 4.9 ListingCopy 与颜色结构化字段

SPU 级文案沿用现有表与版本机制。颜色层**不建 copy 行**:

- 确认后的标准颜色名称、图案配色、渠道颜色属性 = VARIANT 层 `CONFIRMED` 事实(注册表新增对应字段);
- 可选颜色补充关键词 = VARIANT 层字段 `color_keywords`(人工或模型建议,走同一套确认与版本机制);
- 只有渠道 spec 明确要求颜色专属文本时,才在导出映射层拼装短补充内容,不落独立版本对象。

## 4.10 Draft = ListingDraft(扩展)

新增列:`upstream_versions_json`(SPU 事实版本集、各 ACTIVE 颜色事实版本集、各颜色 ImageSet 版本、Copy 版本、GenerationPlan 指纹、双作用域样品指纹)、`color_sku_image_map_json`(颜色 → SKU → 主图/附图的最终映射)。草稿不复制事实值,只存版本引用与提交快照;READY 门禁扩展见 §6.7。现有导出闸、过期提示展开机制沿用。

---

# 5. 商品事实与素材规则

## 5.1 事实抽取输入白名单(实现方式修订)

白名单条件保留 v3.0:

```text
status = READY
AND evidence_class = PRODUCT_EVIDENCE
AND source IN (MANUAL_UPLOAD, SUPPLIER_FEED, 可信 IMPORTED_URL)
AND generation_task_id IS NULL
```

修订的是**实现约束**:白名单必须收敛为一个查询助手(如 `media.evidence_assets_for(spu_id, scope)`),它是抽取输入的**唯一取数入口**;导入契约测试(沿用 `.importlinter` + 纯测试)断言抽取服务不出现对 media 层的其他查询路径。禁止读取的各类(AI 生成、候选、批准输出、渠道回抓、MODEL_REFERENCE、隔离/损坏/删除)由派生规则与 CHECK 结构性保证,而不是靠每个调用点各自记得过滤。

直接收益(实施后即可验证):属性识别不再对 AI 候选图发起付费调用——现状每张候选都会白花一次识别费用且证据一票投不上。

## 5.2 两类独立能力(现状已满足,补差异说明)

```text
extract_product_facts(original_assets)      → extractors/ 注册表(接真实后端)
validate_generated_images(candidates, facts) → evaluators/ 注册表(已存在)
```

两个注册表、两套 Schema、两条输入查询在代码库中本就分离。本期变化:评分器新增"事实一致性"维度组(§6.5),其输入是候选图 + `CONFIRMED` 事实,不查原始样品表以外的证据。

## 5.3 双作用域样品指纹(修复 D1)

指纹元素保留 v3.0(asset_id、content_hash、source、role、color_variant_id、启用态、文件版本),按稳定排序哈希。作用域拆为两个:

| 指纹 | 集合 | 供谁比较 |
|---|---|---|
| `shared_fingerprint(spu)` | 该 SPU 全部 PRODUCT_EVIDENCE + READY 素材(含各颜色与通用) | SPU 共享事实的 `input_fingerprint` |
| `variant_fingerprint(spu, variant)` | 该颜色的 PRODUCT_EVIDENCE + READY 素材子集 | 该颜色事实的 `input_fingerprint` |

后果:给颜色 A 补传样品 → 共享事实与颜色 A 事实 stale,**颜色 B 事实与图片不受影响**;修改素材颜色归属 → 原颜色、新颜色、共享三个指纹同时变化。改变/不改变指纹的操作清单沿用 v3.0(备注、排序、UI 标签不变指纹)。

---

# 6. 一体化新品生产流程

## 6.1 步骤 1:创建 SPU、颜色变体和 SKU

必填:SPU(编码、内部名称、**受众**、基础类目);颜色(variant_code、临时名称、是否本次在售);SKU(编码、所属颜色、尺码)。可选与"不得要求填写"清单沿用 v3.0。页面要求沿用(一次建 SPU、批量加颜色、尺码模板批量建 SKU、创建后直入样品步骤)。

实现落点:改造现有 `ProductFormModal` / 建档接口为三段式;创建接口不再接收投影列与受众之外的视觉字段;受众控件复用 A45 的 `AudienceConfirmCard` 联动规则(受众切换清理越界品类)。

## 6.2 步骤 2:按 SPU 和颜色上传样品

沿用 v3.0 全部规则(归属必确认、最低完整度、模特参考图单独归类、未归属素材不得进入下游)。两处按现状收紧:

- 门禁读取的 `role` 必须是人工确认口径:`role_source ∈ {HUMAN, CONFIRMED}`。模型建议的角色只用于预填与提示,不satisfy"至少一张 PRODUCT_FRONT/FLAT_LAY"的完整度判定(沿用"模型定的角色不能直接用于主图位"的既有规矩,扩展到流程门禁);
- 重复检测按新去重键提示,跨颜色重复必须人工确认(§4.8)。

## 6.3 步骤 3:异步识别并确认商品事实

```text
POST /api/spus/{spu_id}/attribute-extraction-runs
→ 返回 run_id(QUEUED)
→ Celery 读取输入素材快照(白名单入口)
→ 多模态模型输出固定 JSON Schema
→ 证据落 attribute_evidence,按素材颜色归组合并:
   共享字段跨全部证据合并;颜色字段只在该颜色证据子集内合并
→ 生成 SUGGESTED / CANDIDATE / CONFLICT(经校准与置信度分解,沿用现有 merge/decision)
→ 运营逐字段确认(SPU 共享一次,颜色分别)
```

一期识别字段、禁止字段清单沿用 v3.0。补充三条硬规则:

- 模型对禁止字段给出确定值 → 过滤并计入"不可见属性编造率"指标,不入确认队列(§11 场景 6);
- 未校准的(字段 × 模型 × Prompt)组合一律不自动确认——沿用现有 fail-closed,一期结果是**全量人工确认**,自动确认是校准积累后的产物,不是本期承诺;
- 模型不确定时返回 `missing_reason`,不猜测(§4.5)。

完成条件(按双指纹修订):所有必填共享事实 `CONFIRMED` 且其 `input_fingerprint == shared_fingerprint`;每个 ACTIVE 颜色必填颜色事实 `CONFIRMED` 且指纹匹配该颜色当前指纹;无未处理的关键 `CONFLICT`。

## 6.4 步骤 4:按颜色选择模特和生成方案

方案对象 = `GenerationPlan`(§4.7):SPU 默认一份,颜色可覆盖。创建任务前校验(全部映射到既有机制):颜色事实已确认(§6.3 完成条件的颜色子集)、颜色样品完整(§6.2)、ModelTemplate 可用(现有授权闸 + 受众筛选 §10.5 + 生成前阻断 §12.4)、预算通过(方案 budget_cap + 现有花费台账)、幂等键无活动任务(§9.3)。

保留 v3.0 约束:不建授权平台,但自由上传模特图不得绕过 ModelTemplate 校验——本期借 `generation_task_id` 溯源列落地后,收掉"MODEL_REFERENCE 素材直接 return"的已知绕行缝(STATUS 已知缺口第 2 条)。

## 6.5 步骤 5:生成、核对并批准图片

后台流程沿用现有链路,新增一环:

```text
创建 GenerationTask(绑定 color_variant_id + plan + 双指纹)
→ Provider 调用 → 下载候选 → 文件校验(现有)
→ 视觉评分(现有 11 维)+ 新增"事实一致性"维度组
→ 分档 → 自动重生或人工审核(现有)
→ 形成颜色专属 ImageSet
```

事实一致性检查清单沿用 v3.0(主辅色、图案、肩带、领口、背部、覆盖度、结构改写、不对称、融合缺陷),实现为评分器 Schema 的新维度组:逐项对照 `CONFIRMED` 事实,命中即 hard_fail 阻断自动批准(接现有一票否决机制),受众相关项复用 review_focus 下发。

颜色图片规则(即 BLOCK-02 挂起所缺的业务决定,本期定死):

- 颜色主图与颜色附图只能来自 `variant_id = 该颜色` 的条目;
- SPU 通用图(`variant_id IS NULL`)只能出现在附图位,且必须由运营在图片集编辑器中明确标记"通用",默认不混入;
- 不得回退使用其他颜色的图片,缺图就是缺图(BLOCKED);
- 必要角度按 GenerationPlan 的 `angles_json` 配置验收,不是只数总张数;
- 重生、换图、改序派生新图片集版本(现有机制)。

UI 落点:图片集编辑器补 variant 绑定入口(候选入集时默认继承生成任务的 `color_variant_id`,人工只处理例外),`variant_coverage` 后端计算结果开始参与门禁。

## 6.6 步骤 6:生成并批准 Listing

输入白名单、SPU 级产出、颜色结构化字段、"尺码不重复生成文案"、声明可追溯,全部沿用 v3.0;存储落点按 §4.9。校验链沿用现有禁词/声明/长度 + 批准前强制重校验。

## 6.7 步骤 7:形成商品草稿

草稿预览表格与 READY 条件沿用 v3.0,补两条实现口径:

- "所有上游版本和指纹仍然有效" = `upstream_versions_json` 逐项与当前版本比较派生,不落 STALE 状态列(现有草稿过期机制的推广);
- 范围口径:只检查 `sellable_status=ACTIVE` 的颜色与其 SKU;PLANNED/PAUSED/DISABLED 不阻塞、不参与映射完整性判定。

---

# 7. 新品生产向导

## 7.1 向导定位(不变)

统一入口 + 聚合读模型,不建第二套可手工修改的状态机。底层对象:Spu、ColorVariant、Sku(products)、MediaAsset、ProductAttributeExtraction、属性值、GenerationPlan、GenerationTask、ImageSet、ListingCopy、ListingDraft。

## 7.2 七步向导(不变)

| 步骤 | 核心内容 | 唯一主要动作 |
|---|---|---|
| 1 建档 | SPU、颜色、SKU | 创建新品 |
| 2 样品 | 按颜色上传和检查素材 | 补齐样品 |
| 3 商品事实 | 识别运行、共享/颜色事实、冲突 | 确认事实 |
| 4 生成方案 | 模特、Provider、角度、预算 | 确认方案 |
| 5 图片 | 任务、候选、评分、审核、图片集 | 完成下一个颜色 |
| 6 Listing | 文案、颜色字段、来源和风险 | 审核 Listing |
| 7 草稿 | 颜色、SKU、图片和业务字段映射 | 完成或导出草稿 |

## 7.3 派生状态(实现方式修订)

十个状态清单沿用 v3.0。实现:`flow.py` 增维而非新建——

- SPU 聚合态 = 按 STEP_ORDER 扩展(建档、样品、事实、方案、图片、文案、草稿)找第一个未完成步骤;
- 颜色子态 = 图片/事实两步内按颜色展开,"完成下一个颜色"由子态序推出;
- 三级问题(BLOCKING/NEEDS_CONFIRM/REMINDER)、STALE 与 BLOCKED 的语义区分、"唯一下一步"判定全部沿用既有纯函数模式并保持穷举测试;
- `IMAGE_PROCESSING` 等运行中态由任务状态聚合派生,服务层只负责把库行翻译成判定输入。

## 7.4 页面固定信息、7.5 交互约束

沿用 v3.0 全部条目(不提供手工标记完成、单一主按钮、详情页往返、刷新重派生、防双击、上游变化前展示失效影响)。

---

# 8. 版本与失效规则

## 8.1 失效矩阵(扩展现有封闭集合)

在 `stale_matrix` 上扩展:变更源新增 6 项、目标新增 FACTS 列,并把素材类变更按颜色作用域细化。扩展后矩阵(新增与修订行标注作用域):

| 上游变化 | 商品事实 | 图片结果 | Listing | 草稿 |
|---|---|---|---|---|
| 替换/删除/隔离颜色 A 原始样品 | 共享事实 + 颜色 A 事实 stale;**颜色 B 不受影响** | 颜色 A 图片集过期 | 颜色 A 结构化字段过期;SPU 文案视共享事实是否受影响 | 过期 |
| 修改素材颜色归属(A→B) | 共享 + A + B 三个指纹变化,对应事实 stale | A、B 两色图片结果过期 | 两色字段过期 | 过期 |
| 修改确认的共享结构事实 | 新版本 | 全部颜色重新一致性检查 | SPU 文案过期 | 过期 |
| 修改颜色事实 | 新颜色事实版本 | 对应颜色重新一致性检查 | 对应颜色字段过期 | 过期 |
| 更换生成方案(plan_fingerprint 变化) | 不变 | 对应作用域图片集过期 | 通常不变 | 图片映射过期 |
| 替换批准图片集 | 不变 | 新版本生效 | 通常不变 | 图片映射过期 |
| 修改人工材质/卖点 | 不变 | 通常不变 | SPU 文案过期 | 过期 |
| 修改价格或库存 | 不变 | 不变 | 不变 | 过期 |
| 新增 ACTIVE 颜色 | 共享不变,新颜色事实待确认 | 新颜色待生成 | 新颜色字段待确认 | 过期 |
| 停用颜色 | 共享不变 | 该颜色不再参与 READY | 该颜色字段不再参与 | 重新计算 |
| 导出模板/文案规则变更(现有行) | 不变 | 按现有矩阵 | 按现有矩阵 | 按现有矩阵 |

实现约束:沿用"矩阵即封闭集合、每格 mechanism 点名负责函数、逐格测试"的既有方法;改行为先改矩阵,两边不一致由测试指出。

## 8.2 版本规则(沿用 v3.0,机制对齐)

属性值:is_current 版本链 + input_fingerprint;识别运行:输入指纹与素材快照;GenerationTask:样品指纹 + 事实版本指纹 + plan 指纹;ImageSet:现有版本机制 + plan 版本引用;ListingCopy:现有版本 + 事实版本集引用;Draft:upstream_versions_json 全量快照。下游过期一律派生比较,不写 STALE 列;历史结果不自动删除。

---

# 9. 幂等、并发与恢复

## 9.1 创建新品

`POST /api/spus` 接受 `Idempotency-Key`:同 key 同指纹返回同一 SPU;同 key 不同指纹 409;`spu_code` 唯一约束是最终裁决;并发均返回同一对象。SKU 批量创建沿用现有"重复记为跳过/409"语义,整批携带同一请求指纹。

## 9.2 属性识别

幂等键 = `spu_id + requested_scope 相关指纹(共享/颜色) + model_version + prompt_version + requested_fields`。同意图:QUEUED/RUNNING 返回原 run;COMPLETED 可复用;仅输入、模型、Prompt 或字段范围变化才建新 run。落 `idempotency_key` 唯一约束,数据库裁决。

## 9.3 图片生成

幂等键 = `color_variant_id + variant_fingerprint + confirmed_fact_version(该颜色+共享) + model_template_id + provider + plan_fingerprint`。沿用现有"幂等键由输入派生 + 网络重发/双击/worker 重投不产生第二次外部生成"的机制,仅更换键的组成。

## 9.4 Listing 生成

幂等键 = `spu_id + confirmed_fact_versions + 人工业务事实版本 + channel/site/locale + prompt_version`。尺码 SKU 不单独触发。

## 9.5 草稿

相同上游版本组合只有一个当前草稿版本;上游变化建新版本,旧草稿保留但不可再 READY。沿用现有草稿与导出闸,新增 upstream_versions_json 参与判定。

---

# 10. 权限、审计与安全

## 10.1 本期权限边界(落地口径明确)

沿用现状:无账号体系,操作者 = `X-Actor` 头,管理动作 = `ADMIN_TOKEN`。三个职责的一期落法:

| 职责 | 落法 |
|---|---|
| Operator | 现有 operator 口径:建档、上传、启动识别与生成、编辑建议值 |
| Reviewer | **不建新角色**。以流程限制实现:确认/批准接口保持独立端点 + 独立审计动作码;界面上确认动作与编辑动作分离。完整 RBAC 明确不在本期 |
| Admin | 现有 `require_admin`:模型、属性字典、Provider、ModelTemplate、高风险恢复 |

## 10.2 审计(沿用现有 audit_log + 各域流水)

v3.0 清单全部保留,逐项映射到现有审计动作码体系;新增对象(Spu/ColorVariant/GenerationPlan/识别 run 异步态)补动作码即可,不另建审计通道。

## 10.3 安全(全部为现有机制的沿用声明)

模型输入不含密钥(llm/redaction + core/redaction);日志脱敏(现有);外部 URL 下载防 SSRF(core/net_safety);上传类型/大小/解码校验(upload_validation + image_probe,20MB 内存上限现状不变);AI 图不得伪装原始样品——机制 = 溯源列 + evidence_class CHECK + 同 SPU sha256 撞车检测(候选注册的素材行已带溯源,同哈希再走人工上传通道即命中去重键并暴露溯源冲突,进隔离待人工);ModelTemplate 不可用禁止创建任务(现有授权闸)。

---

# 11. 异常与边界场景

v3.0 十四个场景全部保留,预期行为不变。新增三行:

| 场景 | 预期行为 |
|---|---|
| 同一 run 内部分颜色的图全部失败 | run = PARTIAL_SUCCESS;成功颜色的建议正常入队,失败颜色明确列出,可单独重试该颜色作用域 |
| AI 候选文件被当作"原始样品"重新上传 | 同 SPU sha256 命中已带溯源的素材行 → 拒绝并提示来源冲突,落隔离待人工放行 |
| 未校准字段大量涌入 | 按现有 CANDIDATE 语义留证据不采信、不进确认队列;确认队列只出现 SUGGESTED/CONFLICT,避免运营被低质建议淹没 |

---

# 12. API 需求

标注每条是**新增**还是**改造现有端点**,避免平行双轨:

## 12.1 SPU、颜色和 SKU

```text
POST   /api/spus                                   新增(带 Idempotency-Key)
GET    /api/spus/{spu_id}                          新增
PATCH  /api/spus/{spu_id}                          新增(row_version 乐观锁)
POST   /api/spus/{spu_id}/color-variants           新增(支持批量)
PATCH  /api/color-variants/{variant_id}            新增
POST   /api/color-variants/{variant_id}/skus:batch-create   新增(尺码模板展开)
PATCH  /api/skus/{sku_id}                          改造现有 PATCH /api/products/{id}
```

现有 `POST /api/products`、`POST /api/products/import` 收敛为向 spus/skus 结构写入的改造版;不保留可绕过 SPU 结构的旧建档路径。

## 12.2 素材

```text
POST   /api/spus/{spu_id}/assets                   改造现有商品素材上传(强制携带归属 + source + role)
PATCH  /api/assets/{asset_id}/ownership            新增(颜色归属确认,改变指纹)
PATCH  /api/assets/{asset_id}/classification       新增(role/来源修正;evidence_class 不可直改)
DELETE /api/assets/{asset_id}                      改造(软删,触发失效)
```

## 12.3 属性识别

```text
POST /api/spus/{spu_id}/attribute-extraction-runs  改造现有 extract-attributes(异步化 + 幂等)
GET  /api/attribute-extraction-runs/{run_id}       改造现有 extractions/{id}
POST /api/attribute-extraction-runs/{run_id}/cancel 新增
GET  /api/spus/{spu_id}/facts                      改造现有属性读接口(按 owner 分层返回)
POST .../facts/{id}/confirm|reject|resolve-conflict 沿用现有确认/拒绝/冲突端点,owner 换层
```

## 12.4 生成方案与向导

```text
GET/PUT /api/spus/{spu_id}/generation-plan                     新增(SPU 默认)
GET/PUT /api/color-variants/{variant_id}/generation-plan       新增(颜色覆盖)
GET     /api/spus/{spu_id}/production-workflow                 新增聚合端点,落在 workbench 域
```

聚合返回沿用 v3.0 清单(当前步骤、下一步、颜色摘要、各状态、阻塞、费用、版本与过期原因);判定复用 flow 增维结果,服务层不自建第二份逻辑。

---

# 13. 分阶段实施(重排)

## 阶段 P0:基础设施门禁(前置,不依赖任何新功能)

v3.0 把这些放进 §14.3"人工测试准入",但它们与新功能无依赖关系,后置等于让每个阶段都建在未验证的地基上。逐条挂钩现有追踪项:

交付与验收:

- 真库 pytest 全量跑通:含 batch12-3/12-4/12-5 三批用例——12-4 恢复链路 6 条(REG-03 影子写注入已修正)+ 12-5 计费幂等/租约协议/接管 fencing 7 条,合计 13 条**已写、均未执行**;STATUS 明示"真库测试一次都没跑过,batch12-3 不能关闭"的口径仍然成立;
- Alembic 升降级(0001→0034→回退)在真 PostgreSQL 验证;注意迁移 0034 在库里已有重复 submit 计费流水时会**拒绝升级并列出重复项**——那要求先人工对账,是刻意行为不是缺陷;
- 前端 typecheck / Vitest / 正式构建 / Docker build 全绿(STATUS"仍未执行"清单清零);
- 关闭 R-04(候选文件丢失后重试恒失败且错误码不指根因)与 R-05(retry_task 在 CREATED 下的非法边);
- 重复扣费残窗验证:`BILLED_RESULT_UNKNOWN` 闸(MAX_BILLED_EXECUTIONS=2)与 `tools/resolve_billed_unknown.py` 解除通道在真库演练;"worker 死在付费调用与写回执之间"场景有双 session 用例;生成侧的费用重复记账(batch12-5 / NEW-01)**代码已修**,真库断言随上条 12-5 用例一并执行;
- 租约 fencing 与"租约过期但 worker 活着":**生成阶段租约**侧已在 batch12-6 落码(心跳/续期/易主停写/回收双信号,见 §0.4),P0 剩余动作是把已写的真库用例跑绿;**批次条目租约**(`_apply_outcome` 不校验持有者,STATUS 已知限制点名的那条)未在 batch12-6 范围内,其真库双 session 场景仍欠,保留为独立待办。

## 阶段 1:身份规范化

交付:`spus` / `color_variants` 表;products 增 spu_id / color_variant_id / barcode / price / inventory / cost;受众迁到 SPU 且必填,删除 NULL 受众兼容矩阵;属性 owner_id 切 UUID,命名空间 hack 与 variant_key 退役;三步建档 UI 与尺码模板;测试数据与 Fixture 重置为新结构。

验收:可构造三颜色九 SKU 的 SPU;不填视觉属性即可建档;不存在依赖显示名称或字符串 spu 推断归属的接口(守卫测试);受众必填且规则包派生正常。

## 阶段 2:素材归属与证据分层

交付:media_assets 归属外键与新去重键;`generation_task_id` / `generation_candidate_id` 溯源列(候选落盘写入);evidence_class 单点派生 + CHECK + 守卫;白名单查询助手成为唯一取数入口;按颜色上传 UI 与完整度检查;role 门禁按人工确认口径。

验收:AC-03/AC-04 通过;**识别输入不含任何 AI 图**(付费调用清单可证);§17-2 图片模特受众核对接线;"不指定模特"绕行缝关闭(**当前未满足**:`MODEL_REFERENCE` 分支仍直接返回,见 STATUS 已知限制与 AC-VERIFICATION §3);跨颜色重复图需人工确认。

## 阶段 3:真实多模态识别(可与阶段 1/2 并行开发,放量依赖阶段 2 白名单)

交付:真实 vision 抽取器接入 `extractors` 注册表(复用 llm/ 传输层与 images 层,fail-closed 语义沿用);识别 run 异步化 + 状态机 + cancel + 幂等;固定 JSON Schema 与 missing_reason;不可见字段过滤;双作用域指纹计算与 facts_stale 派生;部分失败(PARTIAL_SUCCESS)与按颜色重试。

验收:20 件单颜色、5 件多颜色真实样品完成识别(样照来源见 §14.2 依赖);空响应/非法 JSON/部分失败/429/500 均无半截数据;未确认事实不能进 Listing;双击不产生第二个 run;**传 A 色图不 stale B 色事实**(AC-21)。

## 阶段 4:多颜色图片生产

交付:GenerationPlan 实体与 UI;任务绑定 color_variant_id 与双指纹;评分器"事实一致性"维度组接一票否决;图片集 variant 绑定入口 + §6.5 混排规则 + variant_coverage 参与门禁;图片版本失效接扩展矩阵。

验收:AC-08/AC-09/AC-12/AC-13(图片部分);红色 SKU 不会使用黑色主图;新增颜色只新增该颜色任务;更换样品只使对应作用域图片过期。

## 阶段 5:Listing 与草稿

交付:颜色结构化字段注册表项与确认流;来源追溯;color_sku_image_map 与 upstream_versions 快照;READY 门禁扩展;导出预览。

验收:AC-10/AC-11/AC-18/AC-19;文案只用确认事实;S/M/L 不重复生成;只检查 ACTIVE 颜色。

## 阶段 6:一体化向导

交付:flow 增维(七步 + 颜色子态);聚合工作流 API;七步向导 UI;阻塞与费用展示;刷新恢复;上游变化影响提示(修改前展示将失效对象)。

验收:AC-01/AC-05/AC-14/AC-15/AC-16/AC-17;普通运营不离开向导完成主流程;高级异常跳详情并正确返回;不允许手工伪造完成;双击与重发不建重复对象。

---

# 14. 总体验收标准

## 14.1 业务用例

AC-01 ~ AC-20 原计划沿用 v3.0 原文，但仓库工作树与 Git 全历史均不存在
v3.0 文档。为避免阶段 5 继续对着不可得的判据开发，以下四条按 §4.9、§4.10、
§6.7 与本阶段三句散文验收重述为**阶段 5 仓内执行版**。它们不是伪称找回的
v3.0 原文；若后续取得原文，必须逐条做差异签认，不能静默覆盖。

| 编号 | 用例 | 通过标准 |
|---|---|---|
| AC-10 | 确认事实隔离 | 文案计划、文案生成与草稿只消费 `CONFIRMED` 事实；把同一字段的 `SUGGESTED` / `REJECTED` 值改掉，不改变文案输入、草稿快照或导出预览；提示能点名字段键而不是事实版本 UUID。 |
| AC-11 | SPU/颜色粒度去重 | 同一 SPU、同一语言、同一确认事实版本集下，S/M/L 三行 SKU 只建立一个文案生成幂等单元；并发或重试不产生第二次付费生成。颜色维文案若启用，则幂等粒度为 SPU + 颜色 + 语言，仍不含尺码。 |
| AC-18 | ACTIVE 颜色 READY 合取门禁 | 只检查 `sellable_status=ACTIVE` 的颜色及其 `products.sku` 全集；PLANNED/PAUSED/DISABLED 不阻塞。READY 同时要求图片集通过 §6.5 规则且 `color_sku_image_map` 与 ACTIVE SKU 全集一致，任一侧失败都不得 READY。 |
| AC-19 | 草稿追溯、失效与预览一致 | 草稿保存可解释的上游版本引用与颜色→SKU→图片映射；ACTIVE 颜色集合、确认事实、样品、方案或图片集变化后，由 `refresh_draft` 派生 STALE 并给出对象/字段/动作；导出预览与最终导出读取同一份已存映射，不在预览时重新推断。 |

这四条的工程签认日期为 2026-08-08，签认依据是本文件上述条文与
`docs/REVIEW-STAGE5-5-1-CONCLUSION.md`。业务方对原 v3.0 文案的追认仍是外部事项，
但不再阻塞仓内自动化守卫的编写。

其余 AC-01 ~ AC-09、AC-12 ~ AC-17、AC-20 仍沿用 v3.0 原文，当前不可得。
新增两条:

| 编号 | 用例 | 通过标准 |
|---|---|---|
| AC-21 | 失效作用域隔离 | 给颜色 A 补传样品后:共享事实与 A 色事实 stale,B 色事实、B 色图片集、B 色草稿映射均不受影响 |
| AC-22 | AI 图伪装拦截 | 将某 AI 候选文件经上传通道重新提交为"原始样品"→ 被溯源冲突拦下并隔离,不进入事实识别 |

### 阶段 6 仓内执行版(A45-batch25 重述,签认日期 2026-08-09)

阶段 6 的验收是 AC-01/AC-05/AC-14~AC-17,而这六条与 AC-10/11/18/19 一样
**原文不可得**。沿用阶段 5 的处理方式:按 §13 阶段 6 那两句散文、§16 的
最终产品定义(七步管线)与 §4.9/§4.10/§6.7 重述为可执行判据。

它们同样**不是伪称找回的 v3.0 原文**;若后续取得原文,必须逐条做差异签认,
不能静默覆盖。不先做这件事就开工,就是本节上面那句「避免继续对着不可得的
判据开发」点名禁止的做法 —— 而它的代价在 A45-batch24 刚刚付过一次:
阶段 5 的 5-3 与 5-5 都"交付"了,而两者各差一半,因为没有一条可执行的
判据说得清"完成"是什么。

| 编号 | 用例 | 通过标准 |
|---|---|---|
| AC-01 | 主流程不离开向导 | 普通运营从建档到形成完整 Draft 的七步全程在向导内完成,不需要为**任何一步的常规路径**跳到详情页;跳详情页只允许发生在高级异常上(冲突处理、退回、隔离),且返回后向导停在原来那一步。 |
| AC-05 | 不允许手工伪造完成 | 任何一步的"完成"都由该步的判定层给出,不接受前端传入的完成标记;绕过前置直接调用后续动作时,服务端按同一份判定拒绝(而不是仅前端置灰)。判定层与前端读**同一个**结果。 |
| AC-14 | 七步与颜色子态 | flow 输出七步(建档 / 样品 / 事实 / 方案 / 图片 / Listing / 草稿),其中颜色级步骤同时给出**每个 ACTIVE 颜色**的子态与"哪几个颜色拦着";非 ACTIVE 颜色如实显示但不阻塞(§6.7 / AC-18 的同一条范围口径)。 |
| AC-15 | 聚合工作流 API | 向导一次请求拿到七步状态、颜色子态、阻塞项与费用预估;该响应与列表页、详情页读同一份判定结果,三处不得出现互相矛盾的状态。 |
| AC-16 | 刷新恢复 | 浏览器刷新或断线重连后,向导回到刷新前那一步与那个颜色;进行中的异步任务不因刷新丢失,也不因刷新重复提交。 |
| AC-17 | 上游变化影响提示 | 在**修改之前**展示这次修改将使哪些对象失效(对象 / 字段 / 需要执行的动作),口径取自 `stale_matrix`,不在提示处另算一份;双击与重发不建重复对象。 |

**七步的取值不是新定义的**,它就是 §16 那条管线逐行:

```
1 SETUP      创建 SPU / ColorVariant / SKU
2 MATERIAL   按颜色上传原始样品
3 ATTRIBUTE  异步识别并确认共享事实与颜色事实
4 PLAN       选择模特与生成方案
5 IMAGE_SET  按颜色生成、核对并批准图片
6 COPY       生成并批准 SPU Listing 与颜色字段
7 DRAFT      形成完整 Draft
```

七步增维**改变了完成度口径**(5 步 × 20 分 → 下表),而完成度驱动列表排序
与审阅队列。它作为批次 6-2 单独交付,分批与理由见
`docs/REVIEW-STAGE6-CONCLUSION.md`,口径变更公告见 `docs/STATUS.md` batch27 一节。

#### 两个新步骤「完成」的定义(A45-batch27 补记)

**这两条本该在写代码之前就写在这里。** 实际顺序反了:判定层先落地,
定义后补 —— 记在这里是因为下一次不该这么做,而不是因为这次没事。
补记的内容与 `flow._evaluate_setup` / `_evaluate_plan` 逐条核对过。

| 步骤 | 完成 | 未完成的两档 |
|---|---|---|
| **SETUP 建档** | 这一行 SKU 的 `spu_id` 与 `color_variant_id` 两个归属外键**都不为空**(§4.4) | 任一为空 → 待确认「这一行还没挂到 SPU/颜色上」;两者未核对(视图为空)→ 待确认「建档归属未核对」。**都不是阻断** —— 归属是数据问题,运营在 SPU 页能自己解决 |
| **PLAN 方案** | 这个颜色有一份**当前生效**(非 ARCHIVED)的生成方案,且方案里选了模特 | 没有方案 → 待办;有方案没模特 → 待确认(参数齐了也跑不出图,任务会一直排着);属性未确认时 → 阻断(§8.2:受众没确认,模特候选集是错的) |

三条边界:

1. **建档不做回落。** `variants.variant_id_for()` 的三级回落(外键 →
   variant_key → 种子)是给「要一个稳定的作用域键」用的;这一步问的是
   「归属**建好了没有**」。用回落回答的话每一行都答"建好了",
   而回落到种子的那些恰恰是没建好的那些。
2. **方案颜色级优先、SPU 级回落算完成。** 这不是新发明的回落,是
   `generation_plans` 上那条 `COALESCE(color_variant_id::text, '')`
   唯一索引本来的形状(每层当前生效一份)。回落要**说出来**:
   运营问"为什么这个颜色的图和别的颜色风格不一样"时需要知道它用的是
   SPU 那一份。
3. **两步都不给宽松默认值。** 空视图判"未核对",不判"完成" ——
   判完成的话所有存量商品的完成度当场涨 15 分、列表排序整体上移,
   而没有任何人做过任何事。由 batch26 的
   `test_the_two_new_steps_are_not_free_points` 钉着。



## 14.2 测试数据集(补来源依赖)

数量表沿用 v3.0(20 单色 / 5 多色 / 3 组结构冲突 / 10 损坏 / 10 AI 偏离 / 10 组并发 / 10 组模型异常响应)。补充说明:

- 现有样例仅 10 件女装占位图(sample-data),**不满足**"真实样品"要求;真实样照的拍摄/采购是阶段 3 验收的外部依赖,需提前安排;
- 男装真实样照另行采集,男装评分校准不阻塞本 PRD 验收(§2.2);
- AI 偏离图可由现有 Mock Provider 的失败注入与真实候选中的 C/D 档积累。

## 14.3 人工测试准入(与阶段 P0 合并)

开始真实人工测试前必须满足:阶段 P0 全部关闭(§13);测试环境按新 schema 全新初始化;AC-01~AC-22 有自动或人工测试记录;环境真实性横幅显示真实抽取器与评分器(非 SIMULATED)。原 v3.0 列出的"重复扣费、fencing、租约心跳"各条已并入阶段 P0 逐项。

**本机真环境验收记录见 [`docs/AC-VERIFICATION.md`](./AC-VERIFICATION.md)**
(2026-08-07 第一版;P0 6 项逐项结果 + AC-01~AC-22 状态表 + 复现命令)。该
文件随每次真环境复跑更新 —— §14.3 的准入条件仍以"全部满足"为准,不以
单次跑过的条目数计。

---

# 15. 运行指标

沿用 v3.0 十项指标定义(事实建议接受率、冲突率、不可见属性编造率、结构化输出成功率、首轮图片可用率、颜色错图率、单件人工活跃时长、各阶段返工、单件内容生产成本、工作流阻塞时长)。补两条口径:

- 全部指标按受众分组统计(沿用 A45 §26 的既有分组键);
- 一期只建基线不设承诺;"事实建议接受率"在校准积累前的预期形态是全人工确认下的修改率统计。

---

# 16. 最终产品定义(不变)

> 运营可以创建一个 SPU、多个颜色变体及其尺码 SKU,按颜色上传真实样品。系统只从原始商品证据异步建立可确认的商品事实;事实确认后,运营为各颜色选择模特和生成方案,生成并审核商品图片;系统基于确认事实生成 SPU 级 Listing 和颜色结构化字段,最终形成颜色、SKU、图片、文案和业务数据映射完整的商品草稿。

```text
创建 SPU / ColorVariant / SKU
→ 按颜色上传原始样品
→ 异步识别并确认共享事实与颜色事实
→ 选择模特与生成方案
→ 按颜色生成、核对并批准图片
→ 生成并批准 SPU Listing 与颜色字段
→ 形成完整 Draft
```

本期结束时,系统从"多个独立后台能力"转变为"运营可连续完成新品内容生产的一条主流程"——且这条主流程是在既有的幂等、审计、失效与审核机制**之上**编排出来的,不是与它们并行的第二套。

---

## 附录 A:v3.0 条款处置索引

| v3.0 条款 | 处置 |
|---|---|
| §0~§2 目标 | 保留,G5/G8 表述收紧 |
| §3 无迁移约束 | 修订:无数据迁移 ≠ 无代码迁移;重建改规范化 |
| §4.2~4.4 Spu/ColorVariant/Sku | 保留;Sku 落在 products 表 |
| §4.5~4.6 ProductFact | **废弃**,由现有属性层承接 + input_fingerprint |
| §4.7 MediaAsset | 保留四维诉求;evidence_class 改派生单写,lifecycle_stage 不落列;状态枚举沿用现有 |
| §4.8 AttributeExtractionRun | 改为扩展现有表 + 异步化 |
| §4.9 GenerationTask | 保留,补 GenerationPlan 依赖 |
| §4.10~4.11 Copy/Draft | 保留,落在现有表 |
| §5.1 白名单 | 保留,实现收敛为唯一取数入口 |
| §5.3 指纹 | **修订**:双作用域 |
| §6 流程 | 保留;§6.2 role 门禁收紧;§6.4 引入 GenerationPlan;§6.5 即 BLOCK-02 业务决定 |
| §7 向导 | 保留;实现 = flow.py 增维 |
| §8 矩阵 | 保留语义;实现 = 扩展现有封闭集合;素材行按颜色作用域细化 |
| §9 幂等 | 保留;键组成对齐新实体;机制沿用 |
| §10 权限/审计/安全 | 保留;Reviewer 一期为流程限制;安全条目映射到现有机制 |
| §11 边界 | 保留 + 3 场景 |
| §12 API | 保留;逐条标注新增/改造 |
| §13 阶段 | **重排**:P0 门禁前置;抽取器与规范化并行;共 P0+6 阶段 |
| §14 验收 | 保留 + AC-21/22;数据集补来源依赖;准入并入 P0 |
| §15~16 | 保留,指标补受众分组 |
