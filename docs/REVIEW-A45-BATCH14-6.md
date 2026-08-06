# REVIEW:A45-batch14-6(阶段 P0:1/6 -> 4/6,真库 pytest 第一次跑起来)

这一批不是走读,是**把前提补齐**。从 batch10 起,每一份评审都写着同一句话:
「这台机器没有 PostgreSQL / node_modules / pytest」。这台机器上它们都装得起来。

> **一句话结论:P0 从 1 项通过推到 4 项通过。真库 pytest **2307/2307**、
> Alembic 升降级全程走通、前端四件套里三件全绿。那批「已写、一次都没跑过」的
> 用例第一次跑就掉出 3 条红 —— **三条是三种不同的东西**,只有一条是真 bug,
> 一条是代码对而用例问法不对。分清楚这三种,比修好它们更要紧。**

---

## 一、机器补了什么

    PostgreSQL 16.14   apt(第一次失败是索引过期报 404,`apt-get update` 之后就好了)
    后端依赖           pip install -e ".[dev]" —— pytest 9.1.1 / ruff 0.16.1 / psycopg / celery ...
    前端依赖           npm ci,396 个包
    两个库             imagegen(主) + imagegen_test(测试,`_test` 后缀是夹具的护栏)

`tests/conftest.py` 的两道护栏都满足了才跑:库名以 `_test` 结尾、
显式设 `ALLOW_DESTRUCTIVE_TEST_DB=1`。那批夹具会无条件
`DROP SCHEMA public CASCADE`,护栏不是形式。

## 二、P0 现状

| 项 | 之前 | 现在 |
|---|---|---|
| P0-1 真库 pytest 全量 | 未验证 | ✅ **2307/2307** |
| P0-2 Alembic 升降级 | 未验证 | ✅ 0001 → head → base → head |
| P0-3 前端 typecheck/lint/Vitest/build + Docker | 未验证 | 四件套三件全绿,**Docker build 做不到** |
| P0-4 R-04 / R-05 | ✅ | ✅ |
| P0-5 重复扣费残窗 | 未验证 | ✅ 演练 11 条跑通 |
| P0-6 租约 fencing | 未验证 | **Redis 连不上** |

P0-2 是整程走的:`0001 → head`(36 条迁移)、`head → base`(全部 downgrade)、
再 `base → head`。降级那一半是通常会烂掉的一半,这次没烂。

## 三、第一次跑真库,掉出来的 3 条

**这一节的重点不是"修好了三条",是这三条属于三个不同的类别。**
把它们都叫"测试挂了"会让最要紧的那一条淹掉。

### 3.1 真 bug:422 的 `loc` 指向一个不存在的字段

`test_an_illegal_variant_code_tells_the_form_which_row`。

接口字段叫 `color_variants`(`schemas/spu.py` 明写),而 422 返回的
`loc` 是 `variant_codes[1]`。前端靠 `error.fields[].loc` 高亮到具体那一行,
拿到一个表单里根本没有的名字 —— 高亮不到任何一行,表现退回
"整体弹一句『编码不合法』",而一个三颜色的建档表单里运营要自己一行行试。

根因在 `services/spu_service.py::_translate()`,而**它的注释是错的**:

> `SkuPlanError.field` 是 `color_variants[0].variant_code` 这种点分路径 ——
> 它和 pydantic 的 `loc` 同源,所以原样进 `FieldProblem.loc`,**不做转换**。

纯层 `sku_matrix.expand()` 抛的实际是 `variant_codes[1]` —— 那是**它自己的
形参名**。纯层这么做是对的:让一个零依赖模块知道 HTTP 载荷长什么样,
等于把接口形状漏进纯层。错的是边界那一句"不做转换":
**两边从来就不同源**,这个决定建立在一个不成立的前提上。

已在 `_translate` 加一张映射表(`variant_codes` → `color_variants`),
并下钻到列:`variant_codes[1]` → `color_variants[1].variant_code` ——
只说第几行的话,表单仍要在 `variant_code` / `working_name` 两个输入框之间猜。

这条又是那个熟悉的形状:**注释宣称的事情,代码没有做**,
而唯一会发现它的那条用例躺在"已写、一次都没跑过"里。

### 3.2 代码对,用例问法不对

`test_the_billed_failure_gate_opens_once_and_then_stops` —— 而且它就是
**P0-5 自己那条演练**,失败信息写着「那道闸在库里不生效」。

