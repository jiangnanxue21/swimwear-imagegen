/**
 * Vitest 全局 setup。
 *
 * 只做两件事，两件都是**防退化**的，不是便利设施。
 *
 * ## 1. jest-dom 断言
 *
 * `toBeInTheDocument()` / `toBeDisabled()` 这类。只在 jsdom 环境下装，
 * node 环境（那 29 条纯函数用例）用不上它。
 *
 * ## 2. 禁止 `.skip` / `.only` 混进来
 *
 * 方案 v4.1 第 10.1 节把「Vitest 通过，**0 skip**」列为人工测试准入项，
 * 并且写明「不接受 skip / todo 状态进入人工测试」。
 *
 * 靠人工确认这条会失效，理由和这个仓库里其它门禁一样：它不会在发生的当时叫一声。
 * 一条 `it.skip` 混进来之后，测试输出里只是多一行浅色的 skipped，
 * CI 仍然是绿的，而下一个人打开这个目录看到的是「有覆盖」。
 * 这正是 29 条用例躺了八个版本的那个机制。
 *
 * 所以把它变成断言：跑完之后一旦有 skipped / todo，直接让进程非零退出。
 * 要临时跳过某条用例，正确做法是**删掉它并在提交信息里说明**，
 * 而不是留一个看起来还在的空壳。
 */
import { afterAll, afterEach, expect } from 'vitest'

if (typeof window !== 'undefined') {
  await import('@testing-library/jest-dom/vitest')

  // Testing Library 只在 `globals: true` 时自动注册 cleanup。这里刻意关了 globals
  // （用例显式 import describe/it/expect，读起来更清楚来源），所以要自己挂。
  // 漏掉它的表现很有迷惑性：**上一条用例渲染的 DOM 会留在页面上**，
  // 于是 `getByText` 报「找到多个」，而错的其实是上一条用例没被清掉。
  const { cleanup } = await import('@testing-library/react')
  afterEach(() => cleanup())

  // ---------------------------------------------------------------- jsdom 垫片
  //
  // 两条，都是 jsdom 没实现而 antd 一定会碰的东西。不垫的话每条用例都会
  // 往 stderr 里吐一屏 "Not implemented" ——**用例照样是绿的**，
  // 只是输出被淹掉。而真正的失败信息就混在那一屏里。
  //
  // 垫片本身不改变任何被测行为：一个返回空的媒体查询，一个把
  // getComputedStyle 的伪元素参数丢掉（antd 量滚动条宽度时用它）。
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia
  }

  const nativeGetComputedStyle = window.getComputedStyle.bind(window)
  window.getComputedStyle = ((element: Element, pseudoElement?: string | null) =>
    pseudoElement
      ? nativeGetComputedStyle(element)
      : nativeGetComputedStyle(element)) as typeof window.getComputedStyle
}

// `expect` 在这里没有别的用途，但导入它能让 setup 文件本身出错时更早暴露
void expect

afterAll(async (suite) => {
  const offenders: string[] = []

  const walk = (task: { name?: string; mode?: string; tasks?: unknown[] }, trail: string[]) => {
    const path = task.name ? [...trail, task.name] : trail
    if (task.mode === 'skip' || task.mode === 'todo') {
      offenders.push(path.join(' › '))
    }
    for (const child of (task.tasks ?? []) as typeof task[]) {
      walk(child, path)
    }
  }

  walk(suite as never, [])

  if (offenders.length > 0) {
    // 直接抛：Vitest 会把 afterAll 的失败算进退出码，CI 因此会红
    throw new Error(
      `发现 ${offenders.length} 条 skip/todo 用例。方案 10.1 节要求 Vitest 0 skip —— ` +
        `请修好它或删掉它，不要留一个看起来还在的空壳：\n  ` +
        offenders.join('\n  '),
    )
  }
})
