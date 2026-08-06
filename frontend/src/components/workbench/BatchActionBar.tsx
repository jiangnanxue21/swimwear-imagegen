/**
 * 批量选择与动作(FE-301)。
 *
 * 验收结果只有一句:**执行前显示可执行 / 不可执行数量**。
 * 所以这个组件的核心不是那四个按钮,而是点下去之后的那个预览弹窗 ——
 * 它必须在真的执行之前告出两个数字,并且让"12 件不可执行"能被继续追问一句
 * "为什么":逐条给理由、给建议、给跳转。
 *
 * 两个数字**都来自后端**的 `/batches/plan`。前端不数,理由和阶段 1 一样:
 * 前端数出来的"可执行 8 件"和后端真正执行的件数迟早会差一件,
 * 而那一件通常正是运营在追的那件。
 */
import { useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, App, Button, Checkbox, Empty, Input, Modal, Space, Table, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  ExportOutlined, FileTextOutlined, ScanOutlined, ThunderboltOutlined,
} from '@ant-design/icons'
import { readError, readWriteError } from '../../api/client'
import {
  BATCH_ACTION_HINT,
  BATCH_ACTION_LABEL,
  BATCH_ERROR_LABEL,
  PAID_BATCH_ACTIONS,
  batchApi,
  readDispatchOutcome,
  type BatchAction,
  type BatchPlanBlocked,
  type BatchPlanResponse,
} from '../../api/batch'
import { STEP_TAB } from '../../api/workbench'
import { brandVars, fontScale } from '../../theme'
import { newRequestKey } from '../../utils/requestKey'

const ACTION_ICONS: Record<BatchAction, ReactNode> = {
  EXTRACT: <ScanOutlined />,
  GENERATE_COPY: <FileTextOutlined />,
  VALIDATE_DRAFT: <ThunderboltOutlined />,
  EXPORT: <ExportOutlined />,
}

const ACTIONS: BatchAction[] = ['EXTRACT', 'GENERATE_COPY', 'VALIDATE_DRAFT', 'EXPORT']

/** 超过这个件数的付费批量要手打确认。取 50 是因为 UAT 的全量批次就是 50 件 ——
    低于它属于日常操作,不该加摩擦;等于或超过它意味着这是一次「全量」动作 */
const TYPED_CONFIRM_THRESHOLD = 50
const CONFIRM_WORD = '执行'

