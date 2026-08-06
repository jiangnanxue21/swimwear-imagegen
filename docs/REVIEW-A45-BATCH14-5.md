# REVIEW:A45-batch14-5(收掉 batch14-4 §五的三条遗留)

batch14-4 第五节留了三条。本批把三条都做完,并且第一次在**装得齐依赖的机器上**
跑了那一批一直标着"没跑过"的门禁 —— `tsc` / ESLint / Vitest,外加
`pydantic` / `sqlalchemy` / `pydantic-settings` 装上之后的纯层。

> **一句话结论:三条遗留全部落地,门禁全绿(纯层 2066/2066、四份变异
> 87 条锚点 / 87 条变异全红、Vitest 66/66、tsc 干净、ESLint 13 errors -> 0)。
> 但真正值钱的是补的过程里撞出来的四件事 —— 每一件都是同一个形状:
> **红/绿是真的,原因不是。** 四件里有三件已经在这个仓库里被写过名字
> (F7、假绿、宽集合),而它们仍然各自躲过了一整轮评审。**

---

## 一、遗留之一:离开保护的 Vitest 那一层

### 缝在哪里,和 §五之一说的不完全一样

§五之一说的是「`test_router_is_a_data_router_so_blocker_works` 断言的是源码里有
`createBrowserRouter`,真正的证明要 Vitest 挂一个脏表单再触发导航」。

但 `tests/component/unsaved-guard.test.tsx` **已经有 6 条在做这件事了** ——
挂脏表单、点链接、点「留在本页」、点「放弃改动」、验保存后放行、验
`beforeunload` 的挂载与解绑。所以缺的不是"有没有 Vitest",而是别的东西:

    源码扫描(纯层)      看得见 `createBrowserRouter` 这串字,看不见 router 对象
    unsaved-guard 那 6 条  证明了「**给它一个数据路由**,它会拦」
    没有任何人证明        「**这个应用真正挂上去的那个 router** 是数据路由」

那 6 条是自己 `createMemoryRouter` 搭的壳。缝的宽度正好是一次
`<BrowserRouter>` 回退:6 条照样全绿(壳是自带的),纯层那条也照样绿
(只要 `App.tsx` 里还留着那个词),而线上离开保护整条静默失效。

### 补法

`tests/component/real-router-blocks.test.tsx`,4 条,直接拿 `App.tsx` 导出的
**真实 router 对象**开刀:

    router.getBlocker / deleteBlocker 是函数、state 是对象   —— useBlocker 内部走的就是这套 API
    在真实 router 上挂 blocker,navigate 之后**停在原地**
    blocker.proceed() 之后,那次被挂起的导航走得掉
    非数据路由下 useBlocker **抛错**,不是静默 no-op

第四条钉的不是我们的代码,是 react-router 的失败方式,而它正对着 §五之一
真正担心的那句「拦不住『这套机制从来就没生效过』」:只要那是抛错,
真有人换回去就是当场白屏 —— 刺眼、当天就会被发现。哪天 react-router
把它改成静默 no-op,这条会红,那时候才轮到"静态守卫是仅剩的防线"。

三件刻意的决定:

**不渲染整个应用。** 最"完整"的做法是 `<RouterProvider router={router} />`
挂起来走到真实编辑页。但那需要 QueryClientProvider、antd 上下文、
以及给几十个查询兜底的 MSW —— **这些垫片本身会变成被测对象**,
而它们和离开保护没有一分钱关系。垫塌了的表现是这条用例红,
读的人会以为离开保护坏了。

**不调 `router.initialize()`。** 走过一次:它会 `history.listen(...)`,
而这个 router 是模块级单例,第二次就撞上「A history only accepts one active
listener」。而且根本不需要 —— 本应用所有路由只有 element、没有 loader,
`createRouter` 当场就把 `state.initialized` 置真了;`initialize()` 只负责
接管浏览器的前进/后退。

