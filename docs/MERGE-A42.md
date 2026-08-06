# A42 合入记录:四个补丁并成一棵树

> ## ⚠️ A43 追加:这棵树**仍然没有**跑过真库全量门禁
>
> 本文件原有结论「合并树未重跑,必须带真库重测」**在 a43 之后依然成立**,
> 而且 a43 又动了几处高风险区域(批次落库改成条件更新、属性写入改了 owner 口径、
> 新增迁移 0026)。因此:
>
> - 各补丁分支曾报告的「真 PostgreSQL 下 1652 条全绿」**不适用于这棵树**;
> - a43 的 1525/1527 是**纯逻辑测试**,不含数据库、不含前端类型与构建;
> - 放行清单见 `docs/REVIEW-A43-RESPONSE.md` §6,**必须绑定同一个 commit SHA**;
> - 新增的 0026 迁移**务必验降级**(`upgrade -> downgrade base -> upgrade`)。
>
> 在那份清单全绿之前,任何文档、交接或对外说明都不得出现「全量测试已通过」。

**合入日期:** 2026/08/02
**基线:** `swimwear-imagegen-a41` 原始解压树
**结果:** 52 个文件改动,2808 增 / 144 删

本文件记录的是**合入这件事本身**:哪些补丁进来了、冲突怎么判的、哪些结论
因为合入而作废。各补丁自己的技术叙述在 `HANDOVER.md` 与 `docs/DECISIONS.md`,
这里不重复。

---

## 一、合入了什么,以及一个被丢弃的

| 补丁 | 处置 | 理由 |
|---|---|---|
| `a41-fixes-1.patch` | ✅ 合入 | 走读修复 + 出参时间戳收敛 + 前端两处 |
| `a42-review-fixes.patch`(final) | ✅ 合入 | 外部评审两条阻断项 + 密钥泄漏 + 两个生产缺陷 |
| `a42-task19-transaction-boundaries.patch` | ✅ 合入 | 任务 19 后半 + 范围口径改为「服装」 |
| `a41-fixes.patch` | ❌ 丢弃 | 被 `a41-fixes-1` **完全取代** |
| `files-8.zip` 里的 `a42-review-fixes.patch` | ❌ 丢弃 | 是 final 的**早期草稿**(21 文件 vs 28) |

### 为什么 `a41-fixes.patch` 可以整份丢掉

逐 hunk 比对过(剥掉时间戳后按文件比字节):

```text
字节完全相同的 hunk   10 个   CLAUDE.md / workbench.py / workbench_batch.py /
                              clock.py / search.py / product_service.py /
                              batch.py / batch_service.py / batch.ts /
                              WorkbenchListPage.tsx
被扩写的               3 个   test_a41_fixes.py / DECISIONS.md / STATUS.md
新增的源文件           6 个   listings/export_parity.py, listings/export_writer.py,
                              services/cleanup_service.py, services/poll_service.py,
                              scripts/cleanup_test_listings.py, scripts/export_parity.py
```

`a41-fixes-1` 是严格超集,没有任何回退。

### 为什么 `files-8.zip` 里那份 a42 可以整份丢掉

它缺的正是两个生产缺陷修复:`reviews.py` 的 `_single_review_out`
与 `conftest.py` 的 savepoint。final 是它的超集。

---

## 二、一个重要的事实:四个补丁是**兄弟**,不是串行的

`a42-应用说明.md` 写着「cd swimwear-imagegen-a41(原始解压树的根)」,
读起来像是 a42 打在 a41 之上。**不是。** 三个补丁都能各自干净地打在同一棵
base 树上,blob 哈希可以验:

```text
CLAUDE.md            base=e7a61a8   task19 补丁的 index 行=e7a61a8
backend/api/reviews.py base=deea40e task19 补丁的 index 行=deea40e
```

`a42-task19` 的交接自己也写着「一个任务、一次文档口径调整,**没有合入别人的补丁**」。

**这就是为什么必须做三方合并而不是顺序 `patch -p1`** —— 顺序打会在重叠处
悄悄用后打的那份覆盖先打的那份,而下面第三节那条冲突正好是「覆盖任一边都是缺陷」。

---

## 三、五处冲突的判法

### 3.1 唯一的代码冲突:`backend/app/api/reviews.py`(approve / reject)

两个补丁**各改了同两行的一半**:

```text
a42-review   _basic_review_out(session, item)  ->  _single_review_out(session, item)
             (修:三个写接口无条件 500)
task19       return 前加 session.commit()
             (因为它摘掉了 get_session() 的请求级自动提交)
```

**取并集。** 两个反事实都实跑验证过:

