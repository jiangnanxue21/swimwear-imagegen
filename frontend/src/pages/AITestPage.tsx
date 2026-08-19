import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Alert, App, Button, Card, Checkbox, Col, Descriptions, Empty, Image, Row, Select,
  Radio, Space, Spin, Table, Tag, Typography,
} from 'antd'
import { ClockCircleOutlined, ExperimentOutlined, PlayCircleOutlined } from '@ant-design/icons'
import { useMutation, useQuery } from '@tanstack/react-query'
import { generationApi } from '../api/generation'
import { evaluationApi } from '../api/reviews'
import { isAuthError, readWriteError } from '../api/client'
import { aiTestsApi } from '../api/aiTests'
import { workbenchApi } from '../api/workbench'
import { CANDIDATE_STATUS_LABEL, type AiTestRun, type Evaluation } from '../api/types'
import EvaluationDetail from '../components/EvaluationDetail'
import BrandTag from '../components/BrandTag'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { formatDateTime } from '../utils/datetime'
import {
  durationLabel,
  engineLabel,
  outcomeLabel,
  subjectLabel,
  versionLabel,
} from '../utils/aiTestRuns'
import { brandVars, fontScale, space } from '../theme'

/**
 * 独立 AI 能力测试。
 *
 * 这里测试的是生产评分器与生产文案生成器,而不是重新跑完整业务流程。
 * 评分结果只写 diagnostic 调用留痕;文案结果只回显,不创建正式版本。
 */
const { Text } = Typography

