# A45-batch14-20 交接:两条并行线合并 —— 阶段 3 接线欠账还清,阶段 4 落码

> **给其余并行线的一句话:本包动了迁移链**(head `0039` → **`0041`**)。
> **两份迁移,不是一份。**`0040` 是识别 run 身份五列,`0041` 是生成方案表 +
> 任务四列 + 图片项两列。后者原写作 `0040`,合并时改的号。
>
> 后端动了 `models/attribute.py`、`attributes/service.py`、`extractors/*`、
> `listings/image_set_{rules,service}.py`(**有一处行为翻转**)、
> `evaluators/*`、`workbench/stale_matrix.py`、`workflows/idempotency.py`、
> `services/generation_service.py`、`tools/verify_delivery.py`。
> 前端新增方案面板与 API 客户端,**没接进任何路由,也没编译过**。

逐条说明分两份:

    docs/MERGE-A45-BATCH14-20-RUN-IDENTITY.md             阶段 3,§4.6 五列 + §9.2 幂等
    docs/MERGE-A45-BATCH14-20-STAGE4-IMAGE-PRODUCTION.md  阶段 4,§6.5 + 生成方案

## 〇、先读这一条:这个包是两批并线合的,撞了三处

两条线都自称 `A45-batch14-20`,基线都是 14-19,谁都不知道对方存在。

| 撞在哪 | 表现 | 处理 |
|---|---|---|
| **迁移号** | 两份都是 `revision = "0040"` / `down_revision = "0039"` | 阶段 4 那份改 `0041`,`down_revision` 指向 `0040` |
| `tools/mutate_batch14_20.py` | 同名不同内容(20 条 vs 41 条) | 拆成 `_run_identity.py` / `_stage4.py` |
| `docs/DECISIONS.md` | 两份 §3.34 | 阶段 4 那节改编为 §3.35 |

**迁移撞号那条是门禁抓到的,不是人看出来的。**它不是"两个 head" ——
alembic 在**加载期**就报 `Multiple revisions with the same identifier`,
整条链一步都跑不了。`verify_delivery.py` 那条「迁移链单一 head」里恰好有一句
revision 唯一性检查(A24 那轮 `0021` 撞号返工换来的),合并时它当场变红。

**三处撞车里,只有这一处有门禁** —— 而它有,是因为有人为它付过一次代价。
另外两处(同名变异脚本、两份 §3.34)当时没有任何东西会响:同名文件是
后写的覆盖先写的,同号决策则永远不会炸,它只是让「§3.34 说了什么」从此有
两个都说得通的答案。本次给决策编号补了一条门禁(交付项 14 条,新增
「决策日志编号不重复」);同名文件那一条没补,理由写在 §3.36。

两份动的是互不相交的表(`attribute_extractions` vs `generation_plans` /
`generation_tasks` / `listing_image_items`),所以合并方式是**串起来**,
不是合表。

## 一、下一台有库的机器,前三件事

```
alembic upgrade head                                          # 0040 + 0041
pytest tests/test_a45_batch14_20_run_identity_db.py -v        # 11 条,没跑过
pytest tests/test_a45_batch14_20_stage4_db.py -v              #  9 条,没跑过
```

**这两条先跑:**

`test_the_partial_unique_index_lets_a_failed_run_be_retried`(0040)。
它验的是那个「部分」:谓词写丢了(退化成全表唯一)的表现是**一次识别失败之后
同样的输入再也建不出第二个 run** —— 输入没变、模型没变、字段没变,而那正是
重试的定义。运营看到的是一个再也识别不了的商品,唯一的解法是去改点什么来
骗过约束。

`uq_generation_plans_scope` 那条(0041)。它是**表达式唯一索引**
(`COALESCE(color_variant_id::text,'')` + `WHERE status <> 'ARCHIVED'`)。
写成 `UniqueConstraint` 会因为 NULL 互不相等而挡不住第二份 SPU 默认方案,
那时 `resolve_plan()` 每次按查询顺序挑一份,同一个 SPU 两次创建任务用了不同的
参数。**纯层守卫看不见数据库索引。**

它红的时候**先看是不是索引写法的问题,不要去改模型让它变绿** ——
让它变绿最省事的做法正好是把 COALESCE 删掉,而那正是这条索引要防的东西。

