/**
 * 五个 multipart 上传点必须挂 `LONG_TIMEOUT_MS`(前端评审 P1-4)。
 *
 * ## 修的是什么
 *
 * `LONG_TIMEOUT_MS` 的注释写着「加新的长接口时往这里挂」,发布、批次、导出、
 * 评审十来处都挂了 —— 而**全部五个上传点一处都没挂**,它们跑在 60 秒的默认
 * 超时上。axios 的 timeout 盖的是整个请求周期,**含把字节推上去的那一段**:
 * 一个 20MB 的素材(后端上限)在办公室上行带宽下 60 秒传不完是常态,
 * 而超时的那一刻后端可能刚收完、正在写库。
 *
 * 后果分两档:读侧(预览、模板)超时只是失败重来;`importCommit` 是**写**,
 * 它超时会走进本仓最重的那一支 ——「结果未知,不许直接重做」—— 而这个
 * "未知"是前端用一个偏短的超时**自己制造**的。A-33 修的正是这个病灶,
 * 只是当时只看了 JSON 接口。
 *
 * ## 为什么用 adapter 而不是 mock 掉 apiClient
 *
 * 与 `client.test.ts` 同一个理由:替换 adapter 让请求走完真实的拦截器链,
 * 我们在最后一站把 config 截下来。测的是**这一次调用真的发出去时带的
 * timeout**,而不是源码里有没有出现那个标识符 —— 后者用 grep 就能骗过。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { apiClient, LONG_TIMEOUT_MS } from '../../src/api/client'
import { productsApi } from '../../src/api/products'
import { batchApi } from '../../src/api/batch'
import { modelTemplatesApi } from '../../src/api/generation'

/** 每次调用截下来的 config。断言读它,不读源码文本。 */
let seen: InternalAxiosRequestConfig[]
let originalAdapter: typeof apiClient.defaults.adapter

beforeEach(() => {
  seen = []
  originalAdapter = apiClient.defaults.adapter
  apiClient.defaults.adapter = async (config) => {
    seen.push(config)
    return {
      data: {},
      status: 200,
      statusText: 'OK',
      headers: {},
      config,
    } as AxiosResponse
  }
})

afterEach(() => {
  apiClient.defaults.adapter = originalAdapter
})

/** 内容无所谓,大小也无所谓 —— 断言的是这次请求配了多久,不是传了什么 */
const file = () => new File(['x'], 'a.csv', { type: 'text/csv' })

describe('multipart 上传:超时按长动作算,不吃 60 秒的默认值', () => {
  it('素材上传(单文件上限 20MB)', async () => {
    await productsApi.uploadAsset('p1', file(), 'GARMENT_FRONT', null)
    expect(seen[0].timeout).toBe(LONG_TIMEOUT_MS)
  })

  it('商品 CSV 导入', async () => {
    await productsApi.importCsv(file())
    expect(seen[0].timeout).toBe(LONG_TIMEOUT_MS)
  })

  it('批量导入预览', async () => {
    await batchApi.importPreview(file())
    expect(seen[0].timeout).toBe(LONG_TIMEOUT_MS)
  })

  it('批量导入提交 —— 五个里唯一的写,超时会造出「结果未知」', async () => {
    await batchApi.importCommit(file(), 'tok')
    expect(seen[0].timeout).toBe(LONG_TIMEOUT_MS)
  })

  it('模特模板创建', async () => {
    await modelTemplatesApi.create(file(), {})
    expect(seen[0].timeout).toBe(LONG_TIMEOUT_MS)
  })
})
