/**
 * 外框 + 侧栏导航。
 *
 * ## A8:菜单按工作阶段分组
 *
 * 上一版是 17 项平铺,没有分组 —— 新运营第一次打开侧栏,
 * 「Provider」「系统状态」「操作审计」和「商品工作台」并排站着,而他今天的活
 * 只涉及最后一个。现在按工作阶段分组,但不再按账号隐藏入口:
 *
 *     分组      三个组回答三个问题:今天要干什么、商品在哪儿、导出的东西在哪儿
 *     可发现性  运营看得见自己用得上的每一项;「系统管理」整组只对管理员显示
 *
 * 分组用 antd 的 `type: 'group'`(常显的组标题)而不是可折叠的子菜单:
 * 折叠起来的话,新人要先点开三个组才能看见全部入口,而 A9 的验收标准恰恰是
 * 「无需先理解菜单结构即可找到当前任务」。
 *
 * ## 菜单收敛不是权限边界
 *
 * 「系统管理」组对 operator 隐藏(PRD §30),但**路由仍然全部注册**:手输
 * `/settings` 打得开,页面上是一句 403。涉及 API Key、Provider 和系统配置的
 * 真实读写一律由后端 `require_admin` 校验 —— 隐藏菜单只是让运营不必看见
 * 一组他点了只会失败的入口,它挡不住任何人。
 */
