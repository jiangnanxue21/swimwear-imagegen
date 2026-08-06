# A44 第三批修复说明（A-01 ～ A-06 · A-19 · A-20 · A-25 · A-26 / B-01 代码侧）

> 依据：`REVIEW-A44-FINAL-verified-r2-1.md`（修订二）第八节的第一批剩余项。
> 环境同前：**无网络，`pydantic / sqlalchemy / fastapi / alembic / pytest` 均未安装。**
> 纯逻辑回归 **1576 → 1595，失败数恒为 2**（那 2 条是缺 `sqlalchemy` / `pydantic` 的 import 失败）。

## 一、这一批修了什么

| 编号 | 级 | 位置 | 做法 |
|---|:--:|---|---|
| **A-01** | P1 | `WorkbenchSpuPage.tsx` | 去掉硬编码 `limit: 100`，主表改服务端分页；`api/batch.ts` 的 `spus()` 参数表里**删掉** `limit`，换成 `page / page_size` |
| **A-02** | P1 | `workbench_batch.py list_spus` | `total` 改 `COUNT` over `GROUP BY spu` 子查询，与翻页无关 |
| **A-03** | P1 | 同上 | `inconsistent_spus` 抽成 `_inconsistent_spu_count()`，全量算 |
| **A-04** | P1 | `WorkbenchSpuPage.tsx` | 删掉「客户端排序对的是全量」那句注释，列头补「排序只作用于当前页」提示 |
| **A-05** | P1 | `workbench_batch.py list_spus` | 翻页下推到 SQL（`GROUP BY spu` + `OFFSET/LIMIT`），`collect()` 只对本页 SPU 跑 |
| **A-06** | P1 | 同上 | 新增 `_confirmed_attributes_bulk()`，循环内零库读 |
| **A-19** | P1 | `ImageSetTab.tsx` | `toDraft()` / `payload()` 补 `derivative_purpose` |
| **A-20** | P1 | `frontend/nginx.conf` | `client_max_body_size` 16m → **24m** |
| **A-25** | P1 | `ImageSetTab.tsx` | 行标识改 `rowKey(asset, variant)`，与后端唯一键同口径；**所有行操作改按下标**，不再按 key 过滤 |
| **A-26** | P1 | `ImageSetTab.tsx` | 每行加颜色变体下拉 + 「再绑一个颜色」复制按钮 |
| **B-01** | **P0** | `ImageSetTab.tsx` | 代码侧三块合齐：A-25 + A-26 + 批准前的知情确认 |

## 二、几个刻意的取舍

### 1. `inconsistent_spus` 没有下推成 SQL

判据是「同一 SPU 下某个公共字段出现两个不同的**显示值**」，而显示值要经过
`_display_attribute_value()`：`normalized_value` 回落 `value_json`、非字符串走
`json.dumps(ensure_ascii=False)`。SQL 里 `jsonb ->> ''` 对非字符串值给出的文本
和 Python 的 `json.dumps` **并不逐字节相同** —— 翻译一遍就是把判据写成两份，
某些属性值上界面和角标会给出两个数字（§3.4 拦的正是这个）。

所以：**取数批量化，判定仍旧走 `spu_rules.aggregate` 那一份。**
代价是两条扁平查询覆盖全量（属性行 + 商品的 id/spu/sku 三列），没有 `collect()`、
没有 ORM 关系加载。改动前是「全量商品 × 每件若干次 `collect()` 库读」，
所以这仍是数量级的下降。

一条已确认属性都没有时直接返回 0，省掉那次商品扫描。

**要再省的话**，正确方向是在 `spu_rules` 里加一个只判「有没有不一致」的快路径，
不是把判据重写成 SQL。

### 2. `limit` 保留为 deprecated 别名

`GET /workbench/spus` 仍接受 `limit`，映射到 `page_size`。
不留的话，浏览器缓存里那份旧 bundle 会从 100 行**悄悄**变成 50 行 ——
又是一个不报错的错答案，正是这一簇 bug 本身的形状。

### 3. 排序仍是客户端的

完成度 / SKU 数 / 阻断三列的 `sorter` 没有下推到 SQL，所以翻页之后
**排的是当前页**。改注释而不是改行为，是因为下推排序要给后端加 `sort` 参数
并和 `GROUP BY spu` 的口径对齐，是另一件事。

