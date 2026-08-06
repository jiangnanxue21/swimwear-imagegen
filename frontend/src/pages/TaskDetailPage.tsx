import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  Alert, App, Button, Card, Descriptions, Drawer, Empty, Image, Skeleton, Space, Table, Tag,
  Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { generationApi, SUBMIT_RESULT_UNKNOWN } from '../api/generation'
import { evaluationApi } from '../api/reviews'
import GradeTag from '../components/GradeTag'
import ErrorNotice from '../components/ErrorNotice'
import EvaluationDetail from '../components/EvaluationDetail'
import ForceRetryModal from '../components/ForceRetryModal'
import { isResultUnknown, readError, readWriteError } from '../api/client'
import {
  MODE_LABEL, TASK_STATUS_LABEL, taskLiveness, taskPollInterval,
  type Attempt, type Candidate, type Evaluation,
} from '../api/types'
import { brandVars, fontScale, imageTile } from '../theme'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

/*
 * FE-GLOBAL-02 / FE-TASK-DETAIL-01:这里原来有第二份活动状态清单。
 *
 * 它是手工维护的"进行中"白名单,漏掉了 `AUTO_APPROVED` 和 `MANUALLY_APPROVED`
 * —— 而那两个状态之后还要走 FORMATTING -> COMPLETED。后果是详情页在批准的
 * 那一刻停止轮询,任务看着永远停在"已自动通过",而列表页(用的是公共
 * `isLiveTaskStatus`)在同一时间显示它已经完成。同一个任务,两个页面两种事实。
 *
 * 根因不是漏了两个值,是**判定有两份**。所以这里不是补两个值,是删掉这一份:
 * 公共判定用"终态反推"写(`TERMINAL_TASK_STATUSES`),后端加中间状态时
 * 不需要有人记得来改前端。
 *
 * A32:那个公共判定后来又被发现**自己也是错的** —— 它把 FAILED 与
 * MANUAL_REVIEW 当成了终态,而后端这两个都还有出边。现在拆成三档
 * (`taskLiveness`),本页三处轮询与一处空态文案都改读它。
 */

function CandidateTile({
  candidate, evaluation, onInspect,
}: {
  candidate: Candidate
  evaluation?: Evaluation
  onInspect: () => void
}) {
  const failed = candidate.status === 'DOWNLOAD_FAILED'
  return (
    <figure className="asset-tile" style={{ margin: 0 }}>
      {failed ? (
        <div
          style={{
            height: imageTile.review, display: 'grid', placeItems: 'center',
            background: brandVars.dangerBg, color: brandVars.dangerDeep,
            fontSize: fontScale.body, padding: 8, textAlign: 'center',
          }}
        >
          下载失败
        </div>
      ) : (
        <Image
          src={candidate.url}
          alt={`候选图 #${candidate.candidate_index + 1}`}
          preview={{ mask: '查看大图' }}
        />
      )}
      <figcaption>
        <Space size={4} wrap style={{ marginBottom: 4 }}>
          <Tag>#{candidate.candidate_index + 1}</Tag>
          <GradeTag grade={candidate.grade} score={candidate.overall_score} compact />
          {candidate.status === 'SELECTED' && <Tag color="success">采用</Tag>}
          {evaluation?.hard_fail && <Tag color="error">硬错误</Tag>}
          {failed && (
            <Tooltip title={candidate.error_message}>
              <Tag color="error">错误</Tag>
            </Tooltip>
          )}
        </Space>
        <div>
          {candidate.width ?? '—'}×{candidate.height ?? '—'}
        </div>
        <div className="mono" style={{ color: brandVars.textMuted }}>seed {candidate.seed ?? '—'}</div>
        {evaluation && (
          <Button size="small" type="link" style={{ padding: 0, height: 20 }} onClick={onInspect}>
            查看评分
          </Button>
        )}
      </figcaption>
    </figure>
  )
}

