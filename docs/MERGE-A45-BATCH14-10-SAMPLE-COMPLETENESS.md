# A45-batch14-10:样品完整度门禁的单点派生(§6.2)

> **一句话结论:这道门禁今天是对的,但它对得**没有道理** —— 它不问角色是谁标的,
> 也不问那张图是不是本商品的证据,而它至今没出事,靠的是 M3 的角色识别还没接。
> 顺带修掉一条真的在假阻断的规则:PRD 写的是 `PRODUCT_FRONT/FLAT_LAY`,代码只认前者。
> 纯逻辑 2148/2148、变异 23/23 验红、锚点 150/150、交付 13/13、导入 365、样例 5/5、
> 前端 syntax-check 84/84。**真库用例池仍然是 60 条,没有增加一条。**
> SPU 作用域**已接线**;颜色作用域接不了,原因写成了守卫(见第七节)。

---

## 一、动了什么

| 文件 | 改动 |
|---|---|
| `app/media/sample_completeness.py` | **新建。**门禁口径判定 + 满足组 + 双作用域,零依赖 |
| `app/workbench/flow.py` | `MaterialFacts` 补两列;素材步按组判定;新增 `CONFIRM_ASSET_ROLE` |
| `app/workbench/service.py` | `_material_facts` 把库里的行递给判定 |
| `app/workbench/batch.py` | 新增 `ASSET_ROLE_UNCONFIRMED` 异常类别 |
| `tools/verify_delivery.py` | `WIRED_MODULES` 登记四个函数 |
| `tests/pure/test_a45_batch14_10_sample_completeness.py` | **新建。**37 条守卫,其中 4 条穷举 |
| `tools/mutate_batch14_10.py` | **新建。**23 条变异,先于守卫写 |
| `frontend/src/api/workbench.ts` / `batch.ts` / `pages/TodayPage.tsx` | 新动作码与新异常类别的镜像 |
| `tests/pure/test_workbench_flow.py` / `test_a45_batch10_fixes.py` | 夹具补 `gate_roles` |

## 二、开口有多大,以及为什么它今天还没漏

`workbench/service.py` 那句:

    usable_roles = frozenset(r.role for r in usable if r.role)

不筛来源、不筛 `role_source`。而 AI 图至今没有满足过「至少一张正面图」,
靠的是两件**尚未发生**的事:

    shadow_from_candidate() 把候选图的 role 留空       所以 `if r.role` 把它滤掉了
    M3 的角色识别还没接                                所以没有任何一行写着 role_source=MODEL

第二条是写在 `media/service.py` 那句注释里的**计划**(「留 UNSET,由 M3 的角色识别
或人工来定」)。M3 落地那天,每一张 AI 候选图都会拿到一个模型给的角色,然后满足完整度。
**没有任何测试会变红**:那时的代码和现在逐字相同,变的只是库里的数据。

还有一条不用等 M3。`ingest()` 的去重键是 `(product_id, sha256)`,而
`_fill_missing_role()` 允许在去重命中时补一次角色 —— AI 影子行正好是
`role=None` + `role_source=UNSET`。于是运营把生成好的图下载下来、当样品传给另一个颜色
(这条流水线上会发生的事),命中的是那条 AI 行,给它写上 `role_source=HUMAN`。

**所以「只筛角色来源」挡不住 AI 图,「只筛来源」挡不住模型猜的角色。两个条件都要。**

## 三、落码时和 PRD 对不上的三处

1. **`CONFIRMED` 不是 `RoleSource` 的成员。**§6.2 写 `role_source ∈ {HUMAN, CONFIRMED}`,
   而枚举只有 `HUMAN / RULE / MODEL / UNSET`。与 batch14-8 撞上的 `MODEL_REFERENCE`
   同一种东西。处理:白名单里**保留这个字符串**(删掉的话,将来「人工确认模型建议」
   落地时没人记得规格写过它),并由守卫钉住「今天它确实还不是成员」。
2. **`RULE` 不在白名单里,尽管它不是模型猜的。**§6.2 给的是白名单不是排除法。
   两种写法今天等价(见下一条),分歧在将来:排除法之下新增任何来源取值都默认合格,
   而新增取值时没有人会想起这道门禁。fail closed。
3. **白名单今天一个取值都拦不住。**全仓只有两处写 `role_source`:
   `shadow_from_product_asset` 写 `HUMAN`、`shadow_from_candidate` 写 `UNSET` 且 role 为空。
   `RULE` 与 `MODEL` **一个写入点都没有**。所以这条收紧今天不改变任何一件商品的判定 ——
   把它当成「反正没影响」而省掉,等于把落地时机排在 M3 之后,而那时要拦的数据已经在库里了。

## 四、一处放宽,它是真的在假阻断

