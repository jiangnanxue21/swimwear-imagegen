/**
 * 草稿页图片预览的渲染用例(AC-19 的运营侧那一半,A45-batch24)。
 *
 * ## 它挡的是什么
 *
 * 5-5 交付时后端把 `preview.images` 算好了、`api/workbench.ts` 的类型也齐了,
 * 而**没有任何组件读它**:全前端搜 `DraftImagePreview` / `missing_count` /
 * `UNPROVEN`,命中全部落在类型声明里,零组件命中。界面上那一段不存在。
 *
 * 后端那一侧的守卫(`test_draft_preview_actually_exposes_it`)钉到
 * `draft_preview` 的返回值为止,它的 docstring 自己写着「少这一跳的话本模块
 * 就是"算好了没人看"」—— 而下一跳当时没有任何守卫。这份文件就是那一跳。
 *
 * ## 为什么渲染 DraftTab 而不是只测 ImagePreview
 *
 * 缺口不在 `ImagePreview` 里(它当时根本不存在),而在**没有人调用它**。
 * 单独渲染那个子组件的用例,在有人把 `<ImagePreview>` 从 DraftTab 里删掉之后
 * 仍然全绿 —— 那正好回到 5-5 交付时的状态。所以这里渲染整个标签页,
 * 从运营看得到的 DOM 上断言。
 *
 * ## 为什么 mock 掉 useWriteError 与 react-query
 *
 * DraftTab 的「重新生成」按钮走 `useMutation`,而本文件要验的东西
 * 全部只取决于传进来的 `draft.preview.images`。连真实 mutation 等于让
 * 「界面上有没有这段图片表」的用例在 HTTP 桩改了以后跟着红。
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'

import type { ListingDraft } from '../../src/api/workbench'

vi.mock('@tanstack/react-query', () => ({
  useMutation: () => ({ mutate: () => {}, isPending: false }),
  useQueryClient: () => ({ invalidateQueries: () => {} }),
}))

vi.mock('../../src/hooks/useWriteError', () => ({
  useWriteError: () => () => {},
}))

const { default: DraftTab } = await import('../../src/components/workbench/DraftTab')

type Images
 = NonNullable<NonNullable<ListingDraft['preview']>['images']>

/** 一份最小但形状完整的草稿。图片那一段由每条用例自己给。 */
function draftWith(images: Images | undefined): ListingDraft {
  return {
    draft_id: 'd-1',
    spu: 'SW-001',
    status: 'READY',
    channel: 'generic',
    site: 'ANY',
    locale: 'en',
    spec_version: '1',
    fingerprint: 'abc123def456',
    image_set_id: 'is-1',
    validation_errors: [],
    stale: null,
    preview: {
      header: [],
      rows: [],
      images,
    },
  } as unknown as ListingDraft
}

/**
 * `UnsavedGuard` 里的 `useBlocker` 要一个**数据路由**上下文。
 * `MemoryRouter` 给不了(它不是 data router,`useBlocker` 会 invariant),
 * 所以走 `createMemoryRouter` + `RouterProvider` —— 与
 * `unsaved-guard.test.tsx` 里那一套同源。不套的话渲染在那一行就炸,
 * 而报错内容与图片预览毫无关系。
 */
function renderTab(images: Images | undefined) {
  const router = createMemoryRouter(
    [
      {
        path: '/',
        element: (
          <DraftTab
            draft={draftWith(images)}
            productId="p-1"
            step={undefined}
            onJump={() => {}}
          />

        ),
      },
    ],
    { initialEntries: ['/'] },
  )
  render(<RouterProvider router={router} />)
}



const READY: Images = {
  status: 'READY',
  note: '',
  colors: [
    {
      variant_id: 'v-red',
      variant_code: 'RED',
      missing_count: 0,
      rows: [{ sku: 'SW-001-RED-S', primary: 'asset-1', extras: ['asset-2'], status: 'OK' }],
    },
    {
      variant_id: 'v-blue',
      variant_code: 'BLU',
      missing_count: 1,
      rows: [{ sku: 'SW-001-BLU-S', primary: null, extras: [], status: 'MISSING' }],
    },
  ],
}

describe('草稿页的图片预览', () => {
  it('把每个颜色的每个 SKU 都摆出来', () => {
    renderTab(READY)
    expect(screen.getByText('图片预览')).toBeTruthy()
    expect(screen.getByText(/颜色 RED/)).toBeTruthy()
    expect(screen.getByText(/颜色 BLU/)).toBeTruthy()
    expect(screen.getByText('SW-001-RED-S')).toBeTruthy()
    expect(screen.getByText('asset-1')).toBeTruthy()
  })

  it('缺主图的行留在表里并说出来,不被过滤掉', () => {
    renderTab(READY)
    // 过滤掉的话运营会读成「这个尺码不在这次导出里」,而它在,只是没有图
    expect(screen.getByText('SW-001-BLU-S')).toBeTruthy()
    expect(screen.getByText('缺主图')).toBeTruthy()
    expect(screen.getByText('1 个 SKU 缺图')).toBeTruthy()
  })

  it('UNPROVEN 与 INCOMPATIBLE 分开显示,不合并成一句「没有图片信息」', () => {
    // 两者要运营做的事完全不同:前者重新生成草稿就好,
    // 后者是有人换过快照形状 —— 重新生成一百次也不会变好
    //
    // 断言看的是**中文那一句**而不是枚举值:界面上原来是「图片映射未算过」
    // 后面再挂一个印着 UNPROVEN 的 Tag,同一件事说两遍、其中一遍是英文。
    // 英文那一遍去掉了,这条用例要守的"两档分得开"由中文文案继续守。
    const unproven: Images = {
      status: 'UNPROVEN',
      note: '这份草稿建于颜色维快照之前,重新生成一次即可',
      colors: [],
    }
    const { unmount } = render(<div />)
    unmount()

    renderTab(unproven)
    expect(screen.getByText('图片映射未算过')).toBeTruthy()
    expect(screen.getByText(/重新生成一次即可/)).toBeTruthy()
    expect(screen.queryByText('图片映射形状不兼容')).toBeNull()
  })

  it('后端没带图片预览时给一句可执行的话,而不是空白', () => {
    renderTab(undefined)
    expect(screen.getByText(/这次返回没带图片预览/)).toBeTruthy()
  })
})
