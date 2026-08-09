import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { App, Button, Card, Empty, Select, Space, Table, Tag, Tooltip } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  generationApi, SUBMIT_RESULT_UNKNOWN, type TaskCreatePayload,
} from '../api/generation'
import { isResultUnknown, readError, readWriteError } from '../api/client'
import {
  MODE_LABEL, TASK_STATUS_LABEL, listPollInterval, type Task, type TaskStatus,
} from '../api/types'
import TaskCreateModal from '../components/TaskCreateModal'
import ForceRetryModal from '../components/ForceRetryModal'
import { useServerSort } from '../hooks/useServerSort'
import { enumParam, intParam, useUrlFilters } from '../hooks/useUrlFilters'
import { brandVars, fontScale } from '../theme'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

/**
 * 这一页能排的字段:三列 `sorter: true` 加一个默认值。必须是后端
 * `generation_service.TASK_SORTABLE` 的子集,守卫盯着这一条。
 */
const TASK_SORT_KEYS = ['created_at', 'provider', 'current_round', 'status'] as const

export default function TaskListPage() {
  useDocumentTitle('生成任务')
  const navigate = useNavigate()
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  //: 正在走强制重试确认的那一行。整行留着而不是只留 id ——
  //: 弹窗要显示 provider 与外部任务 ID,那是运营去后台核对的全部依据
  const [forceTarget, setForceTarget] = useState<Task | null>(null)
  /**
   * 筛选住在 URL 里(GAP-033)。
   *
   * A9:首页「生成失败」卡片带 `?status=FAILED` 过来,参数名不变,旧链接照旧能用。
   * 合法值以文案表为准 —— 那张表由契约测试盯着与后端 TaskStatus 对齐,
   * 不必在这里再抄一份状态清单。
   */
  const filters = useUrlFilters({
    status: enumParam<TaskStatus>(Object.keys(TASK_STATUS_LABEL)),
    page: intParam(1, { min: 1 }),
    sort: enumParam<string>(TASK_SORT_KEYS),
    order: enumParam<'asc' | 'desc'>(['asc', 'desc']),
  })
  const { status, page } = filters.values
  const [open, setOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<Task | null>(null)
  const editInitialValues = useMemo<Partial<TaskCreatePayload> | undefined>(
    () => editTarget
      ? {
          product_id: editTarget.product_id,
          mode: editTarget.mode,
          provider: editTarget.provider,
          routing_mode: editTarget.routing_mode,
          model_template_id: editTarget.model_template_id,
          candidate_count: editTarget.candidate_count,
          max_rounds: editTarget.max_rounds,
          base_seed: editTarget.base_seed,
          prompt: editTarget.prompt ?? undefined,
        }
      : undefined,
    [editTarget],
  )

  // 服务端排序。这张表默认按创建时间倒序,但排障时最常要的是「按更新时间」——
  // updated_at 同时是 worker 心跳(reap_stalled 靠它判断进程还活着),
  // 按它倒序就是「最近还有动静的任务」。
  // 换排序回第一页,和排序一起一次写进 URL:分两次写会进两格后退栈
  const sort = useServerSort({ sort: 'created_at', order: 'desc' }, undefined, {
    value: {
      sort: filters.values.sort ?? 'created_at',
      order: filters.values.order ?? 'desc',
    },
    set: (next) => filters.patch({ sort: next.sort, order: next.order, page: 1 }),
  })

  const query = useQuery({
    queryKey: ['tasks', { status, page, ...sort.params }],
    queryFn: () => generationApi.list({ status, ...sort.params, page, page_size: 20 }),
    // 三档节拍(见 `types.ts` 的 `taskPollInterval`):机器在跑 3 秒,
    // 等人动 30 秒,终态停。上一版是二档,而"等人动"被算进了终态 ——
    // 于是别人在审核台批准之后,这一页永远显示待审核。
    refetchInterval: (q) =>
      listPollInterval((q.state.data?.items ?? []).map((t) => t.status), 3000),
  })

  const create = useMutation({
    mutationFn: generationApi.create,
    onSuccess: (result) => {
      setOpen(false)
      setEditTarget(null)
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      if (result.deduplicated) {
        message.info('相同参数的任务已存在,已为你打开它')
      } else {
        message.success('任务已提交,生成在后台进行')
      }
      navigate(`/tasks/${result.task.id}`)
    },
    onError: (err) => {
      // 建任务是付费动作:超时后如实说"结果未知",别引导他再建一个
      message.error(readWriteError(err))
      if (isResultUnknown(err)) queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
  })

  /**
   * 写请求失败的统一收尾(FE-GLOBAL-01)。
   *
   * 结果未知时**顺手刷新列表**:这一步的第一个正确动作是"看一眼现在是什么状态",
   * 而不是再点一次。刷新之后多数情况列表自己就给出了答案。
   */
  const onWriteError = (err: unknown) => {
    message.error(readWriteError(err))
    if (isResultUnknown(err)) queryClient.invalidateQueries({ queryKey: ['tasks'] })
  }

  const cancel = useMutation({
    mutationFn: generationApi.cancel,
    onSuccess: () => {
      message.success('任务已取消')
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
    onError: onWriteError,
  })

  const retry = useMutation({
    mutationFn: (vars: { id: string; force: boolean; note?: string }) =>
      generationApi.retry(vars.id, { force: vars.force, note: vars.note }),
    onSuccess: (_data, vars) => {
      message.success(vars.force ? '已在对账后强制重试' : '已重新排队')
      setForceTarget(null)
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
    onError: onWriteError,
  })

  const remove = useMutation({
    mutationFn: generationApi.remove,
    onSuccess: () => {
      message.success('未执行任务已删除')
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
    },
    onError: onWriteError,
  })

  /*
   * 「提交结果未知」的对账确认(§7.2)。和详情页**共用同一个组件**,
   * 因为它们是同一个决定 —— 两份文案各自演化的话,同一个动作在两个页面上
   * 会有两种风险说明,而运营记住的是先看到的那一份。
   *
   * 弹窗里的对账结论是必填(后端缺它即 422),所以这里不能再用 modal.confirm:
   * 理由见 `components/ForceRetryModal.tsx` 顶部。
   */

  const columns: ColumnsType<Task> = [
    {
      title: '任务',
      dataIndex: 'id',
      width: 130,
      render: (id: string) => (
        <a className="mono" onClick={() => navigate(`/tasks/${id}`)}>
          {id.slice(0, 8)}
        </a>
      ),
    },
    {
      title: 'Provider',
      dataIndex: 'provider',
      key: 'provider',
      width: 100,
      sorter: true,
      sortOrder: sort.orderFor('provider'),
    },
    {
      title: '模式',
      dataIndex: 'mode',
      width: 130,
      render: (m: keyof typeof MODE_LABEL) => MODE_LABEL[m] ?? m,
    },
    {
      title: '轮次',
      key: 'current_round',
      width: 90,
      align: 'center',
      sorter: true,
      sortOrder: sort.orderFor('current_round'),
      render: (_, row) => `${row.current_round} / ${row.max_rounds}`,
    },
    { title: '候选数', dataIndex: 'candidate_count', width: 80, align: 'center' },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 120,
      sorter: true,
      sortOrder: sort.orderFor('status'),
      render: (s: TaskStatus, row) => (
        <Space direction="vertical" size={0}>
          <Tag color={TASK_STATUS_LABEL[s]?.color}>{TASK_STATUS_LABEL[s]?.text ?? s}</Tag>
          {row.error_code && (
            <span className="mono" style={{ fontSize: fontScale.meta, color: brandVars.dangerDeep }}>
              {row.error_code}
            </span>
          )}
        </Space>
      ),
    },
    {
      title: '操作',
      width: 330,
      render: (_, row) => {
        // FE-TASK-LIST-03:loading 绑到**这一行**。用页面级 isPending 的话,
        // 点一行的重试会让整列按钮一起转圈,运营看不出自己点的是哪一条
        const cancelling = cancel.isPending && cancel.variables === row.id
        const retrying = retry.isPending && retry.variables?.id === row.id
        const submitUnknown = row.error_code === SUBMIT_RESULT_UNKNOWN
        return (
          <Space size={4}>
            <Button
              size="small"
              icon={<EditOutlined />}
              onClick={() => setEditTarget(row)}
            >
              修改参数
            </Button>
            <Button
              size="small"
              disabled={!row.can_cancel || cancelling}
              loading={cancelling}
              onClick={() => cancel.mutate(row.id)}
            >
              取消
            </Button>
            {submitUnknown ? (
              // FE-TASK-LIST-04:普通重试对这条稳定 409。给它自己的按钮,
              // 而不是让运营点一次失败再去别处找出路
              <Tooltip title="提交结果未知:先到 Provider 后台核对,确认没有产生结果后再强制重试">
                <Button
                  size="small"
                  danger
                  disabled={!row.can_retry || retrying}
                  loading={retrying}
                  onClick={() => setForceTarget(row)}
                >
                  强制重试
                </Button>
              </Tooltip>
            ) : (
              <Button
                size="small"
                disabled={!row.can_retry || retrying}
                loading={retrying}
                onClick={() => retry.mutate({ id: row.id, force: false })}
              >
                重试
              </Button>
            )}
            <Tooltip
              title={row.can_delete
                ? '只删除这条尚未调用 Provider 的空任务'
                : '已有执行痕迹的任务必须保留，用于费用、错误与审核追踪'}
            >
              <span>
                <Button
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  disabled={!row.can_delete || (remove.isPending && remove.variables === row.id)}
                  loading={remove.isPending && remove.variables === row.id}
                  onClick={() => modal.confirm({
                    title: '删除这条未执行任务？',
                    content: '只会删除在 Provider 调用前已经取消的空任务，此操作不可撤销。',
                    okText: '删除',
                    okButtonProps: { danger: true },
                    cancelText: '取消',
                    onOk: () => remove.mutateAsync(row.id),
                  })}
                >
                  删除
                </Button>
              </span>
            </Tooltip>
          </Space>
        )
      },
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader title="生成任务" subtitle="AI 出图的执行记录。跑挂的任务在这里重试或换 Provider" />

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space wrap>
          <Select
            allowClear
            placeholder="状态"
            style={{ width: 150 }}
            value={status}
            onChange={(v) => filters.patch({ status: v, page: 1 })}
            options={Object.entries(TASK_STATUS_LABEL).map(([value, meta]) => ({
              value,
              label: meta.text,
            }))}
          />
          <Button icon={<ReloadOutlined />} onClick={() => query.refetch()}>
            刷新
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            创建任务
          </Button>
        </Space>
      </Card>

      <Table<Task>
        rowKey="id"
        size="small"
        bordered
        columns={columns}
        dataSource={query.data?.items ?? []}
        loading={query.isFetching && !query.data}
        onChange={sort.onTableChange}
        locale={{
          emptyText: (
            <Empty
              description={
                query.isError
                  ? readError(query.error)
                  : '还没有生成任务。挑一个已上传素材的商品创建一个。'
              }
            />
          ),
        }}
        pagination={{
          current: page,
          pageSize: 20,
          total: query.data?.total ?? 0,
          showTotal: (t) => `共 ${t} 个任务`,
          onChange: (next) => filters.patch({ page: next }),
        }}
      />

      <TaskCreateModal
        open={open || Boolean(editTarget)}
        productId={editTarget?.product_id}
        initialValues={editInitialValues}
        title={editTarget ? '修改参数并新建任务' : '创建生成任务'}
        confirmLoading={create.isPending}
        onCancel={() => {
          setOpen(false)
          setEditTarget(null)
        }}
        onSubmit={(values) => create.mutate(values as never)}
      />

      <ForceRetryModal
        open={Boolean(forceTarget)}
        provider={forceTarget?.provider ?? ''}
        externalTaskId={forceTarget?.external_task_id ?? null}
        // 只有强制重试这一路会开这个弹窗,所以 loading 直接跟 mutation ——
        // 行内那两个按钮的 loading 仍按 `mutation.variables` 绑定到具体行
        submitting={retry.isPending}
        onCancel={() => setForceTarget(null)}
        onConfirm={(note) => {
          if (forceTarget) retry.mutate({ id: forceTarget.id, force: true, note })
        }}
      />
    </Space>
  )
}
