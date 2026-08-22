/**
 * 发布上架(台账 B-02 / A45-#8)。**这是 `/publish/*` 六个端点的第一个前端入口。**
 *
 * 在这一页之前,后端从任务 18 起就有完整的发布链路,而运营够不着它:
 * 流水线走到"导出上架文件"就断了,再往后只能拿 curl 打接口。评审对这条的
 * 措辞是「只能用 API 验收,运营无法完整操作」—— 也就是说人工评测跑不完。
 *
 * ## 这一页只做展示与触发,不做判定(CLAUDE.md 硬规则 4)
 *
 * 每一行都带 `display_status_label` / `next_action_label` / `blocking_reasons`
 * / `allowed_actions`,全部由后端 `workflows/publish_view.py` 算好。这一页里
 * **没有一个 if 在推断状态**:按钮亮不亮读 `allowed_actions.includes(...)`,
 * 状态显示读 `display_status_label`,为什么不能提交读 `blocking_reasons`。
 *
 * 理由写在 `publish_view.py` 顶部,这里只重复最贵的那一条:一次退避耗尽落进
 * DEAD 的提交,listing 仍写着 SUBMITTING、attempt 仍写着 IN_FLIGHT,任何
 * 自己拼这两样的界面都会说「正在处理」—— 而运营会一直等一件已经死掉的事。
 *
 * ## 三块的划分对应 6.4 的三段
 *
 *     清单      现在有哪些发布记录、各自卡在哪
 *     详情抽屉  发布前(草稿)/ 发布中(尝试 + 投递)/ 发布后(平台状态 + 驳回)
 *     清理预案  4.1 节 H:本轮测试在平台上建了哪些,还有几个占着位置
 *
 * ## 为什么提交入口在这一页而不是商品页
 *
 * 提交是 `POST /publish/products/{id}/submit`,天然属于商品。但**第一次提交
 * 之前没有 listing 行**,只在商品页给入口的话,这一页会永远是空的,而运营
 * 找不到"我提交过的东西在哪"。所以两边都有:工作台导出标签页的
 * 「发布到平台」按钮跳到这里并带上 `?product_id=`,由这一页统一收口。
 */
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  Alert, App, Badge, Button, Card, Checkbox, Descriptions, Drawer, Empty, Form,
  Input, Modal, Select, Space, Statistic, Table, Tabs, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  AuditOutlined, CloudUploadOutlined, ExperimentOutlined, ReloadOutlined, SyncOutlined,
  SendOutlined, StopOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  PUBLISH_ATTEMPT_STATUS_LABEL,
  PUBLISH_OPERATION_LABEL,
  PUBLISH_OUTBOX_STATUS_LABEL,
  PUBLISH_REJECTION_STATUS_LABEL,
  PUBLISH_STATUS_LABEL,
  publishApi,
  type ChannelListingRow,
  type CleanupRow,
  type EnqueueNotice,
  type PublishAction,
  type SubmitResult,
} from '../api/publish'
import { DRAFT_STATUS_LABEL, LOCATED_BY_LABEL, REJECTION_REASON_LABEL } from '../api/workbench'
import BrandTag from '../components/BrandTag'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import { msg } from '../i18n/msg'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { useWriteError } from '../hooks/useWriteError'
import { brandVars, fontScale, space } from '../theme'
import { formatDateTime } from '../utils/datetime'

/**
 * 展示状态 -> 颜色。**只有颜色在前端,文案一律用后端的 `display_status_label`。**
 *
 * 分成这三档而不是给 15 个状态各配一个色:运营在清单上要区分的只有
 * 「要我做点什么」「等着就行」「已经结束」。查不到的状态回落成默认灰 ——
 * 后端新加一个展示状态时,这一页显示的是它的中文名配一个中性色,
 * 而不是崩掉或者显示一个瞎猜的颜色。
 */
const STATUS_COLOR: Record<string, string> = {
  NEEDS_FIX: 'error',
  PLATFORM_REJECTED: 'error',
  SUBMIT_FAILED: 'error',
  RESULT_UNKNOWN: 'warning',
  STALLED: 'warning',
  QUEUED: 'processing',
  IN_FLIGHT: 'processing',
  PLATFORM_REVIEWING: 'processing',
  DELISTING: 'processing',
  LISTED: 'success',
  DELISTED: 'default',
  PAUSED: 'default',
  CLOSED: 'default',
  // 干跑完成 = **什么都没发出去**,和「未提交」属于同一族:平台上没有这个商品。
  // 刻意不给它一个调色板色 —— 那类色不受 theme.ts 控制(见 BrandTag 顶部),
  // 而这一档的区分本来就该由后端那句 `display_status_label` 承担
  DRY_RUN_DONE: 'default',
  NOT_SUBMITTED: 'default',
}

const ACTION_ICON: Record<PublishAction, React.ReactNode> = {
  SUBMIT: <CloudUploadOutlined />,
  DRY_RUN: <ExperimentOutlined />,
  POLL: <ReloadOutlined />,
  DELIST: <StopOutlined />,
  RECONCILE: <AuditOutlined />,
  REDELIVER: <SendOutlined />,
}

