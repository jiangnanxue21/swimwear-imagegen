# A43:对 A42-merged 复查意见的逐条答复

> 基线包：`swimwear-imagegen-a42-merged.zip`
> 本轮版本：**a43**
> 答复日期：2026-08-02
> 原则：**区分"这一轮真的改了代码""这一轮只改了文档口径""这一轮没做"**。
> 不把"已实现"和"已验证"混为一谈——上一版报告点名的正是这件事。

---

# 0. 先说清楚这一版**不是**什么

**a43 仍然不是"正式人工测试基线"。**

BLOCK-01(合并树缺少真库运行时证明)在本轮**没有被关闭**，也无法在本轮关闭：
整改环境没有网络、没有 PostgreSQL/Redis、`npm ci` 装不了依赖。本轮所有验证
都来自离线门禁，与上一版报告 §3.2 所描述的局限**完全相同**。

因此本文档里凡是写"已修复"的条目，含义一律是：

> 代码已经改，**且被离线门禁覆盖到了**（纯逻辑断言或 AST 静态钉），
> 但**没有**在真 PostgreSQL + Redis + 真前端构建下跑过。

真库那一步是谁也替不了的。§6 给出必须在 CI 上完成的清单。

---

# 1. 本轮实际执行的门禁

| 门禁 | 改动前 | 改动后 |
|---|---:|---:|
| 后端纯逻辑测试 | 1496 / 1498 | **1525 / 1527** |
| 交付门禁 verify_delivery | 13 / 13 | 13 / 13 |
| `app.*` 导入检查 | 296 文件 | 298 文件 |
| 样例数据检查 | 5 / 5 | 5 / 5 |
| 离线 lint(F401/UP017) | 290 文件 | 292 文件 |
| 前端源码语法解析 | 78 / 78 | 78 / 78 |
| `compileall` | 通过 | 通过 |

那 2 条失败**不是代码缺陷**，改动前后是同两条：本机缺 `pydantic` / `sqlalchemy`，
用例在 import 期就断了。这与上一版报告的判断一致。

新增 **29 条**测试，全部落在本轮改动的行为上。

**本轮仍未执行**（与上一版相同）：真库 pytest、Ruff 全规则、`lint-imports`、
Alembic 升降级、TypeScript typecheck、ESLint、Vitest、Vite build、Playwright、
Docker build。

---

# 2. 阻断项逐条答复

## A42-BLOCK-01 合并树没有完整运行时验证 —— **未关闭（无法在本轮关闭）**

代码上做了一件事：把"未重跑"这个结论从口头承诺变成**文档里的显式状态**，
并在 §6 给出必须绑定同一个 commit SHA 的 job 清单。

不做的事：不在任何文档里写"全量测试已通过"。上一版报告指出仓库里同时存在
两组互相矛盾的事实（补丁分支报 1652 全绿 vs `MERGE-A42.md` 写明未重跑），
本轮不制造第三组。

---

## A42-BLOCK-02 批次租约缺少 fencing token —— **已修复**

### 改了什么

| 位置 | 改动 |
|---|---|
| `models/batch_job.py` | 新增 `lease_token` 列 |
| `migrations/0026_batch_item_lease_token.py` | 新迁移 |
| `workbench/batch.py` | 新增纯判定 `lease_still_held()` / `should_renew_lease()`；新增 `LEASE_LOST` 错误码与 `LeaseLostError` |
| `workbench/batch_service.py` | `claim_items()` 每次领取换新令牌；新增 `renew_lease()`；`apply_outcome()` 改条件更新；`_record_stale_outcome()`；回收器与人工重试吊销令牌 |
| `frontend/src/api/batch.ts` | `LEASE_LOST` 文案 |

### 一个与建议不同的设计决定，需要复核者确认

报告建议的条件更新是：

```sql
WHERE id = :id AND status = 'RUNNING' AND lease_owner = :owner AND lease_token = :token
```

**本轮完全照做，但刻意没有加 `AND lease_until > now`。** 理由写在
`lease_still_held()` 的注释里，这里重复一次：

> 真正的交接发生在回收器把 `lease_token` 吊销的那一刻，不是时钟走过某一秒
> 的那一刻。把到期时刻写进归属判定，后果是一件"跑超了但**还没被抢走**"
> 的条目连自己的结果都落不了库——钱已经花掉，结果却被自己丢掉。