export default function BatchActionBar({
  selected,
  onClear,
  onDone,
}: {
  selected: string[]
  onClear: () => void
  onDone: () => void
}) {
  const navigate = useNavigate()
  // 用 App 上下文里的实例而不是静态 `message` / `notification`:
  // 静态方法拿不到 ConfigProvider 的主题与 locale(antd v5 会在控制台警告)
  const { notification } = App.useApp()
  const [action, setAction] = useState<BatchAction | null>(null)
  const [plan, setPlan] = useState<BatchPlanResponse | null>(null)
  const [includeExported, setIncludeExported] = useState(false)
  const [planning, setPlanning] = useState(false)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** 大额付费批量的手打确认。每次开弹窗清空 */
  const [typed, setTyped] = useState('')

  /**
   * 这一次**提交意图**的幂等键(A45-batch12-3 / EX-06)。
   *
   * 上面那个 ref 挡的是同一轮渲染里的两次点击,挡不住另外两种重复:
   *
   *     响应在回程丢失   浏览器/代理超时,批次其实已经建好了
   *     他自己再点一次    上面那个 ref 已经在 finally 里放开了
   *
   * 两种都会建出第二个逻辑相同的批次。回执那一层挡得住重复的模型调用,
   * 但挡不住批次中心里多出一份进度、一套审计,而运营没有依据判断哪个有效。
   *
   * 生命周期是"一次提交意图",不是"一次 HTTP 请求":
   *
   *     开/换计划   清掉 —— 换了动作或筛选就是另一件事了
   *     提交失败    **留着** —— 这正是重发要复用它的场景
   *     提交成功    清掉 —— 下一次点击是新的一件事
   */
  const pendingKey = useRef<string | null>(null)

  /**
   * `keepTyped` 给"弹窗已经开着、只是换了个筛选"那条路径用(A45-#38)。
   *
   * 50 件以上的付费批量要求手打"执行"。运营打完之后勾一下「包含已导出商品」,
   * Checkbox 的 onChange 直接调 `openPlan` → `setTyped('')` → 输入被静默清空、
   * 确定按钮重新变灰,**界面不说明原因**。他会以为自己打错了,再打一遍。
   *
   * 手打确认防的是"没看清就点了确定",而换筛选这个动作本身是**看清了才会做的**
   * —— 清掉它既不增加安全性,又让人以为界面坏了。
   *
   * 首次开弹窗仍然清:那时候他还没看过任何计划。
   */
  const openPlan = async (
    next: BatchAction,
    exported = false,
    keepTyped = false,
  ) => {
    setAction(next)
    setIncludeExported(exported)
    setPlanning(true)
    setError(null)
    setPlan(null)
    // 换动作或换筛选 = 另一次提交意图,换一把幂等键。
    // 留着旧键的话,后端会拿新入参撞上旧键,报一个"这把键已经用在另一组入参上"
    // 的 409 —— 那句话对着一个从没见过这把键的人说,没有任何意义
    pendingKey.current = null
    if (!keepTyped) setTyped('')
    try {
      setPlan(await batchApi.plan(next, selected, exported))
    } catch (err) {
      setError(readError(err))
    } finally {
      setPlanning(false)
    }
  }

  /**
   * 评审 P2-5:提交中的护栏。
   *
   * 原来只靠 antd 的 `loading={running}` 挡二次点击 —— 那是**重渲染之后**才生效的,
   * 两次足够快的点击(或回车连按)会在同一轮里都进到这里。
   *
   * 后端有回执,所以重复提交不会重复计费。但那是**调用顺序保证的,
   * 不是原语自己保证的** —— 和后端评审 P1-10 是同一句话。加一个 ref 成本为零,
   * 而且它不依赖任何关于 React 批处理时机的假设。
   */
  const submitting = useRef(false)

  const run = async () => {
    if (!action || !plan) return
    if (submitting.current) return
    submitting.current = true
    setRunning(true)
    if (!pendingKey.current) pendingKey.current = newRequestKey()
    try {
      const job = await batchApi.create(action, selected, {
        includeExported,
        requestKey: pendingKey.current,
      })
      pendingKey.current = null
      const { succeeded, needs_human: needsHuman, failed, skipped } = job.counts
      const leftover = needsHuman + failed

      /*
       * FE-BATCH-02 / 03:这次调用的投递结果决定这条通知该说什么。
       *
       *   queued   投给 worker 了,一件都还没跑。说"已提交",
       *            counts 是排队快照,报出来会误导
       *   stuck    celery 模式下投递失败(broker 不可用)。批次会永远停在
       *            待处理 —— 这是要报警的事,而上一版把它和"跑完了"
       *            显示成同一句话
       *   done     inline 模式,确实已经跑完。照旧报四个数字
       *
       * 判定来自后端的 `dispatch_state`,这里不再用 `dispatched` + `status`
       * 自己拼(硬规则第 4 条)。收敛在 `readDispatchOutcome` 一处。
       */
      const outcome = readDispatchOutcome(job)
      const queued = outcome === 'queued'
      const stuck = outcome === 'stuck'

      // A45-batch12-3 / EX-06:同一把键的重发。后端什么都没做,返回的是上一次
      // 那个批次。必须说出来 —— 不说的话运营看到的是一条和平时一模一样的
      // "已执行 N 件",而那 N 件是上一次的,他会以为刚刚又跑了一遍
      if (job.reused_request) {
        notification.info({
          message: `${BATCH_ACTION_LABEL[action]} · 这次提交没有重复执行`,
          duration: null,
          key: `batch-${job.id}`,
          description: (
            <Space direction="vertical" size={6} style={{ width: '100%' }}>
              <Typography.Text style={{ fontSize: fontScale.body }}>
                这批已经提交过了,下面看到的是那一次的批次和进度,没有新建、也没有重复计费。
              </Typography.Text>
              <Button
                size="small"
                type="primary"
                onClick={() => {
                  notification.destroy(`batch-${job.id}`)
                  navigate(`/workbench-batches?open=${job.id}`)
                }}
              >
                去批次页看进度
              </Button>
            </Space>
          ),
        })
        setAction(null)
        onClear()
        onDone()
        navigate(`/workbench-batches?open=${job.id}`)
        return
      }

      if (queued || stuck) {
        notification[stuck ? 'error' : 'info']({
          message: stuck
            ? `${BATCH_ACTION_LABEL[action]} · 批次已建好,但没能投给后台`
            : `${BATCH_ACTION_LABEL[action]} · 已提交后台执行 ${job.counts.total} 件`,
          duration: null,
          key: `batch-${job.id}`,
          description: (
            <Space direction="vertical" size={6} style={{ width: '100%' }}>
              <Typography.Text style={{ fontSize: fontScale.body }}>
                {stuck
                  ? '条目已经落库并停在待处理,不会丢。请让管理员检查消息队列是否可用,恢复后在批次详情里点一次重试即可 —— 不要重新建批次,那会重复计费。'
                  : '批次已经落库,由后台 worker 逐件执行。批次页会自动刷新进度,不用重复提交。'}
              </Typography.Text>
              <Button
                size="small"
                type="primary"
                onClick={() => {
                  notification.destroy(`batch-${job.id}`)
                  navigate(`/workbench-batches?open=${job.id}`)
                }}
              >
                去批次页看进度
              </Button>
            </Space>
          ),
        })

        setAction(null)
        onClear()
        onDone()
        navigate(`/workbench-batches?open=${job.id}`)
        return
      }

      // 走查 P2:这里原本是 `message.success`,3 秒后自己消失。而那四个数字里
      // **需人工**和**失败**正是运营接下来要处理的事 —— 它们一闪就没,等于把
      // 待办说了一遍然后收走。改成 notification:不自动关闭,运营处理完再手动关。
      notification[leftover ? 'warning' : 'success']({
        message: `${BATCH_ACTION_LABEL[action]} · 已执行 ${job.counts.total} 件`,
        duration: null,
        key: `batch-${job.id}`,
        description: (
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            <Space size={12} wrap>
              <span>成功 <b>{succeeded}</b></span>
              <span style={{ color: needsHuman ? brandVars.warning : undefined }}>
                需人工 <b>{needsHuman}</b>
              </span>
              <span style={{ color: failed ? brandVars.danger : undefined }}>
                失败 <b>{failed}</b>
              </span>
              <span style={{ color: brandVars.textMuted }}>跳过 {skipped}</span>
            </Space>
            {leftover > 0 && (
              <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
                这 {leftover} 件没走完,批次详情里逐条给了原因和下一步。
              </Typography.Text>
            )}
            <Button
              size="small"
              type={leftover ? 'primary' : 'default'}
              onClick={() => {
                notification.destroy(`batch-${job.id}`)
                navigate(`/workbench-batches?open=${job.id}`)
              }}
            >
              {leftover ? `去处理这 ${leftover} 件` : '看批次详情'}
            </Button>
          </Space>
        ),
      })

      setAction(null)
      onClear()
      onDone()
      navigate(`/workbench-batches?open=${job.id}`)
    } catch (err) {
      // 建批次是付费动作的入口:超时后如实说"结果未知",并让他去批次页确认。
      //
      // A45-batch12-3:这里原来还有一句"而不是提示重试 —— 重试会建出第二个
      // 同样的批次"。接上 `Idempotency-Key` 之后那句话不再成立:`pendingKey`
      // 刻意**不在**这一支清掉,所以再点一次发的是同一把键,后端会返回第一次
      // 那个批次而不是新建。措辞仍然保守 —— 让他去批次页看一眼永远是对的,
      // 而"可以放心重试"这种话要等真库上的第 6 组测试跑过之后再说
      setError(readWriteError(err))
    } finally {
      submitting.current = false
      setRunning(false)
    }
  }

  const blockedColumns: ColumnsType<BatchPlanBlocked> = [
    {
      title: '商品',
      key: 'sku',
      width: 200,
      render: (_, row) => (
        <div>
          <div className="mono">{row.sku}</div>
          <div style={{ color: brandVars.slate, fontSize: fontScale.meta }}>{row.spu}</div>
        </div>
      ),
    },
    {
      title: '原因',
      key: 'reason',
      render: (_, row) => (
        <div>
          <Tag color="warning">{row.code_label || BATCH_ERROR_LABEL[row.code] || row.code}</Tag>
          <div style={{ fontSize: fontScale.body }}>{row.reason}</div>
          {/* §6.3.1 第三条:运营无需开发协助即可判断下一步动作 */}
          {row.advice && (
            <div style={{ color: brandVars.slate, fontSize: fontScale.body }}>下一步:{row.advice}</div>
          )}
        </div>
      ),
    },
    {
      title: '',
      key: 'jump',
      width: 92,
      render: (_, row) => (
        <Button
          size="small"
          type="link"
          onClick={() => {
            const tab = row.target_step ? STEP_TAB[row.target_step] : 'overview'
            navigate(`/workbench/${row.product_id}?tab=${tab}`)
          }}
        >
          去处理
        </Button>
      ),
    },
  ]

  const paid = action !== null && PAID_BATCH_ACTIONS.includes(action)
  const needsTyping = paid && (plan?.executable_count ?? 0) >= TYPED_CONFIRM_THRESHOLD
  const typedOk = !needsTyping || typed.trim() === CONFIRM_WORD

  return (
    <>
      {/* 走查 P0-4:这一条原本跟着页面滚。一页 20 行(可调到 50/100),
          运营勾到第 15 行时动作条已经在视口外 —— 要么滚上去点、滚下来继续勾,
          要么勾完再滚上去,而滚上去之后又看不见自己勾了哪些。
          批量是这套系统对付 50 件全量的主力路径,这处摩擦每天要吃掉很多次。

          二次走查 A-5:原本这里有一对 `margin: 0 -20px` 的负值,想让它顶到内容区
          边缘。但 M-5 之后中间多了一层 1680px 的居中容器 —— 宽屏上那个负值只是
          让它超出居中容器 20px,**并没有**顶到边缘,注释在说谎。
          现在撤掉:与表格同宽反而更好读(吸顶条和它下面的表头对齐),
          也不再依赖任何关于外层 padding 的假设。 */}
      <div
        style={{
          position: 'sticky',
          top: 0,
          zIndex: 11,
        }}
      >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          flexWrap: 'wrap',
          border: `1px solid ${brandVars.sand}`,
          background: brandVars.sandSoft,
          borderRadius: 4,
          padding: '8px 12px',
        }}
      >
        <Typography.Text strong>已选 {selected.length} 件</Typography.Text>
        {ACTIONS.map((a) => (
          <Tooltip key={a} title={BATCH_ACTION_HINT[a]}>
            <Button
              size="small"
              icon={ACTION_ICONS[a]}
              onClick={() => openPlan(a)}
              loading={planning && action === a}
            >
              {BATCH_ACTION_LABEL[a]}
            </Button>
          </Tooltip>
        ))}
        <Button size="small" type="text" onClick={onClear}>
          取消选择
        </Button>
      </div>
      </div>

      <Modal
        open={action !== null}
        title={`${action ? BATCH_ACTION_LABEL[action] : ''} · 执行前确认`}
        onCancel={() => setAction(null)}
        onOk={run}
        okButtonProps={{
          disabled: !plan || plan.executable_count === 0 || !typedOk,
          loading: running,
          danger: needsTyping,
        }}
        okText={plan ? `执行 ${plan.executable_count} 件` : '执行'}
        cancelText="不执行"
        width={720}
        destroyOnClose
      >
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}

        {plan && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Space size={16}>
              <Tag color="success" style={{ fontSize: fontScale.strong, padding: '2px 10px' }}>
                可执行 {plan.executable_count}
              </Tag>
              <Tag color="default" style={{ fontSize: fontScale.strong, padding: '2px 10px' }}>
                不可执行 {plan.blocked_count}
              </Tag>
            </Space>

            {paid && plan.executable_count > 0 && (
              <Alert
                type="warning"
                showIcon
                message={`这个动作会调用按次计费的模型,本次 ${plan.executable_count} 次`}
                description={
                  <Space direction="vertical" size={6} style={{ width: '100%' }}>
                    <span>
                      同一 SPU 只会执行一次;上游没变化的商品会命中回执直接复用,不重复计费。
                    </span>
                    {/* 走查 P0-4 附带:200 件的付费动作和 5 件原本是同一颗按钮、
                        同一个手势。`okText` 已经把件数说出来了,但那行字和
                        「点确定」之间没有任何摩擦 —— 而这一步花的是真钱,
                        且不可撤销。超过阈值要求手打一次,把肌肉记忆打断 */}
                    {needsTyping && (
                      <div>
                        <Typography.Text strong style={{ color: brandVars.dangerDeep }}>
                          超过 {TYPED_CONFIRM_THRESHOLD} 件,请手动输入 {CONFIRM_WORD} 确认:
                        </Typography.Text>
                        <Input
                          value={typed}
                          onChange={(e) => setTyped(e.target.value)}
                          placeholder={CONFIRM_WORD}
                          style={{ marginTop: 4, maxWidth: 220 }}
                          status={typed && typed !== CONFIRM_WORD ? 'error' : undefined}
                        />
                      </div>
                    )}
                  </Space>
                }
              />
            )}

            {action === 'EXPORT' && (
              <Checkbox
                checked={includeExported}
                onChange={(e) => openPlan('EXPORT', e.target.checked, true)}
              >
                包含已导出且未过期的商品(平台侧上传失败需要重导时勾选)
              </Checkbox>
            )}

            {plan.blocked_count > 0 ? (
              <div>
                <div className="section-label" style={{ marginBottom: 6 }}>
                  不可执行的 {plan.blocked_count} 件及原因
                </div>
                <Table<BatchPlanBlocked>
                  rowKey="product_id"
                  size="small"
                  bordered
                  columns={blockedColumns}
                  dataSource={plan.blocked}
                  pagination={plan.blocked.length > 8 ? { pageSize: 8 } : false}
                />
              </div>
            ) : (
              <Empty
                image={null}
                description="选中的商品全部可执行"
                style={{ margin: '8px 0' }}
              />
            )}
          </Space>
        )}
      </Modal>
    </>
  )
}
