import { App as AntdApp, ConfigProvider, theme as antdTheme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import type { ReactNode } from 'react'

import { useThemeMode } from '../hooks/useThemeMode'
import { buildTheme } from '../theme'

/** 把当前模式接到 antd 上。antd 组件内部的色是算出来的,吃不了 CSS 变量。 */
export default function ThemedApp({ children }: { children: ReactNode }) {
  const { mode } = useThemeMode()
  return (
    <ConfigProvider
      locale={zhCN}
      theme={buildTheme(mode, {
        defaultAlgorithm: antdTheme.defaultAlgorithm,
        darkAlgorithm: antdTheme.darkAlgorithm,
      })}
    >
      <AntdApp>{children}</AntdApp>
    </ConfigProvider>
  )
}
