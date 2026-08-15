import { useEffect, useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Checkbox, Col, Descriptions, Empty, Image, Row, Select,
  Radio, Space, Spin, Tag, Typography,
} from 'antd'
import { ClockCircleOutlined, ExperimentOutlined, PlayCircleOutlined } from '@ant-design/icons'
import { useMutation, useQuery } from '@tanstack/react-query'
import { generationApi } from '../api/generation'
import { evaluationApi } from '../api/reviews'
import { readWriteError } from '../api/client'
import { workbenchApi } from '../api/workbench'
import type { Evaluation } from '../api/types'
import EvaluationDetail from '../components/EvaluationDetail'
import BrandTag from '../components/BrandTag'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { brandVars, fontScale, space } from '../theme'

/**
 * 独立 AI 能力测试。
 *
 * 这里测试的是生产评分器与生产文案生成器，而不是重新跑完整业务流程。
 * 评分结果只写 diagnostic 调用留痕；文案结果只回显，不创建正式版本。
 */
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
      if (result.success) message.success('评分能力测试完成，正式评分未被覆盖')
      else message.warning(result.message)
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
      if (result.success) message.success('文案能力测试完成，结果未保存为正式版本')
      else message.warning(result.message)
    },
    onError: (error) => message.error(readWriteError(error)),
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
        subtitle="单独验证出图后的大模型评分，以及标题、卖点和描述生成；测试结果不会覆盖正式业务数据。"
      />

      <Alert
        type="warning"
        showIcon
        message="这是生产能力测试，不是 Mock 演示"
        description="如果当前启用真实外部模型，点击测试可能产生费用。评分会留下 diagnostic 调用记录；文案只回显并执行合规校验，不保存版本。"
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
                                {item.status} · {item.width ?? '—'}×{item.height ?? '—'}
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
                    alt={`当前选择：第 ${selectedCandidate.round_number} 轮候选图 ${selectedCandidate.candidate_index + 1}`}
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
                我确认：若当前评分器为真实模型，本次测试可能产生费用
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
                      <span>请保持页面打开，不要刷新或重复点击；按钮会在收到明确结果后恢复。</span>
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
                只使用该商品已确认的属性。若没有已确认属性，后端会拒绝生成，而不是让模型猜。
              </Typography.Text>
              <Checkbox checked={copyCostConfirmed} onChange={(event) => setCopyCostConfirmed(event.target.checked)}>
                我确认：若当前文案生成器为真实模型，本次测试可能产生费用
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
                  description="长模型调用最多等待约 5 分钟。请保持页面打开，不要重复点击。"
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
                      ? '文案已生成，但合规校验未通过'
                      : '文案已生成并通过硬性校验'}
                    description={
                      <Space size={4} wrap>
                        <Tag>{copyTest.data.generator}</Tag>
                        {copyTest.data.trace.model && <Tag>{copyTest.data.trace.model}</Tag>}
                        {copyTest.data.trace.duration_ms != null && <Tag>{copyTest.data.trace.duration_ms} ms</Tag>}
                        {copyTest.data.trace.total_tokens != null && <Tag>{copyTest.data.trace.total_tokens} tokens</Tag>}
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
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选择商品后运行一次测试，结果会显示在这里" />
              )}
            </Space>
          </Card>
        </Col>
      </Row>
    </Space>
  )
}