于是分工是：**时间决定谁可以来抢（`lease_expired`），令牌决定谁能写
（`lease_still_held`）。** 这也是 fencing token 与单纯超时租约的全部区别。
如果复核者认为超时后即使无人接管也不该落库，这一条要改回去——但那会引入
"钱花了、结果丢了"的新损失面，建议保留现状。

### 关于续租的覆盖范围

`renew_lease()` 在**每一件开跑之前**调用一次，这做到了两件事：

1. 把 `lease_until` 从"领取时刻 + 30 分钟"推到"开跑时刻 + 30 分钟"，
   领取与开跑之间的排队时间不再吃掉租约；
2. 提供了链路上**唯一**一个"已经知道自己出局、但还没花钱"的停止点——
   返回 False 时抛 `LeaseLostError`，落 `LEASE_LOST`，`paid_calls` 为 0。

**没有做**报告里提到的"长轮询期间周期性续租"。那需要把续租回调穿透到
Provider 的轮询循环里，涉及 FASHN adapter 的执行模型，本轮不做。
当前的缓解是：`FASHN_POLL_TIMEOUT_SECONDS = 300` 远小于
`ITEM_LEASE_SECONDS = 1800`，单次外部调用不可能吃掉一个租约周期。
`CLAIM_CHUNK` 仍然是 1，那条 assert 仍然守着这个关系。

### `paid_calls` 的处理

按报告验收标准：旧 worker 的结果**不累加** `paid_calls`、不改 attempt、
不写回执。但那笔钱是真花了的，所以 `_record_stale_outcome()` 把它写进审计
（`action=stale_outcome`，带 `paid_calls`）并打一条 warning。
业务行上的计数属于"当前有效的那一次执行"，账在审计里。

### 验收标准对照

报告要求的双 session 用例已写在 `tests/test_batch_lease_concurrency_db.py`：

- `test_the_new_owner_decides_the_final_state` —— A 租约过期 / B 接管拿新令牌 /
  A 返回成功 / A 的条件更新影响 0 行 / 最终状态由 B 决定 / `paid_calls` 不重复累加
- `test_renewal_fails_once_the_lease_has_been_handed_over` —— 续租在交接后返回 False
- `test_the_reaper_does_not_ask_whether_the_owner_is_still_alive` —— A42 记录缺口的那条，
  前半段保留（抢活照旧），后半段翻转（覆盖不再发生）

**这三条本轮没有跑过**，它们带 `requires_db`。

---

## A42-BLOCK-03 属性确认接口不验证类型、枚举、层级 —— **已修复**

### 类型与枚举

新增纯模块 `app/attributes/validation.py`。校验**挂在 `set_value()` 上，
不挂在 API 层**——那个函数自称"唯一允许写 `product_attribute_values` 的入口"，
而识别合并走的是同一个函数。只在 API 层拦等于放过机器那条路，
而模型输出恰恰是最不该被信任的那一路。

覆盖报告列出的全部条目：ENUM 归一到注册表取值、ENUM_LIST 逐项校验+去重+上限、
TEXT trim+长度、TEXT_LIST 逐项+总数、NUMBER 类型+区间+非 NaN/Inf、BOOL 严格布尔、
**dict 一律拒绝**（注册表没有为任何字段声明结构化 schema）。

三个补充说明：

- `bool` 是 `int` 的子类，单独挡掉，否则 `True` 会变成 `1` 悄悄存进 NUMBER 字段；
- `None` 放行，表示清空——把它伪装成空字符串会让空值有两种长相；
- 单值字段收到字符串形式的列表（`"RED / BLUE"`）会被拒绝。这一条钉的是
  BLOCK-11 那个真实事故。

### owner_type 分层

`owner_for(field_name, *, spu, variant_id, sku)` —— **owner_type 只从注册表取，
调用方不许指定**。签名里根本没有 `owner_type` 参数，并有一条测试钉住这件事。
人工确认与识别写入两条路径都改了。

三层 id 按报告要求落地：`SPU -> product.spu`、`VARIANT -> variant_id_for()`、
`SKU -> product.sku`。