export default function TaskDetailPage() {
  const { id = '' } = useParams()
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [inspecting, setInspecting] = useState<string>('')
  // 强制重试弹窗。开关放在页面上,是因为它带一个必填输入 ——
  // 见 `components/ForceRetryModal.tsx` 顶部为什么不用 modal.confirm
  const [forceOpen, setForceOpen] = useState(false)

  const query = useQuery({
    queryKey: ['task', id],
    queryFn: () => generationApi.get(id),
    enabled: Boolean(id),
    refetchInterval: (q) =>
      (q.state.data ? taskPollInterval(q.state.data.status, 2500) : false),
  })

  useDocumentTitle(query.data ? `任务 · ${TASK_STATUS_LABEL[query.data.status]?.text ?? query.data.status}` : '生成任务')

  const cancel = useMutation({
    mutationFn: () => generationApi.cancel(id),
    onSuccess: () => {
      message.success('任务已取消')
      queryClient.invalidateQueries({ queryKey: ['task', id] })
    },
    onError: (err) => {
      message.error(readWriteError(err))
      if (isResultUnknown(err)) queryClient.invalidateQueries({ queryKey: ['task', id] })
    },
  })

  // 评分单独取一次:任务详情接口只带候选图上的分档与总分,维度分和问题在这里。
  const evaluations = useQuery({
    queryKey: ['task-evaluations', id],
    queryFn: () => evaluationApi.forTask(id),
    enabled: Boolean(id),
    // 任务跑完就停,否则 CANCELLED 这类永远拿不到评分的任务会一直轮询。
    // 评分只在**机器还在跑**的时候才会变,所以这里不用三档:等人动的两个
    // 状态下评分已经定稿了(MANUAL_REVIEW 正是评分给出结论之后才进的),
    // 慢轮询只会白问。真正需要的那一次由下面的补拉 effect 负责。
    refetchInterval: () =>
      query.data && taskLiveness(query.data.status) === 'LIVE' ? 4000 : false,
  })

  /*
   * FE-TASK-DETAIL-02:任务先到终态,评分轮询在同一轮渲染里立刻停掉。
   *
   * 评分是任务链路的最后一段写入,它落库的时刻**晚于**任务转终态的时刻。
   * 于是最后一次评分经常刚好落在两次轮询之间,而页面从此不再问了 ——
   * 表现是"跑完的任务没有评分",而库里其实有。
   *
   * 补一次:状态一变成终态就再拉一次评分。只补一次,不是继续轮询 ——
   * 真的没有评分的任务(FAILED / CANCELLED)不该被无限追问。
   */
  const status = query.data?.status
  useEffect(() => {
    // 触发条件是「机器不再写了」,不是「到终态了」——— MANUAL_REVIEW 恰恰是
    // 评分刚落库才进的状态,把它排除掉等于在最需要补拉的那一档上不补。
    if (!id || !status || taskLiveness(status) === 'LIVE') return
    evaluations.refetch()
    // evaluations 是 query handle,每次渲染都是新对象;依赖只跟状态走
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, status])

  const retry = useMutation({
    mutationFn: (vars: { force: boolean; note?: string }) =>
      generationApi.retry(id, { force: vars.force, note: vars.note }),
    onSuccess: (_data, vars) => {
      message.success(vars.force ? '已在对账后强制重试,任务重新排队' : '已重新排队')
      setForceOpen(false)
      queryClient.invalidateQueries({ queryKey: ['task', id] })
    },
    onError: (err) => {
      // 写请求:超时不等于没重试成功。结果未知时先让他刷新,别再点一次
      message.error(readWriteError(err))
      if (isResultUnknown(err)) queryClient.invalidateQueries({ queryKey: ['task', id] })
    },
  })

  /*
   * 「提交结果未知」的人工对账出口(FE-TASK-CREATE-06 / §7.2)。
   *
   * 后端对这个错误码 fail-closed,普通重试稳定 409。要放行必须有人为
   * "Provider 那边确实没有产生结果"背书,而且从 a28 后端整改起,那份背书
   * **必须写下来**(`reconciliation_note` 缺失即 422)。所以这里不是一个
   * 更显眼的重试按钮,是一个带必填输入的弹窗 —— 组件在
   * `components/ForceRetryModal.tsx`,和列表页共用同一份,因为它们是同一个决定。
   */

  if (query.isLoading) return <Skeleton active />
  if (query.isError) {
    return <Alert type="error" showIcon message="打不开这个任务" description={readError(query.error)} />
  }

  const task = query.data!
  const submitUnknown = task.error_code === SUBMIT_RESULT_UNKNOWN
  const rounds = [...new Set(task.candidates.map((c) => c.round_number))].sort((a, b) => a - b)
  const evalByCandidate = new Map((evaluations.data ?? []).map((e) => [e.candidate_id, e]))

  const attemptColumns: ColumnsType<Attempt> = [
    { title: '轮', dataIndex: 'round_number', width: 50, align: 'center' },
    { title: '次', dataIndex: 'attempt_number', width: 50, align: 'center' },
    { title: 'Provider', dataIndex: 'provider', width: 90 },
    {
      title: 'seed',
      dataIndex: 'seed',
      width: 110,
      render: (v: number | null) => <span className="mono">{v ?? '—'}</span>,
    },
    { title: '候选', dataIndex: 'candidate_count', width: 60, align: 'center' },
    {
      title: '耗时',
      dataIndex: 'duration_ms',
      width: 80,
      render: (v: number | null) => (v === null ? '—' : `${(v / 1000).toFixed(1)}s`),
    },
    {
      title: '结果',
      dataIndex: 'status',
      render: (s: string, row) => (
        <Space size={4}>
          <Tag color={s === 'SUCCEEDED' ? 'success' : s === 'FAILED' ? 'error' : 'default'}>{s}</Tag>
          {row.error_code && (
            <Tooltip title={row.error_message}>
              <Tag color="error" className="mono">{row.error_code}</Tag>
            </Tooltip>
          )}
          {row.regeneration_reason && <Tag>{row.regeneration_reason}</Tag>}
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space>
        <Link to="/tasks">
          <Button size="small" icon={<ArrowLeftOutlined />}>返回任务列表</Button>
        </Link>
        <Tag color={TASK_STATUS_LABEL[task.status]?.color}>
          {TASK_STATUS_LABEL[task.status]?.text ?? task.status}
        </Tag>
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={query.isFetching || evaluations.isFetching}
          onClick={() => {
            // FE-TASK-DETAIL-03:刷新按钮原来只刷任务。评分是另一条 query,
            // 于是"刷新"之后分数还是旧的 —— 而运营点刷新多半正是为了看分
            query.refetch()
            evaluations.refetch()
          }}
        >
          刷新
        </Button>
        <Button size="small" disabled={!task.can_cancel} loading={cancel.isPending} onClick={() => cancel.mutate()}>
          取消
        </Button>
        {submitUnknown ? (
          // 普通重试对这条任务稳定 409(后端 fail-closed),所以这里换成
          // 走对账确认框的那一个,而不是让运营先撞一次错误再去猜怎么办
          <Button
            size="small"
            danger
            disabled={!task.can_retry}
            loading={retry.isPending}
            onClick={() => setForceOpen(true)}
          >
            对账后强制重试
          </Button>
        ) : (
          <Button
            size="small"
            disabled={!task.can_retry}
            loading={retry.isPending}
            onClick={() => retry.mutate({ force: false })}
          >
            重试
          </Button>
        )}
      </Space>

      {submitUnknown && (
        <Alert
          type="warning"
          showIcon
          message="提交结果未知:这次生成可能已经在 Provider 那边跑起来了"
          description={
            <Space direction="vertical" size={4}>
              <span>
                提交阶段网络超时,系统没有拿到受理回执。系统不会自动重试 ——
                自动重试可能重复计费。
              </span>
              <span>
                请到 <strong>{task.provider}</strong> 后台按外部任务 ID
                <span className="mono"> {task.external_task_id ?? '(无)'} </span>
                核对:已经有结果就不要重试,等它跑完;确认没有结果再用「对账后强制重试」。
              </span>
            </Space>
          }
        />
      )}

      {task.error_code && !submitUnknown && (
        <Alert
          type="error"
          showIcon
          message={`生成失败 · ${task.error_code}`}
          description={task.error_message}
        />
      )}

      {task.status === 'SCORING' && (
        <Alert type="info" showIcon message="候选图已生成,正在评分与分档" />
      )}

      {task.status === 'REGENERATING' && (
        <Alert
          type="warning"
          showIcon
          message="本轮没有达到 A 档,已自动淘汰并排下一轮"
          description="重生原因与修复策略记录在下方的 Provider 调用记录里。"
        />
      )}

      {task.status === 'MANUAL_REVIEW' && (
        <Alert
          type="warning"
          showIcon
          message="轮次已耗尽,任务转入人工审核"
          description={<Link to="/reviews">去人工审核队列处理</Link>}
        />
      )}

      <Card size="small" title={<span className="mono">{task.id.slice(0, 8)}</span>}>
        <Descriptions size="small" column={3} bordered>
          <Descriptions.Item label="商品">
            <Link to={`/products/${task.product_id}`}>查看商品</Link>
          </Descriptions.Item>
          <Descriptions.Item label="Provider">{task.provider}</Descriptions.Item>
          <Descriptions.Item label="路由">{task.routing_mode}</Descriptions.Item>
          <Descriptions.Item label="模式">{MODE_LABEL[task.mode] ?? task.mode}</Descriptions.Item>
          <Descriptions.Item label="轮次">{task.current_round} / {task.max_rounds}</Descriptions.Item>
          <Descriptions.Item label="每轮候选">{task.candidate_count}</Descriptions.Item>
          <Descriptions.Item label="外部任务 ID" span={2}>
            <span className="mono">{task.external_task_id ?? '—'}</span>
          </Descriptions.Item>
          <Descriptions.Item label="基础 seed">{task.base_seed ?? '默认'}</Descriptions.Item>
          <Descriptions.Item label="幂等键" span={3}>
            <span className="mono" style={{ fontSize: fontScale.meta }}>{task.idempotency_key}</span>
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <Card size="small" title={`候选图 · 共 ${task.candidates.length} 张`}>
        {rounds.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              taskLiveness(task.status) === 'LIVE'
                ? '生成中,候选图稍后出现'
                : '这个任务没有产出候选图'
            }
          />
        ) : (
          rounds.map((round) => (
            <div key={round} style={{ marginBottom: 20 }}>
              <div className="section-label">第 {round} 轮</div>
              <div className="asset-grid asset-grid--review">
                {task.candidates
                  .filter((c) => c.round_number === round)
                  .map((c) => (
                    <CandidateTile
                      key={c.id}
                      candidate={c}
                      evaluation={evalByCandidate.get(c.id)}
                      onInspect={() => setInspecting(c.id)}
                    />
                  ))}
              </div>
            </div>
          ))
        )}
      </Card>

      <Drawer
        open={Boolean(inspecting)}
        width={520}
        title="候选图评分"
        onClose={() => setInspecting('')}
      >
        {/*
          * 评分查询失败**不许表现成"这张图没有评分"**(FE-TASK-DETAIL-04 /
          * 阶段 A 验收第 6 条)。"没有评分"是一个业务结论:它意味着这张图
          * 还没走到评分,或者评分器判它不需要打分。而拉不到评分只是网络。
          * 两者的下一步一个是等、一个是重试,说错了运营会一直等下去。
          */}
        {evaluations.isError ? (
          <ErrorNotice
            title="拉不到这张图的评分"
            error={evaluations.error}
            onRetry={() => evaluations.refetch()}
            retrying={evaluations.isFetching}
          />
        ) : (
          <EvaluationDetail evaluation={evalByCandidate.get(inspecting) ?? null} />
        )}
      </Drawer>

      <Card size="small" title={`Provider 调用记录 · ${task.attempts.length} 次`}>
        <Table<Attempt>
          rowKey="id"
          size="small"
          bordered
          pagination={false}
          columns={attemptColumns}
          dataSource={task.attempts}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有调用记录" /> }}
        />
      </Card>

      <ForceRetryModal
        open={forceOpen}
        provider={task.provider}
        externalTaskId={task.external_task_id ?? null}
        submitting={retry.isPending}
        onCancel={() => setForceOpen(false)}
        onConfirm={(note) => retry.mutate({ force: true, note })}
      />
    </Space>
  )
}
