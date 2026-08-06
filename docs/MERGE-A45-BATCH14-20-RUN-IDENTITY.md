# A45-batch14-20:识别 run 的身份落库与 §9.2 幂等接线

## 一句话

`run_state.py`(14-12)与 `scope_fingerprint.py`(14-9)两个判定模块写完并
穷举验过之后**接不上线整整八批**,每一批的理由都是同一句:§4.6 的五个列
一个都不存在。迁移 0040 把列落下去,本批接线。

判定本身**一个字没动**。

## 改了什么

| 文件 | 改动 |
|---|---|
| `migrations/versions/0040_extraction_run_identity.py` | 新增。五列 + `spu_id` 外键 + 两条索引(其中一条是**部分**唯一索引) |
| `app/models/attribute.py` | 五列落 ORM;派生属性 `status` 删除;索引谓词用 `unique_index_predicate()` 生成 |
| `app/extractors/base.py` `mock.py` `vision.py` | 新增 `declared_versions()` 钩子 |
| `app/attributes/service.py` | `run_extraction` 接上幂等与指纹;终态写回 `status` 列 |
| `tools/verify_delivery.py` | `run_state` 补登记四个,`scope_fingerprint` 新登记两个 |

## 三个判断,以及为什么这么判

### 1. `status` 不回填

回填要按三个计数重新推导每一行,而那个推导是 `terminal_status_for` ——
一个**判定**。写成 SQL 的 CASE 等于给它造第二个判定点,而两个判定点漂移时
没有人会发现(§5.1 白名单那一批为同一个理由拒绝把过滤写成 WHERE 子句)。

把它 import 进迁移更糟:迁移是**冻结在时间里**的脚本,而判定会演进。今天
import 它,三个月后规则改了,在一台新库上 `alembic upgrade head` 会用
**新规则**去写**旧行**,而这件事不会有任何提示。全仓 40 份迁移没有一份
import 过 `app.*`,这条不由本批开口子。

于是 `server_default='FAILED'`,存量行落在**不放行**那一侧 —— 既不进事实
合并也不占幂等键。方向有代价(一次真的成功过的旧 run 会显示成失败),
选它是因为反方向更贵:默认 COMPLETED 会让一次从来没有被判定过的 run
以「算数」的身份参与两件要花钱的事。§3.1 另说明系统尚未正式使用。

### 2. 建不出键就留空,不编一个

三种情况一律留空:

```
只识别指定素材    canonical_scope 只有共享/指定颜色/全部三种形状,
                  「任意素材子集」不是其中之一
商品没有 spu_id   老建档路径(阶段 1 剩余项)还不写这一列
抽取器报不出版本   取响应里的版本 = 付过钱才算得出键,而键要挡的是付钱前那两下
```

留空的代价是这几条路径退回本批之前的行为(双击付两次钱),**不是**错误地
拦住请求。方向是刻意的:少挡一次的代价是一次重复付费,挡错一次的代价是
一个再也识别不了的商品。

第二条正是 14-12 欠账守卫当初点名的那条捷径(「让门禁变绿最省事的做法是
拿 product_id 凑一个假的 SPU 作用域」),现在有正向守卫盯着它。

### 3. 索引谓词在迁移里是字面量,在 ORM 里是生成的

迁移冻结、判定演进,所以迁移写字面量。正因为冻结,
`test_the_migration_predicate_is_the_twin_of_the_pure_one` 才必须存在:
`KEY_OCCUPYING_STATUSES` 哪天变了它会红 —— 那时该**新写一条迁移重建索引**,
不是回来改那一行。

唯一索引必须是**部分**的。全表唯一的后果是一次 FAILED 之后同样的输入再也
建不出第二个 run,而输入没变、模型没变、字段没变 —— 那正是重试的定义。

## 五条欠账守卫按它们自己的约定被换掉

