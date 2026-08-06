# A45-batch14-20:PRD 阶段 4「多颜色图片生产」

基线 A45-batch14-19。对应 **PRD v3.1 §13 阶段 4** 的五项交付:

> GenerationPlan 实体与 UI;任务绑定 color_variant_id 与双指纹;
> 评分器"事实一致性"维度组接一票否决;图片集 variant 绑定入口 +
> §6.5 混排规则 + variant_coverage 参与门禁;图片版本失效接扩展矩阵。

**本包动了迁移链**(head `0040` → `0041`)。合并时改过号,见下面这一节。

---

## 〇、并线合并:本批不是单独落地的,撞了三处

本批与另一条并行线(阶段 3「识别 run 身份」)**同名同基线**:两条都自称
`A45-batch14-20`,基线都是 14-19,谁都不知道对方存在。合并时的三处冲突:

| 撞在哪 | 表现 | 处理 |
|---|---|---|
| **迁移号** | 两份都是 `revision = "0040"` / `down_revision = "0039"` | 本批改 `0041`,`down_revision` 指向 `0040` |
| `tools/mutate_batch14_20.py` | 同名不同内容(那份 20 条,本批 41 条) | 拆成 `_run_identity.py` / `_stage4.py` |
| `docs/DECISIONS.md` | 两份 §3.34 | 本批那节改编为 §3.35 |

**迁移撞号那条是门禁抓到的,不是人看出来的。**它不是"两个 head" ——
alembic 在**加载期**就报 `Multiple revisions with the same identifier`,
整条链一步都跑不了。`verify_delivery.py` 那条「迁移链单一 head」里恰好有一句
revision 唯一性检查(A24 那轮 `0021` 撞号返工换来的),合并时它当场变红。

**三处撞车里,只有这一处有门禁** —— 而它有,是因为有人为它付过一次代价。
另外两处(同名变异脚本、两份 §3.34)当时没有任何东西会响:同名文件是
后写的覆盖先写的,同号决策则永远不会炸,它只是让「§3.34 说了什么」从此有
两个都说得通的答案。本次给决策编号补了一条门禁(交付项 14 条,新增
「决策日志编号不重复」);同名文件那一条没补,理由写在 §3.36。

两份动的是互不相交的表(那份动 `attribute_extractions`,本份动
`generation_plans` / `generation_tasks` / `listing_image_items`),所以合并
方式是**串起来**,不是合表。

合并复审还改了本文两处措辞,都是"理由过期"那一类,逐条写在第六节与第九节。

## 一、先说一条:BLOCK-02 等的不是 schema,是一句业务决定

`ListingImageItem.variant_id` 与 COALESCE 唯一约束**从 0013 起就在库里**。
挂了几个版本没接的原因写在 `image_set_service.variant_coverage()` 的注释里:

> 改成硬阻断呢?那会让每一个多色 SPU 立刻无法批准 —— 因为它们的图全是
> 通用图。那不是修复,是停产。

缺的从来不是列,是"通用图与颜色图混排以谁为准"这个决定。§6.5 把它定死了,
所以这一批**才敢**把 `variant_coverage` 从诊断变成门禁。

四条规则逐条落到判定上:

| §6.5 原文 | 落点 |
|---|---|
| 颜色主图与附图只能来自 `variant_id = 该颜色` 的条目 | `primary_for()` 去掉回落 + `SHARED_IMAGE_AS_PRIMARY` |
| 通用图只能进附图位,且须运营明确标记"通用",默认不混入 | `ItemView.shared_opt_in`(默认 False)+ `UNMARKED_SHARED_IMAGE` |
| 不得回退使用其他颜色的图片,缺图就是缺图(BLOCKED) | `coverage()` 里通用图不算覆盖 + `FOREIGN_VARIANT_IMAGE` |
| 必要角度按 `angles_json` 验收,不是只数总张数 | `MISSING_REQUIRED_ANGLE` |

**`primary_for()` 的行为翻转是本批唯一一处"改了既有正确行为"。**
旧写法 `for want in (variant_id, None)` 在单色时代是对的;多色下它的
具体后果是**红色 SKU 挂着黑色主图上架** —— 因为颜色绑定入口上线之前,
通用图就是第一个颜色的图。

## 二、改了什么

