# A45-batch14-12:识别 run 的终态、取消与幂等键(§4.6 / §9.2)

> **本批原本也叫 14-11。**同一个号被三条线同时占用,硬冲突只有
> `tools/mutate_batch14_12.py` 一处(同名不同物)。按 §3.11 补号:
> 先到的两批留在 14-11,本批改成 14-12,守卫与变异脚本一并改名。
> 一般化见 `DECISIONS.md` §3.28 第〇节。

> **一句话结论:「这次识别算成功、部分成功还是失败」此前由**前端**回答,
> 而后端从来没有派生过这个值 —— 全部失败的 run 与全部成功的 run 在库里
> 长得一模一样。前端那三档还判漏了一种:跑了一半但一张都没失败时,
> 它显示「识别完成」。判定收进零依赖模块并接线;§9.2 的幂等键判定一并验完,
> 但**接不了线**,原因写成了守卫。
> 纯逻辑 2185/2185、变异 34/34 验红、锚点 184/184、交付 13/13、导入 368、
> 样例 5/5、前端 syntax-check 84/84、`lint_offline` 349 文件无发现。
> **真库用例池仍然是 60 条,没有增加一条。**
> **本批最值钱的两条不在这一批的守卫里,在别人的守卫里 —— 见第六节。**

---

## 一、动了什么

| 文件 | 改动 |
|---|---|
| `app/attributes/run_state.py` | **新建。**终态派生 + 转移表 + 复用判定 + §9.2 幂等键,零依赖 |
| `app/core/enums.py` | 新增 `ExtractionRunStatus`(六值) |
| `app/attributes/scope_fingerprint.py` | `_field` 提升为公开的 `length_prefixed`(幂等键要用同一个编码器) |
| `app/models/attribute.py` | 新增派生属性 `status`(**不是列**) |
| `app/attributes/service.py` | 全部失败时补写 `error_code`;新增常量 `EXTRACTION_ALL_FAILED` |
| `app/api/attributes.py` | 合并证据的判据换成 `run_is_authoritative` |
| `app/schemas/attribute.py` | 出参新增 `status`,默认值 fail closed 到 `FAILED` |
| `frontend/src/api/attributes.ts` | 联合类型 + 两张表(`RUN_STATUS_LABEL` / `RUN_STATUS_NOTICE`) |
| `frontend/src/components/workbench/AttributeTab.tsx` | 删掉自己判三档那段,改成只挑语气 |
| `tests/pure/test_frontend_contract.py` | 契约表登记三行(两张文案表 + 一个联合类型) |
| `tests/pure/test_gate_a_guards.py` | **改了两条既有守卫** —— 见第六节 |
| `tests/pure/test_a45_batch14_12_run_state.py` | **新建。**37 条守卫,其中 3 条穷举 |
| `tools/mutate_batch14_12.py` | **新建。**34 条变异,先于守卫写 |
| `tools/verify_delivery.py` | `WIRED_MODULES` 只登记接了线的两个函数 |

## 二、开口有多大

`AttributeTab.tsx` 原来这样判:

    (succeeded === 0) ? 失败 : (failed > 0) ? 部分成功 : 完成

三档全在前端算 —— 硬规则 4 第一句就禁止这件事。而后端那一侧**连一个可读的
结论都没有**:`run_extraction` 跑完之后 `error_code` 是 NULL、接口返回 200,
全部失败与全部成功的差别只有两个计数。于是「最近哪些识别是失败的」
这个问题在 SQL 里问不出来,而按计数反推等于把判定再抄一份。

更要紧的是那串三元运算**判漏了一种**。后端的循环可以在跑完之前停下
(worker 被回收、将来的 cancel),那时:

    succeeded = 2, failed = 0, image_count = 5

`failed > 0` 是假,于是落在最后一档 ——「识别完成:5 张图,成功 2 条」。
**一次跑了一半的识别显示成功**,而缺的那三张不会有任何人回来补。
变异 T1 就是这条病灶的复现:它把这个 bug 原样搬进后端。

## 三、落码时和 PRD 对不上的三处

**一、§9.2 的「唯一约束」按字面落码会把商品锁死。**原文是「落
`idempotency_key` 唯一约束,数据库裁决」。做成全表唯一之后,一次 FAILED
之后**同样的输入再也建不出第二个 run** —— 输入没变、模型没变、字段没变,
而那正是重试的定义。运营看到的是一个再也识别不了的商品,唯一的解法是
去改点什么(换个字段范围、传张图)来骗过那条约束。

处理:占键的只有还会被复用的那几档(`KEY_OCCUPYING_STATUSES`,
今天是 QUEUED / RUNNING / COMPLETED),索引写成部分唯一索引,谓词由
`unique_index_predicate()` 生成 —— 它是那条 DDL 的**孪生**,与
`media/evidence_rules.py` 的 CHECK 孪生同一套办法。而且这份名单
**从 `reuse_verdict` 派生**,不是手写的第二张:手写的那一版会和判定漂移,
表现是「代码说该新建一个 run,库说这个键被占了」,接口报一个和用户
动作毫无关系的 409。

