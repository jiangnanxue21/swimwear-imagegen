# A45-batch14-19:§5.1 白名单成为取数入口

基线 A45-batch14-18。对应 **PRD v3.1 阶段 2「素材归属与证据分层」交付第四项**:
「白名单查询助手成为唯一取数入口」。

---

## 一、先更正一条过期的警报

`docs/STATUS.md`「已知限制」里长期挂着这条:

> 今天 `run_extraction` 走的仍是 `media_service.usable_assets(product_id)` ——
> 只过滤 `status = READY`。**后果是 AI 生成的候选图如果被落成该商品的素材,
> 会进识别输入并产生一次真实的付费调用。**

**这句话从 A45-batch14-7 起就不成立。** 那一批把
`evidence_rules.asset_is_extraction_input()` 接进了 `run_extraction`,
AI 图进不来。也就是说这条"正在烧钱"的警报已经响了好几批,而钱并没有在烧。

一条过期的警报比没有警报更糟:它会让人去查一个不存在的问题,也会让人
以为这条路还没通(于是不会去用它)。本批一并改掉。

## 二、真正没做完的是那半句"唯一取数入口"

搬家之前的形状:

```
取数    usable_assets()              未过滤,谁都能调
过滤    run_extraction 里一段推导式   只保护这一个调用点
```

判定是对的,**入口不是收口的**。任何一个新写的调用点只要照着
`usable_assets()` 抄一行,就能把 AI 图喂进付费抽取器,而**没有任何地方会红**。

这两件事保护的根本不是同一个对象:

| | 保护的是 |
|---|---|
| 判定对了 | 今天写好的那**一个**调用点 |
| 入口收了 | 明天照着抄的那**一个**调用点 |

§5.1 要的是后者。

## 三、改了什么

| 文件 | 改动 |
|---|---|
| `app/media/service.py` | 新增 `evidence_assets_for()`(SQL 粗筛 + 同一个判定)、`usable_asset_count()`、`SHARED_SCOPE`、`_COARSE_FILTER_COLUMNS` |
| `app/attributes/service.py` | `run_extraction` 改走入口;两条"空"错误分开报;不再持有未过滤的行 |
| `app/media/evidence_rules.py` | 修掉「这两列今天不存在」那句过期说明(14-16 已落库) |
| `tools/verify_delivery.py` | 接线门禁按相对路径而不是文件名去重(见第六节) |
| `tests/pure/test_a45_batch14_19_evidence_query.py` | **新增,10 条守卫** |
| `tests/test_a45_batch14_19_evidence_query_db.py` | **新增,6 条真库用例,一次都没跑过** |
| `tools/mutate_batch14_19.py` | **新增,15 条变异,一次全红** |
| `tests/pure/test_a45_batch14_7_evidence_class.py` | 两条守卫一般化(见第五节) |
| `tools/mutate_batch14_7.py` | 三条锚点跟着搬家;P1 退役并写明理由 |

### 判定仍然只有一处

SQL 只做**粗筛**,每一条都被判定蕴含:

```
status = READY                    判定里 status_is_ready 是与项
generation_task_id IS NULL        溯源列非空必然派生成 GENERATED_RESULT
generation_candidate_id IS NULL   同上
source NOT IN (AI_GENERATED,      前者派生 GENERATED_RESULT
               PLATFORM_SYNC)     后者派生 CHANNEL_DERIVATIVE
```

最终一律由 `asset_is_extraction_input()` 说了算。全写成 WHERE 子句等于给
`evidence_class` 造第二个判定点,而两个判定点漂移时没有人会发现。

### 两个方向的失效代价不对称

```
粗筛多放一行   Python 判定兜住了,只是多读几行 —— 可接受
粗筛多挡一行   那张图静静不进识别输入,而"本该识别却没识别"没有任何地方会报
```

后者是这一批最需要盯的方向,所以粗筛用到的列被收进一张显式的表,
加一列必须先去补蕴含证明。

## 四、本批验到了什么、验不到什么