| 文件 | 改动 |
|---|---|
| `app/workflows/generation_plan.py` | **新增**,零依赖:角度规范化、方案指纹、覆盖解析、`effective_fingerprints`、`image_set_stale_scopes`、预算 fail-closed |
| `app/evaluators/fact_consistency.py` | **新增**,零依赖:§6.5 九项清单 → 硬错误码 |
| `app/evaluators/vision_schema.py` | Schema 与提示词接 `fact_consistency` 段;清单只有一份 |
| `app/evaluators/scoring.py` | 解析该段,产物并进既有 `hard_fail_codes` |
| `app/evaluators/base.py` | `EvaluationResult.fact_findings` |
| `app/evaluators/repair.py` | 两条新码的定向重生方向 |
| `app/core/enums.py` | `GenerationPlanStatus` / `ImageAngle` / `ASYMMETRY_INTRODUCED` / `FUSION_DEFECT` |
| `app/channels/generic/spec/*.yaml` | 三份规则包启用两条新码 |
| `app/listings/image_set_rules.py` | §6.5 四条规则 + `coverage()` 单一判定点 |
| `app/listings/image_set_service.py` | `_to_view` 带两列;校验传 `required_angles` |
| `app/workbench/stale_matrix.py` | §8.1 补两行(6×4 → 8×4) |
| `app/models/generation_plan.py` | **新增表** |
| `app/models/generation.py` | 任务四列 |
| `app/models/listing_image.py` | 图片项两列 |
| `app/workflows/idempotency.py` | 颜色 + 双指纹进键;截断 128 → 256 |
| `app/services/generation_plan_service.py` | **新增** |
| `app/services/generation_service.py` | `create_task` 接方案、预算闸、颜色作用域样品指纹 |
| `app/api/generation_plans.py` + `schemas/` | **新增** |
| `migrations/versions/0041_*.py` | **新增,一次都没执行过**(原写作 `0040`,并线撞号改号) |
| `frontend/src/api/generationPlans.ts` / `components/GenerationPlanPanel.tsx` | **新增,跑不了 tsc / Vitest** |
| `tests/pure/test_a45_batch14_20_stage4_image_production.py` | **新增,44 条守卫** |
| `tests/test_a45_batch14_20_stage4_db.py` | **新增,9 条真库用例,一次都没跑过** |
| `tools/mutate_batch14_20_stage4.py` | **新增,41 条变异,一次全红** |

## 三、判定与持久化分开,这一次有具体代价

第一版把 `plan_fingerprint()` 写在 `models/generation_plan.py` 里。那个文件
import sqlalchemy,而**这台机器上没有 sqlalchemy** —— 于是指纹算法一行都测
不到。而指纹算错不报错,只会让"换了方案"这件事静静地不触发重出图。

搬到 `workflows/generation_plan.py` 之后,指纹的每一条性质都被穷举:
归属不进指纹、每个参数各自改一次、三种写法的金额编成一个串、角度顺序不进键。

### 一条容易写反的:改 SPU 默认方案只影响没有覆盖的颜色

§4.7 的唯一约束是 `UNIQUE(spu_id, COALESCE(color_variant_id,''))`,库里同时
存在 SPU 默认与若干颜色覆盖。`effective_fingerprints()` 先解析出**每个颜色
当前生效的是哪一份**,再逐颜色比。

按"方案变了就全部过期"写的话,一个九色 SPU 调一次默认角度会重出八个颜色的
全部候选 —— 每一张都是真实付费调用,而没有任何地方会报错。

## 四、事实一致性:产物是码,不是第二个否决点

否决权已经在 `rules.grade_candidate` 的第一条线上(`hard_fail` → D + REJECT)。
本批的产物是**硬错误代码**,走和评分器自报的码完全同一条路(去重、白名单、
并进 `hard_fail`)。

另建一条"事实不一致就拒绝"的分支,系统就有两个地方能把一张图判死,
而两者漂移时"这张图为什么是 D"会有两个都说得通的答案。

### 模型只回答"看到了什么"

每一项模型给 `observed` 与 `verdict`,**是不是不一致由后端拿 `CONFIRMED`
事实比**。让模型自己比有一个具体的坏处:事实值是我们的、图是它的,
它没有理由知道"这个 SPU 的肩带确认为可拆卸",于是它会猜 —— 而猜错的方向
恒定偏向"一致"。

### 事实没确认时一律不判不一致

