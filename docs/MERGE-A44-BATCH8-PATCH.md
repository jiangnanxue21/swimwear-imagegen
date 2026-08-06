# 合入记录:`a44-batch8-fixes.patch` → batch9 树

**合入日期:** 2026/08/03
**基线:** `swimwear-imagegen-a44-batch9-manual-test.zip` 原始解压树
**补丁:** `a44-batch8-fixes.patch`(27 个文件)
**结果:** 取 5 项、跳过 12 项、丢弃 3 项;9 个文件改动 + 3 个新文件

---

## 一、结论:这不是一次「直接打补丁」

补丁与 zip 是**同一批评审意见的两条平行修复线**。zip 已经以自己的方式
(A45-BATCH8 → BATCH9)修掉了补丁大部分内容,而且有几处修得比补丁更好。
`git apply` 会做两件坏事:把已修的东西按补丁的写法**再改一遍**(等于倒退),
以及**打红 zip 自己的门禁**——补丁想把 `saveBlob` 挪去 `utils/download.ts`,
而 zip 有一条门禁明写正本必须在 `api/batch.ts`。

所以处置方式是逐文件审,不是 apply。下面每一条的判断都对着**原始 zip**
复验过,不是照补丁的自述转录。

---

## 二、跳过的(zip 已修,12 处)

| 补丁改的 | zip 的做法 | 判断 |
| --- | --- | --- |
| `copy_generator.py` 四个文案模型配置直读 `settings` | 已走 `provider_setting("TEXT_MODEL_*", settings.TEXT_MODEL_*)` | zip 更好:显式传兜底值,不依赖函数内部回落 |
| `batch_service.py` 指纹直读 `settings.COPY_GENERATOR` / `VISION_MODEL_*` | 同上,已走 `provider_setting` | 同 |
| `generation_tasks.py` 下载白名单直读 `settings` | 已 `allowed_hosts = provider_setting("DOWNLOAD_ALLOWED_HOSTS")` 求值一次 | 一致 |
| compose 生产 overlay 的 `volumes` 合并 | 已用 `!override`(5 处) | 一致 |
| nginx 安全响应头 | `nginx-security-headers.conf` + 三处 `include` | 指令内容与补丁完全一致,只是文件名不同 |
| `api/batch.ts` 漏带 `adminHeaders()` | 已带 | 一致 |
| `saveBlob` 复制件 | 收敛到 `api/batch.ts`,门禁按**全仓**扫描 `function saveBlob(` 定义数 | **保留 zip 的**,理由见下 |
| `ProductDetailPage` / `WorkbenchBatchPage` / `WorkbenchImportPage` 的 import 改址 | 无需改 | 上一条的连带 |
| 变体撞名消歧 | `distinctNameOfVariant()` | 与补丁的 `variantDisplayName()` 是两套平行实现,保留 zip 的 |
| `test_a44_batch3` / `test_a44_batch5` / `test_copy_pipeline` 三处断言 | zip 版本已跟着 zip 的实现改过 | 见下 |

### `saveBlob` 这条为什么反过来保留 zip 的

补丁把正本放 `utils/download.ts`,zip 放 `api/batch.ts`。**位置之争没有对错,
但门禁的形状有。** zip 那条门禁扫的是全仓 `function saveBlob(` 的定义数量,
补丁那条钉的是"正本在某个特定文件里"。前者盯的是不变量(只许一个定义),
后者盯的是当前写法。按补丁改,门禁会当场变红,而且换到一条更弱的断言上。

同理,补丁对 `test_a44_batch5` 的改写把断言从 `ExportTab.tsx` 挪到
`utils/download.ts` —— 而 zip 那版已经是全仓扫描,**比补丁想改成的样子更强**。

---

## 三、合入的(补丁独有,5 项)

### 1. 发布链路前端(B-02 / A45-#8)—— 补丁的核心价值

zip 的 STATUS.md 自己承认「发布链路只有 API 没有界面」。这是补丁里唯一一件
zip 完全没做、而且**不是风格之争**的事。

| 文件 | 处置 |
| --- | --- |
| `frontend/src/api/publish.ts` | 新增,取补丁原文 |
| `frontend/src/pages/PublishPage.tsx` | 新增,取补丁原文 |
| `frontend/src/App.tsx` | 补丁 hunk 直接 apply(路由 + 侧栏「发布上架」,组名「导出」→「导出与上架」) |
| `frontend/src/components/workbench/ExportTab.tsx` | **手工移植**:只取「发布到平台」按钮 + `useNavigate` + 图标 import,不动 zip 的 `saveBlob` 设计 |
| `backend/app/api/publish.py` | 模块头补「前端在哪」纪律段 |

合入前对着 zip 后端逐项核对过,不是照搬:

- `SubmitIn` 四字段(`shop_id` / `operation` / `dry_run` / `test_batch_tag`)
- `PublishView.as_dict()` 六字段、`_listing_out` / `_attempt_out` / `_outbox_out` / `_rejection_out`
- `InventoryReport.as_dict()` 十字段(含 `to_queue` 与 `occupying` 两个不同口径)
- 页面依赖的 `brandVars.slate` / `textFaint`、`space.md` / `sm`、`fontScale.body` / `meta`,
  以及 PageHeader / ErrorNotice / BrandTag / useWriteError 的 props 签名

### 2. `fontScale.small` → `fontScale.meta`(3 处)

**这是 zip 里的一个真实 bug**,不是补丁的风格偏好。`theme.ts` 只有
`meta / body / strong / title / metric / metricLg`,`small` 这个键早已删掉。
命中处在 `ReviewDetailPage.tsx`(2)与 `ReviewQueuePage.tsx`(1),
运行期是 `fontSize: undefined` —— 样式静默失效,不报错。