| 取法 | 结果 |
|---|---|
| 只取 a42-review | `test_every_write_endpoint_commits_its_own_transaction` 变红,点名 `reviews.approve_review; reviews.reject_review` —— 接口返回 200 但什么都没落库 |
| 只取 task19 | `_basic_review_out` 形参只有 `item` 一个,三处调用传 2 个位置参数 → 运行期 `TypeError` → **生产 500 原样回来** |
| 并集(已采用) | `test_transaction_boundaries` 20/20 全绿 |

`regenerate` 那处 base 本来就有 `session.commit()`,所以没冲突,自动合对了。

合入后的形状(两处相同):

```python
    session.commit()
    return _single_review_out(session, item)
```

### 3.2 `docs/DECISIONS.md`:章节号撞车

两个补丁都新增了 §3.14 和 §3.15。

**保留 `a42-review` 的 3.14–3.18,`task19` 的两节顺延为 §3.19 / §3.20。**
判据不是先来后到,是**引用的硬度**:`tests/test_batch_lease_concurrency_db.py`
的 docstring 里硬写着 `docs/DECISIONS.md` §3.14 与 §3.16,
而 task19 那两节的引用全在 markdown 里,改起来没有代价。

同步改掉的引用点 6 处:

```text
CLAUDE.md              §3.15 -> §3.20   (服装范围口径)
docs/REVIEW.md         §3.15 -> §3.20   (抬头标注层)
docs/REVIEW.md         §3.14 -> §3.19   (任务 19 行,长事务例外)
docs/STATUS.md         §3.14 -> §3.19   (请求事务边界行)
sample-data/README.md  §3.15 -> §3.20
HANDOVER.md            §3.14 -> §3.19、§3.15 -> §3.20
```

### 3.3 `HANDOVER.md`:两份互不知情的 A42 交接

重建成「合入版」:抬头一段合入说明(含**过期结论清单**,见下一节)
+ 两部分并列 + 共用的历史节。

**历史节是从 `a42-review` 那份保下来的** —— task19 那份把
`# 历史:A41 交接` 整段删掉了(161 行 vs 356 行),顺序打会连带丢掉。

两部分各自的叙述**一字未改**,只把 `##` 降级成 `###` 让它们能挂在部分标题下。

### 3.4 `docs/STATUS.md`

task19 那侧的三行租约相关是 base 的**过期副本**(它没看到 a42-review 的修复),
a42-review 那侧是新的。取 a42-review 的 3 行 + task19 新增的 2 行。

### 3.5 `docs/REVIEW.md`

同一个表格行,两侧各加了一半信息(a42 的租约不变量修正、task19 的任务 19 收口)。
手工合成一行,两条都保留。

---

## 四、合入顺手清掉的两条 ruff 债

`a41-fixes-1` 的新测试文件带进来两条,会打破 `a42-review` 刚清零的
`ruff check app tests`:

```text
tests/pure/test_a41_fixes.py:14    F401  ESCAPE_CHAR 只出现在 import 行
tests/pure/test_a41_fixes.py:436   E501  102 字符
app/api/workbench.py:28            I001  app.core.clock 插在了 app.core.http_headers 之后
```

**第三条是复核时才发现的,第一轮漏了。** 漏的原因值得记下来:
`tools/lint_offline.py` 只覆盖 F401 / UP017 两条(它自己在输出里说了),
而离线环境装不了 ruff,于是"ruff 全绿"这句话在合入时**没有任何东西在验**。
第一轮我只手查了 F401 和 E501,I001 就这么过去了。

复核时补了一个覆盖 I001 / F821 / UP042 / B006 的检查再扫一遍才抓到。
**这仍然不等于跑过 `make lint`** —— E/F/I/B/UP 五组规则里还有大片没模拟到,
真正的验证只能在装得了 ruff 的机器上做。

前两条不是谁写错了 —— 是两个补丁各自在自己的树上都是绿的,
只有合到一起才会红,而没有人在合之前跑过合入树。

---

## 五、⚠️ 因为合入而作废的结论

**下面这些数字与判断分散在两份交接里,它们各自在自己那棵树上是对的,合入后一律作废。**
已在 `HANDOVER.md` 抬头列了同一份清单,这里给出实测替代值。

| 位置 | 原文 | 合入后实测 |
|---|---|---|
| 第一部分「三、门禁」 | `make test-pure 1473/1473(基线 1466 + 新增 7)` | **1498 条** |
| 第一部分「三、门禁」 | `全量 pytest 1652 条,1637 通过,15 失败` | 未重跑,**必须带真库重测** |
| 第一部分「三、门禁」 | `ruff check 6 条,本轮新增 0` | 6 条已清,合入后新增 0 |
| 第一部分「五、」标题与正文 | `15 条失败,都不是产品缺陷、单独排期` | **同一个补丁已经修了它们**(reviews 500 + savepoint),正文没跟着改 |
| 第一部分抬头 | `14 个源文件 + 4 个测试文件` | 实为 **17 源 + 6 测试 + 5 文档 = 28** |
| 第二部分「七、下一步」第 2 条 | `真库验证租约与回收,a41 留下的,仍一次没跑过` | 第一部分已跑了 **8 条并发用例** |
| `verify_imports` | `293 个文件` | **296 个文件** |