这一条写反是致命的:默认判不一致会让整批图在事实确认之前**全部被否**,
而运营看到的是"模型说所有图都错",查不到是自己少确认了一个字段。
跳过项进 `uncertain_items` 并**单独标出"未判定"** —— 变异 F6 打的就是
"跳过项报成看不清"那条路,少了它,"少确认一个字段"会被显示成"模型看不清",
运营按后者去补图。

### 新增了两条硬错误码,不复用 GARMENT_WRONG

§6.5 九项里前七项都有对应码,「不对称」「融合缺陷」没有。复用
`GARMENT_WRONG` 会让 `repair.py` 的定向重生选错方向:那条码的对策是换 seed /
换 Provider,而这两条的对策是**换模特模板并降低融合强度**。一件左右不对称的
正确泳衣会被反复换 seed 重生,每一轮都是付费调用而命中率不变。

三份规则包一起启用:这两条描述的是渲染工艺,与受众无关。

## 五、本批验到了什么、验不到什么

### 验不到:迁移 0041 一次都没执行过

四样东西的正确性完全落在数据库上,而**它们失效时都不报错**:

```
uq_generation_plans_scope 是表达式唯一索引   写成 UniqueConstraint 会因为
                                            NULL 互不相等而挡不住第二份
                                            SPU 默认方案 —— 纯层看不见索引
WHERE status <> 'ARCHIVED'                  少了它,一个 SPU 一辈子只能改
                                            一次方案(归档那份占着唯一位)
ondelete 方向                               删一份方案不该连着删掉它出过的图
ck_generation_plans_budget_cap              负上限的表现是"每次都被预算拦下",
                                            而提示写的是"预算不足"
```

真验证在 `tests/test_a45_batch14_20_stage4_db.py`,**9 条,一次都没跑过。**
下一台有库的机器第一件事就是它。

第一条失败时**先看是不是索引写法的问题**,不要去改模型让它变绿 ——
让它变绿最省事的做法正好是把 COALESCE 删掉。

### 验不到:§6.5 门禁上线会不会让存量图片集集体无法批准

§3.1 写着"系统尚未投入使用、不考虑存量数据迁移",但那句话**从来没有在真库
上被验证过**。DB 用例里有一条专门断言这个前提(库里没有绑定颜色的已批准
图片集)。它红了说明那句话不成立,那时该做的是先做数据盘点,**不是调松门禁**。

### 变异清单里刻意缺席的

`Index(..., unique=True)` 改成 `UniqueConstraint(...)` 这类**索引语义变异**
没有列进 `mutate_batch14_20_stage4.py`。它们在这台机器上必然 GREEN(纯守卫看不见
数据库索引),列一条明知抓不住的变异进去只会让"41/41"变成一句谎话。

## 六、双指纹只接了一半,这是刻意的

§6.5 要求"绑定 color_variant_id + plan + 双指纹"。**颜色那一半接了,
共享那一半没接。**

```
颜色作用域   sample_fingerprint 落在任务行上(0041),比较对象在同一批 —— 接了
共享作用域   比较对象是**一条确认事实的** input_fingerprint(§4.6) ——
             `ProductAttributeValue` 上没有这一列
```

**这一条在并线合并那天改过理由,原稿写的是"那批列还不存在"。**
并行线 `0040_extraction_run_identity` 确实把 `input_fingerprint` 落了,
但落在 `ProductAttributeExtraction` —— 那是**识别 run 行**,不是事实行。
一次 run 可以产出/更新多条事实,一条事实也可以跨多次 run 存活,所以
"一条事实继承哪一次 run 的指纹"是个**还没做的决定**,不是一次赋值。

缺的东西因此从"列不存在"变成"事实行上的对照方 + 那个继承决定",
但结论一个字没动 —— 硬接的表现还是那个:算出来没地方存,每次现算一个
和空值比,而 `facts_stale(stored=None)` 恒为 True —— **全库事实一次性
集体过期**,运营看到"所有东西同时过期了"而查不出原因。

改这句话本身是有代价的事:按 §3.33 的规矩,**一条过期的理由比没有理由更糟**
—— 下一个人照着"列不存在"去查,会发现列就在那儿,然后顺手把它接上,
接到的是 run 行。

取数走 §5.1 那个唯一入口(`media.evidence_assets_for`),**没有照着
`usable_assets()` 抄一行** —— 那正是 14-19 收口掉的形状。

## 七、门禁

