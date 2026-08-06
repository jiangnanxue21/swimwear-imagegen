/**
 * 冷启动引导(OPS-REVIEW P5)。
 *
 * ## 要修的那次体验
 *
 * 新人第一次打开系统:侧栏点哪一页都是一片红,每条错误都写着"到「系统设置」页
 * 核对口令后重试"。信息其实是对的,但它出现在**每一页的每一个失败请求上**,
 * 而没有一处告诉他:系统是好的,你只差一个口令,填的地方在这儿。
 *
 * OPS-REVIEW 的原话是"别让新人先撞一次错"。所以这条横幅在所有页面之上、
 * 在任何请求失败之前就出现 —— 判断依据只是 localStorage 里有没有口令。
 *
 * ## 三种状态说三句不同的话
 *
 *     后端连不上    管理员看 docker compose 那一行,运营看"稍后重试 / 找管理员"
 *                   (A12:命令不进运营视野);这时填口令没有意义,别把人引去设置页
 *     口令未配置    到设置页填;附一句"日常操作口令"是哪一个,新人分不清两把口令
 *     口令被拒过    到设置页改;这条只能由拦截器告诉我们(见 client.ts)
 *
 * 优先级是上面的顺序:后端都没起来时,口令那两句都是噪音。
 *
 * ## 不做的两件事
 *
 * 不做"关闭"按钮:它不是通知,是阻断态的说明,关掉之后页面依然是空的。
 * 不在这里替用户存口令:设置页才是那件事的唯一入口(它还要处理管理口令),
 * 两个地方都能填,下次就会有人问"我明明填了为什么不生效"。
 */
import { useEffect, useState } from 'react'
import { Alert, Space, Typography } from 'antd'
import { Link, useLocation } from 'react-router-dom'
import {
  isAuthRejected,
  onAuthRejectedChange,
  readAdminToken,
  readOperatorToken,
} from '../api/client'
import { useIdentity } from '../hooks/useIdentity'
import { fontScale } from '../theme'

export default function ColdStartBanner() {
  const location = useLocation()
  const [rejected, setRejected] = useState(isAuthRejected())

  useEffect(() => onAuthRejectedChange(setRejected), [])

  // 带口令的探测。这是"口令填错了"唯一诚实的判据 —— 匿名探活成功只说明
  // 后端活着。后端不认时守卫回 401/403,响应拦截器会顺手点亮全局 rejected
  // 状态,所以这里不自己判断"口令好不好",只是把探测发出去。
  //
  // A8 把这两个请求挪进了 `useIdentity`:菜单也要问同一件事,而两处各写一份
  // `useQuery` 会让 `enabled` 条件分叉(理由见那个文件的注释)。
  const identity = useIdentity()

  // 已经在设置页了就不显示:那一页自己会说该填什么,顶上再顶一条重复的横幅
  // 只会把表单挤下去
  if (location.pathname.startsWith('/settings')) return null

  if (identity.backendDown) {
    // A12:同一件事对两种人说两句话。运营执行不了 `docker compose up -d`,
    // 把它摆在他面前只会让他以为是自己弄坏了什么;而第一次装系统的人
    // 恰恰需要那一行。`isAdmin` 在后端连不上时会退回"本浏览器填了管理口令没"
    // —— 对这一支来说那正好是对的判据:装系统的人手里有管理口令
    return (
      <Alert
        type="error"
        showIcon
        style={{ marginBottom: 12 }}
        message="连不上后端服务"
        description={
          <Typography.Text style={{ fontSize: fontScale.body }}>
            {identity.isAdmin ? (
              <>
                这不是口令问题(探活接口本身不需要口令),先确认后端已启动:
                <code>docker compose up -d</code>。启动后刷新本页。
              </>
            ) : (
              <>
                这不是你操作错了,也不是口令问题 —— 系统整体暂时联系不上。
                请稍后刷新本页;若一直如此,请联系管理员。
              </>
            )}
          </Typography.Text>
        }
      />
    )
  }

  if (identity.loading) return null

  // A5:"填了没有"只能由 localStorage 回答,"填对了没有"只能由后端回答。
  // 上一版把两件事合成了一句 `readOperatorToken() || readAdminToken()`,
  // 于是口令填错时横幅静默 —— 它以为"已配置"就等于"没问题"。
  const hasToken = Boolean(readOperatorToken() || readAdminToken())

  if (!hasToken) {
    return (
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="还差一步:填入操作口令"
        description={
          <Space direction="vertical" size={2}>
            <Typography.Text style={{ fontSize: fontScale.body }}>
              后端已就绪。读列表和做操作都需要「日常操作口令」(后端的
              OPERATOR_TOKENS),没有它每一页都会是空的。
            </Typography.Text>
            <Typography.Text style={{ fontSize: fontScale.body }}>
              到 <Link to="/settings">系统设置</Link> 页填一次即可,浏览器会记住它。
              改 API Key、切换生成模型用的是另一把「管理口令」,同一页里分开填。
            </Typography.Text>
          </Space>
        }
      />
    )
  }

  /*
   * C-04(部分):正在拿管理口令跑日常业务。
   *
   * 这个回落本身是刻意的(只配 admin 的部署照样能用,后端也认),但代价是
   * **每一次列表 GET 都在发送能改 API Key 的那把口令** —— 而运营完全看不到
   * 这件事正在发生,也就没有任何理由去配一把运营口令。
   *
   * 所以不删回落,只让它可见。真正消除它要账号体系(C-05')。
   *
   * 排在"口令被拒"之前:口令被拒是更急的问题,而这一条是长期建议。
   */
  if (identity.usingAdminFallback && !rejected && !identity.authFailed) {
    return (
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message="正在用管理口令做日常操作"
        description={
          <Typography.Text style={{ fontSize: fontScale.body }}>
            这台浏览器只填了「管理口令」,于是<b>每一个</b>请求(包括拉列表)都带着它,
            而那把口令能改 API Key、能切换生成模型、能烧掉额度。
            到 <Link to="/settings">系统设置</Link> 页补一把「日常操作口令」之后,
            管理口令就只会在改配置时才发出去。
          </Typography.Text>
        }
      />
    )
  }

  if (rejected || identity.authFailed) {
    return (
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message="口令被后端拒绝了"
        description={
          <Typography.Text style={{ fontSize: fontScale.body }}>
            后端在正常运行,但它不认这把口令(可能是换过、或复制时多了空格)。
            到 <Link to="/settings">系统设置</Link> 页重新填一次;下一个成功的请求
            会让这条提示自动消失。
          </Typography.Text>
        }
      />
    )
  }

  return null
}