const ACTION_LABEL: Record<PublishAction, string> = {
  SUBMIT: '提交',
  DRY_RUN: '预览报文',
  POLL: '刷新状态',
  DELIST: '下架',
  // 措辞不叫"重试"。那两个字会让人以为点了就重新提交一次,而这两个动作
  // 都复用原幂等键、不会创建第二件商品 —— 说清这件事正是它们敢上界面的前提
  RECONCILE: '去平台核对',
  REDELIVER: '重新投递',
}

/**
 * 提交结果的通知条。
 *
 * 用 `will_deliver` 而不是 `reused` 决定这是好消息还是坏消息 ——
 * 「沿用了在途的那一条」和「沿用了已经死掉的那一条」在 `reused` 上一模一样,
 * 而后者再点多少次都不会有变化。这一位信息是后端专门为此给的。
 */
function NoticeAlert({ notice, dryRun }: { notice: EnqueueNotice; dryRun: boolean }) {
  return (
    <Alert
      showIcon
      type={dryRun ? 'info' : notice.will_deliver ? 'success' : 'warning'}
      message={notice.message}
      description={
        notice.will_deliver ? undefined : (
          <span style={{ fontSize: fontScale.body }}>
            <b>接下来不会真的发出任何东西。</b>再点一次提交也一样 ——
            先处理上一次的失败原因,或者改动草稿内容(内容变了才算一次新的提交)。
          </span>
        )
      }
    />
  )
}

/** 模拟器标记。4.1 节 F 要求它在关键页面可见,而"可见"必须是一眼的 */
function RealityTag({ simulated }: { simulated: boolean }) {
  /*
   * 用 BrandTag 而不是 `<Tag color="purple">`:antd 的调色板预设色不受
   * theme.ts 的令牌控制,暗色模式下不会跟着翻(理由见 BrandTag 顶部)。
   *
   * 冷色给模拟、暖色给真实,不是配色偏好:这一页上两种行长得几乎一样,
   * 而"点下架会不会真的动到平台"完全取决于这一格。暖色是那个需要多看
   * 一眼的信号。
   */
  return simulated ? (
    <BrandTag tone="slate" title="外部 ID 由渠道模拟器生成 —— 真实平台上并不存在这个商品">
      模拟
    </BrandTag>
  ) : (
    <BrandTag tone="sand" title="真实渠道上的商品 —— 下架等操作会真的动到平台">
      真实渠道
    </BrandTag>
  )
}

