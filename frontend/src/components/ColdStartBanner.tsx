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
import { Alert, Typography } from 'antd'
import { useLocation } from 'react-router-dom'
import { useIdentity } from '../hooks/useIdentity'
import { fontScale } from '../theme'

export default function ColdStartBanner() {
  const location = useLocation()
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

  /*
   * 会话模式:这条横幅整个让位给登录页(a46-phase2)。
   *
   * 下面每一支说的都是**口令**的事 —— 去设置页填、去设置页改、别拿管理口令
   * 跑日常业务。会话模式下浏览器根本不持有口令,这三句话一句都不成立:
   * 运营照着去设置页填一把口令,填完仍然是未登录,而横幅还在。
   *
   * 未登录本身不需要横幅来说:`AppLayout` 已经把人送到 `/login` 了,
   * 而那一页比一条横幅说得清楚得多。这里返回 null 不是"没话说",
   * 是"这件事有别的地方在说,而且说得更好"。
   *
   * 位置在 `backendDown` **之后**:后端连不上时那句话两种模式都成立,
   * 而且那时登录页也进不去 —— 它是先要说的那一句。
   */
  /*
   * 到这里只剩一种情况:后端活着。**横幅不再承担"未登录"这件事** ——
   * `AppLayout` 已经把人送到 `/login`,那一页比一条横幅说得清楚得多。
   *
   * 这里原来还有三支:「还差一步:填入操作口令」「正在用管理口令做日常操作」
   * 「口令被后端拒绝」。三支都是 localStorage 口令链的产物,随那条链一起退役
   * (PRD §32)。留着的话,运营会照着去设置页填一把**根本不会被读**的口令 ——
   * 而那个输入框本轮也删掉了。
   */
  return null
}
