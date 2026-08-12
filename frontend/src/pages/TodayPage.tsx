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
 * ## 为什么是八张卡而不是计划里的七张
 *
 * A9 点名七类。落地时多了一张「候选图待审」:`/reviews` 那条队列(评分落到 C 档、
 * 或 A 档抽检抽中)同样在等人处理,而它**不在** `next_action` 体系里 ——
 * 七张卡一张都盖不住它。首页一处都不提的话,任务停在 MANUAL_REVIEW 就没人管,
 * 而运营看完首页的结论仍然是"今天的活都干完了"。
 *
 * ## 数字全部来自后端
 *
 * `by_next_action` 由 `flow.summarize` 给,`failed` / `in_flight` 由
 * `/dashboard/summary` 给。这一页**一个加号都没有** —— §3.2.1 与 §6.3.2 定过
 * 两次的纪律:前端不推算状态、不重算验收数字,否则验收会上会出现两个数,
 * 然后花半小时争论信哪个。所以「其余待办」那一行只列每个码各自的数,
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
import { Card, Empty, Skeleton, Space, Tag, Tooltip, Typography } from 'antd'
import {
  ClockCircleOutlined, ExportOutlined, EyeOutlined, FileTextOutlined, LoadingOutlined, PictureOutlined,
  ProfileOutlined, StopOutlined, TagsOutlined, WarningOutlined,
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
  /** 一句话说清"这是什么、为什么要我处理" */
  hint: string
  tone: Tone
}

const TONE_COLOR: Record<Tone, string> = {
  act: brandVars.marine,
  wait: brandVars.slate,
  idle: brandVars.textMuted,
}

/**
 * 首页七张卡片之外的动作码。
 *
 * A9 点名的是七张卡,但动作码有十七个 —— 卡在「补充素材」「生成文案」
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
        <Space size={6} style={{ color: brandVars.slate, fontSize: fontScale.body }}>
          {spec.icon}
          {spec.label}
        </Space>
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
        <Typography.Text type="secondary" style={{ fontSize: fontScale.meta, lineHeight: 1.5 }}>
          {spec.hint}
        </Typography.Text>
      </Space>
    </Card>
  )
}

function RunningTaskIcon({ count }: { count: number | undefined }) {
  // LoadingOutlined 自带无限旋转。没有运行中任务时仍然用它，会让首页看起来像
  // 一直没有加载完；只有后端明确报告有运行中任务时，旋转才表达真实状态。
  return count !== undefined && count > 0 ? <LoadingOutlined /> : <ClockCircleOutlined />
}

export default function TodayPage() {

  const navigate = useNavigate()

  // page_size=1:只要顶部那份 summary。它统计的是全部匹配商品,与分页无关,
  // 所以不必把 20 条明细一起拉回来
  const board = useQuery({
    queryKey: ['today-board'],
    queryFn: () => workbenchApi.list({ page: 1, page_size: 1 }),
  })

  // 生成任务的两个数在另一个接口里。刻意不为首页新开一个"待办计数"接口:
  // 那会变成第三处定义"什么算待办"的地方
  const tasks = useQuery({
    queryKey: ['today-tasks'],
    queryFn: () => dashboardApi.summary(),
  })

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
        label: '待确认受众',
        icon: <TagsOutlined />,
        count: byAction?.CONFIRM_AUDIENCE,
        to: '/workbench?next_action=CONFIRM_AUDIENCE',
        hint: '受众决定模特候选集、检查项与导出模板。它排在选模特之前',
        tone: 'act',
      },
      {
        key: 'CONFIRM_ATTRIBUTES',
        label: '待确认属性',
        icon: <TagsOutlined />,
        count: byAction?.CONFIRM_ATTRIBUTES,
        to: '/workbench?next_action=CONFIRM_ATTRIBUTES',
        hint: '识别结果已出,等人确认后才能编排图片集',
        tone: 'act',
      },
      {
        key: 'APPROVE_IMAGE_SET',
        label: '待审核图片',
        icon: <PictureOutlined />,
        count: byAction?.APPROVE_IMAGE_SET,
        to: '/workbench-review?filter=APPROVE_IMAGE_SET',
        hint: '进逐件快审,一屏一件,批完自动下一件',
        tone: 'act',
      },
      {
        key: 'APPROVE_COPY',
        label: '待审核文案',
        icon: <FileTextOutlined />,
        count: byAction?.APPROVE_COPY,
        to: '/workbench-review?filter=APPROVE_COPY',
        hint: '标题、卖点、描述全文一屏,含葡语校验问题',
        tone: 'act',
      },
      {
        key: 'FAILED',
        label: '生成失败',
        icon: <StopOutlined />,
        count: tasks.data?.tasks.failed,
        to: '/tasks?status=FAILED',
        hint: '任务跑挂了。先看错误码再决定重试还是换 Provider',
        tone: 'act',
      },
      {
        key: 'RESOLVE_REJECTION',
        label: '平台驳回',
        icon: <WarningOutlined />,
        count: byAction?.RESOLVE_REJECTION,
        to: '/workbench?next_action=RESOLVE_REJECTION',
        hint: '平台退回的件。改完要重新导出才算处理完',
        tone: 'act',
      },
      {
        key: 'EXPORT',
        label: '可导出',
        icon: <ExportOutlined />,
        count: byAction?.EXPORT,
        to: '/workbench?next_action=EXPORT',
        hint: '五步齐全、草稿校验通过,等人点导出',
        tone: 'act',
      },
      {
        key: 'MANUAL_REVIEW',
        label: '候选图待审',
        icon: <EyeOutlined />,
        count: tasks.data?.reviews.pending,
        to: '/reviews',
        hint: 'C 档或抽检落到人工的任务。审的是单张候选图,不是整个图片集',
        tone: 'act',
      },
      {
        key: 'IN_FLIGHT',
        label: '运行中任务',
        icon: <RunningTaskIcon count={tasks.data?.tasks.in_flight} />,
        count: tasks.data?.tasks.in_flight,
        to: '/tasks',
        hint: '在等 Provider 出图,不用做什么。点开是全部任务列表',
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
   * 七张「要动手」的卡片跨**两个接口**。所以后端补一个 `pending_total` 也补不齐:
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
      <PageHeader title="今日工作" subtitle="每个数字都是一次筛选,点进去就是筛好的列表" />

      {failure && (
        <ErrorNotice
          title="读不到今天的待办"
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

      <Card size="small" title="其余待办" styles={{ body: { padding: 12 } }}>
        {secondary.length === 0 ? (
          <Empty
            image={null}
            description={
              <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
                上面七类之外没有别的待办。
              </Typography.Text>
            }
            style={{ margin: 0 }}
          />
        ) : (
          <Space size={[6, 6]} wrap>
            {secondary.map((code) => (
              <Tooltip key={code} title="点开工作台,已按这一步筛好">
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
              商品总数 {board.data?.summary.total ?? '—'} · 已完成{' '}
              {board.data?.summary.done ?? '—'} · 有阻断{' '}
              {board.data?.summary.blocked ?? '—'}
            </Typography.Text>
          </Space>
          <Typography.Link onClick={() => navigate('/workbench')}>
            打开完整工作台
          </Typography.Link>
          <Typography.Link onClick={() => navigate('/workbench-exceptions')}>
            按问题看异常与驳回
          </Typography.Link>
        </Space>
      </Card>
    </Space>
  )
}
