/**
 * 操作审计查询(FE-307)。
 *
 * 验收结果:**可追溯到用户和时间**。写入在阶段 2 已由 BE-206 完成
 * (FE-204/205 的验收就依赖它),这一页只读。
 *
 * 读的难点不是查询,是**读得懂**:
 *
 *     `ListingDraft` 对运营没有意义,「上架草稿」有
 *     CREATE / UPDATE 太粗,真正发生的事写在 payload.action 里(export / approve…)
 *     一行一句人话,而不是让人读 JSON
 *
 * 三张翻译表都在后端(`app/workbench/audit_view.py`),出参里直接带
 * `action_label` / `entity_label` / `summary`。前端这里的两张表只用于筛选下拉
 * ——它要在拉到数据之前就能列出全部取值。
 *
 * 「只看上架闭环」默认开着:审计表里混着生成任务的状态流转,而这一页要查的是
 * 关键修改、批准与导出记录。这是降噪,不是权限。
 */
import { useEffect, useState } from 'react'
import { Button, Card, Empty, Input, Select, Space, Switch, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { readError } from '../api/client'
import {
  AUDIT_ACTION_LABEL,
  AUDIT_ENTITY_LABEL,
  batchApi,
  type AuditEntry,
} from '../api/batch'
import { brandVars, fontScale } from '../theme'
import { formatDateTime } from '../utils/datetime'
import {
  enumParam, flagParam, intParam, narrowOneOf, oneOfParam, textParam, useUrlFilters,
} from '../hooks/useUrlFilters'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import PageHeader from '../components/PageHeader'

const DAY_OPTIONS = [
  { value: 7, label: '最近 7 天' },
  { value: 30, label: '最近 30 天' },
  { value: 90, label: '最近 90 天' },
]
const DAY_VALUES = DAY_OPTIONS.map((o) => o.value)

/**
 * 每页条数的档位。上限 200 对齐后端 `app/api/workbench_batch.py` 的
 * `page_size: int = Query(default=50, ge=1, le=200)`,守卫盯着「≤ 后端 le=」。
 */
const PAGE_SIZES = [20, 50, 100, 200] as const
/** 默认档位。`oneOfParam` 的 fallback 与 antd 回传收窄时的 fallback 共用这一个。 */
const DEFAULT_PAGE_SIZE = 50

export default function AuditLogPage() {
  useDocumentTitle('操作审计')
  /**
   * 顶栏「我的操作记录」带着 `?actor=<操作人名>` 过来(走查 P1-1)。
   *
   * 这一页原本不读 URL,于是那个链接会落到一张**未筛选**的全量审计页 ——
   * 界面承诺了「我的」,给出的却是「所有人的」。这和走查 P0-3 批评搜索框的
   * 是同一类毛病:界面说了一件没发生的事。
   *
   * 操作人名是开放集合(来自后端 OPERATOR_TOKENS 配置),所以不给白名单 ——
   * 查一个不存在的操作人得到一张空列表,那本来就是正确答案。
   *
   * GAP-033:这一页的七个筛选项现在全在 URL 上。从前是"读一次初值然后擦掉",
   * 于是「我的操作记录」这个链接刷新一次就变回全站记录。
   */
  const filters = useUrlFilters({
    actor: textParam(),
    entity_type: enumParam<string>(Object.keys(AUDIT_ENTITY_LABEL)),
    action: enumParam<string>(Object.keys(AUDIT_ACTION_LABEL)),
    days: oneOfParam(30, DAY_VALUES),
    // 默认**开着**,所以 URL 上出现的是关掉它的那一次(`?workbench_only=0`)。
    // 这正是 flagParam 要能带默认值的理由:退回 false 会悄悄把降噪关掉
    workbench_only: flagParam(true),
    page: intParam(1, { min: 1 }),
    page_size: oneOfParam(DEFAULT_PAGE_SIZE, PAGE_SIZES),
  })
  const {
    actor, entity_type: entityType, action, days,
    workbench_only: workbenchOnly, page, page_size: pageSize,
  } = filters.values

  /** 搜索框里的字。与已生效的 `actor` 分开(理由同走查 P0-3) */
  const [actorDraft, setActorDraft] = useState(actor)

  // 生效的那一份在 URL 上,框里的字是本地的 —— 刷新/后退之后要同步回来,
  // 否则框里留着名字、列表却是全量
  useEffect(() => {
    setActorDraft(actor)
  }, [actor])

  const query = useQuery({
    queryKey: ['audit', { actor, entityType, action, days, workbenchOnly, page, pageSize }],
    queryFn: () =>
      batchApi.audit({
        actor: actor || undefined,
        entity_type: entityType,
        action,
        days,
        workbench_only: workbenchOnly,
        page,
        page_size: pageSize,
      }),
  })

  const columns: ColumnsType<AuditEntry> = [
    {
      title: '时间',
      key: 'at',
      width: 165,
      render: (_, row) => (
        <span className="mono" style={{ fontSize: fontScale.meta }}>
          {formatDateTime(row.at)}
        </span>
      ),
    },
    {
      title: '操作人',
      key: 'actor',
      width: 110,
      render: (_, row) => (
        <Tooltip title="操作人只从验证通过的凭据里取,不来自请求头 —— 伪造不了">
          <Tag>{row.actor ?? 'system'}</Tag>
        </Tooltip>
      ),
    },
    {
      title: '动作',
      key: 'action',
      width: 130,
      render: (_, row) => <Tag color="processing">{row.action_label}</Tag>,
    },
    {
      title: '对象',
      key: 'entity',
      width: 120,
      render: (_, row) => <span>{row.entity_label}</span>,
    },
    {
      title: '摘要',
      key: 'summary',
      render: (_, row) => <span style={{ fontSize: fontScale.body }}>{row.summary}</span>,
    },
    {
      title: '对象 ID',
      key: 'entity_id',
      width: 110,
      render: (_, row) =>
        row.entity_id ? (
          <Tooltip title={row.entity_id}>
            <span className="mono" style={{ fontSize: fontScale.meta }}>
              {row.entity_id.slice(0, 8)}
            </span>
          </Tooltip>
        ) : (
          <span style={{ color: brandVars.textFaint }}>—</span>
        ),
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader
        title="操作审计"
        subtitle={actor ? `只看操作人「${actor}」` : '谁在什么时候改了什么'}
      />

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="操作人"
            style={{ width: 160 }}
            value={actorDraft}
            onChange={(e) => setActorDraft(e.target.value)}
            onSearch={(value) => filters.patch({ actor: value, page: 1 })}
          />
          <Select<string>
            allowClear
            placeholder="对象类型"
            style={{ width: 150 }}
            value={entityType}
            onChange={(value) => filters.patch({ entity_type: value, page: 1 })}
            options={Object.entries(AUDIT_ENTITY_LABEL).map(([value, label]) => ({
              value,
              label,
            }))}
          />
          <Select<string>
            allowClear
            placeholder="业务动作"
            style={{ width: 150 }}
            value={action}
            onChange={(value) => filters.patch({ action: value, page: 1 })}
            options={Object.entries(AUDIT_ACTION_LABEL).map(([value, label]) => ({
              value,
              label,
            }))}
          />
          <Select<number>
            style={{ width: 130 }}
            value={days}
            onChange={(value) => filters.patch({ days: value, page: 1 })}
            options={DAY_OPTIONS}
          />
          <Space size={6}>
            <Switch
              size="small"
              checked={workbenchOnly}
              onChange={(value) => filters.patch({ workbench_only: value, page: 1 })}
            />
            <Tooltip title="只看商品、属性、素材、图片集、文案、草稿与批量任务。关掉会带上生成任务的状态流转,那是技术后台关心的东西">
              <span style={{ fontSize: fontScale.body }}>只看上架闭环</span>
            </Tooltip>
          </Space>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => query.refetch()}
            loading={query.isFetching}
          >
            刷新
          </Button>
        </Space>
      </Card>

      {query.isError && (
        <Card size="small">
          <Empty description={`拉不到审计记录:${readError(query.error)}`} />
        </Card>
      )}

      <Table<AuditEntry>
        rowKey="id"
        size="small"
        bordered
        columns={columns}
        dataSource={query.isError ? [] : query.data?.items ?? []}
        loading={query.isFetching}
        expandable={{
          expandedRowRender: (row) => (
            <pre style={{ margin: 0, fontSize: fontScale.meta, whiteSpace: 'pre-wrap' }}>
              {JSON.stringify(row.payload, null, 2)}
            </pre>
          ),
          rowExpandable: (row) => Object.keys(row.payload).length > 0,
        }}
        pagination={{
          current: page,
          pageSize,
          total: query.data?.total ?? 0,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZES.map(String),
          showTotal: (total) => `共 ${total} 条`,
          // antd 回传的是 number,而 codec 只认 PAGE_SIZES 那几档;
          // 收窄走 narrowOneOf,和 codec 用同一个 fallback
          onChange: (nextPage, nextSize) =>
            filters.patch({
              page: nextPage,
              page_size: narrowOneOf(nextSize, PAGE_SIZES, DEFAULT_PAGE_SIZE),
            }),
        }}
        locale={{
          emptyText: <Empty description="这个时间范围内没有匹配的操作记录。" />,
        }}
      />
    </Space>
  )
}
