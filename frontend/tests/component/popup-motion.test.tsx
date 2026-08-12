import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { theme as antdTheme } from 'antd'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ThemedApp from '../../src/components/ThemedApp'

function MotionProbe() {
  const { token } = antdTheme.useToken()
  return <output aria-label="motion-enabled">{String(token.motion)}</output>
}

function reducedMotionMedia(query: string): MediaQueryList {
  return {
    matches: query === '(prefers-reduced-motion: reduce)',
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }
}

afterEach(() => vi.unstubAllGlobals())

describe('antd 浮层与减少动态效果', () => {
  it('用 antd motion 令牌关闭动画,不抢跑 rc-trigger 的定位阶段', () => {
    vi.stubGlobal('matchMedia', reducedMotionMedia)

    render(
      <ThemedApp>
        <MotionProbe />
      </ThemedApp>,
    )

    expect(screen.getByLabelText('motion-enabled')).toHaveTextContent('false')
  })

  it('不再用全局近零时长覆盖所有元素', () => {
    const css = readFileSync(resolve('src/styles/global.css'), 'utf8')

    expect(css).not.toMatch(
      /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*?\*\s*,\s*\*::before/,
    )
  })
})
