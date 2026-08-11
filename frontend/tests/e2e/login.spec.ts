/**
 * 浏览器登录 —— **真浏览器的那一半**(a46-phase2)。
 *
 * ## 为什么这几条不能只有 Vitest
 *
 * `tests/component/browser-login.test.tsx` 验的是组件行为:表单提交调了哪个
 * 函数、跳到哪条路由。它跑在 jsdom 上,而 jsdom **没有 Cookie 与 axios 凭据
 * 的真实交互** —— `withCredentials` 那一行在那边只能靠读 `defaults` 来断言,
 * 断言的是"配置写对了",不是"Cookie 真的跟着走了"。
 *
 * 这里能多验的是链路形状:
 *
 *     未登录访问深链   会不会带着 `?next=` 落到登录页
 *     登录成功         整个 JS 上下文里,应用会不会真的换页
 *     退出登录         会不会回到登录页,而且回不去原来那一页
 *
 * ## 仍然拦掉 /api
 *
 * 与 `smoke.spec.ts` / `wizard-refresh.spec.ts` 同一条理由:连后端等于把
 * "登录跳转对不对"和"数据库在不在"绑成同一个红灯。这里用一个模块级变量
 * 扮演"服务端到底认不认这个浏览器",`whoami` 按它回 200 或 401 ——
 * 这样"登录之后 whoami 就认人了"才是一次真的状态变化,而不是两条各自写死的桩。
 *
 * ## 这份文件的执行状态
 *
 * **写这一批时容器里没有 Playwright 浏览器,这几条一次都没有跑过。**
 * 这句话写在这里而不是只写在交接文档里:下一个打开这个文件的人,
 * 需要在读第一条用例之前就知道它的成色。复跑:
 *
 *     cd frontend && npx playwright install chromium && npm run e2e
 */
import { expect, test } from '@playwright/test'

/** 服务端此刻认不认这个浏览器。`/auth/login` 把它翻真,`/auth/logout` 翻假 */
let authenticated = false

test.beforeEach(async ({ page }) => {
  authenticated = false

  await page.route('**/api/**', async (route) => {
    const url = route.request().url()

    // 匿名探针。`auth_mode` 是前端判断"该跳登录页还是设置页"的唯一来源
    if (url.includes('/api/health')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'ok',
          app: 'e2e',
          env: 'test',
          auth_mode: 'session',
        }),
      })
      return
    }

    if (url.includes('/api/auth/login')) {
      authenticated = true
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ name: 'operator', role: 'operator', is_admin: false }),
      })
      return
    }

    if (url.includes('/api/auth/logout')) {
      authenticated = false
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true }),
      })
      return
    }

    if (url.includes('/api/auth/whoami')) {
      if (!authenticated) {
        await route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({
            error: { code: 'AUTH_FAILED', message: '未登录或登录已失效,请重新登录' },
          }),
        })
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ name: 'operator', role: 'operator', is_admin: false }),
      })
      return
    }

    // 外壳自己会拉的两个接口。形状见 smoke.spec.ts 里那段说明:
    // 给错形状会让两个横幅在 ErrorBoundary 外面抛,整页打不开
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

test('未登录访问一条深链,会带着 next 落到登录页', async ({ page }) => {
  await page.goto('/tasks')

  await expect(page).toHaveURL(/\/login\?next=/)
  // next 里要保住原来那一页。丢掉它的话,运营从聊天窗点进来的那条链接就白点了
  expect(decodeURIComponent(new URL(page.url()).search)).toContain('/tasks')
  await expect(page.getByRole('button', { name: '登录' })).toBeVisible()
})

test('登录成功后回到原来要去的那一页', async ({ page }) => {
  await page.goto('/tasks')
  await expect(page).toHaveURL(/\/login\?next=/)

  await page.getByLabel('用户名').fill('operator')
  await page.getByLabel('密码').fill('pw-for-e2e')
  await page.getByRole('button', { name: '登录' }).click()

  await expect(page).toHaveURL(/\/tasks$/)
  // 外壳起来了 = 侧栏那一套现在点得动
  await expect(page.getByText('服装上架平台')).toBeVisible()
})

test('退出登录之后回到登录页,原来那一页也进不去了', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('用户名').fill('operator')
  await page.getByLabel('密码').fill('pw-for-e2e')
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page).toHaveURL(/\/today$/)

  await page.getByRole('button', { name: /operator/ }).click()
  await page.getByText('退出登录').click()

  await expect(page).toHaveURL(/\/login/)

  // 反向确认:退出是真的退了,不是只换了一个地址。直接敲回原来那一页,
  // 应当再次被弹回登录页 —— 少了这一条,一个只做 navigate 而没调后端的
  // "退出登录"照样能让上面那句 toHaveURL 通过
  await page.goto('/today')
  await expect(page).toHaveURL(/\/login\?next=/)
})