### 存量兼容——这一条报告没提，但不做会出事

A43 之前**所有**值都写在 `(SPU, product.id)` 上。直接切口径会让已确认的属性
在界面上凭空消失，那比不分层更糟。所以 `effective_map()` 保留一条回落链：
新位置优先，读不到时回落到 `(SPU, product.id)`。同时加了一条约束——
一个字段只认它在注册表里声明的那一层，否则存量的 SPU 值会盖掉新写的变体值，
回落链会变成覆盖链。

这条回落链是**临时**的，等属性表迁移过一遍之后应当删掉。已记入 `DECISIONS.md`。

### CanonicalVariant.attrs 组装

`build_canonical()` 现在按注册表把值分回 SPU / VARIANT 两层，
`CanonicalVariant.attrs` 第一次被真正填充。渠道映射的 `_variant_attr_bucket()`
一直在读它，只是从来没有组装方填过。

`_canonical_with_skus()` 额外做了一件报告没点名、但多颜色 SPU 一定会踩的事：
**为 SPU 下的每一个变体都组装颜色层**，不只是当前这一件。原来从红色那一件
生成草稿时，蓝色行的 `variant.primary_color` 解析不到值，导出文件里蓝色那一行
的颜色列是空的——单色商品全程正常，双色商品"看起来完整"，只有一行是空的。

第一版实现被本仓库自己的门禁拦下过一次：`test_workbench_query_budget.py`
报"库读嵌在两层循环里"。已改成 `variant_attr_map()` 一条 SQL 批量查完。

### 错误响应

422 + `detail={"field_name", "reason"}` + **整批回滚**。
一次确认 3 个字段、第 2 个类型错时只写进第 1 个，页面刷新后显示的是一个
谁也没打算要的中间状态。

识别路径不同：单个字段不合契约时**跳过并记 warning**，不炸掉整次识别。
人工那条路提交者就在屏幕前，他能改；机器这条路让一个坏字段拖垮另外 7 个
正常字段，运营看到的只是"识别失败"，指不到真正的原因。

### 验收标准对照

报告列的 6 条验收，前 5 条有对应测试（`tests/pure/test_attribute_validation.py`
19 条）。第 6 条"错误输入不会产生新版本，也不会替换当前人工值"由整批回滚
保证，但**需要真库 API 测试证明**，本轮没有。

---

## A42-BLOCK-04 多颜色图片变体仍只有诊断 —— **部分修复**

**本轮完成**：属性侧的变体层（见 BLOCK-03），多变体草稿的颜色层组装。

**本轮未完成**：稳定 variant ID、图片绑定 UI、批准硬校验、通用图迁移策略。

报告对这一条的分析是准确的：真正缺的不是优先级决定（`_image_bucket()` 里
已经写死了"变体图优先、通用图兜底、绝不回退其他变体"），而是那四件事。
其中"稳定 variant ID"与本轮改的 `variant_id_for()` 是同一个函数——
它今天仍然是 `primary_color or sku`，颜色文案一改，图片标签就变 `unknown_tags`。

**这意味着 BLOCK-03 的变体分层目前挂在一个不稳定的 ID 上。**
这是本轮引入的一个已知遗留，必须在下一轮连同图片绑定一起解决，
不能只改图片侧——两边用的是同一个 `variant_id_for()`。

---

## A42-BLOCK-05 ReviewDetail 切换 ID 时状态残留 —— **已修复**

按报告建议的方案落地：以路由 `id` 为边界的统一 reset effect。
依赖数组**只有 `id`**——写成 `[id, review]` 会在每次轮询返回新对象时
把运营正在写的备注清掉。

没有用 `<ReviewDetailPage key={id} />`：`<Route element>` 那一层拿不到参数，
要拿得包一个中间组件，而组件内 reset 让"重置这件事"在组件里看得见。

代码注释里记了一件报告已经指出的事实：候选串位大概率只表现成 409，
真正会写坏数据的是**备注和标签**——一条写着"颜色不符"的驳回理由挂到
另一件商品上，事后没有任何地方能看出它是串过来的。

**行为测试未补**（见 P1-06）。

---

## A42-BLOCK-06 单件导出超时未按"写结果未知"处理 —— **已修复（前端部分）**

