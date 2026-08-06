import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { applyBrandCssVars, type ThemeMode } from '../theme'

/**
 * 亮 / 暗模式。
 *
 * ## 三种取值,不是两种
 *
 * 存的是 `light` / `dark` / `system`,而界面上呈现的是前两者之一。多出来的
 * `system` 不是为了凑一个选项:运营的机器多半配了「日出转亮、日落转暗」,
 * 只存 light/dark 的话,他第一次点了暗色之后就再也跟不上系统了,而且他不会
 * 意识到是自己那一次点击造成的。
 *
 * ## 为什么不用 media query 直接写 CSS
 *
 * `@media (prefers-color-scheme: dark)` 只能表达 system,表达不了「我今天就要亮的」。
 * 而这一页有它自己的理由需要手动挡:同一批候选图在亮底和暗底上判出来的结果不完全
 * 一样(见 theme.ts 里 imageBg 那条注释),所以复核别人的判断时,能把模式对齐
 * 到对方那一档是有意义的。
 *
 * ## 切换为什么不需要重渲染
 *
 * 组件里的颜色是 `var(--brand-*)`(见 theme.ts 的 `brandVars`),换主题只是改
 * `:root` 上那二十几个变量,浏览器自己重算。这里 setState 只为了让 antd 的
 * ConfigProvider 换一套算法 —— antd 组件内部的颜色是算出来的,吃不了 var()。
 */
const STORAGE_KEY = 'imagegen.theme-mode'

export type ThemePreference = ThemeMode | 'system'

interface ThemeModeValue {
  /** 实际生效的模式,只会是 light 或 dark */
  mode: ThemeMode
  /** 用户的选择,可能是 system */
  preference: ThemePreference
  setPreference: (next: ThemePreference) => void
  /** 在亮 / 暗之间来回切(顶栏那个按钮用)。会把 preference 定死,不再跟随系统 */
  toggle: () => void
}

const ThemeModeContext = createContext<ThemeModeValue | null>(null)

function readPreference(): ThemePreference {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (raw === 'light' || raw === 'dark' || raw === 'system') return raw
  } catch {
    /* 隐私模式下读不到,当作没设置过 */
  }
  return 'system'
}

function systemMode(): ThemeMode {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export function ThemeModeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(readPreference)
  const [system, setSystem] = useState<ThemeMode>(systemMode)

  // 跟随系统时,系统换了要跟上。preference 是 light/dark 时这个监听仍然挂着但不生效 ——
  // 摘不摘它没有区别,而条件挂载 effect 会多一处出错的地方
  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
    if (!mq) return
    const onChange = (e: MediaQueryListEvent) => setSystem(e.matches ? 'dark' : 'light')
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  const mode: ThemeMode = preference === 'system' ? system : preference

  useEffect(() => {
    applyBrandCssVars(mode)
  }, [mode])

  const setPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next)
    try {
      window.localStorage.setItem(STORAGE_KEY, next)
    } catch {
      /* 存不下不影响本次会话 */
    }
  }, [])

  const value = useMemo<ThemeModeValue>(
    () => ({
      mode,
      preference,
      setPreference,
      toggle: () => setPreference(mode === 'dark' ? 'light' : 'dark'),
    }),
    [mode, preference, setPreference],
  )

  return <ThemeModeContext.Provider value={value}>{children}</ThemeModeContext.Provider>
}

export function useThemeMode(): ThemeModeValue {
  const value = useContext(ThemeModeContext)
  if (!value) {
    // 没包 Provider 时给一个能用的降级,而不是抛错:这个 hook 只影响外观,
    // 让整页崩掉的代价远高于少一个切换按钮
    return {
      mode: 'light',
      preference: 'system',
      setPreference: () => {},
      toggle: () => {},
    }
  }
  return value
}
