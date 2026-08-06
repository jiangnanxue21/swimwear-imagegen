import { Link } from 'react-router-dom'
import {
  Alert, Button, Card, Col, Empty, Progress, Row, Skeleton, Space, Statistic, Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { dashboardApi } from '../api/exports'
import { readError } from '../api/client'
import GradeTag from '../components/GradeTag'
import { GRADE_LABEL, type DashboardSummary, type Grade } from '../api/types'
import { brandVars, fontScale } from '../theme'
import { formatDateTime } from '../utils/datetime'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import PageHeader from '../components/PageHeader'

type Failure = DashboardSummary['recent_failures'][number]

const GRADE_ORDER: Grade[] = ['A', 'B', 'C', 'D']

/** 分档分布条。比饼图有用:运营真正关心的是"A 占比够不够高",一眼要能看出来。 */
function GradeBar({ grades }: { grades: Record<Grade, number> }) {
  const total = GRADE_ORDER.reduce((sum, g) => sum + (grades[g] ?? 0), 0)
  if (total === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有评分记录" />
  }
  const colors: Record<Grade, string> = {
    A: brandVars.success, B: brandVars.marine, C: brandVars.sand, D: brandVars.warning,
  }
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
    <PageHeader title="指标仪表盘" subtitle="系统跑得好不好:分档分布、Provider 调用、出图覆盖率" />
      <div style={{ display: 'flex', height: 18, borderRadius: 3, overflow: 'hidden' }}>
        {GRADE_ORDER.map((grade) => {
          const count = grades[grade] ?? 0
          if (count === 0) return null
          return (
            <Tooltip key={grade} title={`${GRADE_LABEL[grade].text} · ${count} 张`}>
              <div style={{ width: `${(count / total) * 100}%`, background: colors[grade] }} />
            </Tooltip>
          )
        })}
      </div>
      <Space size={6} wrap>
        {GRADE_ORDER.map((grade) => (
          <span key={grade}>
            <GradeTag grade={grade} compact />
            <span className="mono" style={{ color: brandVars.slate }}>
              {grades[grade] ?? 0} · {Math.round(((grades[grade] ?? 0) / total) * 100)}%
            </span>
          </span>
        ))}
      </Space>
    </Space>
  )
}

function CountRow({ data, empty }: { data: Record<string, number>; empty: string }) {
  const entries = Object.entries(data).filter(([, v]) => v > 0)
  if (entries.length === 0) {
    return <span style={{ color: brandVars.textMuted, fontSize: fontScale.body }}>{empty}</span>
  }
  return (
    <Space size={4} wrap>
      {entries.sort((a, b) => b[1] - a[1]).map(([key, value]) => (
        <Tag key={key}>
          {key} <span className="mono">{value}</span>
        </Tag>
      ))}
    </Space>
  )
}

export default function DashboardPage() {
  useDocumentTitle('指标仪表盘')
  const query = useQuery({
    queryKey: ['dashboard'],
    queryFn: dashboardApi.summary,
    refetchInterval: 30_000,
  })

  if (query.isLoading) return <Skeleton active paragraph={{ rows: 8 }} />
  if (query.isError) {
    return <Alert type="error" showIcon message="读不到仪表盘数据" description={readError(query.error)} />
  }

  const data = query.data!
  const outputCoverage =
    data.products.total > 0
      ? Math.round((data.outputs.products_with_outputs / data.products.total) * 100)
      : 0

  const failureColumns: ColumnsType<Failure> = [
    {
      title: '任务',
      dataIndex: 'id',
      width: 100,
      render: (id: string) => <Link to={`/tasks/${id}`} className="mono">{id.slice(0, 8)}</Link>,
    },
    { title: 'Provider', dataIndex: 'provider', width: 90 },
    {
      title: '错误码',
      dataIndex: 'error_code',
      width: 180,
      render: (code: string | null) => (code ? <Tag color="error" className="mono">{code}</Tag> : '—'),
    },
    {
      title: '说明',
      dataIndex: 'error_message',
      ellipsis: true,
      render: (msg: string | null) => msg ?? '—',
    },
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 150,
      render: (v: string | null) => formatDateTime(v),
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space>
        <Button size="small" icon={<ReloadOutlined />} onClick={() => query.refetch()}>刷新</Button>
        <span style={{ fontSize: fontScale.meta, color: brandVars.slate }}>每 30 秒自动刷新</span>
      </Space>

      <Row gutter={[12, 12]}>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="商品总数" value={data.products.total} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="生成任务" value={data.tasks.total} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title="自动通过"
              value={data.tasks.auto_approved}
              valueStyle={{ color: brandVars.success }}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title="自动淘汰"
              value={data.tasks.auto_rejected}
              valueStyle={{ color: brandVars.warning }}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={[12, 12]}>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title={<Link to="/reviews">待人工审核</Link>}
              value={data.reviews.pending}
              valueStyle={{ color: data.reviews.pending > 0 ? brandVars.sand : undefined }}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Tooltip title="只统计已跑完的任务;还在跑的混进来会让这个数一直很漂亮">
              <Statistic
                title="平均生成轮次"
                value={data.tasks.average_rounds ?? '—'}
                precision={data.tasks.average_rounds ? 2 : undefined}
              />
            </Tooltip>
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic title="Provider 调用" value={data.providers.total_calls} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title="失败任务"
              value={data.tasks.failed}
              valueStyle={{ color: data.tasks.failed > 0 ? brandVars.warning : undefined }}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={12}>
          <Card size="small" title="候选图分档分布" style={{ height: '100%' }}>
            <GradeBar grades={data.grades} />
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" title="网站成品图" style={{ height: '100%' }}>
            <Space direction="vertical" size={10} style={{ width: '100%' }}>
              <Space size={24}>
                <Statistic title="成品图总数" value={data.outputs.total} />
                <Statistic
                  title="已出图商品"
                  value={data.outputs.products_with_outputs}
                  suffix={`/ ${data.products.total}`}
                />
              </Space>
              <div>
                <div className="section-label">出图覆盖率</div>
                <Progress
                  percent={outputCoverage}
                  strokeColor={brandVars.marine}
                  size="small"
                />
              </div>
              <div>
                <div className="section-label">按用途</div>
                <CountRow data={data.outputs.by_purpose} empty="还没有成品图" />
              </div>
            </Space>
          </Card>
        </Col>
      </Row>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={12}>
          <Card size="small" title="Provider 调用次数" style={{ height: '100%' }}>
            <CountRow data={data.providers.calls_by_provider} empty="还没有调用记录" />
            {data.providers.failed_attempts > 0 && (
              <div style={{ marginTop: 10, fontSize: fontScale.body, color: brandVars.slate }}>
                共 {data.providers.attempts} 次尝试,其中{' '}
                <span style={{ color: brandVars.warning }}>{data.providers.failed_attempts}</span> 次失败
              </div>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" title="任务状态分布" style={{ height: '100%' }}>
            <CountRow data={data.tasks.by_status} empty="还没有任务" />
          </Card>
        </Col>
      </Row>

      <Card size="small" title={`最近失败任务 · ${data.recent_failures.length} 条`}>
        <Table<Failure>
          rowKey="id"
          size="small"
          bordered
          pagination={false}
          columns={failureColumns}
          dataSource={data.recent_failures}
          locale={{
            emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有失败任务" />,
          }}
        />
      </Card>
    </Space>
  )
}
