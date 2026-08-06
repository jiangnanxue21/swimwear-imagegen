/**
 * 生成任务标签。
 *
 * 这一页在工作台里是**只读的观察窗**:AI 生成的候选图经审核后进素材库,
 * 才可能被编排进图片集。所以这里只回答「这件商品出图出到哪一步了」,
 * 建任务与审图仍在原有的生成任务页和审核页 —— 把那两套交互复制到工作台里,
 * 只会让同一件事有两个入口、两份行为。
 */
import { Link } from 'react-router-dom'
import { Button, Card, Empty, Skeleton, Space, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useQuery } from '@tanstack/react-query'
import { generationApi } from '../../api/generation'
import ErrorNotice from '../ErrorNotice'
import { TASK_STATUS_LABEL, type Task } from '../../api/types'
import { brandVars, fontScale } from '../../theme'

export default function TasksTab({ productId }: { productId: string }) {
  const tasks = useQuery({
    queryKey: ['tasks', { product_id: productId }],
    queryFn: () => generationApi.list({ product_id: productId, page_size: 50 }),
    enabled: Boolean(productId),
  })

  const columns: ColumnsType<Task> = [
    {
      title: '任务',
      dataIndex: 'id',
      width: 120,
      render: (id: string) => (
        <Link to={`/tasks/${id}`} className="mono">
          {id.slice(0, 8)}
        </Link>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 110,
      render: (s: Task['status']) => (
        <Tag color={TASK_STATUS_LABEL[s]?.color}>{TASK_STATUS_LABEL[s]?.text ?? s}</Tag>
      ),
    },
    { title: 'Provider', dataIndex: 'provider', width: 120 },
    {
      title: '进度',
      key: 'progress',
      render: (_, row) => (
        <span style={{ color: brandVars.slate }}>
          第 {row.current_round}/{row.max_rounds} 轮 · 每轮 {row.candidate_count} 张
        </span>
      ),
    },
  ]

  return (
    <Card
      size="small"
      title={`生成任务 · ${tasks.data?.total ?? 0}`}
      extra={
        <Link to={`/products/${productId}`}>
          <Button size="small">去商品页建任务</Button>
        </Link>
      }
    >
      {tasks.isLoading ? (
        <Skeleton active />
      ) : tasks.isError ? (
        <ErrorNotice
          title="拉不到生成任务"
          error={tasks.error}
          onRetry={() => tasks.refetch()}
        />
      ) : tasks.data?.items.length ? (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Table<Task>
            rowKey="id"
            size="small"
            bordered
            pagination={false}
            columns={columns}
            dataSource={tasks.data.items}
          />
          <div style={{ color: brandVars.slate, fontSize: fontScale.body }}>
            生成图通过审核后才会进素材库,进了素材库才能编排进图片集。
          </div>
        </Space>
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="这件商品还没有生成任务。上架不要求有 AI 图 —— 拍摄的正面图同样能走完整条流程。"
        />
      )}
    </Card>
  )
}