它能活下来是因为 `tsc` 从没在这个仓库跑过(见 D 类缺口)。

### 3. `frontend/Dockerfile` 加 `RUN nginx -t`

按 zip 的文件名改写过(zip 用 `nginx-security-headers.conf`,补丁用
`security-headers.conf`),位置放在两条 `COPY` 之后 —— 它检查的正是两份文件
合起来的结果。少了它,配置写错的表现是容器起来了、健康检查一直不过。

### 4. `docs/DEPLOYMENT.md` 补 Compose ≥ 2.24.4 前置要求

zip 用了 `!override` 却没写版本要求。低于该版本 compose 直接拒绝启动,
而正确处置是升级、**不是**删掉 `!override` —— 删掉会让基座的
`./backend:/app` 一路活到生产,其中一种后果(部署机上有源码目录)是
**服务能正常起来**,跑的却是未经构建的工作区代码。

### 5. 新门禁 `backend/tests/pure/test_publish_frontend_entry.py`(2 条)

补丁原本那份 592 行的 `test_a44_batch8_fixes.py` 与 zip 的
`test_a45_batch8_fixes.py` 五条门禁重复,且按补丁世界观写(断言
`variantDisplayName`、`utils/download.ts`),整份合入必红。只抽了发布相关两条,
**独立成文件**:

- `test_publish_endpoints_have_a_frontend_entry` —— 分三段断言
  (有客户端 / 有页面 / **路由挂上了**),因为「页面写完了但没挂路由」
  是这类返工最常见的形态
- `test_publish_page_does_not_second_guess_the_backend` —— 硬规则 4,
  按钮从 `allowed_actions` 渲染、文案用 `display_status_label`,
  页面里不许有第二张状态表

放独立文件而非并入 `test_a45_batch8_fixes.py`:后者盯的是「唯一入口被绕过」
(有两条路,走错一条),这两条盯的是「一条路都没有」。两类失效方式不同。

---

## 四、丢弃的(3 项)

| 丢弃 | 理由 |
| --- | --- |
| `docs/REVIEW-A44-BATCH8.md`(203 行) | 描述与 zip 实际状态矛盾(按补丁自己的修法写的)。原样收录会在文档里留下第二份互相打架的"当前状态" |
| `test_a44_batch8_fixes.py` 其余 3 条门禁 | 与 zip 的五条重复,且钉的是补丁的实现 |
| `frontend/security-headers.conf` / `utils/download.ts` | zip 已有等价物,文件名/位置不同 |

### 一处刻意的遗漏

`RUN nginx -t` **没有配门禁**。zip 自己的门禁哲学写在
`test_a45_batch8_fixes.py` 顶部:

> 我钉的是这段代码现在长什么样,还是它必须保持什么性质?

断言「Dockerfile 里有 `nginx -t` 这个字符串」属于前者。而且这一行本身就是
构建期自检,删掉它只是少一道检查,不会产生静默错误。

---

## 五、历史评审文档没有改

`docs/REVIEW-A44-FINAL-verified-r3.md`(B-02 标 ⬜)、
`docs/REVIEW-A44-A45-MERGED.md`(#8)、`REVIEW-A44-BATCH7.md`(A-31)
里关于 B-02 的记述**保持原样**。它们是**某一次评审当时的记录**,不是当前状态;
改了就没人知道那次评审实际看到的是什么。当前状态在 `docs/STATUS.md`,
已同步更新(能力表、已知限制表、B-02 章节、A-31 那条的前置条件)。

---

## 六、验证到哪一步

```text
纯逻辑套件   基线 1660/1661  →  合入后 1662/1663
             总数 +2(新门禁两条),失败数不变、失败项相同
唯一失败     test_ci_smokes_the_invariants_under_optimised_mode
             —— 交付包不含 .github/,与本次合入无关(基线同样失败)
verify_imports.py     OK,312 个文件 app.* import 全部解析得通
syntax-check.mjs      81/81 解析通过(含新增的 publish.ts 与 PublishPage.tsx)
verify_delivery.py    9/13 —— 与基线**逐条相同**,没有新增失败
```

裸环境下另有两条因缺 `sqlalchemy` / `pydantic` 而失败,装上依赖后消失;
上表是装了依赖之后的数字。

### `verify_delivery` 那 4 条本来就红,这次**没有顺手修**

交付包不含 `.gitignore`,也不含 `.github/workflows/`。四条失败全部由这两处派生
(缺 .gitignore 一条、缺 CI 三条),基线一模一样。

不修的理由:它们不在这份补丁的范围里,而且都是**需要决策**的东西 ——
`.gitignore` 的内容与 `tools/pack.sh` 的 `EXCLUDES` 要保持同源(pack.sh 注释里
写着有测试盯着两者不许分叉),随手补一份对不上的会制造新的假信号;
CI 更是 P0 级别的独立任务。留在这里,不要让它们混进合入记录里被当成"已处理"。

### 没有验证的

**发布页没有跑过浏览器。** 它和其余前端一样落在 D 类缺口里:没有 tsc、
没有 vitest、没有真的点开过。上面两条门禁验的是"入口存在、且不自建第二份
判定",验不到"点下去这一步真的发生了"。

`PublishPage.tsx` 是这次唯一一份**近千行、未经任何类型检查**的新代码,
所以人工评测时它应当排在最前面。第一件该做的事是
`cd frontend && npm ci && npm run build` —— `tsc -b` 会在这里给出
本仓至今没有过的类型证据。
