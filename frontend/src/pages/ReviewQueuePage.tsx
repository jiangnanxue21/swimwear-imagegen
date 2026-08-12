import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert, Button, Card, Collapse, Descriptions, Empty, Radio, Select, Skeleton,
  Space, Table, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { evaluationApi, reviewsApi } from '../api/reviews'
import { readError } from '../api/client'
import GradeTag from '../components/GradeTag'
import { AudienceTag } from '../components/AudienceBadge'
import {
  DIMENSION_LABEL, HARD_FAIL_LABEL, REVIEW_REASON_LABEL, REVIEW_STATUS_LABEL,
  type Review, type ReviewReason, type ReviewStatus,
} from '../api/types'
import { brandVars, fontScale } from '../theme'
import { formatDateTime } from '../utils/datetime'
import { useServerSort } from '../hooks/useServerSort'
import { useUrlFilters, enumParam, intParam, oneOfParam } from '../hooks/useUrlFilters'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'

const STATUS_TABS: { value: ReviewStatus; label: string }[] = [
  { value: 'PENDING', label: '待处理' },
  { value: 'APPROVED', label: '已通过' },
  { value: 'REJECTED', label: '已驳回' },
  { value: 'REGENERATION_REQUESTED', label: '已转重生' },
]

/** 后端 REVIEW_SORTABLE 的子集。子集关系由 `test_a45_batch14_17_url_filters.py`
 *  盯着 —— 加一档不在白名单的键时守卫会红。 */
const LIST_SORT_KEYS = [
  'created_at', 'best_score', 'round_count', 'attempt_count', 'status',
] as const

/** 分档标准面板。运营对着它就能自己回答「这张为什么被判 C」,不用来问工程。 */
function RuleSetPanel() {
  const rules = useQuery({ queryKey: ['rule-sets'], queryFn: evaluationApi.ruleSets })
  const evaluators = useQuery({ queryKey: ['evaluators'], queryFn: evaluationApi.evaluators })

  const active = (rules.data ?? []).find((r) => r.is_active) ?? rules.data?.[0]
  if (rules.isLoading) return <Skeleton active paragraph={{ rows: 3 }} />
  /*
   * 拉不到规则集**不等于**没有规则集(FE-REVIEW-QUEUE-02 / 阶段 A 验收第 6 条)。
   *
   * 原来这两种情况共用一个 <Empty>,文案是「没有规则集,后端将使用代码默认值」
   * —— 那是一句**关于后端行为的事实陈述**,而查询失败时前端并不知道后端
   * 在用什么。运营照着这句话会认为界面上那些分档阈值不生效,进而怀疑
   * 每一张被判 C 的图。判定失败要说成失败。
   */
  if (rules.isError) {
    return (
      <ErrorNotice
        title="拉不到分档标准"
        error={rules.error}
        onRetry={() => rules.refetch()}
        retrying={rules.isFetching}
      />
    )
  }
  if (!active) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有规则集,后端将使用代码默认值" />

  const th = active.thresholds ?? {}
  const num = (key: string, fallback: number) =>
    typeof th[key] === 'number' ? (th[key] as number) : fallback
  const flag = (key: string, fallback: boolean) =>
    typeof th[key] === 'boolean' ? (th[key] as boolean) : fallback

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
    <PageHeader title="图片人工审核" subtitle="评分没过关的候选图。审的是单张图,不是整个图片集" />
      <Descriptions size="small" column={4} bordered>
        <Descriptions.Item label="规则集">
          {active.name} v{active.version}
        </Descriptions.Item>
        <Descriptions.Item label="A 档总分">≥ {num('a_min_overall', 85)}</Descriptions.Item>
        <Descriptions.Item label="B 档总分">≥ {num('b_min_overall', 70)}</Descriptions.Item>
        <Descriptions.Item label="C 档总分">≥ {num('c_min_overall', 55)}</Descriptions.Item>
        <Descriptions.Item label="商品身份一致性">≥ {num('a_min_garment_identity', 90)}</Descriptions.Item>
        <Descriptions.Item label="结构一致性">≥ {num('a_min_structural_fidelity', 90)}</Descriptions.Item>
        <Descriptions.Item label="人体真实性">≥ {num('a_min_anatomy_realism', 85)}</Descriptions.Item>
        <Descriptions.Item label="网站可用性">≥ {num('a_min_website_usability', 85)}</Descriptions.Item>
        <Descriptions.Item label="轮内预排序">
          {flag('prescreen_enabled', true) ? `开启,完整评分前 ${num('full_evaluation_count', 2)} 名` : '关闭,全部完整评分'}
        </Descriptions.Item>
        <Descriptions.Item label="A 档抽检比例">
          {Math.round(num('spot_check_ratio', 0.1) * 100)}%
        </Descriptions.Item>
        <Descriptions.Item label="允许切换 Provider">
          {flag('allow_provider_switch', true) ? '是' : '否'}
        </Descriptions.Item>
        <Descriptions.Item label="评分器">
          {/*
            * 评分器查询失败时不铺一排空标签(FE-REVIEW-QUEUE-03)。空列表读起来是
            * 「一个评分器都没有」,而那意味着所有图都走不到评分 —— 与"这一格没拉到"
            * 是完全不同的两件事。这里不用 ErrorNotice:它在 Descriptions 的一格里,
            * 撑不下折叠面板;一句话 + 页面上方那条 ErrorNotice 已经够定位
            */}
          {evaluators.isError ? (
            <Typography.Text type="danger" style={{ fontSize: fontScale.meta }}>
              拉不到评分器列表(不是"没有评分器")
            </Typography.Text>
          ) : (
            <Space size={4} wrap>
              {(evaluators.data ?? []).map((e) => (
                <Tag key={e.name} color={e.active ? 'success' : e.configured ? 'processing' : 'default'}>
                  {e.display_name}
                  {e.active ? ' · 生效中' : e.configured ? '' : ' · 未配置'}
                </Tag>
              ))}
            </Space>
          )}
        </Descriptions.Item>
      </Descriptions>

      <div>
        <div className="section-label">总分权重</div>
        <Space size={4} wrap>
          {Object.entries(active.weights ?? {}).map(([dim, weight]) => (
            <Tag key={dim}>
              {DIMENSION_LABEL[dim] ?? dim} {Math.round(weight * 100)}%
            </Tag>
          ))}
        </Space>
        <Typography.Paragraph type="secondary" style={{ fontSize: fontScale.body, marginTop: 8, marginBottom: 0 }}>
          总分由后端按上表权重算出,不采信评分器自报的那个数。规则集只读,改规则请走迁移或运维脚本。
        </Typography.Paragraph>
      </div>
    </Space>
  )
}

