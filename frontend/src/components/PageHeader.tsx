/**
 * 页面标题(走查 M-3)。
 *
 * ## 要修的那件事:整站没有层级
 *
 * 走查前,`.section-label` 同时扮演两个角色:
 *
 *     页面标题   `<div className="section-label">运营工作台</div>`     (WorkbenchListPage 顶部)
 *     区块标题   `<div className="section-label">问题清单</div>`       (EvaluationDetail 内部)
 *
 * 同一个视觉重量。后果是 20 个页面打开长得一模一样,缺少「我现在在哪一页」
 * 的视觉锚点 —— 而运营一天要在这些页面之间跳几十次。
 *
 * 现在拆成两个东西:这个组件负责页面级,`.section-label` 退回它本来的角色。
 *
 * ## 顺带解决:页面级动作没有固定位置
 *
 * 走查发现「刷新」「新建」「返回」这类**页面级**按钮散落在各页不同位置 ——
 * `WorkbenchProductPage` 放在最顶上一排裸按钮,`WorkbenchListPage` 埋在筛选卡里,
 * `MediaLibraryPage` 又在别处。运营每换一页都要重新找。
 * `extra` 槽位把它们钉在同一个位置:标题行右端。
 *
 * ## 为什么标题用 `<h1>` 而不是 `<div>`
 *
 * 屏幕阅读器和浏览器的「跳到主要内容」都靠标题层级导航。走查时全站
 * `aria-*` 只有 1 处,标题也全是 `div` —— 键盘用户没有任何快速定位手段。
 * 一个 `h1` 不解决全部无障碍问题,但它是成本最低的那一步。
 */
import type { ReactNode } from 'react'
import { Space, Typography } from 'antd'
import { brandVars, fontScale, space } from '../theme'

interface Props {
  /** 页面名。同时会被调用方传给 `useDocumentTitle` —— 两处保持同一个词 */
  title: ReactNode
  /** 一句话说清这一页回答什么问题。不写也行,但列表类页面建议写 */
  subtitle?: ReactNode
  /** 页面级动作:刷新、新建、返回。**不放行级动作** */
  extra?: ReactNode
}

export default function PageHeader({ title, subtitle, extra }: Props) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-end',
        justifyContent: 'space-between',
        gap: space.lg,
        flexWrap: 'wrap',
        paddingBottom: space.sm,
        borderBottom: `1px solid ${brandVars.line}`,
      }}
    >
      <div style={{ minWidth: 0 }}>
        <h1
          style={{
            margin: 0,
            fontSize: fontScale.title,
            fontWeight: 600,
            lineHeight: 1.4,
            color: brandVars.ink,
          }}
        >
          {title}
        </h1>
        {subtitle && (
          <Typography.Text
            type="secondary"
            style={{ fontSize: fontScale.body, display: 'block', marginTop: 2 }}
          >
            {subtitle}
          </Typography.Text>
        )}
      </div>
      {extra && <Space wrap>{extra}</Space>}
    </div>
  )
}
