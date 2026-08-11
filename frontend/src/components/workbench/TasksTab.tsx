/**
 * 生成任务标签(a47 §3.2:从只读观察窗升级为可发起)。
 *
 * ## 它以前是什么,为什么改
 *
 * 这一页原来自述是「只读的观察窗」,建任务与审图仍在 `/products/:id`
 * 和 `/tasks`。那个分工在工作台还不是唯一生产入口时说得通,而 a47 §4
 * 要把 `/products` 与 `/tasks` 一起撤出运营菜单 —— 撤完之后,运营在
 * 工作台里看得见"这件商品没有任何生成任务",却没有任何一颗按钮能开始出图。
 *
 * ## 两个入口都留着,主次分明
 *
 *     开始出图      复用 `TaskCreateModal`,就地开。**不重新实现业务逻辑**
 *     去商品页      深链保留 —— 排障与看原始信息仍然要走那一页
 *
 * 「同一件事有两个入口、两份行为」是原注释担心的事,而这里两个入口调的是
 * **同一个组件、同一个接口、同一套幂等键**,所以它是一个入口的两处摆放。
 *
 * ## 为什么它必须能回答"任务跑到哪了"
 *
 * §4.3 把 `/tasks` 移进「系统管理」组(运营整组不可见)是有前提的:
 * 这一页得答得出状态、轮次与失败原因。所以下面那张表比原来多了两列 ——
 * 少了它们,运营会两头落空:老入口没了,新入口答不上。
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { App, Button, Card, Empty, Skeleton, Space, Table, Tag, Tooltip } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { generationApi, type TaskCreatePayload } from '../../api/generation'
import { describeError } from '../../api/client'
import ErrorNotice from '../ErrorNotice'
import TaskCreateModal from '../TaskCreateModal'
import { TASK_STATUS_LABEL, type Task } from '../../api/types'
import { brandVars, fontScale } from '../../theme'

export default function TasksTab({ productId }: { productId: string }) {
  const queryClient = useQueryClient()
  const { message } = App.useApp()
  const [creating, setCreating] = useState(false)

  const tasks = useQuery({
    queryKey: ['tasks', { product_id: productId }],
    queryFn: () => generationApi.list({ product_id: productId, page_size: 50 }),
    enabled: Boolean(productId),
  })

  const create = useMutation({
    mutationFn: (payload: TaskCreatePayload) => generationApi.create(payload),
    onSuccess: (result) => {
      setCreating(false)
      /*
       * 幂等命中要**说出来**,不能和新建共用一句"已提交"。
       *
       * 后端对同参数重复提交返回的是已有任务(需求第九章),运营看到
       * "已提交"之后会去等一批新图,而什么都不会发生 —— 一次不报错的空等。
       */
      message.success(
        result.deduplicated
          ? '参数与已有任务相同,已经复用那一个,没有重复计费'
          : '任务已提交,生成在后台进行',
      )
      queryClient.invalidateQueries({ queryKey: ['tasks', { product_id: productId }] })
      // 流程状态也要重拉:出图会改「图片集」那一步的上游就绪判定
      queryClient.invalidateQueries({ queryKey: ['workbench-flow', productId] })
    },
    onError: (err) => message.error(describeError(err, 'write').text),
  })

  const columns: ColumnsType<Task> = [
    {
      title: '任务',
      dataIndex: 'id',
      width: 110,
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
    { title: 'Provider', dataIndex: 'provider', width: 100 },
    {
      title: '轮次',
      key: 'progress',
      width: 150,
      render: (_, row) => (
        <span style={{ color: brandVars.slate }}>
          第 {row.current_round}/{row.max_rounds} 轮 · 每轮 {row.candidate_count} 张
        </span>
      ),
    },
    {
      /*
       * §4.3 / 验收 #7:`/tasks` 移出运营菜单**之前**,这一页必须能显示
       * 失败原因。少了它,运营对着一个红色的「失败」标签无路可走 ——
       * 而他原来的做法是点进 `/tasks` 看错误码。
       */
      title: '失败原因',
      key: 'error',
      render: (_, row) =>
        row.error_message || row.error_code ? (
          <Tooltip title={row.error_message ?? undefined}>
            <span style={{ color: brandVars.danger, fontSize: fontScale.meta }}>
              {row.error_code ?? '未知错误'}
            </span>
          </Tooltip>
        ) : (
          <span style={{ color: brandVars.textFaint }}>—</span>
        ),
    },
  ]

  return (
    <Card
      size="small"
      title={`生成任务 · ${tasks.data?.total ?? 0}`}
      extra={
        <Space>
          <Button
            size="small"
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => setCreating(true)}
          >
            开始出图
          </Button>
          <Link to={`/products/${productId}`}>
            <Button size="small">商品原始信息</Button>
          </Link>
        </Space>
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

      <TaskCreateModal
        open={creating}
        productId={productId}
        title="开始出图"
        confirmLoading={create.isPending}
        onCancel={() => setCreating(false)}
        onSubmit={(values) => create.mutate(values as TaskCreatePayload)}
      />
    </Card>
  )
}
