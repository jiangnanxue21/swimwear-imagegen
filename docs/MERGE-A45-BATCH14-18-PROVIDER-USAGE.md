# A45-batch14-18:计费量问厂商要,并记下这个数是谁说的

基线 A45-batch14-17。对应 `docs/REVIEW.md` §12.1 **任务 9「FASHN ProviderCall
持久化与 Usage」**,阶段 1 六项里的最后一条代码项。

---

## 一、这一批修的是什么

`providers/fashn.py` 从 `x-fashn-credits-used` 响应头解析出厂商实际扣的额度,
抄进候选图的 `metadata["credits_used"]` —— 然后**全仓再没有第二处读它**。
本批之前 grep 这个字段,除了测试一个调用点都没有。

台账那边记的一直是:

```python
billable_units=max(outcome.provider_count, 1)   # 我们收到几张图
```

两个数不是同一个量。官方参考表(`docs/vendor/fashn-skill/reference.md`「Credits」):

```
tryon-max  balanced  1k:2  2k:3  4k:4     (× num_images)
           quality   1k:3  2k:4  4k:5
```

一张图 **2 到 5 个额度**,取决于 `FASHN_RESOLUTION` 与 `generation_mode` ——
而这两个旋钮运营在设置页都能改。

所以这不是"记得不够精确",是**记错**:

| | |
|---|---|
| 倍数 | 2 到 5,跟着配置浮动,没有一个常数能折算回去 |
| 方向 | **恒定是少记** |
| 征兆 | 没有。预算横幅一路绿着,账单是它的好几倍 |

`REVIEW.md` §10.2 第 5 条准入是「用量记录与 Provider 后台账单条数一致」——
按旧写法不可能对得上,对上了是运气。

### 顺带发现的一个还没人踩的坑

`fetch_results` 把每条 prediction 的额度抄在**它产出的每一张**候选图上
(那是给排查用的现场)。于是 `num_images=4` 那一次,4 张候选各自带着同一个
per-prediction 总额 —— 直接 `sum()` 就是 **4 倍高估**。

今天没人踩进去,只是因为没有人读过这个字段。它会在第一个读者出现时立刻生效,
而第一个读者通常不知道那份数据是怎么写进去的。

---

## 二、改了什么

| 文件 | 改动 |
|---|---|
| `app/core/enums.py` | 新增 `UnitsSource`(`provider` / `inferred`)。放 core 是因为模型层和 Provider 层都要认它,而 core 是两边都能依赖的那一层(契约一) |
| `app/providers/base.py` | `ProviderUsage` 数据结构;`settle_billable_units()`(**唯一**一处"谁说了算"的判定);基类钩子 `usage_from_candidates()`,默认报"厂商没报" |
| `app/providers/fashn.py` | 覆写钩子:按 `prediction_id` 去重求和;任一条出了图却没带额度 → 整笔退回推算 |
| `app/models/generation.py` | `ProviderUsageRecord.units_source` 列 + 与迁移同名的 CHECK |
| `backend/migrations/versions/0039_usage_units_source.py` | **新迁移。动了迁移链,head 从 0038 变成 0039** |
| `app/services/generation_service.py` | `record_usage(units_source=...)`,默认 `inferred`;更新路径让来源跟金额同进同退 |
| `app/tasks/generation_tasks.py` | 接线:先问厂商,两数不一致记 info 日志 |
| `tools/verify_delivery.py` | `WIRED_MODULES` 登记两个新函数 |
| `tests/pure/test_a45_batch14_18_provider_usage.py` | **新增,19 条守卫** |
| `tools/mutate_batch14_18.py` | **新增,19 条变异,一次全红** |
| `tests/pure/test_a45_batch12_4_fixes.py` | 一条老守卫一般化(见第四节) |

### 判定的形状

```
厂商报了      → 用厂商的数,标 provider
厂商没报      → 退回推算值,标 inferred
厂商报了 0    → 用 0,标 provider     ← 和"没报"是两件事
报得不完整    → 整笔退回推算,标 inferred
```

最后一条值得单说:某条 prediction 出了图却没带额度时,求和的结果是一个
**偏小**的数,而它看起来和真数一模一样。少记钱这个方向的错误没有人会去发现,
所以整笔作废、退回推算并如实标记。

---

## 三、为什么要落一列而不是只写日志

§10.2 第 5 条要人拿这张表去和厂商账单核。对不上时第一个问题是:

> 这个数是厂商说的,还是我们猜的?

前者对不上要去查厂商,后者对不上是我们算错了 —— **两条路完全相反**。
不落这一列,每一行看起来同样权威,而在本批之前**每一行都是猜的**。

