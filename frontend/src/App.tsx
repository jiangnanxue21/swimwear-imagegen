import { createBrowserRouter, createRoutesFromElements, Navigate, Route } from 'react-router-dom'
import {
  ApiOutlined, AuditOutlined, DashboardOutlined, FileTextOutlined, HeartOutlined,
  FolderOpenOutlined, PictureOutlined, ProfileOutlined, SettingOutlined, SkinOutlined,
  TeamOutlined, ThunderboltOutlined, WarningOutlined, ImportOutlined, ClusterOutlined,
  HistoryOutlined, CheckSquareOutlined, HomeOutlined, WalletOutlined,
  CloudUploadOutlined,
} from '@ant-design/icons'
import AppLayout, { type NavGroup } from './components/AppLayout'
import NotFoundPage from './pages/NotFoundPage'
import SystemStatusPage from './pages/SystemStatusPage'
import TodayPage from './pages/TodayPage'
import WorkbenchListPage from './pages/WorkbenchListPage'
import WorkbenchProductPage from './pages/WorkbenchProductPage'
import WorkbenchBatchPage from './pages/WorkbenchBatchPage'
import WorkbenchReviewPage from './pages/WorkbenchReviewPage'
import WorkbenchExceptionsPage from './pages/WorkbenchExceptionsPage'
import WorkbenchImportPage from './pages/WorkbenchImportPage'
import WorkbenchSpuPage from './pages/WorkbenchSpuPage'
import AuditLogPage from './pages/AuditLogPage'
import MediaLibraryPage from './pages/MediaLibraryPage'
import ProductListPage from './pages/ProductListPage'
import ProductDetailPage from './pages/ProductDetailPage'
import TaskListPage from './pages/TaskListPage'
import TaskDetailPage from './pages/TaskDetailPage'
import ProvidersPage from './pages/ProvidersPage'
import ModelTemplatesPage from './pages/ModelTemplatesPage'
import ReviewQueuePage from './pages/ReviewQueuePage'
import ReviewDetailPage from './pages/ReviewDetailPage'
import DashboardPage from './pages/DashboardPage'
import PromptsPage from './pages/PromptsPage'
import SettingsPage from './pages/SettingsPage'
import SpendPage from './pages/SpendPage'
import PublishPage from './pages/PublishPage'

/**
 * 侧栏导航(A8:按角色收敛)。
 *
 * ## 与计划里那份清单的三处出入
 *
 * 计划 §3.2 给的普通运营清单是九项。落地时多留了三项,理由都是"少了它这条路
 * 就断了",不是"顺手留着":
 *
 *     商品        `/products` 是**唯一**能手工新建商品的地方(ProductFormModal),
 *                 而 Gate A 就是单件 UAT。工作台列表页的空状态也在指着它说
 *                 「先在「商品」页导入」—— 拿掉它,那句话会指向一个不存在的入口
 *     生成任务    A9 首页的「生成失败」「运行中任务」两张卡片点进来就是这一页。
 *                 首页有卡片、菜单没入口,运营第二次想看时会找不到路
 *     图片审核    `/reviews` 是候选图评分不过关时的落点(MANUAL_REVIEW)。
 *                 它和「待我处理」不是同一个对象:那边审的是图片集与文案,
 *                 这边审的是单张候选图
 *
 * ## 「导出」组里没有独立的导出中心页
 *
 * 计划写的是「导出中心」,但仓库里从来没有这一页:单件导出在商品详情的导出标签,
 * 批量导出的文件在批量任务页。Gate A 期间不为了对齐一个菜单名去新建页面
 * (§2.2 禁止扩张),所以这一组指向批量任务页,单件路径由 A9 首页的
 * 「可导出」卡片进入。
 *
 * ## 组的顺序就是一天的顺序
 *
 * 今日工作 -> 商品生产 -> 导出。运营早上打开侧栏,从上往下就是他今天的动线;
 * 「系统管理」在最下面,而且只有管理员看得见。
 */
export const NAV: NavGroup[] = [
  {
    label: '今日工作',
    items: [
      { key: '/today', label: '工作首页', icon: <HomeOutlined /> },
      { key: '/workbench-review', label: '待我处理', icon: <CheckSquareOutlined /> },
      { key: '/reviews', label: '图片人工审核', icon: <AuditOutlined /> },
      { key: '/workbench-exceptions', label: '异常与驳回', icon: <WarningOutlined /> },
    ],
  },
  {
    label: '商品生产',
    items: [
      { key: '/workbench', label: '商品工作台', icon: <ProfileOutlined /> },
      { key: '/products', label: '商品', icon: <SkinOutlined /> },
      { key: '/workbench-import', label: '商品导入', icon: <ImportOutlined /> },
      { key: '/media', label: '素材管理', icon: <FolderOpenOutlined /> },
      { key: '/tasks', label: '生成任务', icon: <PictureOutlined /> },
      { key: '/workbench-spus', label: 'SPU 聚合', icon: <ClusterOutlined /> },
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
      { key: '/workbench-batches', label: '批量任务与导出', icon: <ThunderboltOutlined /> },
      { key: '/publish', label: '发布上架', icon: <CloudUploadOutlined /> },
    ],
  },
  {
    label: '系统管理',
    adminOnly: true,
    items: [
      { key: '/dashboard', label: '指标仪表盘', icon: <DashboardOutlined /> },
      { key: '/spend', label: '付费调用花费', icon: <WalletOutlined /> },
      { key: '/model-templates', label: '模特模板', icon: <TeamOutlined /> },
      { key: '/providers', label: 'Provider', icon: <ApiOutlined /> },
      { key: '/prompts', label: '提示词', icon: <FileTextOutlined /> },
      { key: '/settings', label: '设置', icon: <SettingOutlined /> },
      { key: '/audit', label: '操作审计', icon: <HistoryOutlined /> },
      { key: '/system', label: '系统状态', icon: <HeartOutlined /> },
    ],
  },
]

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
 * ## 路由**不按角色裁剪**,只有菜单裁剪(A8)
 *
 * 两个理由:一,新人第一次进来还不是管理员,而他必须能顺着冷启动横幅的链接走到
 * /settings 填口令 —— 前端守卫会把他锁在门外;二,权限边界在后端 require_admin,
 * 再在这里加一份判断只会多出一处可能与后端不一致的地方。手敲 URL 打开管理页的
 * 后果是页面上一片 403,不是数据泄露。
 */
export const router = createBrowserRouter(
  createRoutesFromElements(
    <Route element={<AppLayout groups={NAV} />}>
        {/* A9:默认落点从指标仪表盘改成今日待办。/dashboard 保留给管理员看技术指标 */}
        <Route path="/" element={<Navigate to="/today" replace />} />
        <Route path="/today" element={<TodayPage />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/workbench" element={<WorkbenchListPage />} />
        <Route path="/workbench/:id" element={<WorkbenchProductPage />} />
        {/* 阶段 3。刻意不挂在 /workbench/* 下:那个前缀的下一段是商品 id,
            加一个 /workbench/batches 会和 :id 抢同一个位置 */}
        <Route path="/workbench-review" element={<WorkbenchReviewPage />} />
        <Route path="/workbench-batches" element={<WorkbenchBatchPage />} />
        <Route path="/workbench-exceptions" element={<WorkbenchExceptionsPage />} />
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
    </Route>,
  ),
)
