/**
 * 上游变化影响提示(阶段 6 批次 6-5 / AC-17)。
 *
 * AC-17 的原话是「在**修改之前**展示这次修改将使哪些对象失效
 * (对象 / 字段 / 需要执行的动作)」。所以这一段是一个**主动查询**:
 * 运营选"我要改什么",界面回答"会波及什么、你之后要做什么"。
 *
 * ## 三样东西都由后端给
 *
 *     哪些对象会失效   `stale_matrix` 那张 32 格的封闭表
 *     具体是哪一个     当前这件商品的图片集版本 / 文案版本 / 草稿
 *     之后要做什么     `TARGET_ACTION`(取值来自 `NextActionCode`)
 *
 * 前端一条都不推。推的表现是提示说"不影响",而系统照样把草稿打成 STALE
 * —— 而运营正是照着这条提示决定要不要改的。
 *
 * ## 「不受影响」的行也显示
 *
 * 只列受影响的,清单里没有文案就有两种可能:不受影响,或者这张表漏了它。
 * 两者在界面上长得一样,而后者本仓真的发生过(矩阵的 FACTS 那一列是补的)。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, Empty, Select, Space, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import ErrorNotice from '../ErrorNotice'
import { workbenchApi, type ImpactRow } from '../../api/workbench'
import { brandVars, fontScale } from '../../theme'

const COLUMNS: ColumnsType<ImpactRow> = [
  { title: '对象', dataIndex: 'target_label', width: 110 },
  {
    title: '影响',
    dataIndex: 'effect',
    width: 130,
    render: (effect: string, row) =>
      row.affected ? <Tag color="warning">{effect}</Tag> : <Tag>{effect}</Tag>,
  },
  {
    title: '之后要做什么',
    dataIndex: 'action_label',
    width: 150,
    render: (label: string | null) => label ?? '—',
  },
  {
    title: '负责这一格的机制',
    dataIndex: 'mechanism',
    render: (mechanism: string, row) => (
      <span
        className="mono"
        style={{ color: brandVars.slate, fontSize: fontScale.meta }}
      >
        {/* 原样透出。提示说会过期而实际没过期时,要查的就是这个函数 */}
        {mechanism || '—'}
        {row.note && ` · ${row.note}`}
      </span>
    ),
  },
]

export default function ImpactPreview({ productId }: { productId: string }) {
  const [source, setSource] = useState<string | null>(null)

  const sources = useQuery({
    queryKey: ['workbench-change-sources'],
    queryFn: () => workbenchApi.changeSources(),
  })

  const impact = useQuery({
    queryKey: ['workbench-impact', productId, source],
    queryFn: () => workbenchApi.changeImpact(productId, source!),
    enabled: Boolean(source),
  })

  if (sources.isError) {
    return (
      <Card size="small" title="改之前先看影响">
        <ErrorNotice
          title="取不到变更源清单"
          error={sources.error}
          onRetry={() => sources.refetch()}
        />
      </Card>
    )
  }

  const objects = impact.data?.objects

  return (
    <Card size="small" title="改之前先看影响">
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <Select
          data-testid="impact-source"
          style={{ width: '100%' }}
          placeholder="我要改什么?"
          loading={sources.isLoading}
          value={source ?? undefined}
          onChange={setSource}
          options={(sources.data ?? []).map((s) => ({
            value: s.source,
            label: s.affects.length ? `${s.label} → 波及${s.affects.join('、')}` : s.label,
          }))}
        />

        {!source && (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="选一项,看它会让什么失效。这里只看不改。"
          />
        )}

        {impact.isError && (
          <ErrorNotice
            title="算不出影响"
            error={impact.error}
            onRetry={() => impact.refetch()}
          />
        )}

        {impact.data && (
          <>
            <div style={{ color: brandVars.slate, fontSize: fontScale.meta }}>
              失效时机:{impact.data.trigger}
            </div>
            <Table<ImpactRow>
              rowKey="target"
              size="small"
              bordered
              pagination={false}
              columns={COLUMNS}
              dataSource={impact.data.rows}
            />
            <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>
              {/* 具体对象。`null` = 今天还没有这个对象,**不是"不受影响"** ——
                  两者要做的事不同,而它们在一张只列对象的清单上长得一样 */}
              当前对象:
              {objects?.image_set
                ? `图片集 v${objects.image_set.version}(${objects.image_set.status})`
                : '图片集 尚未创建'}
              ·
              {objects?.copy
                ? `文案 v${objects.copy.version}(${objects.copy.status})`
                : '文案 尚未创建'}
              ·
              {objects?.draft ? `草稿(${objects.draft.status})` : '草稿 尚未创建'}
            </div>
          </>
        )}
      </Space>
    </Card>
  )
}
