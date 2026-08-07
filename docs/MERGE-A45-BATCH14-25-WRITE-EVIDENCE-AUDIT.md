# A45-batch14-25:审计自己的证据链——`height` 那条假阳性不是孤例,是三个缺陷的其中一个出口

基线 14-24。本批不改业务代码,交付的是对 `tools/audit_column_writers.py`
**判定证据**的复核。

## 一、先把 `GenerationCandidate.height` 说完,因为它把话说全了

14-24 报告第二节写着:

> `GenerationCandidate.height` 恒为 NULL,而 `width` 有写入点 ——
> 两列本该成对;Provider 回传的尺寸只落了一半

**这段叙述在代码里没有对应物。** 两列写在同一行:

```
app/tasks/generation_tasks.py:2176
    row.width, row.height = info.width, info.height
```

`row` 在 2149 行绑到 `GenerationCandidate(...)`。所以真实情况是两列**都**有
写入点、**都**成对落库、Provider 的尺寸**两个都落了**。

那份报告为什么会得出相反的结论,值得逐步拆开:

| | 审计的判定 | 真相 | 判定来自哪 |
|---|---|---|---|
| `width` | 有写入点 ✔ | 有写入点 ✔ | **`export_writer.py:237` 的 openpyxl `sheet.column_dimensions[…].width = width`** |
| `height` | 无写入点 ✘ | 有写入点 | 2176 行的解构赋值,扫描器看不见 |

`width` 判对了,而**判对的理由与那一列毫无关系** —— 证据来自 Excel 导出里
设置列宽的一行。`height` 判错了,因为扫描器只认顶层 `ast.Attribute` 目标,
`a.x, a.y = ...` 整条不进判定。

**两个方向相反的缺陷,合起来造出了一个读起来完全合理的发现。**
"两列本该成对而只落了一半"——这句话有形状、有因果、有下一步动作,
唯独没有对应的代码。

### 这条推翻了审计文档里的一句话

模块顶部写着:

> 这是一个明确偏"多认写入"的近似 —— 它会漏掉列,不会造出假红。

**它造出了。** 而且比假红更难发现:因为这一列被写进了 `LEDGER`,
它不是红的,是**绿的、带着理由和还款日的一条欠账**。假红会有人来吵;
一条措辞得体的假欠账没有人吵,它会一直躺在台账上等阶段 5。

## 二、三个缺陷,按能造成的伤害排

### 缺陷 1:解构赋值不进判定

`collect_writes` 里:

```python
for target in targets:
    if isinstance(target, ast.Attribute):   # ← 只认顶层
```

`ast.Tuple` / `ast.List` / `ast.Starred` 目标不递归展开。全仓命中一处
(`generation_tasks.py:2176`),影响 6 个模型 × 2 列,其中只有
`GenerationCandidate.height` 因为没有别的证据兜底而落进台账。

**一处就够了。** 这类语法在 Python 里很常见,今天只有一处是运气不是设计。

### 缺陷 2:属性赋值按名字认,不解接收者——已经在往台账里灌错条目

文档把这一条列为"已知失真方向",并论证它只会**少报**。给接收者做局部
类型解析之后,全仓 **752 处**属性赋值被记到了错误的模型上,涉及
**140 个「模型.列」**。绝大多数无害(那些列另有构造点证据),但有 4 列的
"有人写"判定 **100% 建立在误判上**:

| 列 | 唯一"证据" | 接收者实为 | 真相 |
|---|---|---|---|
| `BatchJobItem.reused` | `outcome.reused += 1`<br>`generation_tasks.py:2144` | `_PersistOutcome`(局部 dataclass) | 无写入点,**恒为 `False`** |
| `AttributeCalibration.field_name` | `self.field_name = …`<br>`attributes/validation.py:84` | `AttributeValueError` | 无写入点 |
| `AttributeCalibration.model_name` | `row.model_name = …`<br>`attributes/service.py:346` | `ProductAttributeExtraction` | 无写入点 |
| `AttributeCalibration.prompt_version` | `row.prompt_version = …`<br>`attributes/service.py:347` | `ProductAttributeExtraction` | 无写入点 |

`AttributeCalibration` 全表零构造点,与台账"整张表由运维灌数据"一致 ——
问题是台账只列了它 8 列里的 5 列,另外 3 列靠误判溜过去了。

**`BatchJobItem.reused` 是一笔真欠账,形状与 `provider_request_id` 完全一样:**

