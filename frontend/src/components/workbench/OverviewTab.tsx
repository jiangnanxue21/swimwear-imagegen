/**
 * 总览标签:七个步骤各自的状态、一句话摘要与全部问题,以及**颜色维**。
 *
 * 顶部进度区只给数字,这一页给理由 —— 徽标点过来要能看到**具体是哪几条**,
 * 而不是又一个「3 个阻断」。提醒级问题只在这里出现(§3.3.2:不计入徽标)。
 *
 * ## 颜色维那一段(阶段 6 / AC-15,A45-batch28)
 *
 * SPU 是多颜色的,而一个 SPU 级的百分数回答不了运营真正在问的问题:
 * 九色 SPU 显示「图片集 30%」时,是九个颜色各差一点,还是八个齐了、
 * 只有一个一张图都没有?**这两种情况要做的事完全不同**
 * (前者等生成,后者去传样品)。
 *
 * 所以这一段按颜色逐行铺开,并把"哪几个颜色拦着"单独说一遍。
 */
import { Button, Card, Empty, Space, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  COLOR_SUBSTATE_LABEL,
  FLOW_STEP_LABEL,
  ISSUE_LEVEL_LABEL,
  STEP_TAB,
  type ColorFlowRow,
  type ColorRollup,
  type ColorSubState,
  type ProductFlow,
  type WorkbenchTab,
} from '../../api/workbench'
import { IssueList, StepStateTag } from './FlowBits'
import { brandVars } from '../../theme'

/** 子态 -> 标签颜色。UNKNOWN 用灰色:它不是错,只是没查过。 */
const SUBSTATE_COLOR: Record<ColorSubState, string> = {
  UNKNOWN: 'default',
  TODO: 'processing',
  BLOCKED: 'error',
  NEEDS_CONFIRM: 'warning',
  DONE: 'success',
}

function ColorMatrix({ colors }: { colors: ColorRollup }) {
  const steps = colors.colors[0]?.steps.map((s) => s.step) ?? []
  const columns: ColumnsType<ColorFlowRow> = [
    {
      title: '颜色',
      dataIndex: 'variant_code',
      width: 140,
      render: (code: string, row) => (
        <Space size={6}>
          <span className="mono">{code}</span>
          {/* 非 ACTIVE 的颜色如实显示,但它不拦路(§6.7 / AC-18)。
              不显示的话运营会以为这个颜色不存在 */}
          {!row.is_active && <Tag>未开售</Tag>}
        </Space>
      ),
    },
    ...steps.map((step) => ({
      // 表头是**中文步骤名**。这里原来直接摆 `step`,于是颜色维表头是
      // MATERIAL / ATTRIBUTE / PLAN / IMAGE_SET / COPY 五个英文枚举值,
      // 而同一屏上方的步骤条写的是「素材 属性 方案 图片集 文案」——
      // 同一组东西,同一个页面,两套名字
      title: FLOW_STEP_LABEL[step] ?? step,
      dataIndex: step,
      render: (_: unknown, row: ColorFlowRow) => {
        const cell = row.steps.find((s) => s.step === step)
        if (!cell) return null
        return (
          <Space size={4} direction="vertical" style={{ lineHeight: 1.3 }}>
            <Tag color={SUBSTATE_COLOR[cell.state]}>
              {COLOR_SUBSTATE_LABEL[cell.state]}
            </Tag>
            {cell.detail && (
              <span style={{ color: brandVars.slate, fontSize: 12 }}>{cell.detail}</span>
            )}
          </Space>
        )
      },
    })),
  ]

  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      {/* 这一句由后端拼(`blocking_summary`)。同一句话要出现在向导、
          列表页悬浮提示与批量跳过理由三处,前端各拼一次会分叉成三种说法 */}
      {colors.blocking_summary ? (
        <div style={{ color: brandVars.danger }}>{colors.blocking_summary}</div>
      ) : (
        <div style={{ color: brandVars.slate }}>
          {colors.active_count} 个在售颜色,没有颜色拦着
        </div>
      )}
      <Table<ColorFlowRow>
        rowKey="variant_id"
        size="small"
        bordered
        pagination={false}
        columns={columns}
        dataSource={colors.colors}
        rowClassName={(row) => (row.is_blocking ? 'attr-conflict-row' : '')}
      />
    </Space>
  )
}

export default function OverviewTab({
  flow,
  colors,
  onJump,
}: {
  flow: ProductFlow
  colors: ColorRollup | null
  onJump: (tab: WorkbenchTab) => void
}) {
  const reminders = flow.steps.flatMap((s) => s.issues.filter((i) => i.level === 'REMINDER'))

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      {/* `colors === null` 的含义是**没查过**(没有 SPU 归属的存量行),
          不是"颜色都齐了" —— 所以整块不显示,而不是显示一个空表 */}
      {colors && (
        <Card size="small" title={<Space size={8}><span>颜色维</span><Tag>{colors.active_count} 个在售</Tag></Space>}>
          <ColorMatrix colors={colors} />
        </Card>
      )}

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
              // 切换同一页内的标签,不产生新地址 —— 按钮而不是无 href 的锚点
              <Button type="link" size="small" onClick={() => onJump(STEP_TAB[step.step])}>
                去{step.label}标签页
              </Button>
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
