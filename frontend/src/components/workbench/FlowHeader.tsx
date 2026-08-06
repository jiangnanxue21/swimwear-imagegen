/**
 * 详情页顶部固定进度区(FE-105 / §3.4)。
 *
 * 必须固定显示的四样:完成度、阻断问题、待确认问题、当前步骤与下一步。
 * 这四个数字**全部来自 `flow`**,和列表页那一行读的是同一个接口字段 ——
 * §3.4「任何情况下不得出现两个数字」是这条数据路径保证的。
 */
import { Alert, Button, Progress, Space, Tag, Tooltip, Typography } from 'antd'
import { ArrowRightOutlined, WarningOutlined } from '@ant-design/icons'
import {
  ACTION_TAB,
  FLOW_STEP_ORDER,
  NEXT_ACTION_LABEL,
  detectFlowAnomaly,
  type ProductFlow,
  type WorkbenchProduct,
  type WorkbenchTab,
} from '../../api/workbench'
import { StepStateTag } from './FlowBits'
import { brandVars, fontScale } from '../../theme'
import BrandTag from '../BrandTag'

/** 完成度配色:有阻断就压成红,免得 90% 的绿条盖住「导不出去」这件事。 */
function progressColor(flow: ProductFlow): string {
  if (flow.blocking_count) return brandVars.danger
  if (flow.completion >= 100) return brandVars.success
  return brandVars.marine
}

