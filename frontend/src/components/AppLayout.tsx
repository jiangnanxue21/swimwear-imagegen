/**
 * 外框 + 侧栏导航。
 *
 * ## A8:菜单按角色收敛
 *
 * 上一版是 17 项平铺,没有分组也没有角色区分 —— 新运营第一次打开侧栏,
 * 「Provider」「系统状态」「操作审计」和「商品工作台」并排站着,而他今天的活
 * 只涉及最后一个。收敛做两件事:
 *
 *     分组      三个组回答三个问题:今天要干什么、商品在哪儿、导出的东西在哪儿
 *     按角色    「系统管理」整组只对管理员显示
 *
 * 分组用 antd 的 `type: 'group'`(常显的组标题)而不是可折叠的子菜单:
 * 折叠起来的话,新人要先点开三个组才能看见全部入口,而 A9 的验收标准恰恰是
 * 「无需先理解菜单结构即可找到当前任务」。
 *
 * ## 这里收敛的是显示,不是权限
 *
 * 角色来自后端 `/auth/whoami`(降级规则见 `useIdentity`)。**路由本身对所有人
 * 保持注册** —— 见 App.tsx 里那段注释:新人还不是管理员的时候,得能顺着冷启动
 * 横幅的链接走进设置页填口令。真正的权限边界在后端 `require_admin`,
 * 藏起来的菜单项即使被手敲 URL 打开,每个请求也还是会被挡回来。
 */