但界面**不能继续声称**它排的是全量：列头打了 `*` 并给出 Tooltip。

### 4. 搜索的语义原样保留

搜索命中的是**商品**。一个 SPU 只要有一个 SKU 的名称命中就进结果，
而该组里只装命中的那几个 SKU。这是改动前就有的行为，本次不动。

### 5. B-01 的批准仍**不硬阻断**，改成知情放行

`variant_coverage` 的 docstring 写得很清楚：改成硬阻断会让每一个多色 SPU
立刻无法批准，因为它们的图全是通用图 —— **那不是修复是停产**。

但 A-26 之后「绑不了」这个理由不成立了：入口就在每一行里。所以从
「默默放行」改成「**知情放行**」：全是通用图的多色集在批准前弹一次确认，
说明代价（每个颜色拿到的会是同一张），并给出去哪里绑。

**要不要升级成硬阻断是业务规则决定，不是代码决定** —— 它取决于存量集
要不要先补绑一轮。覆盖缺口刻意**没有**写进 `canApprove`，测试里有一条
专门钉这一点。

### 6. 变体下拉提交 key、显示名字

`resolve_ref` 第一级是 key 完全相等；颜色名那一级在两个变体撞名时返回
`None`（不猜）。所以下拉的 value 是稳定 key，label 是当前颜色名。

两个变体现在同名时（改名撞车），label 后面缀 key 的尾六位 —— 否则下拉里
会出现两个「红色」，而选错的后果要到平台侧才暴露。

### 7. 「同一张素材绑两个变体」现在前端也造得出来

这是 A-25 那个 bug 的**前提数据**：后端唯一键带 `variant_id`，这种数据合法，
但 A-26 之前只有经 API 才造得出来 —— 于是重复 key 的 bug 在界面上永远
复现不了，也就永远修不掉。「再绑一个颜色」按钮把它变成一次点击。

### 8. A-19 只做「原样带回」，不加编辑入口

界面上仍然没有编辑 `derivative_purpose` 的地方（与 A-26 的行级变体选择器同类，
归 B-01）。这一批只保证**保存不再把它清成 NULL**。

## 三、新增的门禁

### `tests/pure/test_workbench_query_budget.py` —— 第三个入口（D-13）

原来只有 `workbench.service.collect` 和 `api.publish.list_listings` 两个出发点。
补上 `api.workbench_batch.list_spus` 之后：

```
改前：深度2 session.scalars() @ list_spus > _confirmed_attributes > effective_map
改后：（空）
```

**这条拿改动前的代码验过会红**，不是假绿。

三条断言：循环内零库读（排除 `iter_flow_contexts` 那条路径——那是 `collect()`
自己的预算）、翻页落在 SQL 层、`total` 不是 `len(...)` 且 `inconsistent_spus`
不提到 `groups`。

### `tests/pure/test_a44_batch3_fixes.py`

- **口径**：轻量行（只有 attributes）与完整行的不一致判定必须一致；缺值不算第二个值；变体维度不算不一致。
- **A-06**：逐件版与批量版共用 `_display_attribute_value`；`list_spus` 里不许出现 `_confirmed_attributes`。
- **A-01/A-04**：`limit: 100` 与主表 `pagination={false}` 不许长回来；那句撒谎的注释不许回来。
- **A-19**：`ImageSetItemInput` 的字段集 ⊆ `toDraft()` 与 `payload()` 的字段集。**这条不是在测某一个字段，是在测下次加字段时会不会又漏** —— 即评审第七节给 D-11 提的那句「字段集相等」。
- **A-20**：nginx 的 `client_max_body_size` 必须**大于** `MAX_UPLOAD_SIZE_MB`。钉的是关系不是数字，改后端上限而没动 nginx 会红。
- **A-25**：`rowKey` 的归一口径必须等于 `schemas/image_set.py` 的 `item.variant_id or ""`；`i.key !== key` 这类按 key 过滤不许回来。
- **A-26**：行级 `setVariant` 存在、下拉选项来自 `coverage.required`、提交 key 而不是颜色名、撞名时能分开。
- **B-01**：批准前看 `variant_binding_missing` 并弹确认，**且覆盖缺口不许出现在 `canApprove` 里**（那是硬阻断）。

A-19 / A-20 / A-25 / A-26 / B-01 全部拿改动前的文件验过会红（六条断言六条红）。

