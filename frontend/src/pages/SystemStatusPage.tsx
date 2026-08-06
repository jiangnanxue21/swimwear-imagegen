import { Alert, Badge, Card, Descriptions, Skeleton, Space } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { apiClient, readError } from '../api/client'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

interface Readiness {
  status: 'ok' | 'degraded'
  checks: Record<string, string>
}

const LABELS: Record<string, string> = {
  database: 'PostgreSQL',
  redis: 'Redis',
  storage: '本地存储目录',
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

  return (
    <Space direction="vertical" size={16} style={{ width: '100%', maxWidth: 760 }}>
      <PageHeader
        title="系统状态"
        subtitle="确认后端进程、数据库、队列与存储目录都能连通,再进入商品与素材录入"
      />

      {health.isError && (
        <Alert type="error" showIcon message="后端未响应" description={readError(health.error)} />
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
    </Space>
  )
}