```
纯逻辑          2394/2394   0 失败,7 跳过   ← 合并后重跑,含另一条线
本批变异        41/41       一次全红
阶段 3 变异     20/20       一次全红(另一条线,合并后重跑)
锚点            372/372     18 份脚本
守卫窗口审计    495 个      反向断言都吃着封闭窗口
交付            14/14       含「迁移链单一 head」(已认 0041)+ 新增「决策日志编号不重复」
样例数据        5/5
导入            400 个文件
```

**这些数字是合并后重跑的,不是本批单独的成绩。**本批单独跑时是
2372 / 352 / 17 份 / 397 —— 差值来自另一条并行线,以及合并复审新增的
两条欠账守卫(见第九节)。

**仍未执行**:前端四条(tsc / ESLint / Vitest / build)、
`alembic upgrade/downgrade`(0037 / 0038 / 0039 / **0040** / **0041** 从未执行)、
**真库 pytest(含本批那 9 条)**、Ruff / lint-imports 本体、
Docker build、Playwright 浏览器。

## 八、阶段 4 还剩什么

| 项 | 卡在哪 |
|---|---|
| 共享作用域样品指纹 | 要**事实行上**的 `input_fingerprint` + "一条事实继承哪次 run 指纹"这个决定,**单独一批**(理由见第六节,合并当天改过) |
| §6.5 两列的写入路径 | **没写**,不是卡住 —— 见第九节 |
| 前端方案面板接进路由 | **没接**,不是卡住 —— 见第九节 |
| §8.1 剩下四行变更源 | 机制要等阶段 5 的颜色结构化字段与价格/库存快照 |
| 前端方案面板实测 | 无 node_modules |
| AC-08/09/12/13 验收 | 要真库 + 真样品 |

## 九、合并复审记的两条:把"没写"从"验不到"里拆出来

原稿有两处把**接线欠账**说成了**环境限制**。这两件事的处理方式完全相反:
前者等有人写一行代码,后者等一台有依赖的机器。混成一句话的后果一样 ——
下一个人以为"等机器就行",而机器到了之后东西照样不工作。

### 一、`shared_opt_in` / `angle` 两列没有写入路径

两列本批落库,`_to_view` 读它们,§6.5 的四条规则用它们判定。
**但 `create_set` 没有写过它们**,全树没有任何写入路径。

原稿的措辞是"角度继承:接线点在服务层,而服务层要 sqlalchemy"。
**在 `create_set` 里多写两个 kwarg 不需要运行 sqlalchemy,只需要有人写。**

后果不是"少了个功能":

    shared_opt_in 恒 False   每张通用图恒定命中 UNMARKED_SHARED_IMAGE
    angle 恒 NULL            某个颜色一配上方案,那个颜色的必要角度
                            **永远覆盖不了**,图片集再也批不过

第二行尤其安静:门禁不报"少了写入路径",它报的是**「缺正面图」**。
运营会去补图,补多少张都没用,因为新图的 `angle` 同样是 NULL。
这与本批自己在 §4 记的那条同源:**报错报在别的地方,比不报错更贵。**

### 二、方案面板全树零 import

`GenerationPlanPanel.tsx` 与 `api/generationPlans.ts` 本批新增,
**没有任何地方 import 它们** —— 面板存在,但进不去任何路由。
原稿只写了"跑不了 tsc / Vitest";那句话是真的,但 tsc 会给一个没有任何
引用的组件开绿灯。

### 两条都换成了点名守卫,而不是只写进文档

```
test_the_two_new_item_columns_have_no_writer_yet_and_this_is_the_ledger
test_the_plan_panel_is_written_but_not_reachable_yet_and_this_is_the_ledger
```

**接线那天它们会红,那是还款日** —— 这是 §3.34 立的规矩(欠账守卫是有
还款日的)的第一次复用。写进文档不写守卫的话,还清的那天没有任何东西会提醒
"顺手把这条记录删掉",于是文档里会留下一条**已经不成立的欠账**,
而那正是 §3.33 判过死刑的东西。

第二条**刻意不测可达性**:`test_frontend_contract.py` 顶部那张分工表把
"路由可达"划给了 Playwright(P5)。这条守卫只测更弱也更确定的一件事 ——
有没有人 import 过。越界去测可达性,等于在纯层重建一份前端路由知识,
而那份知识会和真实路由漂移。
