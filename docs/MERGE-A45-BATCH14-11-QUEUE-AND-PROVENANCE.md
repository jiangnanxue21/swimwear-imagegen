# A45-batch14-11:§11 的两条新场景(确认队列口径 + AI 图伪装拦截)

> **一句话结论:两条修的都是「今天不报错、等外部事实发生才漏」的规则,
> 而两件外部事实都已经排在日程上了 —— 真实抽取器接上的第一天,
> 和运营第一次把生成好的图下载下来当样品传回去的那一天。**
> 纯逻辑 2185/2185、变异 32/32 验红、锚点 182/182、交付 13/13、
> 导入 369、样例 5/5、前端 syntax-check 84/84、batch14-10 的 23 条变异重跑仍全红。
> **真库用例池仍然是 60 条,没有增加一条。**
> **与阶段 3 第二批(§4.6 异步化)零交集**,理由见第一节。

---

## 一、为什么是这两条,以及它们和 §4.6 的边界在哪

`HANDOVER.md` 第三节把阶段 3 没做的第一条点成 §4.6:

    status / spu_id / input_fingerprint / requested_scope / idempotency_key
    五列 + Celery 任务 + cancel + 幂等键唯一约束

那一批由另一条线在做。本批**完全没有碰**:`ProductAttributeExtraction`
模型与迁移、`attributes/service.py::run_extraction` 的事务形状、Celery 任务、
取消语义、`PARTIAL_SUCCESS` 与按颜色重试(它们的状态取值属于 §4.6 的状态机)。

挑的是 PRD §11「新增三行」里剩下的两行。它们与 run 的生命周期无关:

| §11 那一行 | 落在哪一层 | 与 §4.6 的关系 |
|---|---|---|
| 未校准字段大量涌入 | 确认队列口径(`attributes/` + `workbench/flow`) | 无。改的是「哪些**事实**进队列」,不是「run 怎么跑」 |
| AI 候选文件被当作原始样品重新上传 | 素材入库(`media/`) | 无。发生在识别之前 |
| 同一 run 内部分颜色的图全部失败 | **没做** —— PARTIAL_SUCCESS 是 §4.6 的状态机成员 | 冲突,已避开 |

## 二、动了什么

| 文件 | 改动 |
|---|---|
| `app/attributes/queue_policy.py` | **新建。**状态 → 去向的穷举归档表 + 分桶,零依赖 |
| `app/media/provenance_conflict.py` | **新建。**溯源冲突判定 + 补角色闸,零依赖 |
| `app/media/service.py` | `ingest()` 改按 SPU 取数、冲突落隔离;`_fill_missing_role()` 加闸;新增 `_same_digest_in_spu()` |
| `app/workbench/flow.py` | 属性步拆出 `ATTR_EVIDENCE_ONLY`;新增 `FILL_ATTRIBUTES`;删掉 `has_primary_image` |
| `app/workbench/service.py` | `_attribute_facts` 改走分桶;删掉 `has_primary_image` 写入点 |
| `app/workbench/batch.py` | 新增 `ATTRIBUTE_NOT_CREDIBLE` 异常类别与前置映射 |
| `tools/verify_delivery.py` | `WIRED_MODULES` 登记两个新模块共三个函数 |
| `tests/pure/test_a45_batch14_11_queue_and_provenance.py` | **新建。**37 条守卫,其中 4 条穷举 |
| `tools/mutate_batch14_11.py` | **新建。**32 条变异,先于守卫写 |
| `frontend/src/api/workbench.ts` / `batch.ts` / `pages/TodayPage.tsx` | 新动作码与新异常类别的四处镜像 |

## 三、第一条:CANDIDATE 一直在混进确认队列

### 规格早就写好了,而代码一直在违反

`core/enums.py` 里 `AttributeStatus` 自己的文档字符串:

> `CANDIDATE` 与 `SUGGESTED` 的区别是这一版的关键:前者是「留了证据但不采信」
> (未校准、置信度过低、模型说看不清),后者是「够格进人工确认队列」。
> **混成一个的话,未校准字段会和低分字段一起涌进待确认列表,人只会全部忽略。**

PRD §11 新增的第三行把它写成了明文:「确认队列只出现 SUGGESTED/CONFLICT」。

而 `workbench/flow.py::_evaluate_attribute` 两处都在混:

    level = NEEDS_CONFIRM if name in facts.suggested or name in facts.candidate_only ...
    state = NEEDS_CONFIRM if all(m in facts.suggested or m in facts.candidate_only ...)

后果是一个 CANDIDATE 字段产出「**有建议值待确认**」这句话、级别 NEEDS_CONFIRM、
步骤状态跟着进 NEEDS_CONFIRM(`STATE_PROGRESS` 记 0.6 分)。
运营点进去会发现没有任何值可以确认 —— 那个字段的证据按定义就是不采信的。

