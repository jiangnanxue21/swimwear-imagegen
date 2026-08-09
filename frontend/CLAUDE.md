# CLAUDE.md — 前端

React 18 + TypeScript + Vite 5 + antd 5 + TanStack Query + react-router 6。
Node 22(与 `Dockerfile` 的 `node:22-alpine` 对齐,分叉会导致 CI 绿而镜像红)。

## 门禁与顺序

```bash
npm ci            # 严格按 lockfile,两份清单不一致直接失败
npm run typecheck # tsc --noEmit    最快、报错最准,先跑
npm run lint      # eslint .
npm run test      # vitest run
npm run build     # tsc -b && vite build   最慢,但唯一验证产物真能生成的一步
npm run syntax-check
```

`make fe-check` 和 CI 的 frontend job 都按这个顺序跑。改顺序要三处一起改,
否则本地和 CI 的失败点会不一致,复现时得靠猜。

E2E 单独跑:`npm run e2e`(先 `npx playwright install chromium`)。

## 测试分层

```
frontend/tests/**/*.test.ts(x)   Vitest + Testing Library + msw。组件行为、hook、
                                 URL 筛选、未保存保护、错误提示这类
frontend/tests/e2e/*.spec.ts     Playwright。跑 vite preview 的**构建产物**
```

三条硬规则:

1. **0 skip。** 方案 10.1 节要求,两处拦:`tests/setup.ts` 的 afterAll 在运行期拦,
   `backend/tools/verify_delivery.py` 在源码层拦(CI 第一步就报,不需要 node_modules)。
   写 `it.skip` / `test.todo` 会让交付自检直接红。

2. **`tests/` 必须在 `tsconfig.json` 的 include 里。** 用例当初放在 `src/` 外面
   是为了不让 typecheck 因为找不到 `describe`/`expect` 而红;装上 vitest 之后
   这个理由没了。不纳进来的话,一批不被 tsc 看的测试会慢慢和源码类型脱节。

3. **不要用 Python 扫前端源码来做断言。** `test_frontend_contract.py` 曾经有 86 条
   干这个,已经拆到只剩 10 条真正的跨语言契约(枚举↔标签表、API 路径存在性、
   字段长度上限)。重复顶层变量交给 TypeScript,未绑定变量交给 ESLint,
   lockfile 一致性交给 `npm ci`,组件里有没有某个按钮交给 Vitest。

## Playwright 现在只是骨架

`playwright.config.ts` 的边界写在文件顶部:任务 3(接骨架)在 P0,
任务 24(补完整主流程)在 P5,依赖任务 20 的发布页。

**「那些页面现在还不存在」这句话已经过期**(A45-batch24 订正):
`src/pages/PublishPage.tsx` 在阶段 4 就落地了,syntax-check 与 tsc 都覆盖着它。
任务 24 仍未开工,但拦着它的不再是"页面不存在",而是**没有人写过那些用例**
—— 两者要做的事完全不同,而这句过期的话会让下一个人先去找页面。


骨架阶段用例内部 `page.route()` 拦掉 `/api/**`,不连后端。
连后端等于把「前端能不能起来」和「数据库在不在」绑成一个红灯,两者修法完全不同。

跑的是 `vite preview` 的构建产物不是 dev server:两者在本仓库分叉过一次
(`tsc -b` 报错而 dev 一切正常)。代价是每次 E2E 先 build 一次,这是对的代价。

## 状态一律由后端驱动

前端**不许**自己推测这些:

```
非 OPEN = 已解决          任务无错误 = 成功
文件已生成 = 已上传        API 返回 200 = 已发布
```

后端返回 `display_status` / `next_action` / `blocking_reasons` / `allowed_actions`,
前端只负责展示和触发动作。判断逻辑写在前端意味着同一个业务规则有两份实现,
而它们迟早不一致 —— 不一致的那天,运营看到的是「已上架」,平台上什么都没有。

关键页面必须可见的信息(方案 4.1 节 F):Mock/真实、测试/UAT/生产、
真实渠道/Simulator、渠道、站点、语言、干跑/真实提交、发布状态。
其中「真实渠道 / Simulator」与环境标识对应任务 5、6。
**这里原来写着「尚未实现」—— 那句话是错的**:a37 已经落了
`GET /api/environment` + `src/components/EnvironmentBanner.tsx`,
A42 又修正了三列的上报口径(Mock 出图 / Mock 评分曾被报成 REAL、
正常工作的渠道 Simulator 曾被报成 UNAVAILABLE)。12.1 表里两条都标 ✅。
按这一段开工的人会去重写一个已经挂在页面上的横幅。

新接一个真后端时,`is_simulator` 由实现类自己声明(默认 True),
不查名单 —— 忘了写 `is_simulator = False` 只会多喊一次警告,
不会反过来把假的说成真的。

## 代码组织

```
src/api/          按资源分文件,统一走 client.ts(拦截器、错误归一、身份头)
src/components/   通用组件 + workbench/ 下的工作台各 Tab
src/hooks/        useUrlFilters(URL 是唯一真相)、useServerSort、useIdentity、useThemeMode
src/theme.ts      antd token 与暗色模式,颜色不要写死在组件里
```

`useUrlFilters` 的约定:筛选条件进 URL query,刷新和分享链接都要能还原同一屏。
新增筛选项时一并加进去,不要只存 useState。

**这里原来写的是 `useUrlSeed`,而那个文件在 A45-batch14-17 就删掉了** ——
它的做法是"用完把参数擦掉",于是 URL 只是初值、组件内 state 是第二处真相,
§8.2 的四条要求里三条落空。删它是刻意的:留着下一个人会照着抄回来。
照这一段去找 `useUrlSeed` 的人会找不到文件,然后多半自己写一个 —— 
那正是它被删掉要防的东西。
