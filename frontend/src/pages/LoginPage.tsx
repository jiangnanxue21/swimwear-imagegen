/**
 * 浏览器登录页(a46-phase2)。
 *
 * ## 它接的是 phase1 已经做完的那一半
 *
 * 后端在 phase1 就有了 `/auth/login` / `/auth/logout` / Session 中间件,
 * 而前端整个仍然跑在 localStorage 口令上 —— 也就是说那三个端点**没有任何
 * 界面调用它们**。这一页是那条链路的入口。
 *
 * ## 三件刻意不做的事
 *
 *     记住我 / 自动登录    Cookie 的存活由后端 `AUTH_SESSION_MAX_AGE_SECONDS`
 *                          决定(而且那是滑动过期,见 DECISIONS §3.66),
 *                          前端再存一份"上次登录的用户名"只会多一处过期真相
 *     注册 / 改密          没有用户表。要按人追溯的下一步是"users 表 + 每人
 *                          一个账号",不是在这一页加一个改密表单
 *     前端校验密码强度      密码是运维在 `.env` 里配的,这一页只是输入框。
 *                          在这里拦"太短"只会拦住一个已经配好的部署
 *
 * ## 为什么它不在 `AppLayout` 里
 *
 * 侧栏上的每一项都要求已登录。把登录页挂在布局路由下的表现是:未登录的人
 * 看到一个完整的侧栏,点哪一项都被弹回来 —— 而他会以为是自己点错了。
 * 所以这一页是 `AppLayout` 的**兄弟**,不是它的子路由(见 `App.tsx`)。
 */
import { useEffect, useState, type ReactNode } from 'react'
import { Alert, Button, Card, Form, Input, Skeleton, Space, Typography } from 'antd'
import { LockOutlined, UserOutlined } from '@ant-design/icons'
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { authApi, type LoginRequest } from '../api/auth'
import { clearSessionExpired, describeError } from '../api/client'
import { useIdentity } from '../hooks/useIdentity'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { brandVars, fontScale, space } from '../theme'

/** 登录后的落点。`?next=` 拿不到或不可信时用它 */
const DEFAULT_LANDING = '/today'

/**
 * `?next=` 只接受**本站的绝对路径**。
 *
 * 不校验的话这是一个开放重定向:`/login?next=//evil.example` 里那个
 * 协议相对 URL 会被浏览器当成外站。react-router 的 `navigate()` 本身不出站,
 * 但这个值将来很容易被谁塞进 `href` 或 `location.assign` —— 而那时没有人
 * 会回来想起它的来源是地址栏。在唯一的入口处收干净,比在每个出口处防要可靠。
 *
 * 判据是白名单式的:必须以单个 `/` 开头。`//host`、`https://host`、
 * `javascript:` 全部落在外面。
 */
export function safeNext(raw: string | null): string {
  if (!raw) return DEFAULT_LANDING
  if (!raw.startsWith('/')) return DEFAULT_LANDING
  if (raw.startsWith('//')) return DEFAULT_LANDING
  // 登录页自己不能是落点,否则登录成功后停在原地,看起来像"没反应"
  if (raw === '/login' || raw.startsWith('/login?')) return DEFAULT_LANDING
  return raw
}

