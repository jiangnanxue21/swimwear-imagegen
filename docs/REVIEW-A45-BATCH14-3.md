# REVIEW:A45-batch14-3(走读:变异工具链 + 解析失败的诊断通道)

审的是 batch14-2 交付包本身的两处工具债与一处服务端诊断缺口。
起点是 batch14-2 第三节留下的那句话 ——「锚点过期是最常发生的一件事」——
把它做成一条**跑得起来的检查**之后,当场发现被审的那三份脚本里有一份
从头到尾没有验过任何东西。

> **一句话结论:三条,其中两条 P1。最重的一条(F7)是
> `mutate_contract_tests.py` 的 18 条变异全部失效,却一直打印
> 「18 个变异,18 个被测试抓住」并退出 0 —— 它点名的 15 个测试
> 在目标模块里一个都不存在,`getattr` 抛的 AttributeError 被
> 「非零 = 响了」读成了「被抓住」。退役它的代价是 **12 条决定
> 就此没有任何守卫**,清单在第四节,那是下一阶段的正题。**

---

## 一、发现

### F5(P2)解析失败时模型原文整条丢掉

`attributes/service.py` 的逐图 catch-all 只记 `type(exc).__name__`,
注释给的理由是「错误原文不入库(可能带 URL 里的凭据)」。

**这个理由对一半。** provider / 传输错误的消息里确实嵌着请求地址,
而 `EXTRACTOR_MODEL_SEND_PUBLIC_URLS=true` 时预签名地址的查询串就是凭据。
但它把 `ExtractionParseError` 一起吞了 —— 那一条是**我们自己的解析器**
对**模型输出**报的错:消息是我们写的字符串(「fields[3] 缺少字段名」),
`detail` 是 json 解码器的位置信息,两者都不含请求凭据。

代价不是「日志少一行」。模型开始返回坏 JSON 时(`json_object` /
`prompt_only` 两个降级档位连 Schema 都没有,这是**今天就会发生**的事),
运营看到的是那张图 failed、日志里一句 `error: ExtractionParseError`,
**没有任何办法知道模型到底说了什么**。唯一想得到的动作是再点一次识别,
而每一次都在花钱 —— 与 §4.5 那三个 `missing_reason` 指向三个不同动作
是同一个病灶,只是换到了日志这条通道上。

**修**:三处。
`schema.redact_for_log()` 是纯判定(抹查询串 + 截断,零依赖、边界穷举得起来);
`vision._parse_or_explain()` 在原文还有作用域的地方把摘要挂到
`ExtractionParseError.detail` 上(`extract()` 之外谁都拿不到 `text`);
`service.py` 按异常类型分支 —— 解析失败多记 `parse_error` 与 `detail`,
其余**原样**只记类型名。

**顺序不能反**:先抹后截。反过来做的话截断点可能落在查询串中间,
留下半截签名。守卫里有一条专钉这件事(把带签名的地址放在超出上限的位置上)。

**刻意不做的**:原文仍然**不入库**(`error_code` 还是只有类型名)——
日志有轮转和访问控制,`product_attribute_extractions` 没有。

### F6(P1)锚点/派发目标会过期,而失效的变异安静地什么都没验

batch14-2 第三节记下了这件事,但没有做成检查。变异脚本自己发现不了 ——
它跑一次要几十份工作树、十几分钟,所以刻意不进 CI,于是锚点可以过期很久。

**修**:`tools/audit_anchors.py`,只解析、不执行,秒级、零子进程,
进 `check-offline` 与 `make check`。

**为什么必须解析而不是执行**,这一轮有硬证据:`mutate_contract_tests.py`
**没有 `__main__` 保护**,变异循环写在模块顶层 —— import 它一次就是当场
改写工作树里的 `App.tsx` / `TodayPage.tsx` / `flow.py`,再起 18 个子进程。
一份「审计工具」把被审对象跑起来,本身就是事故。`ast` 只要求语法合法,
被审脚本依赖缺失、甚至自己有 bug 时它照样给得出答案。