`readExportError()` 保留 Blob 解析，最终改用 `describeError(err, 'write')`。
新增 `isExportResultUnknown()`。`ExportTab` 的 `onError` 在 UNKNOWN 分支：
**先 `await history.refetch()`，再提示**，文案明说"已为你刷新导出历史 ——
如果新记录已经出现，不要再导一次"，不提供直接重试按钮。

**未做**：报告建议的"后端支持客户端生成的导出幂等键"。那需要新增一列 +
唯一索引 + 迁移，且验收标准要求"同一个幂等键再次请求不增加 `export_count`"——
那是一条必须真库验证的断言，本轮给不出证明。**这一条仍然半场。**

---

## A42-BLOCK-07 真实 SHEIN 与运营发布界面 —— **未完成**

真实 Transport 需要官方文档、字段模板、测试店铺凭证，本轮一样都没有。
`_TRANSPORTS` 仍然只有 `GENERIC -> SIMULATOR`。发布运营页面本轮没做。

**文档口径不变**：不把"通用发布框架对 Simulator 可用"写成"SHEIN 已可用"。

---

# 3. 中高风险项

| 编号 | 状态 |
|---|---|
| P1-01 全量扫描 | **未做**。`list_spus()` 的 total 口径问题仍在 |
| P1-02 发布清单先全量再分页 | **未做** |
| P1-03 56 个路由缺 response_model | **未做** |
| P1-04 A28 跟踪表对 BLOCK-11 过于乐观 | 本文档已给出准确口径，跟踪表待同步 |
| P1-05 图片变体文档与代码不一致 | 本文档已按代码现实重述（见 BLOCK-04） |
| P1-06 前端关键修复无行为测试 | **未做**。本轮新增的 BLOCK-05 / BLOCK-06 行为同样没有 React 测试 |

---

# 4. 本轮新引入的、需要下一位复核者注意的东西

诚实列出，不藏在正文里：

1. **变体分层挂在一个不稳定的 ID 上。** `variant_id_for()` 仍是
   `primary_color or sku`，运营改颜色文案会让属性的 `owner_id` 变化——
   旧值不会跟着走，表现是"改了个颜色名，已确认的颜色属性不见了"。
   见 BLOCK-04。**这是本轮最需要优先处理的遗留。**

2. **`effective_map()` 的回落链是临时的。** 它同时读新旧两个位置，
   多了 1~3 次查询。删除条件是属性表完成一次数据迁移。

3. **`LEASE_LOST` 是一个新错误码。** 异常中心、批次详情、成功率分母
   都会看到它。它算 FAILED 且 retryable，不算 skip——如果运营侧希望它
   不进失败率分母，需要显式决定，本轮按"它是一次真实的未执行"处理。

4. **`apply_outcome()` 之后有一次 `session.expire(row)`。** 条件 UPDATE
   绕过了 ORM，不过期的话紧接着的 `count_rows` 会按内存里的旧值算。
   代价是多一次 SELECT，落在每件之后。

5. **`_apply_outcome()`(下划线版)仍然存在但已废弃。** 它不带 WHERE，
   任何还在用它的路径等于把 BLOCK-02 重新打开。保留只为让旧调用点可见，
   下一轮应删除。

---

# 5. 建议的下一轮顺序

不改上一版报告给的顺序，只把已完成的划掉：

```
全量合并门禁            ← 仍然是第一位，本轮无法执行
→ lease fencing         ← 本轮完成（待真库验证）
→ 属性后端契约          ← 本轮完成（待真库验证）
→ 审核状态重置          ← 本轮完成（待行为测试）
→ 稳定 variant ID + 多颜色图片绑定   ← 下一轮第一件事
→ 发布运营页面
```

---

# 6. 放行前必须在 CI 上完成的清单

与上一版报告一致，一条不减。**必须绑定同一个 commit SHA**：

```text
PostgreSQL 16 + Redis 7
pytest 0 skip
ruff check app tests
lint-imports
alembic upgrade -> downgrade base -> upgrade   ← 本轮新增 0026，务必验降级
npm ci + typecheck + lint + vitest + build
Playwright
前后端 Docker build
```

任何一个 job 红，都不允许把这一版标记为人工测试基线。