```
列定义:  Boolean, nullable=False, default=False   → 库里恒为 False
读点:    batch_service.py:2725   "reused": row.reused   进出参
前端:    api/batch.ts:237        reused: boolean        有类型有位置
写点:    全仓零处
```

#### 这一条值得单独记:审计的文档里,`reused` 是被当作正面例子写的

模块顶部解释为什么动态 `setattr` 的范围取函数而不是文件:

> 按文件取会把 `batch_service.py` 里 `"reused"` 那一段也算成写入,
> 而它与落库的 `_outcome_values` 隔着一千行,把它算成写入会
> **漏掉一列真的没人写的列**

这段推理是对的,结论也是对的,那扇门确实关上了。**然后同一列从另一扇门
走了进来** —— 不是 `setattr` 路径,是属性赋值路径;不是
`batch_service.py`,是 `generation_tasks.py`;不是出参组装,是一个
局部计数器。被点名当反例的那一列,最后正是漏掉的那一列。

### 缺陷 3:台账只自净一半

```
AttributeCalibration.notes    ← 台账上有,而 AttributeCalibration 没有这一列
```

`report()` 的自净判定是「**列有写入点 → 条目失效**」。它不检查
**条目指向的列是否还存在**。列被删掉或改名之后,条目永久留在台账上,
永远绿。

29 条台账里 1 条是幽灵。比例不高,但这条缺陷不会自愈:自净机制按定义
碰不到它。

## 三、为什么七条守卫一条都没响

自净守卫(`test_a_ledger_entry_that_stopped_being_true_gets_reported`)
是这批里做得最用心的一条 —— 14-24 报告第五节专门讲了它怎么从"读源码断言"
改成"注入了直接算"。它注入的是:

```python
written_column = "MediaAsset.sha256"
```

`MediaAsset.sha256` 的写入点是**构造点关键字**。它落在扫描器能力范围的
正中央。

**于是这条守卫验的是机制,不是覆盖面。** 它证明了"扫描器看得见的写入
会让台账条目失效",而台账里最可能出错的条目,恰恰是扫描器**看不见**
其写入的那些 —— 那类条目对自净机制天然免疫。

一般化(建议进 §3.40):

> **自净守卫注入的样本落在扫描器能力范围内时,它验的是机制不是覆盖面。**
> 一份靠扫描器自净的台账,只能清掉扫描器看得见的那部分错误;
> 而扫描器的盲区与台账的错误条目,是同一批列。

这与 §3.37「判据落在别人身上的守卫,守不住自己」是同一族,但更隐蔽:
14-24 已经按 §3.37 把守卫从"读源码"改成了"算",**改对了,仍然没接住**,
因为问题不在判定放在哪,在注入的样本选在哪。

## 四、一条暂时无害的参数关联缺口,记上不修

关系 kwarg 到外键列的映射用的是 `f"{rel}_id" in columns`。全仓 10 个关系
对不上:

```
BatchJobItem.job    →  外键列叫 batch_id,不叫 job_id
CandidateEvaluation.problems / GenerationTask.attempts / …   一对多,本来就没有列
```

真正有影响的只有 `BatchJobItem.job` 这一类多对一。**今天不构成失真** ——
`batch_id` 另有构造点写入(`batch_service.py:1312`),没有任何一列的唯一
写入证据是这种关系 kwarg。真出现时会静默漏一列,所以记在这里。

## 四点五、"今天没有一份迁移回填过列"——这句话是错的

14-24 报告第七节与交接"寅"节都写着:

> 今天没有一份迁移回填过列(0040 / 0041 / 0042 都明写不回填),
> 所以暂时不构成失真

**核了 42 份迁移,有一份回填:**

```
migrations/versions/0021_batch_receipt_lifecycle_and_export_file.py:156
    UPDATE platform_rejections SET status = 'OPEN'
    WHERE status = 'FIXED_PENDING_EXPORT'
```

**结论不变,前提是错的。** 那段在 `_narrow_rejection_status()` 里,
只被 `downgrade()`(94 行)调用;`platform_rejections.status` 在
`platform_service.py` 有一堆应用侧写入点。所以它不改变任何一列的判定。

但那句话是从三份最新迁移推到"没有一份"的,而正确的说法要窄得多:
**没有一份 `upgrade()` 回填过列。**

差别在下一个人身上:他要加回填时会先找先例,会找到 0021,
会得出"回填是常规操作"——而审计仍然看不见回填。§3.33 那条
(照着错理由去做的人不会发现自己在做错事)在这里是原样适用的。

