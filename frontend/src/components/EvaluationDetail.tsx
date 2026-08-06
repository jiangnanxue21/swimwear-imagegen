import { Alert, Empty, Progress, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import GradeTag from './GradeTag'
import {
  DIMENSION_LABEL, GATE_DIMENSIONS, HARD_FAIL_LABEL, RECOMMENDED_ACTION_LABEL,
  SEVERITY_LABEL, type Evaluation, type EvaluationProblem, type ProblemSeverity,
} from '../api/types'
import { brandVars, fontScale } from '../theme'

/** A 档四条底线的默认值(需求第十二章)。规则集能改,所以允许外部覆盖。 */
const DEFAULT_GATES: Record<string, number> = {
  garment_identity: 90,
  structural_fidelity: 90,
  anatomy_realism: 85,
  website_usability: 85,
}

interface Props {
  evaluation: Evaluation | null | undefined
  /** 来自 /api/rule-sets 的 thresholds,用于画底线。缺省用需求默认值。 */
  thresholds?: Record<string, number | boolean>
}

function gateOf(dimension: string, thresholds?: Record<string, number | boolean>): number | null {
  if (!(GATE_DIMENSIONS as readonly string[]).includes(dimension)) return null
  const fromRules = thresholds?.[`a_min_${dimension}`]
  return typeof fromRules === 'number' ? fromRules : DEFAULT_GATES[dimension] ?? null
}

function barColor(score: number, gate: number | null): string {
  if (gate !== null) return score >= gate ? brandVars.success : brandVars.warning
  if (score >= 85) return brandVars.success
  if (score >= 70) return brandVars.marine
  if (score >= 55) return brandVars.sand
  return brandVars.warning
}

/**
 * 单张候选图的评分详情。
 *
 * 排版原则:先给结论(分档 + 硬错误),再给判据(维度分 + 问题),最后才是元信息。
 * 审核员的第一个问题永远是「这张为什么不能用」,不是「各维度多少分」。
 */
export default function EvaluationDetail({ evaluation, thresholds }: Props) {
  if (!evaluation) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="这张候选图没有评分记录"
        style={{ margin: '12px 0' }}
      />
    )
  }

  const quick = evaluation.depth === 'QUICK'
  const drift =
    evaluation.model_reported_overall === null
      ? null
      : Math.round((evaluation.model_reported_overall - evaluation.overall_score) * 10) / 10

  const problemColumns: ColumnsType<EvaluationProblem> = [
    {
      title: '问题',
      dataIndex: 'code',
      render: (code: string, row) => (
        <Space size={4}>
          <span className="mono">{code}</span>
          {row.hard_fail && <Tag color="error">硬错误</Tag>}
        </Space>
      ),
    },
    {
      title: '严重度',
      dataIndex: 'severity',
      width: 80,
      render: (s: ProblemSeverity) => (
        <Tag color={SEVERITY_LABEL[s]?.color}>{SEVERITY_LABEL[s]?.text ?? s}</Tag>
      ),
    },
    {
      title: '维度',
      dataIndex: 'dimension',
      width: 120,
      render: (d: string | null) => (d ? DIMENSION_LABEL[d] ?? d : '—'),
    },
    { title: '说明', dataIndex: 'message' },
  ]

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <Space size={6} wrap>
        <GradeTag grade={evaluation.grade} score={evaluation.overall_score} />
        <Tag>{RECOMMENDED_ACTION_LABEL[evaluation.recommended_action] ?? evaluation.recommended_action}</Tag>
        {quick && (
          <Tooltip title="预排序后排名靠后的候选只做快速硬错误检查,不做完整打分(需求第十三章)">
            <Tag color="default">快速检查</Tag>
          </Tooltip>
        )}
        {evaluation.rank_index !== null && <Tag>轮内排名 #{evaluation.rank_index + 1}</Tag>}
        <Typography.Text type="secondary" style={{ fontSize: fontScale.meta }}>
          {evaluation.evaluator}
          {evaluation.model_name ? ` · ${evaluation.model_name}` : ''}
          {evaluation.duration_ms !== null ? ` · ${evaluation.duration_ms}ms` : ''}
        </Typography.Text>
      </Space>

      {evaluation.hard_fail && (
        <Alert
          type="error"
          showIcon
          message="存在硬错误,不论总分多高都不能自动通过"
          description={
            <Space size={4} wrap>
              {evaluation.hard_fail_codes.map((code) => (
                <Tag key={code} color="error">
                  {HARD_FAIL_LABEL[code] ?? code}
                  <span className="mono" style={{ marginLeft: 4, opacity: 0.7 }}>{code}</span>
                </Tag>
              ))}
            </Space>
          }
        />
      )}

      {evaluation.grade_reasons.length > 0 && (
        <div>
          <div className="section-label">判据</div>
          <ul style={{ margin: 0, paddingLeft: 18, color: brandVars.slate, fontSize: fontScale.body }}>
            {evaluation.grade_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}

      {quick ? (
        <Alert
          type="info"
          showIcon
          message="这张只做了快速硬错误检查"
          description="轮内预排序把它排在后面,为压低评分成本没有做完整打分。需要完整分数可在规则集里关闭预排序。"
        />
      ) : (
        <div>
          <div className="section-label">
            维度分 · 总分 {Math.round(evaluation.overall_score)} 由后端按权重算出
            {drift !== null && drift !== 0 && (
              <Tooltip title="评分器自报总分与后端加权总分之差,用于监控大模型打分漂移">
                <span style={{ marginLeft: 8, fontWeight: 400, textTransform: 'none' }}>
                  (评分器自报偏差 {drift > 0 ? '+' : ''}{drift})
                </span>
              </Tooltip>
            )}
          </div>
          {Object.entries(evaluation.scores).map(([dimension, score]) => {
            const gate = gateOf(dimension, thresholds)
            const failed = gate !== null && score < gate
            return (
              <div
                key={dimension}
                style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}
              >
                <div style={{ width: 108, fontSize: fontScale.body, color: failed ? brandVars.warning : brandVars.slate }}>
                  {DIMENSION_LABEL[dimension] ?? dimension}
                  {gate !== null && (
                    <Tooltip title={`A 档底线 ${gate}`}>
                      <span style={{ color: brandVars.sand, marginLeft: 2 }}>*</span>
                    </Tooltip>
                  )}
                </div>
                <Progress
                  percent={Math.max(0, Math.min(100, score))}
                  size="small"
                  showInfo={false}
                  strokeColor={barColor(score, gate)}
                  style={{ flex: 1, marginBottom: 0 }}
                />
                <div className="mono" style={{ width: 30, textAlign: 'right' }}>
                  {Math.round(score)}
                </div>
              </div>
            )
          })}
          <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
            带 * 的是 A 档必须达标的四条底线,任意一条未达标都不会自动通过。
          </Typography.Text>
        </div>
      )}

      {evaluation.problems.length > 0 && (
        <div>
          <div className="section-label">问题清单</div>
          <Table<EvaluationProblem>
            rowKey="code"
            size="small"
            bordered
            pagination={false}
            columns={problemColumns}
            dataSource={evaluation.problems}
          />
        </div>
      )}

      {evaluation.uncertain_items.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message="评分器无法确认的项"
          description={evaluation.uncertain_items.join(';')}
        />
      )}
    </Space>
  )
}
