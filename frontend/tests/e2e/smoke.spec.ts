/**
 * Playwright 骨架用例（方案 v4.1 Phase 0 / 任务 3）。
 *
 * ## 这三条用例断言的是「基础设施」，不是「业务」
 *
 * 它们要回答的问题只有一个：**这条链路真的能跑吗？**
 *
 *     浏览器起得来 → 构建产物起得来 → 路由跑得动 → 断言失败会红
 *
 * 业务断言（图片生产 / Listing / 自动上架 / 驳回修复 / 小批量，见 6.5 节）
 * 是任务 24，在 P5。那时候要点的几个页面现在还不存在。
 *
 * ## 为什么第三条要故意验「失败会红」
 *
 * 这个仓库刚从「29 条用例写好了但一次也没执行过」里出来，
 * 教训是：**一个从未失败过的门禁，和一个不存在的门禁，可靠性是一样的。**
 *
 * `webServer` 配错、`baseURL` 指到一个 404、`page.route` 把所有请求都吞了 ——
 * 这几种情况下用例都可能「通过」，因为它们根本没跑到断言。
 * 所以留一条用例，专门确认在这套配置下断言确实会执行、失败确实会冒出来。
 *
 * ## 为什么拦掉 /api
 *
 * 见 `playwright.config.ts` 文件头。骨架阶段不连后端，
 * 免得把「前端起不来」和「后端没起」混成同一个红灯。
 */
import { expect, test } from '@playwright/test'

/** 让每个页面在没有后端的情况下也能渲染出外壳 */
test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', async (route) => {
    const url = route.request().url()
    // 身份接口给一个最小可用的回答，否则顶栏会一直停在 loading
    if (url.includes('/api/auth/whoami') || url.includes('/api/auth/me')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ name: 'e2e', is_admin: false }),
      })
      return
    }
    // 外壳自己会拉的两个接口:环境状态条和预算横幅。
    //
    // 它们不是列表,兜底那句 `{ items: [], total: 0 }` 对它们是**错的形状** ——
    // `EnvironmentBanner` 读 `data.facets.filter(...)`、`SpendAlertBanner` 读
    // `data.budget.should_alert`,拿到 undefined 当场抛,而这两个组件在
    // `AppLayout` 里、在页面级 ErrorBoundary **外面**,于是整页
    // 「Unexpected Application Error」——外壳根本没渲染出来,
    // 而这三条用例断言的正是外壳。
    //
    // 这两处给的是「一切正常、没什么要说的」那一档:两个横幅都自行返回 null,
    // 外壳干干净净地起来 —— 正是「不连后端也要能起来」想验的那个状态。
    if (url.includes('/api/environment')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          fidelity: 'REAL',
          trustworthy: true,
          facets: [],
          deployment: { batch_execution_mode: 'e2e', storage_backend: 'e2e' },
        }),
      })
      return
    }
    if (url.includes('/api/usage/spend')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          budget: { level: 'OK', should_alert: false, used_ratio: 0 },
        }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [], total: 0 }),
    })
  })
})

test('应用外壳能从构建产物里起来', async ({ page }) => {
  await page.goto('/')
  // 根路由会重定向到 /today
  await expect(page).toHaveURL(/\/today$/)
  await expect(page.getByText('商品展示图生产台')).toBeVisible()
})

test('路由跳转不会把页面打空', async ({ page }) => {
  await page.goto('/workbench')
  // 断言的是「外壳还在」——具体内容要等后端，那是 P5 的事
  await expect(page.getByText('商品展示图生产台')).toBeVisible()
  await expect(page).toHaveURL(/\/workbench$/)
})

test('断言失败真的会让这条用例红（门禁的门禁）', async ({ page }) => {
  await page.goto('/')
  // 这一条如果哪天「通过」了，说明断言压根没执行 —— 那才是要查的事
  await expect(
    page.getByText('这段文字在任何页面上都不该存在-e2e-canary'),
  ).toHaveCount(0)
  // 反向确认：上面那句 toHaveCount(0) 通过，不代表定位器在工作。
  // 再钉一个确实存在的东西，两条一起才说明定位器既能找到也能找不到
  await expect(page.getByText('商品展示图生产台')).toHaveCount(1)
})