export default function LoginPage() {
  useDocumentTitle('登录')
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const identity = useIdentity()
  const [submitting, setSubmitting] = useState(false)
  const [failure, setFailure] = useState('')

  const next = safeNext(params.get('next'))

  // 已经登录的人不该停在登录页。**用 effect + navigate 而不是渲染期的
  // `<Navigate>`**:这一支要处理的是"在这一页登录成功"那一刻,那时组件
  // 已经挂着,渲染期跳转会和 antd Form 的收尾撞在一起
  useEffect(() => {
    if (identity.who) navigate(next, { replace: true })
  }, [identity.who, navigate, next])

  /*
   * 还没问出这个部署认哪种凭据之前,**什么都不画**。
   *
   * 先画表单再改主意的代价不是"闪一下":口令模式下人会看到一个登录框、
   * 开始输入,然后整块被换成"这个部署没有开启浏览器登录" —— 他输的东西
   * 没了,而且他有理由以为是自己弄错了什么。已登录的人刷新页面时同理:
   * 表单闪一下再跳走,看起来像被登出了一次。
   *
   * `/health` 是个不碰数据库的探针,这段等待通常是几十毫秒。
   */
  if (identity.loading) {
    return (
      <Shell>
        <Card>
          <Skeleton active paragraph={{ rows: 3 }} />
        </Card>
      </Shell>
    )
  }

  /*
   * 这个部署根本不走浏览器登录。**不是空白页,也不是一句"登录失败"** ——
   * 那两种都会让人一直试密码。直说没有这条路,并指向真正该去的地方。
   *
   * ## 这一支原来只有一句话,而它服务着两种完全不同的部署(2026-08-11 评审)
   *
   * 那句话是「到<系统设置>页填一次口令即可」,而它同时被两种情况读到:
   *
   *     免登录模式(dev)   本机三种凭据全空。**根本不需要登录**,任何请求都通;
   *                        人会走到这一页只是因为点了侧栏的"退出登录"
   *     口令模式(token)   只配了 Header 口令。浏览器**进不来**,
   *                        而且设置页里那个口令输入框随 localStorage
   *                        口令链一起删掉了 —— 那句指引指向一个不存在的输入框
   *
   * 现在按 `auth_mode` 分开说。两句话指向的动作完全相反:前者"直接用",
   * 后者"改服务器配置"。
   */
  if (!identity.backendDown && !identity.sessionAuth) {
    return (
      <Shell>
        {identity.tokenOnly ? (
          <Alert
            type="warning"
            showIcon
            message="这个部署的浏览器登录没有开启,现在无法从界面登录"
            description={
              <Typography.Text style={{ fontSize: fontScale.body }}>
                后端只配置了 Header 口令(<code>ADMIN_TOKEN</code> /{' '}
                <code>OPERATOR_TOKENS</code>),而浏览器不使用它 —— 界面上没有、
                也不应该有填口令的地方。两条出路,都需要改服务器配置后重启后端:
                <ul style={{ margin: '8px 0 0', paddingLeft: 20 }}>
                  <li>
                    要用账号密码登录:配 <code>ADMIN_PASSWORD</code> /{' '}
                    <code>OPERATOR_PASSWORD</code> / <code>AUTH_SESSION_SECRET</code>
                  </li>
                  <li>
                    本机开发想免登录:清空 <code>ADMIN_TOKEN</code> 与{' '}
                    <code>OPERATOR_TOKENS</code>
                  </li>
                </ul>
              </Typography.Text>
            }
          />
        ) : (
          <Alert
            type="info"
            showIcon
            message="这台机器处于免登录的开发模式"
            description={
              <Typography.Text style={{ fontSize: fontScale.body }}>
                后端没有配置任何凭据,所以这里没有账号密码可填,也不需要登录 ——
                直接<Link to="/today">回到工作台</Link>即可。要真的验证
                admin / operator 的差异、退出登录与 403,由管理员在{' '}
                <code>.env</code> 里配 <code>ADMIN_PASSWORD</code> /{' '}
                <code>OPERATOR_PASSWORD</code> / <code>AUTH_SESSION_SECRET</code>{' '}
                后重启后端。
              </Typography.Text>
            }
          />
        )}
      </Shell>
    )
  }

  if (identity.backendDown) {
    return (
      <Shell>
        <Alert
          type="error"
          showIcon
          message="连不上后端服务"
          description={
            <Typography.Text style={{ fontSize: fontScale.body }}>
              这不是密码问题(登录接口本身还没被调用过)。请稍后刷新本页;
              若一直如此,请联系管理员。
            </Typography.Text>
          }
        />
      </Shell>
    )
  }

  // 已登录时上面那个 effect 会把人带走,这一帧先不画表单,免得闪一下
  if (identity.who) return <Navigate to={next} replace />

  const submit = async (values: LoginRequest) => {
    setSubmitting(true)
    setFailure('')
    try {
      const who = await authApi.login({
        username: values.username.trim(),
        password: values.password,
      })
      // 登录成功 = 之前那次 401 已经过去了。不清的话横幅会在新会话上
      // 继续说"登录失效",而它说的是上一次的事
      clearSessionExpired()
      // 后端 `_identity_payload` 与 `/auth/whoami` 是同一个形状,所以可以直接
      // 写进缓存,省掉一次往返。两边形状分叉时这里会静默写进一个不同构的对象,
      // 所以后端那个函数上钉着一句注释,两边一起改
      queryClient.setQueryData(['auth-probe'], who)
      navigate(next, { replace: true })
    } catch (err) {
      // 后端对"用户名不存在 / 密码错误 / 该账号没配密码"回的是**完全一致**的
      // 401,原样透出。前端不要在这里替它区分,那等于把后端刻意合并的三种
      // 情况又拆开来告诉调用方
      setFailure(describeError(err, 'write').text)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Shell>
      <Card>
        <Space direction="vertical" size={space.lg} style={{ width: '100%' }}>
          <Typography.Title level={3} style={{ margin: 0 }}>
            登录
          </Typography.Title>

          {failure && <Alert type="error" showIcon message={failure} />}

          <Form<LoginRequest> layout="vertical" onFinish={submit} requiredMark={false}>
            <Form.Item
              name="username"
              label="用户名"
              rules={[{ required: true, message: '请输入用户名' }]}
            >
              <Input
                autoFocus
                autoComplete="username"
                prefix={<UserOutlined />}
                placeholder="admin 或 operator"
              />
            </Form.Item>
            <Form.Item
              name="password"
              label="密码"
              rules={[{ required: true, message: '请输入密码' }]}
            >
              {/* `Input.Password` 而不是 `type="password"` 的裸 input:
                  它带一个"看一眼"的开关,而复制密码时多带一个空格是这个仓库
                  已经踩过的事(后端两侧都 strip,但看得见仍然更省事) */}
              <Input.Password autoComplete="current-password" prefix={<LockOutlined />} />
            </Form.Item>
            <Button type="primary" htmlType="submit" block loading={submitting}>
              登录
            </Button>
          </Form>
        </Space>
      </Card>
    </Shell>
  )
}

/** 登录页的外框。侧栏和顶栏都不画 —— 那些入口现在一个都点不动 */
function Shell({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: space.xl,
      }}
    >
      <div style={{ width: '100%', maxWidth: 420 }}>
        <Typography.Title
          level={4}
          style={{ color: brandVars.ink, textAlign: 'center', marginBottom: space.lg }}
        >
          服装上架平台
        </Typography.Title>
        {children}
      </div>
    </div>
  )
}
