import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from 'react-router-dom'
import { router } from './App'
import { isAuthError } from './api/client'
import { applyBrandCssVars, assertSameKeys } from './theme'
import { ThemeModeProvider } from './hooks/useThemeMode'
import ErrorBoundary from './components/ErrorBoundary'
import ThemedApp from './components/ThemedApp'
import './styles/global.css'

// 令牌 -> CSS 变量。必须在首帧之前跑,否则 global.css 里的 var() 会先用 fallback
// 再跳一次。放在这里而不是模块顶层的副作用里,是为了让顺序看得见。
//
// 这里读的是首帧的模式:localStorage 里存过就用它,否则跟系统。之后由
// ThemeModeProvider 接管。**这一步不能省成「等 Provider 的 effect 来跑」** ——
// effect 在首帧之后才执行,中间那一帧会是亮的,暗色下就是一次白闪。
applyBrandCssVars(initialMode())

function initialMode(): 'light' | 'dark' {
  try {
    const saved = window.localStorage.getItem('imagegen.theme-mode')
    if (saved === 'light' || saved === 'dark') return saved
  } catch {
    /* 隐私模式:当作没设置过 */
  }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

// 两份调色板的键必须一一对应。只在开发环境查:漏键是编码错误,
// 该在写的时候当场炸,而不是让线上多一次启动期抛错的可能
if (import.meta.env.DEV) assertSameKeys()

/**
 * 评审 P1-5:401 / 403 不重试。
 *
 * 原来是无条件 `retry: 1`。于是口令没填或填错时,每个页面的每个查询都打两次 ——
 * 而这恰好是请求最密集的那一刻:冷启动横幅要亮起来的那一帧,页面上十几个
 * query 同时在跑,全都失败两次。
 *
 * 判断本来就已经有了:`describeError` 对 401/403 给的是 `retriable: false`。
 * 缺的只是把它接到 react-query 上 —— 全站唯一一份失败判定既然已经算出
 * 「重试没有意义」,就不该有第二处地方自己再决定一遍。
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failureCount, error) => !isAuthError(error) && failureCount < 1,
      refetchOnWindowFocus: false,
      staleTime: 10_000,
    },
  },
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ThemeModeProvider>
      <ThemedApp>
        {/* 最外层兜底:连 AppLayout 自己都渲染不出来的时候(比如 useIdentity 抛错)
            至少还有一张能读的错误页,而不是白屏。页面级的那层在 AppLayout 里,
            它挂在 Outlet 外面,能保住侧栏 */}
        <ErrorBoundary scope="界面">
          <QueryClientProvider client={queryClient}>
            {/* 数据路由(A11:useBlocker 只在数据路由下可用)。
                三个 Provider 仍在它外面 —— AppLayout 里的身份查询要用 react-query,
                而 antd 的 App 提供全局 message/notification/Modal 上下文 */}
            <RouterProvider router={router} />
          </QueryClientProvider>
        </ErrorBoundary>
      </ThemedApp>
    </ThemeModeProvider>
  </React.StrictMode>,
)
