/**
 * 向导左侧的七步轨道(阶段 6 批次 6-4 / AC-01)。
 *
 * ## 前端一个判断都不做
 *
 * 「这一步能不能点」「点不了是谁挡着」全部读后端的 `is_open` /
 * `gate_reason`(硬规则 4)。自己按 `state !== 'BLOCKED'` 推的那一版
 * 会把**素材步**也判成"还没轮到" —— 而素材步的 BLOCKED 是"这一步有活
 * 要干"(`flow.StepState` 的文档写死的那条例外)。后果是一件刚建档的商品
 * 打开向导七步全灰,而运营要做的第一件事恰恰在那个灰格子里。
 *
 * ## 关着的步骤仍然可见,只是点不动
 *
 * 藏起来的话,运营看不到"这条路一共有七步、我在第三步" —— 而那正是
 * 向导相对于八个标签页的全部增量。
 */
import { Badge, Space, Tag, Tooltip } from 'antd'
import { CheckCircleFilled, LockOutlined } from '@ant-design/icons'
import type { WizardStep } from '../../api/workbench'
import { StepStateTag } from '../workbench/FlowBits'
import { brandVars, fontScale, radius } from '../../theme'

export default function WizardRail({
  steps,
  activeStep,
  onPick,
}: {
  steps: WizardStep[]
  activeStep: string
  onPick: (step: WizardStep) => void
}) {
  return (
    <Space direction="vertical" size={6} style={{ width: '100%' }}>
      {steps.map((step, index) => {
        const active = step.step === activeStep
        return (
          <div
            key={step.step}
            data-testid={`wizard-step-${step.step}`}
            data-open={step.is_open ? '1' : '0'}
            data-active={active ? '1' : '0'}
            onClick={() => step.is_open && onPick(step)}
            style={{
              padding: '8px 10px',
              borderRadius: radius.md,
              cursor: step.is_open ? 'pointer' : 'not-allowed',
              // 关着的步骤压暗而不是隐藏:运营要看得见这条路一共几步
              opacity: step.is_open ? 1 : 0.55,
              background: active ? brandVars.surfaceAlt : 'transparent',
              boxShadow: active ? `inset 2px 0 0 ${brandVars.marine}` : undefined,
            }}
          >
            <Space size={8} align="start">
              <span
                className="mono"
                style={{ color: brandVars.slate, fontSize: fontScale.meta }}
              >
                {index + 1}
              </span>
              <div>
                <Space size={6} wrap>
                  <strong>{step.label}</strong>
                  <StepStateTag
                    step={step.step}
                    state={step.state}
                    summary={step.intro}
                  />
                  {step.is_current && <Tag color="processing">当前</Tag>}
                  {step.state === 'DONE' && (
                    <CheckCircleFilled style={{ color: brandVars.success }} />
                  )}
                  {!step.is_open && (
                    /* 挡路理由由后端拼。前端拼一句"等上一步"的话,
                       它与服务端 409 里那句会分叉 —— 而 AC-05 的最后一句
                       是「判定层与前端读同一个结果」 */
                    <Tooltip title={step.gate_reason}>
                      <LockOutlined style={{ color: brandVars.slate }} />
                    </Tooltip>
                  )}
                  {/* 颜色维:**有几个颜色在这一步上还没完** 与
                      「这一步没有颜色维」是两回事,后者不显示任何徽标 */}
                  {step.has_color_axis && step.pending_colors.length > 0 && (
                    <Badge
                      count={step.pending_colors.length}
                      color={brandVars.sand}
                      title={`还差:${step.pending_colors.join('、')}`}
                    />
                  )}
                </Space>
                <div
                  style={{
                    color: brandVars.slate,
                    fontSize: fontScale.meta,
                    marginTop: 2,
                  }}
                >
                  {step.is_open ? step.intro : step.gate_reason}
                </div>
              </div>
            </Space>
          </div>
        )
      })}
    </Space>
  )
}
