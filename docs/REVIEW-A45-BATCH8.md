# A45 第八批修复说明（第一批 4 条 + 第二批 20 条 + **五条结构性门禁**）

> 依据：`docs/REVIEW-A44-A45-MERGED.md`（合并台账修订四）
> 环境同前：无网络、无 `pydantic / sqlalchemy / fastapi / alembic / pytest`、无 `node_modules`。
> 纯逻辑回归 **1648 → 1658，失败数恒为 2**；`verify_imports` 310 文件全通；`verify_delivery` 13/13；
> 前端 80 个 `.ts/.tsx` 语法解析全过（仍**不是** `tsc`）。

## 一、修了什么

### 第一批（合入即修）

| A45 | 一句话 |
|:--:|---|
| **#1** | `regenerateFile` 补 `adminHeaders()` —— A-21 那个唯一出口原来对每个正常用户 403 |
| **#31** | 三处直读 `settings.DOWNLOAD_ALLOWED_HOSTS` 改 `provider_setting`，并**取一次值供三处共用** |
| **#4 + #22** | 删掉 `ExportTab` 的 `saveBlob` 复制件，正本 revoke 从 0ms 放宽到 60s |
| **#5** | 回执出参补 `idempotency_key`（前 16 位），前端 rowKey 换掉 |

### 第二批

`#6` prod volumes 用 `!override`（不是 `!reset`——那是清空，清完没法在同键再列）·
`#7` 安全头抽成 snippet + Dockerfile COPY，三处 include ·
`#8` `publish.py` 模块头与 STATUS 注明「当前仅 API 消费」·
`#9` 0027 owner_id 改写补 `BTRIM` ·
`#12` 本地存储写盘 fsync ·
`#13` 探针文件收进 `.health/` 并每次清扫 ·
`#14` 批量入口 `dict.fromkeys` 去重 ·
`#16` 让位分支同步 `last_polled_at` ·
`#17` `plan()` 注释订正 ·
`#19 / #20` 下载与恢复改按行 loading ·
`#21` URL 参数只动 `open` 一个键 ·
`#23` 同名消歧抽成 `distinctNameOfVariant()`，下拉与横幅共用 ·
`#24` `dragover` 仅在落点变化时 setState ·
`#25` 决策后失效预取播下的 `workbench-flow` / `image-set` ·
`#26` `ProcessedItem.step` 的触发器订正 ·
`#27` 删掉不可达的大写响应头回退 ·
`#28` 签名 TTL 600→1800s + 图片集页 `onError` 重签 ·
`#32` 两个「重生」在 dirty 时弹确认（点明会覆盖编辑 + 产生一次模型调用）·
`#33` 导出历史时间列走 `formatDateTime` ·
`#34` `WorkbenchImportPage` 改用 `App.useApp()` ·
`#35` 比对用 CSV 拆出 `export_preview` 审计动作，不再关掉驳回闸门、不再计入 `export_count` ·
`#36` 私网判定改 `ipaddress` 解析 ·
`#37` 审核决策后失效 `products` / `workbench` ·
`#38` 切换「包含已导出」不再清空手打确认

## 二、门禁二在第一次运行时就抓到了 13 处同类问题

这是本批**最有价值的一件事**，值得单独说。

A45-#31 报的是 3 处直读 `settings.DOWNLOAD_ALLOWED_HOSTS`。我按它的建议写了
「后台可改的键不许直读 `settings`」这条门禁，第一次跑就红出 **13 处新的**：

| 位置 | 键 | 后果 |
|---|---|---|
| `copy_generator._llm_from_settings` | `TEXT_MODEL_API_KEY/BASE_URL/NAME/API_STYLE` | 设置页换模型或换 Key **要重启后端才生效** |
| `copy_generator.get_generator` | `COPY_GENERATOR` | 设置页换生成器同上 |
| `batch_service._copy_fingerprint` | `COPY_GENERATOR` `TEXT_MODEL_*` | **比 #31 更要紧**，见下 |
| `batch_service._fingerprint` | `VISION_MODEL_*` | 同上 |

指纹那两处的后果比 #31 严重：执行时走覆盖层（新模型），指纹读启动期快照（旧值）
→ 指纹不变 → 幂等命中 → **旧结果被当成新配置的产物复用**。而那个函数的
docstring 写的正是「换了配置要重跑一遍最该生效的时候」。

