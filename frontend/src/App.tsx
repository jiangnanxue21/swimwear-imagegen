import { lazy } from 'react'
import { createBrowserRouter, createRoutesFromElements, Navigate, Route } from 'react-router-dom'
import {
  ApiOutlined, DashboardOutlined, FileTextOutlined, HeartOutlined,
  FolderOpenOutlined, PictureOutlined, ProfileOutlined, SettingOutlined,
  TeamOutlined, ThunderboltOutlined, WarningOutlined,
  HistoryOutlined, CheckSquareOutlined, HomeOutlined, WalletOutlined,
  CloudUploadOutlined,
  ExperimentOutlined,
} from '@ant-design/icons'
import AppLayout, { type NavGroup } from './components/AppLayout'
import NotFoundPage from './pages/NotFoundPage'
import TodayPage from './pages/TodayPage'

/**
 * 路由级代码分割(a51)。
 *
 * ## 改之前:28 个页面全部静态 import,首屏一个 1.9MB 的 chunk
 *
 * 构建自己一直在警告(`Some chunks are larger than 500 kB`),而这份文件把
 * 每一个页面都拉进了入口 —— 一个只想看今日待办的人,要先下完发布页、
 * 审核台、批次页、设置页的全部代码。
 *
 * ## 这三个保持同步导入,是刻意的
 *
 *     LoginPage     未登录时的第一眼。懒加载它等于在最需要"快"的那一刻
 *                   多一次网络往返,而它很小
 *     TodayPage     `/` 的默认落点。首屏必到,拆出去只是多一个请求
 *     NotFoundPage  兜底路由。它要在"什么都不对"的时候还能渲染出来,
 *                   而那正是最不该依赖"再取一个 chunk"的时刻
 *
 * ## Suspense 边界在 `AppLayout` 里,不在这里
 *
 * 包在这里的话,切页时整个界面(含侧栏)会被 fallback 顶掉一帧,
 * 表现是每次点菜单都闪一下。放在 `<Outlet />` 外面,侧栏和顶栏留在原地,
 * 只有内容区显示加载态 —— 这也是懒加载唯一会被用户看见的地方。
 */
const AuditLogPage = lazy(() => import('./pages/AuditLogPage'))
const AITestPage = lazy(() => import('./pages/AITestPage'))
const DashboardPage = lazy(() => import('./pages/DashboardPage'))
const MediaLibraryPage = lazy(() => import('./pages/MediaLibraryPage'))
const ModelTemplatesPage = lazy(() => import('./pages/ModelTemplatesPage'))
const ProductDetailPage = lazy(() => import('./pages/ProductDetailPage'))
const ProductListPage = lazy(() => import('./pages/ProductListPage'))
const PromptsPage = lazy(() => import('./pages/PromptsPage'))
const ProvidersPage = lazy(() => import('./pages/ProvidersPage'))
const PublishPage = lazy(() => import('./pages/PublishPage'))
const ReviewDetailPage = lazy(() => import('./pages/ReviewDetailPage'))
const ReviewQueuePage = lazy(() => import('./pages/ReviewQueuePage'))
const SettingsPage = lazy(() => import('./pages/SettingsPage'))
const SpendPage = lazy(() => import('./pages/SpendPage'))
const SpuCreatePage = lazy(() => import('./pages/SpuCreatePage'))
const SpuDetailPage = lazy(() => import('./pages/SpuDetailPage'))
const SystemStatusPage = lazy(() => import('./pages/SystemStatusPage'))
const TaskDetailPage = lazy(() => import('./pages/TaskDetailPage'))
const TaskListPage = lazy(() => import('./pages/TaskListPage'))
const WizardPage = lazy(() => import('./pages/WizardPage'))
const WorkbenchBatchPage = lazy(() => import('./pages/WorkbenchBatchPage'))
const WorkbenchExceptionsPage = lazy(() => import('./pages/WorkbenchExceptionsPage'))
const WorkbenchImportPage = lazy(() => import('./pages/WorkbenchImportPage'))
const WorkbenchListPage = lazy(() => import('./pages/WorkbenchListPage'))
const WorkbenchProductPage = lazy(() => import('./pages/WorkbenchProductPage'))
const WorkbenchReviewPage = lazy(() => import('./pages/WorkbenchReviewPage'))
const WorkbenchSpuPage = lazy(() => import('./pages/WorkbenchSpuPage'))
import LoginPage from './pages/LoginPage'

