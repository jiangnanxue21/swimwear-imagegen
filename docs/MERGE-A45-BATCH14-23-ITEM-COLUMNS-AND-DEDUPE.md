# A45-batch14-23:§6.5 两列的写入路径 + §4.8 去重键拆账

基线 14-22。本批只做"只缺有人写"的那一类,不替任何人做决定 ——
分界线与逐条归类见第五节。

## 一、还清:`shared_opt_in` / `angle` 的写入路径

### 欠了什么

14-20 落库了 `listing_image_items.shared_opt_in` 与 `angle`,`_to_view` 读它们,
§6.5 的四条规则用它们判定,**而全树没有任何代码路径写过它们**。

那一批的守卫 `test_the_two_new_item_columns_have_no_writer_yet_and_this_is_the_ledger`
逐字记着后果:

    shared_opt_in 恒 False   每张通用图恒定命中 UNMARKED_SHARED_IMAGE
    angle 恒 NULL            配了方案的颜色**永远覆盖不了必要角度**

第二条尤其安静:门禁不报"少了写入路径",它报的是「缺正面图」。运营会去补图,
补多少张都没用,因为新图的 `angle` 同样是 NULL。

### 落了什么

| 层 | 改动 |
|---|---|
| 入参 | `ImageItemIn` 增 `shared_opt_in: bool = False`、`angle: ImageAngle \| None` |
| 出参 | `ImageSetItemOut` 带回这两列 |
| 服务层 | `create_set` 的 `ListingImageItem(...)` 写两个 kwarg |
| 归一化 | 新增 `_angle_value()`:空串收成 `None` |

### 三处刻意的不对称,别顺手抹平

**一、入参用枚举,出参用 `str`。**

`angle` 是拿去和 `GenerationPlan.angles_json` 的键比对的。一个拼错的
`"Front"` 不会报错,它只是**覆盖不到任何角度** —— 表现为图片集批不过,
而错在三层之外的一个入参。所以入参必须在 422 就停住。

出参不收紧:库里是 `String(24)`,存量值(以及将来枚举加成员之前写进去的值)
读不回来时不该让整个详情接口 500 —— 那不是调用方能修的。**入严出宽。**

**二、`shared_opt_in` 在入参层有默认值,不是可缺省为 `None`。**

这一列在库里 `nullable=False`。入参可空会让「少传一个字段」与「显式传 false」
变成两件事,于是"没勾"和"勾了又取消"在两条路径上走出两个结果。

**三、空串归一成 `None`,不留两种"没标注"。**

`covered_angles` 是一个集合。空串会作为成员进去,于是"这个颜色覆盖了几个角度"
多算一个,而那一个永远匹配不上 `angles_json` 里的任何键 —— 表现是角度验收
**差一个**,而库里看上去每一行都有值。

### 守卫翻转:断言"每一个",不是"存在一个"

欠账守卫按它自己 docstring 的交代删掉,换成正向守卫。断言写成
**每一个 `ListingImageItem` 构造点都写这两列**,理由是:退化回去最省事的
路径不是删掉 kwarg,是**加第二个构造点**(候选入集、复制图片集、导入)
而漏掉它们。那时旧构造点仍然写着,"存在一个"照样绿,而漏掉的那一批行
在库里与从前完全一样。

## 二、记账:§4.8 去重键,一笔逾期欠账

### 事实

PRD v3.1 §4.8 那张表逐字写着:

    去重键 | 改为 `UNIQUE(spu_id, COALESCE(color_variant_id,''), sha256)`

今天库里仍是 `UNIQUE(product_id, sha256)`,来自迁移 `0011`。`0037` 自己在
注释里列过"这一版**不含**……新去重键(spu_id, sha256)",随后六批没有一批补上。

### 它躲过六批的方式与 14-22 那一列不同

14-22 那一列(`color_variant_id` 无写入路径)躲得掉,是因为**没有守卫盯它**。
这一条相反:**它有守卫,而守卫是绿的。**

    def test_dedupe_key_is_product_scoped():
        assert 'UniqueConstraint("product_id", "sha256"' in source   # 现状
        assert 'UniqueConstraint("sha256"' not in source             # 不变量

第二句永久成立。第一句不是 —— 它描述的是今天恰好长这样。两句写在一起时,
整条守卫按"永久不变量"被对待,于是它读起来像成绩,清点表把阶段 2 交付第一项
记成了完整落地。

更贵的是第二层:**这条守卫把 PRD 的待办钉成了退化路径。** 谁按 §4.8 落新键,
它当场红;而让它变绿最省事的做法正好是把新键改回旧键,守卫的措辞
("跨商品不去重是刻意的")还会替这个动作提供理由。

一般化写进 `docs/DECISIONS.md` §3.39。

### 本批的处理:拆成两条,不动键本身

| 新守卫 | 位置 | 性质 |
|---|---|---|
| `test_the_dedupe_key_is_never_global` | `test_media_layer.py` | 永久不变量 |
| `test_the_section_4_8_dedupe_key_is_still_owed_and_this_is_the_ledger` | 本批文件 | 欠账,还款日:阶段 5 |

欠账守卫钉住旧键(自翻转:落新键那天它红,那是**还款的确认**),同时挂还款日
(那是**催促**)。§3.37 说的两半都要有。它还多钉一条:`media_assets` 不许
只加 `spu_id` 而不动去重键 —— 那个中间态正是它要挡的。

### 为什么是记账不是还账:这一步缺的是决定,不是代码

**`media_assets` 今天没有 `spu_id` 列。** 它有的是:

    spu: String(64)              一个反规范化的字符串码
    color_variant_id: UUID       14-22 刚接上写入路径的那一列

§4.8 的键要 `spu_id`。所以第一步不是"改一个约束",是先回答:

