import { Tag, Tooltip } from 'antd'
import { GRADE_LABEL, type Grade } from '../api/types'

interface Props {
  grade?: Grade | string | null
  score?: number | null
  /** 只显示字母,不带说明文字。用在候选图缩略图这种空间紧张的地方。 */
  compact?: boolean
}

/**
 * 分档标签。
 *
 * 分档是整个阶段 4 的结论,出现在候选图、任务详情、审核队列三处,
 * 颜色语义必须完全一致 —— 所以只在这里定义一次。
 */
export default function GradeTag({ grade, score, compact = false }: Props) {
  if (!grade) return <Tag>未评分</Tag>
  const meta = GRADE_LABEL[grade as Grade]
  if (!meta) return <Tag>{grade}</Tag>

  const text = compact ? grade : meta.text
  return (
    <Tooltip title={meta.hint}>
      <Tag color={meta.color} style={{ marginInlineEnd: score == null ? undefined : 4 }}>
        {text}
        {score != null && ` · ${Math.round(score)}`}
      </Tag>
    </Tooltip>
  )
}
