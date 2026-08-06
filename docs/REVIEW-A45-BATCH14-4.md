# REVIEW:A45-batch14-4(把退役 `mutate_contract_tests.py` 时失去的守卫补回来)

batch14-3 退役了 `tools/mutate_contract_tests.py` —— 它的 18 条变异全部失效
却报告"全部被抓住"。退役时第四节记了账:**那 18 行指向的 15 个守卫,
主题一条都没有被别处接住**。本批把这 15 条补回来。

> **一句话结论:15 条守卫、19 条变异全红。补的过程里自己踩中两次
> 这个仓库反复出现的陷阱 —— 一次是"宽集合让『必须属于』失效"(假通过),
> 一次是"文件里出现过这串字 != 这行代码在生效"(第四次出现)。
> 两次都是变异验出来的,不是走读看出来的。**

---

## 一、先修正上一轮的一个判定

batch14-3 第四节说"18 条里 12 条无人守卫,另 6 条的主题仍有守卫"。
**那 6 条是误报。** 逐条读过之后:

| 探针 | 命中的文件 | 实际在守什么 |
|------|-----------|------------|
| `RESOLVE_REJECTION` | `test_workbench_flow.py` | 后端状态机 `result.next_action.code`,与首页卡片指向哪条路由无关 |
| `RELEASE_QUARANTINE` | `test_workbench_flow.py` | 同上 |
| `useIdentity` | `test_a44_batch6_fixes.py` 等三处 | hook 自身的内部逻辑、ColdStartBanner 用了它 —— 没有一条守"菜单不再开第二份 whoami" |
| `UnsavedGuard` | `test_a44_batch4_fixes.py` | ImageSetTab 草稿被 refetch 清空,注释里提了一句离开保护 |
| `NextActionCode` | 7 个文件 | 后端流转判定,不是首页零填充 |
| `by_next_action` | 无 | grep 零命中 |

用「符号在测试里出现过」判断「那条决定被守着」,和用「文件里出现过这串字」
判断「这行代码在生效」是同一个陷阱,只是换了一层。**正确的数字是 18 行 /
15 个不重复的守卫名,全部无人守卫。**

## 二、补了什么

`tests/pure/test_a45_batch14_4_fixes.py`,15 条,对应 `tools/mutate_batch14_4.py`
的 19 条变异(原 18 条 + 新增 Q9「卡片跳到没注册的路由」)。

全部读源码、不 import:这些决定住在前端,纯层没有运行时。一律先过
`_code_only()` —— A8/A9 那一轮 12 个变异有 2 个没被抓住,两处都是
"断言代码里用了 X"被注释里的 X 满足了。

变异脚本换了两件事,都不是顺手改的:

    形状   CASES -> MUTATIONS,与在用的另外三份一致
    机制   改真实工作树 -> 复制到临时目录再改
    派发   点名单条测试 -> 跑整个套件

第三条是 F7 的正题:`getattr(m, name)()` 在测试改名时抛 `AttributeError`,
退出码和断言失败一样是非零,于是失效被读成"被抓住"。按套件跑没有这个洞,
而且 `audit_anchors.py` 的 `SUITE_FILTER` 检查会先一步拦住。

## 三、补的过程里自己踩的两个坑

### 3.1 宽集合让「必须属于」类断言失效(假通过)

第一版用正则从 `flow.py` 里抓动作码:`^\s{4}([A-Z_]+)\s*=\s*["']`。
那个文件里不止一个枚举,于是 `FlowStep` 的 `MATERIAL` / `ATTRIBUTE` /
`IMAGE_SET` 等等一起被捞进来。

后果分两半,**只有一半会报错**:

- `test_secondary_actions_do_not_hide_a_whole_step` 当场红,报了 12 个
  根本不是动作码的名字 —— 这一半有人管。
- `test_today_page_cards_point_at_real_actions_and_real_routes` 断言的是
  "首页用的码必须**属于**真实码集合",集合被撑大之后它**假通过**了 ——
  这一半没有任何迹象。

「集合太宽」对两类断言的表现相反:对"必须覆盖"是假红,对"必须属于"是假绿。
已改成按 AST 取 `NextActionCode` 类的成员。

### 3.2 「文件里出现过这串字」—— 第四次

`test_landing_pages_accept_the_filter_from_the_url` 第一版写的是
`assert "useUrlSeed" in src`。变异 Q10 注释掉的是 **import 那一行**,
而调用点 `const { seed } = useUrlSeed<TaskStatus>(...)` 里还留着这个名字 ——
断言照样满足,守卫绿着,而那个页面其实根本编译不过。

**这是同一个陷阱在本仓库的第四次出现**(batch13-3 的 M2 与 M11、
batch14-2 的 N15 是前三次)。已改成行首整行锚定 import,并加一条
"import 了就必须真的调用"。

两条都不是走读看出来的,是变异验出来的 —— 与 batch14-3 的 P1
(验出我自己写的"顺序是安全边界"站不住)同一个来路。

## 四、跑过的

**跑过(这台机器)**:纯逻辑 **2056/2056**(跳过 7,缺 pydantic/sqlalchemy)、
本批变异 **19/19 全红**、batch14-3 变异 17/17 仍全红(交叉回归)、
`audit-anchors` 4 份脚本 **86/86**、verify-delivery 13/13、
verify-imports 355、verify-sample-data 5/5、前端 syntax-check 84/84。

**没跑过(换有前提的机器必须补)**:`tsc` / ESLint / Vitest —— 本批
**没有改任何前端源码**,只加了读它们的守卫,但 Q10 那条暴露的问题
(注释掉 import 仍能通过静态守卫)正是 `tsc` 该兜的一层;
`pytest`、`ruff`、Alembic 升降级同前。

## 五、遗留

1. **这 15 条守的是"代码长什么样",不是"运行起来对不对"。**
   `test_router_is_a_data_router_so_blocker_works` 断言的是源码里有
   `createBrowserRouter` —— 真正的证明要 Vitest 挂一个脏表单再触发导航。
   静态守卫拦的是"有人手滑改回去",拦不住"这套机制从来就没生效过"。
   Vitest 那一层是 FE 侧的正题。
2. **`SUITE_FILTER` 子串匹配**(batch14-3 §五 已记):现在
   `"a45_batch14"` 会同时匹配 `_2` `_3` `_4` 四个套件。随批次增加,
   `mutate_batch14.py` 每条变异的耗时线性上涨,该收成精确匹配了。
3. **Q19 的变异是 `dirty={true}`,而写死 `false` 同样有害**(等于没挂)。
   守卫两种都拦,但变异只造了恒真那一种。
