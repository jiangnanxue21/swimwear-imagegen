import { App, Button, Card, Space, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { providersApi } from '../api/generation'
import { readError } from '../api/client'
import { MODE_LABEL, type GenerationMode, type Provider } from '../api/types'
import BrandTag from '../components/BrandTag'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

export default function ProvidersPage() {
  useDocumentTitle('出图服务商')
  const { message } = App.useApp()
  const query = useQuery({ queryKey: ['providers'], queryFn: providersApi.list })

  const test = useMutation({
    mutationFn: providersApi.test,
    onSuccess: (result) => {
      const notice = result.configured ? message.info : message.warning
      const model = result.tryon_model ? `(模型 ${result.tryon_model})` : ''
      notice(`${result.provider}:${result.message}${model}`)
    },
    onError: (err) => message.error(readError(err)),
  })

  const columns: ColumnsType<Provider> = [
    { title: '名称', dataIndex: 'display_name' },
    {
      title: '标识',
      dataIndex: 'name',
      width: 100,
      render: (v: string) => <span className="mono">{v}</span>,
    },
    {
      title: '配置',
      dataIndex: 'configured',
      width: 100,
      render: (v: boolean) => (
        <Tag color={v ? 'success' : 'default'}>{v ? '已配置' : '未配置'}</Tag>
      ),
    },
    {
      title: '可用性',
      dataIndex: 'implemented',
      width: 140,
      render: (v: boolean) =>
        v ? <BrandTag tone="accent">可提交任务</BrandTag> : (
          <Tooltip title="接口骨架已就位,请求映射待按官方文档实现">
            <Tag>待接入</Tag>
          </Tooltip>
        ),
    },
    {
      title: '支持模式',
      width: 220,
      render: (_, row) => (
        <Space size={4} wrap>
          {row.capabilities.supported_modes.map((m) => (
            <Tag key={m}>{MODE_LABEL[m as GenerationMode] ?? m}</Tag>
          ))}
        </Space>
      ),
    },
    {
      title: '能力',
      render: (_, row) => (
        <Space size={4} wrap>
          {row.capabilities.supports_multiple_candidates && (
            <Tag>多候选 ≤{row.capabilities.max_candidates_per_call}</Tag>
          )}
          {row.capabilities.supports_seed && <Tag>随机种子</Tag>}
          {row.capabilities.supports_cancel && <Tag>可取消</Tag>}
          {row.capabilities.supports_webhook && <Tag>回调通知</Tag>}
        </Space>
      ),
    },
    {
      title: '操作',
      width: 100,
      render: (_, row) => (
        <Button size="small" loading={test.isPending} onClick={() => test.mutate(row.name)}>
          测试连接
        </Button>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader title="出图服务商" subtitle="查看各项出图服务的能力和配置状态（只读）" />
      <Card size="small" styles={{ body: { padding: 12 } }}>
        此页面仅供查看。密钥与模型请在 <Link to="/settings">设置</Link> 页填写，
        或写进 <span className="mono">.env</span> 后重启后端。两处都不会显示密钥明文。
      </Card>
      {/*
        * 查询失败**不许渲染成空表**(FE-PROVIDER-01 / 阶段 A 验收第 6 条)。
        *
        * 一张空的 Provider 表和"一家 Provider 都没配"长得一模一样,而这两件事
        * 的下一步完全相反:前者要去看后端活没活,后者要去设置页填密钥。
        * 这一页还是运营判断"能不能出图"的地方 —— 说错了就是把人支去改配置。
        */}
      {query.isError ? (
        <ErrorNotice
          title="拉不到服务商列表"
          error={query.error}
          onRetry={() => query.refetch()}
          retrying={query.isFetching}
        />
      ) : (
        <Table<Provider>
          rowKey="name"
          size="small"
          bordered
          pagination={false}
          columns={columns}
          dataSource={query.data ?? []}
          loading={query.isFetching}
        />
      )}
    </Space>
  )
}