export default function ReviewQueuePage() {
  useDocumentTitle('图片人工审核')

  /**
   * 筛选与排序都住 URL 里(GAP-033)。这一页 4 项筛选 + 2 项排序,全在这张表里 ——
   * 加筛选项 = 往这张表加一行,而不是再开一个 useState。
   *
   * PENDING 那一档是默认,刷新之后仍然停在 PENDING;切到「已驳回」再贴链接给同事,
   * 他打开也是「已驳回」。这一页的评审筛选项(comment by REVIEW III.6 之后)
   * 仍然在 —— useUrlFilters 接进来时一并搬了。
   */
  const PAGE_SIZES = [10, 20, 50, 100] as const
  const pageSizeParam = oneOfParam(20, PAGE_SIZES)

  const filters = useUrlFilters({
    status: enumParam<ReviewStatus>(['PENDING', 'APPROVED', 'REJECTED', 'REGENERATION_REQUESTED']),
    reason: enumParam<ReviewReason>(Object.keys(REVIEW_REASON_LABEL) as ReviewReason[]),
    page: intParam(1, { min: 1 }),
    page_size: pageSizeParam,
    sort: enumParam<string>(LIST_SORT_KEYS),
    order: enumParam<'asc' | 'desc'>(['asc', 'desc']),
  })
  const { status, reason, page, page_size: pageSize } = filters.values

  // 服务端排序。这张表上最值钱的排序维度是「最佳候选」的分数:
  // 队列长的时候,先看接近门槛的那批(改一版就能过)比从头翻更省时间。
  // best_score 可空(一张候选图都没跑出来的项),后端把空值压到最后 ——
  // 「没有分」不是「分很低」
  const sort = useServerSort(
    { sort: 'created_at', order: 'desc' },
    undefined,
    {
      value: {
        sort: filters.values.sort ?? 'created_at',
        order: filters.values.order ?? 'desc',
      },
      set: (next) => filters.patch({ sort: next.sort, order: next.order, page: 1 }),
    },
  )

  const query = useQuery({
    queryKey: ['reviews', status, reason, page, pageSize, sort.params],
    queryFn: () => reviewsApi.list({ status, reason, ...sort.params, page, page_size: pageSize }),
    refetchInterval: status === 'PENDING' ? 15_000 : false,
  })

  const columns: ColumnsType<Review> = [
    {
      title: '商品',
      dataIndex: 'product_sku',
      render: (sku: string, row) => (
        <Space direction="vertical" size={0}>
          <Space size={6}>
            <Link to={`/products/${row.product_id}`} className="mono">{sku || '—'}</Link>
            {/* §13.2:受众在**决定先审哪一件**时就要看得见 —— 男装与女装
                的检查项完全不同(§14.1 / §14.2),排队时心里要有数 */}
            <AudienceTag product={row} />
          </Space>
          <span style={{ fontSize: fontScale.meta, color: brandVars.slate }}>{row.product_name}</span>
        </Space>
      ),
    },
    {
      title: '进队原因',
      dataIndex: 'reason',
      width: 150,
      render: (r: ReviewReason, row) => (
        <Tooltip title={REVIEW_REASON_LABEL[r]?.hint}>
          <Space size={4}>
            <Tag color={row.blocking ? 'warning' : 'default'}>
              {REVIEW_REASON_LABEL[r]?.text ?? r}
            </Tag>
            {!row.blocking && <Tag>不阻塞</Tag>}
          </Space>
        </Tooltip>
      ),
    },
    {
      title: '最佳候选',
      dataIndex: 'best_grade',
      key: 'best_score',
      width: 130,
      sorter: true,
      sortOrder: sort.orderFor('best_score'),
      render: (_: unknown, row) => <GradeTag grade={row.best_grade} score={row.best_score} />,
    },
    {
      title: '硬错误',
      dataIndex: 'hard_fail_codes',
      width: 170,
      render: (codes: string[]) =>
        codes.length === 0 ? (
          <span style={{ color: brandVars.textMuted }}>无</span>
        ) : (
          <Space size={2} wrap>
            {codes.slice(0, 2).map((c) => (
              <Tooltip key={c} title={c}>
                <Tag color="error">{HARD_FAIL_LABEL[c] ?? c}</Tag>
              </Tooltip>
            ))}
            {codes.length > 2 && <Tag>+{codes.length - 2}</Tag>}
          </Space>
        ),
    },
    {
      title: 'Provider',
      dataIndex: 'provider',
      key: 'provider',
      width: 90,
    },
    {
      title: '生成次数',
      dataIndex: 'round_count',
      key: 'round_count',
      width: 90,
      align: 'center',
      sorter: true,
      sortOrder: sort.orderFor('round_count'),
      render: (rounds: number, row) => (
        <Tooltip title={`${rounds} 轮 / ${row.attempt_count} 次 Provider 调用`}>
          <span className="mono">{rounds} 轮 · {row.attempt_count} 次</span>
        </Tooltip>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      sorter: true,
      sortOrder: sort.orderFor('status'),
      render: (s: ReviewStatus) => (
        <Tag color={REVIEW_STATUS_LABEL[s]?.color}>{REVIEW_STATUS_LABEL[s]?.text ?? s}</Tag>
      ),
    },
    {
      title: '进队时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 140,
      sorter: true,
      sortOrder: sort.orderFor('created_at'),
      render: (v: string) => formatDateTime(v),
    },
    {
      title: '',
      key: 'action',
      width: 90,
      render: (_: unknown, row) => (
        <Link to={`/reviews/${row.id}`}>
          <Button size="small" type={row.status === 'PENDING' ? 'primary' : 'default'}>
            {row.status === 'PENDING' ? '审核' : '查看'}
          </Button>
        </Link>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="审核对象是商品任务,不是每一张低分候选图"
        description="低分与硬错误候选会被自动淘汰并重新生成;只有轮次(和可用 Provider)都耗尽仍拿不到 A 档的任务,才会出现在这里。"
      />

      <Card
        size="small"
        title="人工审核队列"
        extra={
          <Space>
            <Select<ReviewReason>
              allowClear
              size="small"
              style={{ width: 170 }}
              placeholder="按进队原因筛选"
              value={reason}
              onChange={(v) => filters.patch({ reason: v, page: 1 })}
              options={Object.entries(REVIEW_REASON_LABEL).map(([value, meta]) => ({
                value: value as ReviewReason, label: meta.text,
              }))}
            />
            <Button size="small" icon={<ReloadOutlined />} onClick={() => query.refetch()}>
              刷新
            </Button>
          </Space>
        }
      >
        <Radio.Group
          size="small"
          value={status}
          onChange={(e) => filters.patch({ status: e.target.value, page: 1 })}
          style={{ marginBottom: 12 }}
        >
          {STATUS_TABS.map((t) => (
            <Radio.Button key={t.value} value={t.value}>{t.label}</Radio.Button>
          ))}
        </Radio.Group>

        {query.isError ? (
          <Alert type="error" showIcon message="读不到审核队列" description={readError(query.error)} />
        ) : (
          <Table<Review>
            rowKey="id"
            size="small"
            bordered
            loading={query.isLoading}
            columns={columns}
            dataSource={query.data?.items ?? []}
            onChange={sort.onTableChange}
            pagination={{
              current: page,
              pageSize: query.data?.page_size ?? pageSize,
              total: query.data?.total ?? 0,
              onChange: (p, ps) => filters.patch({ page: p, page_size: pageSizeParam.narrow(ps) }),
              showTotal: (t) => `共 ${t} 条`,
              size: 'small',
            }}
            locale={{
              emptyText: (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={status === 'PENDING' ? '队列是空的,没有需要人工介入的任务' : '没有这个状态的记录'}
                />
              ),
            }}
          />
        )}
      </Card>

      <Collapse
        size="small"
        items={[{ key: 'rules', label: '当前分档标准与评分器(只读)', children: <RuleSetPanel /> }]}
      />
    </Space>
  )
}