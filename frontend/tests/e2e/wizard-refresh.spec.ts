/**
 * AC-16 刷新恢复 —— **真浏览器的那一半**(任务 24 的第一批,2026-08-09 评审补)。
 *
 * ## 为什么这条必须是 Playwright,不能是 Vitest
 *
 * AC-16 的判据是「浏览器刷新或断线重连后,向导回到刷新前那一步与那个颜色」。
 * `tests/component/wizard.test.tsx` 里那条同名用例验的是**组件卸载重挂载**,
 * 而两者差着一整层:组件重挂载时 React 进程没死、内存里的 state 还在,
 * 一个把当前步存在 `useState` 里的实现照样能过。
 * 只有真的 `page.reload()`(整个 JS 上下文重建)才能证明"当前步住在 URL 里"。
 *
 * `REVIEW-STAGE6-CONCLUSION.md` §七.1 把「浏览器未实测」列为阶段 6 欠着的
 * 第一件事,而 `STATUS.md`「已知限制」的「向导浏览器未实测」写得更直接:
 * 「**没有人在浏览器里从头走过一遍**」。这份文件还上其中一条。
 *
 * ## 仍然拦掉 /api
 *
 * 与 `smoke.spec.ts` 同一条理由(见 `playwright.config.ts` 文件头):
 * 连后端等于把「刷新恢复对不对」和「数据库在不在」绑成同一个红灯,
 * 而两者的修法完全不同。这里桩出一份形状完整的 `/flow` 响应,
 * **刻意让后端算出来的 `current_step` 与 URL 上的不同** ——
 * 这样"URL 赢了"才是一个真的断言,而不是两边恰好一样。
 */
import { expect, test } from '@playwright/test'

const PRODUCT_ID = 'p-e2e'

/** 后端算出来的当前步。URL 不带 `?step=` 时向导该落在它上面。 */
const SERVER_CURRENT_STEP = 'ATTRIBUTE'

const STEPS = [
  ['SETUP', '建档', 'DONE', true],
  ['MATERIAL', '素材', 'DONE', true],
  ['ATTRIBUTE', '属性', 'NEEDS_CONFIRM', true],
  ['PLAN', '方案', 'BLOCKED', false],
  ['IMAGE_SET', '图片集', 'BLOCKED', false],
  ['COPY', '文案', 'BLOCKED', false],
  ['DRAFT', '草稿', 'BLOCKED', false],
] as const

function flowBody() {
  return {
    product: {
      id: PRODUCT_ID,
      spu: 'SW-E2E',
      spu_id: 'spu-e2e',
      sku: 'SW-E2E-M',
      name: 'e2e 泳衣',
      audience: null,
      audience_label: '待确认',
      review_focus: [],
      category: null,
      size: 'M',
      status: 'ACTIVE',
      updated_at: null,
    },
    thumbnail_url: null,
    flow: {
      completion: 45,
      blocking_count: 0,
      pending_count: 1,
      reminder_count: 0,
      current_step: SERVER_CURRENT_STEP,
      current_step_label: '属性',
      next_action: {
        code: 'CONFIRM_ATTRIBUTES',
        label: '确认属性',
        step: 'ATTRIBUTE',
        reason: '',
      },
      steps: [],
    },
    colors: {
      active_count: 2,
      blocking_codes: [],
      blocking_summary: '',
      worst_by_step: {},
      colors: [
        {
          variant_id: 'variant-red',
          variant_code: 'RED',
          is_active: true,
          current_step: 'ATTRIBUTE',
          is_blocking: false,
          steps: [],
        },
        {
          variant_id: 'variant-blue',
          variant_code: 'BLU',
          is_active: true,
          current_step: 'ATTRIBUTE',
          is_blocking: false,
          steps: [],
        },
      ],
    },
    wizard: {
      current_step: SERVER_CURRENT_STEP,
      focus_color: 'RED',
      allowed_actions: [],
      blocking: [],
      steps: STEPS.map(([step, label, state, open]) => ({
        step,
        label,
        intro: `${label}这一步要做的事`,
        state,
        is_open: open,
        gate_reason: open ? '' : '等上一步',
        is_current: step === SERVER_CURRENT_STEP,
        actions: [],
        pending_colors: [],
        has_color_axis: step === 'IMAGE_SET',
      })),
      cost: null,
    },
    color_selection: {
      variant_id: 'variant-red',
      variant_code: 'RED',
      product_id: PRODUCT_ID,
    },
    image_set: null,
    copy: null,
    draft: null,
  }
}

test.beforeEach(async ({ page }) => {
  await page.route('**/api/**', async (route) => {
    const url = route.request().url()

    // **写请求一律炸掉。** AC-16 的后半句是「不因刷新重复提交」——
    // 挂载时发一个 POST 的实现会在这里当场变红,而不是安静地多花一次钱。
    if (route.request().method() !== 'GET') {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ error: { message: '刷新不该触发写请求' } }),
      })
      return
    }

    if (url.includes('/api/auth/whoami') || url.includes('/api/auth/me')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ name: 'e2e', is_admin: false }),
      })
      return
    }
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
    if (url.includes(`/products/${PRODUCT_ID}/flow`)) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(flowBody()),
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

test('刷新之后仍停在 URL 上那一步,而不是回到后端算出的当前步', async ({
  page,
}) => {
  // 故意选一个**不等于** `SERVER_CURRENT_STEP` 的步:两者一样的话,
  // 一个把当前步丢掉、每次都用后端值的实现也能过这条用例
  await page.goto(`/wizard/${PRODUCT_ID}?step=DRAFT`)
  await expect(page).toHaveURL(/step=DRAFT/)

  await page.reload()

  // 整个 JS 上下文重建之后,URL 与界面都该还在 DRAFT
  await expect(page).toHaveURL(/step=DRAFT/)
  await expect(page.locator('body')).toContainText('草稿')
})

test('刷新之后仍停在 URL 上那个颜色', async ({ page }) => {
  await page.goto(`/wizard/${PRODUCT_ID}?step=IMAGE_SET&color=BLU`)
  await expect(page).toHaveURL(/color=BLU/)

  await page.reload()

  await expect(page).toHaveURL(/color=BLU/)
  await expect(page).toHaveURL(/step=IMAGE_SET/)
})

test('不带 step 时落在后端算出的当前步,不是第一步', async ({ page }) => {
  /**
   * 落第一步的话,一件做到第五步的商品每次打开都从建档看起,
   * 运营要点四次才回到他昨天的位置(`DECISIONS.md` §3.59 四)。
   */
  await page.goto(`/wizard/${PRODUCT_ID}`)
  await expect(page.locator('body')).toContainText('属性')
})
