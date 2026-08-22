/**
 * 今日待办(A9)。
 *
 * ## 为什么把首页换掉
 *
 * 上一版首页是 `/dashboard` —— 任务总数、平均生成轮次、各 Provider 调用次数、
 * 分档分布。那些数字是给管理员看系统跑得好不好的,不是给运营看今天干什么的:
 * 新运营登录后第一屏读完,仍然不知道自己该点哪儿。所以 `/` 改指这一页,
 * `/dashboard` 原样保留在「系统管理」里。
 *
 * ## 每张卡片是一次筛选,不是一个数字
 *
 * 点「待确认属性 7」就落到已经筛好 `CONFIRM_ATTRIBUTES` 的工作台列表 ——
 * 也就是 FE-106 那条「计数即筛选」。为此三个落地页(工作台列表、逐件快审、
 * 生成任务)这一轮补上了从 URL 读筛选条件的能力:在此之前它们的筛选全是
 * 组件内 `useState`,卡片点过去只会落到一张未筛选的全量列表上,
 * 首页也就等于没做。
 *
 * ## 为什么还要单列任务级卡片
 *
 * `next_action` 只描述商品生产流程,覆盖不了生成任务自己的失败、人工复核和
 * 运行中状态。这三类仍然会占用运营注意力,所以通过 `/dashboard/summary`
 * 单独显示；否则任务停在 MANUAL_REVIEW 时,首页仍会让人误以为没有待办。
 *
 * ## 数字全部来自后端
 *
 * `by_next_action` 由 `flow.summarize` 给,`failed` / `in_flight` 由
 * `/dashboard/summary` 给。这一页**一个加号都没有** —— §3.2.1 与 §6.3.2 定过
 * 两次的纪律:前端不推算状态、不重算验收数字,否则验收会上会出现两个数,
 * 然后花半小时争论信哪个。所以「其他待办」那一行只列每个码各自的数,
 * 不给它们求和。
 *
 * ## 「运行中任务」这张卡为什么不带筛选
 *
 * 它的计数是一个**状态集合**(后端 `IN_FLIGHT_STATES`:排队、预处理、提交中、
 * Provider 执行中、下载中),而任务列表接口只收单个 status。为它扩一个
 * 多状态筛选参数属于 Gate A 期间的扩张(§2.2),所以这张卡点进去是全量任务列表,
 * 并且卡片上把这件事说出来 —— 不做那种"卡片说 12、落地页显示 40"的错位。
 */
import { useMemo, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, Card, Empty, Skeleton, Space, Tag, Tooltip, Typography } from 'antd'
import {
  ClockCircleOutlined, ExportOutlined, EyeOutlined, FileTextOutlined, InfoCircleOutlined,
  LoadingOutlined, PictureOutlined, ProfileOutlined, ReloadOutlined, StopOutlined,
  TagsOutlined, WarningOutlined,
} from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import ErrorNotice from '../components/ErrorNotice'
import { dashboardApi } from '../api/exports'
import {
  NEXT_ACTION_LABEL,
  workbenchApi,
  type NextActionCode,
} from '../api/workbench'
import { brandVars, fontScale } from '../theme'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { formatClock } from '../utils/datetime'
import { stalestOf } from '../utils/freshness'

/**
 * 自动刷新节拍。
 *
 * ## 为什么这一页必须自己刷
 *
 * 全局 `QueryClient` 设的是 `refetchOnWindowFocus: false`(`main.tsx`,理由是
 * 冷启动那一帧十几个 query 同时重试)。于是**不设 `refetchInterval` 的页面
 * 一旦渲染完就永远不再取数** —— 而这一页是运营开着不关的那一页:
 * 上午拉的数字下午还挂在屏幕上,点进去落地页却是空的。
 * 一份不会更新的待办清单,运营只会被骗一次,之后就不看了。
 *
 * ## 为什么是 60 秒
 *
 * 这一页的每次刷新要对全部商品跑一遍 flow 判定(`summarize` 的注释里算过
 * 这笔账),比任务列表那种按行读状态的查询贵得多。而它回答的是"今天先干哪件",
 * 不是"这一秒发生了什么" —— 分钟级足够,再快只是替后端加压。
 * 任务详情那种真的在等结果的页面用的是 2~3 秒,两者不该同一个数。
 */
const REFRESH_MS = 60_000