/**
 * 侧栏导航(A8:按工作阶段分组 + 按角色收敛;a47 §4:菜单收敛 13 → 7)。
 *
 * 「按角色收敛」那一半到 a46 才落地:标了 `adminOnly` 的组只对管理员显示。
 * 在此之前它一直是 `const visible = groups`,当时的理由与撤销它的理由
 * 都写在 `AppLayout.tsx` 那一行上。**路由不跟着裁**,见下面那一节。
 *
 * ## a47:运营那三组从 13 项收到 7 项
 *
 * 收敛的判据是**同一件事只留一个入口**,不是"少即是好"。六项的去处:
 *
 *     /reviews          候选图审核 —— 审核中心顶部的计数入口通向它
 *     /products         工作台详情覆盖日常;原始信息走商品详情的深链
 *     /spus/new         工作台「新建」里的「新建商品款式」
 *     /workbench-import 工作台「新建」里的「批量导入 SKU」
 *     /workbench-spus   工作台的「按款」视图取代它
 *     /tasks            工作台详情的「生成任务」页签
 *
 * **`/tasks` 那一条有硬顺序。** 它移进「系统管理」组等于对运营彻底消失,
 * 前提是工作台详情的生成任务页签先能回答"我这件商品的任务跑到哪了" ——
 * 状态、轮次、失败原因三样。a47 §3.2 先落地了那三样,这一步才动。
 * 反过来做,运营会有一段时间两头落空。
 *
 * ## 路由一条都没删
 *
 * 撤出菜单 ≠ 删路由。上面六条手输地址全部照常打开,深链、书签、
 * 聊天窗里发的链接一条都没断。`nav-and-url-filters.test.tsx` 的
 * `declaredPaths()` 那条断言钉着这一点。
 *
 * ## 「导出」组里没有独立的导出中心页
 *
 * 仓库里从来没有这一页:单件导出在商品详情的导出标签,批量导出的文件在
 * 批量任务页。不为了对齐一个菜单名去新建页面,所以这一组指向批量任务页,
 * 单件路径由 A9 首页的「可导出」卡片进入。
 *
 * ## 组的顺序就是一天的顺序
 *
 * 今日工作 -> 商品生产 -> 导出。运营早上打开侧栏,从上往下就是他今天的动线;
 * 「系统管理」在最下面,而且**只有管理员看得见**(`adminOnly: true`)。
 */
