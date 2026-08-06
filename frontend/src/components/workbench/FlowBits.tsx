/**
 * 列表页与详情页共用的小件。
 *
 * 抽出来不是为了省行数,是为了让「同一个状态在两个页面上长得一样」由代码保证。
 * 列表页的素材列和详情页进度区里的素材格必须是同一个颜色同一个词 ——
 * 运营在列表上看到「待确认」,点进去看到「需处理」,会以为自己看错了商品。
 */
import type { CSSProperties } from 'react'
import { Alert, Space, Tag, Tooltip } from 'antd'
import { useQuery } from '@tanstack/react-query'
import {
  FLOW_STEP_LABEL,
  FLOW_STEP_ORDER,
  ISSUE_LEVEL_LABEL,
  STEP_STATE_LABEL,
  workbenchApi,
  type FlowIssue,
  type FlowStep,
  type FlowStepResult,
  type StepState,
} from '../../api/workbench'
import { brandVars, fontScale } from '../../theme'
import BrandTag from '../BrandTag'
import { formatDateTime } from '../../utils/datetime'

/** 除素材步以外,BLOCKED 一律指「上游还没就绪」,不是这一步自己有问题。 */
function stateHint(step: FlowStep, state: StepState, summary: string): string {
  if (state === 'BLOCKED') {
    return step === 'MATERIAL'
      ? `缺少必备素材,整条流程从这里起步不了 · ${summary}`
      : `上游还没就绪,这一步现在做不了 · ${summary}`
  }
  if (state === 'STALE') return `做完了但上游变了,重新生成一次即可 · ${summary}`
  return summary
}

export function StepStateTag({
  step,
  state,
  summary,
}: {
  step: FlowStep
  state: StepState
  summary: string
}) {
  const meta = STEP_STATE_LABEL[state]
  // 认不出的状态退回中性灰,不猜。后端加了新枚举而前端还没跟上时,
  // 一个灰标签加原始值比一个瞎选的颜色好 —— 后者会让人以为系统对它有判断
  return (
    <Tooltip title={stateHint(step, state, summary)}>
      <BrandTag tone={meta?.tone ?? 'textMuted'}>{meta?.text ?? state}</BrandTag>
    </Tooltip>
  )
}

/**
 * 每个状态除颜色外的**第二个维度**(二次走查 A-2)。
 *
 * 改动前这五个色块只靠颜色区分 —— 而上一轮刚提完无障碍,转头就引入一个
 * 纯颜色编码的信号:红绿色盲看这五格是五个深浅差不多的灰块。
 *
 * 现在每一档在「高度 / 是否填实 / 透明度」上也不同,不看颜色也能读出形状:
 *
 *     已完成    满高实心      —— 视觉最重,一行里绿满格 = 这件走完了
 *     阻断      满高实心      —— 同样满高,靠颜色和位置区分(它一定伴随红色)
 *     已过期    满高半透      —— 做完过但失效了,「褪色」正好是这个语义
 *     待确认    满高空心      —— 空心 = 还没落定,等人填
 *     进行中    半高实心      —— 长到一半
 *     未开始    底部细条      —— 几乎不占面积,一行里前面细后面粗就是进度形状
 */
const STEP_SHAPE: Record<StepState, (color: string) => CSSProperties> = {
  DONE: (c) => ({ height: 18, background: c }),
  BLOCKED: (c) => ({ height: 18, background: c }),
  STALE: (c) => ({ height: 18, background: c, opacity: 0.5 }),
  NEEDS_CONFIRM: (c) => ({ height: 18, background: 'transparent', boxShadow: `inset 0 0 0 2px ${c}` }),
  IN_PROGRESS: (c) => ({ height: 11, background: c, alignSelf: 'flex-end' }),
  TODO: (c) => ({ height: 4, background: c, alignSelf: 'flex-end' }),
}

/**
 * 紧凑列的图例(二次走查 A-2)。
 *
 * 没有它的话,五个色块在界面上**没有任何地方**解释「左起第一格是素材」
 * 或者「沙色代表待确认」—— 运营只能逐格悬停 Tooltip 去问。
 * 而紧凑模式的全部意义是让他顺着列往下**扫**,找哪一行形状不对;
 * 要悬停就不叫扫了,那比原来五个带字的 Tag 还慢。
 *
 * 放在表格上方而不是塞进表头单元格:96px 的列宽塞不下,
 * 而且图例是「看一次就记住」的东西,不需要每行重复。
 */