import { Suspense, useState } from 'react'
import { App as AntApp, Button, Dropdown, Layout, Menu, Skeleton, Space, Tag, Tooltip } from 'antd'
// `WarningOutlined` 在这里死过一次(a48 删):a46-phase6 撤掉 `sharedActor`
// 那条告警横幅时留下了它的 import。离线门禁只解析语法,看不见死 import,
// 于是它跟着 a47 一起交付了出去 —— 而 `noUnusedLocals` 会让 typecheck 变红。
// 现在 `tools/syntax-check.mjs` 第二遍盯着这一类,不必再靠人自审时想起来。
import {
  BulbOutlined, BulbFilled, HistoryOutlined, LogoutOutlined, SettingOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { Link, Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { authApi } from '../api/auth'
import { describeError } from '../api/client'
import { brandVars, layoutMax } from '../theme'
import ColdStartBanner from './ColdStartBanner'
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
   * 只对管理员显示这一组。**这是可发现性,不是权限边界** ——
   * 路由仍然全部注册(见 `App.tsx`),operator 手输地址打得开,
   * 页面上是一句 403,而真正拦住他的是后端 `require_admin`。
   */
  adminOnly?: boolean
  items: NavItem[]
}

interface Props {
  groups: NavGroup[]
}

export default function AppLayout({ groups }: Props) {
  const location = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { message } = AntApp.useApp()
  const { who, isAdmin, loading: identityLoading, sessionAuth, needsLogin } = useIdentity()
  const [loggingOut, setLoggingOut] = useState(false)
  /*
   * 这里原来有一句 `const sharedActor = who?.name === 'operator'`,命中就在顶栏
   * 挂一个「共用口令,审计追不到人」的告警。**随浏览器登录一起删掉(PRD §3.3
   * 方案 A)**:新方案里 operator 账号的 `name` 就是 `operator`,那条告警会对
   * 每一个正常登录的运营常亮,而它说的事已经不成立 —— 现在是账号,不是共用口令。
   *
   * 具名审计的丧失是真实回归,但它不该由一条常亮的黄色三角来表达:
   * 那只会训练人忽略顶栏。结论记在 `docs/DECISIONS.md` §3.71。
   */
  const { mode, toggle } = useThemeMode()

  /*
   * 菜单按角色收敛(PRD §30)。
   *
   * 这一行在 a46 之前是 `const visible = groups` —— 不做任何过滤,理由写在
   * 当时的注释里:"新人第一次打开系统时既不是管理员、又必须进设置页填口令"。
   * **浏览器登录上线之后那个前提消失了**:密码在 `.env` 里,没有任何人需要
   * 先把自己变成管理员。所以本轮显式撤掉它,连同它的门禁一起
   * (`nav-and-url-filters.test.tsx` 现在反过来钉着 operator 看不到 `/settings`)。
   *
   * `isAdmin` 只认后端 `/auth/whoami` 的答案(`useIdentity` 里那条降级也一并
   * 删了)。判据在前端 = 可发现性;判据在后端 `require_admin` = 权限。
   */
  const visible = groups.filter((g) => !g.adminOnly || isAdmin)

  /**
   * 退出登录失败之后**留在原地**,不假装已经退出了(2026-08-11 评审)。
   *
   * ## 上一版为什么是错的
   *
   * 上一版无论成败都清缓存 + 跳 `/login`,理由写着"把人留在一个显示着登录态
   * 的界面上更糟"。那个理由建立在一个不成立的前提上:**Session 是 HttpOnly
   * Cookie,只有后端能删它。** `/auth/logout` 超时或失败时 Cookie 原样还在,
   * 于是:
   *
   *     跳到 /login   ->  登录页的 effect 问一次 whoami
   *                   ->  旧 Cookie 仍然有效,答"你是 operator"
   *                   ->  把人送回 /today
   *
   * 运营看到的是「我明明退出了,怎么又自动登录了」——比"退出按钮报错"糟得多:
   * 前者让他以为退不掉,后者告诉他这一次没退成。在共用电脑上离席的场景里,
   * 这个差别是"他以为自己退了"和"他知道自己没退"。
   *
   * ## 所以只有确认成功才跳
   *
   * 失败时留在当前页并弹一条可读的错误。缓存也不清 —— 清了会让界面显示成
   * 未登录,而 Cookie 还在,那正是同一个谎的另一半。
   */
  const logout = async () => {
    setLoggingOut(true)
    try {
      await authApi.logout()
    } catch (err) {
      message.error(`退出登录没有成功,你仍处于登录状态:${describeError(err, 'write').text}`)
      return
    } finally {
      setLoggingOut(false)
    }
    // 先清缓存再跳。反过来的话登录页会先读到一份"还登着"的 auth-probe,
    // 于是它的 effect 立刻把人送回来 —— 表现是点了退出没反应
    queryClient.removeQueries({ queryKey: ['auth-probe'] })
    navigate('/login', { replace: true })
  }

  /*
   * 会话模式下没有登录态:送到登录页,并把当前地址带上。
   *
   * **`?next=` 不是锦上添花。** 运营的常态是从聊天窗里点一条深链进来
   * (某件商品的向导、某个批次),会话恰好过期时如果只把他丢到首页,
   * 他手上那条链接就白点了 —— 而他多半不会想到回去再点一次。
   *
   * 判据取 `needsLogin` 而不是 `!who`:后者在探测还没有结论时也为真,
   * 于是已登录的人每次刷新都会先闪一下登录页。见 `useIdentity` 里的说明。
   */
  if (needsLogin) {
    const next = encodeURIComponent(`${location.pathname}${location.search}`)
    return <Navigate to={`/login?next=${next}`} replace />
  }

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
          服装上架平台
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

            **二、管理员需要一条常驻的进设置页的路。** 侧栏「系统管理」整组
            只对管理员显示(PRD §30),而 admin 有两个正常入口:侧栏那一组,
            以及下面这个下拉里的「系统设置」。两个都给,是因为运营的动线里
            侧栏是"找东西"、顶栏是"关于我自己" —— 改配置这件事两边都说得通。

            这一项对 operator **不出现**(`isAdmin &&`),和侧栏那一组同一个判据。
            它原来叫「更换口令」,那个名字随 localStorage 口令链一起退役:
            设置页那两把是机器凭据,不是任何人的登录密码。 */}
        <Dropdown
          trigger={['click']}
          menu={{
            items: [
              // 只有管理员看得到这一项(PRD §29)。名字不再叫"更换口令":
              // 设置页那两把是**机器凭据**(CLI、脚本、pytest),不是任何人的
              // 登录密码,叫那个名字会让人去改一把和自己完全无关的东西。
              // operator 这里没有入口,侧栏「系统管理」整组也不显示 ——
              // 但路由仍然注册,手输地址会看到一句 403,不是 404
              ...(isAdmin
                ? [
                    {
                      key: 'settings',
                      icon: <SettingOutlined />,
                      label: <Link to="/settings">系统设置</Link>,
                    },
                  ]
                : []),
              {
                key: 'audit',
                icon: <HistoryOutlined />,
                label: (
                  <Link to={who?.name ? `/audit?actor=${encodeURIComponent(who.name)}` : '/audit'}>
                    我的操作记录
                  </Link>
                ),
              },
              // 非会话模式下没有"退出"这个动作可做。给一个点了什么都不会
              // 发生的菜单项,比不给更糟。
              //
              // 这里原来写的是"凭据在 localStorage 里,清掉它是设置页的事" ——
              // **那句话已经不成立**:localStorage 口令链在 PRD §26/§27 里整个
              // 退役了。今天走到这一支的有两种部署,两种都确实没有可退的东西:
              // 免登录模式(没登录过)与只配 Header 口令(浏览器根本没进来,
              // 那一档的解释由 `ColdStartBanner` 与 `/login` 负责说)
              ...(sessionAuth
                ? [
                    { key: 'logout-divider', type: 'divider' as const },
                    {
                      key: 'logout',
                      icon: <LogoutOutlined />,
                      danger: true,
                      disabled: loggingOut,
                      label: '退出登录',
                      onClick: () => {
                        void logout()
                      },
                    },
                  ]
                : []),
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
            <SpendAlertBanner />
            {/* 子页面从这里出来(A11 之后 AppLayout 是布局路由,不再收 children)。
                边界挂在这里而不是更外面:一页崩了侧栏还在,运营能自己走去别的页面,
                不必刷新整站。resetKey 给 pathname —— 否则崩过之后点菜单换页,
                看到的还是那张兜底页 */}
            <ErrorBoundary resetKey={location.pathname} scope="这一页">
              {/* 页面 chunk 的加载态(a51 路由级代码分割)。
                  **在 ErrorBoundary 里面**:chunk 取不下来(发版后旧页面拿着
                  失效的文件名、或者网络断了)会抛,而那一抛必须落进上面那张
                  兜底页,不能穿到最外层把整个界面顶掉。
                  fallback 用 Skeleton 而不是转圈:它占的位置和真实内容接近,
                  切页时不会先塌一下再撑开 */}
              <Suspense fallback={<Skeleton active paragraph={{ rows: 8 }} />}>
                <Outlet />
              </Suspense>
            </ErrorBoundary>
          </div>
        </Content>
      </Layout>
    </Layout>
  )
}