**二、`model_version` 只能取配置里的那个。**§9.2 没说取哪来的。本仓有两个
来源:配置(`EXTRACTOR_MODEL_NAME`)与模型响应(`result.model_name`)。
取后者的话,键只有在**付过钱之后**才算得出来 —— 而幂等键要挡的正是双击
与网络重发,那两件事都发生在付钱之前。`run_extraction` 今天写的恰恰是
`row.model_name = row.model_name or result.model_name`,照那个来源建键的
实现**编译得过、测试也绿**,只是每一次双击都买两次。所以本模块拒绝用空的
模型/Prompt 版本建键,逼调用方去配置里取。

**三、`PARTIAL_SUCCESS` 是终态,而且不占键。**§4.6 只把它列成一个状态,
没说是不是终态。放进非终态的话,「重试失败的那几张」会变成在同一行上继续写,
而那一行的 `input_fingerprint` 是第一次跑时的快照 —— 重试之后的证据挂在
一个过期的输入上。判成「可复用」则更直接:失败的那几张永远不会被重跑,
而「按颜色重试」是 §13 阶段 3 点名的交付项。

## 四、一处实现自己踩中的坑(留成了变异 S4)

作用域的第一版把「这个颜色在不在作用域里」写成在 token 里找子串:

    if k is not SHARED and length_prefixed(k) in scope.token

长度前缀让**大多数**伪造失效,但不是全部:一个 id 恰好长成 `x1:a` 的颜色,
会让 `1:a`(颜色 a 的编码)成为它的子串,于是颜色 a 的指纹被算进一个
根本不含 a 的键 —— 两个不同的请求拿到同一个键,第二个被当成重复直接复用。

这是「文件里出现过这串字 ≠ 这件事成立」换到数据编码上的样子,本仓库
已经在三处栽过(batch13-3 的 M2/M11、batch14-2 的 N15)。改成结构化的
`Scope`(token 与 `variant_ids` 一起走),成员判定读那个真实的元组。
守卫用伪造 id 钉住,变异 S4 把它改回子串写法。

## 五、这一批的变异:34/34,但第一轮全红这件事本身要说明

`34/34 第一轮全红`——按 batch14 那次的教训,这个数字**好得可疑**。
逐条核过三件事:

    每条都由**它自己那条守卫**报出来        是(脚本第三列打印了名字)
    没有一条是导入期炸掉冒充的红            是(crashed 检测未触发)
    没有守卫在未变异的树上就是红的          是(基线 37/37 全绿)

但还有第四件事,它是这次真正的收获:**变异脚本只跑本批那一份套件,
所以它天生看不见「本批改动打破了别人的守卫」。** 那两条正是本批
最值钱的发现,而它们是 `make check-offline` 跑全量时掉出来的,
不是 34 条变异跑出来的。见下一节。

## 六、两条既有守卫,一条钉错了东西,一条一直是假绿

两条都在 `tests/pure/test_gate_a_guards.py`,都属于 A2(「空结果不能
当成识别过了」)。

**一、`test_frontend_does_not_report_a_total_failure_as_success` 把临时办法
钉成了规格。**它断言前端源码里出现 `succeeded_count ?? 0) === 0` ——
也就是**前端自己判三档**那段代码。A2 要的性质是「全失败不能显示成功」,
而这条守卫钉的是「用哪一行代码做到它」。于是判定搬到后端(硬规则 4
要求的那个方向)之后它变红,**而它变红的原因是病治好了**。

守卫钉实现不钉性质时就会这样:它把当时那个凑合办法变成了以后不许动的
规格。现在钉的是性质本身,两头一起钉 —— 后端算好的那一档真的被读了,
error 语气真的还在(全失败用 success 弹一下,运营的结论是「识别过了」)。

**二、`test_single_item_extract_skips_apply_evidence_when_all_failed`
此前是假绿。**它断言源码里出现过 `succeeded_count` 且出现在 `apply_evidence`
之前。本批把判据换成 `run_is_authoritative` 之后,这条守卫**照样绿** ——
因为换判据时留下的那句注释里写着「判据从 `succeeded_count > 0` 换成……」,
而注释也是源码里的字。

这是同一件事的第五次(batch13-3 的 M2 / M11、batch14-2 的 N15、batch14-4
那次),只是方向反过来:前四次是变异**注释掉**代码而守卫照样匹配,
这次是重构**移走**了代码而守卫匹配到了注释。根因是同一句话。
改法与 §3.26 第四节一致:锚在语法结构上 —— 用 AST 取那个 `if` 的条件本身,
而注释在 AST 里根本不存在。

两条改完之后都**单独验过它们真的咬得住**:把 W2 / W5 两条变异手工打上去,
各自那一条当场变红(变异脚本的过滤器够不着这份套件,所以这一步是手工的)。

