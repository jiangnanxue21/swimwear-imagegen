/**
 * `saveBlob` 的行为测试（A45-#4 / #22 的回归护栏）。
 *
 * ## 为什么这份测试到现在才有
 *
 * A45-#4 把重复的 `saveBlob` 合并成一份，护栏是
 * `backend/tests/pure/test_a45_batch8_fixes.py` 里一条**文本断言**：全仓只允许
 * 出现一个 `export function saveBlob`。那条断言守的是「只有一份实现」，
 * 守不住「这一份实现是对的」—— 两件事被合并之后，**没有任何用例执行过下载**。
 *
 * 于是 A45-#22 那个 bug（同 tick 内 `revokeObjectURL`，Firefox/Safari 上静默
 * 取消下载）是靠读代码发现的，不是靠测试。它修完之后同样没有用例：下一个人
 * 把 60 秒改回 0，全仓依然是绿的，而现象只在非 Chrome 浏览器上出现。
 *
 * ## 为什么在 tests/component/ 而不是 tests/unit/
 *
 * `saveBlob` 要 `document.createElement` 和 `URL.createObjectURL`。
 * `vitest.config.ts` 里只有 `tests/component/**` 拿 jsdom，unit 那批是 node
 * 环境且文件头有整段论证说它们刻意不碰 DOM —— 放进去会破坏那个前提。
 * 文件是 `.ts` 不是 `.tsx`：这里没有组件，只有 DOM。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { saveBlob } from '../../src/api/batch'

/** jsdom 不实现 object URL，两个都得自己接。 */
let created: Blob[]
let revoked: string[]

beforeEach(() => {
  created = []
  revoked = []
  vi.useFakeTimers()
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: (blob: Blob) => {
      created.push(blob)
      return `blob:mock/${created.length}`
    },
    revokeObjectURL: (url: string) => {
      revoked.push(url)
    },
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('saveBlob', () => {
  it('把 blob 挂到一个带 download 名的 <a> 上并点掉', () => {
    const blob = new Blob(['zip-bytes'], { type: 'application/zip' })
    const clicks: HTMLAnchorElement[] = []
    // 不能 spy 在某个具体元素上 —— 元素是函数内部造的。挂在原型上。
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        clicks.push(this)
      })

    saveBlob(blob, '批次-2026-08.zip')

    expect(created).toEqual([blob])
    expect(clicks).toHaveLength(1)
    expect(clicks[0].href).toBe('blob:mock/1')
    // 中文名不该被转义成 URL 编码后再落到磁盘上
    expect(clicks[0].download).toBe('批次-2026-08.zip')

    click.mockRestore()
  })

  it('点完就把 <a> 摘掉，不在 body 里留残骸', () => {
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {})

    saveBlob(new Blob(['x']), 'a.xlsx')
    saveBlob(new Blob(['y']), 'b.xlsx')

    // 运营一天导十几次。每次留一个 <a> 的话，DOM 会一直长
    expect(document.querySelectorAll('a')).toHaveLength(0)

    click.mockRestore()
  })

  /**
   * 这一条是 A45-#22 的**唯一**证伪点。
   *
   * 把实现改回 `URL.revokeObjectURL(url)` 同步调用、或把 60_000 改成 0，
   * 它都会红。Chrome 上这两种写法都能下载成功，所以人工测试测不出来。
   */
  it('不在同一个 tick 里 revoke —— 否则 Firefox/Safari 会静默取消下载', () => {
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {})

    saveBlob(new Blob(['big']), 'big.zip')

    expect(revoked).toEqual([])

    // 一个宏任务还不够：几十 MB 的 zip 在 Safari 上开始读之前可能更久
    vi.advanceTimersByTime(1)
    expect(revoked).toEqual([])

    click.mockRestore()
  })

  it('但最终会 revoke —— 否则 blob 挂到页面关闭', () => {
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {})

    saveBlob(new Blob(['big']), 'big.zip')
    vi.advanceTimersByTime(60_000)

    expect(revoked).toEqual(['blob:mock/1'])

    click.mockRestore()
  })

  it('两次下载各自持有自己的 URL，互不吊销对方', () => {
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {})

    saveBlob(new Blob(['1']), 'one.zip')
    vi.advanceTimersByTime(30_000)
    saveBlob(new Blob(['2']), 'two.zip')

    // 第一份到期时，第二份才走了一半，不该被连坐
    vi.advanceTimersByTime(30_000)
    expect(revoked).toEqual(['blob:mock/1'])

    vi.advanceTimersByTime(30_000)
    expect(revoked).toEqual(['blob:mock/1', 'blob:mock/2'])

    click.mockRestore()
  })
})