export default function FlowHeader({
  product,
  flow,
  thumbnail,
  onJump,
  actionPending,
}: {
  product: WorkbenchProduct
  flow: ProductFlow
  thumbnail: string | null
  onJump: (tab: WorkbenchTab) => void
  actionPending?: boolean
}) {
  const anomaly = detectFlowAnomaly(flow)
  const action = flow.next_action
  const done = action.code === 'DONE'

  return (
    <div
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 5,
        background: brandVars.surface,
        border: `1px solid ${brandVars.line}`,
        borderRadius: 4,
        padding: 12,
      }}
    >
      <div style={{ display: 'flex', gap: 14, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        {thumbnail ? (
          <img
            src={thumbnail}
            alt=""
            style={{
              width: 64,
              height: 64,
              objectFit: 'contain',
              background: brandVars.imageBg,
              borderRadius: 4,
            }}
          />
        ) : (
          <div
            style={{
              width: 64,
              height: 64,
              background: brandVars.imageBg,
              borderRadius: 4,
              display: 'grid',
              placeItems: 'center',
              color: brandVars.textMuted,
              fontSize: fontScale.meta,
            }}
          >
            无主图
          </div>
        )}

        <div style={{ minWidth: 220, flex: 1 }}>
          <Space size={8} wrap>
            <span className="mono" style={{ fontWeight: 600 }}>
              {product.sku}
            </span>
            <Tag>{product.spu}</Tag>
            {/* §13.2:受众常驻显示。它决定了模特候选集、检查项和导出模板 ——
                审阅时看不到它,就无从判断"这条检查项为什么是这几项"。
                待确认用醒目色:那不是一个可以放着不管的状态(§8.2 它排在
                待选模特之前 —— 受众定了模特候选集才是对的) */}
            {product.audience ? (
              <Tag>{product.audience_label}</Tag>
            ) : (
              <BrandTag tone="warning">{product.audience_label || '待确认'}</BrandTag>
            )}
          </Space>
          <div style={{ color: brandVars.slate, marginTop: 2 }}>{product.name}</div>
          {/* §19:重点检查项来自规则包,不是前端的一张表。
              读不出来(受众待确认/spec 缺失)时后端给空数组 —— 那时这一行
              不出现,而不是显示一份猜出来的清单 */}
          {product.review_focus?.length ? (
            <Tooltip title="按商品受众下发的重点检查区域,来自该受众的规则包">
              <div
                style={{
                  color: brandVars.textMuted,
                  fontSize: fontScale.meta,
                  marginTop: 4,
                }}
              >
                重点检查:{product.review_focus.join(' · ')}
              </div>
            </Tooltip>
          ) : null}
          <Progress
            percent={flow.completion}
            size="small"
            strokeColor={progressColor(flow)}
            style={{ marginTop: 6, marginBottom: 0, maxWidth: 320 }}
            format={(p) => `${p}%`}
          />
        </div>

        <Space size={16} align="start">
          <Tooltip title="阻断问题:不解决就走不到导出。点击跳到第一条">
            <div
              role="button"
              tabIndex={0}
              onClick={() => flow.blocking_count && onJump('overview')}
              onKeyDown={(e) => {
                if ((e.key === 'Enter' || e.key === ' ') && flow.blocking_count) {
                  e.preventDefault()
                  onJump('overview')
                }
              }}
              aria-label={`阻断问题 ${flow.blocking_count} 条,点击查看`}
              style={{ cursor: flow.blocking_count ? 'pointer' : 'default' }}
            >
              <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>阻断</div>
              <div
                style={{
                  fontSize: fontScale.metric,
                  fontWeight: 600,
                  lineHeight: 1.1,
                  color: flow.blocking_count ? brandVars.danger : brandVars.textMuted,
                }}
              >
                {flow.blocking_count}
              </div>
            </div>
          </Tooltip>
          <div>
            <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>待确认</div>
            <div
              style={{
                fontSize: fontScale.metric,
                fontWeight: 600,
                lineHeight: 1.1,
                color: flow.pending_count ? brandVars.sand : brandVars.textMuted,
              }}
            >
              {flow.pending_count}
            </div>
          </div>
          <div>
            <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>当前步骤</div>
            <div style={{ fontSize: fontScale.strong, fontWeight: 600, lineHeight: 1.6 }}>
              {flow.current_step_label}
            </div>
          </div>
        </Space>

        {/* 唯一下一步(FE-104)。非法状态下**不显示任何动作**(§3.2.3) */}
        <div style={{ minWidth: 200 }}>
          <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>下一步</div>
          {anomaly ? (
            <Tag color="error" icon={<WarningOutlined />} style={{ marginTop: 4 }}>
              状态异常
            </Tag>
          ) : (
            <>
              <Button
                type={done ? 'default' : 'primary'}
                disabled={done}
                loading={actionPending}
                icon={done ? undefined : <ArrowRightOutlined />}
                onClick={() => onJump(ACTION_TAB[action.code])}
                style={{ marginTop: 4 }}
              >
                {action.label || NEXT_ACTION_LABEL[action.code]}
              </Button>
              <div style={{ fontSize: fontScale.meta, color: brandVars.slate, marginTop: 4, maxWidth: 260 }}>
                {action.reason}
              </div>
            </>
          )}
        </div>
      </div>

      {/**
        * 五个子状态一览 —— **只读进度指示,不可点**(二次走查 B-2)。
        *
        * ## 为什么把它降级
        *
        * 改动前这一屏有两套导航:这条五步状态条(带状态、可点),以及下面的
        * 八个标签页。上一轮给标签补了问题徽标(P1-3)之后,矛盾显形了 ——
        * **同一件事在同屏上被说了两遍**:这里说「图片集 待批准」,
        * 标签栏的图片集标签上也挂着一个数字。运营的合理反应是
        * 「这两个是不是不同的东西?」,而它们是同一个。
        *
        * 二选一时保留标签徽标:它离它要跳转的内容更近,点击到达路径更短,
        * 而且标签栏本来就是这一页的导航。这条降级为纯进度指示,
        * 只回答「整体走到哪一步了」—— 它在这个位置(流程头里)本来就更适合
        * 回答这个问题,而不是当第二套菜单。
        */}
      <div style={{ display: 'flex', gap: 6, marginTop: 10, flexWrap: 'wrap' }}>
        {FLOW_STEP_ORDER.map((step) => {
          const row = flow.steps.find((s) => s.step === step)
          if (!row) return null
          const current = flow.current_step === step
          return (
            <div
              key={step}
              style={{
                border: `1px solid ${current ? brandVars.marine : brandVars.line}`,
                borderRadius: 4,
                padding: '4px 8px',
                background: current ? brandVars.marineSoft : brandVars.surface,
                minWidth: 128,
              }}
            >
              <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>{row.label}</div>
              <Space size={6}>
                <StepStateTag step={row.step} state={row.state} summary={row.summary} />
                <span className="mono" style={{ color: brandVars.slate, fontSize: fontScale.meta }}>
                  {row.summary}
                </span>
              </Space>
            </div>
          )
        })}
      </div>

      {anomaly && (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 10 }}
          message="状态异常,请联系管理员"
          description={
            <Typography.Paragraph style={{ marginBottom: 0 }}>
              {anomaly.reason}。
              <br />
              <span className="mono">
                商品 {product.id} · {anomaly.detail}
              </span>
              <br />
              <span style={{ color: brandVars.slate }}>
                这类状态组合按 §3.2.3 属于 P0 缺陷:前端不猜测下一步,请把上面这行原始状态附给开发。
              </span>
            </Typography.Paragraph>
          }
        />
      )}
    </div>
  )
}