**一条门禁的产出是原报告那一条的四倍多。** 这就是 A45 §7 想说的事。

> 门禁本身也被自己红过一次：`provider_setting("X", settings.X)` 里那个
> `settings.X` 是**兜底默认值**、是正确用法，第一版把 `environment.py` 两处
> 正确写法判红了。已加"往前 120 字符里有 `provider_setting(`就跳过"。

## 三、五条门禁

| 门禁 | 拦的类 | 来源 |
|---|---|---|
| 一 · admin 路由 ↔ 前端 `adminHeaders` 对齐 | #1 | A45 §7 |
| 二 · 后台可改的键不许直读 `settings` | #31（+13） | A45 §7 |
| 三 · prod overlay 重列 `volumes` 必须 `!override` | #6 | A45 §7 |
| 四 · 共享 helper 全仓只许一个定义 | #4 / #23 | 本轮追加 |
| 五 · 声明 `add_header` 的 location 必须 include 安全头 | #7 | 本轮追加 |

门禁五的来历值得记：b5 修 `/healthz` 时我在代码里写下「这个形状会被复制到下一个
location 上」——**而它已经被复制了，就在同一个文件里**（`/assets/` 与
`/index.html`），我没有去看。**注释拦不住复制，门禁可以。**

## 四、这一批踩的坑（三次，都是老朋友的新形态）

1. **门禁钉住坏形状，第四、五次。** `test_the_blob_url_is_not_revoked_in_the_same_tick`
   钉的是 `ExportTab.tsx` 里 `function saveBlob` 的存在——**它钉住的正是那个
   复制件**，删掉复制件（正确修法）就变红。变体下拉那条钉的是两串字面量，
   把内联逻辑抽成共用函数就变红。两条都已改成钉不变量。

2. **`_fn` 找错了类，第二次。** fsync 断言全模块搜 `save`，命中的是抽象基类。
   凡是模块里有同名方法的，一律按类定位。

3. **注释陷阱，第五次，新形态是"位置比较"。**
   `src.index("os.fsync") < src.index("os.link")` —— 而 `os.link` 在同一个函数的
   docstring 里就出现过，断言拿到的是注释的位置，判定当场反过来。已改成比 AST 行号。

4. **import 路径照抄评审文档。** #31 我把 `provider_setting` 的模块写成
   `app.core.settings_runtime`（评审文档里提到的名字），真实位置是
   `app/providers/_config.py`——**是仓库自己的 `verify_imports.py` 当场抓住的**。

## 五、这一批**没有**做的

| A45 | 为什么 |
|:--:|---|
| **#10** 崩溃重试白烧轮次配额 | 要动 `_build_request` 里 `current_round += 1` 与 attempt 创建的事务边界。**没有真库时改事务边界，是把一个已知的浪费换成一个不可验证的顺序** |
| **#11** `mint_key` 并发铸键 | 它建议的巡检（"同 SPU 同 key 异色"）**b4 已经实现**（`drift().key_label_conflicts`）。剩余的并发铸键要在创建路径上加 advisory lock，属于执行链改动，与 C-01/C-02 同批 |
| **#28 的其余页面** | 只在图片集页接了 `onError` 重签（它有现成的 `refresh()`）。素材库与商品详情页仍会裂 —— 🔁 |
| **#30** | 已在 0027 注释里点名第二个成因；修复路径（`repair_variant_owners`）本来就覆盖这批行 |

## 六、验证缺口（新增）

| 归属 | 待验项 |
|---|---|
| **D-01** | **#35 改了 `export_count` 与 `exported_at` 的写入条件**。这两列是驳回定位的回退依据，真库上要验：导一次 CSV 后 `export_count` 不变、闸门仍关着 |
| **D-01** | #14 的去重改变了 `candidates()` 的入参语义，批量计划/执行的条目数会变 |
| **D-01** | #16 多写了一列 `last_polled_at`，轮询退避的观测值会跟着变 |
| **D-02** | #9 的 `BTRIM` 与 C-13 的长度前缀切分要在**同一次** 0027 往返里验 |
| **D-10** | #6 的 `!override`、#7 的 snippet COPY、#13 的探针目录 —— 三条都只有一次真实部署能证伪 |
| **D-05 ～ D-08** | 本批动了 12 个前端文件，仍然只有语法解析 |