| 守卫 | 处理 |
|---|---|
| `14-12 ::test_the_idempotency_half_cannot_be_wired_yet_...` | 删,换四条正向 |
| `14-9  ::test_this_module_cannot_be_wired_yet_...` | 删,换三条正向 |
| `14-13 ::test_the_wired_half_is_registered_and_the_unwired_half_is_not` | 反向断言下移到 `facts_stale`/`changed_scopes` |
| `14-13 ::test_listing_the_failed_scopes_needs_a_column_...` | 收窄。`requested_scope` 落库了但**不是**它要的那一列 —— 那一列记「请求哪个作用域」,§11 要的是「跑完之后哪个作用域全军覆没」,一个写在付钱之前、一个只有跑完才知道。拿它顶数会让重试范围宽成整批 |
| `14-12 ::test_the_model_property_asks_the_pure_verdict_...` | 参数化宿主(`VERDICT_HOST`),这是「点名做法」第六次 |

## 变异:20/20 + 34/34

`tools/mutate_batch14_20_run_identity.py` 第一轮只红 12/20。**8 条 GREEN 全是守卫上的
真洞**,不是变异写错了,逐条补完才到 20/20。值得记下来的三个:

- **docstring 被当成代码**(P2、F4)。`VisionAttributeExtractor.declared_versions`
  的文档里写着「不取 `result.model_name`」,而一条查 `result.` 的断言把那句
  **解释**当成了实现,守卫红在一个完全正确的代码上;反方向同样成立 ——
  `_run_identity` 的文档里出现 `imported_url_trusted=True`,把断言喂成了平凡真。
  这是 §3.26 第四节那句话的第七次成立,修法是剥 docstring(`_code()` 助手)。
- **宽断言在宿主搬家后变松**(D3)。`"row.status" in body` 原来只可能命中那个
  比较,本批之后终态自己也写这一列,于是它变成平凡真。改成点名那次比较。
- **模块级赋值绕过类体扫描**(S2)。`ProductAttributeExtraction.status = property(...)`
  写在文件底部同样能盖住那一列,而只扫类体的守卫一个字都不会说。

14-12 那份脚本的 W1 也跟着宿主搬了家(原来打模型属性,现在打写入方),
并在搬完之后暴露出同一个洞:变异保留了判定调用、只把返回值丢给 `_unused`,
而守卫只查「调用出现了没有」。补成断言那次赋值的右边就是判定本身。

## 验不到什么

三条真库语义在这台机器上一次都执行不了:

```
部分唯一索引到底建没建起来      纯层只能看到 ORM 里那行声明
IntegrityError 那一路捞不捞得到赢家   要两条真的并发事务
server_default 对存量行生不生效  要一张真的有旧行的表
```

规格写在 `tests/test_a45_batch14_20_run_identity_db.py`(11 条),**本轮
一次都没跑过**。没有把它们改写成纯层守卫凑数 —— 一条只验了「源码里出现过
IntegrityError」的守卫会让 20/20 变成一句谎话。

先跑 `test_the_partial_unique_index_lets_a_failed_run_be_retried`:
它验的是那个「部分」,写丢了的表现是一个再也识别不了的商品。

## 上线顺序

```
1. alembic upgrade head          迁移 0040(纯增列,无回填)
2. pytest tests/test_a45_batch14_20_run_identity_db.py -v
3. 确认 pg_get_indexdef 里的谓词与 unique_index_predicate() 一致
```

第 3 步不是形式:ORM 声明、迁移字面量、真的建出来的索引,三者之间任意两个
漂移都不报错,只是让「这个键被占了吗」在不同环境下有不同答案。

## 本批之后仍然欠着的

- **§4.6 的异步那一半**:Celery 任务 + cancel 端点 + `QUEUED` 那一档。
  要 Redis + worker,不是这台机器能验的。今天建行即 `RUNNING`,
  `cancel_requested=False` 是如实描述现状。
- **§11 的失败作用域清单**:欠 `failed_scopes` 一列,见 14-13 那条守卫。
- **事实侧的指纹列**:`facts_stale` / `changed_scopes` 要等属性值行也带上
  `input_fingerprint` 才能接线,那是阶段 4。
- **老建档路径不写 `spu_id`**:阶段 1 剩余项。在它落地之前,
  CSV 导入与 `create_product` 建出来的商品拿不到幂等保护。
