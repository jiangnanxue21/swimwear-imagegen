/**
 * 运行日志页(docs/LOG-CONSOLE.md §5 / §8 第四条)。
 *
 * 这里有两类用例,别把它们混成一类:
 *
 *     行为    折叠、链路模式、环形不可用时说什么
 *     守卫    **这一页不许持有任何一张分类表**(硬规则第 4 条)
 *
 * 守卫那条是源码级的,而它是这一整套设计能不能守住的关键:前端一旦自己存了
 * 一份域名或事件码,它就会和后端注册表分叉 —— 分叉的表现是筛选框里列着一个
 * 后端已经改名的码,筛出来永远是空的,而运营会读成「这段时间没发生」。
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from 'antd'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import OpsLogPage from '../../src/pages/OpsLogPage'
import type { LogEntry, LogMeta, LogPage, RingMeta } from '../../src/api/ops'

const opsMocks = vi.hoisted(() => ({ logs: vi.fn(), meta: vi.fn(), llmPayload: vi.fn() }))
vi.mock('../../src/api/ops', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/ops')>()
  return { ...actual, opsApi: opsMocks }
})

const RING: RingMeta = {
  cap: 5000,
  held: 3,
  enabled: true,
  dropped_since_boot: 0,
  last_error: null,
}

function entry(over: Partial<LogEntry>): LogEntry {
  return {
    seq: '1-1',
    ts: '2026-08-16T10:00:00+0000',
    ts_unparsed: false,
    level: 'INFO',
    logger: 'app.services.publish_service',
    service: 'api',
    pid: 1234,
    domain: 'publish',
    domain_label: '发布上架',
    event: null,
    event_label: null,
    routine: false,
    routine_group: null,
    round_summary: false,
    message: 'something happened',
    request_id: 'req-1',
    fields: {},
    raw: '{"message":"something happened"}',
    ...over,
  }
}

const META: LogMeta = {
  domains: [
    { key: 'publish', label: '发布上架' },
    { key: 'gen', label: '生成流水线' },
  ],
  services_seen: { api: 3 },
  events: [],
  routine_groups: [{ key: 'lease', label: '租约让位' }],
  levels: ['INFO', 'WARNING', 'ERROR'],
  ring: RING,
  payload_capture: {
    enabled: true,
    ttl_seconds: 86400,
    dropped_since_boot: 0,
    last_error: null,
  },
}

function page(
  items: LogEntry[],
  ring: RingMeta = RING,
  domainCounts: LogPage['domain_counts'] = {},
): LogPage {
  return {
    items,
    ring,
    oldest_ts: items.length ? items[items.length - 1].ts : null,
    shown_oldest_ts: items.length ? items[items.length - 1].ts : null,
    matched: items.length,
    truncated: false,
    services_seen: items.length ? { api: items.length } : {},
    domain_counts: domainCounts,
  }
}

function renderPage(initial = '/ops-logs') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <QueryClientProvider client={client}>
        <App>
          <OpsLogPage />
        </App>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('运行日志页', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    opsMocks.meta.mockResolvedValue(META)
    opsMocks.logs.mockResolvedValue(page([entry({})]))
  })

  it('中文标签来自后端,前端不翻译', async () => {
    opsMocks.logs.mockResolvedValue(
      page([entry({ event: 'x.y', event_label: '投递放弃', domain_label: '发布上架' })]),
    )
    renderPage()

    expect(await screen.findByText('投递放弃')).toBeInTheDocument()
    // 「发布上架」同时出现在左侧域轨道和行内标签上 —— 两处都来自后端
    expect(screen.getAllByText('发布上架').length).toBeGreaterThan(0)
  })

  it('例行事件折成一根计数条,而折进去的 WARN 单独点名', async () => {
    opsMocks.logs.mockResolvedValue(
      page([
        entry({ seq: 'a', routine: true, routine_group: '租约让位', level: 'WARNING' }),
        entry({ seq: 'b', routine: true, routine_group: '租约让位' }),
        entry({ seq: 'c', message: '这一轮白花钱了' }),
      ]),
    )
    renderPage()

    expect(await screen.findByText(/例行 ×2/)).toBeInTheDocument()
    expect(screen.getByText(/租约让位 ×2/)).toBeInTheDocument()
    expect(screen.getByText(/含 WARN 1/)).toBeInTheDocument()
    // 非例行那条不许被折进去
    expect(screen.getByText('这一轮白花钱了')).toBeInTheDocument()
  })

  it('后端说不是例行(ERROR)时就不折 —— 判定不在前端', async () => {
    opsMocks.logs.mockResolvedValue(
      page([entry({ level: 'ERROR', routine: false, routine_group: '租约让位', message: '炸了' })]),
    )
    renderPage()

    expect(await screen.findByText('炸了')).toBeInTheDocument()
    expect(screen.queryByText(/例行 ×/)).not.toBeInTheDocument()
  })

  it('环形读不到时说清楚,而不是画一张空列表', async () => {
    opsMocks.logs.mockResolvedValue(page([], { ...RING, unavailable_reason: 'ConnectionError' }))
    renderPage()

    expect(await screen.findByText(/这不代表这段时间没有日志/)).toBeInTheDocument()
    expect(screen.getByText('ConnectionError')).toBeInTheDocument()
  })

  it('链路模式从 URL 恢复,并且只按链路键查', async () => {
    renderPage('/ops-logs?trace_kind=task&trace_id=abc-123')

    await waitFor(() => expect(opsMocks.logs).toHaveBeenCalled())
    const sent = opsMocks.logs.mock.calls[0][0]
    expect(sent.task_id).toBe('abc-123')
    // 进了链路就该看见这条链路的**全部**;带着域筛选会让人以为它只做了这些事
    expect(sent.domain).toBeUndefined()
    expect(sent.level).toBeUndefined()
    expect(await screen.findByText('abc-123')).toBeInTheDocument()
  })

  it('窗口边界要说出来 —— 查不到更早的不是没发生', async () => {
    renderPage()
    expect(await screen.findByText(/滚出窗口了/)).toBeInTheDocument()
  })
})

// ================================================================ 守卫四

/**
 * 前端不内置分类表。
 *
 * 判据用「点号分隔的小写码」这个**形状**,而不是去和后端那张表逐条比对:
 * 后者要么把注册表复制进测试(那本身就是第三份真相),要么跨语言读 Python
 * (`frontend/CLAUDE.md` 明令不要用 Python 扫前端源码)。形状判据挡得住
 * 真正想防的那件事 —— 有人把 `'gen.round_evaluated'` 写进前端。
 */
