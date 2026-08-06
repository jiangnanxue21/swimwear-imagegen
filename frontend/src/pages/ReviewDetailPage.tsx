import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  Alert, App, Button, Card, Col, Collapse, Descriptions, Divider, Empty, Image, Input,
  InputNumber, Modal, Row, Select, Skeleton, Space, Tag, Tooltip, Typography,
} from 'antd'
import {
  ArrowLeftOutlined, CheckOutlined, CloseOutlined, ReloadOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { evaluationApi, reviewsApi } from '../api/reviews'
import { providersApi } from '../api/generation'
import { readError } from '../api/client'
import { useWriteError } from '../hooks/useWriteError'
import GradeTag from '../components/GradeTag'
import { AudienceTag, ReviewFocusLine } from '../components/AudienceBadge'
import EvaluationDetail from '../components/EvaluationDetail'
import CompareView from '../components/CompareView'
import { brandVars, fontScale } from '../theme'
import { formatDateTime } from '../utils/datetime'
import {
  ASSET_TYPE_LABEL, HARD_FAIL_LABEL, MODE_LABEL, REVIEW_REASON_LABEL, REVIEW_STATUS_LABEL,
  TASK_STATUS_LABEL, type ReviewCandidate, type TaskStatus,
  isProviderSelectable,
  providerOptionLabel,
} from '../api/types'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

/** 常用人工标签。做成预设是为了让标签可统计 —— 自由输入也允许,但先给默认项。 */
const TAG_PRESETS = [
  '商品结构错误', '颜色不符', '图案畸变', '模特不合适', '皮肤质感差',
  '光影不可用', '构图需重拍', '原图质量不足', '可用于列表页', '待补拍',
]

function CandidateTile({
  candidate, selected, onSelect,
}: {
  candidate: ReviewCandidate
  selected: boolean
  onSelect: () => void
}) {
  const hardFail = candidate.evaluation?.hard_fail ?? false
  return (
    <figure
      className="asset-tile"
      onClick={onSelect}
      style={{
        margin: 0,
        cursor: 'pointer',
        outline: selected ? `2px solid ${brandVars.accent}` : 'none',
        outlineOffset: -1,
      }}
    >
      <Image
        src={candidate.url}
        alt={`第 ${candidate.round_number} 轮候选图 #${candidate.candidate_index + 1}`}
        preview={{ mask: '查看大图' }}
      />
      <figcaption>
        <Space size={4} wrap style={{ marginBottom: 4 }}>
          <Tag>第 {candidate.round_number} 轮 · #{candidate.candidate_index + 1}</Tag>
          <GradeTag grade={candidate.grade} score={candidate.overall_score} compact />
          {hardFail && <Tag color="error">硬错误</Tag>}
        </Space>
        <div className="mono" style={{ color: brandVars.textMuted }}>seed {candidate.seed ?? '—'}</div>
      </figcaption>
    </figure>
  )
}

export default function ReviewDetailPage() {
  const { id = '' } = useParams()
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()

  const [selectedId, setSelectedId] = useState<string>('')
  const [note, setNote] = useState('')
  const [tags, setTags] = useState<string[]>([])
  const [regenOpen, setRegenOpen] = useState(false)
  const [regenProvider, setRegenProvider] = useState<string | undefined>()
  const [extraRounds, setExtraRounds] = useState(1)

  const query = useQuery({
    queryKey: ['review', id],
    queryFn: () => reviewsApi.get(id),
    enabled: Boolean(id),
  })

  useDocumentTitle(query.data ? `审核 ${query.data.product_sku}` : '图片人工审核')
  const rules = useQuery({ queryKey: ['rule-sets'], queryFn: evaluationApi.ruleSets })
  const providers = useQuery({ queryKey: ['providers'], queryFn: providersApi.list })

  const review = query.data

  /**
   * 换一条审核记录 = 清空全部本地状态(BLOCK-05)。
   *
   * React Router 从 `/reviews/A` 走到 `/reviews/B` 时**复用同一个组件实例**,
   * 于是这六个 useState 会原样留在那里。下面那个默认候选 effect 的条件是
   * `if (!review || selectedId) return` —— A 已经有 selectedId 了,
   * B 的默认候选就永远不会被设上。
   *
   * 候选串位大概率只表现成 409(后端会拒绝不属于 B 的候选 id),
   * 真正会写坏数据的是另外两个:**A 的备注和 A 的标签会提交到 B 的记录上**。
   * 那是审核意见,不是缓存 —— 一条写着"颜色不符"的驳回理由挂到另一件商品上,
   * 事后没有任何地方能看出它是串过来的。
   *
   * 用 effect 而不是给路由元素加 `key={id}`:`<Route element>` 那一层拿不到
   * 参数,要拿得包一个中间组件。effect 的代价是多一次渲染,
   * 换来的是"重置这件事在组件里看得见"。
   *
   * **依赖只有 `id`。** 写成 `[id, review]` 会在每次轮询返回新对象时把
   * 运营正在写的备注清掉。
   */
  useEffect(() => {
    setSelectedId('')
    setNote('')
    setTags([])
    setRegenOpen(false)
    setRegenProvider(undefined)
    setExtraRounds(1)
  }, [id])

  // 默认选中系统挑出的最佳候选;它没有时退回各轮最佳里的第一张。
  //
  // 上面那个 reset 已经保证换记录时 selectedId 是空的,所以这里的
  // `selectedId` 短路只剩下它本来的意思:**不要覆盖运营已经手选的那一张。**
  useEffect(() => {
    if (!review || selectedId) return
    setSelectedId(review.best_candidate_id ?? review.round_best_candidates[0]?.id ?? '')
  }, [review, selectedId])

  const refreshDecision = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['review', id] })
    queryClient.invalidateQueries({ queryKey: ['reviews'] })
  }, [queryClient, id])

  const afterDecision = (text: string) => {
    message.success(text)
    refreshDecision()
    queryClient.invalidateQueries({ queryKey: ['task', review?.task_id] })
    /*
     * A45-#37:**一次批准的影响会跨出评审模块。**
     *
     * 它会让任务走到 COMPLETED,并把商品状态改成 COMPLETED —— 而
     * `['products']` 与 `['workbench']` 原来都不在失效列表里。于是运营
     * 批完回到列表页,看到的还是"待处理",他会再点一次。
     *
     * 快审页对同类跨模块影响是显式失效 `['workbench']` 的,两处保护标准
     * 本来就该一致。
     */
    queryClient.invalidateQueries({ queryKey: ['products'] })
    queryClient.invalidateQueries({ queryKey: ['workbench'] })
  }

  /**
   * 三个决策动作都是**改别人看到的状态**的写请求(BLOCK-05)。
   *
   * 超时后说"失败了,请重试"是错的:后端可能已经签了这一件,而审计里
   * 一次批准会变成两次 —— 两个时间戳、同一个人、同一条记录,事后谁也说不清
   * 当时到底发生了什么。结果未知时先刷新,记录本身会直接给出答案。
   */
  const onWriteError = useWriteError(refreshDecision)

  const approve = useMutation({
    mutationFn: () =>
      reviewsApi.approve(id, { note: note || undefined, tags, candidate_id: selectedId || undefined }),
    onSuccess: () => afterDecision('已通过,任务转人工通过状态'),
    onError: onWriteError,
  })

  const reject = useMutation({
    mutationFn: () => reviewsApi.reject(id, { note: note || undefined, tags }),
    onSuccess: () => afterDecision('已驳回'),
    onError: onWriteError,
  })

  const regenerate = useMutation({
    mutationFn: () =>
      reviewsApi.regenerate(id, {
        note: note || undefined,
        tags,
        provider: regenProvider,
        extra_rounds: extraRounds,
      }),
    onSuccess: () => {
      setRegenOpen(false)
      afterDecision('已追加轮次并重新排队')
    },
    onError: onWriteError,
  })

  /**
   * 审核决策锁(BLOCK-08 / FE-REVIEW-DETAIL)。
   *
   * 三个按钮原来各自持有自己的 `isPending`,互不排斥 —— 点了「通过」再点
   * 「驳回」,两个请求同时在飞。后端的状态机会拒掉后到的那一个(MANUAL_REVIEW
   * 只能转出去一次),所以数据不会写坏;但**运营不知道最终生效的是哪一个**,
   * 而这三个动作的后果完全不同:通过会继续走格式化与导出,驳回是终态,
   * 重新生成会再花一轮 Provider 的钱。
   *
   * 一把锁盖住三个,而不是每个按钮各锁各的:它们是同一个决定的三个选项,
   * 不是三件可以并行的事。
   */
  const decisionBusy = approve.isPending || reject.isPending || regenerate.isPending

  if (query.isLoading) return <Skeleton active />
  if (query.isError || !review) {
    return <Alert type="error" showIcon message="打不开这条审核记录" description={readError(query.error)} />
  }

  const pending = review.status === 'PENDING'
  const selected =
    review.all_candidates.find((c) => c.id === selectedId) ?? review.round_best_candidates[0] ?? null
  const roundsWithCandidates = [...new Set(review.all_candidates.map((c) => c.round_number))]
    .sort((a, b) => a - b)

  const confirmReject = () =>
    modal.confirm({
      title: '驳回这个任务?',
      content: '驳回后任务进入「人工驳回」终态,不会再自动重生。需要重来请改用「重新生成」。',
      okText: '确认驳回',
      okButtonProps: { danger: true },
      cancelText: '再想想',
      // 失败要**关掉**弹窗。`mutateAsync` 的拒绝会被 antd 当成"再留一会儿",
      // 而留着的那个「确认驳回」正是结果未知时最不该再点一次的按钮。
      // 错误已经由 onWriteError 说过了,它是这件事唯一的出口
      onOk: async () => {
        try {
          await reject.mutateAsync()
        } catch {
          /* 已提示 */
        }
      },
    })

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space wrap>
        <Link to="/reviews">
          <Button size="small" icon={<ArrowLeftOutlined />}>返回队列</Button>
        </Link>
        <Tag color={REVIEW_STATUS_LABEL[review.status]?.color}>
          {REVIEW_STATUS_LABEL[review.status]?.text ?? review.status}
        </Tag>
        <Tooltip title={REVIEW_REASON_LABEL[review.reason]?.hint}>
          <Tag color={review.blocking ? 'warning' : 'default'}>
            {REVIEW_REASON_LABEL[review.reason]?.text ?? review.reason}
          </Tag>
        </Tooltip>
        <Button size="small" icon={<ReloadOutlined />} onClick={() => query.refetch()}>刷新</Button>
      </Space>

      {review.reason === 'SPOT_CHECK' && (
        <Alert
          type={selectedId && selectedId !== review.best_candidate_id ? 'warning' : 'info'}
          showIcon
          message="这是 A 档随机抽检,不阻塞任务"
          description={
            selectedId && selectedId !== review.best_candidate_id
              ? '注意:你选的不是系统当初采用的那张。通过之后会按新选的这张重出成品图,并替换网站上正在用的图(旧图停用,原链接仍可打开)。不想换图就把选择改回系统最佳候选,或直接驳回。'
              : '任务已经自动通过并继续走后续流程,这里只做事后复核。保持系统挑出的候选不变时,通过与驳回都不会动已经发布的图;改选另一张候选再通过,则会重出成品图并替换网站上的图。'
          }
        />
      )}

      {review.summary && <Alert type="warning" showIcon message={review.summary} />}

      <Card
        size="small"
        title={
          <Space size={8} wrap>
            <span>{`${review.product_sku} · ${review.product_name}`}</span>
            {/* §13.2:受众常驻。这一页判的是单张候选图能不能用,
                §13.4 第 5 条(受众一致且呈现为成年人)就在这里执行 */}
            <AudienceTag product={review} />
          </Space>
        }
      >
        {/* §13.3:标出需要重点检查的区域(按受众,见 §14)。
            放在评分明细之前 —— 它要在人看图之前被读到 */}
        <div style={{ marginBottom: 8 }}>
          <ReviewFocusLine product={review} />
        </div>
        <Descriptions size="small" column={4} bordered>
          <Descriptions.Item label="任务">
            <Link to={`/tasks/${review.task_id}`} className="mono">
              {review.task_id.slice(0, 8)}
            </Link>
          </Descriptions.Item>
          <Descriptions.Item label="任务状态">
            <Tag color={TASK_STATUS_LABEL[review.task_status as TaskStatus]?.color}>
              {TASK_STATUS_LABEL[review.task_status as TaskStatus]?.text ?? review.task_status}
            </Tag>
          </Descriptions.Item>
          <Descriptions.Item label="生成模式">
            {MODE_LABEL[review.task_mode as keyof typeof MODE_LABEL] ?? review.task_mode ?? '—'}
          </Descriptions.Item>
          <Descriptions.Item label="Provider">{review.provider}</Descriptions.Item>
          <Descriptions.Item label="生成轮次">
            {review.round_count} / {review.max_rounds}
          </Descriptions.Item>
          <Descriptions.Item label="Provider 调用">{review.attempt_count} 次</Descriptions.Item>
          <Descriptions.Item label="最佳候选">
            <GradeTag grade={review.best_grade} score={review.best_score} />
          </Descriptions.Item>
          <Descriptions.Item label="进队时间">
            {formatDateTime(review.created_at)}
          </Descriptions.Item>
          <Descriptions.Item label="累计硬错误" span={4}>
            {review.hard_fail_codes.length === 0 ? (
              <span style={{ color: brandVars.textFaint }}>无</span>
            ) : (
              <Space size={4} wrap>
                {review.hard_fail_codes.map((c) => (
                  <Tag key={c} color="error">
                    {HARD_FAIL_LABEL[c] ?? c}
                    <span className="mono" style={{ marginLeft: 4, opacity: 0.7 }}>{c}</span>
                  </Tag>
                ))}
              </Space>
            )}
          </Descriptions.Item>
          {review.decided_by && (
            <Descriptions.Item label="处理记录" span={4}>
              {review.decided_by} ·{' '}
              {formatDateTime(review.decided_at)}
              {review.decision_note ? ` · ${review.decision_note}` : ''}
              {review.manual_tags.length > 0 && (
                <Space size={4} wrap style={{ marginLeft: 8 }}>
                  {review.manual_tags.map((t) => <Tag key={t}>{t}</Tag>)}
                </Space>
              )}
            </Descriptions.Item>
          )}
        </Descriptions>
      </Card>

      <Row gutter={12}>
        <Col xs={24} lg={8}>
          <Card size="small" title="商品原图" style={{ height: '100%' }}>
            {review.product_assets.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有素材" />
            ) : (
              <div className="asset-grid asset-grid--review">
                <Image.PreviewGroup>
                  {review.product_assets.map((a) => (
                    <figure key={a.id} className="asset-tile" style={{ margin: 0 }}>
                      <Image
                        src={a.url}
                        alt={`商品原图 · ${ASSET_TYPE_LABEL[a.asset_type] ?? a.asset_type}`}
                      />
                      <figcaption>
                        {ASSET_TYPE_LABEL[a.asset_type] ?? a.asset_type}
                        <div className="mono" style={{ color: brandVars.textMuted }}>
                          {a.width ?? '—'}×{a.height ?? '—'}
                        </div>
                      </figcaption>
                    </figure>
                  ))}
                </Image.PreviewGroup>
              </div>
            )}
          </Card>
        </Col>

        <Col xs={24} lg={16}>
          <Card size="small" title="各轮最佳候选 · 点击查看评分">
            {review.round_best_candidates.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这个任务没有产出候选图" />
            ) : (
              <div className="asset-grid asset-grid--review">
                <Image.PreviewGroup>
                  {review.round_best_candidates.map((c) => (
                    <CandidateTile
                      key={c.id}
                      candidate={c}
                      selected={c.id === selectedId}
                      onSelect={() => setSelectedId(c.id)}
                    />
                  ))}
                </Image.PreviewGroup>
              </div>
            )}

            <Divider style={{ margin: '14px 0 10px' }} />
            <div className="section-label">
              选中候选的评分 {selected ? `· 第 ${selected.round_number} 轮 #${selected.candidate_index + 1}` : ''}
            </div>
            {/*
              * 分档阈值拉不到时说一句,不要让它静默消失。没有阈值时
              * `EvaluationDetail` 仍然显示分数,只是不再标出"这一项过没过" ——
              * 而那正是运营判断该不该驳回的依据
              */}
            {rules.isError && (
              <Typography.Text type="danger" style={{ fontSize: fontScale.meta }}>
                拉不到分档标准,下面只有分数、没有"过没过"的标注
              </Typography.Text>
            )}
            <EvaluationDetail
              evaluation={selected?.evaluation}
              thresholds={(rules.data ?? []).find((r) => r.is_active)?.thresholds}
            />
          </Card>
        </Col>
      </Row>

      <Card
        size="small"
        title="原图 / 候选图 对照"
        extra={
          <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
            上面选中哪张候选,这里就比哪张
          </Typography.Text>
        }
      >
        <CompareView
          originals={review.product_assets.map((a) => ({
            id: a.id,
            url: a.url,
            label: `${ASSET_TYPE_LABEL[a.asset_type] ?? a.asset_type} ${a.width ?? '—'}×${a.height ?? '—'}`,
          }))}
          candidate={
            selected
              ? {
                  id: selected.id,
                  url: selected.url,
                  label: `第 ${selected.round_number} 轮 #${selected.candidate_index + 1}`,
                }
              : null
          }
        />
      </Card>

      <Collapse
        size="small"
        items={[
          {
            key: 'all',
            label: `全部候选图 · ${review.all_candidates.length} 张(含被自动淘汰的)`,
            children: roundsWithCandidates.map((round) => (
              <div key={round} style={{ marginBottom: 16 }}>
                <div className="section-label">第 {round} 轮</div>
                <div className="asset-grid asset-grid--review">
                  <Image.PreviewGroup>
                    {review.all_candidates
                      .filter((c) => c.round_number === round)
                      .map((c) => (
                        <CandidateTile
                          key={c.id}
                          candidate={c}
                          selected={c.id === selectedId}
                          onSelect={() => setSelectedId(c.id)}
                        />
                      ))}
                  </Image.PreviewGroup>
                </div>
              </div>
            )),
          },
        ]}
      />

      <Card size="small" title="处理">
        {!pending && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message="这条记录已处理完毕,不能再次操作"
          />
        )}
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Input.TextArea
            rows={2}
            value={note}
            disabled={!pending}
            onChange={(e) => setNote(e.target.value)}
            placeholder="审核意见。写清楚判断依据,评分器校准会回看这些记录。"
            maxLength={2000}
            showCount
          />
          <Select
            mode="tags"
            disabled={!pending}
            style={{ width: '100%' }}
            placeholder="人工标签,可多选或自行输入"
            value={tags}
            onChange={setTags}
            options={TAG_PRESETS.map((t) => ({ value: t, label: t }))}
            maxCount={20}
          />
          <Space wrap>
            {/* 三个按钮共用 `decisionBusy`(BLOCK-08):任一动作在飞行中,
                另外两个一律不可点。`loading` 仍各归各的 —— 转圈要转在
                运营真正点下去的那个按钮上,否则他不知道是哪一个在跑 */}
            <Button
              type="primary"
              icon={<CheckOutlined />}
              disabled={!pending || !selectedId || decisionBusy}
              loading={approve.isPending}
              onClick={() => approve.mutate()}
            >
              通过选中候选
            </Button>
            <Button
              danger
              icon={<CloseOutlined />}
              disabled={!pending || decisionBusy}
              loading={reject.isPending}
              onClick={confirmReject}
            >
              驳回
            </Button>
            <Button
              icon={<ReloadOutlined />}
              disabled={!pending || decisionBusy}
              loading={regenerate.isPending}
              onClick={() => {
                setRegenProvider(review.provider)
                setRegenOpen(true)
              }}
            >
              重新生成 / 切换 Provider
            </Button>
            <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
              通过时采用上方选中的那张候选图;不选则采用系统挑出的最佳候选。
            </Typography.Text>
          </Space>
        </Space>
      </Card>

      <Modal
        open={regenOpen}
        title="重新生成"
        okText="追加轮次并排队"
        cancelText="取消"
        confirmLoading={regenerate.isPending}
        onCancel={() => setRegenOpen(false)}
        onOk={() => regenerate.mutate()}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="任务是轮次耗尽才进的队列"
          description="不追加轮次的话,它会立刻再次弹回审核队列。"
        />
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <div>
            <div className="section-label">Provider</div>
            <Select
              style={{ width: '100%' }}
              value={regenProvider}
              onChange={setRegenProvider}
              status={providers.isError ? 'error' : undefined}
              options={(providers.data ?? []).map((p) => ({
                value: p.name,
                label: providerOptionLabel(p),
                disabled: !isProviderSelectable(p),
              }))}
            />
            {/*
              * 拉不到 Provider 列表时不许静默给一个空下拉(FE-REVIEW-DETAIL-05 /
              * 阶段 A 验收第 6 条)。空下拉读起来是"没有可用的 Provider",而重新
              * 生成**是要花钱的**:运营会以为这次点不动是配置问题,转头去改设置。
              * 更要紧的是 `isProviderSelectable` 这道拦截也跟着失效了 ——
              * 列表为空时它一条都拦不到,不是"拦住了"。
              */}
            {providers.isError && (
              <Typography.Text type="danger" style={{ fontSize: fontScale.meta }}>
                拉不到 Provider 列表(不是"没有可用 Provider")。这次重新生成会用任务原来的 Provider
              </Typography.Text>
            )}
          </div>
          <div>
            <div className="section-label">追加轮次</div>
            <InputNumber
              min={1}
              max={5}
              value={extraRounds}
              onChange={(v) => setExtraRounds(v ?? 1)}
              style={{ width: '100%' }}
            />
          </div>
        </Space>
      </Modal>
    </Space>
  )
}