### 为什么现在要紧,而不是一条纸面洁癖

`attributes/decision.py` 的第二条分支:

    if system_confidence is None or system_confidence < MIN_SUGGEST_CONFIDENCE:
        return AttributeStatus.CANDIDATE   # 未校准返回的正是 None

阶段 3 已经把真实抽取器接上了,而校准分箱是空的。**真实模型接上的第一天,
每一个字段都是 CANDIDATE。**那批商品会集体显示「有建议值待确认」、
属性步 60% 完成度 —— 排在真的做了一半的商品后面,而它们一个可确认的值都没有。

**这条规则今天一条守卫都没有:**全仓 `candidate_only` 只在 `flow.py` 出现两次、
`service.py` 一次,`tests/` 里**零次**,`DECISIONS.md` 里查不到任何相关决策。
也就是说它不是一个被权衡过的决定,是一处没人看过的地方。

### 落法:归档表在纯层,`flow` 只读分好的桶

`attributes/queue_policy.py` 把「状态 → 去向」写成一张**必须覆盖每个
`AttributeStatus` 成员**的表(守卫穷举钉着)。写成 `!= CANDIDATE` 的排除法
今天等价,分歧在将来:新增一个取值时它默认进确认队列,
而新增取值的那个人不会想起这道口径。与 `sample_completeness` 挑白名单
不挑排除法是同一条理由。

五档去向里有两处需要说明:

1. **`REJECTED` / `SUPERSEDED` 归 `DISMISSED`,不归 `EVIDENCE_ONLY`。**
   两者都不进队列,所以合并**不改变任何行为** —— 但它们不是一回事:
   `EVIDENCE_ONLY` 说「系统看过了,不采信」,那是一条还欠人去填值的待办;
   `DISMISSED` 说「这个值已经被处理掉了」,它不欠任何动作。
   合并的代价要到「有证据但不采信」这条动线接上界面那天才付。
2. **认不出的状态兜底成 `EVIDENCE_ONLY`,不是 `DISMISSED`。**
   兜底方向选「产出一条待办」而不是「静默消失」。反过来兜的话,
   库里一个拼错的状态字符串会让那个字段从确认队列和阻断清单里**同时**消失,
   而商品照样导不出去 —— 运营看到的是一件卡住但没有任何理由的商品。

### 新动作码:`FILL_ATTRIBUTES`

`_decide_next` 原来无条件说「确认属性」。识别跑过、结论全是 CANDIDATE 时
那句话是假的:队列是空的。运营点开属性页找不到可点的东西,
下一步多半是去点「启动属性识别」—— **一次真实付费调用,换回同一批不采信的证据。**

不复用 `CONFIRM_ATTRIBUTES`(说错话),也不复用 `RUN_EXTRACTION`(直接把他送去花钱)。
与 batch14-10 的 `CONFIRM_ASSET_ROLE` 同一条理由:**说错话比不说话更难查。**

带出来的联动与 batch14-10 那次一字不差:

- `test_every_next_action_code_has_a_precheck_mapping` 当场要求一个批次异常类别。
  新增 `ATTRIBUTE_NOT_CREDIBLE`,**`retryable=False`**——识别没有失败,
  它成功地告诉了我们「这批结论不够格」,而 `retryable=True` 会让批次自动再花一次钱。
  这一条由守卫单独钉着。
- 前端四处镜像(动作码联合类型 / 文案表 / 标签页映射 / 首页「其余待办」),
  由 `test_frontend_contract.py` 与 `test_a45_batch14_4_fixes.py` 离线钉着。
  漏掉最后一处的表现是:卡在这一步的商品在首页一处都不显示。

## 四、第二条:AI 图伪装拦截(AC-22)

### 这条开口是 batch14-10 自己点的名,但那一批没修

`ingest()` 的去重键是 `(product_id, sha256)`,而 `_fill_missing_role()` 允许在
去重命中时补一次角色 —— AI 影子行正好是 `role=None` + `role_source=UNSET`。
于是一条 `source=AI_GENERATED` 的记录拿到了 `role_source=HUMAN`。

§6.2 的完整度门禁问的正是「角色是不是人定的」,而它被答错了。
今天 §5.1 那道白名单还拦得住(`evidence_class` 由 `source` 派生),所以没出事 ——
但**一道门禁靠另一道门禁碰巧还在,不叫防住了**。

### 作用域必须比去重键宽 —— 本批最贵的一条

