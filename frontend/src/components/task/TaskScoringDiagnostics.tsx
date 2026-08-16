import { Alert, Card, Empty, Space, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  EVALUATION_OUTCOME_LABEL, type EvaluationAttempt, type TaskStatus,
} from '../../api/types'
import { brandVars, fontScale } from '../../theme'
import BrandTag from '../BrandTag'
import ErrorNotice from '../ErrorNotice'

interface ScoringStatusProps {
  taskStatus: TaskStatus
  evaluationCount: number
  attemptCount: number
  evaluationsLoading: boolean
  attemptsLoading: boolean
  evaluationsError: unknown | null
  attemptsError: unknown | null
  evaluationsFetching: boolean
  attemptsFetching: boolean
  onRetryEvaluations: () => void
  onRetryAttempts: () => void
}

/** 任务详情里的评分总览;成功评分和失败调用刻意分成两份事实。 */
export function ScoringStatusCard(props: ScoringStatusProps) {
  const empty = !props.evaluationsLoading && !props.attemptsLoading
    && props.evaluationCount === 0 && props.attemptCount === 0
    && !props.evaluationsError && !props.attemptsError

  return (
    <Card
      size="small"
      title={`大模型评分 · 成功 ${props.evaluationCount} 条 / 调用 ${props.attemptCount} 次`}
    >
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <span>图片下载完成后评分会自动执行;成功分数与失败调用分开留痕,下面两类记录都能排查。</span>
        {Boolean(props.evaluationsError) && (
          <ErrorNotice
            title="拉不到成功评分记录"
            error={props.evaluationsError}
            onRetry={props.onRetryEvaluations}
            retrying={props.evaluationsFetching}
          />
        )}
        {Boolean(props.attemptsError) && (
          <ErrorNotice
            title="拉不到评分调用记录"
            error={props.attemptsError}
            onRetry={props.onRetryAttempts}
            retrying={props.attemptsFetching}
          />
        )}
        {empty && (
          <Alert
            type={props.taskStatus === 'SCORING' ? 'info' : 'warning'}
            showIcon
            message={props.taskStatus === 'SCORING' ? '评分正在执行,暂时还没有落库记录' : '这个任务没有评分记录'}
            description={props.taskStatus === 'SCORING'
              ? '本页会自动刷新。'
              : '系统既没有返回成功评分,也没有返回评分调用留痕;请结合任务状态与后台执行进程的日志,判断是不是还没走到评分这一步。'}
          />
        )}
      </Space>
    </Card>
  )
}

const columns: ColumnsType<EvaluationAttempt> = [
  { title: '轮', dataIndex: 'round_number', width: 50, align: 'center' },
  {
    title: '候选图', dataIndex: 'candidate_id', width: 100,
    render: (value: string | null) => <span className="mono">{value ? value.slice(0, 8) : '整轮'}</span>,
  },
  {
    title: '评分器 / 模型', key: 'evaluator',
    render: (_, row) => (
      <Space direction="vertical" size={0}>
        <Space size={4} wrap>
          <span>{row.evaluator || '—'}</span>
          {row.diagnostic && <BrandTag tone="accent">独立测试</BrandTag>}
        </Space>
        <span className="mono" style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
          {row.model_name || '—'}
        </span>
      </Space>
    ),
  },
  {
    title: '结果', dataIndex: 'outcome', width: 190,
    render: (outcome: EvaluationAttempt['outcome'], row) => (
      <Space size={4} wrap>
        <Tag color={outcome === 'SUCCEEDED' ? 'success' : outcome === 'ROUND_RETRY_SCHEDULED' ? 'warning' : 'error'}>
          {EVALUATION_OUTCOME_LABEL[outcome]}
        </Tag>
        <span className="mono" style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>{outcome}</span>
        {row.error_code && (
          <Tooltip title={row.error_message}>
            <Tag color="error" className="mono">{row.error_code}</Tag>
          </Tooltip>
        )}
      </Space>
    ),
  },
  {
    title: '耗时 / Token 用量', key: 'usage', width: 130,
    render: (_, row) => (
      <Space direction="vertical" size={0}>
        <span>{row.duration_ms === null ? '—' : `${(row.duration_ms / 1000).toFixed(1)}s`}</span>
        <span style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
          {row.total_tokens === null ? 'Token 未记录' : `${row.total_tokens} Token`}
        </span>
      </Space>
    ),
  },
  {
    title: '厂商响应', key: 'response',
    render: (_, row) => (
      <Space direction="vertical" size={0}>
        <span>{row.http_status === null ? 'HTTP —' : `HTTP ${row.http_status}`}</span>
        <span className="mono" style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
          {row.response_id || '无响应编号'}
        </span>
      </Space>
    ),
  },
]

interface AttemptCardProps {
  attempts: EvaluationAttempt[]
  loading: boolean
  fetching: boolean
  error: unknown | null
  onRetry: () => void
}

export function EvaluationAttemptCard({ attempts, loading, fetching, error, onRetry }: AttemptCardProps) {
  return (
    <Card size="small" title={`评分调用记录 · ${attempts.length} 次`}>
      {error ? (
        <ErrorNotice title="拉不到评分调用记录" error={error} onRetry={onRetry} retrying={fetching} />
      ) : (
        <Table<EvaluationAttempt>
          rowKey="id"
          size="small"
          bordered
          pagination={false}
          loading={loading}
          columns={columns}
          dataSource={attempts}
          locale={{
            emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有评分调用记录" />,
          }}
        />
      )}
    </Card>
  )
}
