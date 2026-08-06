/**
 * Vitest 接线（方案 v4.1 Phase 0 / 任务 2）。
 *
 * ## 为什么单独一个配置文件而不是塞进 vite.config.ts
 *
 * `vite.config.ts` 是**生产构建**的配置。把测试环境、setup 文件、
 * environmentMatchGlobs 一起写进去，等于让每一次 `npm run build` 都去解析
 * 一堆只有测试才用得上的东西；更要紧的是，改测试配置时会碰到构建配置 ——
 * 而构建配置出错的表现是产物不对，不是测试红。两件事的失败代价不一样，
 * 不放在同一个文件里。
 *
 * 共用的部分（`@` 别名、react 插件）在这里显式重复一遍，只有 3 行。
 * 用 `mergeConfig` 把两者串起来看似更 DRY，但它会让「测试为什么会碰到
 * dev server 代理配置」这种问题变得难查。
 *
 * ## 为什么按目录分环境，而不是全局 jsdom
 *
 *     tests/unit/*.test.ts    node —— 纯函数与 api client，不需要 DOM
 *     tests/component/*.tsx   jsdom —— React 组件，需要 DOM
 *
 * 已存在的 29 条用例（`client.test.ts` 17 条 + `workbench.test.ts` 12 条）
 * 是刻意写成不依赖 DOM 的，`client.test.ts` 文件头有整段论证。
 * 全局开 jsdom 会让这 29 条每次多背一个 DOM 实现的启动开销，
 * 而且会掩盖「这批用例不需要 DOM」这个已经被论证过的事实。
 *
 * ## `passWithNoTests: false` 是刻意的
 *
 * 这个仓库刚刚才从「29 条用例写好了但一次也没跑过」的状态里爬出来。
 * 如果哪天 include 规则被改错、一条都没匹配上，Vitest 默认会**成功退出** ——
 * 那正好是回到原点，而且这一次 CI 还会是绿的。
 */
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'node:path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  test: {
    globals: false,
    environment: 'node',
    environmentMatchGlobs: [['tests/component/**', 'jsdom']],
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    // Playwright 用例在 tests/e2e 下，由 playwright.config.ts 跑。
    // 不排除的话 Vitest 会把它们当成自己的用例收进来，然后在
    // `import { test } from '@playwright/test'` 上炸掉
    exclude: ['node_modules/**', 'dist/**', 'tests/e2e/**'],
    passWithNoTests: false,
    reporters: ['default'],
  },
})