export function FlowStripLegend() {
  const shown: StepState[] = ['DONE', 'NEEDS_CONFIRM', 'BLOCKED', 'STALE', 'TODO']
  return (
    <Space size={12} wrap style={{ fontSize: fontScale.meta, color: brandVars.slate }}>
      <span>
        流程列左起:{FLOW_STEP_ORDER.map((s) => FLOW_STEP_LABEL[s]).join(' · ')}
      </span>
      <Space size={8} wrap>
        {shown.map((st) => {
          const meta = STEP_STATE_LABEL[st]
          return (
            <Space key={st} size={3}>
              <span
                style={{
                  display: 'inline-block',
                  width: 8,
                  borderRadius: 2,
                  ...STEP_SHAPE[st](brandVars[meta.tone]),
                  height: 12,
                }}
              />
              {meta.text}
            </Space>
          )
        })}
      </Space>
    </Space>
  )
}

/**
 * 五步状态压成一格(走查 P0-1)。
 *
 * ## 为什么要有紧凑形态
 *
 * 工作台列表原本给五个步骤各开一列,每列 96px —— **480px 只为了显示五个 Tag**。
 * 加上其余各列,表格最小宽度 1280px;而侧栏 190 + 内容区 padding 40 = 230px
 * 是固定开销,于是浏览器视口要 ≥1510px 才不横向滚。实测:
 *
 *     1366(办公本主流)   内容区 1136   缺 144px
 *     1440(MacBook Air)  内容区 1210   缺  70px
 *
 * 而被挤出去的正是最后一列「唯一下一步」—— 这一整页存在的理由。
 * 运营的实际动作变成:看一行 → 横滚 → 点按钮 → 横滚回来 → 看下一行。
 *
 * 压成一格省下 384px,1366 上就装得下了。
 *
 * ## 为什么是色块而不是缩写文字
 *
 * 这一列的用途是**扫**,不是读:运营顺着列往下看,找的是「哪一行的形状不对」。
 * 五个色块的形状差异比五个「待/完/阻」的字形差异大得多,而且不依赖识字速度。
 * 具体是哪一步什么状态,进 Tooltip —— 需要精读的那几行本来就不多。
 *
 * 色块保留了顺序(素材→属性→图片集→文案→草稿),所以「前两格绿、后三格灰」
 * 这种进度形态直接可读,不用逐格看。
 */
export function FlowStrip({
  steps,
  onJump,
}: {
  steps: FlowStepResult[]
  onJump?: (step: FlowStep) => void
}) {
  const byStep = new Map(steps.map((s) => [s.step, s]))
  return (
    <Tooltip
      title={
        <Space direction="vertical" size={2} style={{ width: '100%' }}>
          {FLOW_STEP_ORDER.map((s) => {
            const row = byStep.get(s)
            return (
              <div key={s} style={{ fontSize: fontScale.meta, whiteSpace: 'nowrap' }}>
                {FLOW_STEP_LABEL[s]}:{row ? STEP_STATE_LABEL[row.state]?.text ?? row.state : '—'}
                {row?.summary ? ` · ${row.summary}` : ''}
              </div>
            )
          })}
        </Space>
      }
    >
      <div
        role={onJump ? 'button' : undefined}
        tabIndex={onJump ? 0 : undefined}
        aria-label={FLOW_STEP_ORDER.map((s) => {
          const row = byStep.get(s)
          return `${FLOW_STEP_LABEL[s]} ${row ? STEP_STATE_LABEL[row.state]?.text ?? row.state : '未知'}`
        }).join(',')}
        onClick={() => onJump?.(FLOW_STEP_ORDER[0])}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onJump?.(FLOW_STEP_ORDER[0])
          }
        }}
        style={{
          display: 'flex',
          gap: 3,
          // stretch 之外的对齐要生效,容器必须有确定高度 —— 否则 alignSelf: flex-end
          // 无处可 end,半高与细条两档会退化成顶对齐
          alignItems: 'stretch',
          height: 18,
          justifyContent: 'center',
          cursor: onJump ? 'pointer' : 'default',
        }}
      >
        {FLOW_STEP_ORDER.map((s) => {
          const row = byStep.get(s)
          const state = row?.state
          const tone = state ? STEP_STATE_LABEL[state]?.tone ?? 'textMuted' : 'lineSoft'
          // 颜色之外再给一个形状维度(二次走查 A-2),不看颜色也能读
          const shape = state ? STEP_SHAPE[state]?.(brandVars[tone]) : { height: 4, background: brandVars[tone], alignSelf: 'flex-end' as const }
          return (
            <span
              key={s}
              style={{
                width: 8,
                borderRadius: 2,
                // 未开始那一档底色很浅,不描边的话在白底上看不出有没有这一格,
                // 而「有几格」本身是信息
                outline: `1px solid ${brandVars.line}`,
                outlineOffset: -1,
                ...shape,
              }}
            />
          )
        })}
      </div>
    </Tooltip>
  )
}

/**
 * 一条问题。阻断与提醒**视觉可区分**(§5.2 步骤 4 的验收点)。
 *
 * `onJump` 给了就整条可点 —— FE-103 的验收结果是「阻断问题醒目且可点击」,
 * 点了要落到对应标签页。
 */