## 七、接线:终态那一半接了,幂等那一半接不了

`WIRED_MODULES` 只登记 `terminal_status_for` 与 `run_is_authoritative`。

幂等那一半接不了,原因很具体:§4.6 的五个列(status / spu_id /
input_fingerprint / requested_scope / idempotency_key)**一个都不存在**,
没有列就没有地方存键、也没有行可以查重。登记进去会让那条门禁红,
而让它变绿最省事的做法是拿 `product_id` 凑一个假的 SPU 作用域 ——
那会造出一批算错的键,而键算错不报错,只是让「双击不产生第二个 run」
这条验收静静地不成立。欠账由
`test_the_idempotency_half_cannot_be_wired_yet_and_here_is_exactly_why`
记账:那五列落库那天它会红,那时接线并删掉它。

同一条守卫还钉着 `status` **今天不是列**。它哪天变成列,派生要挪到写入方,
而不是留下两个都在算的地方。

## 八、`status` 做成派生属性而不是等那一列

硬规则 4 要求后端返回的每个状态字段都能追溯到真实来源。派生属性满足它:
输入是 `image_count` / `succeeded_count` / `failed_count` 三个真实的列。

选它而不是等迁移,是因为**这个值今天就有人在算**,而算它的那一侧
(前端)算错了。等那一列意味着这个错再多活一批。

出参的默认值是 `FAILED` 而不是 `COMPLETED`:漏了这个字段时必须往
**不放行**那一侧倒。宽松默认值的漏填表现是悄悄放行 —— 硬规则 4
第二次事故的形状正是如此,变异 W4 复现了那种写法。

## 九、跑过的

| 门禁 | 结果 |
|---|---|
| 纯逻辑 `run_pure_tests.py` | 本批单独 **2185/2185**;**三批合树后 2223/2223**,0 失败,7 跳过 |
| 变异 `mutate_batch14_11.py` | **34/34 验红**(见第五节) |
| 变异 `mutate_batch14_9.py` 重跑 | **16/16 验红**(本批改了它的模块,所以复跑) |
| 变异 `mutate_batch14_10.py` 基线复核 | 23/23 验红 |
| audit-anchors | 本批单独 184/184;**合树后 216/216**(9 份脚本) |
| verify-delivery | 13/13 |
| verify-imports | 本批单独 368;**合树后 372 个文件** |
| verify-sample-data | 5/5 |
| 前端 syntax-check | 84/84 |
| `lint_offline`(F401 / UP017) | 349 文件无发现 |

## 十、仍未执行

与 batch14-9 / 14-10 相同,本机无外网、无 pip 工具:`ruff` 本体、
`lint-imports` 本体、真库 pytest、Alembic 升降级、前端 tsc / Vitest / build、
Docker build、Redis 相关的 P0-6。**前端本轮改了两个文件,只过了
syntax-check** —— 按总纲那句「改了前端还只跑 offline,等于没验」,
类型与 Vitest 欠着。这一条尤其要紧:本批新增的联合类型是**编译期比较的
依据**,而编译这件事没有在这台机器上发生过。

## 十一、这一批**没有**做的

- §4.6 的五个列、Celery 任务、cancel 端点、幂等键唯一索引 —— 要真库。
  判定已备好,索引谓词由孪生函数生成。
- `input_fingerprint` 落库与比较:它要 `scope_fingerprint` 接线,
  而那一条卡在阶段 2 的归属外键上(batch14-9 第五节)。
- 按颜色重试的接口与 UI(§13 阶段 3 的 PARTIAL_SUCCESS 那一半)。
  判定认它是终态且不占键,所以重试这条路是通的,缺的是入口。
- `QUEUED` / `RUNNING` 两档今天**不会出现**(同步执行)。前端两张表里
  有它们的位置,是为了 §4.6 落地那天不至于拿到 undefined。


## 十二、合树补记(与另外两批的关系)

本批与同期两批(门禁批、§11 两条新场景批)**互不重叠**,合树只有五个文件相交,
处理见 `HANDOVER.md` 第一节。其中一处需要在这里单独说明:

`app/attributes/scope_fingerprint.py` 被两批同时改过。门禁批改的是 ruff 体例
(`Iterable` 从 `collections.abc` 取、一处折行),本批把 `_field` 提升为公开的
`length_prefixed`(幂等键要用同一个编码器,不许抄第二份)。两处正交,都保留 ——
**先落门禁批的体例,再在其上重施本批的提升**,顺序反过来会让折行那一处失锚。

另有一条本批当时没有的信息:门禁批在装齐依赖的机器上跑出了 4 条一直在
「跳过 7」里的假绿用例。本批的 `run_state` 判定是纯层的,不在那 4 条里,
但**本批新增的三个文件同样没有被 ruff 本体覆盖过** —— 已按门禁批订正的口径
人工对齐(UP035 / I001 / E501),口径来源是它们的修法,不是我自己的判断。