跑完手工确认一次三方一致(ORM 声明 / 迁移里的冻结字面量 / 真建出来的索引):

```sql
SELECT pg_get_indexdef(indexrelid) FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
 WHERE c.relname IN ('uq_attr_extractions_idempotency_key',
                     'uq_generation_plans_scope');
```

三者之间任意两个漂移都不报错,只是让「这个键被占了吗」在不同环境下有不同
答案,而只有其中一边会多付钱。

## 二、有一处既有行为被翻转了

`image_set_rules.primary_for()` 原来在颜色没有专属主图时**回落到 SPU 通用图**。
§6.5 把 BLOCK-02 挂了几版才等到的那个业务决定定死了:

> 不得回退使用其他颜色的图片,缺图就是缺图(BLOCKED)。

回落是"看起来更友好"的那一侧,它的具体后果是**红色 SKU 挂着黑色主图上架**
—— 因为颜色绑定入口上线之前,通用图就是第一个颜色的图。

同一条决定让 `variant_coverage` 从**诊断**变成**门禁**:原来那句"只要集里存在
通用图,所有变体都算被覆盖"没有了。三条老守卫因此翻转,翻转后的断言由变异
R1 / R2 重新咬一遍。

BLOCK-02 等的从来不是 schema —— `ListingImageItem.variant_id` 与 COALESCE
唯一约束**从 0013 起就在库里**。缺的是"通用图与颜色图混排以谁为准"这一句话。

## 三、三条接线欠账,各有一条点名守卫记账

阶段 4 的五项交付里有三项是**半截**。三条都有守卫,**接线那天它们会红,
那是还款日**:

```
test_the_plan_panel_is_written_but_not_reachable_yet_and_this_is_the_ledger
test_the_two_new_item_columns_have_no_writer_yet_and_this_is_the_ledger
_variant_sample_fingerprint() 的 docstring(共享作用域那一半)
```

### 第二条最要紧:它的失效方式是"报错报在别的地方"

`listing_image_items` 的 `shared_opt_in` / `angle` 两列本批落库,`_to_view`
读它们,§6.5 的四条规则用它们判定。**但 `create_set` 没有写过它们** ——
全树没有任何写入路径。

后果不是"少了个功能":

    shared_opt_in 恒 False   每张通用图恒定命中 UNMARKED_SHARED_IMAGE
    angle 恒 NULL            某个颜色一配上方案,那个颜色的必要角度
                            **永远覆盖不了**,图片集再也批不过

第二行尤其安静:门禁不报"少了写入路径",它报的是**「缺正面图」**。
运营会去补图,补多少张都没用,因为新图的 `angle` 同样是 NULL。

阶段 4 原稿把这件事记在「验不到什么」里,措辞是"接线点在服务层,而服务层要
sqlalchemy"。**合并复审时改了**:在 `create_set` 里多写两个 kwarg 不需要
**运行** sqlalchemy,只需要有人写。缺的不是环境,是代码 —— 混成一句话的后果
是下一个人以为"等机器就行"。

### 第三条:一句理由在合并当天变质了

阶段 4 原稿写的是「共享作用域那一半接不了,因为 §4.6 的 `input_fingerprint`
**那批列还不存在**」。

**合并之后这句话不成立了** —— 阶段 3 那批刚刚把 `input_fingerprint` 落了。
但结论没变,变的是理由:那一列落在 `ProductAttributeExtraction`,也就是
**识别 run 行**;而 `facts_stale()` 问的是"这条事实还成不成立",
`ProductAttributeValue` 上没有这一列。一次 run 可以产出多条事实,一条事实
也可以跨多次 run 存活 —— **"一条事实继承哪一次 run 的指纹"是个还没做的决定,
不是一次赋值。**

按 §3.33 的规矩改了措辞:**一条过期的理由比没有理由更糟**。下一个人照着
"列不存在"去查,会发现列就在那儿,然后顺手把它接上 —— 接到的是 run 行。

硬接的表现没变:`facts_stale(stored=None)` 恒为 True,**全库事实一次性集体
过期**,运营看到"所有东西同时过期了"而查不出原因。

## 四、两份迁移各自的刻意选择,别顺手改掉

