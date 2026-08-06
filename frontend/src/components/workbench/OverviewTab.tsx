/**
 * 总览标签:五个步骤各自的状态、一句话摘要与全部问题。
 *
 * 顶部进度区只给数字,这一页给理由 —— 徽标点过来要能看到**具体是哪几条**,
 * 而不是又一个「3 个阻断」。提醒级问题只在这里出现(§3.3.2:不计入徽标)。
 */
import { Card, Empty, Space, Tag } from 'antd'
import {
  ISSUE_LEVEL_LABEL,
  STEP_TAB,
  type ProductFlow,
  type WorkbenchTab,
} from '../../api/workbench'
import { IssueList, StepStateTag } from './FlowBits'
import { brandVars } from '../../theme'

export default function OverviewTab({
  flow,
  onJump,
}: {
  flow: ProductFlow
  onJump: (tab: WorkbenchTab) => void
}) {
  const reminders = flow.steps.flatMap((s) => s.issues.filter((i) => i.level === 'REMINDER'))

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      {flow.steps.map((step) => {
        const actionable = step.issues.filter((i) => i.level !== 'REMINDER')
        return (
          <Card
            key={step.step}
            size="small"
            title={
              <Space size={8}>
                <span>{step.label}</span>
                <StepStateTag step={step.step} state={step.state} summary={step.summary} />
                <span className="mono" style={{ color: brandVars.slate, fontWeight: 400 }}>
                  {step.summary}
                </span>
              </Space>
            }
            extra={
              <a onClick={() => onJump(STEP_TAB[step.step])}>
                去{step.label}标签页
              </a>
            }
          >
            <IssueList
              issues={actionable}
              onJump={(target) => onJump(STEP_TAB[target])}
              empty={
                step.state === 'DONE'
                  ? '这一步已完成。'
                  : step.state === 'BLOCKED'
                    ? '等上游做完,这一步本身没有待办。'
                    : '没有待处理的问题。'
              }
            />
          </Card>
        )
      })}

      <Card
        size="small"
        title={
          <Space size={8}>
            <span>提醒</span>
            <Tag color={ISSUE_LEVEL_LABEL.REMINDER.color}>{reminders.length}</Tag>
          </Space>
        }
      >
        {reminders.length ? (
          <IssueList issues={reminders} onJump={(target) => onJump(STEP_TAB[target])} />
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="没有提醒。提醒不阻断导出,也不计入顶部徽标。"
          />
        )}
      </Card>
    </Space>
  )
}