默认值方向也是刻意的:`record_usage(units_source=...)` 默认 `inferred`,
忘了接线的表现是台账诚实地说"这是估的",而不是一张估算表冒充和账单同源。
和 `is_simulator = True` 同一个取舍。

### 存量回填成 `inferred`,和迁移 0034 拒绝回填不矛盾

0034 写着「不回填历史行的 `billing_key`:回填等于替过去的账做一个今天才定下来
的判断」。区别在于**存量的正确取值今天可不可知**:

- 0034 的 `billing_key`:不可知,哪一行才是真的取决于厂商账单;
- 本批的 `units_source`:可知 —— 读厂商额度的代码本批之前不存在,
  所以每一条历史流水都出自 `max(provider_count, 1)`。

`inferred` 不是猜测,是对**我们自己那条代码路径**的陈述。变异 M4 钉着这条的
边界:一条按 `billable_units > 1` 挑行的 UPDATE 会把它变回猜测。

---

## 四、一条老守卫因为「点名做法」第四次变红

`test_the_ledger_bills_what_the_provider_produced_not_what_we_saved` 变红了。
它断言的是 `billable_units=` 那个实参的**字面量里有没有 `provider_count` 这几个字**。

第四次同一个形状(前三次:14-16 的 `heads == {"0037"}`、14-4 的
`import { useUrlSeed }`、14-10 的 `"setAudience" in page`)。

这一次尤其说明问题:**那条守卫的名字说的是不变式,断言查的是字面量**。
名字对、断言错,所以没人怀疑过它。

改法:把实参连同它引用的局部变量的赋值一起摊平,再问"这笔账追得到 Provider 吗",
两条合法路径(厂商自述 / `provider_count`)取并;另加一条反向断言禁止追到 `stored`。
变异 W3 验的就是它还咬得住。

一般化补在 `DECISIONS.md` §3.32 第四节:

> **守卫的名字和它的断言必须说同一句话。** 名字里写的是不变式而断言查的是
> 某个字面量时,以名字为准去改断言 —— 不是反过来把名字改窄。

---

## 五、本批验到了什么、验不到什么

### 验到的(真的调函数,不是 AST 形状)

19 条守卫里大半是零依赖直接跑的:去重、缺数作废、空候选不冒充、负数夹取、
来源取值封闭、基类默认不冒权威。变异 19/19 一次全红。

### 验不到的:**厂商到底会不会那么答**

这是本批最要紧的一条免责。判定验得很干净,但下面三件事这里一个字都证明不了:

```
一、每一次 status 响应是不是都带 x-fashn-credits-used
二、带的是本次消耗还是累计消耗   ← 是累计的话去重逻辑要换
三、失败的 prediction 是不是真的不计费(文档这么说,没验过)
```

任何一条不成立,`units_source='provider'` 的那些行就是错的 —— 而它们比
`inferred` 更容易被当成权威。**真连之前先跑一次单张、拿后台账单对一次。**

### 还报不出真实用量的三类付费调用(欠账,守卫记着)

| 调用 | 今天 | 为什么本批不接 |
|---|---|---|
| FASHN 轮询 / 取结果 | `inferred` | 厂商每次真的又调了一次,但那几次不单独计费 |
| 评分(vision) | `inferred` | 响应体里有 `usage`,**是 token 不是次** |
| 属性识别(vision) | `inferred` | 同上 |

vision 那两类刻意不接:价目表里 `attribute_extract` / `vision_score` 今天配的是
**每次调用**的价,改成 token 会在不改任何配置的情况下**静默改变已配价目表的
含义** —— 金额一夜之间差几个数量级,而没有任何地方会说为什么。

`test_the_paid_paths_that_still_cannot_report_are_named_here` 记着这笔账,
接线那天它会红,那时更新 STATUS 的欠账表并删掉它。

---

## 六、门禁

```
纯逻辑          2315/2315   0 失败,7 跳过(缺 pydantic / sqlalchemy)
本批变异        19/19       一次全红
锚点            297/297     15 份脚本
audit-guards    476 个守卫  反向断言窗口全封闭
交付            13/13
样例数据        5/5
导入            385 个文件
```

**仍未执行**:前端四条(tsc / ESLint / Vitest / build)、`alembic upgrade/downgrade`
(0037 / 0038 / **0039** 从未执行过)、真库 pytest、Ruff / lint-imports 本体、
Docker build、Playwright 浏览器。

**本批动了迁移链**(head 0038 → 0039)。并行线上还有人加 revision 的话,
先对一次 `down_revision`。