export default function PublishPage() {
  useDocumentTitle('发布上架')
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()

  const [channel, setChannel] = useState('')
  const [shopId, setShopId] = useState('')
  const [tag, setTag] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [openId, setOpenId] = useState<string | null>(null)
  const [submitFor, setSubmitFor] = useState<string | null>(null)
  const [lastSubmit, setLastSubmit] = useState<SubmitResult | null>(null)
  const [form] = Form.useForm<{
    shop_id: string
    test_batch_tag?: string
    dry_run: boolean
  }>()

  /*
   * 工作台导出页跳过来时带着 `?product_id=` —— 直接把提交弹窗打开。
   *
   * 读完就从 URL 上摘掉:留着的话运营关掉弹窗、刷新页面,它又弹出来一次,
   * 而那时他已经提交过了。**用函数式更新**而不是 `setParams({})`:
   * 后者整体替换 searchParams,日后这一页加任何别的 URL 参数都会被这里清掉
   * (A45-#21 在别处点名的正是这个写法)。
   */
  const seededProduct = params.get('product_id')
  useEffect(() => {
    if (!seededProduct) return
    setSubmitFor(seededProduct)
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        next.delete('product_id')
        return next
      },
      { replace: true },
    )
  }, [seededProduct, setParams])

  const listQuery = useQuery({
    queryKey: ['publish-listings', { channel, shopId, tag, page, pageSize }],
    queryFn: () =>
      publishApi.listings({
        channel: channel || undefined,
        shop_id: shopId || undefined,
        test_batch_tag: tag || undefined,
        page,
        page_size: pageSize,
      }),
  })

  const detailQuery = useQuery({
    queryKey: ['publish-listing', openId],
    queryFn: () => publishApi.get(openId as string),
    enabled: Boolean(openId),
  })

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['publish-listings'] })
    if (openId) queryClient.invalidateQueries({ queryKey: ['publish-listing', openId] })
    /*
     * 提交与下架会改 `ChannelListing.status`,而工作台按它算流程步骤 ——
     * 不失效这两个键的话,运营切回工作台看到的是上一次的状态
     * (A45-#37 在审核页点名的是同一个形状)
     */
    queryClient.invalidateQueries({ queryKey: ['workbench'] })
    queryClient.invalidateQueries({ queryKey: ['products'] })
  }
  const onWriteError = useWriteError(refresh)

  const submitMutation = useMutation({
    mutationFn: ({
      productId,
      dryRun,
      shop,
      batchTag,
    }: {
      productId: string
      dryRun: boolean
      shop: string
      batchTag?: string
    }) =>
      publishApi.submit(productId, {
        shop_id: shop,
        dry_run: dryRun,
        test_batch_tag: batchTag || null,
      }),
    onSuccess: (result) => {
      setLastSubmit(result)
      setSubmitFor(null)
      refresh()
    },
    onError: onWriteError,
  })

  const pollMutation = useMutation({
    mutationFn: (listingId: string) => publishApi.poll(listingId),
    onSuccess: () => {
      message.success('已问过平台一次')
      refresh()
    },
    onError: onWriteError,
  })

  /**
   * A45-batch12-2 / EX-04:两条恢复动作。
   *
   * 都要求填一句说明,后端 422 挡空值。前端**不重复实现**那条校验(硬规则:
   * 业务规则只有一份),这里只负责把输入收上来 —— 填不出来时后端报的那句话
   * 比前端自己编的更准确。
   */
  const reconcileMutation = useMutation({
    mutationFn: ({ listingId, note }: { listingId: string; note: string }) =>
      publishApi.reconcile(listingId, note),
    onSuccess: () => {
      message.success('已带原幂等键问过平台一次')
      refresh()
    },
    onError: onWriteError,
  })

  const redeliverMutation = useMutation({
    mutationFn: ({ listingId, note }: { listingId: string; note: string }) =>
      publishApi.redeliver(listingId, note),
    onSuccess: () => {
      message.success('已重新排队,等待投递')
      refresh()
    },
    onError: onWriteError,
  })

  /**
   * 逐条问一遍平台(§6.4 清单那一段的补齐)。
   *
   * ## 为什么这颗按钮值得存在
   *
   * 「刷新状态」是逐行的,而在途记录十几条时运营要点十几次、每次等一个来回。
   * 这是**读**动作(`POST /publish/listings/{id}/poll` 只问平台不改意图),
   * 多问一次没有任何副作用 —— 与提交、下架完全不同,所以它不需要确认框。
   *
   * ## 三个刻意的选择
   *
   *     只处理当前这一页    分页之外的记录不在屏幕上,一颗按钮动了看不见的东西
   *                         是这一页最不该有的行为
   *     谁能问由后端说了算  判据是 `allowed_actions` 里有没有 POLL,不是前端
   *                         按状态自己挑(硬规则 4)。DEAD / 已下架的行不会被打扰
   *     串行不并发          并发几十个请求会把平台的限流打出来,而限流回来的
   *                         错误会被记进每一条 attempt —— 为了快几秒钟污染台账
   *                         不划算
   *
   * 单条失败**不中断整轮**:剩下的仍然值得问。结束时给一句总结,
   * 失败数不为零时用 warning —— 一句"已刷新"盖住三条失败是这一页最贵的谎。
   */
  /**
   * 当前这一页里后端说可以刷新的行。
   *
   * 判据只有一条:`allowed_actions` 里有没有 `POLL`。不按 `display_status`
   * 自己挑 —— 那等于在前端重写一遍"什么状态该问平台",而那份规则住在
   * `workflows/publish_view.py`。
   */
  const pollable = (listQuery.data?.items ?? []).filter((row) =>
    row.allowed_actions.includes('POLL'),
  )

  const pollAllMutation = useMutation({
    mutationFn: async (rows: ChannelListingRow[]) => {
      let ok = 0
      let failed = 0
      for (const row of rows) {
        try {
          await publishApi.poll(row.listing_id)
          ok += 1
        } catch {
          failed += 1
        }
      }
      return { ok, failed }
    },
    onSuccess: ({ ok, failed }) => {
      if (failed) message.warning(msg(`问过 ${ok + failed} 条:${ok} 条成功,${failed} 条失败`))
      else message.success(msg(`已问过平台 ${ok} 条`))
      refresh()
    },
    // 整轮失败(比如登录态掉了)才走到这里:单条失败在上面被吞进计数了
    onError: onWriteError,
  })

  const delistMutation = useMutation({
    mutationFn: (listingId: string) => publishApi.delist(listingId),
    onSuccess: ({ notice }) => {
      message[notice.will_deliver ? 'success' : 'warning'](notice.message)
      refresh()
    },
    onError: onWriteError,
  })

  /**
   * 下架前确认一次。
   *
   * 它不是"危险操作都加个确认"的条件反射:下架在多数平台上**撤不回来**,
   * 而这一页上真实渠道和模拟器的行混在一起长得几乎一样。确认框里
   * 复述的正是那两件分辨不出来的事 —— 是不是真实渠道、外部 ID 是哪个。
   */
  const confirmDelist = (row: ChannelListingRow) => {
    modal.confirm({
      title: '确认下架?',
      okText: '排队下架',
      okButtonProps: { danger: true },
      cancelText: '取消',
      content: (
        <Space direction="vertical" size={4}>
          <span>
            渠道 <b>{row.channel}</b> / 店铺 <b>{row.shop_id}</b>
          </span>
          <span className="mono">外部 ID:{row.external_spu_id ?? '—'}</span>
          <span>
            {row.simulated
              ? '这是渠道模拟器造的记录,平台上并不存在这个商品。'
              : '这是真实渠道上的商品,下架之后通常撤不回来。'}
          </span>
        </Space>
      ),
      onOk: () => delistMutation.mutateAsync(row.listing_id).then(() => undefined),
    })
  }

  /**
   * 恢复动作的确认框(EX-04)。
   *
   * 必须复述的只有一句:**这不会在平台上多出一件商品。** 运营对"再发一次
   * 请求"的天然恐惧正是这两个出口以前没人敢做的原因,而恐惧的来源是分不清
   * "复用原键"和"重新提交"。说清楚它,人才会用这个出口,而不是绕去改草稿。
   */
  const confirmRecover = (
    action: 'RECONCILE' | 'REDELIVER',
    row: ChannelListingRow,
  ) => {
    let note = ''
    modal.confirm({
      title: action === 'RECONCILE' ? '去平台核对这次提交?' : '重新投递这条记录?',
      okText: action === 'RECONCILE' ? '带原幂等键去问' : '重新排队',
      cancelText: '取消',
      content: (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <span>
            {action === 'RECONCILE'
              ? '发出去的是同一份报文、同一把幂等键。平台要么告诉我们这个键见过,要么返回上次的结果 —— 两种都不会多出商品。'
              : '复用原来那条提交尝试与它的幂等键重新排队,退避从零重算。不会创建第二件商品。'}
          </span>
          <span className="mono">外部 ID:{row.external_spu_id ?? '—'}</span>
          <Input.TextArea
            rows={3}
            maxLength={500}
            placeholder={
              action === 'RECONCILE'
                ? '核对结论 / 为什么现在去问(必填,进审计)'
                : '为什么认为现在投得出去(必填,进审计)'
            }
            onChange={(e) => {
              note = e.target.value
            }}
          />
        </Space>
      ),
      onOk: () =>
        (action === 'RECONCILE' ? reconcileMutation : redeliverMutation)
          .mutateAsync({ listingId: row.listing_id, note })
          .then(() => undefined),
    })
  }

  const runAction = (action: PublishAction, row: ChannelListingRow) => {
    if (action === 'POLL') return pollMutation.mutate(row.listing_id)
    if (action === 'DELIST') return confirmDelist(row)
    if (action === 'RECONCILE' || action === 'REDELIVER')
      return confirmRecover(action, row)
    // SUBMIT / DRY_RUN 都要 shop_id,走同一个弹窗;店铺号用这一行已有的做初值
    form.setFieldsValue({ shop_id: row.shop_id, dry_run: action === 'DRY_RUN' })
    setSubmitFor(row.product_id)
  }

  const columns: ColumnsType<ChannelListingRow> = [
    {
      title: '发布状态',
      key: 'status',
      width: 190,
      render: (_, row) => (
        <Space direction="vertical" size={2}>
          <Space size={4}>
            <Tag color={STATUS_COLOR[row.display_status] ?? 'default'}>
              {row.display_status_label}
            </Tag>
            <RealityTag simulated={row.simulated} />
          </Space>
          <span style={{ fontSize: fontScale.meta, color: brandVars.slate }}>
            下一步:{row.next_action_label}
          </span>
        </Space>
      ),
    },
    {
      title: '渠道 / 店铺',
      key: 'channel',
      width: 160,
      render: (_, row) => (
        <div>
          <div>
            {row.channel}
            <span style={{ color: brandVars.slate }}> · {row.site}</span>
          </div>
          <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>{row.shop_id}</div>
        </div>
      ),
    },
    {
      title: '外部商品 ID',
      key: 'external',
      width: 190,
      render: (_, row) =>
        row.external_spu_id ? (
          <span className="mono" style={{ fontSize: fontScale.meta }}>
            {row.external_spu_id}
          </span>
        ) : (
          <Tooltip title="还没有外部 ID —— 那次提交从没到达平台。刷新与下架都需要它">
            <span style={{ color: brandVars.textFaint }}>—</span>
          </Tooltip>
        ),
    },
    {
      title: '阻断',
      key: 'blockers',
      render: (_, row) =>
        row.blocking_reasons.length === 0 ? (
          <span style={{ color: brandVars.textFaint }}>—</span>
        ) : (
          <Space direction="vertical" size={2}>
            {row.blocking_reasons.map((b) => (
              <Tooltip key={b.code} title={b.advice}>
                <Tag color="error" style={{ whiteSpace: 'normal' }}>
                  {b.label}
                </Tag>
              </Tooltip>
            ))}
          </Space>
        ),
    },
    {
      title: '最近同步',
      key: 'polled',
      width: 150,
      render: (_, row) => (
        <div style={{ fontSize: fontScale.meta }}>
          <div>{formatDateTime(row.last_polled_at)}</div>
          <div style={{ color: brandVars.slate }}>
            {row.last_platform_status ?? '平台未回过状态'}
          </div>
        </div>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 230,
      render: (_, row) => (
        <Space size={4} wrap>
          {/*
            按钮**只**从 `allowed_actions` 渲染。
            这不是省代码:这个数组一旦出现一个没有端点的动作,前端会渲染出一个
            点了报 404 的按钮 —— 反过来,前端自己判断"应该能点"就会渲染出一个
            后端拒绝的按钮,而那种错误只有运营会碰到。
          */}
          {row.allowed_actions.map((action) => (
            <Button
              key={action}
              size="small"
              icon={ACTION_ICON[action]}
              danger={action === 'DELIST'}
              loading={
                (action === 'POLL' &&
                  pollMutation.isPending &&
                  pollMutation.variables === row.listing_id) ||
                (action === 'DELIST' &&
                  delistMutation.isPending &&
                  delistMutation.variables === row.listing_id)
              }
              onClick={() => runAction(action, row)}
            >
              {ACTION_LABEL[action]}
            </Button>
          ))}
          <Button size="small" type="link" onClick={() => setOpenId(row.listing_id)}>
            详情
          </Button>
        </Space>
      ),
    },
  ]

  const detail = detailQuery.data

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader
        title="发布上架"
        subtitle="提交商品到平台、跟踪审核状态，并按批次清理测试商品"
        extra={
          <Space>
            <Tooltip
              title={
                pollable.length
                  ? `逐条问一遍平台,只涉及当前这一页可刷新的 ${pollable.length} 条。这是读操作,不会改变任何提交`
                  : '当前这一页没有可刷新的记录 —— 能不能刷新由后端的 allowed_actions 决定'
              }
            >
              {/* 禁用态的按钮不触发 Tooltip,所以外面包一层 span(同 a69 对
                  SpuCreatePage 那颗删除按钮的处理) */}
              <span>
                <Button
                  icon={<SyncOutlined />}
                  disabled={pollable.length === 0 || pollAllMutation.isPending}
                  loading={pollAllMutation.isPending}
                  onClick={() => pollAllMutation.mutate(pollable)}
                >
                  刷新全部在途（{pollable.length}）
                </Button>
              </span>
            </Tooltip>
            <Button
              icon={<ReloadOutlined />}
              loading={listQuery.isFetching}
              onClick={() => listQuery.refetch()}
            >
              刷新
            </Button>
          </Space>
        }
      />

      {lastSubmit && (
        <Card size="small" styles={{ body: { padding: space.md } }}>
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <NoticeAlert notice={lastSubmit.notice} dryRun={lastSubmit.dry_run} />
            <Descriptions size="small" column={3} bordered>
              <Descriptions.Item label="动作">
                {PUBLISH_OPERATION_LABEL[lastSubmit.operation] ?? lastSubmit.operation}
              </Descriptions.Item>
              <Descriptions.Item label="幂等键">
                <span className="mono" style={{ fontSize: fontScale.meta }}>
                  {lastSubmit.idempotency_key ?? '—'}
                </span>
              </Descriptions.Item>
              <Descriptions.Item label="沿用了旧尝试">
                {lastSubmit.reused ? '是' : '否'}
              </Descriptions.Item>
            </Descriptions>
            <Typography.Paragraph style={{ margin: 0 }}>
              <Typography.Text type="secondary" style={{ fontSize: fontScale.meta }}>
                下面是本次生成的报文（已脱敏）。预览时只生成、不发送；正式提交后也会保留，
                便于核对实际发送的内容。
              </Typography.Text>
            </Typography.Paragraph>
            <Input.TextArea
              readOnly
              autoSize={{ minRows: 4, maxRows: 14 }}
              value={JSON.stringify(lastSubmit.prepared_request, null, 2)}
              style={{ fontFamily: 'monospace', fontSize: fontScale.meta }}
            />
            <Button size="small" onClick={() => setLastSubmit(null)}>
              收起
            </Button>
          </Space>
        </Card>
      )}

      <Tabs
        items={[
          {
            key: 'listings',
            label: '发布清单',
            children: (
              <Space direction="vertical" size={12} style={{ width: '100%' }}>
                <Card size="small" styles={{ body: { padding: space.md } }}>
                  <Space wrap>
                    <Input
                      allowClear
                      placeholder="渠道（如 SHEIN）"
                      style={{ width: 160 }}
                      value={channel}
                      onChange={(e) => {
                        setChannel(e.target.value)
                        setPage(1)
                      }}
                    />
                    <Input
                      allowClear
                      placeholder="店铺 ID"
                      style={{ width: 160 }}
                      value={shopId}
                      onChange={(e) => {
                        setShopId(e.target.value)
                        setPage(1)
                      }}
                    />
                    <Input
                      allowClear
                      placeholder="测试批次标签"
                      style={{ width: 180 }}
                      value={tag}
                      onChange={(e) => {
                        setTag(e.target.value)
                        setPage(1)
                      }}
                    />
                  </Space>
                </Card>

                {listQuery.isError && (
                  <ErrorNotice
                    title="拉不到发布清单"
                    error={listQuery.error}
                    onRetry={() => listQuery.refetch()}
                  />
                )}

                <Table<ChannelListingRow>
                  rowKey="listing_id"
                  size="small"
                  bordered
                  loading={listQuery.isLoading}
                  columns={columns}
                  dataSource={listQuery.data?.items ?? []}
                  locale={{
                    emptyText: (
                      <Empty
                        description={
                          <span>
                            还没有任何发布记录。第一次提交从工作台的「导出」标签页进入 ——
                            那里有「发布到平台」。
                          </span>
                        }
                      />
                    ),
                  }}
                  pagination={{
                    current: listQuery.data?.page ?? page,
                    pageSize: listQuery.data?.page_size ?? pageSize,
                    total: listQuery.data?.total ?? 0,
                    showSizeChanger: true,
                    showTotal: (t) => `共 ${t} 条`,
                    onChange: (p, size) => {
                      setPage(p)
                      setPageSize(size)
                    },
                  }}
                />
              </Space>
            ),
          },
          {
            key: 'cleanup',
            label: '清理预案',
            children: <CleanupPanel />,
          },
        ]}
      />

      <Drawer
        width={720}
        open={Boolean(openId)}
        onClose={() => setOpenId(null)}
        title="发布详情"
        destroyOnClose
      >
        {detailQuery.isError && (
          <ErrorNotice
            title="拉不到发布详情"
            error={detailQuery.error}
            onRetry={() => detailQuery.refetch()}
          />
        )}
        {detail && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Space size={6} wrap>
              <Tag color={STATUS_COLOR[detail.display_status] ?? 'default'}>
                {detail.display_status_label}
              </Tag>
              <RealityTag simulated={detail.simulated} />
              <Tag>下一步:{detail.next_action_label}</Tag>
            </Space>

            {detail.blocking_reasons.length > 0 && (
              <Alert
                type="error"
                showIcon
                message="有阻断,现在提交不会成功"
                description={
                  <Space direction="vertical" size={4}>
                    {detail.blocking_reasons.map((b) => (
                      <span key={b.code}>
                        <b>{b.label}</b> —— {b.advice}
                      </span>
                    ))}
                  </Space>
                }
              />
            )}

            {/* 发布前 */}
            <Card size="small" title="发布前:草稿">
              {detail.draft ? (
                <Descriptions size="small" column={2}>
                  <Descriptions.Item label="草稿状态">
                    {DRAFT_STATUS_LABEL[detail.draft.status]?.text ?? detail.draft.status}
                  </Descriptions.Item>
                  <Descriptions.Item label="版本">v{detail.draft.version}</Descriptions.Item>
                  <Descriptions.Item label="语言">
                    {detail.draft.locale ?? '—'}
                  </Descriptions.Item>
                  <Descriptions.Item label="可提交">
                    {detail.draft.submittable ? (
                      <Badge status="success" text="是" />
                    ) : (
                      <Badge status="error" text="否" />
                    )}
                  </Descriptions.Item>
                </Descriptions>
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="找不到对应草稿。老数据的 draft_id 可能是空的"
                />
              )}
            </Card>

            {/* 发布中 */}
            <Card size="small" title="发布中:最近一次尝试与投递">
              {detail.latest_attempt ? (
                <Descriptions size="small" column={2} bordered>
                  <Descriptions.Item label="动作">
                    {PUBLISH_OPERATION_LABEL[detail.latest_attempt.operation]
                      ?? detail.latest_attempt.operation}
                  </Descriptions.Item>
                  <Descriptions.Item label="尝试结果">
                    {PUBLISH_ATTEMPT_STATUS_LABEL[detail.latest_attempt.status]
                      ?? detail.latest_attempt.status}
                  </Descriptions.Item>
                  <Descriptions.Item label="开始">
                    {formatDateTime(detail.latest_attempt.started_at)}
                  </Descriptions.Item>
                  <Descriptions.Item label="结束">
                    {formatDateTime(detail.latest_attempt.finished_at)}
                  </Descriptions.Item>
                  <Descriptions.Item label="平台请求 ID" span={2}>
                    <span className="mono" style={{ fontSize: fontScale.meta }}>
                      {detail.latest_attempt.provider_request_id ?? '—'}
                    </span>
                  </Descriptions.Item>
                  {detail.latest_attempt.error_code && (
                    <Descriptions.Item label="错误" span={2}>
                      <Typography.Text type="danger">
                        {detail.latest_attempt.error_code} ——{' '}
                        {detail.latest_attempt.error_message ?? ''}
                      </Typography.Text>
                    </Descriptions.Item>
                  )}
                  <Descriptions.Item label="投递" span={2}>
                    {detail.outbox ? (
                      <Space size={8} wrap>
                        <Tag>
                          {PUBLISH_OUTBOX_STATUS_LABEL[detail.outbox.status]
                            ?? detail.outbox.status}
                        </Tag>
                        <span>已尝试 {detail.outbox.attempt_count} 次</span>
                        <span style={{ color: brandVars.slate }}>
                          下次:{formatDateTime(detail.outbox.next_attempt_at)}
                        </span>
                        {detail.outbox.last_error && (
                          <Typography.Text type="danger" style={{ fontSize: fontScale.meta }}>
                            {detail.outbox.last_error}
                          </Typography.Text>
                        )}
                      </Space>
                    ) : (
                      <span style={{ color: brandVars.textFaint }}>没有投递记录</span>
                    )}
                  </Descriptions.Item>
                </Descriptions>
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="还没有提交过"
                />
              )}
            </Card>

            {/* 发布后 */}
            <Card size="small" title="发布后:平台状态与驳回">
              <Descriptions size="small" column={2} style={{ marginBottom: space.sm }}>
                <Descriptions.Item label="平台状态">
                  {detail.last_platform_status ?? '—'}
                </Descriptions.Item>
                <Descriptions.Item label="轮询次数">
                  {detail.poll_attempt_count}
                </Descriptions.Item>
                <Descriptions.Item label="最近同步">
                  {formatDateTime(detail.last_polled_at)}
                </Descriptions.Item>
                <Descriptions.Item label="下次自动同步">
                  {formatDateTime(detail.next_poll_at)}
                </Descriptions.Item>
              </Descriptions>
              {detail.unresolved_rejections.length === 0 ? (
                <Typography.Text type="secondary">没有未解决的驳回。</Typography.Text>
              ) : (
                <Space direction="vertical" size={6} style={{ width: '100%' }}>
                  {detail.unresolved_rejections.map((r) => (
                    <Alert
                      key={r.id}
                      type="warning"
                      showIcon
                      message={`${REJECTION_REASON_LABEL[r.reason_code] ?? r.reason_code}(${
                        PUBLISH_REJECTION_STATUS_LABEL[r.status] ?? r.status
                      })`}
                      description={
                        <Space direction="vertical" size={2}>
                          {r.platform_note && <span>{r.platform_note}</span>}
                          <span style={{ fontSize: fontScale.meta, color: brandVars.slate }}>
                            发现于 {formatDateTime(r.created_at)} · 定位依据{' '}
                            {r.located_by ? LOCATED_BY_LABEL[r.located_by] ?? r.located_by : '—'}
                          </span>
                        </Space>
                      }
                    />
                  ))}
                </Space>
              )}
            </Card>

            <Space wrap>
              {detail.allowed_actions.map((action) => (
                <Button
                  key={action}
                  icon={ACTION_ICON[action]}
                  danger={action === 'DELIST'}
                  onClick={() => runAction(action, detail)}
                >
                  {ACTION_LABEL[action]}
                </Button>
              ))}
            </Space>
          </Space>
        )}
      </Drawer>

      <Modal
        open={Boolean(submitFor)}
        title="提交上架"
        okText="提交"
        confirmLoading={submitMutation.isPending}
        onCancel={() => setSubmitFor(null)}
        onOk={() =>
          form.validateFields().then((values) =>
            submitMutation.mutate({
              productId: submitFor as string,
              dryRun: Boolean(values.dry_run),
              shop: values.shop_id,
              batchTag: values.test_batch_tag,
            }),
          )
        }
      >
        <Form form={form} layout="vertical" initialValues={{ dry_run: true }}>
          <Alert
            style={{ marginBottom: space.md }}
            type="info"
            showIcon
            message="建议先预览报文"
            description="预览只构造报文，不会发送。确认无误后再取消勾选并正式提交；平台上架通常不能立即撤回。"
          />
          <Form.Item
            name="shop_id"
            label="店铺 ID"
            rules={[{ required: true, message: '必须指定店铺' }]}
          >
            <Input placeholder="平台侧的店铺标识" />
          </Form.Item>
          <Form.Item
            name="test_batch_tag"
            label="测试批次标签"
            extra="人工测试期间务必填。不填也进得了清理清单(靠店铺过滤),但分不出是哪一轮建的"
          >
            <Input placeholder="如 uat-2026-08-03" />
          </Form.Item>
          <Form.Item name="dry_run" valuePropName="checked">
            <Checkbox>仅预览，不提交</Checkbox>
          </Form.Item>
          <Typography.Text type="secondary" style={{ fontSize: fontScale.meta }}>
            商品:<span className="mono">{submitFor}</span>
          </Typography.Text>
        </Form>
      </Modal>
    </Space>
  )
}

