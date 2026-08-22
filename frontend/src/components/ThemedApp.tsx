import { App as AntdApp, ConfigProvider, Empty, theme as antdTheme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import { useEffect, useState, type ReactNode } from 'react'

import { useThemeMode } from '../hooks/useThemeMode'
import { buildTheme } from '../theme'

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false,
  )

  useEffect(() => {
    const query = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    if (!query) return
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches)
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])

  return reduced
}

/** 把当前模式接到 antd 上。antd 组件内部的色是算出来的,吃不了 CSS 变量。 */
export default function ThemedApp({ children }: { children: ReactNode }) {
  const { mode } = useThemeMode()
  const reducedMotion = useReducedMotion()
  const theme = buildTheme(mode, {
    defaultAlgorithm: antdTheme.defaultAlgorithm,
    darkAlgorithm: antdTheme.darkAlgorithm,
  })

  // 不用全局 `* { transition-duration: 0.01ms }`：rc-motion 的 prepare/active
  // 两阶段会被压进同一帧,rc-trigger 还没完成定位就收到 transitionend,最终把
  // Select / Dropdown / DatePicker 等浮层留在测量坐标 -1000vw/-1000vh。
  // antd 自己的 motion 令牌能在不破坏定位生命周期的前提下尊重系统的「减少动态效果」。
  theme.token = { ...theme.token, motion: !reducedMotion }

  return (
    <ConfigProvider
      locale={zhCN}
      renderEmpty={() => <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无数据" />}
      theme={theme}
    >
      <AntdApp>{children}</AntdApp>
    </ConfigProvider>
  )
}