§6.2 原文是「至少一张 PRODUCT_FRONT/FLAT_LAY」,而 `flow.py` 的 `REQUIRED_ROLES`
只有 `PRODUCT_FRONT`。旧枚举 `GARMENT_CUTOUT` 映射成 `FLAT_LAY`(`media/mapping.py`
自注有损),于是**只有透明底商品图的商品被永久判「缺少正面图」**,
而运营解除它的唯一办法是把平铺图改标成正面图。

这正是 `flow.py` 自己在那段注释里写过的道理的另一半:门禁定得不对,
人会去改数据来迁就门禁 —— 而被改坏的是素材库的角色标注,
下游每一处按角色取图的地方跟着错。

`REQUIRED_ROLES` 这个常量**从 flow 层删掉了**,不留没人读的副本:留着它的坏处不是
死代码,而是下一个人改必备角色时会改那一个 —— 它就在素材步旁边,而真正生效的表
在另一个模块里,改完没有任何反馈。守卫钉着它不许回来。

## 五、两条变异的说明(与原始记录的差异)

原始记录里 **C3(待确认那条 issue 降级成 REMINDER)与 D1(可确认角色不排序)第一轮是绿的**,
补守卫后才转红。本次落码时这两条**第一轮就是红的** —— 但那不是复现,是因为原始记录
已经点名了它们,守卫是**带着答案**写的。这里如实记一笔,免得把它读成一次独立发现。

两条本身的道理仍然成立,守卫按它们的形状写:

**C3。**行为几乎不变:步骤照样 BLOCKED(缺图那条阻断还在),下一步照样是「确认素材角色」。
变的是**可见度**:`summarize()` 的「待确认」只数 `NEEDS_CONFIRM`,
而前端 `ISSUE_LEVEL_LABEL` 给 `REMINDER` 的是 `badge: false` —— 列表页根本不计数。
于是唯一能解开这条阻断的事情在看板上一处都不显示,而 REMINDER 的语义是
「可以带着它继续走」,对一件走不下去的商品说这句话本身就是错的。
守卫因此把**级别**与**计数**分开断言:级别对而计数不动是可能的。

**D1。**和 batch14-9 的 H1 是同一个教训:**构造了一对输入,不等于构造了一对会分叉的输入。**
去掉 `sorted` 之后集合迭代序接管,而字符串哈希每个进程都不一样 —— 表现是同一件商品
刷新两次、提示语里两个角色先后颠倒,`ref`(前端跳转依据)跟着换一个。
谁都不会把这件事报成 bug,只会觉得这个界面有点飘。守卫用八个元素,
乱序碰巧等于有序的概率是 1/8!。同一形状的第二条(D2,组标签按集合迭代)一并补上。

## 六、`_decide_next` 的理由必须取 issue 文案

`_decide_next` 原本从 `i.ref` 重拼「下一步」的理由,而 `ref` 按设计只带组里第一个角色
(它是跳转依据)。结果是判定说「正面图**或**平铺图」、按钮理由说「缺少正面图」——
**本批要修的那个坏循环原样回来**:运营为了消掉那句话,会把平铺图改标成正面图。
已改成直接取 issue 文案,并补成变异 C5,守卫断言 `issue.message in next_action.reason`。

## 七、接线:SPU 那一半接了,颜色那一半接不了

颜色子集要按 `color_variant_id` 分,而那一列是阶段 2 的归属外键本身,今天不存在。
表上只有 `variant_hint`,§4.8 明写它是「识别建议位」——拿它分子集等于让模型猜出来的值
决定运营能不能往下走,**而 §6.2 收紧 role 口径要禁的正是这件事。
在同一道门禁上违反两次没有道理。**

所以 `WIRED_MODULES` 只登记 SPU 那一半用得到的四个函数
(`gate_roles` / `confirmable_roles` / `missing_role_groups` / `group_label`),
颜色那一半由 `test_the_variant_scope_cannot_be_wired_yet_and_here_is_exactly_why` 记账:
归属外键落库那天它会红,那时接线并删掉它。那条守卫同时钉住 `variant_gate_roles`
**没有**混进 WIRED_MODULES —— 混进去的话让门禁变绿的最省事做法正是拿 hint 凑一下。

v3.0 的「未归属素材不得进入下游」欠的是同一列,理由更硬:今天没有归属外键,
**全库素材的归属都是未确认** —— 落码即等于阻断每一件商品。同上,一起收。

## 八、`gate_roles` 默认空集,于是 30 条既有用例当场变红

