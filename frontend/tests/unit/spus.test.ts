/**
 * 建档客户端(PRD §9.1 的前端那一半)。
 *
 * ## 这份文件挡的是什么
 *
 * `POST /api/spus` 的 `Idempotency-Key` 在 2026-08-09 评审前**两侧都没有**。
 * 后端本轮实现了它;前端不传的话后端那一半等于没接线 —— 而"没接线"这件事
 * **不会有任何东西报错**:请求照常成功,双击照常建两次,只是第二次撞
 * `uq_spus_spu_code` 之后运营看到「SPU 编码 X 已存在」,一句在双击这个语境下
 * 是假的错误。这正是本仓 §3.43 那一族(算好了没人读 / 接线断在最后一跳)。
 *
 * ## 为什么验的是"发出去的头",不是界面
 *
 * 键是发给后端的东西,不是界面上的东西,所以断言点在请求参数上。
 * 建档页那个三步表单的填写流程由它自己的用例负责;把两件事写进一条用例的话,
 * 表单加一个字段就会让"头还在不在"这条断言跟着红,而两者的修法完全不同。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const post = vi.hoisted(() => vi.fn())

vi.mock('../../src/api/client', () => ({
  apiClient: { post, get: vi.fn() },
}))

const { spusApi } = await import('../../src/api/spus')

const PAYLOAD = {
  spu_code: 'SW-101',
  internal_name: '三色泳衣',
  audience: 'WOMEN' as const,
  base_category: 'swimwear',
  supplier_ref: null,
  color_variants: [{ variant_code: 'BLK', working_name: '黑' }],
  size_template: 'ALPHA_SML',
}

describe('spusApi.create 的幂等键', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    post.mockResolvedValue({ data: { id: 'spu-9' } })
  })

  it('给了键就带 Idempotency-Key 头', async () => {
    await spusApi.create(PAYLOAD, 'key-abc')

    expect(post).toHaveBeenCalledTimes(1)
    const [url, body, config] = post.mock.calls[0]
    expect(url).toBe('/spus')
    expect(body).toEqual(PAYLOAD)
    expect(config?.headers).toEqual({ 'Idempotency-Key': 'key-abc' })
  })

  it('没给键就不带那个头,而不是带一个空串', async () => {
    /**
     * 空串是**一把合法的键**在 HTTP 上的样子,而后端会把它 strip 成 None。
     * 两边今天恰好对得上,但让客户端发一个空头是在赌那件事永远成立 ——
     * 不发才是"我没有键"的准确表达。
     */
    await spusApi.create(PAYLOAD)

    const config = post.mock.calls[0][2]
    expect(config?.headers).toBeUndefined()
  })

  it('同一把键传两次,发出去的是同一个值', async () => {
    /**
     * 看起来是废话,写下来是因为它挡的是**调用方每次现算一把键**那种退化:
     * 那样键会退化成随机串,一次都不会命中,而请求看起来完全正常。
     */
    await spusApi.create(PAYLOAD, 'stable-key')
    await spusApi.create(PAYLOAD, 'stable-key')

    expect(post.mock.calls[0][2].headers['Idempotency-Key']).toBe(
      post.mock.calls[1][2].headers['Idempotency-Key'],
    )
  })
})
