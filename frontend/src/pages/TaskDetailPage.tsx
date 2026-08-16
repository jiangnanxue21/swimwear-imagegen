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
import {
  EvaluationAttemptCard, ScoringStatusCard,
} from '../components/task/TaskScoringDiagnostics'
import { isResultUnknown, readError, readWriteError } from '../api/client'
import {
  EVALUATION_OUTCOME_LABEL, MODE_LABEL, TASK_STATUS_LABEL, taskLiveness, taskPollInterval,
  type Attempt, type Candidate, type Evaluation, type EvaluationAttempt,
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
  candidate, evaluation, attempt, evaluationLoading, evaluationError, onInspect,
}: {
  candidate: Candidate
  evaluation?: Evaluation
  attempt?: EvaluationAttempt
  evaluationLoading: boolean
  evaluationError: boolean
  onInspect: () => void
}) {
  const failed = candidate.status === 'DOWNLOAD_FAILED'
  const attemptFailed = attempt && !['SUCCEEDED', 'ROUND_RETRY_SCHEDULED'].includes(attempt.outcome)
  const scoreAction = evaluationError
    ? '评分记录读取失败'
    : evaluation
      ? '查看评分'
      : evaluationLoading
        ? '读取评分中'
        : attemptFailed
          ? '查看评分失败'
          : '查看评分状态'
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
          {!evaluation && attemptFailed && <Tag color="error">评分未成功</Tag>}
          {failed && (
            <Tooltip title={candidate.error_message}>
              <Tag color="error">错误</Tag>
            </Tooltip>
          )}
        </Space>
        <div>
          {candidate.width ?? '—'}×{candidate.height ?? '—'}
        </div>
        <div className="mono" style={{ color: brandVars.textMuted }}>种子 {candidate.seed ?? '—'}</div>
        <Button
          size="small"
          type="link"
          danger={evaluationError || Boolean(attemptFailed)}
          loading={evaluationLoading}
          style={{ padding: 0, height: 20 }}
          onClick={onInspect}
        >
          {scoreAction}
        </Button>
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

  // 成功评分和评分调用台账必须分开取:前者只在解析成功后才有行,后者才包含
  // 限流、鉴权失败、超时和非法 JSON。只查前者会把「调用失败」显示成「没评分」。
  const evaluationAttempts = useQuery({
    queryKey: ['task-evaluation-attempts', id],
    queryFn: () => evaluationApi.attemptsForTask(id),
    enabled: Boolean(id),
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
    evaluationAttempts.refetch()
    // 两个 query handle 每次渲染都是新对象;依赖只跟状态走
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
  const attemptByCandidate = new Map<string, EvaluationAttempt>()
  for (const attempt of evaluationAttempts.data ?? []) {
    if (attempt.candidate_id) attemptByCandidate.set(attempt.candidate_id, attempt)
  }

  const attemptColumns: ColumnsType<Attempt> = [
    { title: '轮', dataIndex: 'round_number', width: 50, align: 'center' },
    { title: '次', dataIndex: 'attempt_number', width: 50, align: 'center' },
    { title: '服务商', dataIndex: 'provider', width: 90 },
    {
      title: '随机种子',
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
          loading={query.isFetching || evaluations.isFetching || evaluationAttempts.isFetching}
          onClick={() => {
            // FE-TASK-DETAIL-03:刷新按钮原来只刷任务。评分是另一条 query,
            // 于是"刷新"之后分数还是旧的 —— 而运营点刷新多半正是为了看分
            query.refetch()
            evaluations.refetch()
            evaluationAttempts.refetch()
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
          message="提交结果未知:这次生成可能已经在服务商那边跑起来了"
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

      {(task.status === 'CREATED' || task.status === 'QUEUED') && task.attempts.length === 0 && (
        <Alert
          type={task.dispatch_status === 'ABANDONED' ? 'error' : 'info'}
          showIcon
          message={
            task.dispatch_status === 'DISPATCHED'
              ? task.status === 'CREATED'
                ? '任务消息已进入队列,但还没有后台执行进程来领'
                : '任务消息已进入队列,正在等后台执行进程来领'
              : task.dispatch_status === 'PENDING'
                ? '任务消息正在等待投递到队列'
                : task.dispatch_status === 'ABANDONED'
                  ? '任务消息投递已放弃,需要排查队列(Redis)或后台执行进程的故障'
                  : '任务正在等待后台执行'
          }
          description={(
            <Space direction="vertical" size={2}>
              <span>接口服务只记录 HTTP 请求;真正的 FASHN 调用与报错在后台执行进程(Celery worker)的窗口里。</span>
              <span className="mono">
                Windows 启动命令:python -m celery -A app.tasks.celery_app:celery_app worker -l info -P solo
              </span>
              {task.dispatch_error && <span>最近一次派发错误:{task.dispatch_error}</span>}
            </Space>
          )}
        />
      )}

      {task.status === 'CANCELLED' && task.attempts.length === 0 && (
        <Alert
          type="info"
          showIcon
          message="任务在调用服务商前已经取消"
          description="因此下面没有 FASHN 请求记录,也没有服务商报错;这类未执行的任务可以从任务列表删除。"
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
          description="重生原因与修复策略记录在下方的出图调用记录里。"
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

      <ScoringStatusCard
        taskStatus={task.status}
        evaluationCount={evaluations.data?.length ?? 0}
        attemptCount={evaluationAttempts.data?.length ?? 0}
        evaluationsLoading={evaluations.isLoading}
        attemptsLoading={evaluationAttempts.isLoading}
        evaluationsError={evaluations.error}
        attemptsError={evaluationAttempts.error}
        evaluationsFetching={evaluations.isFetching}
        attemptsFetching={evaluationAttempts.isFetching}
        onRetryEvaluations={() => evaluations.refetch()}
        onRetryAttempts={() => evaluationAttempts.refetch()}
      />

      <Card size="small" title={<span className="mono">{task.id.slice(0, 8)}</span>}>
        <Descriptions size="small" column={3} bordered>
          <Descriptions.Item label="商品">
            <Link to={`/products/${task.product_id}`}>查看商品</Link>
          </Descriptions.Item>
          <Descriptions.Item label="出图服务商">{task.provider}</Descriptions.Item>
          <Descriptions.Item label="路由">{task.routing_mode}</Descriptions.Item>
          <Descriptions.Item label="模式">{MODE_LABEL[task.mode] ?? task.mode}</Descriptions.Item>
          <Descriptions.Item label="轮次">{task.current_round} / {task.max_rounds}</Descriptions.Item>
          <Descriptions.Item label="每轮候选">{task.candidate_count}</Descriptions.Item>
          <Descriptions.Item label="外部任务 ID" span={2}>
            <span className="mono">{task.external_task_id ?? '—'}</span>
          </Descriptions.Item>
          <Descriptions.Item label="队列派发">
            {task.dispatch_status ?? '—'}（尝试 {task.dispatch_attempts} 次）
          </Descriptions.Item>
          <Descriptions.Item label="基础随机种子" span={3}>{task.base_seed ?? '默认'}</Descriptions.Item>
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
                      attempt={attemptByCandidate.get(c.id)}
                      evaluationLoading={evaluations.isLoading
                        || (!evalByCandidate.has(c.id) && evaluationAttempts.isLoading)}
                      evaluationError={evaluations.isError
                        || (!evalByCandidate.has(c.id) && evaluationAttempts.isError)}
                      onInspect={() => setInspecting(c.id)}
                    />
                  ))}
              </div>
            </div>
          ))
        )}
      </Card>

      <EvaluationAttemptCard
        attempts={evaluationAttempts.data ?? []}
        loading={evaluationAttempts.isLoading}
        fetching={evaluationAttempts.isFetching}
        error={evaluationAttempts.error}
        onRetry={() => evaluationAttempts.refetch()}
      />

      <Card size="small" title="标题、卖点与描述">
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <span>
            文案不是生成任务的一部分:它使用已确认的商品属性生成标题、卖点、描述和关键词,
            并在商品向导中单独版本化、校验和批准。
          </span>
          <Link to={`/wizard/${task.product_id}?step=COPY`}>
            <Button type="primary">进入文案生成</Button>
          </Link>
        </Space>
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
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            <EvaluationDetail evaluation={evalByCandidate.get(inspecting) ?? null} />
            {evaluationAttempts.isError && (
              <ErrorNotice
                title="拉不到这张图的评分调用记录"
                error={evaluationAttempts.error}
                onRetry={() => evaluationAttempts.refetch()}
                retrying={evaluationAttempts.isFetching}
              />
            )}
            {attemptByCandidate.get(inspecting) && (
              <Descriptions size="small" column={1} bordered title="最近一次评分调用">
                <Descriptions.Item label="结果">
                  {attemptByCandidate.get(inspecting)?.outcome
                    ? EVALUATION_OUTCOME_LABEL[attemptByCandidate.get(inspecting)!.outcome]
                    : '—'}
                </Descriptions.Item>
                <Descriptions.Item label="评分器 / 模型">
                  {attemptByCandidate.get(inspecting)?.evaluator || '—'} /{' '}
                  {attemptByCandidate.get(inspecting)?.model_name || '—'}
                </Descriptions.Item>
                <Descriptions.Item label="错误">
                  {attemptByCandidate.get(inspecting)?.error_code || '—'}{' '}
                  {attemptByCandidate.get(inspecting)?.error_message || ''}
                </Descriptions.Item>
              </Descriptions>
            )}
          </Space>
        )}
      </Drawer>

      <Card size="small" title={`出图调用记录 · ${task.attempts.length} 次`}>
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