### 验不到:**SQL 一次都没执行过**

这台机器没有 sqlalchemy。`is_(None)` 写成 `is_not(None)`、`not_in` 写成 `in_`,
两者的 AST 形状几乎一样而结果正好相反 —— **纯层守卫分不出来。**

真验证在 `tests/test_a45_batch14_19_evidence_query_db.py`:对同一批构造出来的
素材行,比较「粗筛 + 判定」与「全量 + 判定」是否给出**完全相同**的 id 集合。
参照那一路刻意不用粗筛 —— 拿粗筛去验粗筛,写反了两边一起反,而测试全绿。

**那 6 条一次都没跑过。** 下一台有库的机器第一件事就是它。

### 变异清单里刻意缺席的几条

`is_(None)` → `is_not(None)` 这类 SQL 语义变异**没有列进 `mutate_batch14_19.py`**。
它们在这台机器上必然 GREEN(纯守卫看不见 SQL 语义),而列一条明知抓不住的
变异进去,只会让"15/15"这个数字变成一句谎话。

## 五、点名做法第五次

14-7 那两条守卫把「过滤写在 `run_extraction` 里」当成了不变式,搬家那天双双
变红 —— 而口径其实更严了。这是同一个形状的第五次(前四次见 §3.31 / §3.32)。

改法是**参数化宿主**:`WHITELIST_HOST = (模块, 函数名)` 写在文件顶部,
两条守卫都对它下断言。搬家再发生一次时改两行,断言本身不必动。

### 顺带退役了一条变异,并说清楚不是漏了

14-7 的 P1(「过滤结果算出来了但没赋回 `assets`」)本批之后跑 GREEN。
**原因不是守卫漏了,是被建模的那个缺陷已经不可能发生**:旧形状里 `assets`
先被绑成未过滤结果,删掉赋值之后代码照跑、用的是未过滤那份(静默致命);
现在 `assets` 只剩一个绑定点,删掉它是 NameError。

退役,并在正面补一条守卫钉住"绑定点唯一" —— 那条性质一旦不成立,
P1 那个洞就重新打开。

## 六、修了接线门禁的一个盲点

`verify_delivery.py` 判断"是不是模块内部互调"时按**文件名**去重。于是
`app/media/service.py` 的函数被 `app/attributes/service.py` 调用时被当成
自己调自己 —— 判定未接线。

而 `service.py` 是本仓最常见的文件名。**这条门禁在最容易发生接线遗漏的
那一批模块上恰好是瞎的。**

更要紧的是失败方向是**假红**:被拦下来的人最省事的做法是把条目从
`WIRED_MODULES` 里删掉 —— 门禁不是被修好,是被静静关掉。改成按相对路径去重。

## 七、阶段 2 还剩什么

| 项 | 卡在哪 |
|---|---|
| `evidence_class` 存储列 + CHECK | 要给存量行**回填**派生值,而回填只能在有库的机器上当场验 |
| 新去重键 `(spu_id, sha256)` | 要先给 `media_assets` 加 `spu_id` 外键,那是又一次归属改造 |
| 按颜色上传 UI | 无 node_modules,写了也跑不了 Vitest |

前两项都要真库,按本轮"跳过需要人工验证的"的口径没有做。

**验收侧一条都没验过**:AC-03 / AC-04、「识别输入不含任何 AI 图(付费调用
清单可证)」——那句"可证"要的是真实账单,不是测试。

## 八、门禁

```
纯逻辑          2325/2325   0 失败,7 跳过
本批变异        15/15       一次全红
14-7 重跑       23/23       三条锚点搬家后仍全红(P1 已退役)
锚点            311/311     16 份脚本
audit-guards    482 个守卫
交付            13/13
样例数据        5/5
导入            388 个文件
```

**仍未执行**:前端四条、`alembic upgrade/downgrade`(0037 / 0038 / 0039)、
**真库 pytest(含本批那 6 条)**、Ruff / lint-imports 本体、Docker、Playwright。

**本批没有动迁移链**(head 仍是 `0039`)。