两种形状都认,行布局全收在 `SHAPES` 一张表里(加第三种只动那张表,
不是在 `main()` 里加 `if`):

    MUTATIONS  (编号, 一句话, 路径, 原文, 替换成)      基准 backend/
    CASES      (一句话, 路径, (原文, 替换成), 测试名)   基准 项目根

查七件事:锚点恰好出现一次(**零次和两次都算失败** —— 三份脚本里只有
`mutate_batch14_2.py` 自己拦两次,另外两份是 `old not in text` 和
`assert old in original`,然后 `replace(..., 1)` 悄悄改掉第一处,
而那一处可能是注释)、目标文件在不在、路径基准对不对、原文≠替换成、
派发的测试函数还在不在、`SUITE_FILTER` 匹配得到用例、行读不读得出来。

**`SUITE_FILTER` 那条是失锚的镜像**:锚点没了 -> 变异改不进去 -> 套件照常绿
-> 报 GREEN;过滤器匹配不到文件 -> 套件里一条用例都没有 -> 平凡通过
-> **每一条变异都报 GREEN**。两者都让脚本退出非零,但后者的报告会指向
「守卫不咬人」,让人去改守卫 —— 而坏掉的是过滤器。

### F7(P1)`mutate_contract_tests.py` 的 18 条全部失效,却报告「全部被抓住」

审计一跑就命中。它 `getattr(m, name)()` 派发的 15 个测试名,在
`tests.pure.test_frontend_contract` 里**一个都不存在** —— 那个模块已经被
改写成契约比对(标签 / 枚举 / 路由 / 字段上限),A8/A9 那一批导航与
离开保护的守卫整体不在了。

名字没了就是 `AttributeError`、子进程退出码 1,而脚本按
`rc != 0` 判定「响了」。**它一直打印「18 个变异,18 个被测试抓住」
并退出 0。** 这正是 `mutate_batch14.py` 顶部记的那个教训
(「只看红绿的变异验证会把假红当成证据」)在老脚本上的原样重演 ——
新脚本补的 `crashed` 检测,marker 名单里也没有 `AttributeError`。

另有一条独立失效:`CASES[9]` 的锚点 `const { isAdmin } = useIdentity()`
在 `AppLayout.tsx` 里已经长成 `{ isAdmin, who, loading: identityLoading }`。
审计一次把两个问题都报出来(不在第一条上 return)—— 只报第一条的话,
修完派发名字再跑一次才发现锚点也过期了。

**修**:退役这份脚本。18 行指向的守卫整体不在了,重新对不回去
(主题本身有一半已经无人守卫,见第四节),而一份会假报「被抓住」的
工具比没有工具更糟。

**代价记在第四节,不是"顺手删掉"。**

---

## 二、守卫与变异

`tests/pure/test_a45_batch14_3_fixes.py`,15 条。

F6 的守卫盯的是审计工具自己 —— **审计工具也要被审**。一份"扫不出任何问题"
的审计比一条恒绿的守卫更危险:它给出的是"全都对得上"这种结论,
读的人会据此不再检查。所以每一条 `test_the_audit_reports_*` 都先在临时目录里
造一份**故意坏掉的**变异脚本,再断言审计报出来 —— 先造变异、再写断言,
反过来做就是 batch13-3 / M11 的来路。

fixture 同时覆盖两种形状。`CASES` 在 F7 之后仓库里已经没有生产用例了,
没有 fixture 的话,审计里那一半解析代码就变成谁也没走过的死代码 ——
batch13 的 M9、batch13-3 的 M11 记的都是这个教训。

另有两条不靠 fixture:
- `test_the_audit_never_imports_the_scripts_it_audits` 解析审计自己的 AST,
  钉住它没有 `import_module` / `exec` / `eval`。这是这份工具存在的前提。
- `test_every_mutation_script_in_the_tree_currently_passes_the_audit`
  对**真树**跑一次。上面全是 fixture,那样的话审计可以完美地审一棵
  不存在的树 —— 这条让"锚点全部对得上"成为一个会被 CI 打断的事实。