import { Button, Dropdown, Layout, Menu, Space, Tag, Tooltip } from 'antd'
import {
  BulbOutlined, BulbFilled, HistoryOutlined, SettingOutlined,
  UserOutlined, WarningOutlined,
} from '@ant-design/icons'
import { Link, Outlet, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { brandVars, fontScale, layoutMax } from '../theme'
import ColdStartBanner from './ColdStartBanner'
import EnvironmentBanner from './EnvironmentBanner'
import SpendAlertBanner from './SpendAlertBanner'
import ErrorBoundary from './ErrorBoundary'
import { useIdentity } from '../hooks/useIdentity'
import { useThemeMode } from '../hooks/useThemeMode'

const { Header, Sider, Content } = Layout

export interface NavItem {
  key: string
  label: string
  icon?: ReactNode
}

export interface NavGroup {
  /** 组标题。不参与路由,只是侧栏上的一行小字 */
  label: string
  /**
   * 整组只对管理员显示。粒度刻意在组上而不在项上:一个组里混着两种可见性,
   * 下次加项的人得先读注释才知道该跟哪边。
   */
  adminOnly?: boolean
  items: NavItem[]
}

interface Props {
  groups: NavGroup[]
}

export default function AppLayout({ groups }: Props) {
  const location = useLocation()
  const { isAdmin, who, loading: identityLoading } = useIdentity()
  /** 后端把所有人都叫 operator = 配的是共用口令,审计追不到人 */
  const sharedToken = who?.name === 'operator'
  const { mode, toggle } = useThemeMode()

  const visible = groups.filter((g) => !g.adminOnly || isAdmin)

  // 选中项按"最长前缀"匹配,和上一版一致:`/workbench/:id` 要点亮
  // 「商品工作台」,而 `/workbench-review` 不能被 `/workbench` 抢走 ——
  // 所以前缀比较带上了分隔符。
  const selected = visible
    .flatMap((g) => g.items.map((i) => i.key))
    .filter((k) => location.pathname === k || location.pathname.startsWith(k + '/'))
    .sort((a, b) => b.length - a.length)
    .slice(0, 1)

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          paddingInline: 20,
        }}
      >
        {/* 顶栏两个模式下都是深色,所以这里用的是 onHeader 而不是 surface ——
            surface 在暗色下会翻成深色,那会让标题变成深底上的深字 */}
        <span style={{ color: brandVars.onHeader, fontWeight: 600, letterSpacing: '0.02em' }}>
          商品展示图生产台
        </span>
        <span style={{ color: brandVars.sand, fontSize: fontScale.meta, letterSpacing: '0.08em' }}>
          泳装 / 服装
        </span>
        <span style={{ flex: 1 }} />

        {/* 走查 P1-1 / P1-2:身份区。修两件事 ——
   
            **一、界面上无从确认「我是谁」。** 这套系统的核心纪律是「批准是人对
            内容负责的动作」,还专门做了审计页可按操作人筛。但走查前顶栏只有标题和
            主题开关,运营无从知道自己这把口令在审计里叫什么。而后端同时支持
            共用口令(所有人都记为 operator)和具名口令
            (`OPERATOR_TOKENS=姓名:口令`)两种模式 —— 前一种下审计根本答不出
            「这件是谁批的」,而那正是驳回复盘时第一个要问的问题。所以共用模式
            要**显式说出来**,不能让运营以为自己被追踪到了。

            **二、非管理员填完口令后,设置页从此找不到。** 侧栏「系统管理」整组
            对非管理员隐藏,而 ColdStartBanner 是他通往 /settings 的唯一入口 ——
            可那条横幅的显示条件是「没口令 / 口令被拒」,填对之后它就消失了。
            于是「我想换台电脑」「口令复制错了想重填」这类主动场景,
            非管理员只能靠背 URL。下面这个「更换口令」就是那条常驻的路。 */}
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              {
                key: 'settings',
                icon: <SettingOutlined />,
                label: <Link to="/settings">更换口令</Link>,
              },
              {
                key: 'audit',
                icon: <HistoryOutlined />,
                label: (
                  <Link to={who?.name ? `/audit?actor=${encodeURIComponent(who.name)}` : '/audit'}>
                    我的操作记录
                  </Link>
                ),
              },
            ],
          }}
        >
          <Button type="text" style={{ color: brandVars.onHeader }}>
            <Space size={6}>
              <UserOutlined />
              <span>{who?.name ?? (identityLoading ? '…' : '未登录')}</span>
              {who?.is_admin && (
                <Tag color={brandVars.sand} style={{ marginInlineEnd: 0 }}>
                  管理员
                </Tag>
              )}
              {sharedToken && (
                <Tooltip
                  title="当前用的是共用口令,审计日志里所有人都记为 operator,追不到具体是谁。
                         需要按人追溯,请让管理员把后端改成「姓名:口令」的具名形式"
                >
                  <WarningOutlined style={{ color: brandVars.sand }} />
                </Tooltip>
              )}
            </Space>
          </Button>
        </Dropdown>

        {/* 放顶栏而不是设置页:它是一个每天可能按好几次的开关(影棚白天亮、
            晚上暗),埋进设置页等于让运营为了换个亮度走三跳。
            设置页那种地方留给「配一次就不再动」的东西 */}
        <Tooltip title={mode === 'dark' ? '切换到亮色' : '切换到暗色'}>
          <Button
            type="text"
            aria-label={mode === 'dark' ? '切换到亮色模式' : '切换到暗色模式'}
            icon={mode === 'dark' ? <BulbFilled /> : <BulbOutlined />}
            onClick={toggle}
            style={{ color: brandVars.onHeader }}
          />
        </Tooltip>
      </Header>
      <Layout>
        <Sider width={190} theme="light" breakpoint="lg" collapsedWidth={0}>
          <Menu
            mode="inline"
            selectedKeys={selected}
            style={{ borderInlineEnd: 'none', paddingTop: 8 }}
            items={visible.map((group) => ({
              // 组标题不是可点的东西,key 只用来做 React 的身份标识
              key: `group:${group.label}`,
              type: 'group' as const,
              label: group.label,
              children: group.items.map((i) => ({
                key: i.key,
                icon: i.icon,
                label: <Link to={i.key}>{i.label}</Link>,
              })),
            }))}
          />
        </Sider>
        <Content style={{ padding: 20, overflow: 'auto' }}>
          {/* 走查 M-5:内容区留白上限。改动前只有系统状态页设了 maxWidth,
              于是 2560 的显示器上,商品详情的文案描述会拉成一行 2000px 的长句 ——
              中文一行超过 45 字左右就很难准确回行,眼睛会跳行。
              1680 是按「表格类页面装得下、阅读类不至于太长」取的。
              真正的纯阅读区块(文案全文)另有更窄的上限,见 CopyTab */}
          <div style={{ maxWidth: layoutMax, margin: '0 auto' }}>
            {/* 冷启动引导(P5)。放在 Content 里而不是 Header 上:它要跟着内容
                一起滚动,而且在设置页会自己隐藏 —— 见 ColdStartBanner 的注释 */}
            <ColdStartBanner />
            {/* 预算告警(A23)。排在冷启动之后:后端连不上时先说那件事,
                「预算快满了」在一个连不上的系统里不是当务之急 */}
            {/* 环境真实性排在预算前面(任务 6):「你看到的是假数据」比
                「这个月花超了」更早影响运营该不该相信这一屏 */}
            <EnvironmentBanner />
            <SpendAlertBanner />
            {/* 子页面从这里出来(A11 之后 AppLayout 是布局路由,不再收 children)。
                边界挂在这里而不是更外面:一页崩了侧栏还在,运营能自己走去别的页面,
                不必刷新整站。resetKey 给 pathname —— 否则崩过之后点菜单换页,
                看到的还是那张兜底页 */}
            <ErrorBoundary resetKey={location.pathname} scope="这一页">
              <Outlet />
            </ErrorBoundary>
          </div>
        </Content>
      </Layout>
    </Layout>
  )
}