**验过它不是空的。** 把 `() => true` 改成 `() => false`,2 条当场红,
恢复后 4 条全绿。这条守卫自己也过了一遍变异。

---

## 二、遗留之二:SUITE_FILTER 收成精确匹配

判定挪进新的 `backend/tools/suite_filter.py`,`run_pure_tests.py` 和
`audit_anchors.py` 都 import 它。规则一行说得完:

    ""              全部
    "xxx.py"        精确文件名
    其他             子串(与历史一致,`p0_gate.py` 和终端里手敲的那种不受影响)

### 为什么非要抽一个模块出来

原来 `audit_anchors.py` 里抄了一句 `suite in name`,并在文档字符串里写
「所以这里也只能照着它判,不能自己发明一套」。**用注释维持两份实现同步,
正是这个仓库反复踩的那个坑** —— `test_in_flight_task_count_comes_from_the_state_machine`
守的是同一件事(「哪些状态算运行中」不许两处各抄一份)。注释拦不住重构。

没让审计直接 import `run_pure_tests`:那个文件**在模块顶层就有副作用**
(`mkdtemp()` + 写两个环境变量,见它顶部关于主密钥的整段说明),
而审计的卖点是"只读文件、零子进程、不到一秒"。`suite_filter` 零副作用。

### §五之二把病因说轻了

§五之二写的是耗时线性上涨。耗时是看得见的那一半。看不见的那一半是**归因**:

`mutate_batch14.py` 的一条变异可以因为 `test_a45_batch14_4_fixes.py` 里的
守卫而变红,而报告记在 `mutate_batch14.py` 头上。于是"我这条守卫咬人"是假的,
删掉那条真正咬人的守卫时,变异仍然红着,没有任何人会发现。

所以审计从"至少挑中一个"收成了"**恰好一个**",并给出改法。

---

## 三、遗留之三:Q19 的镜像

新增 Q20,在提示词页写死 `dirty={false}`。两种的表现是**相反**的,
而恒假更难发现:

    恒真   每次离开都弹窗 -> 运营学会无脑点确定 -> 真有改动那次也被点掉
    恒假   一次都不弹 -> 组件挂着、看起来有保护 -> 改动被静默吞掉

恒真至少还有人抱怨"老弹窗"。恒假没有任何征兆:`<UnsavedGuard ... />`
好端端挂着,`test_every_editing_surface_is_guarded` 的第一条断言(有没有挂)
也照样满足 —— 只有第二条(不许写死)拦得住它。只造恒真那一种,
等于第二条断言里的 `"false"` 那半边从来没被验过。

换一个面(提示词页而不是设置页)是刻意的:和 Q19 用同一个锚点会撞
`text.count(old) != 1`,顺带多验一个挂载点。

---

## 四、补的过程里撞出来的四件事

**这一节比上面三节重要。** 三条遗留是照着清单做的;下面四件是清单上没有的,
而且四件里有三件的名字这个仓库已经写过了。

### 4.1 `mutate_batch14.py` 指向一个**不存在**的套件

收成精确匹配的当天,审计就叫了:`SUITE_FILTER` 按精确匹配挑不中任何文件 ——
因为 `test_a45_batch14_fixes.py` **这个文件根本不存在**。

它那 34 条变异打的是 `app/extractors/*` 与 `app/attributes/service.py`,
套件是 `test_a45_batch14_stage3_extractor.py`。也就是说老的子串
`"a45_batch14"` 挑中的 4 个套件里,**没有一个是它自己的**:

    test_a45_batch14_2_fixes.py       别的批次
    test_a45_batch14_3_fixes.py       别的批次
    test_a45_batch14_4_fixes.py       别的批次
    test_a45_batch14_stage3_extractor.py   ← 它自己的,但它不知道

它历史上报的"34/34 全红",红的是一锅混着的守卫。§二里那段"归因是假的"
是我写审计报错文案时假设的情形 —— 结果它是**实际发生的**,而且已经发生了不止一轮。

### 4.2 M8 十七轮没有验过任何东西

