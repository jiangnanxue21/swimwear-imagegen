import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { App } from 'antd'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PromptsPage from '../../src/pages/PromptsPage'

const promptMocks = vi.hoisted(() => ({
  list: vi.fn(),
  read: vi.fn(),
  preview: vi.fn(),
  save: vi.fn(),
  readVersion: vi.fn(),
  activate: vi.fn(),
  reset: vi.fn(),
}))

vi.mock('../../src/api/prompts', () => ({ promptsApi: promptMocks }))
vi.mock('../../src/components/UnsavedGuard', () => ({ default: () => null }))

const zeroStats = {
  calls: 0,
  failures: 0,
  parse_failed: 0,
  provider_error: 0,
  unavailable: 0,
  diagnostic: 0,
  parse_failure_rate: null,
  failure_rate: null,
}

const overview = {
  key: 'vision_system_prompt',
  content: 'current prompt',
  version: 2,
  is_default: false,
  default_content: 'built-in prompt',
  anti_injection_block: null,
  warnings: [],
  stats_since: '2026-08-19T00:00:00Z',
  versions: [
    {
      version: 2,
      is_active: true,
      note: null,
      updated_by: 'admin',
      chars: 14,
      created_at: '2026-08-19T00:00:00Z',
      stats: zeroStats,
    },
    {
      version: 1,
      is_active: false,
      note: null,
      updated_by: 'admin',
      chars: 12,
      created_at: '2026-08-18T00:00:00Z',
      stats: zeroStats,
    },
  ],
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <App>
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/prompts/vision_system_prompt']}>
          <Routes>
            <Route
              path="/prompts/:key"
              element={(
                <>
                  <Link to="/prompts/vision_system_prompt_men">切到男装</Link>
                  <PromptsPage />
                </>
              )}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </App>,
  )
}

describe('PromptsPage rollback reason', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    promptMocks.list.mockResolvedValue({
      surfaces: [{
        key: 'vision_system_prompt',
        label: '女装视觉评分',
        tier: 'FREE',
        editable: true,
        ui_reachable: true,
        required_slots: [],
        consumers: [],
        has_static_default: true,
        active_version: 2,
        stats: zeroStats,
      }, {
        key: 'vision_system_prompt_men',
        label: '男装视觉评分',
        tier: 'FREE',
        editable: true,
        ui_reachable: true,
        required_slots: [],
        consumers: [],
        has_static_default: true,
        active_version: 2,
        stats: zeroStats,
      }],
      stats_window_days: 7,
      stats_unattributed: 0,
      stats_since: '2026-08-19T00:00:00Z',
    })
    promptMocks.read.mockImplementation(async (key: string) => ({
      ...overview,
      key,
      content: key.endsWith('_men') ? 'men current prompt' : overview.content,
    }))
    promptMocks.preview.mockImplementation(async (key: string, content: string) => ({
      key,
      chars: content.length,
      warnings: [{ code: key, message: `${key} lint` }],
    }))
    promptMocks.activate.mockResolvedValue({ ...overview, version: 1 })
    promptMocks.reset.mockResolvedValue({ ...overview, version: null, is_default: true })
  })

  it('取消回滚后不会把隐藏的旧理由带进恢复默认请求', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: '切回这版' }))
    const rollbackInput = await screen.findByPlaceholderText(/为什么退回这一版/)
    await user.type(rollbackInput, '只属于已取消弹窗的理由')
    await user.click(screen.getByRole('button', { name: /Cancel|取消/ }))

    await user.click(screen.getByRole('button', { name: '恢复默认' }))
    const resetInput = await screen.findByPlaceholderText(/为什么退回内置默认/)
    expect(resetInput).toHaveValue('')
    await user.click(screen.getByRole('button', { name: /OK|确 定|确定/ }))

    await waitFor(() => {
      expect(promptMocks.reset).toHaveBeenCalledWith(
        'vision_system_prompt',
        '',
        2,
      )
    })
  })

  it('切换 Prompt 路由会清空旧草稿并按新 key 重新体检', async () => {
    const user = userEvent.setup()
    renderPage()

    const editor = await screen.findByDisplayValue('current prompt')
    await user.clear(editor)
    await user.type(editor, 'shared edited prompt')
    await waitFor(() => {
      expect(promptMocks.preview).toHaveBeenCalledWith(
        'vision_system_prompt',
        'shared edited prompt',
      )
    })

    await user.click(screen.getByRole('link', { name: '切到男装' }))
    const menEditor = await screen.findByDisplayValue('men current prompt')
    expect(screen.queryByDisplayValue('shared edited prompt')).not.toBeInTheDocument()

    await user.clear(menEditor)
    await user.type(menEditor, 'shared edited prompt')
    await waitFor(() => {
      expect(promptMocks.preview).toHaveBeenCalledWith(
        'vision_system_prompt_men',
        'shared edited prompt',
      )
    })
  })
})