export default function AITestPage() {
  useDocumentTitle('AI 能力测试')
  const { message } = App.useApp()
  const [taskId, setTaskId] = useState('')
  const [candidateId, setCandidateId] = useState('')
  const [productId, setProductId] = useState('')
  const [scoreCostConfirmed, setScoreCostConfirmed] = useState(false)
  const [copyCostConfirmed, setCopyCostConfirmed] = useState(false)
  const [scoreStartedAt, setScoreStartedAt] = useState<number | null>(null)
  const [scoreElapsedSeconds, setScoreElapsedSeconds] = useState(0)

  const tasks = useQuery({
    queryKey: ['ai-test-tasks'],
    queryFn: () => generationApi.list({ page: 1, page_size: 100, sort: 'created_at', order: 'desc' }),
  })
  const task = useQuery({
    queryKey: ['ai-test-task', taskId],
    queryFn: () => generationApi.get(taskId),
    enabled: Boolean(taskId),
  })
  const products = useQuery({
    queryKey: ['ai-test-products'],
    queryFn: () => workbenchApi.list({ page: 1, page_size: 100, sort: 'updated_at', order: 'desc' }),
  })

  /**
   * FE-312 历史记录。**这一查询是那句「请保持页面打开,不要刷新」的替代品** ——
   * 在留档之前,文案测试的输出关掉页面就没了。
   *
   * 不做筛选表单:这一页是"跑一发看看"的地方,要按提示词版本翻档案该去提示词页
   * (FE-314)。多一个筛选面板会让这一页变成第二个查询入口,而两个入口迟早分叉。
   */
  /** FE-314 的落点:提示词页带着 key + 序号跳过来时,历史表只显示那一版。 */
  const [searchParams, setSearchParams] = useSearchParams()
  const focusKey = searchParams.get('prompt_key')
  const focusVersion = searchParams.get('prompt_template_version')

  const runs = useQuery({
    // 筛选参数进 queryKey —— 不进的话换一版之后 react-query 会拿缓存,
    // 界面显示的是上一版的记录而地址栏说的是这一版
    queryKey: ['ai-test-runs', focusKey, focusVersion],
    queryFn: () =>
      aiTestsApi.list({
        limit: 20,
        ...(focusKey ? { prompt_key: focusKey } : {}),
        ...(focusVersion ? { prompt_template_version: focusVersion } : {}),
      }),
    retry: (count, err) => !isAuthError(err) && count < 2,
  })

  useEffect(() => {
    setCandidateId('')
  }, [taskId])

  const selectedCandidate = useMemo(
    () => task.data?.candidates.find((item) => item.id === candidateId),
    [candidateId, task.data?.candidates],
  )

  const scoreTest = useMutation({
    mutationFn: () => evaluationApi.testCandidate(candidateId, scoreCostConfirmed),
    onMutate: () => {
      setScoreStartedAt(Date.now())
      setScoreElapsedSeconds(0)
    },
    onSuccess: (result) => {
      if (result.success) message.success('评分能力测试完成,正式评分未被覆盖')
      else message.warning(result.message)
    },
    // FE-315:**每一次测试之后都要重新勾。** 这个勾的全部意义是「这一次可能真的
    // 花钱」—— 勾上之后一直勾着,等于第二次点击不再需要任何确认,而它同样会计费。
    // 原来换任务时调了 `scoreTest.reset()`,但确认状态没跟着重置:换一个候选图
    // 之后那个勾还在,于是"确认"这个动作对第二次点击是不存在的
    onSettled: () => {
      setScoreCostConfirmed(false)
      runs.refetch()
    },
    onError: (error) => message.error(readWriteError(error)),
  })

  useEffect(() => {
    if (!scoreTest.isPending || scoreStartedAt === null) return undefined
    const updateElapsed = () => {
      setScoreElapsedSeconds(Math.floor((Date.now() - scoreStartedAt) / 1000))
    }
    updateElapsed()
    const timer = window.setInterval(updateElapsed, 1000)
    return () => window.clearInterval(timer)
  }, [scoreStartedAt, scoreTest.isPending])

  const copyTest = useMutation({
    mutationFn: () => workbenchApi.testCopy(productId, copyCostConfirmed),
    onSuccess: (result) => {
      if (result.success) message.success('文案能力测试完成,结果未保存为正式版本')
      else message.warning(result.message)
    },
    onError: (error) => message.error(readWriteError(error)),
    // FE-315,与评分侧同一条。`onSettled` 而不是 `onSuccess`:失败的调用一样计费
    onSettled: () => {
      setCopyCostConfirmed(false)
      runs.refetch()
    },
  })

  const diagnosticEvaluation: Evaluation | null = scoreTest.data?.evaluation
    ? {
        ...scoreTest.data.evaluation,
        id: scoreTest.data.attempt.id,
        candidate_id: scoreTest.data.candidate_id,
        round_number: scoreTest.data.attempt.round_number,
        rank_index: null,
        similarity_score: null,
        realism_score: null,
        created_at: scoreTest.data.attempt.created_at,
      }
    : null

  return (
    <Space direction="vertical" size={space.lg} style={{ width: '100%' }}>
      <PageHeader
        title="AI 能力测试"
        subtitle="单独验证出图后的大模型评分,以及标题、卖点和描述生成;测试结果不会覆盖正式业务数据。"
      />

      <Alert
        type="warning"
        showIcon
        message="这是生产环境的能力测试,不是模拟演示"
        description="如果当前启用的是真实外部模型,点击测试可能产生费用。评分会留下一条标为「诊断」的调用记录;文案只回显并做合规校验,不保存版本。"
      />

      <Row gutter={[space.lg, space.lg]} align="top">
        <Col xs={24} xl={12}>
          <Card
            title={<Space><ExperimentOutlined />出图后大模型评分</Space>}
            styles={{ body: { minHeight: 510 } }}
          >
            <Space direction="vertical" size={space.md} style={{ width: '100%' }}>
              <div>
                <Typography.Text strong>1. 选择已有生成任务</Typography.Text>
                <Select
                  aria-label="生成任务"
                  showSearch
                  optionFilterProp="label"
                  placeholder="选择任务"
                  value={taskId || undefined}
                  loading={tasks.isLoading}
                  status={tasks.isError ? 'error' : undefined}
                  style={{ width: '100%', marginTop: 6 }}
                  options={(tasks.data?.items ?? []).map((item) => ({
                    value: item.id,
                    label: `${item.id.slice(0, 8)} · ${item.provider} · ${item.status}`,
                  }))}
                  onChange={(value) => {
                    scoreTest.reset()
                    setTaskId(value)
                  }}
                />
                {tasks.isError && (
                  <ErrorNotice
                    title="拉不到生成任务"
                    error={tasks.error}
                    onRetry={() => tasks.refetch()}
                    retrying={tasks.isFetching}
                    style={{ marginTop: 6 }}
                  />
                )}
              </div>

              <div>
                <Typography.Text strong>2. 选择候选图</Typography.Text>
                {!taskId ? (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="请先选择生成任务"
                    style={{ marginBlock: space.sm }}
                  />
                ) : task.isLoading ? (
                  <div style={{ padding: space.lg, textAlign: 'center' }}>
                    <Spin size="small" />
                  </div>
                ) : (task.data?.candidates.length ?? 0) === 0 ? (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="这个任务还没有候选图"
                    style={{ marginBlock: space.sm }}
                  />
                ) : (
                  <Radio.Group
                    aria-label="评分候选图"
                    value={candidateId || undefined}
                    onChange={(event) => {
                      scoreTest.reset()
                      setCandidateId(String(event.target.value))
                    }}
                    style={{ width: '100%', marginTop: 6 }}
                  >
                    <Row gutter={[space.sm, space.sm]}>
                      {(task.data?.candidates ?? []).map((item) => (
                        <Col xs={24} sm={12} md={8} key={item.id}>
                          <Radio.Button
                            value={item.id}
                            aria-label={`第 ${item.round_number} 轮候选图 ${item.candidate_index + 1}`}
                            style={{
                              width: '100%', height: 'auto', padding: 6,
                              textAlign: 'center', whiteSpace: 'normal',
                            }}
                          >
                            <Space direction="vertical" size={4} style={{ width: '100%' }}>
                              <Image
                                preview={false}
                                src={item.url}
                                alt={`第 ${item.round_number} 轮候选图 ${item.candidate_index + 1} 缩略图`}
                                width="100%"
                                height={180}
                                fallback="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs="
                                style={{ objectFit: 'contain', background: brandVars.imageBg }}
                              />
                              <Typography.Text strong>
                                第 {item.round_number} 轮 · 候选 #{item.candidate_index + 1}
                              </Typography.Text>
                              <Typography.Text type="secondary" style={{ fontSize: fontScale.meta }}>
                                {CANDIDATE_STATUS_LABEL[item.status] ?? item.status} ·{' '}
                                {item.width ?? '—'}×{item.height ?? '—'}
                              </Typography.Text>
                            </Space>
                          </Radio.Button>
                        </Col>
                      ))}
                    </Row>
                  </Radio.Group>
                )}
                {task.isError && (
                  <ErrorNotice
                    title="拉不到任务候选图"
                    error={task.error}
                    onRetry={() => task.refetch()}
                    retrying={task.isFetching}
                    style={{ marginTop: 6 }}
                  />
                )}
              </div>

              {selectedCandidate && (
                <div style={{ display: 'flex', gap: space.md, alignItems: 'center' }}>
                  <Image
                    src={selectedCandidate.url}
                    alt={`当前选择:第 ${selectedCandidate.round_number} 轮候选图 ${selectedCandidate.candidate_index + 1}`}
                    width={112}
                    height={140}
                    style={{ objectFit: 'contain', background: brandVars.imageBg }}
                  />
                  <Descriptions size="small" column={1} colon={false}>
                    <Descriptions.Item label="候选">#{selectedCandidate.candidate_index + 1}</Descriptions.Item>
                    <Descriptions.Item label="尺寸">{selectedCandidate.width ?? '—'} × {selectedCandidate.height ?? '—'}</Descriptions.Item>
                    <Descriptions.Item label="已有正式评分">{selectedCandidate.grade ?? '无'}</Descriptions.Item>
                  </Descriptions>
                </div>
              )}

              <Checkbox checked={scoreCostConfirmed} onChange={(event) => setScoreCostConfirmed(event.target.checked)}>
                我确认:当前评分器如果是真实模型,这次测试可能产生费用
              </Checkbox>
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                disabled={!candidateId || !scoreCostConfirmed || scoreTest.isPending}
                loading={scoreTest.isPending}
                onClick={() => scoreTest.mutate()}
              >
                开始评分测试
              </Button>

              {scoreTest.isPending && (
                <Alert
                  type="info"
                  showIcon
                  icon={<ClockCircleOutlined />}
                  message={`评分模型正在处理 · 已等待 ${scoreElapsedSeconds} 秒`}
                  description={
                    <Space direction="vertical" size={2}>
                      <span>图片准备、模型推理和瞬时错误重试可能需要 1–5 分钟。</span>
                      <span>可以离开本页,结果会记入下方历史;别重复点击,按钮会在收到明确结果后恢复。</span>
                    </Space>
                  }
                />
              )}

              {scoreTest.isError && (
                <ErrorNotice
                  kind="write"
                  title="评分测试没有收到明确结果"
                  error={scoreTest.error}
                />
              )}

              {scoreTest.data && (
                <Alert
                  type={scoreTest.data.success ? 'success' : 'error'}
                  showIcon
                  message={scoreTest.data.message}
                  description={
                    <Space size={4} wrap>
                      <Tag>{scoreTest.data.attempt.evaluator}</Tag>
                      {scoreTest.data.attempt.model_name && <Tag>{scoreTest.data.attempt.model_name}</Tag>}
                      {scoreTest.data.attempt.diagnostic
                        ? <BrandTag tone="accent">独立测试</BrandTag>
                        : <Tag>正式流程</Tag>}
                      {scoreTest.data.attempt.duration_ms !== null && <Tag>{scoreTest.data.attempt.duration_ms} ms</Tag>}
                      {/* FE-313:此前这一行渲染 evaluator / model_name / duration_ms /
                          diagnostic 四项,**唯独缺提示词版本** —— 而"这一版跑出什么"
                          正是这个页面存在的理由 */}
                      {scoreTest.data.attempt.prompt_version && (
                        <Tag>提示词 v{scoreTest.data.attempt.prompt_version}</Tag>
                      )}
                    </Space>
                  }
                />
              )}
              {scoreTest.data?.success && <EvaluationDetail evaluation={diagnosticEvaluation} />}
            </Space>
          </Card>
        </Col>

        <Col xs={24} xl={12}>
          <Card
            title={<Space><ExperimentOutlined />标题与描述生成</Space>}
            styles={{ body: { minHeight: 510 } }}
          >
            <Space direction="vertical" size={space.md} style={{ width: '100%' }}>
              <div>
                <Typography.Text strong>1. 选择已有商品</Typography.Text>
                <Select
                  aria-label="文案商品"
                  showSearch
                  optionFilterProp="label"
                  placeholder="选择商品"
                  value={productId || undefined}
                  loading={products.isLoading}
                  status={products.isError ? 'error' : undefined}
                  style={{ width: '100%', marginTop: 6 }}
                  options={(products.data?.items ?? []).map(({ product }) => ({
                    value: product.id,
                    label: `${product.sku} · ${product.name}`,
                  }))}
                  onChange={setProductId}
                />
                {products.isError && (
                  <ErrorNotice
                    title="拉不到商品列表"
                    error={products.error}
                    onRetry={() => products.refetch()}
                    retrying={products.isFetching}
                    style={{ marginTop: 6 }}
                  />
                )}
              </div>

              <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
                只使用该商品已确认的属性。若没有已确认属性,后端会拒绝生成,而不是让模型猜。
              </Typography.Text>
              <Checkbox checked={copyCostConfirmed} onChange={(event) => setCopyCostConfirmed(event.target.checked)}>
                我确认:若当前文案生成器为真实模型,本次测试可能产生费用
              </Checkbox>
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                disabled={!productId || !copyCostConfirmed || copyTest.isPending}
                loading={copyTest.isPending}
                onClick={() => copyTest.mutate()}
              >
                开始文案测试
              </Button>

              {copyTest.isPending && (
                <Alert
                  type="info"
                  showIcon
                  message="文案模型正在处理"
                  description="长模型调用最多等待约 5 分钟。请保持页面打开,不要重复点击。"
                />
              )}

              {copyTest.isError && (
                <ErrorNotice
                  kind="write"
                  title="文案测试没有收到明确结果"
                  error={copyTest.error}
                />
              )}

              {copyTest.data?.success && copyTest.data.copy ? (
                <Space direction="vertical" size={space.sm} style={{ width: '100%' }}>
                  <Alert
                    type={copyTest.data.violations.some((item) => item.blocking) ? 'warning' : 'success'}
                    showIcon
                    message={copyTest.data.violations.some((item) => item.blocking)
                      ? '文案已生成,但合规校验未通过'
                      : '文案已生成并通过硬性校验'}
                    description={
                      <Space size={4} wrap>
                        <Tag>{copyTest.data.generator}</Tag>
                        {copyTest.data.trace.model && <Tag>{copyTest.data.trace.model}</Tag>}
                        {copyTest.data.trace.duration_ms != null && <Tag>{copyTest.data.trace.duration_ms} ms</Tag>}
                        {copyTest.data.trace.total_tokens != null && <Tag>{copyTest.data.trace.total_tokens} Token</Tag>}
                      </Space>
                    }
                  />
                  <Descriptions size="small" bordered column={1}>
                    <Descriptions.Item label="标题">{copyTest.data.copy.title || '—'}</Descriptions.Item>
                    <Descriptions.Item label="卖点">
                      <ul style={{ margin: 0, paddingLeft: 18 }}>
                        {copyTest.data.copy.bullet_points.map((item) => <li key={item}>{item}</li>)}
                      </ul>
                    </Descriptions.Item>
                    <Descriptions.Item label="描述">{copyTest.data.copy.description || '—'}</Descriptions.Item>
                    <Descriptions.Item label="关键词">{copyTest.data.copy.keywords.join('、') || '—'}</Descriptions.Item>
                  </Descriptions>
                  {copyTest.data.violations.length > 0 && (
                    <Alert
                      type="warning"
                      showIcon
                      message={`校验问题 ${copyTest.data.violations.length} 条`}
                      description={copyTest.data.violations.map((item) => item.message).join('；')}
                    />
                  )}
                </Space>
              ) : copyTest.data ? (
                <Alert
                  type="error"
                  showIcon
                  message="文案测试未成功"
                  description={
                    <Space direction="vertical" size={2}>
                      <span>{copyTest.data.message}</span>
                      {copyTest.data.error_code && <span className="mono">{copyTest.data.error_code}</span>}
                    </Space>
                  }
                />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择商品后运行一次测试,结果会显示在这里" />
              )}
            </Space>
          </Card>
        </Col>
      </Row>

      {/* FE-312 历史记录。**这张表是那句「请保持页面打开,不要刷新」的替代品。**
          在留档之前,文案测试的输出关掉页面就没了,而评分测试只在
          `evaluation_attempts` 里留了痕,没有任何地方能把两类测试一起按
          提示词版本查出来 */}
      <Card
        title={focusKey ? `历史记录 · ${focusKey} v${focusVersion}` : '历史记录'}
        extra={
          <Space>
            {focusKey && (
              // 筛着的时候必须给得出"看全部"的路 —— 否则从提示词页跳过来的人
              // 会以为这一页只有这几条,而地址栏那两个参数不是每个人都会看
              <Button size="small" onClick={() => setSearchParams({})}>
                看全部
              </Button>
            )}
            <Button size="small" onClick={() => runs.refetch()} loading={runs.isFetching}>
              刷新
            </Button>
          </Space>
        }
      >
        {runs.isError && !isAuthError(runs.error) && (
          <ErrorNotice
            title="拉不到测试历史"
            error={runs.error}
            onRetry={() => runs.refetch()}
            retrying={runs.isFetching}
          />
        )}
        <Table<AiTestRun>
          rowKey="id"
          size="small"
          loading={runs.isLoading}
          pagination={false}
          dataSource={runs.data?.runs ?? []}
          locale={{
            emptyText: '还没有跑过测试。跑一次之后结果会记在这里,可以离开本页',
          }}
          columns={[
            {
              title: '时间',
              dataIndex: 'created_at',
              width: 170,
              render: (v: string) => formatDateTime(v),
            },
            {
              title: '类型',
              dataIndex: 'kind',
              width: 90,
              render: (v: string) => (v === 'copy' ? '文案' : '评分'),
            },
            {
              // 判定全在 `utils/aiTestRuns.ts` —— 那里能被 `node --test` 真的跑,
              // 内联在这里的话一行都测不了(a61 的约定,a63 补做)
              title: '提示词',
              width: 220,
              render: (_: unknown, row: AiTestRun) => {
                const label = versionLabel(row)
                return row.prompt_key ? (
                  <Space size={4}>
                    <Tag>{row.prompt_key}</Tag>
                    {label && <Tag>{label}</Tag>}
                  </Space>
                ) : (
                  <Text type="secondary">—</Text>
                )
              },
            },
            {
              // FE-312 要的「对象」列。后端一直在返回 `subject_id`,而 a62 那版
              // 没画 —— 于是「哪一张候选图 / 哪个商品」这件事只能靠时间猜
              title: '对象',
              width: 150,
              render: (_: unknown, row: AiTestRun) => subjectLabel(row),
            },
            {
              title: '生成器 / 模型',
              width: 200,
              render: (_: unknown, row: AiTestRun) => engineLabel(row),
            },
            {
              title: '结果',
              width: 150,
              render: (_: unknown, row: AiTestRun) => {
                const outcome = outcomeLabel(row)
                return (
                  <Space size={4}>
                    <BrandTag tone={outcome.tone}>{outcome.text}</BrandTag>
                    {outcome.detail && <Text type="secondary">{outcome.detail}</Text>}
                  </Space>
                )
              },
            },
            {
              title: '耗时',
              dataIndex: 'duration_ms',
              width: 90,
              render: (v: number | null) => durationLabel(v),
            },
            {
              title: 'token',
              dataIndex: 'total_tokens',
              width: 90,
              // 0 与 null 不是一回事:Mock 评分器不消耗 token(0),
              // 而模板文案根本不调模型(null)
              render: (v: number | null) => (v == null ? '—' : String(v)),
            },
            {
              // **PRD §12.5 要的是「成本」,而 `ai_test_runs` 没有金额列**
              // (§11.4 的清单里只有 `billable: bool`)。金额在
              // `provider_usage_records` 里,要关联才拿得到 —— 那是另一项工作。
              // 这里如实画「是否计费」,而不是编一个金额出来
              title: '计费',
              dataIndex: 'billable',
              width: 80,
              // 失败也可能计费,所以它与「结果」那一列独立
              render: (v: boolean) => (v ? '是' : '否'),
            },
            { title: '操作者', dataIndex: 'actor', width: 120 },
          ]}
        />
        {runs.data?.has_more && (
          <Text type="secondary">
            只显示最近 {runs.data.limit} 条。要按提示词版本翻更早的档案,去提示词页。
          </Text>
        )}
      </Card>
    </Space>
  )
}