/** 卡片上的一档紧急度。只影响配色,不影响排序 —— 排序是固定的(见下) */
type Tone = 'act' | 'wait' | 'idle'

interface CardSpec {
  key: string
  label: string
  icon: ReactNode
  /** 这张卡的计数。undefined 表示还没读到 */
  count: number | undefined
  /** 点进去落到哪儿。已经带好筛选参数 */
  to: string
  /** 仅在标题不足以说清口径时提供；默认藏在右上角说明里 */
  help?: string
  tone: Tone
}

const TONE_COLOR: Record<Tone, string> = {
  act: brandVars.marine,
  wait: brandVars.slate,
  idle: brandVars.textMuted,
}

/**
 * 首页主要卡片之外的动作码。
 *
 * A9 点名的是一组主要卡片,但动作码有十七个 —— 卡在「补充素材」「生成文案」
 * 「生成上架草稿」的商品如果一处都不显示,运营看完首页的结论会是"今天没事干",
 * 而实际上有五件在等他上传背面图。所以其余的码收成一行小标签,
 * 有几件就显示几件,归零的不显示。
 *
 * `DONE` 不在这里:它是"已导出、无待办",不是待办。
 */
const SECONDARY_ACTIONS: NextActionCode[] = [
  // 建档与方案(阶段 6 的第一、四步,A45-batch27)。放在最前:它们排在
  // 流程最前面,而"整整一步对运营是隐形的"正是这张表的守卫要防的事
  'COMPLETE_SETUP',
  'CHOOSE_PLAN',
  'UPLOAD_MATERIAL',

  'RELEASE_QUARANTINE',
  'CONFIRM_ASSET_ROLE',
  'RUN_EXTRACTION',
  'RESOLVE_CONFLICT',
  'FILL_ATTRIBUTES',
  'BUILD_IMAGE_SET',
  'FIX_IMAGE_SET',
  'GENERATE_COPY',
  'FIX_COPY',
  'BUILD_DRAFT',
  'FIX_DRAFT',
  'REFRESH_DRAFT',
]

function CountCard({ spec, onOpen }: { spec: CardSpec; onOpen: (to: string) => void }) {
  const empty = spec.count === 0
  return (
    <Card
      size="small"
      hoverable
      onClick={() => onOpen(spec.to)}
      styles={{ body: { padding: 14 } }}
      style={{ opacity: empty ? 0.62 : 1 }}
    >
      <Space direction="vertical" size={2} style={{ width: '100%' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 8,
            minHeight: 24,
          }}
        >
          <Space size={6} style={{ color: brandVars.slate, fontSize: fontScale.body }}>
            {spec.icon}
            {spec.label}
          </Space>
          {spec.help && (
            <Tooltip title={spec.help} placement="top">
              <Button
                type="text"
                size="small"
                icon={<InfoCircleOutlined />}
                aria-label={`查看“${spec.label}”说明`}
                onClick={(event) => event.stopPropagation()}
                style={{ color: brandVars.textMuted, flex: '0 0 auto' }}
              />
            </Tooltip>
          )}
        </div>
        <div
          style={{
            fontSize: fontScale.metricLg,
            lineHeight: 1.2,
            fontWeight: 600,
            color: empty ? TONE_COLOR.idle : TONE_COLOR[spec.tone],
          }}
        >
          {spec.count ?? '—'}
        </div>
      </Space>
    </Card>
  )
}

function RunningTaskIcon({ count }: { count: number | undefined }) {
  // LoadingOutlined 自带无限旋转。没有运行中任务时仍然用它,会让首页看起来像
  // 一直没有加载完;只有后端明确报告有运行中任务时,旋转才表达真实状态。
  return count !== undefined && count > 0 ? <LoadingOutlined /> : <ClockCircleOutlined />
}

