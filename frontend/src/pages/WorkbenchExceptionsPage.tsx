/**
 * 简化异常列表(FE-303)。
 *
 * 验收结果是「可跳转具体商品和字段」。所以这一页的每一行末尾都有一个
 * 「去处理」,而它跳的不是商品详情首页,是**出问题的那个标签页**,
 * 带上 `ref`(字段名 / 校验码 / 素材角色)——§6.3.1 第二条要的就是这一键。
 *
 * ## 为什么先分组再列明细
 *
 * 50 件商品平铺出来是 120 行。运营在那张表上做的第一件事是用眼睛分组:
 * "十几件都是缺背面图"。所以后端先按「步骤 -> 问题码」两层收好,
 * 这一页只负责展开:第一层五个分类始终在同一位置(空分类也占位,
 * 否则清空一类会让其余几类跳位),第二层按影响面降序。
 *
 * ## 两个来源不合并计数
 *
 * `flow` 是"现在的状态哪里不对",`batch` 是"执行时发生了什么"。合并会让
 * 同一件商品被数两次。界面上用一个 Tag 区分来源。
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Card, Collapse, Empty, Input, Space, Switch, Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import ErrorNotice from '../components/ErrorNotice'
import { batchApi, type ExceptionEntry, type ExceptionSection } from '../api/batch'
import { FLOW_STEP_LABEL, ISSUE_LEVEL_LABEL, STEP_TAB } from '../api/workbench'
import { brandVars, fontScale } from '../theme'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

const SOURCE_LABEL: Record<string, string> = {
  flow: '状态判定',
  batch: '批次执行',
}

export default function WorkbenchExceptionsPage() {
  useDocumentTitle('异常与驳回')
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  /** 输入框里的字。与已生效的 `search` 分开:敲字不该每个字符打一次接口,
      但两者必须能被同一个动作一起清掉(走查 P0-3:非受控输入框会说谎) */
  const [draft, setDraft] = useState('')
  const [includeBatch, setIncludeBatch] = useState(true)

  const query = useQuery({
    queryKey: ['exceptions', { search, includeBatch }],
    queryFn: () =>
      batchApi.exceptions({
        search: search || undefined,
        include_batch: includeBatch,
      }),
  })

  const entryColumns: ColumnsType<ExceptionEntry> = [
    {
      title: '商品',
      key: 'sku',
      width: 200,
      render: (_, row) => (
        <div>
          <a className="mono" onClick={() => navigate(`/workbench/${row.product_id}`)}>
            {row.sku}
          </a>
          <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>{row.spu}</div>
        </div>
      ),
    },
    {
      title: '级别',
      key: 'level',
      width: 80,
      render: (_, row) => {
        const meta = ISSUE_LEVEL_LABEL[row.level]
        return <Tag color={meta?.color}>{meta?.text ?? row.level}</Tag>
      },
    },
    {
      title: '来源',
      key: 'source',
      width: 90,
      render: (_, row) => (
        <Tooltip
          title={
            row.source === 'batch'
              ? `批次 ${row.batch_id?.slice(0, 8) ?? ''} 执行时发生`
              : '按当前状态判定出的问题'
          }
        >
          <Tag>{SOURCE_LABEL[row.source] ?? row.source}</Tag>
        </Tooltip>
      ),
    },
    {
      title: '问题',
      key: 'message',
      render: (_, row) => (
        <div>
          <div style={{ fontSize: fontScale.body }}>{row.message}</div>
          {row.hint && (
            <div style={{ color: brandVars.slate, fontSize: fontScale.meta }}>下一步:{row.hint}</div>
          )}
        </div>
      ),
    },
    {
      title: '定位',
      key: 'ref',
      width: 140,
      render: (_, row) =>
        row.ref ? <span className="mono">{row.ref}</span> : <span style={{ color: brandVars.textFaint }}>—</span>,
    },
    {
      title: '',
      key: 'jump',
      width: 92,
      render: (_, row) => (
        <Button
          size="small"
          type="link"
          onClick={() => navigate(`/workbench/${row.product_id}?tab=${STEP_TAB[row.step]}`)}
        >
          去处理
        </Button>
      ),
    },
  ]

  const sections = query.data?.sections ?? []
  const summary = query.data?.summary ?? {}

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader title="异常与驳回" subtitle="按「步骤 → 问题码」分组:十几件都缺背面图,一眼可见" />

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="搜索 SKU / SPU / 名称"
            style={{ width: 240 }}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onSearch={setSearch}
          />
          <Space size={6}>
            <Switch checked={includeBatch} onChange={setIncludeBatch} size="small" />
            <Tooltip title="批次执行失败与状态判定问题是两类信息,合并计数会让同一件商品被数两次,所以来源在每一行上标出">
              <span style={{ fontSize: fontScale.body }}>并入批次执行失败</span>
            </Tooltip>
          </Space>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => query.refetch()}
            loading={query.isFetching}
          >
            刷新
          </Button>
        </Space>
      </Card>

      {query.isError && (
        <ErrorNotice
          title="拉不到异常列表"
          error={query.error}
          onRetry={() => query.refetch()}
        />
      )}

      <Space size={8} wrap>
        <Tag style={{ fontSize: fontScale.body, padding: '2px 10px' }}>
          合计 {summary.total ?? 0}
        </Tag>
        <Tag color="error" style={{ fontSize: fontScale.body, padding: '2px 10px' }}>
          阻断 {summary.blocking ?? 0}
        </Tag>
        {sections.map((s) => (
          <Tag key={s.step} color={s.blocking_count ? 'error' : 'default'}>
            {s.label} {s.count}
          </Tag>
        ))}
      </Space>

      {!query.isError && summary.total === 0 && !query.isFetching && (
        <Empty description="当前没有阻断或待确认的异常。" />
      )}

      {sections
        .filter((s: ExceptionSection) => s.count > 0)
        .map((section: ExceptionSection) => (
          <Card
            key={section.step}
            size="small"
            title={
              <Space>
                <span>{FLOW_STEP_LABEL[section.step] ?? section.label}</span>
                <Tag color={section.blocking_count ? 'error' : 'default'}>
                  {section.count} 条 / 阻断 {section.blocking_count}
                </Tag>
              </Space>
            }
            styles={{ body: { padding: 0 } }}
          >
            <Collapse
              ghost
              items={section.groups.map((group) => ({
                key: group.code,
                label: (
                  <Space>
                    <Tag color={ISSUE_LEVEL_LABEL[group.level]?.color}>
                      {group.count} 件
                    </Tag>
                    <span>{group.message}</span>
                    <span className="mono" style={{ color: brandVars.slate, fontSize: fontScale.meta }}>
                      {group.code}
                    </span>
                  </Space>
                ),
                children: (
                  <Table<ExceptionEntry>
                    rowKey={(row) => `${row.product_id}-${row.code}-${row.source}`}
                    size="small"
                    bordered
                    columns={entryColumns}
                    dataSource={group.entries}
                    pagination={group.entries.length > 10 ? { pageSize: 10 } : false}
                  />
                ),
              }))}
            />
          </Card>
        ))}
    </Space>
  )
}