**0040:`status` 不回填,`server_default='FAILED'`。** 回填要把
`terminal_status_for` 重写成 SQL 的 CASE(第二个判定点),或者把它 import
进迁移 —— 后者更糟:迁移冻结在时间里,而判定会演进,三个月后在一台新库上
`alembic upgrade head` 会用**新规则**去写**旧行**,无声无息。全仓 41 份迁移
没有一份 import 过 `app.*`,这条不由这里开口子。

方向是有代价的:一次真的成功过的旧 run 会显示成失败。选它是因为反方向更贵
—— 默认 COMPLETED 会让一次从来没有被判定过的 run 以「算数」的身份参与事实
合并与占键两件要花钱的事。

**0041:也不回填,而且"顺手回填一个默认方案"是错的。** 那会给每个 SPU 造出
一份没人配过的方案,它的指纹会立刻进幂等键,于是第一次真正配方案时反而命中
旧任务。

**0041:`idempotency_key` 从 VARCHAR(128) 扩到 256。** 方案指纹要进键,而
`build_idempotency_key` 对显式键做的是 `[:128]` 截断 —— 截断之后两份不同的
输入可能得到同一个键,那是"幂等"这件事最坏的失效方式。列先扩,截断长度在
`workflows/idempotency.py` 一并放宽。

## 五、幂等今天覆盖不到哪几条路

建不出键就留空,**不编一个**:

| 情况 | 为什么不凑 |
|---|---|
| 增量识别(`only_media_ids`) | `canonical_scope` 只有共享/指定颜色/全部三种形状,「任意素材子集」不是其一。硬塞成 ALL 会让 `requested_scope` 说一句不真的话,而它是键的一部分 |
| 商品没有 `spu_id` | 拿 `product_id` 顶上会让两个不同 SPU 的同型请求算出同一个键 —— 第二个商品填上第一个商品的属性,接口 200 |
| 抽取器报不出调用前版本 | 取响应里的版本 = 付过钱才算得出键,而键要挡的正是付钱前那两下 |

**第二条今天覆盖面很大**:老建档路径(`create_product`、CSV 导入)还不写
`products.spu_id`,那是阶段 1 的剩余项。在它落地之前,只有走 `POST /spus`
三步建档链路建出来的商品拿得到幂等保护。

这个方向是刻意的:少挡一次的代价是一次重复付费,挡错一次的代价是一个再也
识别不了的商品。

## 六、验不到的两件事,别拿门禁绿灯当答案

**一、§6.5 门禁上线会不会让存量图片集集体无法批准。**
§3.1 写着"系统尚未投入使用、不考虑存量数据迁移",但那句话**从来没有在真库上
被验证过**。DB 用例里有一条专门断言这个前提(库里没有绑定颜色的已批准图片集)。
**它红了说明那句话不成立,那时该做的是先做数据盘点,不是调松门禁。**

**二、索引语义类变异在这台机器上必然 GREEN。**
`Index(..., unique=True)` 改成 `UniqueConstraint(...)`、把 `postgresql_where`
删掉这一类,纯层守卫看不见。它们**刻意没有列进**两份变异脚本 ——
列一条明知抓不住的变异进去,只会让「41/41」和「20/20」变成一句谎话。

## 七、门禁(合并后重跑,不是两批数字相加)

```
纯逻辑         2394/2394   0 失败,7 跳过(缺 pydantic / sqlalchemy)
阶段 4 变异       41/41     一次全红
阶段 3 变异       20/20     一次全红
锚点           372/372     18 份脚本
守卫窗口审计      495 个    反向断言都吃着封闭窗口
交付            14/14      含「迁移链单一 head」(已认 0041)+ 本次新增「决策日志编号不重复」
样例数据          5/5
导入            400 个文件
```

**仍未执行:** 前端四条(tsc / ESLint / Vitest / build,无 node_modules)、
`alembic upgrade/downgrade`(0037 / 0038 / 0039 / **0040** / **0041** 从未执行)、
**全部 `requires_db` 用例**(池子 80 条,本合并 +20)、Ruff / lint-imports 本体、
Docker build、Playwright。

**验收侧照旧:AC-01～AC-22 没有一条在真环境验收过。阶段 P0 仍未关闭。**