/**
 * 清理预案(4.1 节 H 第二条)。
 *
 * ## 为什么这一块不自动加载
 *
 * 后端强制要求至少一个作用域参数,否则 400。那道护栏的理由是:不限定的话
 * 这份清单会覆盖全部渠道的全部商品,而运营会拿着它去平台后台逐条核对 ——
 * 一份混进了别人商品的清单核不上,然后就没人信它了。
 *
 * 前端如实照做:没填任何条件时按钮是禁用的,并说明为什么,而不是替他
 * 随便填一个默认值把 400 绕过去。
 */
function CleanupPanel() {
  const [tag, setTag] = useState('')
  const [shopId, setShopId] = useState('')
  const [channel, setChannel] = useState('')
  const [includeSimulated, setIncludeSimulated] = useState(true)
  const [scope, setScope] = useState<{
    tag: string
    shopId: string
    channel: string
    includeSimulated: boolean
  } | null>(null)

  const hasScope = Boolean(tag || shopId || channel)

  const query = useQuery({
    queryKey: ['publish-cleanup', scope],
    queryFn: () =>
      publishApi.cleanupInventory({
        test_batch_tag: scope?.tag || undefined,
        shop_id: scope?.shopId || undefined,
        channel: scope?.channel || undefined,
        include_simulated: scope?.includeSimulated,
      }),
    enabled: Boolean(scope),
  })

  const columns: ColumnsType<CleanupRow> = useMemo(
    () => [
      {
        title: 'SKU',
        key: 'sku',
        width: 180,
        render: (_, row) => (
          <div>
            <div className="mono">{row.sku}</div>
            <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>{row.spu}</div>
          </div>
        ),
      },
      {
        title: '外部 ID',
        key: 'external',
        width: 200,
        render: (_, row) => (
          <Space size={4}>
            <span className="mono" style={{ fontSize: fontScale.meta }}>
              {row.external_spu_id}
            </span>
            <RealityTag simulated={row.simulated} />
          </Space>
        ),
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 150,
        render: (status: string) => PUBLISH_STATUS_LABEL[status] ?? status,
      },
      {
        title: '占位',
        key: 'occupying',
        width: 90,
        render: (_, row) =>
          row.occupying ? <Tag color="warning">占着</Tag> : <Tag>已释放</Tag>,
      },
      {
        title: '可自动下架',
        key: 'delistable',
        render: (_, row) =>
          row.delistable ? (
            <Badge status="success" text="可以" />
          ) : (
            <Tooltip title={row.reason ?? ''}>
              <Badge status="error" text={row.reason ?? '不可以'} />
            </Tooltip>
          ),
      },
    ],
    [],
  )

  const report = query.data

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Card size="small" styles={{ body: { padding: space.md } }}>
        <Space wrap>
          <Input
            allowClear
            placeholder="测试批次标签"
            style={{ width: 180 }}
            value={tag}
            onChange={(e) => setTag(e.target.value)}
          />
          <Input
            allowClear
            placeholder="店铺 ID"
            style={{ width: 160 }}
            value={shopId}
            onChange={(e) => setShopId(e.target.value)}
          />
          <Input
            allowClear
            placeholder="渠道"
            style={{ width: 140 }}
            value={channel}
            onChange={(e) => setChannel(e.target.value)}
          />
          <Select
            style={{ width: 200 }}
            value={includeSimulated ? 'all' : 'real'}
            onChange={(v) => setIncludeSimulated(v === 'all')}
            options={[
              { value: 'all', label: '含模拟记录' },
              { value: 'real', label: '只看真实渠道' },
            ]}
          />
          <Tooltip
            title={
              hasScope
                ? ''
                : '至少填一个条件。不限定的话这份清单会覆盖全部渠道的全部商品,拿去平台后台核不上'
            }
          >
            <Button
              type="primary"
              disabled={!hasScope}
              loading={query.isFetching}
              onClick={() => setScope({ tag, shopId, channel, includeSimulated })}
            >
              生成清单
            </Button>
          </Tooltip>
        </Space>
      </Card>

      {query.isError && (
        <ErrorNotice
          title="生成清单失败"
          error={query.error}
          onRetry={() => query.refetch()}
        />
      )}

      {!scope && (
        <Empty description="填一个作用域(批次标签 / 店铺 / 渠道),再生成清单。" />
      )}

      {report && (
        <>
          <Card size="small">
            <Space size={32} wrap>
              <Statistic title="记录总数" value={report.total} />
              <Statistic title="真实渠道" value={report.real} />
              <Statistic title="模拟记录" value={report.simulated} />
              <Statistic
                title="仍占着平台位置"
                value={report.occupying}
                valueStyle={{ color: report.occupying ? '#cf1322' : undefined }}
              />
              <Statistic title="不能自动下架" value={report.not_delistable} />
            </Space>
          </Card>
          {/*
            `to_queue` 与 `occupying` 不是一个口径,而这个区别必须显示出来:
            后者只数真实商品(回答"平台上还占着几个位置"),前者是**这次动手
            真的会排掉几行**,含模拟行。10 模拟 + 2 真实的批次,只看 occupying
            会以为要处理 2 条,实际会排出去 12 条。
          */}
          <Alert
            type={report.clean ? 'success' : 'warning'}
            showIcon
            message={
              report.clean
                ? '这个作用域已经干净了:没有仍占着平台位置的商品。'
                : `真动手会排队下架 ${report.to_queue} 行、跳过 ${report.to_skip} 行。`
            }
            description={
              report.clean ? undefined : (
                <span style={{ fontSize: fontScale.body }}>
                  「排队下架」的行数含模拟记录,和上面「仍占着平台位置」
                  ({report.occupying})不是一个口径 —— 后者只数真实渠道。
                  逐行下架在「发布清单」里操作。
                </span>
              )
            }
          />
          <Table<CleanupRow>
            rowKey="listing_id"
            size="small"
            bordered
            columns={columns}
            dataSource={report.rows}
            pagination={report.rows.length > 20 ? { pageSize: 20 } : false}
          />
        </>
      )}
    </Space>
  )
}
