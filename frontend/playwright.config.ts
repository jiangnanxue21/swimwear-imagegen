/**
 * Playwright 接线骨架（方案 v4.1 Phase 0 / 任务 3）。
 *
 * ## 这一版**只是骨架**，边界写在这里免得被误读
 *
 * 方案 12.1 节把 Playwright 拆成两个任务：任务 3「接入骨架」在 P0，
 * 任务 24「补完整主流程」在 P5，依赖任务 20（发布前端页面）。
 * 顺序是这样定的，因为主流程用例要走的那几个页面**现在还不存在**
 * ——发布页要等 Phase 2 的三张表落地。
 *
 * 所以这个文件现在负责的只有一件事：**证明这条链路是通的**。
 * 装得上、起得来、点得动、失败会红。真正的业务断言在 P5 补。
 *
 * 一个只有骨架的 Playwright 和没有 Playwright 的区别在于：
 * 到了 P5 那天，要写的是用例，不是「先花两天把环境搭起来」。
 *
 * ## 为什么跑 `vite preview` 而不是 `vite dev`
 *
 * dev server 走的是 esbuild 即时转译，**它跑得通不代表 `npm run build`
 * 的产物跑得通**——这两者在这个仓库里已经分叉过一次（`tsc -b` 报错而
 * dev 一切正常）。E2E 要验的是运营真正会拿到的那份产物。
 *
 * 代价是每次跑 E2E 都要先 build 一次。这是对的代价。
 *
 * ## 为什么不连真实后端
 *
 * 骨架阶段连后端等于把「前端能不能起来」和「后端和数据库在不在」
 * 绑成一个红灯，而这两件事的修法完全不同。用例内部用 `page.route()`
 * 拦掉 `/api/**`，先把浏览器这一层单独钉死。
 *
 * P5 的主流程用例要反过来——那时候要验的正是前后端接在一起对不对，
 * 到时候在 CI 里起 compose，不在这里。
 */
import { defineConfig, devices } from '@playwright/test'

const PORT = Number(process.env.E2E_PORT ?? 4173)

export default defineConfig({
  testDir: './tests/e2e',
  // 骨架阶段用例极少，串行跑，输出好读
  fullyParallel: false,
  workers: 1,
  // `.only` 混进 CI 会让其余用例被静默跳过，且 CI 仍然是绿的
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  timeout: 30_000,
  expect: { timeout: 5_000 },

  use: {
    baseURL: process.env.E2E_BASE_URL ?? `http://127.0.0.1:${PORT}`,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],

  // 外部已经起好服务时（E2E_BASE_URL 指过去）就不要再起一个
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: `npm run build && npx vite preview --port ${PORT} --strictPort`,
        url: `http://127.0.0.1:${PORT}`,
        timeout: 180_000,
        reuseExistingServer: !process.env.CI,
      },
})