审计自身另经 8 种失败形状的手工变异(锚点过期 / 不唯一 / 原文=替换成 /
未知形状 / 元组宽度变了 / 路径基准写错 / f-string 读不出 / 派发目标不存在),
全部报出;并有一条正向对照:把缺的那个函数补进去,18 → 17,
证明派发检查不是恒红。

---

## 三、跑过的、没跑过的

**跑过(这台机器)**:`audit-anchors` 2 份脚本 50/50 锚点全绿、
本批守卫 15/15、`verify-delivery` 13/13、`verify-imports` 352 个文件。

**没跑过(换有前提的机器必须补)**:`pytest`(这台机器没有 pytest,
也没有 pydantic / sqlalchemy)、`ruff`、Alembic 升降级、
`tsc` / ESLint / Vitest —— 本批没改前端,但 F5 改了 `service.py`,
它要 sqlalchemy 才 import 得动,守卫走的是读源码这条路。

---

## 四、退役 `mutate_contract_tests.py` 的代价:12 条决定现在没有守卫

按每条变异改的那个符号回查 `tests/pure/`,**18 条里 12 条的主题
今天没有任何守卫**。这不是本批造成的 —— 它们在 `test_frontend_contract.py`
被改写的那一刻就已经没了,只是一直被那份假绿的脚本盖着。

| # | 决定 | 文件 |
|---|------|------|
| 0 | `/settings` 不在普通运营菜单里 | `frontend/src/App.tsx` |
| 1 | 每个菜单项都指向真实路由 | `frontend/src/App.tsx` |
| 2 | 默认路由落在待办首页而不是仪表盘 | `frontend/src/App.tsx` |
| 3 | 路由不按角色裁剪(裁的是页面里的动作) | `frontend/src/App.tsx` |
| 4 | 首页数字来自后端,不自己加总 | `frontend/src/pages/TodayPage.tsx` |
| 7 | 首页不自己抄一份运行中状态清单 | `frontend/src/pages/TodayPage.tsx` |
| 8 | 落地页接受 URL 传来的筛选 | `frontend/src/pages/TaskListPage.tsx` |
| 11 | `in_flight` 从状态机取,不写死清单 | `backend/app/services/dashboard_service.py` |
| 12 | 路由必须是 data router(否则 `useBlocker` 静默失效) | `frontend/src/main.tsx` |
| 13 | 布局路由渲染 `Outlet` | `frontend/src/components/AppLayout.tsx` |
| 14 | 离开保护挡刷新,不只挡站内导航 | `frontend/src/components/UnsavedGuard.tsx` |
| 15 | 保存后放行挂起的导航 | `frontend/src/components/UnsavedGuard.tsx` |

另 6 条的主题仍有守卫(散在 `test_workbench_flow.py`、
`test_a44_batch4_fixes.py`、`test_a44_batch6_fixes.py` 等),不必重写。

其中 **12 / 13 / 14 / 15 是一组**:它们合起来是"未保存的编辑不会被
四种离开方式吞掉"这一条决定,而 `useBlocker` 在非 data router 下
**不报错、直接失效** —— 正是"静默失效"那一类,最值得优先补。

---

## 五、遗留(看见了、刻意没动)

1. **`SUITE_FILTER` 是子串匹配。** `mutate_batch14.py` 的
   `"a45_batch14"` 会同时匹配 `_2` 与 `_3` 两个套件 —— 不影响红绿判定,
   但会拖慢并让失败归因变糊。与 A45-#36 那个前缀匹配缺陷同族。
2. **审计只查静态锚点,不查变异是否"有意义"。** 一条锚点对得上、
   替换也真的改变了行为、但守卫压根不覆盖那段逻辑的变异,仍然只能靠跑。
3. **`fabricated_field_count` 数的是次数不是字段数**(batch14-2 §五 已记)。
4. **原文摘要没有入库。** 事后回溯只能翻日志;要做成"识别记录上能看到
   模型当时说了什么",得加列 + 迁移 + 脱敏口径评审,不在走读范围。
