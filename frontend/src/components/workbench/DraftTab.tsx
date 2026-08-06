/**
 * 上架草稿标签(FE-231~234)。
 *
 * ## 手填字段从预览里反推,不硬编码
 *
 * 哪些字段是运营手填的,写在渠道 spec 的 `manual_fields` 里(品牌、价格、
 * 币种、库存、备货天数)。前端读不到 YAML,但预览里每个字段都带 `source`,
 * 手填字段的 source 形如 `manual.price` —— 于是表单直接从预览生成。
 * 在前端另抄一份字段清单的话,spec 加一个手填字段就会在界面上凭空消失。
 *
 * **价格与库存永远在这一类里,不允许模型生成** —— 让模型给一个会真的向
 * 消费者收款的数字,是这条链路里唯一不可挽回的错误。
 *
 * ## 过期提示要说三句话(§4.5.1)
 *
 * 「哪个上游变了、变了哪些字段、需要执行什么动作」。只显示「已过期」三个字
 * 按任务书算未完成,所以 `stale.changes` 逐条展开,旧值新值都摆出来。
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Alert, App, Button, Card, Empty, Input, Space, Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useWriteError } from '../../hooks/useWriteError'
import {
  DRAFT_STATUS_LABEL,
  STALE_COMPONENT_LABEL,
  workbenchApi,
  type DraftField,
  type FlowStepResult,
  type ListingDraft,
  type StaleChange,
  type WorkbenchTab,
} from '../../api/workbench'
import { IssueList } from './FlowBits'
import UnsavedGuard from '../UnsavedGuard'
import { brandVars, fontScale } from '../../theme'
import BrandTag from '../BrandTag'

const FIELD_STATUS_TAG: Record<DraftField['status'], { text: string; color: string }> = {
  OK: { text: '通过', color: 'success' },
  WARNING: { text: '提醒', color: 'warning' },
  ERROR: { text: '不合格', color: 'error' },
}

function FieldTable({ fields, caption }: { fields: DraftField[]; caption: string }) {
  const columns: ColumnsType<DraftField> = [
    {
      title: '平台字段',
      dataIndex: 'label',
      width: 130,
      render: (label: string, row) => (
        <Space size={4}>
          <span>{label}</span>
          {row.required && (
            <Tooltip title="必填。缺失会阻断导出">
              <span style={{ color: brandVars.danger }}>*</span>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: '最终值',
      dataIndex: 'value',
      render: (value: string | null, row) => (
        <span style={{ color: value ? undefined : brandVars.danger }}>
          {value ?? '(空)'}
          {row.max_length && value && value.length > row.max_length * 0.9 && (
            <span style={{ color: brandVars.sand, fontSize: fontScale.meta, marginLeft: 6 }}>
              {value.length}/{row.max_length}
            </span>
          )}
        </span>
      ),
    },
    {
      title: '来源',
      dataIndex: 'source_label',
      width: 150,
      render: (label: string, row) => (
        <Tooltip title={row.source ?? '—'}>
          <Tag>{label}</Tag>
        </Tooltip>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (status: DraftField['status'], row) => {
        const meta = FIELD_STATUS_TAG[status]
        const tag = <Tag color={meta.color}>{meta.text}</Tag>
        if (!row.problems.length) return tag
        return (
          <Tooltip title={row.problems.map((p) => p.message).join(';')}>{tag}</Tooltip>
        )
      },
    },
  ]
  return (
    <>
      <div className="section-label" style={{ marginTop: 4 }}>
        {caption}
      </div>
      <Table<DraftField>
        rowKey="key"
        size="small"
        bordered
        pagination={false}
        columns={columns}
        dataSource={fields}
        rowClassName={(row) => (row.status === 'ERROR' ? 'attr-conflict-row' : '')}
      />
    </>
  )
}

/** 过期详情(FE-234 / BE-205)。 */
function StaleBlock({
  changes,
  onRebuild,
  pending,
}: {
  changes: StaleChange[]
  onRebuild: () => void
  pending: boolean
}) {
  return (
    <Alert
      type="error"
      showIcon
      message="这份草稿已过期,禁止导出"
      description={
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          {changes.length ? (
            changes.map((change, index) => (
              <div key={index} style={{ paddingLeft: 8, borderLeft: `3px solid ${brandVars.danger}` }}>
                <div>
                  <BrandTag tone="warning">{STALE_COMPONENT_LABEL[change.component] ?? change.component}</BrandTag>
                  {change.message}
                </div>
                {change.fields.length > 0 && (
                  <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>
                    变化字段:<span className="mono">{change.fields.join('、')}</span>
                  </div>
                )}
                {(change.old_value || change.new_value) && (
                  <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>
                    <span className="mono">{change.old_value ?? '—'}</span> →{' '}
                    <span className="mono">{change.new_value ?? '—'}</span>
                  </div>
                )}
                <div style={{ fontSize: fontScale.body }}>需要执行:{change.action}</div>
              </div>
            ))
          ) : (
            <div>指纹已变但没能定位到具体组件。请把商品 SKU 附给开发,这属于可解释性缺陷。</div>
          )}
          <Button size="small" type="primary" icon={<ReloadOutlined />} loading={pending} onClick={onRebuild}>
            重新生成草稿
          </Button>
        </Space>
      }
    />
  )
}