export const NAV: NavGroup[] = [
  {
    label: '今日工作',
    items: [
      { key: '/today', label: '工作首页', icon: <HomeOutlined /> },
      // a47 §6:改名「审核中心」。它是**唯一**一条审核入口,顶部按类别
      // 给计数,点进去仍是各自现有页面 —— 底层三套审核模型一个都没动
      { key: '/workbench-review', label: '审核中心', icon: <CheckSquareOutlined /> },
      { key: '/workbench-exceptions', label: '异常与驳回', icon: <WarningOutlined /> },
    ],
  },
  {
    label: '商品生产',
    items: [
      { key: '/workbench', label: '商品工作台', icon: <ProfileOutlined /> },
      { key: '/media', label: '素材库', icon: <FolderOpenOutlined /> },
    ],
  },
  {
    /*
     * 组名从「导出」改成「导出与上架」(B-02)。
     *
     * 改的不只是一个词:在这之前流水线在导出那一步**就断了** —— 后端的
     * `/publish/*` 六个端点从任务 18 起就在,而侧栏里没有任何一项通向它们,
     * 于是运营的动线到"下载上架文件"为止,再往后只能拿 curl。
     * 一天的顺序里,上架就排在导出后面,所以它属于这一组而不是新开一组。
     */
    label: '导出与上架',
    items: [
      { key: '/workbench-batches', label: '批量与导出', icon: <ThunderboltOutlined /> },
      { key: '/publish', label: '发布上架', icon: <CloudUploadOutlined /> },
    ],
  },
  {
    /*
     * 只给管理员看(PRD §30)。这是 a46 新做的功能,不是恢复什么 ——
     * 在那之前 `AppLayout` 那一行是 `const visible = groups`,
     * 而 `nav-and-url-filters.test.tsx` 还有一条反向门禁明令禁止这个字段。
     * 撤那条约束的理由:密码在 .env 里之后,没有人再需要"先进设置页把
     * 自己变成管理员"—— 那正是当初"始终可见"的全部理由。
     *
     * a47 把 `/tasks` 收进这一组。它对运营是**整组不可见**,所以这既不是
     * 降级也不是折叠,是消失 —— 前提见文件头那一段的硬顺序。
     * 排在最前面是因为它是这一组里唯一一条业务排障入口,其余八项是配置。
     */
    adminOnly: true,
    label: '系统管理',
    items: [
      { key: '/tasks', label: '生成任务(排障)', icon: <PictureOutlined /> },
      { key: '/ai-tests', label: 'AI 能力测试', icon: <ExperimentOutlined /> },
      { key: '/dashboard', label: '指标仪表盘', icon: <DashboardOutlined /> },
      { key: '/spend', label: '付费调用花费', icon: <WalletOutlined /> },
      { key: '/model-templates', label: '模特模板', icon: <TeamOutlined /> },
      { key: '/providers', label: '出图服务商', icon: <ApiOutlined /> },
      { key: '/prompts', label: '提示词', icon: <FileTextOutlined /> },
      { key: '/settings', label: '设置', icon: <SettingOutlined /> },
      { key: '/audit', label: '操作审计', icon: <HistoryOutlined /> },
      { key: '/system', label: '系统状态', icon: <HeartOutlined /> },
    ],
  },
]

/**
 * 运营看得见的菜单项数。**门禁按它做一致性断言,散文里不抄这个数。**
 *
 * 仓库规矩(CLAUDE.md「数字不写死」):会变的数写进守卫,别抄进文档 ——
 * README 里那句「7 项」在改成 8 项时不会有任何地方变红,而这一行会。
 */
export const OPERATOR_NAV_ITEM_COUNT = NAV
  .filter((g) => !g.adminOnly)
  .reduce((sum, g) => sum + g.items.length, 0)

/**
 * 路由表。
 *
 * ## 为什么是 `createBrowserRouter` 而不是 `<BrowserRouter>` + `<Routes>`
 *
 * A11 要求「点菜单离开未保存的页面时挡一下」,而 react-router 的 `useBlocker`
 * **只在数据路由下可用** —— 挂在 `<BrowserRouter>` 里它直接抛错。
 * 换掉的只是装配方式:路由仍然用 JSX 声明(`createRoutesFromElements`),
 * 这张表逐行没动,评审时还是同一份东西。
 *
 * 顺带得到的是布局路由:`AppLayout` 成为父路由,子页面从 `<Outlet />` 出来,
 * 不再靠 `children` 透传。
 *
 * ## 菜单按角色裁剪,**路由不裁**
 *
 * 两件事分开:菜单收敛是可发现性(运营不必看见一组点了只会失败的入口),
 * 路由全量注册是安全设计。operator 手输 `/settings` 打得开,页面上是一句
 * 403 —— 真正的边界在后端 `require_admin`,前端再加一道守卫只会多出一处
 * 可能与后端不一致的地方。**不要**因为"菜单都藏了"就顺手删路由:
 * 那会把一个说得清楚的 403 变成一个看不懂的 404。
 */