各分支基线条数(用仓库自己的 `tools/run_pure_tests.py` 数的):

```text
base(原始 a41 树)       1466
+ a41-fixes-1            1486
+ a42-review-fixes       1473
+ a42-task19             1471
合入树                   1498
```

---

## 六、合入树上跑过的门禁

```text
run_pure_tests            1496/1498     2 条失败均为本机缺 sqlalchemy / pydantic
verify_imports            296 个文件全绿
verify_delivery           12/12
verify_sample_data        5/5
lint_offline              290 个文件,F401 / UP017 无发现
frontend syntax-check     78/78 解析干净
py_compile                43 个改动的 py 文件全过
E501(按字符宽度)         0
密钥泄漏复验              跑完 run_pure_tests 后仓库根干净
```

**冲突解的变异验证:** `reviews.py` 那条的两个反事实都实跑变红(见 §3.1 表格)。

---

## 六又二分之一、合入之后补的一条:CI 跑 pytest 但没有 Redis

**这不是合入引入的,是合入之后复查 CI 时发现的 —— 它会让「下一步」在第一天就红。**

两份 A42 交接的下一步都是「启用真库持续集成」。但 `.github/workflows/ci.yml`
的 backend job 只声明了 `postgres` 服务,**一处 Redis 都没有**,
而同一个 job 里跑着 `pytest`,`conftest.py` 在 `CI=true` 时又是硬失败不跳过。

a42 自己已经把后果写进了 `docs/STATUS.md`:

```text
task_always_eager = True 不代表不需要 Redis —— Celery 仍要构造 result backend
缺它时 .delay() 抛 AttributeError: 'NoneType' object has no attribute 'Redis'
异常被 _deliver 吞进 outbox,任务静默停在 CREATED
表现:有库无 Redis 是 24 条失败,而那 24 条里没有一条提到 Redis
```

**知道了、写进文档了、CI 配置没改。** 这正是这个仓库反复在治的那件事
(「清单不等于执行」),只是这一次犯在门禁自己身上。

补了三处:

| 位置 | 改动 |
|---|---|
| `.github/workflows/ci.yml` | backend job 加 `redis:7-alpine` 服务 + `REDIS_URL` |
| `backend/tests/conftest.py` | 加一条与数据库守卫同形状的 Redis 守卫:CI 里连不上就在 collect 阶段炸掉,**并点名 Redis** |
| `backend/tools/verify_delivery.py` | 加 `check_ci_backs_pytest_with_redis()` —— 盯住「pytest 需要的服务有没有全部声明」,删掉 redis 服务会当场红 |

变异验证过:把 `redis` 服务从 `ci.yml` 拿掉,`verify_delivery` 从 13/13 变成
12/13,报错直接点名 Redis。

**刻意没做的一件事:** 没有配 `requires_redis` 标记。本机「有库、无 Redis」
那种组合仍然会得到那 24 条看不懂的失败 —— 要治它得把标记逐条贴到真正依赖
Celery 的用例上,而**哪些用例依赖它需要真库跑一遍才知道**。
靠读代码猜一份名单,贴多了会把该跑的用例静默跳过,比现在更糟。
理由写在 `conftest.py` 那段注释里,留给第一次真库全跑。

---

## 七、⚠️ 放行前必须做的三件事

合入是结构正确的,但**离线门禁验不到的东西,合入不会替它验**。

1. **真库全量 pytest。** 两条最高风险的改动都只能在真库上验:
   - `a42-review` 的 `conftest.py` savepoint —— 它改的是**全部集成测试**的事务模型
   - `task19` 摘掉 `get_session()` 的请求级自动提交 —— 它的「49 个写端点 / 13 个漏 commit」
     审计是在 **base 树**上做的,合入后有 `test_transaction_boundaries.py` 的
     HTTP 边界五条兜着(已绿),但结构绿不等于真的提交了

2. **重跑并订正两份交接里的所有数字**(见第五节)。

3. **前端四条与 Playwright。** `a41-fixes-1` 改了两个前端文件
   (`saveBlob` 的 revoke 时序、列密度偏好),**两处都没有 Vitest 覆盖** ——
   a41 自己在 `STATUS.md` 里承认了这件事。本次合入只跑了语法解析。