改对套件之后重跑,34 条里有一条不是红,是脚本自己标的
「**导入期就炸了,这条变异不算数**」。

M8 把 `plan = plan_evidence(result, targets)` 换成
`_EvidencePlanShim(result, targets)` —— 一个谁都没定义的名字。
跑起来当场 NameError,而脚本按 `rc != 0` 判"响了"。

于是 `test_the_service_materialises_the_plan_instead_of_deciding_again`
**从来没有被任何一条变异验证过**。`crashed` 那道闸事后标得出来,
但标出来之后没有人回来修 —— 一条标记如果不阻断,和没有一样。

这与 batch14-3 的 F7(`AttributeError` 的非零退出码被读成"被抓住")
是**同一句话的第三次出现**:红是真的,红的原因不是。

已改成语法合法、import 得动、跑得起来的写法:服务层就地自己拼一份
`EvidencePlan`,不再走纯判定。它同时踩中那条守卫的两条断言
(没调 `plan_evidence`、服务层重新长出目标过滤)。重跑:**34/34,
且不是靠炸掉模块**。

### 4.3 那 7 条 "环境事实" 的 SKIP 里,藏着一条假绿

batch12-5 给缺依赖跳过定的调子是「这是**环境事实**,不是代码问题」。
装上 `pydantic` / `sqlalchemy` / `pydantic-settings` 之后,7 条 SKIP 变成
0 条 SKIP,同时**多出一条 FAIL**:

`test_the_budget_tracks_configuration_instead_of_freezing_at_import`
改的是 `os.environ["VISION_MODEL_TIMEOUT_SECONDS"]`。而 `provider_setting()`
的读取顺序是:

    1. `_override(name)`            后台设置页写进数据库的覆盖值,**每次重读**
    2. `app.core.config.settings`   pydantic BaseSettings,**import 时读一次**
    3. `os.environ`                 **仅在第 2 步 ImportError 时**

改环境变量只有走到第 3 步才看得见,而第 3 步的前提是 `pydantic_settings`
**装不上**。`_config.py` 自己的文档字符串写着「生产环境里永远走第一条分支;
退化分支只在被裁剪过的运行环境生效」。

所以这条用例在离线容器里绿了很久,**绿的原因是依赖缺失**,与它声称守的
那件事无关;而在 CI、评审机、生产镜像上它是红的。

要说清楚的是:**这不是生产 bug。**"改完就生效"这个承诺由第 1 层兑现,
而第 1 层是活的、每次重读。坏的是这条用例的口径 —— 它推的是退化分支。
已改成推第 1 层,并加一条配套守卫(`test_the_settings_page_promise_goes_
through_the_override_layer_not_the_environment`)钉住覆盖层每次调用都读,
免得下一个人把它换回环境变量之后又在离线容器里看到一片绿。

### 4.4 两道门禁互相要求对方红

修 ESLint 那 13 条时,拿掉了 `TaskCreateModal.tsx` 里用户可见文案中的 `§11`。
纯层当场红:`test_the_model_reference_bypass_is_recorded_as_a_known_gap`
断言 `"§11" in modal`。

    前端 ESLint  `no-restricted-syntax`:内部编号不许进 title/extra/description
    纯层守卫      `"§11"` 必须在 TaskCreateModal 里

两道门禁互相要求对方红,而**先跑的那一道决定谁赢**。之所以一直没爆,
是因为 ESLint 从来没跑过。

谁对?看那条测试自己的文档字符串:「界面这一半已经改了:下拉不再把它说成
一个等价选项,而是**明说它跳过 §11 的检查**」。它要守的是**那句话的实质**,
而它守了一个编号 —— 运营手里没有那份文档,一个编号对他等于
"这里有个原因,保密"。

已改成三条断言:用户可见文案里必须有人话("没有授权与年龄记录")、
用户可见文案里**不许**有 `§11`(和 ESLint 同向)、注释里必须留着 `§11`
(可追性不丢,且钉住它没被顺手删掉)。

---