export default function TodayPage() {

  const navigate = useNavigate()

  // page_size=1:只要顶部那份 summary。它统计的是全部匹配商品,与分页无关,
  // 所以不必把 20 条明细一起拉回来
  const board = useQuery({
    queryKey: ['today-board'],
    queryFn: () => workbenchApi.list({ page: 1, page_size: 1 }),
    refetchInterval: REFRESH_MS,
  })

  // 生成任务的两个数在另一个接口里。刻意不为首页新开一个"待办计数"接口:
  // 那会变成第三处定义"什么算待办"的地方
  const tasks = useQuery({
    queryKey: ['today-tasks'],
    queryFn: () => dashboardApi.summary(),
    refetchInterval: REFRESH_MS,
  })

  // 卡片来自两个接口,所以这一屏的新鲜度是两者里较旧的那个 —— 判定与理由
  // 都在 `stalestOf` 上,那里也是它唯一被测到的地方
  const updatedAt = stalestOf(board.dataUpdatedAt, tasks.dataUpdatedAt)

  const refreshing = board.isFetching || tasks.isFetching
  const refresh = () => {
    void board.refetch()
    void tasks.refetch()
  }

  const byAction = board.data?.summary.by_next_action
  const failure = board.isError ? board.error : tasks.isError ? tasks.error : null

  const cards: CardSpec[] = useMemo(
    () => [
      {
        /*
         * §8.2 明确点名的第一类待办:「待确认受众/品类 2」。
         *
         * 上一版没有这张卡,也没有 `CONFIRM_AUDIENCE` 这个动作码 —— 于是
         * `FlowHeader` 那个橙色「待确认」标记是个死胡同:它告诉你有问题,
         * 界面上没有任何地方能解决它。
         *
         * 它排在待选模特之前(§8.2):受众确认之后模特候选集才是对的,
         * 顺序反过来会让运营先选一次模特、确认受众后又被清空。
         */
        key: 'CONFIRM_AUDIENCE',
        label: '待确认受众与品类',
        icon: <TagsOutlined />,
        count: byAction?.CONFIRM_AUDIENCE,
        to: '/workbench?next_action=CONFIRM_AUDIENCE',
        help: '确认商品面向人群和品类后，系统才会匹配合适的模特和检查规则。',
        tone: 'act',
      },
      {
        key: 'CONFIRM_ATTRIBUTES',
        label: '待确认属性',
        icon: <TagsOutlined />,
        count: byAction?.CONFIRM_ATTRIBUTES,
        to: '/workbench?next_action=CONFIRM_ATTRIBUTES',
        help: '核对系统识别的商品属性；确认后才能编排图片集。',
        tone: 'act',
      },
      {
        key: 'APPROVE_IMAGE_SET',
        label: '待审核图片集',
        icon: <PictureOutlined />,
        count: byAction?.APPROVE_IMAGE_SET,
        to: '/workbench-review?filter=APPROVE_IMAGE_SET',
        help: '审核一件商品的整套图片；提交结果后会自动进入下一件。',
        tone: 'act',
      },
      {
        key: 'APPROVE_COPY',
        label: '待审核文案',
        icon: <FileTextOutlined />,
        count: byAction?.APPROVE_COPY,
        to: '/workbench-review?filter=APPROVE_COPY',
        tone: 'act',
      },
      {
        key: 'FAILED',
        label: '生成失败',
        icon: <StopOutlined />,
        count: tasks.data?.tasks.failed,
        to: '/tasks?status=FAILED',
        tone: 'act',
      },
      {
        key: 'RESOLVE_REJECTION',
        label: '待处理驳回',
        icon: <WarningOutlined />,
        count: byAction?.RESOLVE_REJECTION,
        to: '/workbench?next_action=RESOLVE_REJECTION',
        help: '处理渠道平台驳回：按原因修正内容，并重新导出或提交。',
        tone: 'act',
      },
      {
        key: 'EXPORT',
        label: '待导出',
        icon: <ExportOutlined />,
        count: byAction?.EXPORT,
        to: '/workbench?next_action=EXPORT',
        tone: 'act',
      },
      {
        key: 'MANUAL_REVIEW',
        label: '待审核候选图',
        icon: <EyeOutlined />,
        count: tasks.data?.reviews.pending,
        to: '/reviews',
        help: '审核单张候选图，通常来自自动审核未通过或抽检复核。',
        tone: 'act',
      },
      {
        key: 'IN_FLIGHT',
        label: '进行中的生成任务',
        icon: <RunningTaskIcon count={tasks.data?.tasks.in_flight} />,
        count: tasks.data?.tasks.in_flight,
        to: '/tasks',
        help: '统计排队与处理中的任务；进入任务列表后可查看全部状态。',
        tone: 'wait',
      },
    ],
    [byAction, tasks.data],
  )

  const secondary = SECONDARY_ACTIONS.filter((code) => (byAction?.[code] ?? 0) > 0)

  /**
   * 二次走查 B-3 提出「标签页标题带待办数」,上一版在这里用 `.reduce()` 求了和,
   * 并把取舍留给评审。**评审结论:不给这个例外,数字撤掉。**
   *
   * ## 为什么撤掉而不是照 B-3 做
   *
   * 撤掉的直接原因是它踩了 `test_today_page_reads_its_numbers_from_the_backend`
   * ——§3.2.1 / §6.3.2 那条「前端不重算验收数字」唯一一处机器执行的门禁。
   * 但真正的理由不是门禁红了,是**这个数字今天没有产地**:
   *
   *     待确认属性 / 待审核图片 / 待审核文案 / 平台驳回 / 可导出
   *                              ← `by_next_action`,`/workbench/products` 给
   *     生成失败 / 候选图待审      ← `/dashboard/summary` 给
   *
   * 首页的待办卡片跨**两个接口**。所以后端补一个 `pending_total` 也补不齐:
   * 补在哪一边,前端都还要把两边加一次 —— 只是把 `.reduce()` 换成一个加号,
   * 门禁看不见了,而两个数不一致的风险一点没变。那是绕过门禁,不是修好它。
   *
   * ## 真要这个徽标,该怎么做(留给排期,不要在这一层解决)
   *
   * 让 `/workbench/products` 的 summary 一并带上任务与候选图的待审计数
   * —— 它已经为 flow 判定付了全表的代价,多两条 COUNT 近乎免费 ——
   * 然后由 `flow.summarize` 输出**一个** `pending_total`,前端原样显示。
   * 这样「什么算待办」仍然只有一处定义,也不违反本文件开头那条
   * 「不为首页新开一个待办计数接口」的决定(加字段不是加接口)。
   *
   * 在那之前,标题不带数字。**少一个提示**远好过**两个不一致的数**:
   * 后者会在验收会上花掉半小时,而且第一个发现的人会是运营。
   */
  useDocumentTitle('今日工作')

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader
        title="今日工作"
        extra={
          <>
            <Typography.Text type="secondary" style={{ fontSize: fontScale.meta }}>
              {updatedAt ? `更新于 ${formatClock(updatedAt)}` : '正在读取…'}
            </Typography.Text>
            <Button size="small" icon={<ReloadOutlined />} loading={refreshing} onClick={refresh}>
              刷新
            </Button>
          </>
        }
      />

      {failure && (
        <ErrorNotice
          title="暂时无法读取今日待办"
          error={failure}
        />
      )}

      {board.isLoading && <Skeleton active paragraph={{ rows: 4 }} />}

      {!board.isLoading && (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(196px, 1fr))',
            gap: 12,
          }}
        >
          {cards.map((spec) => (
            <CountCard key={spec.key} spec={spec} onOpen={(to) => navigate(to)} />
          ))}
        </div>
      )}

      <Card size="small" title="其他待办" styles={{ body: { padding: 12 } }}>
        {secondary.length === 0 ? (
          <Empty
            image={null}
            description={
              <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
                当前没有其他待办。
              </Typography.Text>
            }
            style={{ margin: 0 }}
          />
        ) : (
          <Space size={[6, 6]} wrap>
            {secondary.map((code) => (
              <Tooltip key={code} title="查看这一步的待办">
                <Tag
                  style={{ cursor: 'pointer', marginInlineEnd: 0 }}
                  onClick={() => navigate(`/workbench?next_action=${code}`)}
                >
                  {NEXT_ACTION_LABEL[code]} {byAction?.[code]}
                </Tag>
              </Tooltip>
            ))}
          </Space>
        )}
      </Card>

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space size={16} wrap style={{ fontSize: fontScale.body }}>
          <Space size={6}>
            <ProfileOutlined style={{ color: brandVars.slate }} />
            <Typography.Text type="secondary">
              共 {board.data?.summary.total ?? '—'} 件商品 · 已完成{' '}
              {board.data?.summary.done ?? '—'} 件 · 有问题待处理{' '}
              {board.data?.summary.blocked ?? '—'} 件
            </Typography.Text>
          </Space>
          <Typography.Link onClick={() => navigate('/workbench')}>
            查看全部商品
          </Typography.Link>
          <Typography.Link onClick={() => navigate('/workbench-exceptions')}>
            查看异常与平台驳回
          </Typography.Link>
        </Space>
      </Card>
    </Space>
  )
}
