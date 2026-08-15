/**
 * a51:字段级校验明细,与建档试算/停用三个新方法。
 *
 * ## 这份文件挡的是什么
 *
 * 后端为 `loc` 付过一次真金白银的代价:`spu_service._api_loc()` 专门把
 * 纯层的 `variant_codes[1]` 翻成 `color_variants[1].variant_code`,而那个
 * 函数的 docstring 里记着上一版翻错时的表现 ——「前端高亮不到任何一行」。
 *
 * 翻它的**唯一**理由是让表单定位到具体那一行,而在此之前前端把这个结构
 * 丢在了 `` `${loc}: ${msg}` `` 的字符串拼接里。后端付了钱、前端没接 ——
 * 而这件事**不会有任何东西报错**:界面照样弹一条红字,只是它指不到任何
 * 一个输入框。又一次 §3.43 那一族。
 *
 * ## 为什么验的是函数,不是界面
 *
 * `fieldProblems` 是纯输入输出:给一个 axios 错误,回一组 `{loc, msg}`。
 * 建档页怎么把 `loc` 映射到第几行是那一页自己的事(由
 * `backend/tests/pure/test_a51_*` 的读源码守卫钉着)。写进一条用例的话,
 * 表单加一个字段会让"结构还在不在"这条断言跟着红,而两者的修法完全不同。
 */
import { describe, expect, it, vi } from 'vitest'

const post = vi.hoisted(() => vi.fn())
const get = vi.hoisted(() => vi.fn())

vi.mock('../../src/api/client', async () => {
  const actual = await vi.importActual<typeof import('../../src/api/client')>(
    '../../src/api/client',
  )
  return { ...actual, apiClient: { post, get } }
})

const { fieldProblems } = await import('../../src/api/client')
const { spusApi } = await import('../../src/api/spus')

/**
 * 最后一次调用的参数。
 *
 * 不用 `.at(-1)` —— `tsconfig.json` 的 `lib` 停在 ES2021,而 `Array.prototype.at`
 * 是 ES2022。为一条测试便利去抬整个项目的 lib 是反过来的:那会让**生产代码**
 * 也能用上一批运行时未必支持的方法,而这份 tsconfig 的 target 是有人定过的。
 */
function lastCall(fn: { mock: { calls: unknown[][] } }): unknown[] {
  const { calls } = fn.mock
  return calls[calls.length - 1]
}

/** 一个长得像 axios 的 422。`isAxiosError` 认的是这个标志位 */
function axios422(fields: { loc: string; msg: string }[]) {
  return {
    isAxiosError: true,
    response: {
      status: 422,
      data: { error: { code: 'INPUT_INVALID', message: '入参不合法', fields } },
    },
  }
}

describe('fieldProblems', () => {
  it('保住 loc 与 msg 的结构,而不是拼成一行', () => {
    const err = axios422([
      { loc: 'color_variants[1].variant_code', msg: '颜色编码 BLK 在同一个 SPU 里重复了' },
    ])

    expect(fieldProblems(err)).toEqual([
      { loc: 'color_variants[1].variant_code', msg: '颜色编码 BLK 在同一个 SPU 里重复了' },
    ])
  })

  it('msg 里含冒号时也不会被切坏', () => {
    // 这一条解释了为什么不能拿 `describeError` 那份拼过的字符串再解析回来:
    // 分隔符本身就可能出现在内容里,而后端的错误文案经常带冒号
    const err = axios422([{ loc: 'spu_code', msg: '含不允许的字符:请只用 A-Z 0-9 _' }])

    expect(fieldProblems(err)[0].msg).toBe('含不允许的字符:请只用 A-Z 0-9 _')
  })

  it('多行明细逐条回,顺序不变', () => {
    const err = axios422([
      { loc: 'color_variants[0].variant_code', msg: 'a' },
      { loc: 'color_variants[2].variant_code', msg: 'b' },
    ])

    expect(fieldProblems(err).map((p) => p.loc)).toEqual([
      'color_variants[0].variant_code',
      'color_variants[2].variant_code',
    ])
  })

  it('没有明细时回空数组,不是 null', () => {
    // 调用方一律 `.filter()` / `.find()`,少一次判空就少一次漏判
    expect(fieldProblems(axios422([]))).toEqual([])
    expect(fieldProblems({ isAxiosError: true, response: { status: 500, data: {} } })).toEqual(
      [],
    )
  })

  it('根本不是请求失败时也回空数组', () => {
    expect(fieldProblems(new Error('前端自己抛的'))).toEqual([])
    expect(fieldProblems(undefined)).toEqual([])
  })
})

describe('spusApi 的试算与停用', () => {
  it('试算打 /spus/preview,而且**不带**幂等键', async () => {
    // 试算不写库,带键反而有害:它会占用一把键,而真正那次提交要用同一把
    post.mockResolvedValue({ data: { skus: [], sku_count: null, code_taken: false } })

    await spusApi.preview({
      spu_code: 'SW-101',
      color_variants: [{ variant_code: 'BLK', working_name: '黑' }],
      size_template: null,
    })

    const [url, , config] = lastCall(post)
    expect(url).toBe('/spus/preview')
    expect(config).toBeUndefined()
  })

  it('第二步试算不传尺码模板 —— 那时它还没被选', async () => {
    post.mockResolvedValue({ data: { skus: [], sku_count: null, code_taken: false } })

    await spusApi.preview({
      spu_code: 'SW-101',
      color_variants: [{ variant_code: 'BLK', working_name: '' }],
      size_template: null,
    })

    expect(lastCall(post)[1]).toMatchObject({ size_template: null })
  })

  it('停用带理由,恢复不带 —— 后者是撤销,前者是决定', async () => {
    post.mockResolvedValue({ data: { id: 'spu-9' } })

    await spusApi.disable('spu-9', '供应商撤款')
    expect(lastCall(post).slice(0, 2)).toEqual([
      '/spus/spu-9/disable',
      { reason: '供应商撤款' },
    ])

    await spusApi.restore('spu-9')
    expect(lastCall(post)[0]).toBe('/spus/spu-9/restore')
    expect(lastCall(post)[1]).toBeUndefined()
  })

  it('列表把 include_disabled 透传成 query', async () => {
    get.mockResolvedValue({ data: { items: [] } })

    await spusApi.list({ page: 1, include_disabled: true })

    expect(lastCall(get)[1]).toMatchObject({
      params: { page: 1, include_disabled: true },
    })
  })
})