## 五、ESLint 那 13 条

第一次跑 ESLint,13 errors + 5 warnings,**全部是既有的**,与 batch14-4
一行关系都没有。两类,都是仓库自己的自定义规则:

    6 条   内部文档编号进了用户可见文案(§8.2 / §10.5 / §11.3 / §13.2 / §19 …)
    7 条   antd 预设调色板色绕过 theme.ts(`<Tag color="orange|green|red">`)

第二类按 `BrandTag.tsx` 顶部那段既有论证处理:不是换成
`<Tag color={brandVars.x}>`(那会把浅色形态换成实底白字,表格里密度一上来
整页会花),而是换成 `<BrandTag tone="...">`。受众未确认 -> `warning`,
授权未验证 -> `warning`,授权异常 -> `danger`,已覆盖变体 -> `success`。

13 -> 0。剩 5 条 `react-hooks/exhaustive-deps` 的 warning,既有、不影响
退出码,没动 —— 它们要改的是 `useMemo` / `useEffect` 的依赖数组,
属于会改变运行时行为的那一类,不该和一批 lint 清理混在同一次里。

---

## 六、跑过的

**这台机器装得齐依赖,所以 batch14-4 第四节列为"没跑过"的三样,跑了两样。**

| 门禁 | 结果 |
|------|------|
| 纯逻辑 | **2066/2066**,0 skip |
| `mutate_batch14.py` | **34/34 全红**(且不是靠炸掉模块;M8 是本批修的) |
| `mutate_batch14_2.py` | 16/16 全红(交叉回归) |
| `mutate_batch14_3.py` | 17/17 全红(交叉回归) |
| `mutate_batch14_4.py` | **20/20 全红**(原 19 + 新增 Q20) |
| `audit-anchors` | 87/87(4 份脚本) |
| verify-delivery | 13/13 |
| verify-imports | 356 个文件 |
| verify-sample-data | 5/5 |
| **Vitest** | **66/66**(原 62 + 新增 4) |
| **tsc --noEmit** | **干净** |
| **ESLint** | **0 errors**(原 13),5 warnings 既有 |
| 前端 syntax-check | 84/84 |

**仍然没跑过**:`pytest`(要起数据库)、`ruff`、Alembic 升降级、Playwright
E2E(要浏览器,本机装不了)。

---

## 七、遗留

1. **`_DOC_ONLY` 式的白名单没有,但 `crashed` 标记仍然只是标记。**
   4.2 的根因不是没检测到,是**检测到了不阻断**。`mutate_*.py` 现在对
   crashed 的处理是 `ok = False` 会让退出码非零 —— 这一点是对的;
   问题在于报告里那行 `<- 导入期就炸了,这条变异不算数` 混在 34 行里,
   跟一行普通的 RED 长得太像。该把它提到摘要行上("34 条里 1 条不算数"),
   否则下一次还是没人回来修。

2. **5 条 `exhaustive-deps` warning 没动。** 它们是真的可能有 bug
   (`CopyTab` 那条尤其:`useEffect` 漏了四个依赖,表现会是切商品时
   文案没跟着刷新)。要单独一轮,配 Vitest 用例,不能跟着 lint 清理走。

3. **`suite_filter` 的精确匹配只覆盖了变异脚本。** `p0_gate.py` 仍然用
   子串 `"batch12_7"`。今天它只挑中一个文件,但那是巧合不是保证 ——
   下一次有人加 `test_a45_batch12_7_2_fixes.py`,同样的归因问题会在
   P0 门禁上重演一遍。

4. **本批没有改任何后端运行时代码**,改的是 4 个前端组件的展示层
   (Tag -> BrandTag)、6 处文案、2 条测试的口径、1 条变异的写法。
   前端那 4 个组件过了 tsc / ESLint / Vitest / syntax-check,
   但**没有人在浏览器里看过** —— `BrandTag` 的浅色形态在
   `ModelTemplatesPage` 的表格密度下是否还读得出来,要人眼看一次。