去重键是 `(product_id, sha256)`,而 §11 写的是**同 SPU**。
两者看起来应该一样,而它们不一样,差出来的那一块正是最常见的走法:

    一个卖三色的款,运营把 A 色生成好的图下载下来,当样品传给 B 色的 SKU
    → product_id 不同 → 去重不命中
    → 新建一条 source=MANUAL_UPLOAD 的行
    → 它派生成 PRODUCT_EVIDENCE
    → **直接进识别输入,每张一次真实付费调用**,投的票还是一张 AI 图看出来的结论

新增 `_same_digest_in_spu()` 一次查出同 SPU 同 sha256 的全部行,
去重与冲突判定共用这一次观察 —— 分两次查的话,两个结论在并发下来自两个快照。

`spu` 可空,所以那一句**不能**写成 `MediaAsset.spu == product.spu` 了事:
SQLAlchemy 会把 `spu == None` 渲染成 `spu IS NULL`,于是全库没有 SPU 的素材
(存量数据、导入中途的行)会被当成同一个 SPU 的兄弟,一次上传能被一件
毫不相干的商品的历史 AI 图拦下来。空 SPU 退回按商品找,守卫钉着这个分支。

### 判据是溯源事实,不是 `source` 一列

认三个信号,任一命中即算带溯源:两个溯源列(`generation_task_id` /
`generation_candidate_id`)、`source=AI_GENERATED`、`legacy_kind=generation_candidate`。

**第三个是今天唯一真的会命中的那一路** —— 溯源列是阶段 2 的,还没落库;
`source=AI_GENERATED` 只在生成链路自己回写时出现,而那一路按设计不算冲突。
少了它,这个模块今天一条也拦不住,**而两侧的测试都不看这件事**。
所以守卫穷举四个信号的全部 16 种组合,并单独用
`test_the_legacy_shadow_marker_is_the_only_signal_that_fires_today`
把「今天为什么拦得住」写成断言,而不是让它继续当一条运气。

两个溯源列用 `getattr` 取,与 `evidence_rules.asset_is_extraction_input` 同一处理:
写成属性访问会在列落库前 AttributeError,写死 `None` 则会在列落库**之后**继续瞎。

### 为什么是隔离,不是抛异常

§11 写的是「拒绝并提示来源冲突,**落隔离待人工放行**」——两件事都要。
抛异常只做到前一半,而且更糟:影子写和业务写在同一个事务里
(`media/service.py` 顶部那段),抛出去会把这次上传整笔回滚,
于是那条「待人工放行」的记录也一起没了,运营手上只剩一句报错。
放行通道 `media.release()` 已经存在,隔离态正是它认的输入。

### 去重命中那一路**不改状态**,这是刻意的不对称

命中的那条很可能就是生成链路自己的候选行(`candidate.media_asset_id` 指着它),
隔离它等于把一张合法候选图从图片集里拿掉 —— **修一个洞,砸一条正常动线。**
那一路的防线是 `may_fill_role()`:它拒绝给带溯源的行补角色,
于是 §6.2 门禁不会认下这张图。同时留一条 `warning` 日志,
因为这件事发生了就该有人知道。

两个闸不重叠,两个都要:

    verdict()        管**新建行**那一路(同 SPU 不同 SKU,去重不命中)
    may_fill_role()  管**去重命中**那一路(同一件商品,不建新行)

只做前者,把图传回原 SKU 仍会给 AI 行盖上 `role_source=HUMAN`;
只做后者,传给兄弟 SKU 会新建一条干净的 `PRODUCT_EVIDENCE`。

### 一处顺带的文案保护

`shadow_from_product_asset` 原来无条件写 `quarantine_reason`。上传检查未通过
且同时撞上溯源冲突时,「上传检查未通过」会盖掉溯源那一句 ——
而两句话指向的下一步完全不同。改成理由已存在时不覆盖,守卫钉着。

## 五、顺带删掉的一个死字段

`MaterialFacts.has_primary_image`:`service.py` 是唯一写入点,全仓**零个读取点**
(batch14-10 第十二节记过这一笔)。留着的坏处不是多一个布尔字段,
而是它读 `usable_roles` 而 §6.2 门禁读 `gate_roles` —— 第一个接它的人会拿到
一个「AI 图也算有主图」的答案,而两侧的测试全绿。

选删而不是改口径:改成 `gate_roles` 之后它答的仍然只是「有没有 PRODUCT_FRONT」,
而门禁认的是「正面图**或**平铺图」的满足组,那就又是第二份必备角色口径。
真需要这个答案时问 `missing_role_groups(gate_roles)`。守卫钉着它不许回来。

## 六、变异第一轮是 30/32,两条 GREEN 都是我自己的守卫写松了

**这一节的价值不在最后那个 32/32。**两条漏网的都是 AST 守卫,
而且都是这个仓库栽过的同一型 ——「出现了某个东西」不等于「那个东西问的是对的问题」:

| 变异 | 第一版守卫怎么写的 | 为什么抓不住 | 同型旧账 |
|---|---|---|---|
| **W2** 把 `if product.spu` 换成 `if True` | `assert "IfExp" in dumped` | `if True` 照样是一个 `IfExp` | batch14 的 M10(`if False: 记流水(...)` 照样满足「记流水出现在 except 里」) |
| **W6** 删掉「理由已存在就不覆盖」那一问 | `assert "Not()" in dumped and "quarantine_reason" in dumped` | 这个函数本来就有 `not deduped`、本来就要给那一列赋值 —— 两个各自都真的东西凑在一起,证明不了它们在同一处 | batch13-3 的 M2 |

修法一律是**把断言打到能精确指认的结构上**,不是把断言写得更长:
W2 现在断言那个三元表达式的 `test` 是 `product.spu` 这个属性访问本身;
W6 现在遍历 `If` 节点,要求存在一个条件里含 `not <某个>.quarantine_reason` 的分支。

另外,所有读源码的守卫都走 `_body_without_docstring()`:按整段源码找字符串的写法
在这个仓库栽过三次(batch13-3 的 M2/M11、batch14 的 M30 —— 那次比较的是一段文档字符串)。

## 七、这一批**没有**做的

- **§4.6 的异步化及其全部附属**(五列 / Celery / cancel / 幂等键唯一约束 /
  PARTIAL_SUCCESS / 按颜色重试)。第一节说明了边界。
- **「有证据但不采信」的界面。**判定已经产出 `ATTR_EVIDENCE_ONLY` 与
  `FILL_ATTRIBUTES`,前端只补了三张镜像表,**没有**为这条动线做属性页上的
  分组展示或「一键人工填写」入口。无 node_modules,改了验不了。
- **`media.evidence_assets_for(spu_id, scope)` 的 SQL 实现**、颜色作用域接线、
  归属外键 —— 与 batch14-9 / 14-10 同,要真库。
- **溯源冲突没有落审计。**`ingest()` 手里没有 actor(`_audit` 要),
  今天只记 `logger.warning`。接审计要先决定这条记谁头上,那是一个业务决定。
- **没有加索引。**`(spu, sha256)` 走的是既有的 `ix_media_assets_sha256`,
  再按 spu 过滤。同一张图在全库的行数是个位数,所以代价可以忽略 ——
  但这是**推理**,没有实测。素材量上来之后这里是第一个要看的地方。
- **迁移一条都没加。**本批不需要新列。

## 八、跑过的

| 门禁 | 结果 |
|---|---|
| 纯逻辑 `run_pure_tests.py` | **2185/2185**,0 失败,7 跳过(本机缺 pydantic / sqlalchemy)。基线 2148,本批 +37 |
| 变异 `mutate_batch14_11.py` | **32/32 验红**(第一轮 30/32,见第六节) |
| 变异 `mutate_batch14_10.py` 重跑 | **23/23 仍全红**(本批动了 `flow.py` 与 `workbench/service.py`) |
| audit-anchors | **182/182**(8 份脚本) |
| verify-delivery | **13/13**(含新登记的两个模块接线检查) |
| verify-imports | 369 个文件 |
| verify-sample-data | 5/5 |
| `lint_offline`(F401 / UP017) | 350 文件无发现 |
| 前端 syntax-check | 84/84 |

## 九、仍未执行

与 batch14-9 / 14-10 相同,本机无外网、无 pip 工具、无 PostgreSQL、无 node_modules:
`ruff` 本体、`lint-imports` 本体、真库 pytest(池子仍是 60 条)、Alembic 升降级、
前端 tsc / Vitest / build、Docker build、Redis 相关的 P0-6。

**本批前端改了四处,只过了 syntax-check。**按总纲那句「改了前端还只跑 offline,
等于没验」,类型与 Vitest 欠着。

**两条最要紧的没验:**

1. **`ingest()` 的新查询没有在真库上跑过。**守卫验的是「SQL 是按 spu 拼的」,
   验不到「这条 WHERE 在 PostgreSQL 上真的选中了那些行」,更验不到并发下
   两个上传同时命中同一条 AI 行时的行为。
2. **隔离态的下游没有走一遍。**冲突行落 `QUARANTINED` 之后,素材页能不能看见它、
   `release()` 放行之后它会不会重新变成 `PRODUCT_EVIDENCE`(会 —— 它的
   `source` 是 `MANUAL_UPLOAD`,而 `evidence_class` 由 source 派生),
   这两件事只有真库 + 浏览器验得了。**第二件尤其要在验收时确认是不是想要的行为:
   人工放行的语义就是「我确认这确实是实物样品照」,所以它重新成为证据是设计,
   不是漏洞 —— 但没有人在真界面上走过这一步。**