export function IssueLine({
  issue,
  onJump,
}: {
  issue: FlowIssue
  onJump?: (step: FlowStep) => void
}) {
  const meta = ISSUE_LEVEL_LABEL[issue.level]
  const clickable = Boolean(onJump)
  return (
    <div
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={clickable ? () => onJump?.(issue.target_step) : undefined}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.key === 'Enter' || e.key === ' ') onJump?.(issue.target_step)
            }
          : undefined
      }
      style={{
        display: 'flex',
        gap: 8,
        alignItems: 'baseline',
        padding: '5px 8px',
        borderLeft: `3px solid ${issue.level === 'BLOCKING' ? brandVars.danger : issue.level === 'NEEDS_CONFIRM' ? brandVars.sand : brandVars.lineStrong}`,
        background: issue.level === 'BLOCKING' ? brandVars.dangerBg : brandVars.surfaceAlt,
        cursor: clickable ? 'pointer' : 'default',
      }}
    >
      <Tag color={meta?.color} style={{ marginInlineEnd: 0 }}>
        {meta?.text ?? issue.level}
      </Tag>
      <span style={{ flex: 1 }}>
        {issue.message}
        {issue.hint && (
          <div style={{ color: brandVars.slate, fontSize: fontScale.meta, marginTop: 2 }}>{issue.hint}</div>
        )}
      </span>
      <span className="mono" style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
        {issue.code}
      </span>
    </div>
  )
}

export function IssueList({
  issues,
  onJump,
  empty = '这一步没有待处理的问题。',
}: {
  issues: FlowIssue[]
  onJump?: (step: FlowStep) => void
  empty?: string
}) {
  if (!issues.length) return <div style={{ color: brandVars.slate }}>{empty}</div>
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      {issues.map((issue, index) => (
        <IssueLine key={`${issue.code}-${issue.ref ?? index}`} issue={issue} onJump={onJump} />
      ))}
    </Space>
  )
}

/**
 * 退回回执(A10)。图片集与文案两个标签页共用一份。
 *
 * ## 为什么需要它
 *
 * 退回原因与补充说明原本**在界面上任何地方都不显示**:两样东西落库、进审计、
 * 经接口返回,然后消失。而快审弹窗的占位符写着「下一个接手的人会看到这句」,
 * `validate_reject` 还强制选「其他」必须写明 —— 两处都在向运营承诺这句话会被
 * 看见。看不见的代价不是少一个字段:运营写完第三条没人看的说明之后就不会再写
 * 第四条,而「UAT 结束要回答退回集中在哪一类」会跟着一起落空。
 *
 * ## 为什么原因中文不写在这里
 *
 * 走 `rejectReasons(target)`,与快审弹窗**共用同一个 queryKey**,因此也共用
 * 那份一小时的缓存,打开标签页不会多一次请求。前端抄一份原因表的后果写在
 * `test_the_reject_reason_list_is_not_copied_into_the_frontend` 里。
 *
 * ## 为什么不显示「下一步」
 *
 * 原因清单里带着 `next_action_label`,这里刻意不用它:当前该干什么由
 * `flow.next_action` 回答,已经显示在流程头上,而且它是**现在**的结论 ——
 * 退回之后有人补过素材、改过属性,下一步就变了。在同一屏上按原因再推一次,
 * 两句话会在那种时候互相矛盾,而运营没有理由知道该信哪一句。
 * 这块只回答「为什么被退回」。
 */
export function RejectNotice({
  target,
  reason,
  note,
  at,
}: {
  target: 'image_set' | 'copy'
  reason: string | null
  note: string | null
  at: string | null
}) {
  const reasons = useQuery({
    queryKey: ['reject-reasons', target],
    queryFn: () => workbenchApi.rejectReasons(target),
    staleTime: 60 * 60 * 1000,
    enabled: Boolean(reason),
  })

  if (!reason) return null

  // 认不出的原因码如实回显原值,不写「未知」—— 与后端 `flow._reject_label`
  // 同一条口径:看到一个陌生的码,下一步是去查它是谁写进来的,
  // 而「未知」把这条线索抹掉了
  const label = (reasons.data ?? []).find((o) => o.code === reason)?.label ?? reason

  return (
    <Alert
      type="error"
      showIcon
      message={`这一版被快审退回:${label}`}
      description={
        <Space direction="vertical" size={2} style={{ width: '100%' }}>
          {note ? (
            <span>{note}</span>
          ) : (
            <span style={{ color: brandVars.slate }}>退回时没有留下补充说明。</span>
          )}
          {at && (
            <span style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
              退回时间 {formatDateTime(at)}
            </span>
          )}
        </Space>
      }
    />
  )
}
