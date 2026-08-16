/**
 * 批量任务中心(FE-302)与失败重试(FE-304)。
 *
 * FE-302 的验收结果是「单件失败不终止批次」。界面上让这件事可见的办法是
 * **把五个计数并排放**:总数 / 成功 / 处理中 / 失败 / 需人工。一个批次显示
 * "成功 47、失败 2、需人工 1"而状态是"完成(有失败)",运营就直接看懂了
 * 那 2 件没有拖累其余 47 件。
 *
 * 另外两个数字是阶段 3 的签字门槛(§6.3.2),所以给了它们单独的位置:
 *
 *     不可解释失败   必须为 0。不为 0 时红底显示,并说明这是异常码缺口
 *     完整成功率     A 批次 ≥95%。分母是"真正被尝试的件数",跳过的不算
 *
 * 「需人工」和「失败」在这一页刻意分开:重试对前者永远无效(重试一百次
 * 结果相同),所以重试按钮只对后者亮。
 */
import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import {
  Alert, Button, Card, Descriptions, Drawer, Empty, Input, Modal, Space, Statistic,
  Table, Tag, Tooltip, Typography, App,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DownloadOutlined, ReloadOutlined, WarningOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { isResultUnknown, readError, readWriteError } from '../api/client'
import ErrorNotice from '../components/ErrorNotice'
import {
  BATCH_ERROR_LABEL,
  BATCH_ITEM_STATUS_LABEL,
  BATCH_JOB_STATUS_LABEL,
  FILE_STATE_COLOR,
  batchApi,
  batchListPollInterval,
  batchLivenessOf,
  batchPollInterval,
  readDispatchOutcome,
  percent,
  saveBlob,
  type BatchItem,
  type BatchJob,
  type FileAuditItem,
  type ReceiptRow,
} from '../api/batch'
import { STEP_TAB } from '../api/workbench'
import { brandVars, fontScale } from '../theme'
import { compareTime, formatDateTime } from '../utils/datetime'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

/**
 * 「上周导的压缩包还能用吗」(OPS-REVIEW P4)。
 *
 * 这里曾经是一条**按时间猜**的横幅:导出超过 24 小时就提示"可能含有过期商品"。
 * 它有两个方向都错:导出 25 小时且什么都没变的批次被冤枉一次(下次运营就
 * 不再读这条横幅了),而导出 2 小时后有人改了属性的批次一声不响 ——
 * 那份文件正被传上平台。
 *
 * 现在问后端 `file-audit`:逐件比对导出时的指纹与当前草稿,过期口径复用
 * `service.refresh_draft`(与列表页、草稿页同一处判定)。三种结论对应三种
 * 不同的下一步,所以表里逐件给出后端写好的那句建议,前端不改写它。
 */
function BatchFileAuditBanner({ job }: { job: BatchJob }) {
  const navigate = useNavigate()
  const audit = useQuery({
    queryKey: ['batch-file-audit', job.id],
    queryFn: () => batchApi.fileAudit(job.id),
    enabled: job.action === 'EXPORT',
  })

  if (job.action !== 'EXPORT') return null
  if (audit.isError) {
    return <Alert type="warning" showIcon message={readError(audit.error)} />
  }
  const data = audit.data
  // banner 为 null = 全部仍然有效。此时**什么也不显示** ——
  // 一条"一切正常"的横幅出现三次之后就没人再读横幅了,包括真出问题那次
  if (!data?.applicable || !data.banner) return null

  const columns: ColumnsType<FileAuditItem> = [
    { title: 'SKU', dataIndex: 'sku', width: 160 },
    {
      title: '状态',
      key: 'state',
      width: 120,
      render: (_, row) => (
        <Tag color={FILE_STATE_COLOR[row.state]}>{row.state_label}</Tag>
      ),
    },
    { title: '该怎么办', dataIndex: 'advice' },
    {
      title: '指纹(导出时 / 现在)',
      key: 'fp',
      width: 200,
      render: (_, row) => (
        <Typography.Text type="secondary" style={{ fontSize: fontScale.meta }}>
          {row.exported_fingerprint ?? '—'} / {row.current_fingerprint ?? '—'}
        </Typography.Text>
      ),
    },
    {
      title: '',
      key: 'go',
      width: 90,
      render: (_, row) => (
        <Button
          size="small"
          type="link"
          onClick={() => navigate(`/workbench/${row.product_id}?tab=draft`)}
        >
          去草稿页
        </Button>
      ),
    },
  ]

  return (
    <Alert
      type={data.blocks_redownload ? 'error' : 'warning'}
      showIcon
      icon={<WarningOutlined />}
      message={data.banner}
      description={
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text style={{ fontSize: fontScale.body }}>
            共 {data.total} 件导出成功,其中 {data.items.length} 件需要处理。
            {data.blocks_redownload && (
              <strong>
                {' '}
                这个批次现在点「下载上架文件」会被后端拒绝(过期草稿一律禁止导出),
                先按下面的建议逐件处理。
              </strong>
            )}
          </Typography.Text>
          <Table<FileAuditItem>
            rowKey="product_id"
            size="small"
            bordered
            columns={columns}
            dataSource={data.items}
            loading={audit.isFetching}
            pagination={false}
          />
        </Space>
      }
    />
  )
}

function CountTiles({ job }: { job: BatchJob }) {
  const c = job.counts
  const tiles: { label: string; value: number | string; color?: string; hint: string }[] = [
    { label: '总数', value: c.total, hint: '批次里的全部条目,含被跳过的' },
    { label: '成功', value: c.succeeded, color: brandVars.success, hint: '动作已完成' },
    { label: '处理中', value: c.pending + c.running, hint: '尚未到终态' },
    { label: '失败', value: c.failed, color: c.failed ? brandVars.danger : undefined, hint: '执行没成功,可重试' },
    {
      label: '需人工',
      value: c.needs_human,
      color: c.needs_human ? brandVars.sand : undefined,
      hint: '系统正确地停下来等人做决定;重试对它无效',
    },
    { label: '已跳过', value: c.skipped, hint: '前置不满足或同 SPU 已执行,不计入成功率分母' },
  ]
  return (
    <Space size={16} wrap>
      {tiles.map((t) => (
        <Tooltip key={t.label} title={t.hint}>
          <Statistic
            title={t.label}
            value={t.value}
            valueStyle={{ fontSize: fontScale.metric, color: t.color }}
          />
        </Tooltip>
      ))}
      <Tooltip title="完整成功率 = 成功 ÷ 真正被尝试的件数。验收线:20 件标准批次要求 ≥ 95%">
        <Statistic
          title="完整成功率"
          value={percent(c.success_rate)}
          valueStyle={{ fontSize: fontScale.metric }}
        />
      </Tooltip>
      <Tooltip title="成功或可解释失败的比例。验收线:50 件全量要求 ≥ 95%。「需人工」计入分子 —— 异常件的正确终态就是被阻断,不是导出成功">
        <Statistic
          title="可解释比例"
          value={percent(c.explainable_rate)}
          valueStyle={{ fontSize: fontScale.metric }}
        />
      </Tooltip>
    </Space>
  )
}

export default function WorkbenchBatchPage() {
  useDocumentTitle('批量任务与导出')
  // App 上下文里的实例:静态 message/notification 拿不到 ConfigProvider 的主题与 locale
  const { message, notification } = App.useApp()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()

  /*
   * A45-#21:**只动 `open` 这一个参数,不整体替换。**
   *
   * 原来是 `setParams({ open: row.id })` / `setParams({})` —— 整份 searchParams
   * 被替换掉。今天这一页只有 `open` 一个参数,所以看不出问题;等哪天加了筛选、
   * 分页或排序的 URL 参数,打开一次详情抽屉就会把它们全部清掉,
   * 而清掉的那一刻页面看起来完全正常(它只是回到了默认筛选)。
   *
   * 用函数式更新 + `URLSearchParams` 的增删,让"我只管我这一个键"这件事
   * 在代码里是显式的。
   *
   * 打开那一半现在是 `detailHref` + `<Link to>`:详情抽屉本来就有地址,
   * 行入口就该是链接(可复制、可新标签打开、键盘可达)。它与下面
   * `closeDetail` 仍是同一份"只管 `open` 这一个键"的增删 —— 换成整体替换
   * 的话,链接会丢掉筛选参数,而且只有把它贴给别人的那一次才看得出来。
   */
  const detailHref = (id: string) => {
    const next = new URLSearchParams(params)
    next.set('open', id)
    return `?${next.toString()}`
  }

  const closeDetail = () =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.delete('open')
        return next
      },
      { replace: true },
    )
  const openId = params.get('open')

  /*
   * FE-BATCH-01:异步批次必须轮询。
   *
   * `BATCH_EXECUTION_MODE=celery` 时接口立刻返回,批次由 worker 在后台跑。
   * 上一版这两条 query 都不轮询,于是运营建完批次跳到这一页,看到的是
   * "全部待处理",而且**这一页永远不会自己变** —— 唯一的办法是手点刷新,
   * 但页面没有任何地方告诉他需要这么做。
   *
   * 停止条件用公共判定(`batchPollInterval`),不在这里手写状态清单。
   *
   * A32:节拍从两档变三档。原来只按 `status` 判断"是不是终态",而
   * `BATCH_EXECUTION_MODE=celery` 下 Broker 挂着时批次会**合法地**停在
   * QUEUED —— 非终态,于是这一页每 3 秒问一次,问到运营下班。投递失败的
   * 那个 False 只在创建那次的响应里、不落库,刷新之后就没了。
   *
   * 现在后端从 `status` / `created_at` / `started_at` 和条目的 `finished_at`
   * 现算一个 `liveness`,前端只读它(硬规则第 4 条)。STALLED 放慢到 30 秒
   * 而不是停:Broker 恢复之后批次还会继续跑,停掉的话运营要手动刷新才知道。
   */
  const list = useQuery({
    queryKey: ['batches'],
    queryFn: () => batchApi.list({ limit: 30 }),
    refetchInterval: (q) => batchListPollInterval(q.state.data ?? [], 3000),
  })

  const detail = useQuery({
    queryKey: ['batch', openId],
    queryFn: () => batchApi.get(openId as string),
    enabled: Boolean(openId),
    // 详情页开着的时候刷得比列表勤:运营正盯着这一个批次
    refetchInterval: (q) => (q.state.data ? batchPollInterval(q.state.data, 2000) : false),
  })

  const receipts = useQuery({
    queryKey: ['batch-receipts'],
    queryFn: () => batchApi.receipts(30),
  })

  const [selectedItems, setSelectedItems] = useState<string[]>([])
  // A-21:重新生成要一句理由,所以它是一个弹窗而不是 Popconfirm ——
  // 理由会进审计,和 `旧sha → 新sha` 记在同一行
  const [regenerateFor, setRegenerateFor] = useState<string | null>(null)
  const [regenerateReason, setRegenerateReason] = useState('')

  useEffect(() => {
    setSelectedItems([])
  }, [openId])

  const retry = useMutation({
    mutationFn: (itemIds?: string[]) =>
      batchApi.retry(openId as string, itemIds && itemIds.length ? itemIds : undefined),
    onSuccess: (job) => {
      // FE-BATCH-07:排队和跑完不是同一件事,不能用同一句话报。
      //
      // 上一版无条件说"重试完成 · 成功 X、失败 Y"。异步模式下那几个数字是
      // **重置之后的排队快照**:成功数是上一轮剩下的,失败数刚被清零 ——
      // 于是运营读到"重试完成、失败 0",而实际上一件都还没跑。
      //
      // 判定读后端的 `dispatch_state`(硬规则第 4 条)。顺带补上原来没有的
      // 第三种情况:**重试也会投递失败**。后端 a28 整改后重试与创建走同一套
      // 执行模式(BLOCK-03),于是 broker 挂着时这里同样会拿到 DISPATCH_FAILED,
      // 而上一版的两分支把它归进"已提交后台执行" —— 又一次告诉运营
      // "在跑了",而实际上没有人会来跑。
      const outcome = readDispatchOutcome(job)
      if (outcome === 'stuck') {
        notification.error({
          message: '重试已受理,但没能投给后台',
          description:
            '条目已经重置为待处理并落库,不会丢。请让管理员检查消息队列是否可用,'
            + '恢复后在本页再点一次重试即可 —— 不要新建批次,那会重复计费。',
          duration: null,
        })
      } else if (outcome === 'queued') {
        notification.info({
          message: '重试已提交后台执行',
          description:
            '条目已重置为待处理,由后台执行进程领走执行。本页会自动刷新进度,不用重复点。',
          duration: null,
        })
      } else {
        // 常驻:失败数是待办,付费调用数是要记账的事,两个都不该 3 秒后自己消失
        notification[job.counts.failed ? 'warning' : 'success']({
          message: `重试完成 · 成功 ${job.counts.succeeded}、失败 ${job.counts.failed}`,
          description: `本次触发付费调用 ${job.counts.paid_calls} 次。`,
          duration: null,
        })
      }
      setSelectedItems([])
      queryClient.invalidateQueries({ queryKey: ['batch', openId] })
      queryClient.invalidateQueries({ queryKey: ['batches'] })
      queryClient.invalidateQueries({ queryKey: ['batch-receipts'] })
    },
    onError: (err) => {
      // 重试是付费动作:超时后不能说"失败了,再点一次"
      message.error(readWriteError(err))
      if (isResultUnknown(err)) {
        queryClient.invalidateQueries({ queryKey: ['batch', openId] })
        queryClient.invalidateQueries({ queryKey: ['batches'] })
      }
    },
  })

  /**
   * 生成(如果还没生成过)+ 下载(评审第 6 条)。
   *
   * 两次请求而不是一次:生成是写动作走 POST,下载是读动作走 GET。
   * 合成一个 GET 的后果是浏览器预取、重复点击、监控探活都会重新产出
   * 字节并累加导出次数。这里把两步串起来,是为了让运营那边仍然只是
   * 「点一下下载」——**接口的形状变了,操作没变**。
   */
  /*
   * A45-#19:**哪一行在下载,要按行记。**
   *
   * `download.isPending` 是整个 mutation 的状态,而列表里每一行都有一个下载
   * 按钮 —— 点一行会让**整表**的按钮一起转圈。生成上架文件不是几毫秒的事
   * (它要跑完整批的导出闸再打包),转圈期间运营看到的是"我好像点了全部"。
   *
   * 记住正在下载的那个 id 即可,不必给每行各建一个 mutation。
   */
  const [downloadingId, setDownloadingId] = useState<string | null>(null)

  const download = useMutation({
    mutationFn: async (batchId: string) => {
      setDownloadingId(batchId)
      const meta = await batchApi.generateFile(batchId)
      const file = await batchApi.file(batchId)
      return { ...file, regenerated: meta.regenerated }
    },
    onSuccess: ({ blob, filename, regenerated }) => {
      saveBlob(blob, filename)
      message.success(
        regenerated
          ? '已生成并下载:压缩包里含上架文件与批次清单'
          : '已下载此前生成的那一份(内容未变,可与平台上传记录逐字对照)',
      )
      // 生成会写 file_* 四列,批次详情要跟着刷新
      queryClient.invalidateQueries({ queryKey: ['batch', openId] })
      queryClient.invalidateQueries({ queryKey: ['batches'] })
    },
    onError: (err) => message.error(readWriteError(err)),
    // 成功失败都要清,否则失败之后那一行会一直转圈
    onSettled: () => setDownloadingId(null),
  })

  /**
   * 作废重生成(A-21)。**需要管理员口令。**
   *
   * 它和上面那个 `download` 是两件事:`download` 的第一步 `generateFile`
   * 是幂等的,已经生成过就原样返回旧的那一份 —— 那是评审第 6 条的核心
   * 承诺,不能为了"再来一份"把它拆掉。所以再生成是另一个动词。
   *
   * 不自动接下载:换了 sha 之后运营该做的第一件事是确认新旧差异,
   * 而不是拿到一个字节流。下载按钮就在旁边。
   */
  const regenerate = useMutation({
    mutationFn: ({ batchId, reason }: { batchId: string; reason: string }) =>
      batchApi.regenerateFile(batchId, reason),
    onSuccess: (meta) => {
      setRegenerateFor(null)
      setRegenerateReason('')
      message.success(
        meta.bytes_changed
          ? '已重新生成:内容有变化,校验和已更新。旧的那一份不要再传平台'
          : '已重新生成:内容与旧的那一份逐字节相同(说明此前的问题出在存储或传输上,不在内容上)',
      )
      queryClient.invalidateQueries({ queryKey: ['batch', openId] })
      queryClient.invalidateQueries({ queryKey: ['batches'] })
      // 审计页的键是 `['audit', {筛选条件}]`,这里按前缀失效。
      // 写成 `['workbench-audit']` 是**无声的空操作** —— 没有任何查询用那个键,
      // 而 invalidateQueries 对着不存在的键不会报错(A-09 就是这个形状)
      queryClient.invalidateQueries({ queryKey: ['audit'] })
    },
    onError: (err) => message.error(readWriteError(err)),
  })

  const jobColumns: ColumnsType<BatchJob> = [
    {
      title: '批次',
      key: 'id',
      width: 220,
      render: (_, row) => (
        <div>
          {/*
            详情抽屉是**有地址的**(`?open=<id>`),所以行入口该是链接:
            可复制、可新标签打开、键盘可达。`to` 从 `detailHref` 取,
            与 `openDetail` 同一份键增删逻辑 —— 两处各拼一次的话,
            链接会丢掉筛选参数而点击不会,同一个入口两种结果。
          */}
          <Link className="mono" to={detailHref(row.id)}>
            {row.id.slice(0, 8)}
          </Link>
          <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>
            {row.label || row.action_label}
          </div>
        </div>
      ),
    },
    {
      title: '动作',
      dataIndex: 'action_label',
      key: 'action',
      width: 130,
      filters: [...new Set((list.data ?? []).map((j) => j.action_label))].map((v) => ({
        text: v,
        value: v,
      })),
      onFilter: (value, row) => row.action_label === value,
    },
    {
      title: '状态',
      key: 'status',
      width: 130,
      filters: [...new Set((list.data ?? []).map((j) => j.status))].map((v) => ({
        text: BATCH_JOB_STATUS_LABEL[v]?.text ?? v,
        value: v,
      })),
      onFilter: (value, row) => row.status === value,
      render: (_, row) => {
        const meta = BATCH_JOB_STATUS_LABEL[row.status]
        return <Tag color={meta?.color}>{meta?.text ?? row.status_label}</Tag>
      },
    },
    {
      title: '成功 / 需人工 / 失败 / 跳过',
      key: 'counts',
      width: 220,
      render: (_, row) => (
        <Space size={4}>
          <Tag color="success">{row.counts.succeeded}</Tag>
          <Tag color="warning">{row.counts.needs_human}</Tag>
          <Tag color={row.counts.failed ? 'error' : 'default'}>{row.counts.failed}</Tag>
          <Tag>{row.counts.skipped}</Tag>
        </Space>
      ),
    },
    {
      title: '不可解释',
      key: 'unexplained',
      width: 100,
      sorter: (a, b) => a.counts.unexplained - b.counts.unexplained,
      align: 'center',
      render: (_, row) =>
        row.counts.unexplained ? (
          <Tooltip title="不可解释失败数必须为 0 才能签字验收。这是一个异常码缺口,请连同批次号报给开发">
            <Tag color="error" icon={<WarningOutlined />}>
              {row.counts.unexplained}
            </Tag>
          </Tooltip>
        ) : (
          <Tag color="success">0</Tag>
        ),
    },
    {
      title: '付费调用 / 复用',
      key: 'paid',
      width: 130,
      align: 'center',
      render: (_, row) => (
        <Tooltip title="复用 = 上游未变化、命中幂等回执而省下的付费调用次数">
          <span className="mono">
            {row.counts.paid_calls} / {row.counts.reused}
          </span>
        </Tooltip>
      ),
    },
    {
      title: '执行人 / 时间',
      key: 'who',
      width: 180,
      // 批次列表是一次拉全的(pagination={false}),客户端排序等于对全量排序。
      // 服务端分页的那几张表**刻意没加** sorter —— 见文件末尾的说明
      defaultSortOrder: 'descend',
      sorter: (a, b) => compareTime(a.created_at, b.created_at),
      render: (_, row) => (
        <div style={{ fontSize: fontScale.meta }}>
          <div>{row.created_by}</div>
          <div style={{ color: brandVars.slate }}>{formatDateTime(row.created_at)}</div>
        </div>
      ),
    },
    {
      title: '',
      key: 'ops',
      width: 100,
      // 走查 P0-1:表格最小 1280px + 外框 230px = 需要 1510px 视口。
      // 1366 上缺 144px,缺掉的正是这一列 —— 而批次跑完之后
      // 运营唯一要点的东西就是「下载」
      fixed: 'right',
      render: (_, row) =>
        row.downloadable ? (
          <Button
            size="small"
            icon={<DownloadOutlined />}
            loading={download.isPending && downloadingId === row.id}
            onClick={() => download.mutate(row.id)}
          >
            下载
          </Button>
        ) : null,
    },
  ]

  const itemColumns: ColumnsType<BatchItem> = [
    {
      title: '商品',
      key: 'sku',
      width: 190,
      render: (_, row) => (
        <div>
          <Link className="mono" to={`/workbench/${row.product_id}`}>
            {row.sku}
          </Link>
          <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>{row.spu}</div>
        </div>
      ),
    },
    {
      title: '状态',
      key: 'status',
      width: 96,
      render: (_, row) => {
        const meta = BATCH_ITEM_STATUS_LABEL[row.status]
        return <Tag color={meta?.color}>{meta?.text ?? row.status_label}</Tag>
      },
    },
    {
      title: '结果 / 原因',
      key: 'reason',
      render: (_, row) => {
        if (row.status === 'SUCCEEDED') {
          return (
            <span style={{ fontSize: fontScale.body }}>
              {row.error_message || '完成'}
              {row.reused && (
                <Tag color="default" style={{ marginInlineStart: 6 }}>
                  复用回执
                </Tag>
              )}
            </span>
          )
        }
        return (
          <div>
            {row.error_code && (
              <Tag color={row.explainable ? 'warning' : 'error'}>
                {row.error_label || BATCH_ERROR_LABEL[row.error_code] || row.error_code}
              </Tag>
            )}
            {!row.explainable && (
              <Tag color="error" icon={<WarningOutlined />}>
                不可解释
              </Tag>
            )}
            <div style={{ fontSize: fontScale.body }}>{row.error_message}</div>
            {row.advice && (
              <div style={{ color: brandVars.slate, fontSize: fontScale.meta }}>下一步:{row.advice}</div>
            )}
          </div>
        )
      },
    },
    {
      title: '次数',
      key: 'attempts',
      width: 110,
      align: 'center',
      render: (_, row) => (
        <Tooltip title={`执行 ${row.attempts} 次,其中触发付费调用 ${row.paid_calls} 次`}>
          <span className="mono">
            {row.attempts} / {row.paid_calls}
          </span>
        </Tooltip>
      ),
    },
    {
      title: '',
      key: 'jump',
      width: 92,
      render: (_, row) =>
        row.status === 'SUCCEEDED' ? null : (
          <Tooltip
            title={
              row.target_ref
                ? `定位到:${row.target_ref}`
                : '跳到对应标签页'
            }
          >
            <Button
              size="small"
              type="link"
              onClick={() => {
                const tab = row.target_step ? STEP_TAB[row.target_step] : 'overview'
                navigate(`/workbench/${row.product_id}?tab=${tab}`)
              }}
            >
              去处理
            </Button>
          </Tooltip>
        ),
    },
  ]

  const job = detail.data
  const retryableIds = (job?.items ?? []).filter((i) => i.retryable).map((i) => i.id)
  /*
   * 「还在跑」和「卡住了」要分开数,而且分开报。
   *
   * 合成一个数的话,Broker 挂着的时候横幅会说"有 3 个批次还在排队或执行中,
   * 本页每 3 秒自动刷新" —— 三句话里有两句是错的:它们没在执行,本页也
   * 不再是 3 秒刷一次。运营据此等下去,而没有人会来跑。
   */
  const jobs = list.data ?? []
  const liveCount = jobs.filter((j) => batchLivenessOf(j) === 'LIVE').length
  const stalledJobs = jobs.filter((j) => batchLivenessOf(j) === 'STALLED')
  // 同一句建议只说一次,并带上有多少个批次是这个成因
  const stalledAdvice = Object.entries(
    stalledJobs.reduce<Record<string, number>>((acc, job) => {
      const advice = job.liveness_advice
      if (advice) acc[advice] = (acc[advice] ?? 0) + 1
      return acc
    }, {}),
  ).map(([advice, count]) => ({ advice, count }))

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader title="批量任务与导出" subtitle="批次执行结果、付费调用台账与导出文件" />

      {list.isError && (
        <ErrorNotice
          title="拉不到批次列表"
          error={list.error}
          onRetry={() => list.refetch()}
        />
      )}

      {liveCount > 0 && (
        <Alert
          type="info"
          showIcon
          message={`有 ${liveCount} 个批次还在排队或执行中,本页每 3 秒自动刷新`}
        />
      )}

      {stalledJobs.length > 0 && (
        /*
          * 建议句**按内容分组**,不是取第一条。
          *
          * STALLED 有两种成因,后端给的建议也是两句不同的话:「一直没人认领」
          * (多半是 Broker 挂了)和「开跑之后条目不再落地」(多半是 worker 没了)。
          * 两种同时存在时只显示第一条,等于对另一半批次说错话 —— 而运营会
          * 照着那句话去做,做完发现没用。
          *
          * 建议句本身用后端给的那一份,前端不改写(硬规则第 4 条),
          * 这里只负责去重和归组。
          */
        <Alert
          type="warning"
          showIcon
          message={`有 ${stalledJobs.length} 个批次很久没有动静了`}
          description={
            <>
              {stalledAdvice.map(({ advice, count }) => (
                <div key={advice} style={{ marginBottom: 4 }}>
                  {stalledAdvice.length > 1 && <strong>{count} 个:</strong>} {advice}
                </div>
              ))}
              <div style={{ marginTop: 4, color: brandVars.slate, fontSize: fontScale.body }}>
                这几个批次的刷新已经放慢到 30 秒一次。展开对应批次点「重试」可以重新排队。
              </div>
            </>
          }
        />
      )}

      <Space>
        <Button
          icon={<ReloadOutlined />}
          onClick={() => list.refetch()}
          loading={list.isFetching}
        >
          刷新
        </Button>
        <Button type="link" onClick={() => navigate('/workbench')}>
          去商品列表选商品
        </Button>
      </Space>

      <Table<BatchJob>
        rowKey="id"
        size="small"
        bordered
        scroll={{ x: 1280 }}
        columns={jobColumns}
        dataSource={list.isError ? [] : list.data ?? []}
        loading={list.isFetching}
        pagination={false}
        locale={{
          emptyText: (
            <Empty description="还没有批次。到商品列表勾选商品后使用批量动作。" />
          ),
        }}
      />

      <Card
        size="small"
        title={
          <Tooltip title="想知道有没有重复触发付费调用,看「执行次数」这一列 —— 大于 1 才是重复。「付费调用」是逐图计费的花费总数,多图商品本来就大于 1">
            <span>幂等回执台账</span>
          </Tooltip>
        }
        styles={{ body: { padding: 0 } }}
      >
        <Table<ReceiptRow>
          rowKey={(row) => row.idempotency_key}
          size="small"
          columns={[
            { title: '动作', dataIndex: 'action_label', width: 130 },
            { title: '作用域', dataIndex: 'scope_key', className: 'mono' },
            {
              /*
               * A-17:这一列是**钱**,不是次数。识别是逐图调用的,
               * 一件 4 图商品首次执行完这里就是 4 —— 它没有错,所以
               * 不再对着它报红。判"重复执行"的是右边那一列。
               */
              title: '付费调用',
              dataIndex: 'paid_calls',
              width: 100,
              align: 'center' as const,
              render: (value: number) => (
                <Tooltip title="这条作用域一共花掉的付费调用次数。属性识别是逐图调用的,4 张图就是 4 次 —— 这个数大于 1 很正常,不是异常">
                  <span className="mono">{value}</span>
                </Tooltip>
              ),
            },
            {
              title: '执行次数',
              dataIndex: 'executions',
              width: 100,
              align: 'center' as const,
              render: (value: number, row: ReceiptRow) =>
                row.paid_call_anomaly ? (
                  <Tooltip title="同一动作、同一作用域、同一上游指纹被真的开跑了两次,而且花过钱 —— 这是重复付费,属于最高优先级缺陷,请立刻报给开发">
                    <Tag color="error">{value}</Tag>
                  </Tooltip>
                ) : (
                  <span className="mono">{value}</span>
                ),
            },
            {
              title: '省下的调用',
              dataIndex: 'reuse_count',
              width: 110,
              align: 'center' as const,
            },
            {
              title: '最近使用',
              dataIndex: 'last_used_at',
              width: 180,
              render: (v: string | null) => formatDateTime(v),
            },
          ]}
          dataSource={receipts.isError ? [] : receipts.data ?? []}
          loading={receipts.isFetching}
          pagination={false}
          locale={{
            /*
             * 拉不到回执 ≠ 还没有回执(FE-BATCH-05)。回执台账是"这次批量到底
             * 调了几次付费接口"的唯一对账依据 —— 说成"还没有回执"会让运营
             * 以为这批没花钱,而它可能刚花完
             */
            emptyText: receipts.isError ? (
              <ErrorNotice
                title="拉不到回执台账"
                error={receipts.error}
                onRetry={() => receipts.refetch()}
                retrying={receipts.isFetching}
              />
            ) : (
              <Empty description="还没有回执" />
            ),
          }}
        />
      </Card>

      <Drawer
        open={Boolean(openId)}
        width={980}
        onClose={() => closeDetail()}
        title={job ? `批次 ${job.id.slice(0, 8)} · ${job.action_label}` : '批次详情'}
        extra={
          job && (
            <Space>
              {job.downloadable && (
                <Button
                  icon={<DownloadOutlined />}
                  loading={download.isPending && downloadingId === job.id}
                  onClick={() => download.mutate(job.id)}
                >
                  下载上架文件
                </Button>
              )}
              {/*
                A-21:下载报 409 时("文件已不在存储中"/"校验和对不上"),
                错误文案指向的就是这个按钮。在它出现之前那两句话指向的动作
                在系统里根本不存在,运营只能在 409 里打转。

                只在已经生成过时出现:没生成过该点的是「下载」(它会先 POST
                生成一份),"重新"生成没有对象。
              */}
              {job.file_generated && (
                <Tooltip title="作废当前这份上架文件、重新生成一份。会换掉校验和 —— 而它是平台驳回时「我传的是这一版」的物证,所以需要管理员口令并说明原因">
                  <Button
                    danger
                    icon={<ReloadOutlined />}
                    loading={regenerate.isPending}
                    onClick={() => setRegenerateFor(job.id)}
                  >
                    重新生成
                  </Button>
                </Tooltip>
              )}
              <Tooltip
                title={
                  retryableIds.length
                    ? '只重试失败且可重试的条目。成功项不重跑,「需人工」项重试无效'
                    : '没有可重试的条目'
                }
              >
                <Button
                  type="primary"
                  icon={<ReloadOutlined />}
                  disabled={!retryableIds.length}
                  loading={retry.isPending}
                  onClick={() => retry.mutate(selectedItems)}
                >
                  {selectedItems.length
                    ? `重试选中 ${selectedItems.length} 件`
                    : `重试失败项 ${retryableIds.length} 件`}
                </Button>
              </Tooltip>
            </Space>
          )
        }
      >
        {detail.isError && (
          <Alert type="error" showIcon message={readError(detail.error)} />
        )}
        {job && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            {/* FE-BATCH-03:排队中的批次不能只显示一排 0,那读起来像"跑完了、什么都没成" */}
            {batchLivenessOf(job) === 'LIVE' && (
              <Alert
                type="info"
                showIcon
                message={
                  job.status === 'QUEUED'
                    ? '这个批次在排队,后台执行进程还没开始执行'
                    : '这个批次正在后台执行'
                }
                description="下面的数字是当前进度,不是最终结果 —— 本页每 2 秒自动刷新一次。"
              />
            )}
            {/*
              * 卡住的批次走 warning 而不是上面那条 info。两条不能合并:
              * 上面那句承诺"每 2 秒自动刷新",而这一档已经放慢到 30 秒 ——
              * 一条关于系统行为的、当场就不成立的承诺。
              */}
            {batchLivenessOf(job) === 'STALLED' && (
              <Alert
                type="warning"
                showIcon
                message={job.liveness_label ?? '很久没有动静了'}
                description={
                  <>
                    <div>{job.liveness_advice}</div>
                    <div style={{ marginTop: 4, color: brandVars.slate, fontSize: fontScale.body }}>
                      下面的数字是它停下来时的进度。本页对这个批次已放慢到 30 秒刷新一次。
                    </div>
                  </>
                }
              />
            )}
            <CountTiles job={job} />
            <BatchFileAuditBanner job={job} />

            {job.counts.unexplained > 0 && (
              <Alert
                type="error"
                showIcon
                message={`有 ${job.counts.unexplained} 件不可解释失败`}
                description="这个数字必须为 0 才能签字验收。它表示有失败没有对应的异常码 —— 这类不要当普通失败重试,请把批次号发给开发。"
              />
            )}

            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="状态">{job.status_label}</Descriptions.Item>
              <Descriptions.Item label="执行人">{job.created_by}</Descriptions.Item>
              <Descriptions.Item label="计划时可执行">
                {String(job.params.planned_executable ?? '—')}
              </Descriptions.Item>
              <Descriptions.Item label="计划时不可执行">
                {String(job.params.planned_blocked ?? '—')}
              </Descriptions.Item>
            </Descriptions>

            <Table<BatchItem>
              rowKey="id"
              size="small"
              bordered
              columns={itemColumns}
              dataSource={job.items ?? []}
              loading={detail.isFetching}
              pagination={{ pageSize: 20, showSizeChanger: false }}
              rowSelection={{
                selectedRowKeys: selectedItems,
                onChange: (keys) => setSelectedItems(keys.map(String)),
                getCheckboxProps: (row) => ({ disabled: !row.retryable }),
              }}
            />
            <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
              只有「失败且可重试」的条目能被勾选:成功项重试就是重复付费,
              「需人工」项重试一百次结果相同。
            </Typography.Text>
          </Space>
        )}
      </Drawer>

      {/*
        A-21:重新生成的确认框。
        理由必填 —— 它会和 `旧sha → 新sha` 记进同一条审计行,而事后回看
        必须能回答"这份文件为什么换过一版"。写成弹窗而不是 Popconfirm,
        就是因为 Popconfirm 收不了这句话。
      */}
      <Modal
        open={Boolean(regenerateFor)}
        title="重新生成上架文件"
        okText="确认重新生成"
        cancelText="取消"
        okButtonProps={{
          danger: true,
          disabled: !regenerateReason.trim() || regenerate.isPending,
        }}
        confirmLoading={regenerate.isPending}
        onCancel={() => {
          setRegenerateFor(null)
          setRegenerateReason('')
        }}
        onOk={() =>
          regenerateFor &&
          regenerate.mutate({ batchId: regenerateFor, reason: regenerateReason.trim() })
        }
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Alert
            type="warning"
            showIcon
            message="这会换掉文件的校验和"
            description={
              <>
                当前这份文件的 sha256 是平台驳回时「我传的是这一版」的物证。
                重新生成之后它就变了,而且新文件反映的是<b>现在</b>的内容,
                不是运营当时传上去的那一份。
                <br />
                旧文件不会被删,仍可按审计里的路径找回来。
              </>
            }
          />
          <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
            需要管理员口令。理由会连同新旧两个 sha256 校验和一起写进审计。
          </Typography.Text>
          <Input.TextArea
            rows={3}
            maxLength={500}
            showCount
            value={regenerateReason}
            onChange={(e) => setRegenerateReason(e.target.value)}
            placeholder="例:存储清理误删了这份文件,平台还没收到;或:下载时校验和对不上,怀疑传输损坏"
          />
        </Space>
      </Modal>
    </Space>
  )
}
