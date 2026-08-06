/**
 * 预算告警横幅(A23)。
 *
 * ## 为什么是全局横幅而不是只在花费页显示
 *
 * 「欠费」这件事的伤害发生在**运营正要跑一批**的那一刻,而那时他在工作台或
 * 批量任务页,不在花费页。一个只在花费页可见的告警,等于要求运营先想到去看它 ——
 * 而想得到就不会出事了。
 *
 * ## 为什么不做成 Modal
 *
 * 挡路的弹窗会被训练成"闭眼点掉"。预算超支不是不能继续工作,是**要知道**;
 * 横幅常驻、不遮挡、点得进去,比一个每次刷新都要关一遍的弹窗更可能被读进去。
 *
 * ## 三种不显示的情况
 *
 *     未配预算       没有判据,显示"未设预算"只会变成永久噪音。花费页会说这件事
 *     档位是 OK      没有需要打断的信息
 *     就在花费页     那一页自己有完整的预算卡,再顶一条横幅是重复
 */
import { Alert } from 'antd'
import { Link, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { usageApi } from '../api/usage'
import { space } from '../theme'

export default function SpendAlertBanner() {
  const location = useLocation()
  const { data } = useQuery({
    queryKey: ['usage', 'spend', 'banner'],
    queryFn: usageApi.spend,
    // 五分钟一次。花费是累积量,不是实时量 —— 更频繁只会增加后端聚合查询的压力,
    // 而"晚五分钟知道"对一个按月计的预算没有任何区别
    refetchInterval: 5 * 60_000,
    // 这条横幅坏掉不该在控制台刷屏,也不该弹错误提示:
    // 它是附加信息,拿不到就不显示
    retry: false,
  })

  if (!data || !data.budget.should_alert) return null
  if (location.pathname === '/spend') return null

  const exceeded = data.budget.level === 'EXCEEDED'
  const day = data.budget.projected_exhaustion_day

  return (
    <Alert
      type={exceeded ? 'error' : 'warning'}
      showIcon
      style={{ marginBottom: space.lg }}
      message={
        exceeded
          ? `本月付费调用已超出预算:${data.spent_display} / ${data.budget.budget_display}`
          : `本月付费调用已用 ${Math.round(data.budget.used_ratio * 100)}%:` +
            `${data.spent_display} / ${data.budget.budget_display}`
      }
      description={
        <>
          {day !== null && !exceeded && (
            <>按目前速度,预算约在本月 {day} 号用完。</>
          )}
          {exceeded && <>继续跑批量前请先确认厂商账户仍可用。</>}{' '}
          <Link to="/spend">查看花费明细</Link>
        </>
      }
    />
  )
}
