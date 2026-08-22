import { Badge, Card, Descriptions, Skeleton, Space, Tag, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { apiClient } from '../api/client'
import { environmentApi, type ChannelRow } from '../api/environment'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { fontScale, space } from '../theme'

interface Readiness {
  status: 'ok' | 'degraded'
  checks: Record<string, string>
}

const LABELS: Record<string, string> = {
  database: 'PostgreSQL',
  redis: 'Redis',
  storage: '本地存储目录',
}

/**
 * 发送端三档的呈现。**只管颜色和措辞,不管判定** —— 判定在后端
 * `channels/registry.py::transport_kind()`,三档而不是布尔:
 * `is_simulator=false` 同时表示"真实发送端"与"根本没有发送端",
 * 而这两件事在状态页上是完全不同的一句话。
 *
 * 色值取**语义档**(success / warning / error),不取调色板预设名 ——
 * 后者不受 `theme.ts` 控制,会造成色值漂移(见 `BrandTag` 的模块注释)。
 */
const TRANSPORT_TONE: Record<string, { color: string; text: string }> = {
  REAL: { color: 'success', text: '真实发送端' },
  SIMULATOR: { color: 'warning', text: '模拟器' },
  NONE: { color: 'error', text: '没有发送端' },
}

/**
 * 解锁闸三档的呈现。同上,判定在后端 `channels/shein/readiness.py`。
 * 没有这一格的渠道(没接闸的)不显示,**不显示成"未知"** ——
 * "这个渠道没有闸"和"闸说不知道"是两件事。
 */
const MODE_TONE: Record<string, { color: string; text: string }> = {
  REAL: { color: 'success', text: '允许真实写操作' },
  FIXTURE_ONLY: { color: 'warning', text: '只允许 fixture / mock' },
  BLOCKED: { color: 'error', text: '连 fixture 都不该跑' },
}

/** 一个渠道一段。后端给什么显示什么,前端不筛选、不改写、不补默认值。 */
function ChannelFacts({ row }: { row: ChannelRow }) {
  const transport = TRANSPORT_TONE[row.transport_kind]
  const mode = row.mode ? MODE_TONE[row.mode] : undefined
  const reasons = row.blocking_reasons ?? []
  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Space size={4} wrap>
        <Tag color={transport?.color}>{transport?.text ?? row.transport_kind}</Tag>
        {row.transport && <span className="mono">{row.transport}</span>}
        {mode && <Tag color={mode.color}>{mode.text}</Tag>}
        <Tag color={row.spec_complete === true ? 'success' : 'default'}>
          {row.spec_complete === null
            ? '字段规格读不出来'
            : row.spec_complete
              ? '字段规格已确认'
              : '字段规格还有未确认项'}
        </Tag>
        {row.sources_unverified !== undefined && row.sources_unverified > 0 && (
          <Tag color="default">{row.sources_unverified} 页官方文档未核对</Tag>
        )}
      </Space>
      {reasons.length > 0 && (
        <details>
          {/* 默认收起:23 条理由摊开会把这一页压成一堵墙,而运营多数时候
              只需要知道"接没接"。要查为什么时点开 —— 一条都不省略,
              省略会让"还差什么"变成一次猜测 */}
          <summary style={{ cursor: 'pointer', fontSize: fontScale.body }}>
            还差 {reasons.length} 件事
          </summary>
          <ul style={{ margin: `${space.xs}px 0 0`, paddingLeft: space.lg }}>
            {reasons.map((reason) => (
              <li key={reason}>
                <Typography.Text style={{ fontSize: fontScale.body }}>{reason}</Typography.Text>
              </li>
            ))}
          </ul>
        </details>
      )}
    </Space>
  )
}

export default function SystemStatusPage() {
  useDocumentTitle('系统状态')
  const health = useQuery({
    queryKey: ['health'],
    queryFn: async () => (await apiClient.get('/health')).data,
  })
  const ready = useQuery({
    queryKey: ['readiness'],
    queryFn: async () => (await apiClient.get<Readiness>('/health/ready')).data,
    refetchInterval: 15_000,
  })
  /*
   * 渠道接入。与顶部的环境状态条共用同一个 query key —— 同一份事实拉两次的话,
   * 横幅说的和这一页说的会在刷新之间对不上,而运营会以为其中一处坏了。
   */
  const environment = useQuery({
    queryKey: ['environment'],
    queryFn: environmentApi.read,
  })

  return (
    <Space direction="vertical" size={16} style={{ width: '100%', maxWidth: 760 }}>
      <PageHeader
        title="系统状态"
        subtitle="确认后端进程、数据库、队列与存储目录都能连通,再进入商品与素材录入"
      />

      {health.isError && (
        <ErrorNotice
          title="后端未响应"
          error={health.error}
          onRetry={() => health.refetch()}
          retrying={health.isFetching}
        />
      )}

      <Card size="small" title="服务进程">
        {health.isLoading ? (
          <Skeleton active paragraph={{ rows: 2 }} />
        ) : (
          <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label="应用">{health.data?.app ?? '-'}</Descriptions.Item>
            <Descriptions.Item label="环境">{health.data?.env ?? '-'}</Descriptions.Item>
            <Descriptions.Item label="状态">
              <Badge status={health.data ? 'success' : 'error'} text={health.data?.status ?? '未知'} />
            </Descriptions.Item>
          </Descriptions>
        )}
      </Card>

      <Card size="small" title="依赖组件">
        {/*
          * `/health/ready` 拉不到时**不许渲染成一张空的依赖表**(FE-STATUS-02)。
          * 空表读起来是"没有依赖组件",而这一页存在的唯一理由就是回答
          * "数据库、Redis、存储通没通"。拉不到本身已经是一条强信号:
          * 后端要么没起来,要么起来了但答不了 —— 两种都不该显示成一片空白。
          */}
        {ready.isError ? (
          <ErrorNotice
            title="拉不到依赖组件状态"
            error={ready.error}
            onRetry={() => ready.refetch()}
            retrying={ready.isFetching}
          />
        ) : ready.isLoading ? (
          <Skeleton active paragraph={{ rows: 3 }} />
        ) : (
          <Descriptions size="small" column={1} bordered>
            {Object.entries(ready.data?.checks ?? {}).map(([key, value]) => (
              <Descriptions.Item key={key} label={LABELS[key] ?? key}>
                <Badge
                  status={value === 'ok' ? 'success' : 'error'}
                  text={value === 'ok' ? '已连通' : value}
                />
              </Descriptions.Item>
            ))}
          </Descriptions>
        )}
      </Card>

      <Card size="small" title="渠道接入">
        {/*
          * 「SHEIN 到底接没接」在别处问不出来:环境状态条只有一个总档位,
          * 而它答的是"整体可不可信"。这一格逐渠道给答案,每一项都来自后端
          * 注册表与解锁闸实际算出来的值(硬规则 4:前端不推测状态)。
          */}
        {environment.isError ? (
          <ErrorNotice
            title="拉不到渠道接入状态"
            error={environment.error}
            onRetry={() => environment.refetch()}
            retrying={environment.isFetching}
          />
        ) : environment.isLoading ? (
          <Skeleton active paragraph={{ rows: 3 }} />
        ) : (
          <Descriptions size="small" column={1} bordered>
            {(environment.data?.channels ?? []).map((row) => (
              <Descriptions.Item key={row.channel} label={row.channel}>
                <ChannelFacts row={row} />
              </Descriptions.Item>
            ))}
          </Descriptions>
        )}
      </Card>
    </Space>
  )
}