describe('这一页不持有分类表', () => {
  const sources = ['../../src/pages/OpsLogPage.tsx', '../../src/api/ops.ts']

  it.each(sources)('%s 里没有事件码字面量', (relative) => {
    const path = fileURLToPath(new URL(relative, import.meta.url))
    const code = readFileSync(path, 'utf-8')
      // 注释里可以举例(说明文字要举得出例子),扫的是活代码
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, '')

    const literals = [...code.matchAll(/['"]([a-z]+\.[a-z_]+)['"]/g)].map((m) => m[1])
    const allowed = new Set(['node.js'])

    expect(literals.filter((one) => !allowed.has(one))).toEqual([])
  })

  it('域下拉的取值来自 /meta,不是写死的十五个', async () => {
    opsMocks.meta.mockResolvedValue({ ...META, domains: [{ key: 'only', label: '只有这一个' }] })
    opsMocks.logs.mockResolvedValue(page([]))
    renderPage()

    expect(await screen.findByText('只有这一个')).toBeInTheDocument()
    expect(screen.queryByText('发布上架')).not.toBeInTheDocument()
  })
})

// ================================================================ a54 修的那几件

describe('运行日志页(a54 修复)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    opsMocks.meta.mockResolvedValue(META)
    opsMocks.logs.mockResolvedValue(page([entry({})]))
  })

  it('计数条点得开 —— 折叠是降噪,不是掩埋', async () => {
    opsMocks.logs.mockResolvedValue(
      page([
        entry({ seq: 'a', routine: true, routine_group: '租约让位', message: '让位一次' }),
        entry({ seq: 'b', routine: true, routine_group: '租约让位', message: '让位两次' }),
      ]),
    )
    renderPage()

    const bar = await screen.findByText(/例行 ×2/)
    expect(screen.queryByText('让位一次')).not.toBeInTheDocument()
    await userEvent.click(bar)
    expect(await screen.findByText('让位一次')).toBeInTheDocument()
    expect(screen.getByText('让位两次')).toBeInTheDocument()
  })

  it('域计数来自后端,点进一个域之后别的域不会全变 0', async () => {
    opsMocks.logs.mockResolvedValue(
      page([entry({ domain: 'publish' })], RING, {
        publish: { total: 3, warn: 0, error: 0 },
        gen: { total: 7, warn: 1, error: 0 },
      }),
    )
    renderPage('/ops-logs?domain=publish')

    // 这一屏一条 publish 的日志都没别的域,但 gen 的计数照样在
    expect(await screen.findByText('7')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('级别筛选说的是「及以上」,不是「恰好等于」', async () => {
    renderPage()
    // 取值来自 /meta,标签上带 ≥ —— 选 WARNING 的人要看得见 ERROR
    expect(await screen.findByText('≥ WARNING')).toBeInTheDocument()
    expect(screen.getByText('≥ ERROR')).toBeInTheDocument()
  })

  it('事件精筛有入口,取值来自 /meta', async () => {
    opsMocks.meta.mockResolvedValue({
      ...META,
      events: [
        { key: 'a.b', label: '某件事', domain: 'publish', routine: false, routine_group: null },
      ],
    })
    renderPage()

    const picker = await screen.findByRole('combobox', { name: '按事件精筛' })
    await userEvent.click(picker)
    expect(await screen.findByText('某件事')).toBeInTheDocument()
  })

  it('链路模式按轮次分段,段头取后端标出来的那条', async () => {
    opsMocks.logs.mockResolvedValue(
      page([
        entry({ seq: 'a', fields: { round: 1 }, message: '领到任务' }),
        entry({
          seq: 'b',
          fields: { round: 1 },
          round_summary: true,
          message: '4 张候选 · B 档',
        }),
      ]),
    )
    renderPage('/ops-logs?trace_kind=task&trace_id=t-1')

    expect(await screen.findByText(/第 1 轮/)).toBeInTheDocument()
    // 一处是段头摘要,另一处是原始日志行;只剩一处说明段头没有取里程碑。
    expect(screen.getAllByText('4 张候选 · B 档')).toHaveLength(2)
  })

  it('call 芯片一步打开载荷页签', async () => {
    opsMocks.logs.mockResolvedValue(
      page([entry({ seq: 'a', fields: { llm_call_id: 'c41f8a09d2e3' } })]),
    )
    opsMocks.llmPayload.mockResolvedValue({
      llm_call_id: 'c41f8a09d2e3',
      provider: 'vision',
      model: 'v-2026',
      request: {
        endpoint: 'https://example.test/v1/chat',
        headers: { authorization: '***' },
        body: { model: 'v-2026' },
        images: [{ tag: 'CANDIDATE_IMAGE', mime_type: 'image/png', base64_chars: 42, sha256_16: 'ab' }],
        sha256_16: '9b7d31e0aa02c4f8',
        body_bytes: 412083,
      },
      attempts: [
        {
          attempt: 1,
          http_status: 429,
          duration_ms: 775,
          content_type: 'text/html',
          upstream_request_id: null,
          body: '<html>too many requests</html>',
        },
      ],
      truncated: [],
      ttl_seconds: 86400,
    })
    renderPage()

    await userEvent.click(await screen.findByText(/call /))
    // 图片只剩 chip,正文永不留存;耗时不再是空的
    expect(await screen.findByText(/CANDIDATE_IMAGE/)).toBeInTheDocument()
    expect(screen.getByText(/775 ms/)).toBeInTheDocument()
    expect(screen.getByText(/<html>too many requests<\/html>/)).toBeInTheDocument()
  })

  it('环形被关掉时说出来,而不是画一张空列表', async () => {
    opsMocks.logs.mockResolvedValue(page([], { ...RING, enabled: false, unavailable_reason: 'ring_disabled' }))
    renderPage()

    expect(await screen.findByText(/这不代表这段时间没有日志/)).toBeInTheDocument()
    expect(screen.getByText(/OPS_LOG_RING_ENABLED/)).toBeInTheDocument()
  })

  it('展开哪一行进 URL —— 刷新与分享链接都保得住', async () => {
    opsMocks.logs.mockResolvedValue(page([entry({ seq: 'a', message: '这一条' })]))
    renderPage('/ops-logs?expanded=a&tab=raw')

    expect(await screen.findByText(/控制台不改写它/)).toBeInTheDocument()
  })
})