## 四、这一批自己踩的坑（照前两批的规矩记下来）

**同一个注释陷阱，第三、四次出现，这次两个方向各一次。**

1. **假红（注释）**：`test_the_spu_page_does_not_hardcode_a_row_cap` 第一次运行就红了 ——
   红在**修复自己的注释**上：`limit: 100` 和 `pagination={false}` 被写进了
   「改之前是什么样」的说明里。已加 `_code_only()` 先剥注释再做字符串断言。

2. **假红（截取范围）**：`_fn_body()` 第一版写的是 `source.index(end)`，
   从文件开头找终止符，于是 `toDraft` 截出一段空字符串，断言报「七个字段全漏了」。
   红得越吓人越容易被下一个人当成断言写坏了直接删掉。

3. **断言范围写宽**：第一版把展开行里的 SKU 子表也算进「主表不许关分页」，
   而那个子表关分页是对的。已按 `<Table<SpuGroup>` 到 `expandable=` 的区间收窄。

4. **失败信息不可读**：B-01 那条选择器断言第一版直接 `str.index()`，
   在改动前的代码上抛的是 `ValueError: substring not found` 而不是
   一句人话。红得看不懂的断言等于没有断言 —— 已改成先断锚点存在。

结论和前两批一样：**字符串包含类断言必须先剥注释**，且每条新断言都要拿
改动前的代码跑一遍确认它会红。

## 五、验证缺口（并入 D 类）

| 归属 | 待验项 |
|---|---|
| **D-01** | `list_spus` 的 SQL **一条都没有真的执行过**。`GROUP BY spu` + `OFFSET/LIMIT` 的翻页、`COUNT` 子查询、以及 `_inconsistent_spu_count` 里 `cast(Product.id, String)` 的 `scalar_subquery()` —— 后者尤其要验：`owner_id` 存的是 `str(uuid)`，PG 的 `uuid::text` 与 Python 的 `str(UUID)` 相等是**推出来的**，不相等的表现是角标恒为 0（不报错）。 |
| **D-01** | 真库上应补：造 250 个 SPU，断言第 3 页拿得到、`total` = 250、`inconsistent_spus` 不随翻页变化。 |
| **D-01** | 真库上应补：保存一次编排后 `derivative_purpose` 不变（A-19 的直接验收）。 |
| **D-05 ～ D-08** | `WorkbenchSpuPage.tsx` / `batch.ts` / `ImageSetTab.tsx` 的类型对齐仍是手工做的，`tsc` / `vitest` / `vite build` 一项没跑。 |
| **D-10** | nginx `24m` 没有在真容器里发过一个 20MB 的文件。 |
| **D-11** | **变体绑定的 React 行为测试仍然没有**：绑定 → 保存 → 重读之后标签还在、同素材两行删一行只删一行、撞名时下拉能分开 —— 三条都只有静态断言。这是 D-11 原本就在等的那类测试。 |

## 六、第一批还剩什么

**D-01/D-05 ～ D-10 门禁（环境所限）· B-09 主密钥轮换（流程事项）。**

B-01 的**代码侧**已经合齐；它的业务侧（存量通用图集要不要补绑、要不要
升级成硬阻断）不是代码能决定的。

### 本轮做到的前端验证

`tsc` 仍然跑不了（没有 `node_modules`，无网络），但环境里有一份全局
TypeScript。用它对 `src` 下全部 **78 个 .ts/.tsx 做了语法解析，全部通过**。

**这只是语法，不是类型检查** —— 类型要 React / antd 的声明文件。
所以 D-05 只关闭了一半：结构性错误（括号、JSX 闭合、拼写成非法语法）
已经排除，类型不匹配仍然可能存在。

A-01 ～ A-06 关闭之后，评审第九节那条追加放行条件（「SPU 聚合页的『商品都在』
『性能没问题』两个结论不成立」）的前提消失 —— 但**行为一次没在真库上验过**，
所以应当替换为：

> SPU 聚合页在通过 D-01 的翻页用例之前，「商品都在」可以作为验收依据，
> 但**超过一页的场景本身仍应作为待验行为单独记录** —— 一旦发现某个 SPU
> 在任何一页都找不到，先怀疑 `GROUP BY` 与 `ORDER BY` 的口径，
> 而不是怀疑它没被导入。