## 五、怎么改:三条是"只缺有人写",两条"缺一个决定"

按 14-23 那条线分:

**只缺有人写(可以直接做):**

1. `collect_writes` 递归展开 `ast.Tuple` / `ast.List` / `ast.Starred` 赋值目标
2. `report()` 增加一条:台账条目指向的模型/列必须存在,否则报"幽灵条目"
3. 属性赋值加接收者局部解析(函数参数注解 + 局部 `x = Model(...)` 绑定 +
   `for x in`),解出来是别的类就不记;**解不出来的仍按现在的宽口径**,
   不动那条偏向

**缺一个决定(不该由改的人拍板):**

4. `BatchJobItem.reused` —— 给它写入点,还是进台账挂还款日?
   它进出参、前端有类型位,而复用信息今天由 `_PersistOutcome.reused`
   在内存里数完就扔。**这不是"补一行赋值",是"这一列到底该不该存"**
5. `AttributeCalibration` 那 3 列 —— 大概率与另外 5 列同类(运维灌数据),
   但台账写"整张表"而只列 5 列这件事本身要有人确认一次;
   顺带:没有任何守卫检查"声明为整表豁免的表,它的列是不是都在台账上"

**守卫侧建议加一条:** 自净守卫改成注入两个样本,一个构造点写入
(现状的 `MediaAsset.sha256`),一个**解构赋值写入**
(`GenerationCandidate.height`)。第二个能挡住本批这一整类。

## 五点五、查过而干净的:这一节存在是为了让"没有别的了"这句话可复核

一份说"我没找到别的问题"的报告,和一份"我没去找"的报告,在文件里
长得一模一样。所以把查过的写下来:

| 查的是什么 | 结果 |
|---|---|
| `collect_models` 用 `glob` 不是 `rglob` | `app/models/` 无子目录,够用 |
| 模型间继承(子类继承的列不会被记到子类上) | 全仓无模型继承模型 |
| 老式 `= Column(...)` 定义(只认 `mapped_column`) | 0 处,全是 `mapped_column` |
| 模型自定义 `__init__` / `hybrid_property` setter / `synonym` | 0 处 |
| `for obj.attr in ...` / `with ... as obj.attr` 属性目标 | 0 处 |
| `bulk_insert_mappings` / `bulk_update_mappings` / `session.merge` / `__dict__.update` | 0 处 |
| 台账列在 `app/` 之外(`tools/`、`migrations/`)有没有写入点 | 0 处 |

最后一条查出来一个**潜在**缺口而不是现行问题:`tools/repair_variant_owners.py:167`
确实写 `row.owner_id`,而审计只扫 `app/`。今天不失真是因为
`ProductAttributeValue.owner_id` 在 `attributes/service.py` 另有构造点写入。
真出现"唯一写入点在运维脚本里"的列时,审计会把它误报成孤儿 —— **那个方向
是假红**,与缺陷 2 相反,所以到时会有人吵,风险低于本批那三条。

## 六、复核方式

`recheck_writes.py` / `recheck2.py` / `recheck3.py` / `final_pass.py`
零三方依赖,只读文件。判定分四档:

```
strong    构造点 kwarg / 关系赋值 / .values() / 字面量 setattr
correct   属性赋值,接收者解析为这个模型
wrong     属性赋值,接收者解析为别的类        ← 缺陷 2 的来源
unknown   属性赋值,接收者解不出来            ← 残余不确定性,不作为发现
```

`unknown` 那一档有 39 列。**抽查过的都是解析能力不足而不是真问题**
(`copy_service.py:380` 的 `row` 是 `ListingCopy`,
`evaluation_service.py:582` 的 `candidate` 是 `GenerationCandidate`,
两处都是真写入,只是绑定来自更早的查询)。**所以这 39 列不进结论**,
列在这里只是为了说明这份复核自己的边界在哪。

## 七、留给下一个人

本批推翻的是 14-24 的一条**结论**,不是它的方向。那份审计要解决的问题是
真的,`provider_request_id` 也确实是它找出来的。

要带走的是这一句 —— 它是 14-24 结尾那句的另一半:

> 审计说一列有写入点,不等于那个写入点是对的。
> **审计说一列没有写入点,也不等于真的没有。**
> 而后者更贵:前者错了会有人在读代码时发现,
> 后者错了会变成台账上一条措辞得体、无人质疑的欠账。