接线之后 `test_workbench_flow.py` 与 `test_a45_batch10_fixes.py` 的夹具全部变红,
因为它们只填了 `usable_roles`(本机实测:34 条失败,其中 30 条是夹具,
另外 4 条是前端镜像还没补)。**那是设计在起作用**:新字段给一个宽松默认值
(比如「没传就用 usable_roles」)的话,漏填的表现是悄悄放行 —— 硬规则 4 第二次事故
的形状正是如此:缺一列不报错,`bool(None)` 是 False,判定一路走到最宽的一档,
而两侧的测试全绿。变异 C2 复现了那种写法,现在它红。

夹具已逐个补齐,并在注释里写明两份集合不是一回事:

    usable_roles  素材库里**有**哪些角色的图 —— 提醒类问题读它
    gate_roles    §6.2 门禁**认**哪些角色

提醒类问题继续读 `usable_roles` 是刻意的:一件有背面图、但那个角色是模型猜的商品,
显示「没有背面图」是假的,而假提醒会训练人忽略这一列,然后真正缺图的那一件也没人看了。
单独一条守卫钉着它。

## 九、新动作码带出来的两处联动

`CONFIRM_ASSET_ROLE` 不复用 `UPLOAD_MATERIAL`(按钮会说「补充素材」,而运营
不需要补任何素材),也不复用 `RELEASE_QUARANTINE`(那句文案会说「N 条素材被隔离」,
而这条动线上 N 可能是 0)。**说错话比不说话更难查。**

- `test_every_next_action_code_has_a_precheck_mapping` 当场要求一个批次异常类别。
  没有并进 `NO_USABLE_MATERIAL` —— 它的建议是「补图或放行隔离素材」,两个动作在这条
  动线上都是错的,而补进来的新图默认同样没确认,运营会再撞一次。新增
  `ASSET_ROLE_UNCONFIRMED`,分档理由与 `AUDIENCE_UNCONFIRMED` 当初一字不差。
- 前端三张表 + 首页「其余待办」四处镜像,由 `test_frontend_contract.py` 与
  `test_a45_batch14_4_fixes.py` 离线钉着(两处在补镜像之前确实红了)。漏掉最后一处的
  表现是:卡在这一步的商品在首页一处都不显示,运营看完的结论是「今天没事干」。

## 十、跑过的

| 门禁 | 结果 |
|---|---|
| 纯逻辑 `run_pure_tests.py` | **2148/2148**,0 失败,7 跳过(本机缺 pydantic / sqlalchemy) |
| 变异 `mutate_batch14_10.py` | **23/23 验红**(见第五节关于 C3 / D1 的说明) |
| audit-anchors | **150/150**(7 份脚本) |
| verify-delivery | 13/13 |
| verify-imports | 365 个文件 |
| verify-sample-data | 5/5 |
| 前端 syntax-check | 84/84 |
| `lint_offline`(F401 / UP017) | 347 文件无发现 —— 顺带收掉上一轮遗留的一条(见下) |

**顺带收掉的一条:**`tests/pure/test_a45_batch14_9_scope_fingerprint.py` 里未使用的
`from pathlib import Path`。它在 `ruff check app tests` 范围内,会让装了 ruff 的机器
`make check-offline` 直接红,而那台机器上跑不到本批的任何一条结论。

**本批自己撞上同一条:**`flow.py` 一度写成 `from ... import ROLE_LABELS` 再对外转出,
`lint_offline` 当场报 F401。已改成显式别名(`ROLE_LABELS = gate.ROLE_LABELS`),
并顺手把满足组的两个调用改成 `gate.` 前缀 —— 这样「判定住在哪个模块」在调用点上看得见。

## 十一、仍未执行

与 batch14-9 相同,本机无外网、无 pip 工具:`ruff` 本体、`lint-imports` 本体、
真库 pytest、Alembic 升降级、前端 tsc / Vitest / build、Docker build、
Redis 相关的 P0-6。前端本轮改了四处,**只过了 syntax-check** ——
按总纲那句「改了前端还只跑 offline,等于没验」,类型与 Vitest 欠着。

## 十二、这一批**没有**做的

- 存储列、CHECK 约束、归属外键、迁移 —— 要真库(与 batch14-8 同)。
- `media.evidence_assets_for(spu_id, scope)` 的 SQL 实现。
- 颜色作用域接线、「未归属素材不得进入下游」—— 第七节。
- 按颜色上传 UI(§6.2 的另一半)。判定已备好双作用域,UI 那一侧等归属外键。
- `has_primary_image` 全仓算了没人读(`service.py` 是唯一写入点,没有读取点)。
  它不属于本批,单独记一笔:那是「算好了没人读」那一类,`publish_view` 的
  `reused_terminal` 有过同款,当时是用 AST 守卫钉的。**注意它仍然读的是
  `usable_roles` 而不是 `gate_roles`** —— 接读取点那天要先决定它问的是哪个问题。
