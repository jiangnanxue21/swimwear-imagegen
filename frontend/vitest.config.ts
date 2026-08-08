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
 * ## 为什么是 `projects` 而不是 `environmentMatchGlobs`
 *
 * 上面那件事本来是 `environmentMatchGlobs` 做的。Vitest 3 把它标了废弃，
 * **Vitest 4 直接删掉了** —— 而删掉的方式是「配置项不认识就忽略」，
 * 不是报错。表现因此格外坏：升上去之后 `tests/component/**` 会安安静静地
 * 掉回 `environment: 'node'`，然后 21 条用例在 `document is undefined` 上炸掉，
 * 而配置文件里那一行还好端端地写着。
 *
 * 换成 `projects` 之后，环境是每个 project 自己的必填项，
 * 「哪个目录跑在哪个环境」不再依赖一个可能被静默忽略的匹配规则。
 * `extends: true` 让两个 project 继承本文件的 plugins / resolve / setupFiles /
 * `passWithNoTests`，重复的只有 name、environment、include 三行。
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
  /**
   * 缓存目录搬出 `node_modules/`。
   *
   * Vite 默认把它放在 `node_modules/.vite`,而 `node_modules/` 的属主取决于
   * **是谁装的依赖**:在容器里用 root 跑过一次 `npm ci`、再换普通用户跑测试,
   * 这个目录就归 root 了,`mkdir` 会被 `EACCES` 挡下 ——
   * 表现是**一条用例都起不来**,而不是某几条红。
   *
   * 这不是仓库的代码缺陷,是机器前提;但仓库这一侧能把它变成不可能发生:
   * `.vite-cache/` 与 `node_modules/` 同级、由跑测试的那个用户创建,
   * 不再继承别人装依赖时留下的属主。
   *
   * **空结果不能读作通过。** `passWithNoTests: false`(见下)挡的是
   * "一条都没匹配上还绿着";这一行挡的是"一条都没起来"。两件事都会
   * 产出一个空的用例集,而只有前者会被 include 规则本身发现。
   */
  cacheDir: '.vite-cache',
  resolve: {
    // `import.meta.dirname` 而不是 `__dirname`:Vite 8 的原生配置加载器
    // (`configLoader: 'native'`,下个大版本会变成默认)不提供 CJS 的 `__dirname`,
    // 现在每次 dev/build/test 都会为此打一条警告。换掉它是为了别让这条常驻警告
    // 把真正的构建警告淹掉 —— 需要 Node >= 20.11,CI 与 Dockerfile 都是 22。
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  test: {
    globals: false,
    setupFiles: ['./tests/setup.ts'],
    // Playwright 用例在 tests/e2e 下，由 playwright.config.ts 跑。
    // 不排除的话 Vitest 会把它们当成自己的用例收进来，然后在
    // `import { test } from '@playwright/test'` 上炸掉。
    //
    // 下面两个 project 的 include 已经不覆盖 tests/e2e 了，这一行是第二道锁：
    // 哪天有人把 include 放宽回 `tests/**`，它还挡得住。
    exclude: ['node_modules/**', 'dist/**', 'tests/e2e/**'],
    passWithNoTests: false,
    reporters: ['default'],
    projects: [
      {
        extends: true,
        test: {
          name: 'unit',
          environment: 'node',
          include: ['tests/unit/**/*.test.{ts,tsx}'],
        },
      },
      {
        extends: true,
        test: {
          name: 'component',
          environment: 'jsdom',
          // component 目录下有 .ts 也有 .tsx（download.test.ts 不渲染组件，
          // 但要 Blob / URL.createObjectURL，仍然得在 jsdom 里）
          include: ['tests/component/**/*.test.{ts,tsx}'],
        },
      },
    ],
  },
})