查下来闸是好的。`_claim_in_flight` 每次认领都把 `provider_call_at` 清成 NULL,
所以第二次 claim 之后库里那行是 `IN_FLIGHT` + 未派发。`receipt_route()`
走的是 in-flight 那一支,答 `EXECUTE` —— 它的注释写得很清楚:
「这一次压根没跑到花钱那一步,拿它去扣自动付费的额度是把两个数混成一个」。

而用例的文档字符串说的是「第二次**跑完也失败了**之后要不要再跑」。
它到 `_claim` 就去问路了,**少写了第二张 FAILED_BILLED 回执**。
补上之后 `executions=2` → `_billed_again_or_stop(2)` → `NEEDS_RECONCILIATION`。

顺带验了会不会漏钱:worker 在派发前死掉时,回收会多走一次未付费的认领
(`executions` 涨到 3),但实际发生过付费的执行仍然是 2 次 —— 上限守得住。

**这一条如果按"闸坏了"去改代码,会把一条正确的判定改坏。**

### 3.3 陈旧用例

`test_model_template_upload_and_listing` 的载荷没带 `audience`,而它在
batch13 的身份规范化里变成必填(前端表单早就标了 required),422。
用例没跟上,补一个字段。

---

## 四、顺带量出来的两个数(没人报过)

**`make lint` 是 47 条,不是 337 条。** STATUS 里记的 337 是全仓 `ruff check .`
的口径;门禁真正跑的是 `ruff check app tests`,47 条。分布:

    19  I001   未排序 import        体例,可自动修
    14  E501   超长行               体例
     3  B023   闭包捕获循环变量      **真 bug 类**
     2  B007   未用的循环变量
     2  E741   有歧义的变量名(l / I / O)
     1  F841   未用的局部变量
     1  UP037  多余的引号注解        可自动修

B023 那 3 条要逐条看:循环里建的闭包会全部捕到最后一次的值,
而它的表现通常是"批量操作里只有最后一件生效",不报错。

**前端产物 1.86 MB 单块**(gzip 602 KB),没有任何代码分割,
vite 直接报了 chunk 过大警告。不影响 P0,但人工测试第一屏会有感。

---

## 五、跑过的

| 门禁 | 结果 |
|---|---|
| **真库 pytest** | **2307/2307**(第一次执行) |
| **Alembic 升降级** | **0001 → head → base → head 全程** |
| 纯逻辑 | 2066/2066,0 skip |
| 四份变异 | 34/34、16/16、17/17、20/20 |
| audit-anchors | 87/87 |
| verify-delivery / imports / sample-data | 13/13、356、5/5 |
| Vitest | 66/66 |
| tsc --noEmit | 干净 |
| ESLint | 0 errors(5 条既有 warning) |
| **前端 build** | **成功**,26.2s |
| 前端 syntax-check | 84/84 |
| `make lint`(ruff) | **47 条,红** |

---

## 六、没有做的

1. **P0-6 租约 fencing** —— 差一个 Redis。这台机器 apt 装得上,
   上一轮只装了 postgresql,漏了 `redis-server`。这是唯一挡着它的东西。
2. **P0-3 的 Docker build** —— 容器里没有 docker daemon,**这台机器做不到**。
   建议把这一项和"未验证"分开标:**"环境不可达"和"还没跑"对读的人
   是两个意思**,前者要换机器,后者是欠的活。
3. **ruff 47 条** —— 一条没改。B023 那 3 条要逐条读,其余分两批
   (可自动修的一批、手工的一批),不该混在一次里。
4. **arch-check(`lint-imports`)** —— 装了 `import-linter` 但没跑通命令,
   `.importlinter` 契约还没验过。
5. **前端代码分割** —— 1.86 MB 单块。
6. **5 条 `react-hooks/exhaustive-deps` warning**(batch14-5 就欠着) ——
   `CopyTab` 那条尤其像真 bug:`useEffect` 漏了四个依赖,
   表现会是切商品时文案不刷新。
7. **Celery worker / 真 Provider / 端到端冒烟** —— 一次都没跑过。
   `make smoke` 需要起 worker + Redis。
8. **人眼看界面** —— batch14-5 改的 4 个组件(Tag → BrandTag)
   过了四道工具,仍然没有人在浏览器里看过。