> 素材挂在 SPU 上,挂的是那个字符串码,还是一个真外键?

而那个答案会连带决定 `variant_key` 退役(阶段 1 剩余项)那一刀往哪切 ——
两件事分开做的中间态是:一批素材按字符串码去重、另一批按外键,
**而两者对"同一个 SPU"的判断可以不一致**。

阶段 5 的 `color_sku_image_map` 建在素材归属之上,所以那之前是死线。

### 缺了它今天会怎样

§11 场景 1(同图上传到两个颜色 → 命中提示 → 人工确认后各自成行)在库级
没有落点。`provenance_conflict` 那一路是按 `spu + sha256` 自己找的,
它拦得住 AI 图伪装(AC-22 的那一半),**拦不住跨颜色重复本身** ——
两个颜色各建一条商品行时,`(product_id, sha256)` 对它们是两个不同的键,
重复不会命中,人工确认那一步不会发生。

## 三、改准一条过期的理由:方案面板不是"缺一行 import"

14-20 那条面板欠账守卫写着:

> 没接进路由是**接线欠账**……前者等一台有 node_modules 的机器,
> 后者等有人写一行 import。

**逐条核路由之后,后半句是错的。** 面板要 `spuId`(UUID),而:

| 想从哪拿 | 实际有什么 |
|---|---|
| `api/spus.ts` | 不存在 |
| `batchApi` 的 `SpuGroup` | 只有 `spu: string`,反规范化字符串码 |
| workbench 那几组出参 | 同样只有 `spu: string` |
| `publish.ts` 的 `external_spu_id` | **平台侧**外部 id,不是本系统主键 |

唯一在出参里给 `spu_id` 的 schema 是 `generation_plan.py` 自己 ——
**要拿到 spu_id 得先有一份方案,而要列方案得先有 spu_id。**

所以这笔账与「三步建档 UI」(阶段 1 剩余项)是同一笔,不是两笔:两者都卡在
"前端要有一个知道 SPU 主键的宿主页"。按 §3.33 把措辞改准 ——
照着"写一行 import"去做的人不会发现自己在做错事,他会发现无处可写,
然后多半把 `spu` 字符串码传进 `spuId`。那时接口 422,而错因指向后端。

守卫的断言没动(它测的仍是"有没有人 import 过"),改的只有理由。

## 四、门禁

```
纯逻辑      2445/2445   0 失败,7 跳过(缺 pydantic / sqlalchemy)
本批变异       9/9      一次全红
锚点        436/436     21 份脚本
守卫窗口       514 个
交付         15/15
样例数据     10/10
前端语法      86/86
```

变异分三组:

    W 组  写入路径。两个 kwarg 各掐一次 + 绕开归一化 + 归一化不收空串
    E 组  入参形状。角度退化成自由字符串、勾选退化成可空、出参错误地收紧
    D 组  去重键欠账。直接改键、以及"偷偷加 spu_id 而不动键"那个中间态

W3 / W4 第一轮是 GREEN —— 守卫只钉了"写没写",没钉"走没走归一化"。
补两条之后 9/9。**列一条明知抓不住的变异进去,只会让「9/9」变成一句谎话**,
所以补的是守卫,不是删的变异。

## 五、本批没做的,以及它们各属哪一类

分界线是**缺代码**还是**缺一个决定**;第三类是**缺环境**。

| 项 | 类别 | 说明 |
|---|---|---|
| `variant_key` 退役 + `owner_id` 切 UUID | 决定 | 不是数据迁移,是**身份变更**。回填 `color_variant_id` 会让已确认的颜色属性、图片标签、14-21 落的事实指纹同时指向不存在的变体;14-22 之后素材也真的挂在这一列上了。必须与素材归属改写在同一个动作里做 |
| §4.8 新去重键 | 决定 | 先要 `media_assets.spu_id` 那个决定,见第二节 |
| `evidence_class` 存储列 + CHECK | 决定 | 要回填,而回填规则在 `derive_evidence_class` 里会演进。迁移不许 import `app.*`(全仓 42 份没有一份破例),所以要么把规则冻成 SQL CASE(第二个判定点),要么不回填。这是 `0040` 当时逐条权衡过的同一道题,本批不替它答 |
| 老建档路径写 `products.spu_id` | 决定 | `create_product` / CSV 导入建出来的商品没有 SPU。要么拒绝(改变现有接口语义),要么自动建一个 SPU(凭什么字段建、受众从哪来)。两条都是业务决定 |
| 三步建档 UI / 按颜色上传 UI / 方案面板宿主页 | 环境 | 写得出来,但这台机器没有 `node_modules`:tsc / ESLint / Vitest / build 一条都跑不了。**给这个仓库加不可验证的前端等于扩大 D 类缺口,不是缩小它** |
| 识别 run 异步化 + cancel + QUEUED | 环境 | 阶段 1-3 里唯一一条真的卡在 Redis + worker 上的 |
| §8.1 六个变更源 | 决定 + 依赖 | 剩的四行多半要等阶段 5 的颜色结构化字段。守卫在,还款日阶段 5 |

## 六、下一台有库的机器,先跑这两条

```
pytest tests/test_a45_batch14_22_colour_attribution_db.py::test_the_colour_survives_all_the_way_into_the_row
pytest tests/test_a45_batch14_21_facts_stale_db.py::test_adding_an_asset_for_one_colour_leaves_the_other_colours_facts_alone
```

本批**没有新增真库用例** —— 两列的写入路径是纯层可证的(构造点写没写、
归一化做没做),而"真的写进那一行了吗"要等 `alembic upgrade head` 先跑过。
`0041` 从未执行过,那是先决条件,不是本批的欠账。

跑之前先 `make seed`。