export default function DraftTab({
  productId,
  draft,
  step,
  onJump,
}: {
  productId: string
  draft: ListingDraft | null
  step: FlowStepResult | undefined
  onJump: (tab: WorkbenchTab) => void
}) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [manual, setManual] = useState<Record<string, string>>({})

  // 草稿重建后手填值以服务端为准 —— 本地留着旧输入会让人以为没保存成功
  useEffect(() => {
    setManual({})
  }, [draft?.id, draft?.updated_at])

  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['workbench-flow', productId] })
    queryClient.invalidateQueries({ queryKey: ['workbench'] })
  }, [queryClient, productId])

  /**
   * 生成草稿是写请求(BLOCK-05)。它会派生一版新草稿并改变导出闸的判定 ——
   * 超时后再点一次会再生成一版,而"当前草稿是哪一版"正是导出前唯一要看准的事。
   */
  const onWriteError = useWriteError(refresh)

  const build = useMutation({
    mutationFn: (payload?: Record<string, string>) => workbenchApi.buildDraft(productId, payload),
    onSuccess: (row) => {
      const errors = row.validation_errors.length
      if (errors) message.warning(`草稿已生成,但有 ${errors} 个字段不合格,先修完再导出`)
      else message.success('草稿校验通过,可以导出')
      setManual({})
      refresh()
    },
    onError: onWriteError,
  })

  if (!draft) {
    return (
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        {step && step.issues.length > 0 && (
          <Card size="small" title="这一步的问题">
            <IssueList issues={step.issues} onJump={() => onJump('overview')} />
          </Card>
        )}
        <Card size="small">
          <Empty description="还没有上架草稿。草稿只能基于已批准的图片集和已批准的文案生成。">
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              loading={build.isPending}
              disabled={step?.state === 'BLOCKED'}
              onClick={() => build.mutate(undefined)}
            >
              生成上架草稿
            </Button>
            {step?.state === 'BLOCKED' && (
              <div style={{ color: brandVars.slate, marginTop: 8 }}>
                上游还没就绪 —— 先把图片集和文案批准掉。
              </div>
            )}
          </Empty>
        </Card>
      </Space>
    )
  }

  const preview = draft.preview
  const status = DRAFT_STATUS_LABEL[draft.status]
  const stale = draft.stale?.stale ?? draft.status === 'STALE'

  /** 手填字段:预览里 source 以 manual. 开头的那些(见文件头注释) */
  const manualFields: DraftField[] = preview
    ? [...preview.header, ...(preview.rows[0]?.fields ?? [])].filter((f) =>
        (f.source ?? '').startsWith('manual.'),
      )
    : []
  const manualKey = (field: DraftField) => (field.source ?? '').slice('manual.'.length)
  const manualDirty = Object.keys(manual).length > 0

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <UnsavedGuard dirty={manualDirty} what="草稿手填字段" />
      {stale && (
        <StaleBlock
          changes={draft.stale?.changes ?? []}
          pending={build.isPending}
          onRebuild={() => build.mutate(undefined)}
        />
      )}

      {draft.validation_errors.length > 0 && (
        <Alert
          type="error"
          showIcon
          message={`${draft.validation_errors.length} 个字段不合格,阻断正式导出`}
          description={
            <ul style={{ paddingLeft: 18, margin: 0 }}>
              {draft.validation_errors.map((v, i) => (
                <li key={i}>
                  <span className="mono">{v.field_key}</span>
                  {v.row_index !== null && `(第 ${v.row_index + 1} 行)`}:{v.message}
                </li>
              ))}
            </ul>
          }
        />
      )}

      {manualFields.length > 0 && (
        <Card
          size="small"
          title="手填字段"
          extra={
            <Button
              size="small"
              type={manualDirty ? 'primary' : 'default'}
              disabled={!manualDirty}
              loading={build.isPending}
              onClick={() => build.mutate(manual)}
            >
              保存并重新生成
            </Button>
          }
        >
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 10 }}
            message="价格、币种、库存一律人工填写,不允许模型生成"
            description="模型给出的价格会真的向消费者收款,这是整条链路里唯一不可挽回的错误。不填的字段沿用上一版。"
          />
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 10 }}>
            {manualFields.map((field) => {
              const key = manualKey(field)
              return (
                <div key={field.key}>
                  <div style={{ fontSize: fontScale.meta, color: brandVars.slate, marginBottom: 2 }}>
                    {field.label}
                    {field.required && <span style={{ color: brandVars.danger }}> *</span>}
                    <span className="mono" style={{ marginLeft: 6, color: brandVars.textMuted }}>
                      {key}
                    </span>
                  </div>
                  <Input
                    size="small"
                    status={field.status === 'ERROR' && !(key in manual) ? 'error' : undefined}
                    value={manual[key] ?? field.value ?? ''}
                    placeholder={field.status === 'ERROR' ? '必填,当前为空' : ''}
                    onChange={(e) => setManual((prev) => ({ ...prev, [key]: e.target.value }))}
                  />
                </div>
              )
            })}
          </div>
        </Card>
      )}

      <Card
        size="small"
        title={
          <Space size={8}>
            <span>字段预览</span>
            <Tag color={status?.color}>{status?.text ?? draft.status}</Tag>
            {draft.spec_version && <Tag>模板 {draft.spec_version}</Tag>}
            {draft.fingerprint && (
              <Tooltip title="上游数据指纹。它一变,草稿就过期">
                <span className="mono" style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
                  {draft.fingerprint.slice(0, 12)}
                </span>
              </Tooltip>
            )}
          </Space>
        }
        extra={
          <Space>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={build.isPending}
              onClick={() => build.mutate(undefined)}
            >
              重新生成
            </Button>
            <Button size="small" type="primary" onClick={() => onJump('export')}>
              去导出
            </Button>
          </Space>
        }
      >
        {preview ? (
          <>
            <FieldTable fields={preview.header} caption="SPU 级字段" />
            {preview.rows.map((row) => (
              <FieldTable
                key={row.row_index}
                fields={row.fields}
                caption={`变体 ${row.row_index + 1}${row.sku ? ` · ${row.sku}` : ''}`}
              />
            ))}
            <div style={{ color: brandVars.slate, fontSize: fontScale.body, marginTop: 8 }}>
              导出文件与这张表读同一份映射结果,不重新计算 —— 这是「导出一致率 100%」的实现方式。
            </div>
          </>
        ) : (
          <Empty description="这次返回没带预览。点「重新生成」再看一次。" />
        )}
      </Card>
    </Space>
  )
}
