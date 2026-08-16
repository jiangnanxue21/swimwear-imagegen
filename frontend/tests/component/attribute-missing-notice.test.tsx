import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { Extraction } from '../../src/api/attributes'
import {
  ActiveExtractionNotice,
  MissingEvidenceNotice,
  attributeOptionLabel,
} from '../../src/components/workbench/AttributeTab'


const labelOf = (field: string) => ({
  back_style: '背部款式',
  material: '材质',
}[field] ?? field)


describe('属性识别的未解决字段提示', () => {
  it('后端说整批已解决时不显示逐图 missing 警告', () => {
    const { container } = render(
      <MissingEvidenceNotice missing={[]} labelOf={labelOf} />,
    )

    expect(container.textContent).toBe('')
  })

  it('只展示后端判定仍未解决的字段及对应动作', () => {
    render(
      <MissingEvidenceNotice
        missing={[
          { field_name: 'material', reason: 'AWAITING_SUPPLIER_DATA' },
        ]}
        labelOf={labelOf}
      />,
    )

    expect(screen.getByText('本次识别后这些字段仍没有可用值')).toBeInTheDocument()
    expect(screen.getByText(/看图永远判断不了/)).toBeInTheDocument()
    expect(screen.getByText(/材质/)).toBeInTheDocument()
    expect(screen.queryByText('背部款式')).not.toBeInTheDocument()
  })
})


const activeRun: Extraction = {
  id: 'run-1',
  product_id: 'product-1',
  extractor: 'vision',
  model_name: 'qwen',
  prompt_version: 'vision-2.1',
  target_fields: ['back_style'],
  image_count: 3,
  succeeded_count: 1,
  failed_count: 0,
  failed_scopes: [],
  status: 'RUNNING',
  cancel_requested: false,
  can_cancel: true,
  request_disposition: null,
  duration_ms: null,
  created_at: null,
}


describe('属性识别的持续进度', () => {
  it('任务仍在运行时展示逐图进度和自动刷新说明', () => {
    render(<ActiveExtractionNotice run={activeRun} />)

    expect(screen.getByText('识别中:已处理 1/3 张图')).toBeInTheDocument()
    expect(screen.getByText(/每 2 秒自动更新/)).toBeInTheDocument()
  })

  it('终态不保留运行中提示', () => {
    const { container } = render(
      <ActiveExtractionNotice
        run={{ ...activeRun, status: 'COMPLETED', can_cancel: false }}
      />,
    )

    expect(container).toBeEmptyDOMElement()
  })
})


describe('属性枚举中文显示', () => {
  it('显示中文但保留英文编码作为提交值', () => {
    const spec = {
      value_type: 'ENUM',
      multi_value: false,
      options: ['TIE_BACK'],
      option_labels: { TIE_BACK: '系带背' },
    }

    expect(attributeOptionLabel(spec, 'TIE_BACK')).toBe('系带背')
    expect(spec.options[0]).toBe('TIE_BACK')
  })
})