export const router = createBrowserRouter(
  createRoutesFromElements(
    <>
      {/*
        登录页(a46-phase2)。**是 `AppLayout` 的兄弟,不是它的子路由。**

        挂进布局路由的表现:未登录的人看到一个完整的侧栏,点哪一项都被
        `AppLayout` 弹回登录页 —— 而他会以为是自己点错了。更糟的是 `AppLayout`
        本身要跳登录页,而登录页在它里面,那就是一次自指的重定向。

        它也**不在**上面 NAV 的任何一组里:登录是一次性的动作,不是一个
        随时可以去的地方。退出登录挂在顶栏身份区。
      */}
      <Route path="/login" element={<LoginPage />} />
      <Route element={<AppLayout groups={NAV} />}>
        {/* A9:默认落点从指标仪表盘改成今日待办。/dashboard 保留给管理员看技术指标 */}
        <Route path="/" element={<Navigate to="/today" replace />} />
        <Route path="/today" element={<TodayPage />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/workbench" element={<WorkbenchListPage />} />
        <Route path="/workbench/:id" element={<WorkbenchProductPage />} />
        {/* 一体化向导(阶段 6 / AC-01)。**不挂在 `/workbench/:id/wizard` 下** ——
            那个前缀的下一段已经是商品 id 的子路由空间,而向导是一条与八标签页
            并列的视图,不是它的下级。`?step=` / `?color=` 承担刷新恢复(AC-16) */}
        <Route path="/wizard/:id" element={<WizardPage />} />
        {/* 阶段 3。刻意不挂在 /workbench/* 下:那个前缀的下一段是商品 id,
            加一个 /workbench/batches 会和 :id 抢同一个位置 */}
        <Route path="/workbench-review" element={<WorkbenchReviewPage />} />
        <Route path="/workbench-batches" element={<WorkbenchBatchPage />} />
        <Route path="/workbench-exceptions" element={<WorkbenchExceptionsPage />} />
        {/* 三步建档(阶段 1)。放在 /spus/new 而不是 /workbench-spus/new:
            后者的前缀属于只读聚合视图,而这是一条写动线 */}
        <Route path="/spus/new" element={<SpuCreatePage />} />
        {/* 生成方案面板的宿主页(阶段 4)。**必须排在 /spus/new 后面** ——
            `:spuId` 会把 "new" 也吃掉,那时建档页永远打不开,而路由不报错。
            react-router 的排序是分数优先、静态段胜过动态段,所以今天两条
            顺序反过来也对;写成这个顺序是为了让读代码的人不必去查那条规则 */}
        <Route path="/spus/:spuId" element={<SpuDetailPage />} />
        <Route path="/workbench-spus" element={<WorkbenchSpuPage />} />
        <Route path="/workbench-import" element={<WorkbenchImportPage />} />
        {/* B-02:发布链路的前端入口。`?product_id=` 会直接打开提交弹窗 */}
        <Route path="/publish" element={<PublishPage />} />
        <Route path="/audit" element={<AuditLogPage />} />
        <Route path="/products" element={<ProductListPage />} />
        <Route path="/products/:id" element={<ProductDetailPage />} />
        <Route path="/media" element={<MediaLibraryPage />} />
        <Route path="/tasks" element={<TaskListPage />} />
        <Route path="/tasks/:id" element={<TaskDetailPage />} />
        <Route path="/ai-tests" element={<AITestPage />} />
        <Route path="/reviews" element={<ReviewQueuePage />} />
        <Route path="/reviews/:id" element={<ReviewDetailPage />} />
        <Route path="/model-templates" element={<ModelTemplatesPage />} />
        <Route path="/providers" element={<ProvidersPage />} />
        <Route path="/prompts" element={<PromptsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/spend" element={<SpendPage />} />
        <Route path="/system" element={<SystemStatusPage />} />
      {/*
        A-36:兜底路由改成 404 页,不再静默跳首页。
        原来任何一个坏链接、任何一次 base-path 配错、任何一条改过名的路由,
        表现都是"莫名其妙回到了今日" —— 而那看起来完全正常,于是没有人报。
        部署侧的 base-path 问题尤其:整站每个深链都会跳首页,而首页是好的。
      */}
      <Route path="*" element={<NotFoundPage />} />
      </Route>
    </>,
  ),
)
